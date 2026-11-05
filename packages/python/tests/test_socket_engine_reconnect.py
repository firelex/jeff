"""SocketEngine after its engine process restarts: the next call makes one fresh connection (connect, hello,
installed), at most one attempt per RECONNECT_INTERVAL_S, never after the caller's close(), and never resending a
call that was in flight when the connection died. The engines are real EngineServers (jeff_runtime.testing.Running
over a FakeEngine), stopped and started again on the same socket path as a supervisor restarting the process would."""

import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jeff import socket_engine
from jeff.errors import EngineError
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine
from jeff_runtime.testing import Running, SlowPredict

ROW = {"state": {}, "question": {"type": "noul", "instructions": "?"}}
AVAILABLE = {"a.b.c": Path("/x"), "a.b.d": Path("/y"), "a.b.e": Path("/z")}


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    return [0.2, 0.8]


def start(installed: set[str], path: Path | None = None) -> Running:
    return Running(FakeEngine(answers, installed), fixed=AVAILABLE, path=path)


def wait_until_dead(engine: SocketEngine) -> None:
    """The reader thread notices the closed socket a moment after the server closes it."""
    for _ in range(500):
        if engine._closed is not None:
            return
        time.sleep(0.01)
    raise AssertionError("the SocketEngine never noticed its engine close the connection")


def count_connections(engine: SocketEngine) -> list[int]:
    """Counts each connection attempt the SocketEngine makes from now on (calls to its _connect)."""
    made = [0]
    connect = engine._connect

    def counted() -> Any:
        made[0] += 1
        return connect()

    engine._connect = counted  # type: ignore[method-assign]
    return made


def test_a_restarted_engine_is_reconnected_on_the_next_call_and_the_call_in_flight_fails() -> None:
    first = Running(SlowPredict(0.5, {"a.b.c"}), fixed=AVAILABLE)
    engine = SocketEngine(first.path, timeout_s=5)
    second = None
    try:
        failure: list[BaseException] = []

        def in_flight() -> None:
            try:
                engine.predict("a.b.c", [ROW])
            except BaseException as error:  # noqa: BLE001 - recorded for the assertion below
                failure.append(error)

        caller = threading.Thread(target=in_flight)
        caller.start()
        time.sleep(0.1)  # the predict is now waiting on the slow engine
        first.close()
        caller.join(5)
        assert len(failure) == 1 and isinstance(failure[0], EngineError)
        assert str(failure[0]) == f"The engine at {first.path} closed the connection"
        assert engine.reconnects == 0

        second = start({"a.b.c"}, path=first.path)
        result = engine.predict("a.b.c", [ROW])
        assert result.probabilities == [[0.2, 0.8]]
        assert engine.reconnects == 1
        assert engine.installed() == {"a.b.c"}
    finally:
        engine.close()
        first.close()
        if second is not None:
            second.close()


def test_a_successful_reconnect_logs_a_warning_naming_the_path_and_version(caplog: pytest.LogCaptureFixture) -> None:
    first = start({"a.b.c"})
    engine = SocketEngine(first.path, timeout_s=5)
    second = None
    try:
        first.close()
        wait_until_dead(engine)
        second = start({"a.b.c"}, path=first.path)
        with caplog.at_level("WARNING", logger="jeff.socket"):
            engine.predict("a.b.c", [ROW])
        records = [record for record in caplog.records if record.name == "jeff.socket"]
        assert len(records) == 1 and records[0].levelname == "WARNING"
        assert str(first.path) in records[0].getMessage() and "adapters_version 0" in records[0].getMessage()
    finally:
        engine.close()
        if second is not None:
            second.close()


def test_with_the_engine_down_two_calls_within_the_interval_make_one_attempt() -> None:
    server = start({"a.b.c"})
    engine = SocketEngine(server.path, timeout_s=5)
    try:
        server.close()
        wait_until_dead(engine)
        made = count_connections(engine)
        with pytest.raises(EngineError) as first:
            engine.predict("a.b.c", [ROW])
        assert str(first.value).startswith(f"No engine is listening at {server.path}: ")
        assert str(first.value).endswith(" (reconnect attempt)")
        assert str(first.value).count("No engine is listening") == 1
        with pytest.raises(EngineError) as second:
            engine.predict("a.b.c", [ROW])
        assert made[0] == 1
        assert second.value is not first.value
        assert str(first.value) in str(second.value)  # names the first attempt's failure
        assert "s ago" in str(second.value)            # and its age
        assert engine.reconnects == 0
    finally:
        engine.close()


def test_after_the_interval_the_next_call_tries_again(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket_engine, "RECONNECT_INTERVAL_S", 0.2)
    server = start({"a.b.c"})
    engine = SocketEngine(server.path, timeout_s=5)
    second = None
    try:
        server.close()
        wait_until_dead(engine)
        with pytest.raises(EngineError, match="reconnect attempt"):
            engine.predict("a.b.c", [ROW])
        second = start({"a.b.c"}, path=server.path)
        with pytest.raises(EngineError, match="s ago"):
            engine.predict("a.b.c", [ROW])  # still within the interval: no attempt, though the engine is back
        time.sleep(0.25)
        assert engine.predict("a.b.c", [ROW]).probabilities == [[0.2, 0.8]]
        assert engine.reconnects == 1
    finally:
        engine.close()
        if second is not None:
            second.close()


