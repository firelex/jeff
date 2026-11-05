import itertools
import re
import socket
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jeff import Call, Jeff
from jeff.catalogue import Catalogue
from jeff.errors import EngineError, InputTooLong, InvalidRequest, ManifestError, NotInstalled, ProtocolMismatch
from jeff.protocol import encode, fail, hello
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine, fake_tokens
from jeff_runtime.testing import Running, SlowPredict

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
ROW = {"state": {}, "question": {"type": "noul", "instructions": "?"}}


def answer_health(conn: socket.socket, base_format: dict[str, str] | None = None) -> None:
    """For a scripted fake engine: reads the client's "health" call (id 2, the one SocketEngine makes inside
    __init__ after "installed", to read its base format) and answers it."""
    conn.recv(4096)
    base = {"id": "jeff-base", "version": "v1.3", "checkpoint": "fake", "max_options": 32,
            **(base_format if base_format is not None else {"format": "bf16"})}
    conn.sendall(encode({"id": 2, "ok": True, "result": {"status": "ready", "backend": "fake", "adapters": [],
                                                          "adapters_version": 0, "base": base}}))


def answers(adapter: str, row: dict) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return [0.0, 0.0, 0.9, 0.1, 0.0]
    if question["type"] == "noul":
        return [0.2, 0.8]
    return [1.0] + [0.0] * (len(question["criteria"]) - 1)


@pytest.fixture
def running():
    server = Running(FakeEngine(answers, {"jeff.support.triage", "jeff.safety.spam"}),
                     fixed={"jeff.support.triage": Path("/x"), "jeff.safety.spam": Path("/y")})
    yield server
    server.close()


def test_jeff_over_the_socket_matches_local(running) -> None:
    local = Jeff(FakeEngine(answers, {"jeff.support.triage", "jeff.safety.spam"}), Catalogue.from_dir(FIXTURES))
    engine = SocketEngine(running.path, timeout_s=5)
    try:
        remote = Jeff(engine, Catalogue.from_dir(FIXTURES))
        calls = [Call("triage.urgency", "Broken wardrobe", CONTEXT), Call("spam", "WIN", {"channel": "sms"})]
        for a, b in zip(local.decide(calls), remote.decide(calls), strict=True):
            assert (a.value, a.confidence, a.abstained) == (b.value, b.confidence, b.abstained)
    finally:
        engine.close()


def test_the_base_format_is_read_once_when_connecting() -> None:
    """SocketEngine reads the engine's base format from its health when it connects, and a Jeff over it refuses a
    calibrated function with no block for that INT4 format (jeff.manifest.calibration_for) before any row is sent."""
    int4 = {"format": "int4-g64", "format_id": "jeff-base@v1.3+int4-g64-gptq.0123abcd"}
    backing = FakeEngine(answers, {"jeff.support.triage"}, base_format=int4)
    server = Running(backing, fixed={"jeff.support.triage": Path("/x")})
    engine = SocketEngine(server.path, timeout_s=5)
    try:
        assert engine.base_format == int4
        jeff = Jeff(engine, Catalogue.from_dir(FIXTURES))
        assert jeff.format_id == int4["format_id"]
        with pytest.raises(ManifestError, match="jeff calibrate jeff.support.triage.urgency --base"):
            jeff.decide([Call("triage.urgency", "Broken wardrobe", CONTEXT)])
        decision = jeff.decide([Call("triage.needs_human", "Broken wardrobe", CONTEXT)])[0]
        assert decision.calibrated is False  # uncalibrated on bfloat16, so on INT4 too
        assert [adapter for adapter, _ in backing.calls] == ["jeff.support.triage"]  # only needs_human's row
    finally:
        engine.close()
        server.close()


def test_a_health_reply_without_the_base_format_is_an_engine_error_naming_the_engine() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "noformat.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)                 # the client's hello
        conn.sendall(encode(hello(0)))
        conn.recv(4096)                 # the client's "installed" call made inside __init__
        conn.sendall(encode({"id": 1, "ok": True, "result": {"adapters": [], "adapters_version": 0}}))
        conn.recv(4096)                 # the client's "health" call: a base without its format
        conn.sendall(encode({"id": 2, "ok": True, "result": {"status": "ready", "adapters_version": 0,
                                                              "base": {"id": "jeff-base", "version": "v1.3"}}}))
        conn.recv(1)
        conn.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    try:
        with pytest.raises(EngineError, match=rf"{re.escape(str(socket_path))} answered 'health' without base.format"):
            SocketEngine(socket_path, timeout_s=2)
    finally:
        thread.join(2)
        listener.close()


