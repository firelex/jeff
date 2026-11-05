"""Jeff.remote (over REST and over the low-latency channel) and the MCP tools, against a real `jeff runtime serve`
engine process (MLX) and a real uvicorn API server on a free port -- the same two pieces test_http_golden.py and
test_mcp_mount.py each check separately, run together here end to end, with the SDK in between instead of a raw
HTTP body.

Starts one `jeff runtime serve` process for the whole module (loads the 1.7 GB base once), exactly as
test_http_golden.py's `socket_path` fixture does, serving the triage and spam adapters, and one real uvicorn
serving jeff-server in front of it, exactly as `packages/python/tests/served.py`'s `serving` helper does. Covers
the 6 golden cases for those two adapters (contract/golden/step1.json) through `Jeff.remote(..., channel=False)`
and again through `Jeff.remote(..., channel=True)`, checking the real model's answers against the *recorded*
golden values (`jeff.evaluate.golden_problems`); then one `jeff_dispatch`-free MCP tool call, over the MCP SDK's
own streamable HTTP client against `/mcp`, checked the same way."""

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import anyio
import httpx2
import pytest
import uvicorn
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.contract import shipped_dir
from jeff.decision import Decision
from jeff.evaluate import golden_problems
from jeff.mcp import tool_name

from jeff_server.app import create_app
from jeff_server.cli import websocket_options
from jeff_server.settings import ServerSettings

from server_testing import free_port

pytestmark = pytest.mark.slow
GOLDEN = json.loads((shipped_dir() / "contract" / "golden" / "step1.json").read_text())
CASES = [case for case in GOLDEN["cases"] if case["call"]["function"].startswith(("jeff.support.triage",
                                                                                   "jeff.safety.spam"))]
assert len(CASES) == 6, f"expected the 6 triage and spam golden cases, found {len(CASES)}"
TIMEOUT_S = 60.0  # generous: the real model, not a FakeEngine, answers every request below


@pytest.fixture(scope="module")
def socket_path() -> Iterator[Path]:
    """One `jeff runtime serve` process for the whole module, serving triage and spam, torn down by PID (never
    pkill) once every test here is done. Ported from test_http_golden.py's fixture of the same name."""
    tmp_dir = Path(tempfile.mkdtemp(prefix="jeff"))
    path = tmp_dir / "e.sock"
    stderr_path = tmp_dir / "engine.stderr"
    with open(stderr_path, "wb") as stderr_file:
        process = subprocess.Popen(
            [sys.executable, "-m", "jeff.cli", "runtime", "serve", "--socket", str(path),
             "--adapter", "triage", "--adapter", "spam",
             "--batch-window-ms", "1", "--max-batch-rows", "64", "--queue-limit-rows", "4096", "--prepare-workers", "2"],
            env={**os.environ}, stderr=stderr_file)
        try:
            for _ in range(600):
                if path.exists():
                    break
                if process.poll() is not None:
                    pytest.fail(f"jeff runtime serve exited with {process.returncode} before the socket "
                                f"appeared; stderr:\n{stderr_path.read_text(errors='replace')}")
                time.sleep(0.5)
            else:
                pytest.fail(f"jeff runtime serve never created {path} within 300s; stderr:\n"
                            f"{stderr_path.read_text(errors='replace')}")
            yield path
        finally:
            process.terminate()
            try:
                process.wait(30)
            except subprocess.TimeoutExpired:
                process.kill()  # it ignored SIGTERM: never leave a 1.7 GB model process behind
                process.wait(30)


@pytest.fixture(scope="module")
def url(socket_path: Path) -> Iterator[str]:
    """A real uvicorn serving jeff-server (its own engine connections to `socket_path`, as a deployed jeff-server
    would open) on a free port, for the whole module; stopped (and checked stopped) once every test is done."""
    settings = ServerSettings(engine_sockets=(socket_path,), model_name="jeff-v1.3", engine_timeout_s=TIMEOUT_S)
    app = create_app(settings)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           **websocket_options()))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 60
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"uvicorn did not start on port {port}")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=30)
        assert not thread.is_alive(), "uvicorn did not stop"


def _threshold(catalogue: Catalogue, case: dict[str, Any]) -> float | None:
    calibration = catalogue.function(case["call"]["function"]).calibration
    return calibration.threshold if calibration else None


def _problems(cases: list[dict[str, Any]], decisions: list[Decision], catalogue: Catalogue) -> list[str]:
    return [problem for case, decision in zip(cases, decisions, strict=True)
            for problem in golden_problems(case, decision, _threshold(catalogue, case))]


@pytest.mark.parametrize("channel", [False, True], ids=["rest", "channel"])
def test_golden_cases_through_remote_mode_against_the_real_engine(url: str, channel: bool) -> None:
    catalogue = Catalogue.shipped()
    calls = [Call(**case["call"]) for case in CASES]
    with Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=catalogue, channel=channel) as remote:
        decisions = remote.decide(calls)
    assert _problems(CASES, decisions, catalogue) == []


def test_one_mcp_tool_call_answers_through_the_real_engine(url: str) -> None:
    case = next(case for case in CASES if case["call"]["function"] == "jeff.safety.spam.message_type")
    tool = tool_name(case["call"]["function"])
    arguments = {"input": case["call"]["input"], "context": case["call"]["context"]}

    async def call_tool() -> Any:
        async with httpx2.AsyncClient(timeout=TIMEOUT_S) as http:
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http), mode="auto") as client:
                result = await client.call_tool(tool, arguments)
                assert not result.is_error, result
                return result.structured_content

    decision = Decision.from_json(anyio.run(call_tool))
    assert _problems([case], [decision], Catalogue.shipped()) == []
