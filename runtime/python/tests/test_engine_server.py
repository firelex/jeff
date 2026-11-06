import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import stat
import tempfile
import threading
import time
from pathlib import Path

import pytest

import msgpack
from jeff.engine import EngineResult
from jeff.errors import EngineError, ManifestError
from jeff.protocol import PROTOCOL_VERSION
from jeff.testing import FakeEngine, fake_adapter_folder, fake_tokens
from jeff_runtime import engine_server
from jeff_runtime.engine_server import EngineServer
from jeff_runtime.testing import Running, SlowAddAdapter, SlowPredict

BASE = ("jeff-base", "v1.3")  # the catalogue's base, which the fake adapter folders are trained on

# The shipped catalogue's base: the tests that run `jeff runtime serve` (_run_cli) load the real catalogue, so their
# fake adapter folders are trained on it.
CATALOGUE_BASE = "v1.4"
ROW = {"state": {"message": "hi"}, "question": {"type": "noul", "instructions": "?"}}


@pytest.fixture
def running():
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    server = Running(engine)
    yield server
    server.close()


def test_hello_and_installed(running: Running) -> None:
    client = running.client()
    assert client.greeting["op"] == "hello" and client.greeting["version"] == PROTOCOL_VERSION
    client.send({"op": "installed", "id": 1})
    reply = client.receive()
    assert reply == {"id": 1, "ok": True, "result": {"adapters": ["a.b.c"], "adapters_version": 0}}


def test_two_clients_rows_share_one_pass(running: Running) -> None:
    engine = running.server.engine
    first, second = running.client(), running.client()
    first.send({"op": "predict", "id": 1, "adapter": "a.b.c", "rows": [ROW]})
    second.send({"op": "predict", "id": 1, "adapter": "a.b.c", "rows": [ROW, ROW]})
    assert first.receive()["result"]["probabilities"] == [[0.4, 0.6]]
    assert len(second.receive()["result"]["probabilities"]) == 2
    assert [len(rows) for _, rows in engine.calls] == [3]


def test_errors_come_back_typed(running: Running) -> None:
    client = running.client()
    client.send({"op": "predict", "id": 5, "adapter": "jeff.nope.zzz", "rows": [ROW]})
    reply = client.receive()
    assert reply["ok"] is False and reply["id"] == 5 and reply["error"]["type"] == "NotInstalled"
    assert reply["error"]["details"]["adapters_version"] == 0


def test_protocol_mismatch_closes(running: Running) -> None:
    client = running.client(version=99)
    assert client.greeting["ok"] is False and client.greeting["error"]["type"] == "ProtocolMismatch"


def test_serve_changes_adapters_and_writes_the_served_file(tmp_path: Path) -> None:
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    served = tmp_path / "served.json"
    server = Running(engine, served)
    try:
        client = server.client()
        client.send({"op": "serve", "id": 2, "adapters": ["a.b.d"]})
        reply = client.receive()
        assert reply["result"]["adapters"] == ["a.b.d"] and reply["result"]["adapters_version"] == 1
        assert json.loads(served.read_text()) == {"format": "jeff-engine-served", "version": 1,
                                                  "adapters": ["a.b.d"]}
        client.send({"op": "serve", "id": 3, "adapters": ["jeff.nope.zzz"]})
        assert client.receive()["error"]["type"] == "UnknownFunction"
    finally:
        server.close()


def test_disconnected_client_does_not_break_the_engine(caplog: pytest.LogCaptureFixture) -> None:
    engine = SlowPredict(0.2, {"a.b.c"})
    server = Running(engine)
    try:
        gone = server.client()
        gone.send({"op": "predict", "id": 1, "adapter": "a.b.c", "rows": [ROW]})
        time.sleep(0.05)  # the predict is running on the GPU thread; its reply is not ready yet
        gone.disconnect()
        other = server.client()
        other.send({"op": "predict", "id": 9, "adapter": "a.b.c", "rows": [ROW]})
        assert other.receive()["ok"] is True
        time.sleep(0.3)  # let the first predict finish and its (dropped) reply attempt happen
    finally:
        server.close()
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


def test_a_second_engine_cannot_take_the_same_socket(running: Running) -> None:
    clash = EngineServer(running.server.engine, running.scheduler, running.path, [], {"a.b.c": Path("/x")}, None,
                         BASE, pinned=set(), prepare_workers=1)
    with pytest.raises(EngineError, match="another engine"):
        asyncio.run(clash.run())


def test_refuses_a_stale_socket_path_that_is_not_a_socket(running: Running) -> None:
    running.close()
    running.path.unlink(missing_ok=True)  # closing the server removes the socket special file on some platforms
    running.path.write_text("not a socket")
    clash = EngineServer(running.server.engine, running.scheduler, running.path, [], {"a.b.c": Path("/x")}, None,
                         BASE, pinned=set(), prepare_workers=1)
    with pytest.raises(EngineError, match=str(running.path)):
        asyncio.run(clash.run())


def test_socket_permissions_are_never_wider_than_owner_only(running: Running) -> None:
    mode = running.path.stat().st_mode
    assert stat.S_IMODE(mode) == 0o600


def test_concurrent_serves_give_consistent_replies_and_final_state() -> None:
    # This is a regression test for computing to_remove/to_add from a snapshot taken on the event-loop thread
    # before either change actually runs: under that bug, a `serve` request's own reply could describe adapters
    # it never asked for (confirmed by monkeypatching the old _serve back in and running this test against it:
    # see /private/tmp/claude-501/probe6/oldserve.py, which fails this test's new assertions 5/5 times, after
    # passing the previous, weaker version of it 5/5 times).
    engine = SlowAddAdapter(0.2, {"a.b.c"})
    server = Running(engine)
    try:
        first, second = server.client(), server.client()
        first.send({"op": "serve", "id": 1, "adapters": ["a.b.d"]})
        second.send({"op": "serve", "id": 1, "adapters": ["a.b.c"]})
        first_reply, second_reply = first.receive(), second.receive()
        # Each reply must report exactly the adapter set its OWN request asked for, regardless of which one the
        # scheduler actually ran first: a stale, pre-change diff could otherwise leak the other request's
        # adapters into this one's reply.
        assert sorted(first_reply["result"]["adapters"]) == ["a.b.d"]
        assert sorted(second_reply["result"]["adapters"]) == ["a.b.c"]
        # Whichever request the scheduler actually ran last decides the final state; the two replies' versions
        # must be distinct and increasing.
        assert {first_reply["result"]["adapters_version"], second_reply["result"]["adapters_version"]} == {1, 2}
        later = first_reply if first_reply["result"]["adapters_version"] == 2 else second_reply
        assert sorted(engine.installed()) == later["result"]["adapters"]
    finally:
        server.close()


