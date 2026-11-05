"""`jeff-server serve`: argument handling, plus the fast end of its failure modes under `--workers > 1` (a real
uvicorn subprocess with a broken environment, which must exit promptly and loudly rather than respawning workers
forever -- see cli.py's `_check_ready_to_serve` and `app_factory` docstrings). The slow HTTP golden test
(test_http_golden.py) runs the whole thing against a real engine process instead.

Checks here: `--host`, `--port` and `--workers` are all required (no defaults -- binding to every interface or
running several worker processes are deliberate choices, never silent ones); each call to the uvicorn import-string
factory (`jeff_server.cli:app_factory`) builds a fresh app from the current environment, so that when uvicorn starts
several worker processes each one builds its own app and its own connections to the engine; and a broken
environment (a missing setting, or an engine socket that does not answer) makes `jeff-server serve --workers N`
exit quickly and cleanly, for any N, before any port is bound or any worker is spawned."""

import os
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from jeff_server.cli import app_factory, main as cli

from server_testing import free_port


def test_serve_without_any_arguments_fails_loudly() -> None:
    with pytest.raises(SystemExit):
        cli(["serve"])


def test_serve_without_host_fails_loudly() -> None:
    with pytest.raises(SystemExit):
        cli(["serve", "--port", "8000", "--workers", "1"])


def test_serve_without_port_fails_loudly() -> None:
    with pytest.raises(SystemExit):
        cli(["serve", "--host", "127.0.0.1", "--workers", "1"])


def test_serve_without_workers_fails_loudly() -> None:
    with pytest.raises(SystemExit):
        cli(["serve", "--host", "127.0.0.1", "--port", "8000"])


def test_serve_workers_must_be_an_integer() -> None:
    with pytest.raises(SystemExit):
        cli(["serve", "--host", "127.0.0.1", "--port", "8000", "--workers", "many"])


def test_serve_port_must_be_an_integer() -> None:
    with pytest.raises(SystemExit):
        cli(["serve", "--host", "127.0.0.1", "--port", "many", "--workers", "1"])


def test_app_factory_builds_a_fresh_app_from_the_environment(monkeypatch: pytest.MonkeyPatch,
                                                              tmp_path: Path) -> None:
    monkeypatch.setenv("JEFF_ENGINE_SOCKETS", str(tmp_path / "e.sock"))
    monkeypatch.setenv("JEFF_MODEL_NAME", "jeff-v1.3")
    first = app_factory()
    second = app_factory()
    assert first is not second  # a fresh app (and so a fresh engine, once started) per call, as per worker
    assert first.state.settings.model_name == "jeff-v1.3"
    assert first.state.settings.engine_sockets == (tmp_path / "e.sock",)


