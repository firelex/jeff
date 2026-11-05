"""jeff_server.router.EngineRouter: one jeff.engine.Engine over several SocketEngine processes, chosen per row by
hashing its binding (the adapter, its question, and the part of its state that stays fixed across calls sharing
that binding -- see jeff.binding.build_row's live-last layout, where the one field that varies call to call, the
input, is always the LAST key `state` gets)."""

import hashlib
import json
import time
from pathlib import Path
from typing import Any

import pytest

from jeff.engine import EngineResult
from jeff.errors import DeadlineExceeded, EngineError, InvalidRequest
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine, fake_tokens
from jeff_runtime.testing import Running

from jeff_server.router import EngineRouter

ADAPTER = "a.b.c"
QUESTION = {"type": "noul", "instructions": "?"}

# Brute-forced for ADAPTER and QUESTION above: among the sha256-based engine indices (0 or 1, for 2 engines) that
# adapter_id + json(question) + json({"ctx": ctx}) produces, "ctx0" happens to land on index 0 and "ctx1" on
# index 1 -- so tests below can assert exactly which engine answers a row, instead of just hoping for spread.
CTX_FOR_ENGINE = {0: "ctx0", 1: "ctx1"}

# Brute-forced the same way, but varying `question` (its "instructions" text) while holding the fixed state
# ({"ctx": "samectx"}) constant: "q0" lands on index 0, "q4" on index 1.
QUESTION_FOR_ENGINE = {0: {"type": "noul", "instructions": "q0"}, 1: {"type": "noul", "instructions": "q4"}}


def _expected_engine_index(num_engines: int, adapter_id: str, question: dict[str, Any],
                            fixed_state: dict[str, Any]) -> int:
    payload = adapter_id + json.dumps(question) + json.dumps(fixed_state)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return int(digest, 16) % num_engines


def _row(ctx: str, n: int) -> dict[str, Any]:
    # "n" is inserted last: it is the varying input: build_row's live-last layout. "ctx" (inserted first) is the
    # fixed context that the binding key is computed from.
    return {"state": {"ctx": ctx, "n": n}, "question": dict(QUESTION)}


def _wait_until_disconnected(engine: SocketEngine, timeout: float = 2.0) -> None:
    """Polls a real condition instead of sleeping a fixed, guessed duration: SocketEngine._closed is set (from
    None to the error that killed the connection) once the reader thread has actually noticed the far end close
    -- see SocketEngine._shutdown. Bounded so a genuine bug (the connection never dies) fails the test instead of
    hanging it."""
    deadline = time.monotonic() + timeout
    while engine._closed is None:
        if time.monotonic() >= deadline:
            raise AssertionError(f"{engine.path} did not notice its connection close within {timeout}s")
        time.sleep(0.005)


class _MarkedEngine(FakeEngine):
    """A FakeEngine that always answers [1.0, 0.0] if it is "left" or [0.0, 1.0] if it is "right", regardless of
    the row -- so a test can tell which physical engine actually answered a given row -- reports a chosen
    model_ms in its timing instead of FakeEngine's fixed 0.0 (so the merge logic, `model_ms` = max, is exercised
    for real), and optionally sleeps `delay` seconds before answering (so a test can prove two engines are called
    concurrently rather than one after another)."""

    def __init__(self, mark: float, installed: set[str], model_ms: float, delay: float = 0.0) -> None:
        super().__init__(lambda adapter, row: [1.0, 0.0] if mark == 1.0 else [0.0, 1.0], installed)
        self.model_ms = model_ms
        self.delay = delay

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        if self.delay:
            time.sleep(self.delay)
        result = super().predict(adapter_id, rows)
        return EngineResult(result.probabilities, {"path": "fake", "model_ms": self.model_ms})