def test_a_disconnect_during_serve_does_not_cancel_the_version_bump_or_the_file_write(tmp_path: Path) -> None:
    engine = SlowAddAdapter(0.3, {"a.b.c"})
    served = tmp_path / "served.json"
    server = Running(engine, served)
    try:
        client = server.client()
        client.send({"op": "serve", "id": 1, "adapters": ["a.b.c", "a.b.d"]})
        time.sleep(0.05)  # add_adapter is running on the GPU thread; the reply is not ready yet
        client.disconnect()
        time.sleep(0.5)  # let the change (still queued and running independently of the connection) finish
        other = server.client()
        other.send({"op": "installed", "id": 1})
        reply = other.receive()
    finally:
        server.close()
    assert reply["result"] == {"adapters": ["a.b.c", "a.b.d"], "adapters_version": 1}
    assert '"a.b.d"' in served.read_text()


def test_installed_reflects_the_engine_even_when_the_served_file_write_fails(tmp_path: Path) -> None:
    folder = tmp_path / "read-only"
    folder.mkdir()
    served = folder / "served.json"
    served.write_text(json.dumps({"format": "jeff-engine-served", "version": 1, "adapters": ["a.b.c"]}))
    served.chmod(0o400)  # the served file can be read (at startup) but not rewritten: the write fails
    folder.chmod(0o500)
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    server = Running(engine, served)
    try:
        client = server.client()
        client.send({"op": "serve", "id": 1, "adapters": ["a.b.d"]})
        reply = client.receive()
        assert reply["ok"] is False and reply["error"]["type"] == "EngineError"
        client.send({"op": "installed", "id": 2})
        installed_reply = client.receive()
    finally:
        server.close()
        folder.chmod(0o700)
        served.chmod(0o600)
    # The mutation itself (remove a.b.c, add a.b.d) already succeeded before the write failed; the published
    # snapshot and the version must reflect that real engine state, not the state from before the failed `serve`.
    assert installed_reply["result"]["adapters"] == sorted(engine.installed()) == ["a.b.d"]
    assert installed_reply["result"]["adapters_version"] == 1


