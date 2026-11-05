"""Usage in accounts mode: each call's input tokens (from the engine's replies) are added to the account's total for
its billing period; at the allowance the next call is refused with UsageLimitReached (402) before the engine sees it,
while a call that started under it runs to its end; a reply without input_tokens is refused, never guessed; totals
survive a restart; a new period starts from 0.

Every door that reaches the engine is metered: /v1/decide (calls, batches, dispatch, graphs), /v1/systemone, the
channel (decide frames and calls on a handle) and the MCP tools. No other route calls the engine's predict."""

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.engine import EngineResult
from jeff.errors import EngineError, UsageLimitReached
from jeff.testing import FakeEngine, fake_tokens

from jeff_server.accounts import Account, MeteredEngine, Meter, in_force
from jeff_server.auth import ALL_ADAPTERS, Caller
from jeff_server.decide import caller_view
from jeff_server.usage import UsageBook

from accounts_testing import (CATALOGUE, DEC, KEY, NOV, OWNER_KEY, PLAYGROUND_KEY, ROOT, SPAM_CALL, Hub,
                              accounts_app, answers, bearer, fake_engine, key_list, serving)
from channel_testing import start

JAN = "2027-01-03T10:00:00Z"
TRIAGE_CALL = {"function": "triage.urgency", "input": "sure",
               "context": {"company": "A furniture shop.", "channel": "email"}}
ROUTERS = ROOT / "packages" / "python" / "tests" / "fixtures" / "routers"
ROUTED = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
SYSTEMONE = {"model": "jeff", "state": "Book me a flight.",
             "questions": {"q": {"type": "choice", "instructions": "Which destination?",
                                 "criteria": {"Paris": None, "Rome": "sunny"}}}}


def decide(http, headers):  # type: ignore[no-untyped-def]
    return http.post("/v1/decide", json={"calls": [SPAM_CALL]}, headers=headers)


def tokens_sent(engine: FakeEngine) -> int:
    return sum(fake_tokens(row) for _, rows in engine.calls for row in rows)


def rows_sent(engine: FakeEngine) -> int:
    return sum(len(rows) for _, rows in engine.calls)


def test_a_calls_input_tokens_are_added_to_its_account(tmp_path: Path) -> None:
    engine = fake_engine()
    app = accounts_app(tmp_path, Hub(), engine=engine)
    with serving(app) as http:
        assert decide(http, bearer(KEY)).status_code == 200
        assert decide(http, bearer(KEY)).status_code == 200
        assert app.state.accounts.book.total("acct1", NOV) == tokens_sent(engine) > 0
        assert decide(http, bearer(OWNER_KEY)).status_code == 200  # the owner is never counted
        assert app.state.accounts.book.total("acct1", NOV) < tokens_sent(engine)