@pytest.fixture
def pair():
    """Two engine processes (Running + SocketEngine), index 0 answers [1.0, 0.0] with model_ms=5.0, index 1
    answers [0.0, 1.0] with model_ms=12.0. Both serve the same adapter."""
    server_0 = Running(_MarkedEngine(1.0, {ADAPTER}, 5.0), fixed={ADAPTER: Path("/x")})
    server_1 = Running(_MarkedEngine(0.0, {ADAPTER}, 12.0), fixed={ADAPTER: Path("/x")})
    engine_0 = SocketEngine(server_0.path, timeout_s=5)
    engine_1 = SocketEngine(server_1.path, timeout_s=5)
    try:
        yield server_0, server_1, engine_0, engine_1
    finally:
        engine_0.close()
        engine_1.close()
        server_0.close()
        server_1.close()


def test_binding_assumptions_hold_for_the_fixtures_used_below() -> None:
    """Pins down the brute-forced CTX_FOR_ENGINE values against the actual hashing rule, so a change to that rule
    fails loudly here instead of making the tests below pass for the wrong reason."""
    for index, ctx in CTX_FOR_ENGINE.items():
        assert _expected_engine_index(2, ADAPTER, QUESTION, {"ctx": ctx}) == index


def test_question_only_difference_assumption_holds_for_the_fixture_used_below() -> None:
    for index, question in QUESTION_FOR_ENGINE.items():
        assert _expected_engine_index(2, ADAPTER, question, {"ctx": "samectx"}) == index


def test_same_binding_always_lands_on_the_same_engine(pair) -> None:
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    rows = [_row("ctx0", n) for n in range(5)]  # same ctx => same fixed state => same binding, varying input only
    result = router.predict(ADAPTER, rows)
    assert result.probabilities == [[1.0, 0.0]] * 5  # every one of them answered by engine 0


def test_different_bindings_spread_across_engines_and_keep_row_order(pair) -> None:
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    # Interleaved on purpose: binding-key routing must not depend on send order, and the reply must come back in
    # the same order as the request regardless of which engine answered which row.
    rows = [_row("ctx1", 1), _row("ctx0", 2), _row("ctx1", 3), _row("ctx0", 4), _row("ctx0", 5)]
    result = router.predict(ADAPTER, rows)
    assert result.probabilities == [[0.0, 1.0], [1.0, 0.0], [0.0, 1.0], [1.0, 0.0], [1.0, 0.0]]


def test_only_the_question_differing_still_changes_the_engine(pair) -> None:
    """Same adapter, same fixed state ({"ctx": "samectx"}) -- only `question` differs between these two rows --
    yet the binding key folds `question` in too
    (sha256(adapter_id + json(question) + json(state without its last field))), so they must still route to
    different engines. This guards against an implementation that silently drops `question` from the key (both
    rows would then collide on the state-only hash) or serialises it in a way that disagrees with the split
    pinned by test_question_only_difference_assumption_holds_for_the_fixture_used_below (e.g. sorting its keys)."""
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    row_0 = {"state": {"ctx": "samectx", "n": 1}, "question": QUESTION_FOR_ENGINE[0]}
    row_1 = {"state": {"ctx": "samectx", "n": 1}, "question": QUESTION_FOR_ENGINE[1]}
    result = router.predict(ADAPTER, [row_0, row_1])
    assert result.probabilities == [[1.0, 0.0], [0.0, 1.0]]


def test_non_object_state_is_accepted_and_binds_on_adapter_and_question_only(pair) -> None:
    """A plain string or a list state is accepted on purpose: jeff_runtime's decision_messages
    (runtime/python/src/jeff_runtime/engine/model.py) always puts a non-object state "state-first" regardless of
    the configured layout, since there is no trailing field to carve a fixed part out of a string or a list. So
    the binding key for such a row is just sha256(adapter_id + json(question)) -- state plays no part in it at
    all. Three rows below share one question but have wildly different string/list state content; because state
    is not folded into the key, all three must still land on the very same engine -- proving both that a
    non-object state does not get rejected, and that it is routed on adapter+question alone."""
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    str_row_a = {"state": "a short transcript", "question": dict(QUESTION)}
    str_row_b = {"state": "a completely different, much longer transcript altogether", "question": dict(QUESTION)}
    list_row = {"state": ["first line", "second line", 3], "question": dict(QUESTION)}
    result = router.predict(ADAPTER, [str_row_a, str_row_b, list_row])
    assert result.probabilities == [result.probabilities[0]] * 3  # all three answered by the very same engine