def test_installed_reflects_the_engine_when_add_adapter_fails_midway(tmp_path: Path) -> None:
    class AddFailsFor(FakeEngine):
        def add_adapter(self, adapter_id: str, path: Path) -> None:
            if adapter_id == "a.b.d":
                raise EngineError("boom")
            super().add_adapter(adapter_id, path)

    engine = AddFailsFor(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    server = Running(engine)
    try:
        client = server.client()
        client.send({"op": "serve", "id": 1, "adapters": ["a.b.d"]})
        reply = client.receive()
        assert reply["ok"] is False and reply["error"]["message"] == "boom"
        client.send({"op": "installed", "id": 2})
        installed_reply = client.receive()
        client.send({"op": "predict", "id": 3, "adapter": "a.b.c", "rows": [ROW]})
        predict_reply = client.receive()
    finally:
        server.close()
    # a.b.c was removed (it was not in `wanted`) before add_adapter("a.b.d") raised; that removal is not rolled
    # back, so the engine really does end up with nothing installed, and `installed` must say so, not report the
    # pre-change state.
    assert sorted(engine.installed()) == []
    assert installed_reply["result"]["adapters"] == []
    assert installed_reply["result"]["adapters_version"] == 1
    assert predict_reply["ok"] is False and predict_reply["error"]["type"] == "NotInstalled"


def test_garbage_msgpack_body_fails_that_connection_without_breaking_the_server(running: Running) -> None:
    client = running.client()
    client.send_raw(b"\xc1\xc1\xc1")
    reply = client.receive()
    assert reply["ok"] is False and reply["error"]["type"] == "InvalidRequest"
    other = running.client()
    other.send({"op": "installed", "id": 1})
    assert other.receive()["ok"] is True


def test_a_reply_shaped_message_from_a_client_is_a_typed_error_not_a_crash(running: Running) -> None:
    client = running.client()
    client.send({"id": 3, "ok": True, "result": {}})
    reply = client.receive()
    assert reply["id"] == 3 and reply["ok"] is False and reply["error"]["type"] == "InvalidRequest"


def test_a_second_hello_is_a_typed_error_not_a_crash(running: Running) -> None:
    client = running.client()
    client.send({"op": "hello", "version": PROTOCOL_VERSION})
    reply = client.receive()
    assert reply["ok"] is False and reply["error"]["type"] == "InvalidRequest"


@pytest.mark.parametrize("message", [
    {"op": "auth", "id": 2, "token": "t"},
    {"op": "refresh", "id": 2, "token": "t"},
    {"op": "bind", "id": 2, "function": "a.b.c.d"},
    {"op": "call", "id": 2, "handle": 1, "input": "x"},
    {"op": "call_many", "id": 2, "handle": 1, "inputs": ["x"]},
    {"op": "release", "id": 2, "handle": 1},
    {"op": "decide", "id": 2, "calls": [{"function": "a.b.c.d", "input": "x"}]},
    {"op": "dispatch", "id": 2, "dispatch": {"input": "x", "interface": "classify"}},
    {"op": "graph", "id": 2, "graph": {"id": "g"}, "input": "x", "params": {}},
])
def test_channel_operations_are_refused_by_the_engine_socket(running: Running, message: dict) -> None:
    """The socket schema also holds the channel's operations (jeff-server's /v1/channel); the engine answers each
    with InvalidRequest by its id, and the connection stays open."""
    client = running.client()
    client.send(message)
    reply = client.receive()
    assert reply["id"] == 2 and reply["ok"] is False and reply["error"]["type"] == "InvalidRequest"
    assert repr(message["op"]) in reply["error"]["message"]
    client.send({"op": "installed", "id": 3})
    assert client.receive()["id"] == 3


def test_reload_with_a_missing_served_file_is_a_typed_error(tmp_path: Path) -> None:
    served = tmp_path / "missing.json"
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    server = Running(engine, served)
    try:
        served.unlink()  # written at startup, removed while the engine runs
        client = server.client()
        client.send({"op": "reload", "id": 4})
        reply = client.receive()
        assert reply["ok"] is False and reply["error"]["type"] == "EngineError"
        assert str(served) in reply["error"]["message"]
    finally:
        server.close()


def test_reload_with_a_corrupt_served_file_is_a_typed_error(tmp_path: Path) -> None:
    served = tmp_path / "served.json"
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    server = Running(engine, served)
    try:
        served.write_text("not json")  # corrupted after the engine started (it is refused at startup too)
        client = server.client()
        client.send({"op": "reload", "id": 4})
        reply = client.receive()
        assert reply["ok"] is False and reply["error"]["type"] == "EngineError"
    finally:
        server.close()


# ---- final review fixes


class _Malformed(FakeEngine):
    """predict returns no probabilities at all: this escapes the scheduler's per-group error handling and breaks
    the GPU thread, as an unexpected fault would."""
    def predict(self, adapter_id: str, rows: list[dict]) -> EngineResult:
        return EngineResult(None, {})  # type: ignore[arg-type]


def test_health_reports_a_broken_gpu_thread_with_its_cause() -> None:
    server = Running(_Malformed(lambda adapter, row: [0.5, 0.5], {"a.b.c"}))
    try:
        client = server.client()
        client.send({"op": "health", "id": 1})
        healthy = client.receive()["result"]
        assert healthy["status"] == "ready" and healthy["adapters_version"] == 0 and "cause" not in healthy
        client.send({"op": "predict", "id": 2, "adapter": "a.b.c", "rows": [ROW]})
        assert client.receive()["error"]["type"] == "EngineError"
        client.send({"op": "health", "id": 3})
        health = client.receive()["result"]
    finally:
        server.close()
    assert health["status"] == "broken" and "GPU thread failed" in health["cause"]
    assert health["adapters_version"] == 0 and health["backend"] == "fake"


def test_a_schema_invalid_frame_with_an_id_fails_that_request_only(running: Running) -> None:
    client = running.client()
    client.send({"op": "predict", "id": 3, "adapter": "a.b.c", "rows": [ROW]})
    client.send_raw(msgpack.packb({"op": "predict", "id": 4, "adapter": "a.b.c", "rows": [ROW], "deadline_ms": 0}))
    replies = {reply["id"]: reply for reply in (client.receive(), client.receive())}
    assert replies[3]["ok"] is True
    rejected = replies[4]
    assert rejected["ok"] is False and rejected["error"]["type"] == "InvalidRequest"
    message = rejected["error"]["message"]
    assert "'predict'" in message and "deadline_ms" in message and "\n" not in message and len(message) < 400
    client.send({"op": "installed", "id": 5})  # the connection is still open
    assert client.receive()["id"] == 5


def test_a_schema_invalid_frame_without_a_usable_id_fails_the_connection(running: Running) -> None:
    for body in ({"op": "stats"}, {"op": "stats", "id": 0}, {"op": "stats", "id": True}, [1, 2]):
        client = running.client()
        client.send_raw(msgpack.packb(body))
        reply = client.receive()
        assert reply["id"] == 0 and reply["error"]["type"] == "InvalidRequest", body
        with pytest.raises(EngineError, match="closed"):
            client.receive()


def test_a_slow_check_does_not_delay_another_connections_replies() -> None:
    class SlowCheck(FakeEngine):
        def check(self, adapter_id: str, rows: list[dict]) -> None:
            time.sleep(0.5)
            super().check(adapter_id, rows)

    server = Running(SlowCheck(lambda adapter, row: [0.4, 0.6], {"a.b.c"}))
    try:
        slow, other = server.client(), server.client()
        slow.send({"op": "predict", "id": 1, "adapter": "a.b.c", "rows": [ROW]})
        time.sleep(0.05)  # the check is running
        started = time.monotonic()
        other.send({"op": "installed", "id": 1})
        assert other.receive()["ok"] is True
        other.send({"op": "stats", "id": 2})
        assert other.receive()["ok"] is True
        elapsed = time.monotonic() - started
        assert slow.receive()["ok"] is True
    finally:
        server.close()
    assert elapsed < 0.2, f"installed and stats waited {elapsed:.3f}s behind another connection's check"


def test_predict_replies_carry_the_adapters_version_the_batch_ran_with() -> None:
    engine = SlowPredict(0.3, {"a.b.c"})
    server = Running(engine)
    try:
        client = server.client()
        client.send({"op": "predict", "id": 1, "adapter": "a.b.c", "rows": [ROW]})
        time.sleep(0.1)  # batch 1 is running at version 0
        client.send({"op": "predict", "id": 2, "adapter": "a.b.c", "rows": [ROW]})  # queued at version 0
        time.sleep(0.05)
        client.send({"op": "serve", "id": 3, "adapters": ["a.b.c", "a.b.d"]})  # runs before the queued batch
        replies = {reply["id"]: reply for reply in (client.receive(), client.receive(), client.receive())}
    finally:
        server.close()
    assert replies[1]["result"]["adapters_version"] == 0
    assert replies[3]["result"]["adapters_version"] == 1
    assert replies[2]["result"]["adapters_version"] == 1


@pytest.mark.parametrize("content", ['{"served": ["a.b.c"]}', '{"format": "other", "version": 1, "adapters": []}',
                                     '{"format": "jeff-engine-served", "version": 2, "adapters": []}',
                                     '{"format": "jeff-engine-served", "version": 1, "adapters": [3]}'])
def test_a_served_file_in_another_format_is_never_read_or_overwritten(tmp_path: Path, content: str) -> None:
    served = tmp_path / "served.json"
    served.write_text(content)
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    with pytest.raises(EngineError, match=f"{served}.*jeff-engine-served"):
        EngineServer(engine, None, tmp_path / "e.sock", [], {"a.b.c": Path("/x")}, served, BASE, pinned=set(), prepare_workers=1)  # type: ignore[arg-type]
    good = tmp_path / "good.json"
    server = Running(engine, good)
    try:
        good.write_text(content)  # replaced by something else while the engine runs
        client = server.client()
        client.send({"op": "reload", "id": 1})
        reload_reply = client.receive()
        client.send({"op": "serve", "id": 2, "adapters": ["a.b.d"]})
        serve_reply = client.receive()
    finally:
        server.close()
    for reply in (reload_reply, serve_reply):
        assert reply["ok"] is False and reply["error"]["type"] == "EngineError"
        assert str(good) in reply["error"]["message"] and "jeff-engine-served" in reply["error"]["message"]
    assert good.read_text() == content
    assert engine.installed() == {"a.b.c"}  # the refused serve changed nothing


def test_reload_re_applies_the_served_list(tmp_path: Path) -> None:
    served = tmp_path / "served.json"
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    server = Running(engine, served)
    try:
        client = server.client()
        served.write_text(json.dumps({"format": "jeff-engine-served", "version": 1, "adapters": ["a.b.d"]}))
        client.send({"op": "reload", "id": 1})
        reply = client.receive()
    finally:
        server.close()
    assert reply["result"] == {"adapters": ["a.b.d"], "adapters_version": 1}
    assert engine.installed() == {"a.b.d"}


def test_close_removes_the_socket_file() -> None:
    server = Running(FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"}))
    assert server.path.exists()
    server.close()
    assert not server.path.exists()


def test_losing_the_bind_race_is_an_engine_error_naming_the_path(monkeypatch: pytest.MonkeyPatch) -> None:
    path = Path(tempfile.mkdtemp(prefix="jeff")) / "e.sock"  # short: AF_UNIX paths are limited to ~100 bytes
    rival = socket.socket(socket.AF_UNIX)
    real_umask = engine_server.os.umask

    def rival_binds_first(mask: int) -> int:
        # runs between the server's "is anyone serving here?" check and its own bind: another engine wins
        if not path.exists():
            rival.bind(str(path))
            rival.listen()
        return real_umask(mask)

    monkeypatch.setattr(engine_server.os, "umask", rival_binds_first)
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"})
    clash = EngineServer(engine, None, path, [], {"a.b.c": Path("/x")}, None, BASE, pinned=set(), prepare_workers=1)  # type: ignore[arg-type]
    try:
        with pytest.raises(EngineError, match=str(path)):
            asyncio.run(clash.run())
    finally:
        rival.close()


def _socket_appears(path: Path, seconds: float = 120.0) -> bool:
    """Whether the engine's socket appears within `seconds`. The driver threads used to give up after 5 s; a main()
    that took longer to start then served forever, with nobody left to stop it, and the test hung. Waiting longer
    keeps that from happening; when main() refuses before serving, the thread gives up quietly after `seconds`
    (a daemon thread: the test has already returned). It never signals the process: a SIGTERM after main() has
    returned would end the whole pytest run."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if path.exists():
            return True
        time.sleep(0.01)
    return False


def test_runtime_serve_exits_non_zero_once_the_scheduler_breaks(monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    import huggingface_hub
    import jeff_runtime

    import jeff.cli
    from jeff_runtime.testing import Client

    monkeypatch.setattr(jeff_runtime, "LocalEngine",
                        lambda base, available, **options: _Malformed(lambda a, r: [0.5, 0.5], set(available)))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, revision: f"/cache/{repo}@{revision}")
    path = Path(tempfile.mkdtemp(prefix="jeff")) / "e.sock"

    def break_it() -> None:
        if not _socket_appears(path):
            return  # main() refused before serving (the test's assertions then fail)
        client = Client(path, PROTOCOL_VERSION)
        client.send({"op": "predict", "id": 1, "adapter": "jeff.support.triage", "rows": [ROW]})
        client.receive()

    thread = threading.Thread(target=break_it, daemon=True)
    thread.start()
    # This resolves the real shipped catalogue's base and adapter through huggingface_hub.snapshot_download (mocked
    # above). --backend torch (LocalEngine is a fake
    # here): left on auto, a CUDA machine resolves to the fast backend, which also requires --max-adapters and
    # --prefix-pool-mb
    code = jeff.cli.main(["runtime", "serve", "--socket", str(path), "--adapter", "triage", "--backend", "torch",
                          "--batch-window-ms", "1", "--max-batch-rows", "4", "--queue-limit-rows", "8", "--prepare-workers", "2"])
    thread.join(5)
    assert code == 1
    assert "GPU thread failed" in capsys.readouterr().err
    assert not path.exists()


def test_predict_replies_carry_each_rows_input_tokens(running: Running) -> None:
    client = running.client()
    other = {"state": "a b c", "question": ROW["question"]}
    client.send({"op": "predict", "id": 1, "adapter": "a.b.c", "rows": [ROW, other]})
    assert client.receive()["result"]["input_tokens"] == [fake_tokens(ROW), fake_tokens(other)]


def test_serve_never_removes_the_base_and_accepts_it_in_the_list(tmp_path: Path) -> None:
    """The base (adapter id "base") is always served: a serve without it leaves it, a serve naming it is not an
    unknown adapter, and the served file lists only the adapters (the base is not one)."""
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"base", "a.b.c"})
    served = tmp_path / "served.json"
    server = Running(engine, served)
    try:
        client = server.client()
        client.send({"op": "serve", "id": 1, "adapters": ["a.b.d"]})
        assert client.receive()["result"]["adapters"] == ["a.b.d", "base"]
        assert json.loads(served.read_text())["adapters"] == ["a.b.d"]
        client.send({"op": "serve", "id": 2, "adapters": ["base", "a.b.c"]})
        assert client.receive()["result"]["adapters"] == ["a.b.c", "base"]
        client.send({"op": "reload", "id": 3})
        assert client.receive()["result"]["adapters"] == ["a.b.c", "base"]
    finally:
        server.close()
    assert engine.installed() == {"base", "a.b.c"}


def test_health_reports_the_bases_checkpoint_and_option_limit() -> None:
    """For jeff-server's health fields (checkpoint, max_options), which the API reports from the base."""
    engine = FakeEngine(lambda adapter, row: [0.5, 0.5], {"base", "a.b.c"}, max_options={"base": 26, "a.b.c": 3})
    server = Running(engine)
    try:
        client = server.client()
        client.send({"op": "health", "id": 1})
        health = client.receive()["result"]
    finally:
        server.close()
    assert health["base"] == {"id": "jeff-base", "version": "v1.3", "checkpoint": "fake-base", "max_options": 26,
                              "format": "bf16"}


def test_health_and_predict_timing_report_an_int4_bases_format() -> None:
    """An engine serving an INT4 base names its format and the exact INT4 base (format_id) in health.base and in
    every predict timing."""
    engine = FakeEngine(lambda adapter, row: [0.5, 0.5], {"base"}, max_options={"base": 26})
    engine.base_format = {"format": "int4-g64", "format_id": "jeff-base@v1.3+int4-g64-gptq.3f9a1c2e"}
    server = Running(engine)
    try:
        client = server.client()
        client.send({"op": "health", "id": 1})
        health = client.receive()["result"]
        client.send({"op": "predict", "id": 2, "adapter": "base",
                     "rows": [{"state": "s", "question": {"type": "noul"}}]})
        timing = client.receive()["result"]["timing"]
    finally:
        server.close()
    assert health["base"] == {"id": "jeff-base", "version": "v1.3", "checkpoint": "fake-base", "max_options": 26,
                              "format": "int4-g64", "format_id": "jeff-base@v1.3+int4-g64-gptq.3f9a1c2e"}
    assert (timing["format"], timing["format_id"]) == ("int4-g64", "jeff-base@v1.3+int4-g64-gptq.3f9a1c2e")


# ---- adapter folders (--adapters-dir) and starting from the served file


class Recording(FakeEngine):
    """A fake engine that records every add_adapter and remove_adapter, in order, with the folder added from."""
    def __init__(self, installed: set[str]) -> None:
        super().__init__(lambda adapter, row: [0.4, 0.6], installed)
        self.changes: list[tuple[str, str, Path | None]] = []

    def add_adapter(self, adapter_id: str, path: Path) -> None:
        super().add_adapter(adapter_id, path)
        self.changes.append(("add", adapter_id, path))

    def remove_adapter(self, adapter_id: str) -> None:
        super().remove_adapter(adapter_id)
        self.changes.append(("remove", adapter_id, None))


def _served(path: Path, adapters: list[str]) -> Path:
    path.write_text(json.dumps({"format": "jeff-engine-served", "version": 1, "adapters": adapters}))
    return path


def _ask(client, message: dict) -> dict:
    client.send(message)
    return client.receive()


def test_a_folder_dropped_in_is_servable_after_reload(tmp_path: Path) -> None:
    roots = tmp_path / "adapters"
    roots.mkdir()
    engine = Recording({"a.b.c"})
    served = _served(tmp_path / "served.json", ["a.b.c"])
    server = Running(engine, served, adapter_roots=[roots])
    try:
        client = server.client()
        assert _ask(client, {"op": "serve", "id": 1, "adapters": ["a.b.c", "jeff.support.new"]})["error"]["type"] \
            == "UnknownFunction"
        folder = fake_adapter_folder(roots / "apps/new", "jeff.support.new")
        reload_reply = _ask(client, {"op": "reload", "id": 2})
        assert reload_reply["result"] == {"adapters": ["a.b.c"], "adapters_version": 0}  # nothing reloaded
        serve_reply = _ask(client, {"op": "serve", "id": 3, "adapters": ["a.b.c", "jeff.support.new"]})
    finally:
        server.close()
    assert serve_reply["result"] == {"adapters": ["a.b.c", "jeff.support.new"], "adapters_version": 1}
    assert engine.changes == [("add", "jeff.support.new", folder)]
    assert json.loads(served.read_text())["adapters"] == ["a.b.c", "jeff.support.new"]


def test_reload_loads_a_changed_adapter_again_and_bumps_the_version(tmp_path: Path) -> None:
    roots = tmp_path / "adapters"
    folder = fake_adapter_folder(roots / "x", "jeff.support.x")
    other = fake_adapter_folder(roots / "y", "jeff.support.y")
    engine = Recording({"jeff.support.x", "jeff.support.y"})
    served = _served(tmp_path / "served.json", ["jeff.support.x", "jeff.support.y"])
    server = Running(engine, served, fixed={}, adapter_roots=[roots])
    try:
        client = server.client()
        readout = folder / "readout.safetensors"
        readout.write_bytes(b"retrained")
        info = readout.stat()
        os.utime(readout, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
        reply = _ask(client, {"op": "reload", "id": 1})
        again = _ask(client, {"op": "reload", "id": 2})  # nothing changed since: nothing is reloaded
    finally:
        server.close()
    assert reply["result"] == {"adapters": ["jeff.support.x", "jeff.support.y"], "adapters_version": 1}
    assert engine.changes == [("remove", "jeff.support.x", None), ("add", "jeff.support.x", folder)]
    assert again["result"]["adapters_version"] == 1
    assert other.exists()


def test_a_duplicate_id_on_reload_is_refused_and_changes_nothing(tmp_path: Path) -> None:
    roots = tmp_path / "adapters"
    fake_adapter_folder(roots / "x", "jeff.support.x")
    engine = Recording({"jeff.support.x"})
    served = _served(tmp_path / "served.json", ["jeff.support.x"])
    server = Running(engine, served, fixed={}, adapter_roots=[roots])
    try:
        client = server.client()
        fake_adapter_folder(roots / "copy-of-x", "jeff.support.x")
        _served(served, [])  # the reload would remove x, were it not refused first
        reply = _ask(client, {"op": "reload", "id": 1})
        installed = _ask(client, {"op": "installed", "id": 2})
    finally:
        server.close()
    assert reply["ok"] is False and reply["error"]["type"] == "ManifestError"
    assert "jeff.support.x" in reply["error"]["message"] and str(roots / "copy-of-x") in reply["error"]["message"]
    assert installed["result"] == {"adapters": ["jeff.support.x"], "adapters_version": 0}
    assert engine.changes == []


def test_reload_refuses_when_a_served_adapters_folder_has_gone(tmp_path: Path) -> None:
    roots = tmp_path / "adapters"
    folder = fake_adapter_folder(roots / "x", "jeff.support.x")
    engine = Recording({"a.b.c", "jeff.support.x"})
    served = _served(tmp_path / "served.json", ["a.b.c", "jeff.support.x"])
    server = Running(engine, served, adapter_roots=[roots])
    try:
        client = server.client()
        shutil.rmtree(folder)
        reply = _ask(client, {"op": "reload", "id": 1})
    finally:
        server.close()
    assert reply["ok"] is False and reply["error"]["type"] == "ManifestError"
    assert "jeff.support.x" in reply["error"]["message"]
    assert engine.installed() == {"a.b.c", "jeff.support.x"} and engine.changes == []


def test_a_discovered_id_also_given_with_path_is_refused(tmp_path: Path) -> None:
    roots = tmp_path / "adapters"
    fake_adapter_folder(roots / "x", "jeff.support.x")
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], set())
    with pytest.raises(ManifestError, match="jeff.support.x"):
        EngineServer(engine, None, tmp_path / "e.sock", [roots], {"jeff.support.x": Path("/x")}, None, BASE,  # type: ignore[arg-type]
                     pinned={"jeff.support.x"}, prepare_workers=1)


def test_a_discovered_adapter_on_another_base_is_refused(tmp_path: Path) -> None:
    roots = tmp_path / "adapters"
    fake_adapter_folder(roots / "x", "jeff.support.x", base_version="v9.9")
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], set())
    with pytest.raises(ManifestError, match=r"jeff-base@v9\.9.*jeff-base@v1\.3"):
        EngineServer(engine, None, tmp_path / "e.sock", [roots], {}, None, BASE, pinned=set(), prepare_workers=1)  # type: ignore[arg-type]


