"""The HTTP API against a real engine process (slow: loads the 1.7 GB base; run alone).

Starts one real `jeff runtime serve` process (MLX), exactly as runtime/python/tests/test_engine_process.py's
`socket_path` fixture does, serving the triage and spam adapters. The FastAPI app is then built with
`JEFF_ENGINE_SOCKETS` pointing at that process -- `create_app` with no `engine=` kwarg, so the app opens its own
`SocketEngine`/`EngineRouter` at startup, exactly as a deployed jeff-server would (see jeff_server.cli.app_factory).

Covers the 6 golden cases (contract/golden/step1.json) for those two adapters through POST /v1/decide, checking that
the HTTP answers match the *recorded* golden values (`jeff.evaluate.golden_problems`, the same check
runtime/python/tests/test_engine_process.py makes locally against the real engine) -- not that HTTP matches local
mode: that HTTP-vs-local-mode comparison is the separate, fast test in test_decide.py
(test_golden_calls_through_http_equal_local_mode), which runs every golden case over a FakeEngine and so says
nothing about whether the real model's answers are still right. This test is the other half: the real model's
answers, reached over HTTP through a real engine process, are checked against what was recorded. Also one POST
/v1/systemone request with the triage adapter's own worked example, as Task 7's
test_systemone.py::test_the_legacy_request_on_the_real_base_and_an_adapter checks for local mode."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jeff.catalogue import Catalogue
from jeff.contract import shipped_dir
from jeff.decision import Decision
from jeff.evaluate import golden_problems

from jeff_server.app import create_app
from jeff_server.settings import ServerSettings

pytestmark = pytest.mark.slow
GOLDEN = json.loads((shipped_dir() / "contract" / "golden" / "step1.json").read_text())


@pytest.fixture(scope="module")
def socket_path():
    """Ported from runtime/python/tests/test_engine_process.py's fixture of the same name: one `jeff runtime serve`
    process for the whole module, serving triage and spam, torn down by PID (never pkill) when every test in this
    module is done."""
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


def _settings(socket_path: Path) -> ServerSettings:
    # engine_timeout_s raised from the 30s default: the real model, not a FakeEngine, answers these requests.
    return ServerSettings(engine_sockets=(socket_path,), model_name="jeff-v1.3", engine_timeout_s=60.0)


def _threshold(catalogue: Catalogue, case: dict) -> float | None:
    calibration = catalogue.function(case["call"]["function"]).calibration
    return calibration.threshold if calibration else None


def test_golden_cases_through_http_against_the_real_engine(socket_path: Path) -> None:
    cases = [case for case in GOLDEN["cases"]
             if case["call"]["function"].startswith(("jeff.support.triage", "jeff.safety.spam"))]
    assert len(cases) == 6  # the six the engine process above serves
    catalogue = Catalogue.shipped()
    with TestClient(create_app(_settings(socket_path))) as http:
        response = http.post("/v1/decide", json={"calls": [case["call"] for case in cases]})
    assert response.status_code == 200, response.text
    decisions = [Decision.from_json(item) for item in response.json()["decisions"]]
    problems = [problem for case, decision in zip(cases, decisions, strict=True)
                for problem in golden_problems(case, decision, _threshold(catalogue, case))]
    assert problems == []


def test_systemone_answers_the_triage_adapters_own_example_through_http(socket_path: Path) -> None:
    from huggingface_hub import snapshot_download

    triage = Path(snapshot_download("mstrasser/jeff-adapter-triage", revision="v1.3"))
    example = json.loads((triage / "example.json").read_text())
    with TestClient(create_app(_settings(socket_path))) as http:
        response = http.post("/v1/systemone", json={
            "model": "triage", "state": example["state"], "questions": {"route": example["questions"]["route"]}})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["model"] == "triage"
    assert result["answers"]["route"]["choice"] == list(example["questions"]["route"]["criteria"])[1]