def test_non_object_state_content_never_changes_which_engine_answers(pair) -> None:
    """Same binding (same adapter, same question) called twice with totally different non-object state content:
    both calls must land on the same engine, since state plays no part in a non-object binding key at all."""
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    first = router.predict(ADAPTER, [{"state": "alpha", "question": dict(QUESTION)}])
    second = router.predict(ADAPTER, [{"state": ["omega", "omega", "omega"], "question": dict(QUESTION)}])
    assert first.probabilities == second.probabilities


def test_timing_is_merged_as_max_model_ms_plus_the_engine_indexes_used(pair) -> None:
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    rows = [_row("ctx0", 1), _row("ctx1", 2)]
    result = router.predict(ADAPTER, rows)
    assert result.timing == {"model_ms": 12.0, "engines": [0, 1], "format": "bf16"}


def test_timing_only_names_the_engines_actually_used(pair) -> None:
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    result = router.predict(ADAPTER, [_row("ctx0", 1)])
    assert result.timing == {"model_ms": 5.0, "engines": [0], "format": "bf16"}


def test_deadline_ms_reaches_the_engine_for_rows_spread_over_both_engines(pair) -> None:
    """deadline_ms is not just accepted and silently dropped by the router: it must reach each engine it calls.
    jeff_runtime.testing.Running's scheduler is given window_ms=20 (it waits up to 20ms for a batch to fill
    before looking at anything queued), so a deadline far shorter than that (1ms) is certain to have already
    passed by the time either engine's scheduler gets to it -- for a row on EACH engine in the very same
    predict() call, proving the deadline reaches both groups, not just the one dispatched first. If deadline_ms
    were silently dropped on the way to SocketEngine.predict(), request.deadline would stay None and the
    scheduler would just answer normally -- no DeadlineExceeded at all."""
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    with pytest.raises(DeadlineExceeded):
        router.predict(ADAPTER, [_row("ctx0", 1), _row("ctx1", 2)], deadline_ms=1)


def test_groups_are_dispatched_to_both_engines_concurrently_not_one_after_another() -> None:
    server_0 = Running(_MarkedEngine(1.0, {ADAPTER}, 0.0, delay=0.2), fixed={ADAPTER: Path("/x")})
    server_1 = Running(_MarkedEngine(0.0, {ADAPTER}, 0.0, delay=0.2), fixed={ADAPTER: Path("/x")})
    engine_0, engine_1 = SocketEngine(server_0.path, timeout_s=5), SocketEngine(server_1.path, timeout_s=5)
    try:
        router = EngineRouter([engine_0, engine_1])
        started = time.monotonic()
        result = router.predict(ADAPTER, [_row("ctx0", 1), _row("ctx1", 2)])
        elapsed = time.monotonic() - started
        # Sequentially, two 0.2s engine calls would take >= 0.4s; dispatched concurrently (one thread per engine)
        # they overlap almost entirely, so the whole call should finish well under the sum.
        assert elapsed < 0.35, f"expected the two 0.2s engine calls to overlap (<0.35s total), took {elapsed:.3f}s"
        assert result.probabilities == [[1.0, 0.0], [0.0, 1.0]]
    finally:
        engine_0.close()
        engine_1.close()
        server_0.close()
        server_1.close()


def test_one_engine_dying_gives_engine_error_only_for_calls_routed_there(pair) -> None:
    server_0, server_1, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    server_1.close()  # kills the process behind engine_1 ("ctx1" rows route to index 1)
    _wait_until_disconnected(engine_1)
    with pytest.raises(EngineError, match="reconnect attempt"):  # nothing listens at engine_1's socket any more
        router.predict(ADAPTER, [_row("ctx1", 1)])
    # nothing else: a separate call, routed entirely to the still-healthy engine 0, still succeeds.
    result = router.predict(ADAPTER, [_row("ctx0", 1)])
    assert result.probabilities == [[1.0, 0.0]]