def test_many_threads_get_their_own_answers() -> None:
    """Proves two things that the previous version of this test did not: (1) replies on this one multiplexed
    connection can genuinely arrive out of send order, and (2) concurrent predict callers each still get their
    own distinct, correctly-matched answer rather than someone else's.

    The old version used one adapter with only a small per-row delay; the scheduler batches and runs requests on
    a single GPU thread to completion before starting the next batch, so a later-sent predict can never overtake
    an earlier one still in flight on the SAME adapter -- that version's "varying delay" never actually produced
    an out-of-order reply, it only assumed it might. Here, one predict call occupies the one GPU thread for 0.3s
    (`a.slow`); many `stats()` calls sent afterwards -- a real round trip over the wire, which the server answers
    directly on the event loop and never routes through the GPU thread/scheduler at all (unlike `installed()`,
    which is answered purely from a local cache and never touches the wire or the pending-reply machinery, so it
    would not exercise this at all) -- are therefore guaranteed to complete first, proven directly by comparing
    completion order, not inferred from timing. Separately, many concurrent predict calls on a second, undelayed
    adapter (`a.fast`) each still get their own value back, catching a multiplexing bug (e.g. a reader that
    resolves the OLDEST pending call regardless of its real reply id) as a wrong answer."""
    def answers(adapter: str, row: dict) -> list[float]:
        n = row["state"]["n"]
        if adapter == "a.slow":
            time.sleep(0.3)
        return [n / 1000.0, 1 - n / 1000.0]

    server = Running(FakeEngine(answers, {"a.slow", "a.fast"}),
                     fixed={"a.slow": Path("/x"), "a.fast": Path("/y")})
    engine = SocketEngine(server.path, timeout_s=5)
    try:
        completion_seq = itertools.count(1)
        order_lock = threading.Lock()
        slow_completion: list[int] = []
        fast_predict_completions: list[int] = []
        stats_completions: list[int] = []
        errors: list[BaseException] = []

        def slow_call() -> None:
            row = {"state": {"n": 999}, "question": {"type": "noul", "instructions": "?"}}
            try:
                value = engine.predict("a.slow", [row]).probabilities[0][0]
            except Exception as error:  # noqa: BLE001 - surfaced via `errors`, not swallowed
                errors.append(error)
                return
            if abs(value - 999 / 1000.0) > 1e-9:
                errors.append(AssertionError(f"slow call: expected {999 / 1000.0}, got {value}"))
                return
            with order_lock:
                slow_completion.append(next(completion_seq))

        def fast_predict(n: int) -> None:
            row = {"state": {"n": n}, "question": {"type": "noul", "instructions": "?"}}
            try:
                value = engine.predict("a.fast", [row]).probabilities[0][0]
            except Exception as error:  # noqa: BLE001 - surfaced via `errors`, not swallowed
                errors.append(error)
                return
            if abs(value - n / 1000.0) > 1e-9:
                errors.append(AssertionError(f"fast predict n={n}: expected {n / 1000.0}, got {value}"))
                return
            with order_lock:
                fast_predict_completions.append(next(completion_seq))

        def fast_stats() -> None:
            try:
                engine.stats()
            except Exception as error:  # noqa: BLE001 - surfaced via `errors`, not swallowed
                errors.append(error)
                return
            with order_lock:
                stats_completions.append(next(completion_seq))

        slow_thread = threading.Thread(target=slow_call)
        slow_thread.start()
        time.sleep(0.05)  # the slow predict has been sent and is occupying the one GPU thread for ~0.25s more
        later_threads = ([threading.Thread(target=fast_predict, args=(n,)) for n in range(15)]
                        + [threading.Thread(target=fast_stats) for _ in range(16)])
        for thread in later_threads:
            thread.start()
        for thread in [slow_thread, *later_threads]:
            thread.join(5)
    finally:
        engine.close()
        server.close()
    assert not errors
    assert len(slow_completion) == 1
    assert len(fast_predict_completions) == 15
    assert len(stats_completions) == 16
    # every `stats()` call was sent well after the slow predict, yet every one of them must complete before it:
    # a direct demonstration that reply order does not follow send order.
    assert max(stats_completions) < slow_completion[0], (
        f"expected every stats() call (sent later) to complete before the slow predict (sent earlier); "
        f"stats completions={sorted(stats_completions)}, slow completion={slow_completion[0]}")