def test_app_factory_exits_with_uvicorns_startup_failure_code_without_the_required_settings(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """A worker process started by uvicorn with more than one `--workers` calls this factory directly from
    `Config.load()`, which only turns a *TypeError* into a clean exit; anything else -- such as the InvalidRequest
    ServerSettings.from_env raises here -- would otherwise propagate out of the worker uncaught, which looks to
    uvicorn's multi-worker supervisor like a worker that merely died (plain exit code 1) rather than one that is
    broken and must not be retried (exit code uvicorn.config.STARTUP_FAILURE) -- see app_factory's docstring and
    the Fix round 1 note in the task report for how that was confirmed to loop forever otherwise."""
    from uvicorn.config import STARTUP_FAILURE

    monkeypatch.delenv("JEFF_ENGINE_SOCKETS", raising=False)
    monkeypatch.delenv("JEFF_MODEL_NAME", raising=False)
    with pytest.raises(SystemExit) as raised:
        app_factory()
    assert raised.value.code == STARTUP_FAILURE
    assert "JEFF_ENGINE_SOCKETS" in capsys.readouterr().err


# --- a real uvicorn subprocess (fast: fails before any model, socket bind or worker is involved) -------------------

def _run_serve_subprocess(env: dict[str, str], workers: int = 2, timeout: float = 15.0) -> tuple[int, str, int]:
    """Runs `jeff-server serve` as a real subprocess (so it goes through the real `uvicorn.run`, not a mock),
    returns (exit code, combined stdout+stderr, the port it was told to bind). Always waits for the process to
    actually exit (or kills it by PID after `timeout` and fails the test) -- a hung process here would be exactly
    the respawn loop this fix round exists to prevent, so the test must never leave one running."""
    port = free_port()
    process = subprocess.Popen(
        [sys.executable, "-m", "jeff_server.cli", "serve", "--host", "127.0.0.1", "--port", str(port),
         "--workers", str(workers)],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        output, _ = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()  # stop it by PID -- never pkill
        process.wait(10)
        pytest.fail(f"jeff-server serve --workers {workers} did not exit within {timeout}s with this broken "
                    f"environment -- is it stuck respawning workers? partial output:\n{process.stdout}")
    assert process.returncode is not None
    return process.returncode, output, port


def _port_is_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        return probe.connect_ex(("127.0.0.1", port)) != 0


def test_serve_with_workers_and_a_missing_setting_exits_promptly_naming_it() -> None:
    env = {key: value for key, value in os.environ.items()
           if key not in ("JEFF_ENGINE_SOCKETS", "JEFF_MODEL_NAME")}
    code, output, port = _run_serve_subprocess(env, workers=2)
    assert code != 0, output
    assert "JEFF_ENGINE_SOCKETS" in output
    assert _port_is_free(port)  # the broken environment was caught before the port was ever bound


def test_serve_with_workers_and_an_unreachable_engine_socket_exits_promptly_naming_it(tmp_path: Path) -> None:
    missing_socket = tmp_path / "no-engine-here.sock"
    env = {**os.environ, "JEFF_ENGINE_SOCKETS": str(missing_socket), "JEFF_MODEL_NAME": "jeff-v1.3"}
    code, output, port = _run_serve_subprocess(env, workers=2)
    assert code != 0, output
    assert str(missing_socket) in output and "No engine is listening" in output
    assert _port_is_free(port)


def test_serve_bounds_each_websocket_connection_to_one_waiting_frame(monkeypatch: pytest.MonkeyPatch) -> None:
    """uvicorn is run with the channel's WebSocket options: messages up to one 50 MiB frame plus its header, the
    sansio implementation (which stops reading while one message waits for the app) and a queue of one message."""
    import uvicorn

    from jeff_server import cli as cli_module

    ran: dict[str, Any] = {}
    monkeypatch.setattr(cli_module, "_check_ready_to_serve", lambda env, workers: None)
    monkeypatch.setattr(uvicorn, "run", lambda app, **options: ran.update(app=app, **options))
    cli(["serve", "--host", "127.0.0.1", "--port", "8123", "--workers", "1"])
    assert ran["app"] == "jeff_server.cli:app_factory"
    assert ran["ws"] == "websockets-sansio"
    assert ran["ws_max_queue"] == 1
    assert ran["ws_max_size"] == 4 + 50 * 1024 * 1024
    assert ran["ws_per_message_deflate"] is False  # a compressed message could inflate past what was read


def test_a_client_offering_compression_is_served_uncompressed() -> None:
    """A real uvicorn with serve's WebSocket options: a client offering permessage-deflate (the websockets client
    does by default) gets no compression, so a frame takes as many bytes on the wire as in memory."""
    import threading
    import time

    import uvicorn
    from websockets.sync.client import connect

    from jeff.protocol import PROTOCOL_VERSION, decode, encode
    from jeff.testing import FakeEngine

    from jeff_server.app import create_app
    from jeff_server.cli import websocket_options
    from jeff_server.settings import ServerSettings

    app = create_app(ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3"),
                     engine=FakeEngine(lambda adapter, row: [1.0], set()))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           **websocket_options()))
    thread = threading.Thread(target=server.run)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            assert time.monotonic() < deadline, "uvicorn did not start"
            time.sleep(0.02)
        with connect(f"ws://127.0.0.1:{port}/v1/channel", compression="deflate") as ws:
            assert ws.protocol.extensions == []
            assert "sec-websocket-extensions" not in ws.response.headers
            ws.send(encode({"op": "hello", "version": PROTOCOL_VERSION}))
            reply = ws.recv()
            assert isinstance(reply, bytes) and decode(reply[4:])["op"] == "hello"
    finally:
        server.should_exit = True
        thread.join(10)
    assert not thread.is_alive()