def test_an_installed_adapter_with_no_folder_is_refused_at_startup(tmp_path: Path) -> None:
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"base", "a.b.z"})
    with pytest.raises(EngineError, match="a.b.z"):
        EngineServer(engine, None, tmp_path / "e.sock", [], {}, None, BASE, pinned=set(), prepare_workers=1)  # type: ignore[arg-type]


def test_the_served_file_is_written_at_startup_when_missing(tmp_path: Path) -> None:
    served = tmp_path / "served.json"
    server = Running(FakeEngine(lambda adapter, row: [0.4, 0.6], {"base", "a.b.c"}), served)
    server.close()
    assert json.loads(served.read_text()) == {"format": "jeff-engine-served", "version": 1, "adapters": ["a.b.c"]}


# ---- `jeff runtime serve` startup, with a fake LocalEngine and no downloads


def _run_cli(monkeypatch: pytest.MonkeyPatch, argv: list[str], during=None) -> tuple[int, list, dict | None]:
    """Runs `jeff runtime serve` in this (the main) thread with LocalEngine replaced by a FakeEngine and downloads
    replaced by fixed folders; a helper thread waits for the socket, asks `installed` (and runs `during`, given),
    then stops the engine with SIGTERM. Returns the exit code, the LocalEngine calls (base, adapters) and the
    `installed` reply (None when the engine never listened)."""
    import huggingface_hub
    import jeff_runtime

    import jeff.cli

    made: list = []

    def local_engine(base: Path, adapters: dict, **options: object) -> FakeEngine:
        made.append((base, dict(adapters)))
        return FakeEngine(lambda a, r: [0.5, 0.5], {"base", *adapters})

    monkeypatch.setattr(jeff_runtime, "LocalEngine", local_engine)
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, revision: f"/cache/{repo}@{revision}")
    socket_path = Path(tempfile.mkdtemp(prefix="jeff")) / "e.sock"
    seen: dict = {}

    def drive() -> None:
        from jeff_runtime.testing import Client

        if not _socket_appears(socket_path):
            return  # main() refused before serving (the tests that expect a refusal)
        client = Client(socket_path, PROTOCOL_VERSION)
        seen["installed"] = _ask(client, {"op": "installed", "id": 1})
        if during is not None:
            during(client)
        os.kill(os.getpid(), signal.SIGTERM)

    thread = threading.Thread(target=drive, daemon=True)
    thread.start()
    # --backend torch (LocalEngine is a fake here): left on auto, a CUDA machine resolves to the fast backend, which
    # also requires --max-adapters and --prefix-pool-mb
    code = jeff.cli.main(["runtime", "serve", "--socket", str(socket_path), *argv, "--backend", "torch",
                          "--batch-window-ms", "1", "--max-batch-rows", "4", "--queue-limit-rows", "8", "--prepare-workers", "2"])
    if code == 0:
        thread.join(5)
    return code, made, seen.get("installed")