def test_errors_are_the_same_classes(running) -> None:
    engine = SocketEngine(running.path, timeout_s=5)
    try:
        row = {"state": {"__too_long__": 1}, "question": {"type": "noul", "instructions": "?"}}
        with pytest.raises(InputTooLong):
            engine.predict("jeff.support.triage", [row])
    finally:
        engine.close()


def test_serve_refreshes_installed(running) -> None:
    engine = SocketEngine(running.path, timeout_s=5)
    try:
        engine.serve(["jeff.safety.spam"])
        assert engine.installed() == {"jeff.safety.spam"}
        jeff = Jeff(engine, Catalogue.from_dir(FIXTURES))
        with pytest.raises(NotInstalled):
            jeff.rate("x", function="triage.urgency", context=CONTEXT)
    finally:
        engine.close()


def test_a_stopped_engine_is_an_error_not_a_wait(running) -> None:
    """The call either still finds the connection open and fails as it closes, or finds it closed and fails its
    reconnect attempt (nothing listens any more); either way at once. test_socket_engine_reconnect.py covers the
    engine coming back."""
    engine = SocketEngine(running.path, timeout_s=5)
    try:
        running.close()
        with pytest.raises(EngineError, match="closed the connection|reconnect attempt"):
            engine.predict("jeff.support.triage", [ROW])
    finally:
        engine.close()


# ---- fix round 1


def test_close_fails_in_flight_and_later_calls_at_once() -> None:
    server = Running(SlowPredict(0.5, {"a.b.c"}), fixed={"a.b.c": Path("/x")})
    engine = SocketEngine(server.path, timeout_s=5)
    try:
        outcome: dict[str, Any] = {}
        started = threading.Event()

        def in_flight() -> None:
            started.set()
            try:
                engine.predict("a.b.c", [ROW])
            except Exception as error:  # noqa: BLE001 - surfaced via `outcome`, not swallowed
                outcome["in_flight"] = error

        thread = threading.Thread(target=in_flight)
        thread.start()
        started.wait(1)
        time.sleep(0.05)  # the predict is running on the GPU thread; its reply is not ready yet
        t0 = time.monotonic()
        engine.close()
        thread.join(2)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.3, f"close() should fail the in-flight call immediately, took {elapsed}s"
        assert isinstance(outcome.get("in_flight"), EngineError)
        assert "closed by its caller" in str(outcome["in_flight"])
        with pytest.raises(EngineError, match="closed by its caller"):
            engine.predict("a.b.c", [ROW])
    finally:
        engine.close()
        server.close()


def test_encode_failure_does_not_leak_the_pending_id(running) -> None:
    engine = SocketEngine(running.path, timeout_s=5)
    try:
        oversized = {"state": {"x": "a" * (65 * 1024 * 1024)}, "question": {"type": "noul", "instructions": "?"}}
        with pytest.raises(InvalidRequest):
            engine.predict("jeff.support.triage", [oversized])
        assert engine._pending == {}
    finally:
        engine.close()


def test_timeout_does_not_leak_the_pending_id() -> None:
    server = Running(SlowPredict(0.5, {"a.b.c"}), fixed={"a.b.c": Path("/x")})
    engine = SocketEngine(server.path, timeout_s=0.05)
    try:
        with pytest.raises(EngineError, match="No reply"):
            engine.predict("a.b.c", [ROW])
        assert engine._pending == {}
        time.sleep(0.6)  # let the slow reply, if it ever arrives, land without reviving the popped id
        assert engine._pending == {}
    finally:
        engine.close()
        server.close()