def test_many_threads_calling_after_a_restart_share_one_new_connection() -> None:
    first = start({"a.b.c"})
    engine = SocketEngine(first.path, timeout_s=5)
    second = None
    try:
        first.close()
        wait_until_dead(engine)
        second = start({"a.b.c"}, path=first.path)
        made = count_connections(engine)
        barrier = threading.Barrier(16)
        results: list[Any] = []
        lock = threading.Lock()

        def call() -> None:
            barrier.wait()
            try:
                outcome: Any = engine.predict("a.b.c", [ROW]).probabilities
            except BaseException as error:  # noqa: BLE001 - recorded for the assertion below
                outcome = error
            with lock:
                results.append(outcome)

        threads = [threading.Thread(target=call) for _ in range(16)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(10)
        assert results == [[[0.2, 0.8]]] * 16
        assert made[0] == 1
        assert engine.reconnects == 1
    finally:
        engine.close()
        if second is not None:
            second.close()


def test_after_close_there_is_no_reconnect() -> None:
    server = start({"a.b.c"})
    engine = SocketEngine(server.path, timeout_s=5)
    try:
        made = count_connections(engine)
        engine.close()
        with pytest.raises(EngineError, match="closed by its caller"):
            engine.predict("a.b.c", [ROW])
        assert made[0] == 0 and engine.reconnects == 0
    finally:
        server.close()


def test_after_close_of_a_dead_connection_there_is_no_reconnect() -> None:
    """close() is final even when the connection had already died before it (the first error is kept)."""
    first = start({"a.b.c"})
    engine = SocketEngine(first.path, timeout_s=5)
    second = None
    try:
        first.close()
        wait_until_dead(engine)
        second = start({"a.b.c"}, path=first.path)
        made = count_connections(engine)
        engine.close()
        with pytest.raises(EngineError, match="closed the connection"):
            engine.predict("a.b.c", [ROW])
        assert made[0] == 0 and engine.reconnects == 0
    finally:
        if second is not None:
            second.close()


def test_the_new_engines_adapters_replace_the_old_even_at_a_lower_version() -> None:
    """A restarted engine counts its adapters_version from 0 again, so the cache's never-go-backwards rule must
    not keep the old engine's list."""
    first = start({"a.b.c"})
    engine = SocketEngine(first.path, timeout_s=5)
    second = None
    try:
        assert engine.serve(["a.b.c", "a.b.d"]) == {"a.b.c", "a.b.d"}
        assert engine.installed_version() == (1, {"a.b.c", "a.b.d"})
        first.close()
        wait_until_dead(engine)
        second = start({"a.b.e"}, path=first.path)
        engine.predict("a.b.e", [ROW])
        assert engine.installed() == {"a.b.e"}
        assert engine._snapshot[0] == 0
    finally:
        engine.close()
        if second is not None:
            second.close()


def test_an_engine_restarted_on_another_base_format_is_refused() -> None:
    """The functions bound through a SocketEngine chose their calibration for the base format it read when it
    connected; an engine that comes back serving another format is refused, every call, until this process
    restarts."""
    first = start({"a.b.c"})
    engine = SocketEngine(first.path, timeout_s=5)
    second = None
    try:
        assert engine.base_format == {"format": "bf16"}
        first.close()
        wait_until_dead(engine)
        int4 = {"format": "int4-g64", "format_id": "jeff-base@v1.3+int4-g64-gptq.0123abcd"}
        second = Running(FakeEngine(answers, {"a.b.c"}, base_format=int4), fixed=AVAILABLE, path=first.path)
        with pytest.raises(EngineError, match=r"restarted serving a base of format .*int4-g64.*Restart this process"):
            engine.predict("a.b.c", [ROW])
        with pytest.raises(EngineError, match="restarted serving a base of format"):
            engine.predict("a.b.c", [ROW])  # within the interval: the same failure, named again
        assert engine.reconnects == 0 and engine.base_format == {"format": "bf16"}
    finally:
        engine.close()
        if second is not None:
            second.close()


def test_an_engine_restarted_on_another_base_is_refused() -> None:
    """A SocketEngine reads the base its engine serves (id and version, as adapter manifests name it) when it
    connects; the server lists the adapters trained for that base. An engine that comes back serving another base is
    refused, as one serving another format is, until this process restarts."""
    first = start({"a.b.c"})
    engine = SocketEngine(first.path, timeout_s=5)
    second = None
    try:
        assert engine.base == ("jeff-base", "v1.3")
        first.close()
        wait_until_dead(engine)
        second = Running(FakeEngine(answers, {"a.b.c"}), fixed=AVAILABLE, path=first.path, base=("jeff-base", "v1.4"))
        with pytest.raises(EngineError, match=r"restarted serving a base of format .*\(jeff-base@v1\.4\), not .*"
                                              r"\(jeff-base@v1\.3\).*Restart this process"):
            engine.predict("a.b.c", [ROW])
        assert engine.reconnects == 0 and engine.base == ("jeff-base", "v1.3")
    finally:
        engine.close()
        if second is not None:
            second.close()