def test_one_engine_dying_fails_the_whole_call_even_if_another_group_in_it_would_have_succeeded(pair) -> None:
    """A single predict() call with rows for both engines, one of which is dead: the brief says the whole call
    raises that error, not a partial result -- even though the other group's engine is healthy."""
    server_0, server_1, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    server_1.close()
    _wait_until_disconnected(engine_1)
    with pytest.raises(EngineError, match="reconnect attempt"):  # nothing listens at engine_1's socket any more
        router.predict(ADAPTER, [_row("ctx0", 1), _row("ctx1", 2)])


def test_mismatched_installed_sets_raise_naming_each_engines_set_and_a_served_check_gets_the_intersection() -> None:
    server_a = Running(FakeEngine(lambda a, r: [1.0, 0.0], {"a.b.c"}), fixed={"a.b.c": Path("/x")})
    server_b = Running(FakeEngine(lambda a, r: [1.0, 0.0], {"a.b.d"}), fixed={"a.b.d": Path("/y")})
    engine_a, engine_b = SocketEngine(server_a.path, timeout_s=5), SocketEngine(server_b.path, timeout_s=5)
    try:
        router = EngineRouter([engine_a, engine_b])
        with pytest.raises(EngineError) as excinfo:
            router.installed(refresh=True)
        message = str(excinfo.value)
        assert "a.b.c" in message and "a.b.d" in message
        assert router.disagreement() == message
        assert router.installed() == set()  # what every engine serves: nothing in common
    finally:
        engine_a.close()
        engine_b.close()
        server_a.close()
        server_b.close()


def test_installed_returns_the_common_set_when_engines_agree(pair) -> None:
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    assert router.installed() == {ADAPTER}


def test_installed_refresh_is_passed_through_to_each_engine() -> None:
    """EngineRouter.installed(refresh=True) must not just return its own default -- it has to pass refresh=True
    on to every engine it wraps. Two SocketEngine connections to the SAME server (mirroring
    test_socket_engine.py's own installed-refresh test): `other` changes what the server serves behind `mine`'s
    back, so `mine`'s cache is stale until asked to refresh."""
    server = Running(FakeEngine(lambda a, r: [1.0, 0.0], {ADAPTER}),
                     fixed={ADAPTER: Path("/x"), "a.b.d": Path("/y")})
    mine = SocketEngine(server.path, timeout_s=5)
    other = SocketEngine(server.path, timeout_s=5)
    try:
        router = EngineRouter([mine])
        other.serve(["a.b.d"])
        assert router.installed() == {ADAPTER}  # mine's cache, not yet refreshed
        assert router.installed(refresh=True) == {"a.b.d"}
        assert router.installed() == {"a.b.d"}
    finally:
        mine.close()
        other.close()
        server.close()


def test_engines_attribute_is_the_engines_in_order(pair) -> None:
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    assert router.engines == [engine_0, engine_1]


def test_predict_rejects_a_row_without_state_or_question(pair) -> None:
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    with pytest.raises(InvalidRequest):
        router.predict(ADAPTER, [{"question": QUESTION}])
    with pytest.raises(InvalidRequest):
        router.predict(ADAPTER, [{"state": {"ctx": "ctx0"}}])


def test_predict_rejects_no_rows(pair) -> None:
    _, _, engine_0, engine_1 = pair
    router = EngineRouter([engine_0, engine_1])
    with pytest.raises(InvalidRequest):
        router.predict(ADAPTER, [])


def test_empty_state_and_single_field_state_both_bind_to_an_empty_fixed_state() -> None:
    """A row with an empty object state, and a row whose object state has exactly one field (the input, inserted
    last, with no context before it), both reduce to the same empty fixed state -- the documented behaviour for
    "no state" or "empty state", not a fallback."""
    server_0 = Running(FakeEngine(lambda a, r: [1.0, 0.0], {ADAPTER}), fixed={ADAPTER: Path("/x")})
    server_1 = Running(FakeEngine(lambda a, r: [0.0, 1.0], {ADAPTER}), fixed={ADAPTER: Path("/x")})
    engine_0, engine_1 = SocketEngine(server_0.path, timeout_s=5), SocketEngine(server_1.path, timeout_s=5)
    try:
        router = EngineRouter([engine_0, engine_1])
        no_state_row = {"state": {}, "question": dict(QUESTION)}
        one_field_row = {"state": {"only_input": 1}, "question": dict(QUESTION)}
        first = router.predict(ADAPTER, [no_state_row])
        second = router.predict(ADAPTER, [one_field_row])
        assert first.probabilities == second.probabilities  # same (empty) binding key -> the same engine both times
    finally:
        engine_0.close()
        engine_1.close()
        server_0.close()
        server_1.close()