def test_cache_refresh_is_consistent_under_concurrency() -> None:
    """Many threads hammer `serve` (which changes the version) and `installed`/`predict` (which read the cache)
    at once; whatever the final state is, the cache must agree with it -- a stale reply overtaking a newer one
    must never roll the cache backwards."""
    backing = FakeEngine(lambda adapter, row: [0.5, 0.5], {"a.b.c"})
    server = Running(backing, fixed={"a.b.c": Path("/x"), "a.b.d": Path("/y"), "a.b.e": Path("/z")})
    client = SocketEngine(server.path, timeout_s=5)
    try:
        stop = threading.Event()
        errors: list[BaseException] = []

        def churn() -> None:
            toggle = False
            while not stop.is_set():
                try:
                    client.serve(["a.b.c", "a.b.d"] if toggle else ["a.b.c", "a.b.e"])
                except Exception as error:  # noqa: BLE001 - surfaced via `errors`, not swallowed
                    errors.append(error)
                toggle = not toggle

        def poke() -> None:
            while not stop.is_set():
                try:
                    client.installed()
                    client.predict("a.b.c", [ROW])
                except Exception as error:  # noqa: BLE001 - surfaced via `errors`, not swallowed
                    errors.append(error)

        threads = [threading.Thread(target=churn), *(threading.Thread(target=poke) for _ in range(63))]
        for thread in threads:
            thread.start()
        time.sleep(0.5)
        stop.set()
        for thread in threads:
            thread.join(5)
        final = client.serve(["a.b.c"])  # settle on a state no concurrent churner can race any more
        time.sleep(0.05)
        cached = client.installed()
    finally:
        client.close()
        server.close()
    assert not errors
    assert final == cached == {"a.b.c"} == backing.installed()


def test_handshake_times_out_if_the_server_never_says_hello() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "silent.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()
    try:
        with pytest.raises(EngineError, match=f"{re.escape(str(socket_path))}.*within 0.2s"):
            SocketEngine(socket_path, timeout_s=0.2)
    finally:
        listener.close()


def test_handshake_eof_names_the_path() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "disconnects.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()

    def closer() -> None:
        conn, _ = listener.accept()
        conn.close()

    thread = threading.Thread(target=closer, daemon=True)
    thread.start()
    try:
        with pytest.raises(EngineError, match=re.escape(str(socket_path))):
            SocketEngine(socket_path, timeout_s=2)
    finally:
        thread.join(2)
        listener.close()


def test_protocol_mismatch_closes_the_socket() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "mismatch.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()
    closed = threading.Event()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)  # the client's hello; its content does not matter here
        conn.sendall(encode({"op": "hello", "version": 999}))
        if conn.recv(1) == b"":  # the client closed its end once it saw the mismatch
            closed.set()
        conn.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    try:
        with pytest.raises(ProtocolMismatch):
            SocketEngine(socket_path, timeout_s=2)
    finally:
        thread.join(2)
        listener.close()
    assert closed.is_set()


def test_an_id_zero_fail_reply_fails_every_pending_call_and_closes_the_socket() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "idzero.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()
    closed = threading.Event()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)                 # the client's hello
        conn.sendall(encode(hello(0)))  # a normal handshake
        conn.recv(4096)                 # the client's "installed" call, made inside __init__
        conn.sendall(encode(fail(0, InvalidRequest("garbage from the engine"))))
        if conn.recv(1) == b"":
            closed.set()
        conn.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    try:
        with pytest.raises(InvalidRequest, match="garbage from the engine"):
            SocketEngine(socket_path, timeout_s=2)
    finally:
        thread.join(2)
        listener.close()
    assert closed.is_set()


# ---- fix round 2


def test_merge_snapshot_is_monotonic() -> None:
    server = Running(FakeEngine(lambda adapter, row: [0.5, 0.5], {"a.b.c"}), fixed={"a.b.c": Path("/x")})
    engine = SocketEngine(server.path, timeout_s=5)
    try:
        version = engine._snapshot[0]
        engine._merge_snapshot(version + 5, {"newer"})
        assert engine._snapshot == (version + 5, {"newer"})
        engine._merge_snapshot(version + 2, {"older, must be ignored"})  # older: dropped
        assert engine._snapshot == (version + 5, {"newer"})
        engine._merge_snapshot(version + 5, {"same version, must still update"})  # equal: still updates
        assert engine._snapshot == (version + 5, {"same version, must still update"})
        engine._merge_snapshot(version + 6, {"newest"})  # newer: updates
        assert engine._snapshot == (version + 6, {"newest"})
    finally:
        engine.close()
        server.close()