def test_at_the_allowance_the_next_call_is_refused_before_the_engine_sees_it(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    engine = fake_engine()
    app = accounts_app(tmp_path, hub, engine=engine)
    with serving(app) as http:
        crossing = decide(http, bearer(KEY))
        assert crossing.status_code == 200  # started at 0, under 10: runs to its end
        used = app.state.accounts.book.total("acct1", NOV)
        assert used > 10
        calls_before = len(engine.calls)
        refused = decide(http, bearer(KEY))
        assert refused.status_code == 402
        error = refused.json()["error"]
        assert error["type"] == "UsageLimitReached"
        assert error["message"] == ("This account has used its 10 input tokens for the period ending "
                                    "2026-12-03 10:00 UTC; calls resume then")
        assert error["details"] == {"account": "acct1", "used": used, "allowance": 10, "period_end": DEC}
        assert len(engine.calls) == calls_before


def test_a_reply_without_input_tokens_is_refused_and_not_counted(tmp_path: Path) -> None:
    class Untokenised(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            result = super().predict(adapter_id, rows)
            return EngineResult(result.probabilities, result.timing, None)

    app = accounts_app(tmp_path, Hub(), engine=Untokenised(answers, {"jeff.safety.spam", "jeff.support.triage"}))
    with serving(app) as http:
        response = decide(http, bearer(KEY))
        assert response.status_code == 500
        assert response.json()["error"]["type"] == "EngineError"
        assert "carries no input_tokens, so the call cannot be counted" in response.json()["error"]["message"]
        assert app.state.accounts.book.total("acct1", NOV) == 0
        assert decide(http, bearer(OWNER_KEY)).status_code == 200  # the owner's calls need no count


def test_a_restart_keeps_the_totals_and_the_refusal(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    with serving(accounts_app(tmp_path, hub)) as http:
        assert decide(http, bearer(KEY)).status_code == 200
    with serving(accounts_app(tmp_path, hub)) as http:
        assert decide(http, bearer(KEY)).status_code == 402


def test_a_new_period_starts_from_zero_and_the_old_total_is_still_reported(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        assert decide(http, bearer(KEY)).status_code == 200
        assert decide(http, bearer(KEY)).status_code == 402
        hub.keys = key_list(allowance=10, period_start=DEC, period_end=JAN)
        app.state.accounts.refresh()
        assert decide(http, bearer(KEY)).status_code == 200
        periods = {entry["period_start"] for entry in app.state.accounts.book.report()["accounts"]
                   if entry["account"] == "acct1"}
        assert periods == {NOV, DEC}


def test_the_playground_is_counted_but_never_refused(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        for _ in range(3):
            assert decide(http, bearer(PLAYGROUND_KEY)).status_code == 200
        assert app.state.accounts.book.total("playground", "2026-11-01T00:00:00Z") > 10
        assert app.state.accounts.book.total("acct1", NOV) == 0


def test_the_playground_for_a_subscriber_counts_against_and_is_refused_at_their_allowance(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    app = accounts_app(tmp_path, hub)
    as_acct1 = bearer(PLAYGROUND_KEY, **{"X-Jeff-Account": "acct1"})
    with serving(app) as http:
        assert decide(http, as_acct1).status_code == 200
        assert app.state.accounts.book.total("acct1", NOV) > 10
        assert app.state.accounts.book.total("playground", "2026-11-01T00:00:00Z") == 0
        assert decide(http, as_acct1).status_code == 402
        assert decide(http, bearer(KEY)).status_code == 402  # the same account


def test_systemone_is_metered_too(tmp_path: Path) -> None:
    engine = FakeEngine(answers, {"base", "jeff.safety.spam", "jeff.support.triage"})
    app = accounts_app(tmp_path, Hub(), engine=engine)
    with serving(app) as http:
        response = http.post("/v1/systemone", json=SYSTEMONE, headers=bearer(KEY))
        assert response.status_code == 200, response.text
        assert app.state.accounts.book.total("acct1", NOV) == response.json()["usage"]["input_tokens"]


def test_systemone_is_refused_at_the_allowance_before_the_engine_sees_it(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    engine = FakeEngine(answers, {"base", "jeff.safety.spam", "jeff.support.triage"})
    app = accounts_app(tmp_path, hub, engine=engine)
    with serving(app) as http:
        assert http.post("/v1/systemone", json=SYSTEMONE, headers=bearer(KEY)).status_code == 200
        calls_before = len(engine.calls)
        refused = http.post("/v1/systemone", json=SYSTEMONE, headers=bearer(KEY))
        assert refused.status_code == 402, refused.text
        assert refused.json()["error"]["type"] == "UsageLimitReached"
        assert refused.json()["error"]["details"]["account"] == "acct1"
        assert len(engine.calls) == calls_before


def test_a_batch_counts_every_row(tmp_path: Path) -> None:
    engine = fake_engine()
    app = accounts_app(tmp_path, Hub(), engine=engine)
    calls = [{**SPAM_CALL, "input": f"message number {index}"} for index in range(5)]
    with serving(app) as http:
        response = http.post("/v1/decide", json={"calls": calls}, headers=bearer(KEY))
        assert response.status_code == 200, response.text
        assert len(response.json()["decisions"]) == 5
        assert rows_sent(engine) == 5
        assert app.state.accounts.book.total("acct1", NOV) == tokens_sent(engine)


def test_a_dispatch_counts_every_router_and_the_leaf(tmp_path: Path) -> None:
    engine = FakeEngine(answers, set(ROUTED))
    app = accounts_app(tmp_path, Hub(), engine=engine, catalogue=ROUTERS)
    body = {"dispatch": {"input": "My wardrobe is broken", "interface": "rate",
                         "context": {"company": "A furniture shop.", "channel": "email"}}}
    with serving(app) as http:
        response = http.post("/v1/decide", json=body, headers=bearer(KEY))
        assert response.status_code == 200, response.text
        assert len(engine.calls) == 3  # two routers, then the leaf
        assert app.state.accounts.book.total("acct1", NOV) == tokens_sent(engine)


def test_a_graph_run_on_the_server_counts_every_node(tmp_path: Path) -> None:
    engine = fake_engine()
    app = accounts_app(tmp_path, Hub(), engine=engine)
    nodes = {f"n{index}": {"call": "jeff.safety.spam.message_type", "context": {"channel": {"param": "channel"}}}
             for index in range(3)}
    graph = {"graph": "three", "version": "1.0.0", "input": "message", "params": ["channel"], "nodes": nodes,
             "outputs": ["n0", "n1", "n2"]}
    with serving(app) as http:
        response = http.post("/v1/decide", json={"graph": {"inline": graph}, "input": "WIN NOW",
                                                 "params": {"channel": "email"}}, headers=bearer(KEY))
        assert response.status_code == 200, response.text
        assert rows_sent(engine) == 3
        assert app.state.accounts.book.total("acct1", NOV) == tokens_sent(engine)


def test_the_channel_counts_each_frame_and_refuses_one_past_the_allowance(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    engine = fake_engine()
    app = accounts_app(tmp_path, hub, engine=engine)
    with serving(app) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, KEY)
        channel.result({"op": "decide", "id": 2, "calls": [SPAM_CALL]})
        used = app.state.accounts.book.total("acct1", NOV)
        assert used == tokens_sent(engine) > 10  # started at 0: runs to its end
        calls_before = len(engine.calls)
        # Past the allowance, a bind still answers: it asks the engine nothing.
        bound = channel.result({"op": "bind", "id": 3, "function": SPAM_CALL["function"],
                                "context": SPAM_CALL["context"]})
        error = channel.error({"op": "call", "id": 4, "handle": bound["handle"], "input": "WIN NOW"},
                              "UsageLimitReached", "has used its 10 input tokens")
        assert error["details"] == {"account": "acct1", "used": used, "allowance": 10, "period_end": DEC}
        channel.error({"op": "decide", "id": 5, "calls": [SPAM_CALL]}, "UsageLimitReached")
        assert len(engine.calls) == calls_before
        # The channel stays open; a new period (the key list refreshed) admits the next frame.
        hub.keys = key_list(allowance=10, period_start=DEC, period_end=JAN)
        app.state.accounts.refresh()
        channel.result({"op": "decide", "id": 6, "calls": [SPAM_CALL]})
        assert app.state.accounts.book.total("acct1", DEC) > 0
        assert app.state.accounts.book.total("acct1", NOV) == used


def test_the_channel_counts_a_call_many_frame_row_by_row(tmp_path: Path) -> None:
    engine = fake_engine()
    app = accounts_app(tmp_path, Hub(), engine=engine)
    with serving(app) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, KEY)
        bound = channel.result({"op": "bind", "id": 2, "function": SPAM_CALL["function"],
                                "context": SPAM_CALL["context"]})
        channel.result({"op": "call_many", "id": 3, "handle": bound["handle"], "inputs": ["a", "bb", "ccc"]})
        assert rows_sent(engine) == 3
        assert app.state.accounts.book.total("acct1", NOV) == tokens_sent(engine)


def _mcp_call(http: Any, headers: dict[str, str]) -> dict[str, Any]:
    body = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
            "params": {"name": "safety_spam_message_type",
                       "arguments": {"input": "WIN NOW", "context": {"channel": "email"}}}}
    response = http.post("/mcp", json=body, headers={**headers, "Accept": "application/json, text/event-stream"})
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        data = [line[len("data:"):] for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])["result"]  # type: ignore[no-any-return]
    return response.json()["result"]  # type: ignore[no-any-return]


def test_mcp_tool_calls_are_counted_and_refused_past_the_allowance(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    engine = fake_engine()
    app = accounts_app(tmp_path, hub, engine=engine)
    with serving(app) as http:
        called = _mcp_call(http, bearer(KEY))
        assert not called.get("isError"), called
        used = app.state.accounts.book.total("acct1", NOV)
        assert used == tokens_sent(engine) > 10
        calls_before = len(engine.calls)
        refused = _mcp_call(http, bearer(KEY))
        assert refused["isError"] is True
        assert refused["content"][0]["text"].startswith("UsageLimitReached: ")
        assert len(engine.calls) == calls_before
        assert app.state.accounts.book.total("acct1", NOV) == used


def test_caller_view_meters_every_door_that_plans(tmp_path: Path) -> None:
    """The channel and the MCP tools build their per-request Jeff with caller_view, as /v1/decide does. Past the
    allowance a view is still built (a listing or a bind needs no model); its first engine request is refused."""
    book = UsageBook(tmp_path / "usage.jsonl")
    account = Account("acct1", 10, NOV, DEC)
    caller = Caller("account", "acct1", ALL_ADAPTERS, meter=Meter(book, account, None, lambda: datetime.now(UTC)))
    engine = fake_engine()
    shared = Jeff(engine, Catalogue.from_dir(CATALOGUE))
    view = caller_view(shared, caller)
    view.function("spam.message_type", context={"channel": "email"})("WIN NOW")
    assert book.total("acct1", NOV) > 10
    assert not isinstance(shared.engine, MeteredEngine)  # the shared Jeff is never changed
    calls_before = len(engine.calls)
    bound = caller_view(shared, caller).function("spam.message_type", context={"channel": "email"})
    with pytest.raises(UsageLimitReached) as refused:
        bound("WIN NOW")
    assert refused.value.account == "acct1" and refused.value.allowance == 10
    assert len(engine.calls) == calls_before


def test_a_metered_view_refuses_the_engine_methods_that_would_skip_the_count(tmp_path: Path) -> None:
    meter = Meter(UsageBook(tmp_path / "usage.jsonl"), Account("acct1", 10, NOV, DEC), None,
                  lambda: datetime.now(UTC))
    metered = MeteredEngine(fake_engine(), meter)
    for name in ("predict_many", "prepare"):
        with pytest.raises(AttributeError, match="without counting"):
            getattr(metered, name)


class Gate(FakeEngine):
    """A FakeEngine whose predict waits until `parties` predictions are running at once, so calls race."""

    def __init__(self, parties: int) -> None:
        super().__init__(answers, {"jeff.safety.spam", "jeff.support.triage"})
        self.barrier = threading.Barrier(parties, timeout=10)
        self._lock = threading.Lock()

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        self.barrier.wait()
        with self._lock:
            return super().predict(adapter_id, rows)


def _race(tmp_path: Path, parties: int, allowance: int) -> tuple[UsageBook, Gate, list[BaseException | None]]:
    book = UsageBook(tmp_path / "usage.jsonl")
    account = Account("acct1", allowance, NOV, DEC)
    engine = Gate(parties)
    shared = Jeff(engine, Catalogue.from_dir(CATALOGUE))
    outcomes: list[BaseException | None] = [None] * parties

    def one(index: int) -> None:
        caller = Caller("account", "acct1", ALL_ADAPTERS, meter=Meter(book, account, None, lambda: datetime.now(UTC)))
        try:
            caller_view(shared, caller).function("spam.message_type", context={"channel": "email"})(f"m{index}")
        except BaseException as error:  # noqa: BLE001 - recorded for the test to assert on
            outcomes[index] = error

    threads = [threading.Thread(target=one, args=(index,)) for index in range(parties)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=20)
    return book, engine, outcomes


def test_calls_racing_at_the_boundary_all_run_when_each_started_under_it(tmp_path: Path) -> None:
    """Each call is checked when it starts; both start at 0 (under the allowance of 1), so both run to their end and
    both counts are kept, leaving the total past the allowance. The next call is refused."""
    book, engine, outcomes = _race(tmp_path, 2, allowance=1)
    assert outcomes == [None, None]
    assert book.total("acct1", NOV) == tokens_sent(engine) > 1
    caller = Caller("account", "acct1", ALL_ADAPTERS,
                    meter=Meter(book, Account("acct1", 1, NOV, DEC), None, lambda: datetime.now(UTC)))
    shared = Jeff(fake_engine(), Catalogue.from_dir(CATALOGUE))
    with pytest.raises(UsageLimitReached):
        caller_view(shared, caller).function("spam.message_type", context={"channel": "email"})("next")


def test_no_count_is_lost_when_many_calls_finish_at_once(tmp_path: Path) -> None:
    book, engine, outcomes = _race(tmp_path, 16, allowance=1_000_000)
    assert outcomes == [None] * 16
    assert rows_sent(engine) == 16
    assert book.total("acct1", NOV) == tokens_sent(engine)
    book.close()
    reopened = UsageBook(tmp_path / "usage.jsonl")  # and the file holds the same total
    assert reopened.total("acct1", NOV) == tokens_sent(engine)


def test_every_place_the_server_calls_an_engine_is_known() -> None:
    """The doors above are every way a request reaches the served engine: the server calls predict only in
    MeteredEngine (wrapping the engine), the EngineRouter (under it), /v1/systemone (on the caller's view's engine)
    and the factory's scoring job (its own process and its own LocalEngine, run by the owner's factory, never by a
    request). Every other prediction is the SDK's Jeff deciding: on a view built by caller_view, or, in the owner's
    factory worker (factory/publish.py), on its own Jeff. A new call to an engine fails this test until it is
    metered or shown not to be a request's."""
    source = ROOT / "apps" / "server" / "src" / "jeff_server"
    found = {(path.relative_to(source).as_posix(), line.strip())
             for path in source.rglob("*.py")
             for line in path.read_text(encoding="utf-8").splitlines()
             if any(call in line for call in (".predict(", "predict_many(", ".prepare("))
             and not line.strip().startswith(("#", "`"))}
    assert found == {
        ("accounts.py", "result = self.inner.predict(adapter_id, rows, **options)"),
        ("router.py", "outcomes[engine_index] = engine.predict(adapter_id, [row for _, row in members],"),
        ("systemone.py", "result = engine.predict(target.engine_id, sent)"),
        ("factory/score_job.py", "result = engine.predict(ADAPTER_ID, rows[start:start + CHUNK])"),
    }


# --- fix round 1: the allowance is checked at a request's first engine request ---------------------------------------

def _mcp(http: Any, method: str, params: dict[str, Any], headers: dict[str, str]) -> dict[str, Any]:
    response = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                         headers={**headers, "Accept": "application/json, text/event-stream"})
    assert response.status_code == 200, response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        data = [line[len("data:"):] for line in response.text.splitlines() if line.startswith("data:")]
        return json.loads(data[-1])  # type: ignore[no-any-return]
    return response.json()  # type: ignore[no-any-return]


def _past_the_allowance(tmp_path: Path) -> tuple[Any, FakeEngine, Hub]:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    engine = fake_engine()
    return accounts_app(tmp_path, hub, engine=engine), engine, hub


def test_listing_stays_open_past_the_allowance_on_every_door(tmp_path: Path) -> None:
    """GET /v1/functions, MCP tools/list and the jeff_list_functions tool ask the model nothing, so an account past
    its allowance still gets them; a function tool call is then refused before the engine sees it."""
    app, engine, _ = _past_the_allowance(tmp_path)
    with serving(app) as http:
        assert decide(http, bearer(KEY)).status_code == 200
        assert app.state.accounts.book.total("acct1", NOV) > 10
        calls_before = len(engine.calls)
        assert http.get("/v1/functions", headers=bearer(KEY)).status_code == 200
        listed = _mcp(http, "tools/list", {}, bearer(KEY))
        assert "error" not in listed, listed
        assert "safety_spam_message_type" in {tool["name"] for tool in listed["result"]["tools"]}
        listing = _mcp(http, "tools/call", {"name": "jeff_list_functions", "arguments": {}}, bearer(KEY))["result"]
        assert not listing.get("isError"), listing
        assert "spam.message_type" in listing["content"][0]["text"]
        refused = _mcp(http, "tools/call", {"name": "safety_spam_message_type",
                                            "arguments": {"input": "WIN NOW", "context": {"channel": "email"}}},
                       bearer(KEY))["result"]
        assert refused["isError"] is True and refused["content"][0]["text"].startswith("UsageLimitReached: ")
        assert len(engine.calls) == calls_before


def test_a_channel_bind_stays_open_past_the_allowance_and_its_call_is_refused(tmp_path: Path) -> None:
    app, engine, _ = _past_the_allowance(tmp_path)
    with serving(app) as http, http.websocket_connect("/v1/channel") as ws:
        assert decide(http, bearer(KEY)).status_code == 200
        calls_before = len(engine.calls)
        channel = start(ws, KEY)
        bound = channel.result({"op": "bind", "id": 2, "function": SPAM_CALL["function"],
                                "context": SPAM_CALL["context"]})
        channel.error({"op": "call", "id": 3, "handle": bound["handle"], "input": "x"}, "UsageLimitReached")
        assert len(engine.calls) == calls_before


def test_a_request_refused_before_deciding_is_never_refused_for_the_allowance(tmp_path: Path) -> None:
    """Past the allowance, a call that fails its own checks answers its own error, not 402."""
    app, _, _ = _past_the_allowance(tmp_path)
    with serving(app) as http:
        assert decide(http, bearer(KEY)).status_code == 200
        unknown = http.post("/v1/decide", json={"calls": [{"function": "no.such", "input": "x"}]},
                            headers=bearer(KEY))
        assert unknown.status_code == 404 and unknown.json()["error"]["type"] == "UnknownFunction", unknown.text
        assert decide(http, bearer(KEY)).status_code == 402


def test_a_request_admitted_at_its_first_engine_request_runs_to_its_end(tmp_path: Path) -> None:
    """A dispatch asks the engine three times (two routers, then the leaf); with an allowance of 1 the first router
    crosses it, and the request still runs to its end, every reply counted."""
    hub = Hub()
    hub.keys = key_list(allowance=1)
    engine = FakeEngine(answers, set(ROUTED))
    app = accounts_app(tmp_path, hub, engine=engine, catalogue=ROUTERS)
    body = {"dispatch": {"input": "My wardrobe is broken", "interface": "rate",
                         "context": {"company": "A furniture shop.", "channel": "email"}}}
    with serving(app) as http:
        response = http.post("/v1/decide", json=body, headers=bearer(KEY))
        assert response.status_code == 200, response.text
        assert len(engine.calls) == 3
        assert app.state.accounts.book.total("acct1", NOV) == tokens_sent(engine)
        assert http.post("/v1/decide", json=body, headers=bearer(KEY)).status_code == 402
        assert len(engine.calls) == 3


def test_tokens_of_engine_requests_that_ran_are_counted_when_a_later_part_fails(tmp_path: Path) -> None:
    """A batch whose second adapter's engine request fails: the request fails and nothing is served, but the first
    adapter's engine request ran and is counted (the spec: after each engine request)."""
    class TriageFails(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            if adapter_id == "jeff.support.triage":
                raise EngineError("the triage engine failed")
            return super().predict(adapter_id, rows)

    engine = TriageFails(answers, {"jeff.safety.spam", "jeff.support.triage"})
    app = accounts_app(tmp_path, Hub(), engine=engine)
    with serving(app) as http:
        response = http.post("/v1/decide", json={"calls": [SPAM_CALL, TRIAGE_CALL]}, headers=bearer(KEY))
        assert response.status_code == 500 and "the triage engine failed" in response.json()["error"]["message"]
        assert [adapter for adapter, _ in engine.calls] == ["jeff.safety.spam"]
        assert app.state.accounts.book.total("acct1", NOV) == tokens_sent(engine) > 0


@pytest.mark.parametrize("counts", [[1], [5, -1], [5, True], [5, 2.0]])
def test_a_reply_with_unusable_input_token_counts_is_refused_and_not_counted(tmp_path: Path,
                                                                              counts: list[Any]) -> None:
    class Miscounted(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            result = super().predict(adapter_id, rows)
            return EngineResult(result.probabilities, result.timing, counts)

    meter = Meter(UsageBook(tmp_path / "usage.jsonl"), Account("acct1", 10, NOV, DEC), None,
                  lambda: datetime.now(UTC))
    metered = MeteredEngine(Miscounted(answers, {"jeff.safety.spam"}), meter)
    rows = [{"state": "a", "question": {"type": "noul"}}, {"state": "b", "question": {"type": "noul"}}]
    with pytest.raises(EngineError, match="cannot be counted"):
        metered.predict("jeff.safety.spam", rows)
    assert meter.book.total("acct1", NOV) == 0


# --- fix round 1: a period that has ended is followed by the next, assumed until the key list brings it -------------

def test_the_period_in_force_follows_an_ended_one_with_the_same_length() -> None:
    account = Account("acct1", 10, NOV, DEC)  # 30 days
    at = datetime(2026, 12, 3, 10, 0, 0, tzinfo=UTC)
    assert in_force(account, datetime(2026, 12, 3, 9, 59, 59, tzinfo=UTC)) is account
    assert in_force(account, at) == Account("acct1", 10, DEC, "2027-01-02T10:00:00Z")
    assert in_force(account, datetime(2027, 1, 2, 10, 0, 0, tzinfo=UTC)).period_start == "2027-01-02T10:00:00Z"


def test_an_exhausted_account_is_admitted_as_soon_as_its_period_ends(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(allowance=10)
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        assert decide(http, bearer(KEY)).status_code == 200
        assert decide(http, bearer(KEY)).status_code == 402
        used_in_november = app.state.accounts.book.total("acct1", NOV)
        app.state.accounts.now = lambda: datetime(2026, 12, 3, 10, 0, 1, tzinfo=UTC)  # the list still says NOV
        assert decide(http, bearer(KEY)).status_code == 200
        assert app.state.accounts.book.total("acct1", DEC) > 10
        refused = decide(http, bearer(KEY))
        assert refused.status_code == 402
        assert refused.json()["error"]["details"]["period_end"] == "2027-01-02T10:00:00Z"
        assert app.state.accounts.book.total("acct1", NOV) == used_in_november
        # The hub's list then brings the real period: the same start, so the same total, and its own end.
        hub.keys = key_list(allowance=10, period_start=DEC, period_end=JAN)
        app.state.accounts.refresh()
        assert decide(http, bearer(KEY)).json()["error"]["details"]["period_end"] == JAN


# --- fix round 1: every planner entry point in the server is known --------------------------------------------------

def test_every_place_the_server_runs_the_planner_is_known() -> None:
    """The SDK's Jeff predicts inside decide, dispatch, run_graph and the functions it binds, so a door that called
    them on the shared Jeff would ask the model uncounted, unseen by the predict scan above. The server calls them
    only in jeff_server.decide (on the view plan_body builds with caller_view), the channel's bind (on caller_view;
    binding asks the engine nothing) and the owner's factory worker (factory/publish.py, its own Jeff, never a
    request's). The MCP tools run in the SDK (jeff.mcp) on the view mcp_mount builds with caller_view."""
    import re
    source = ROOT / "apps" / "server" / "src" / "jeff_server"
    entry = re.compile(r"\bjeff\.(decide|dispatch|run_graph|function)\(")
    found = {(path.relative_to(source).as_posix(), match.group(1))
             for path in source.rglob("*.py")
             for line in path.read_text(encoding="utf-8").splitlines() if not line.strip().startswith("#")
             for match in entry.finditer(line)}
    assert found == {("decide.py", "decide"), ("decide.py", "dispatch"), ("decide.py", "run_graph"),
                     ("channel.py", "function"), ("factory/publish.py", "decide")}
