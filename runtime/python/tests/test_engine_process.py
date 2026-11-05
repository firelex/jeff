"""The real engine process: `jeff runtime serve` with MLX on the Mac, used through SocketEngine. Run alone.

Covers the 6 golden cases (contract/golden/step1.json) for the two adapters it serves: jeff.safety.spam.is_spam,
jeff.safety.spam.message_type, jeff.support.triage.route, jeff.support.triage.urgency,
jeff.support.triage.sentiment and jeff.support.triage.needs_human."""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from jeff import Call, Jeff
from jeff.catalogue import Catalogue
from jeff.contract import shipped_dir
from jeff.evaluate import golden_problems
from jeff.socket_engine import SocketEngine

pytestmark = pytest.mark.slow
GOLDEN = json.loads((shipped_dir() / "contract" / "golden" / "step1.json").read_text())


@pytest.fixture(scope="module")
def socket_path():
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


def test_golden_cases_through_the_engine_process(socket_path: Path) -> None:
    engine = SocketEngine(socket_path, timeout_s=60)
    try:
        jeff = Jeff(engine, _catalogue())
        cases = [c for c in GOLDEN["cases"]
                 if c["call"]["function"].startswith(("jeff.support.triage", "jeff.safety.spam"))]
        assert len(cases) == 6  # the six named in the module docstring
        decisions = jeff.decide([Call(**case["call"]) for case in cases])
        problems = [p for case, decision in zip(cases, decisions, strict=True)
                    for p in golden_problems(case, decision, _threshold(jeff.catalogue, case))]
        assert problems == []
    finally:
        engine.close()


def _threshold(catalogue: Catalogue, case: dict) -> float | None:
    calibration = catalogue.function(case["call"]["function"]).calibration
    return calibration.threshold if calibration else None


def _catalogue() -> Catalogue:
    return Catalogue.shipped()