class _BrokenSend:
    """Wraps a real connected socket so `sendall` fails deterministically while `shutdown`/`close` still reach
    the real socket (needed for the engine's own cleanup). `socket.socket` instances refuse a direct
    `instance.sendall = ...` override (its methods are read-only at the instance level), hence this wrapper."""
    def __init__(self, real: socket.socket) -> None:
        self._real = real

    def sendall(self, payload: bytes) -> None:
        raise OSError("simulated broken pipe")

    def shutdown(self, how: int) -> None:
        self._real.shutdown(how)

    def close(self) -> None:
        self._real.close()


def test_send_failure_does_not_leak_the_pending_id(running) -> None:
    engine = SocketEngine(running.path, timeout_s=5)
    try:
        engine._sock = _BrokenSend(engine._sock)  # type: ignore[assignment]
        with pytest.raises(EngineError, match=re.escape(str(engine.path))):
            engine.predict("jeff.support.triage", [ROW])
        assert engine._pending == {}
    finally:
        engine.close()


def test_calls_after_death_each_get_a_fresh_error_chained_from_the_failed_reconnect() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "idzero2.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)                 # the client's hello
        conn.sendall(encode(hello(0)))  # a normal handshake
        conn.recv(4096)                 # the client's "installed" call made inside __init__
        conn.sendall(encode({"id": 1, "ok": True, "result": {"adapters": [], "adapters_version": 0}}))
        answer_health(conn)             # the client's "health" call made inside __init__ (its base format)
        conn.recv(4096)                 # the test's own predict() call below
        conn.sendall(encode(fail(0, InvalidRequest("garbage from the engine"))))
        conn.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    engine = SocketEngine(socket_path, timeout_s=2)
    try:
        # this first call is the one in flight when the connection dies: it gets the rebuilt error directly.
        with pytest.raises(InvalidRequest, match="garbage from the engine"):
            engine.predict("a.b.c", [ROW])
        thread.join(2)
        listener.close()  # nothing accepts any more, so the reconnect attempt below is refused at once
        with pytest.raises(EngineError, match="reconnect attempt") as attempt:
            engine.predict("a.b.c", [ROW])
        # every call made AFTER that, within the reconnect interval, must get its own fresh EngineError -- not the
        # same shared exception instance handed to every caller, which would mean many threads mutating one
        # exception's traceback.
        first: EngineError | None = None
        second: EngineError | None = None
        try:
            engine.predict("a.b.c", [ROW])
        except EngineError as error:
            first = error
        try:
            engine.predict("a.b.c", [ROW])
        except EngineError as error:
            second = error
        assert first is not None and second is not None
        assert first is not second and attempt.value not in (first, second)
        assert str(attempt.value) in str(first) and str(attempt.value) in str(second)
        assert first.__cause__ is attempt.value and second.__cause__ is attempt.value
    finally:
        thread.join(2)
        listener.close()
        engine.close()


# ---- final review fixes


def test_an_adapter_changed_by_another_connection_is_seen_without_restarting(running) -> None:
    mine, other = SocketEngine(running.path, timeout_s=5), SocketEngine(running.path, timeout_s=5)
    try:
        jeff = Jeff(mine, Catalogue.from_dir(FIXTURES))
        spam = Call("spam", "WIN", {"channel": "sms"})
        other.serve(["jeff.support.triage"])  # removed elsewhere: this client still thinks spam is loaded
        with pytest.raises(NotInstalled) as removed:
            jeff.decide([spam])
        assert "adapters_version" not in vars(removed.value)  # taken out of the details, not an attribute
        assert mine.installed() == {"jeff.support.triage"}  # the failure's version brought the cache up to date
        other.serve(["jeff.support.triage", "jeff.safety.spam"])  # added back elsewhere
        assert jeff.decide([spam])[0].value is not None  # no restart: the binding asked the engine again
    finally:
        mine.close()
        other.close()