def test_the_engine_starts_from_an_existing_served_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The catalogue's base and triage resolve through huggingface_hub.snapshot_download (mocked in _run_cli)."""
    roots = tmp_path / "adapters"
    folder = fake_adapter_folder(roots / "apps/x", "jeff.support.x", base_version=CATALOGUE_BASE)
    served = _served(tmp_path / "served.json", ["jeff.support.triage", "jeff.support.x"])
    code, made, installed = _run_cli(monkeypatch, ["--served-file", str(served), "--adapters-dir", str(roots)])
    assert code == 0
    assert made == [(Path("/cache/mstrasser/jeff-base@v1.4"),
                     {"jeff.support.triage": Path("/cache/mstrasser/jeff-adapter-triage@v1.4"),
                      "jeff.support.x": folder})]
    assert installed["result"]["adapters"] == ["base", "jeff.support.triage", "jeff.support.x"]


def test_adapter_flags_with_an_existing_served_file_are_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    served = _served(tmp_path / "served.json", ["jeff.support.triage"])
    code, made, _ = _run_cli(monkeypatch, ["--served-file", str(served), "--adapter", "spam"])
    assert code == 1 and made == []
    assert "two sources for the starting adapters" in capsys.readouterr().err


def test_a_first_start_writes_the_served_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    served = tmp_path / "served.json"
    code, made, installed = _run_cli(monkeypatch, ["--served-file", str(served), "--adapter", "triage"])
    assert code == 0 and list(made[0][1]) == ["jeff.support.triage"]
    assert json.loads(served.read_text())["adapters"] == ["jeff.support.triage"]