def test_input_tokens_are_reassembled_in_row_order_across_engines() -> None:
    servers = [Running(FakeEngine(lambda adapter, row: [0.5, 0.5], {ADAPTER}), fixed={ADAPTER: Path("/x")})
               for _ in range(2)]
    engines = [SocketEngine(server.path, timeout_s=5) for server in servers]
    try:
        rows = [_row("ctx0", 1), _row("ctx1", 22), _row("ctx0", 333)]
        result = EngineRouter(engines).predict(ADAPTER, rows)
    finally:
        for engine in engines:
            engine.close()
        for server in servers:
            server.close()
    assert result.timing["engines"] == [0, 1]
    assert result.input_tokens == [fake_tokens(row) for row in rows]


def test_results_without_input_tokens_merge_to_none_and_a_mix_is_an_engine_error(pair) -> None:
    server_0, _, engine_0, engine_1 = pair  # _MarkedEngine answers without input tokens
    router = EngineRouter([engine_0, engine_1])
    assert router.predict(ADAPTER, [_row("ctx0", 1), _row("ctx1", 2)]).input_tokens is None
    tokenised = Running(FakeEngine(lambda adapter, row: [0.5, 0.5], {ADAPTER}), fixed={ADAPTER: Path("/x")})
    engine_t = SocketEngine(tokenised.path, timeout_s=5)
    try:
        with pytest.raises(EngineError, match="input token counts"):
            EngineRouter([engine_0, engine_t]).predict(ADAPTER, [_row("ctx0", 1), _row("ctx1", 2)])
    finally:
        engine_t.close()
        tokenised.close()


def test_engines_serving_different_base_formats_are_refused() -> None:
    """A row may go to any engine and a function's calibration is chosen for one base format, so every engine of a
    deployment must serve the same one."""
    int4 = {"format": "int4-g64", "format_id": "jeff-base@v1.3+int4-g64-gptq.0123abcd"}
    servers = [Running(FakeEngine(lambda adapter, row: [0.5, 0.5], {ADAPTER}), fixed={ADAPTER: Path("/x")}),
               Running(FakeEngine(lambda adapter, row: [0.5, 0.5], {ADAPTER}, base_format=int4),
                       fixed={ADAPTER: Path("/x")})]
    engines = [SocketEngine(server.path, timeout_s=5) for server in servers]
    try:
        with pytest.raises(EngineError, match="different formats.*bf16.*int4-g64"):
            EngineRouter(engines)
        assert EngineRouter(engines[1:]).base_format == int4
    finally:
        for engine in engines:
            engine.close()
        for server in servers:
            server.close()


def test_merged_timing_carries_the_base_format_as_an_engines_own_timing_does() -> None:
    """The socket engine's and the local engine's timing name the base format they serve; the router's merged timing
    names the deployment's one format (every engine serves the same, checked at construction) the same way."""
    int4 = {"format": "int4-g64", "format_id": "jeff-base@v1.3+int4-g64-gptq.0123abcd"}
    servers = [Running(FakeEngine(lambda adapter, row: [0.5, 0.5], {ADAPTER}, base_format=int4),
                       fixed={ADAPTER: Path("/x")}) for _ in range(2)]
    engines = [SocketEngine(server.path, timeout_s=5) for server in servers]
    try:
        rows = [_row("ctx0", 1), _row("ctx1", 22), _row("ctx0", 333)]
        alone = engines[0].predict(ADAPTER, rows[:1]).timing
        result = EngineRouter(engines).predict(ADAPTER, rows)
    finally:
        for engine in engines:
            engine.close()
        for server in servers:
            server.close()
    assert (alone["format"], alone["format_id"]) == (int4["format"], int4["format_id"])
    assert result.timing == {"model_ms": 0.0, "engines": [0, 1], **int4}