def test_installed_refresh_asks_the_engine(running) -> None:
    mine, other = SocketEngine(running.path, timeout_s=5), SocketEngine(running.path, timeout_s=5)
    try:
        other.serve(["jeff.safety.spam"])
        assert mine.installed() == {"jeff.support.triage", "jeff.safety.spam"}  # the cache, not yet refreshed
        assert mine.installed(refresh=True) == {"jeff.safety.spam"}
        assert mine.installed() == {"jeff.safety.spam"}
    finally:
        mine.close()
        other.close()


def test_installed_version_asks_the_engine_for_its_version_and_list(running) -> None:
    mine, other = SocketEngine(running.path, timeout_s=5), SocketEngine(running.path, timeout_s=5)
    try:
        assert mine.installed_version() == (0, {"jeff.support.triage", "jeff.safety.spam"})
        other.serve(["jeff.safety.spam"])
        assert mine.installed_version() == (1, {"jeff.safety.spam"})
        assert mine.installed() == {"jeff.safety.spam"}  # the cache was updated from the same reply
    finally:
        mine.close()
        other.close()


def test_a_reply_without_adapters_version_is_an_engine_error() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "noversion.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)                 # the client's hello
        conn.sendall(encode(hello(0)))
        conn.recv(4096)                 # the client's "installed" call made inside __init__
        conn.sendall(encode({"id": 1, "ok": True, "result": {"adapters": [], "adapters_version": 0}}))
        answer_health(conn)             # the client's "health" call made inside __init__ (its base format)
        conn.recv(4096)                 # the test's stats() call
        conn.sendall(encode({"id": 3, "ok": True, "result": {"queued_rows": 0}}))
        conn.recv(1)
        conn.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    engine = SocketEngine(socket_path, timeout_s=2)
    try:
        with pytest.raises(EngineError, match="stats.*adapters_version"):
            engine.stats()
    finally:
        engine.close()
        thread.join(2)
        listener.close()


# ---- carry-over from 2a-1


def test_refresh_failure_after_a_failed_call_never_hides_the_original_error() -> None:
    """A failure reply whose adapters_version differs from the cached one makes `_call` ask the engine for the
    fresh installed list (`_follow`). If that follow-up call itself fails -- here, because the server closes the
    connection right after sending the failure reply -- the caller must still see the ORIGINAL error (the one the
    failed predict actually describes), never the refresh's own failure in its place; the refresh failure is
    recorded as a note on the original error (`error.add_note(...)`), not silently dropped either."""
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "refresh-fails.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)                 # the client's hello
        conn.sendall(encode(hello(0)))
        conn.recv(4096)                 # the client's "installed" call made inside __init__
        conn.sendall(encode({"id": 1, "ok": True, "result": {"adapters": [], "adapters_version": 0}}))
        answer_health(conn)             # the client's "health" call made inside __init__ (its base format)
        conn.recv(4096)                 # the test's own predict() call below
        reply = fail(3, InvalidRequest("the real failure"))
        reply["error"]["details"]["adapters_version"] = 7  # stale vs the cached 0: _follow tries to refresh
        conn.sendall(encode(reply))
        conn.close()                    # the refresh's own "installed" call will now fail

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    engine = SocketEngine(socket_path, timeout_s=2)
    try:
        with pytest.raises(InvalidRequest) as caught:
            engine.predict("a.b.c", [ROW])
        assert "the real failure" in str(caught.value)
        notes = getattr(caught.value, "__notes__", [])
        assert any("refresh" in note.lower() for note in notes), notes
    finally:
        thread.join(2)
        listener.close()
        engine.close()