def test_a_start_with_no_adapters_needs_the_base_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    served = tmp_path / "served.json"
    code, made, _ = _run_cli(monkeypatch, ["--served-file", str(served)])
    assert code == 1 and made == []
    err = capsys.readouterr().err
    assert "--adapter" in err and "--served-file" in err and "--base-version" in err
    code, made, installed = _run_cli(monkeypatch, ["--served-file", str(served), "--base-version", "jeff-base@v1.4"])
    assert code == 0 and made == [(Path("/cache/mstrasser/jeff-base@v1.4"), {})]
    assert installed["result"]["adapters"] == ["base"]
    assert json.loads(served.read_text())["adapters"] == []


def test_an_empty_served_file_needs_the_base_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     capsys: pytest.CaptureFixture[str]) -> None:
    served = _served(tmp_path / "served.json", [])
    code, made, _ = _run_cli(monkeypatch, ["--served-file", str(served)])
    assert code == 1 and made == []
    assert "--base-version" in capsys.readouterr().err
    code, made, _ = _run_cli(monkeypatch, ["--served-file", str(served), "--base-version", "jeff-base@v1.4"])
    assert code == 0 and made == [(Path("/cache/mstrasser/jeff-base@v1.4"), {})]


def test_a_base_version_that_disagrees_with_the_adapters_is_refused(monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    code, made, _ = _run_cli(monkeypatch, ["--adapter", "triage", "--base-version", "jeff-base@v9.9"])
    assert code == 1 and made == []
    err = capsys.readouterr().err
    assert "jeff-base@v9.9" in err and "jeff-base@v1.4" in err


def test_a_discovered_adapter_given_with_path_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    roots = tmp_path / "adapters"
    fake_adapter_folder(roots / "x", "jeff.support.x", base_version=CATALOGUE_BASE)
    code, made, _ = _run_cli(monkeypatch, ["--adapter", "triage", "--adapters-dir", str(roots),
                                           "--path", f"jeff.support.x={tmp_path}"])
    assert code == 1 and made == []
    assert "jeff.support.x" in capsys.readouterr().err


def test_a_catalogue_adapter_also_in_an_adapters_dir_is_served_from_the_folder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    roots = tmp_path / "adapters"
    folder = fake_adapter_folder(roots / "triage", "jeff.support.triage", base_version=CATALOGUE_BASE)
    code, made, _ = _run_cli(monkeypatch, ["--adapter", "triage", "--adapters-dir", str(roots)])
    assert code == 0 and made[0][1] == {"jeff.support.triage": folder}


def test_a_catalogue_adapter_whose_folder_appears_is_loaded_from_it_on_reload(tmp_path: Path) -> None:
    """A catalogue download (fixed, not pinned by --path) that is later found in an adapters dir: the folder, the
    server's own copy, wins, and the same reload loads the adapter again from it."""
    roots = tmp_path / "adapters"
    roots.mkdir()
    engine = Recording({"jeff.support.triage"})
    served = _served(tmp_path / "served.json", ["jeff.support.triage"])
    server = Running(engine, served, fixed={"jeff.support.triage": Path("/cache/triage")}, pinned=set(),
                     adapter_roots=[roots])
    try:
        client = server.client()
        folder = fake_adapter_folder(roots / "triage", "jeff.support.triage")
        reply = _ask(client, {"op": "reload", "id": 1})
        again = _ask(client, {"op": "reload", "id": 2})
    finally:
        server.close()
    assert reply["result"] == {"adapters": ["jeff.support.triage"], "adapters_version": 1}
    assert engine.changes == [("remove", "jeff.support.triage", None), ("add", "jeff.support.triage", folder)]
    assert again["result"]["adapters_version"] == 1


def test_the_first_reload_compares_against_the_scan_the_engine_was_loaded_from(tmp_path: Path) -> None:
    """The CLI scans, loads the engine (minutes, on a real base), then builds the server with that scan: files
    rewritten during the load are seen as changed by the first reload."""
    from jeff.adapter_dirs import scan

    roots = tmp_path / "adapters"
    folder = fake_adapter_folder(roots / "x", "jeff.support.x")
    found = scan([roots])
    readout = folder / "readout.safetensors"  # rewritten while the engine was loading
    readout.write_bytes(b"newer")
    info = readout.stat()
    os.utime(readout, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    engine = Recording({"jeff.support.x"})
    served = _served(tmp_path / "served.json", ["jeff.support.x"])
    server = Running(engine, served, fixed={}, adapter_roots=[roots], found=found)
    try:
        reply = _ask(server.client(), {"op": "reload", "id": 1})
    finally:
        server.close()
    assert reply["result"]["adapters_version"] == 1
    assert engine.changes == [("remove", "jeff.support.x", None), ("add", "jeff.support.x", folder)]


def test_a_reload_refused_for_an_unknown_adapter_keeps_the_old_available_set(tmp_path: Path) -> None:
    roots = tmp_path / "adapters"
    roots.mkdir()
    engine = Recording({"a.b.c"})
    served = _served(tmp_path / "served.json", ["a.b.c"])
    server = Running(engine, served, adapter_roots=[roots])
    try:
        client = server.client()
        fake_adapter_folder(roots / "x", "jeff.support.x")  # found by this reload, which is then refused
        _served(served, ["a.b.c", "jeff.nope.zzz"])
        assert _ask(client, {"op": "reload", "id": 1})["error"]["type"] == "UnknownFunction"
        serve_reply = _ask(client, {"op": "serve", "id": 2, "adapters": ["a.b.c", "jeff.support.x"]})
    finally:
        server.close()
    # the refused reload did not replace the available set: x is servable only after a reload that succeeds
    assert serve_reply["ok"] is False and serve_reply["error"]["type"] == "UnknownFunction"
    assert engine.changes == []


def test_an_empty_served_file_needs_the_base_version_even_with_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    served = _served(tmp_path / "served.json", [])
    code, made, _ = _run_cli(monkeypatch, ["--served-file", str(served), "--path", f"triage={tmp_path}"])
    assert code == 1 and made == []
    assert "--base-version" in capsys.readouterr().err


def test_a_found_alias_that_names_another_catalogue_adapter_is_refused(tmp_path: Path,
                                                                        monkeypatch: pytest.MonkeyPatch,
                                                                        capsys: pytest.CaptureFixture[str]) -> None:
    import yaml

    roots = tmp_path / "adapters"
    folder = fake_adapter_folder(roots / "x", "jeff.support.x", base_version=CATALOGUE_BASE)
    raw = yaml.safe_load((folder / "manifest.yaml").read_text())
    raw["aliases"] = ["spam"]  # the catalogue's alias for jeff.safety.spam
    (folder / "manifest.yaml").write_text(yaml.safe_dump(raw))
    code, made, _ = _run_cli(monkeypatch, ["--adapter", "triage", "--adapters-dir", str(roots)])
    assert code == 1 and made == []
    err = capsys.readouterr().err
    assert "spam" in err and "jeff.support.x" in err and "jeff.safety.spam" in err


# ---------------------------------------------------------------- the engine's adapter capacity (--max-adapters)


def test_a_serve_over_the_engines_capacity_changes_nothing(tmp_path: Path) -> None:
    """An engine holding at most 2 adapters, asked to serve 3 (one of them already loaded): refused before the
    first change, so nothing is unloaded and the served file is not written."""
    folders = {name: Path(f"/{name}") for name in ("a.b.c", "a.b.d", "a.b.e", "a.b.f")}
    engine = Recording({"base", "a.b.c", "a.b.d"})
    engine.max_adapters = 2
    served = _served(tmp_path / "served.json", ["a.b.c", "a.b.d"])
    before = served.read_text()
    server = Running(engine, served, fixed=folders)
    try:
        client = server.client()
        reply = _ask(client, {"op": "serve", "id": 1, "adapters": ["a.b.c", "a.b.e", "a.b.f"]})
        installed = _ask(client, {"op": "installed", "id": 2})
        changes, after_refusal = list(engine.changes), served.read_text()
        fits = _ask(client, {"op": "serve", "id": 3, "adapters": ["base", "a.b.e", "a.b.f"]})  # the base does not count
    finally:
        server.close()
    assert reply["ok"] is False and reply["error"]["type"] == "EngineError"
    assert "3 adapters were asked for" in reply["error"]["message"]
    assert "--max-adapters 2" in reply["error"]["message"]
    assert installed["result"] == {"adapters": ["a.b.c", "a.b.d", "base"], "adapters_version": 0}
    assert changes == [] and after_refusal == before
    assert fits["ok"] is True, fits
    assert json.loads(served.read_text())["adapters"] == ["a.b.e", "a.b.f"]


def test_a_reload_whose_served_file_is_over_capacity_changes_nothing(tmp_path: Path) -> None:
    folders = {name: Path(f"/{name}") for name in ("a.b.c", "a.b.d", "a.b.e")}
    engine = Recording({"base", "a.b.c"})
    engine.max_adapters = 2
    served = _served(tmp_path / "served.json", ["a.b.c"])
    server = Running(engine, served, fixed=folders)
    try:
        client = server.client()
        _served(served, ["a.b.d", "a.b.e", "a.b.c"])  # three: one more than the engine holds
        written = served.read_text()
        reply = _ask(client, {"op": "reload", "id": 1})
        installed = _ask(client, {"op": "installed", "id": 2})
    finally:
        server.close()
    assert reply["ok"] is False and reply["error"]["type"] == "EngineError"
    assert "3 adapters were asked for" in reply["error"]["message"]
    assert installed["result"] == {"adapters": ["a.b.c", "base"], "adapters_version": 0}
    assert engine.changes == []
    assert served.read_text() == written


def test_the_fake_engine_refuses_an_adapter_past_its_capacity() -> None:
    engine = FakeEngine(lambda adapter, row: [0.4, 0.6], {"base", "a.b.c"}, max_adapters=1)
    with pytest.raises(EngineError, match=r"at most 1 adapters \(--max-adapters 1\) and 1 are loaded"):
        engine.add_adapter("a.b.d", Path("/y"))
    assert engine.installed() == {"base", "a.b.c"}


def test_wait_unused_returns_only_once_no_client_has_been_connected_for_that_long() -> None:
    server = Running(FakeEngine(lambda adapter, row: [0.4, 0.6], {"a.b.c"}))
    try:
        client = server.client()
        client.send({"op": "health", "id": 1})
        client.receive()
        started = time.monotonic()
        waiting = asyncio.run_coroutine_threadsafe(server.server.wait_unused(0.3), server.loop)
        time.sleep(0.6)
        assert not waiting.done()  # a client is still connected, however long it has been quiet
        client.disconnect()
        waiting.result(5)
        assert time.monotonic() - started >= 0.9  # 0.6 s connected, then 0.3 s with no client
        again = asyncio.run_coroutine_threadsafe(server.server.wait_unused(0.4), server.loop)
        time.sleep(0.2)
        server.client().disconnect()  # a client that comes and goes starts the count again
        again.result(5)
    finally:
        server.close()


def test_runtime_serve_exits_cleanly_once_unused(monkeypatch: pytest.MonkeyPatch,
                                                 caplog: pytest.LogCaptureFixture) -> None:
    import huggingface_hub
    import jeff_runtime

    import jeff.cli
    from jeff_runtime.testing import Client

    monkeypatch.setattr(jeff_runtime, "LocalEngine",
                        lambda base, available, **options: FakeEngine(lambda a, r: [0.5, 0.5], set(available)))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, revision: f"/cache/{repo}@{revision}")
    path = Path(tempfile.mkdtemp(prefix="jeff")) / "e.sock"
    seen: dict[str, object] = {}

    def use_then_leave() -> None:
        if not _socket_appears(path):
            return
        client = Client(path, PROTOCOL_VERSION)
        client.send({"op": "health", "id": 1})
        seen["health"] = client.receive()["result"]["status"]
        time.sleep(0.5)
        seen["left"] = time.monotonic()
        client.disconnect()

    thread = threading.Thread(target=use_then_leave, daemon=True)
    thread.start()
    code = jeff.cli.main(["runtime", "serve", "--socket", str(path), "--adapter", "triage", "--backend", "torch",
                          "--batch-window-ms", "1", "--max-batch-rows", "4", "--queue-limit-rows", "8",
                          "--prepare-workers", "2", "--exit-when-unused-s", "0.3"])
    ended = time.monotonic()
    thread.join(5)
    assert code == 0
    assert seen["health"] == "ready"
    assert ended - seen["left"] >= 0.3  # type: ignore[operator]
    assert "No client has been connected for 0.3 seconds" in caplog.text
    assert not path.exists()