def test_refresh_branch_when_the_installed_call_itself_gets_a_typed_failure_reply() -> None:
    """The follow-up `installed` call `_follow` makes to refresh the cache can itself fail with its own typed
    reply (ok=False, a real JeffError from the server), not only a dropped connection; that must also end up as a
    note on the original error, never raised in its place."""
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "refresh-typed-fail.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)                 # the client's hello
        conn.sendall(encode(hello(0)))
        conn.recv(4096)                 # the client's "installed" call made inside __init__
        conn.sendall(encode({"id": 1, "ok": True, "result": {"adapters": [], "adapters_version": 0}}))
        answer_health(conn)             # the client's "health" call made inside __init__ (its base format)
        conn.recv(4096)                 # the test's own predict() call below
        predict_fail = fail(3, InvalidRequest("the real failure"))
        predict_fail["error"]["details"]["adapters_version"] = 9  # stale vs the cached 0: _follow refreshes
        conn.sendall(encode(predict_fail))
        conn.recv(4096)                 # the refresh's own "installed" call
        installed_fail = fail(4, EngineError("installed is broken too"))
        installed_fail["error"]["details"]["adapters_version"] = 9
        conn.sendall(encode(installed_fail))
        conn.recv(1)
        conn.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    engine = SocketEngine(socket_path, timeout_s=2)
    try:
        with pytest.raises(InvalidRequest) as caught:
            engine.predict("a.b.c", [ROW])
        assert "the real failure" in str(caught.value)
        notes = getattr(caught.value, "__notes__", [])
        assert any("installed is broken too" in note for note in notes), notes
    finally:
        thread.join(2)
        listener.close()
        engine.close()


def test_a_failure_reply_missing_its_adapters_version_still_raises_the_servers_error() -> None:
    """`_version` itself can raise -- a failure reply with no adapters_version in its details at all, a protocol
    violation on the server's part. The server's real error must still be the one raised (with that problem
    recorded as a note), never replaced by a bare "no adapters_version" EngineError."""
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "missing-version.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)                 # the client's hello
        conn.sendall(encode(hello(0)))
        conn.recv(4096)                 # the client's "installed" call made inside __init__
        conn.sendall(encode({"id": 1, "ok": True, "result": {"adapters": [], "adapters_version": 0}}))
        answer_health(conn)             # the client's "health" call made inside __init__ (its base format)
        conn.recv(4096)                 # the test's own predict() call below
        conn.sendall(encode(fail(3, InvalidRequest("the real failure"))))  # no adapters_version in its details
        conn.recv(1)
        conn.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    engine = SocketEngine(socket_path, timeout_s=2)
    try:
        with pytest.raises(InvalidRequest) as caught:
            engine.predict("a.b.c", [ROW])
        assert "the real failure" in str(caught.value)
        notes = getattr(caught.value, "__notes__", [])
        assert any("adapters_version" in note for note in notes), notes
    finally:
        thread.join(2)
        listener.close()
        engine.close()


def test_predict_returns_each_rows_input_tokens(running) -> None:
    engine = SocketEngine(running.path, timeout_s=5)
    try:
        other = {"state": "a few more words", "question": ROW["question"]}
        result = engine.predict("jeff.safety.spam", [ROW, other])
    finally:
        engine.close()
    assert result.input_tokens == [fake_tokens(ROW), fake_tokens(other)]


def test_a_health_reply_without_the_base_id_and_version_is_an_engine_error_naming_the_engine() -> None:
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "noid.sock"
    listener = socket.socket(socket.AF_UNIX)
    listener.bind(str(socket_path))
    listener.listen()

    def fake_server() -> None:
        conn, _ = listener.accept()
        conn.recv(4096)                 # the client's hello
        conn.sendall(encode(hello(0)))
        conn.recv(4096)                 # the client's "installed" call made inside __init__
        conn.sendall(encode({"id": 1, "ok": True, "result": {"adapters": [], "adapters_version": 0}}))
        conn.recv(4096)                 # the client's "health" call: a base with its format but no id or version
        conn.sendall(encode({"id": 2, "ok": True, "result": {"status": "ready", "adapters_version": 0,
                                                              "base": {"format": "bf16"}}}))
        conn.recv(1)
        conn.close()

    thread = threading.Thread(target=fake_server, daemon=True)
    thread.start()
    try:
        with pytest.raises(EngineError, match=rf"{re.escape(str(socket_path))} answered 'health' without base.id "
                                              "and base.version"):
            SocketEngine(socket_path, timeout_s=2)
    finally:
        thread.join(2)
        listener.close()
