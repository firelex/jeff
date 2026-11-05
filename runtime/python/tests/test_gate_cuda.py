"""`jeff runtime gate` on datigator's GPU (plan 2b-2 Task 7, design Section 6 and Section 8 item 7), with the v1.3
base as the test bed (the real gate run targets the owner's new base and adapters; the command takes any):

1. test_the_v13_int4_base_passes_the_gate: the nine v1.3 adapters on the v1.3 GPTQ base pass (the design measured,
   in engine mode: mean -0.22 points, worst triage -0.39, and the INT4 fast path agreeing with the INT4 standard path
   on at least 99% of the rows). The gate runs as the command, `jeff runtime gate --base --reference --adapters
   --path`, into a folder of links to the INT4 base's files (so gate.json is written there, not into the shared
   folder); the record is copied to results/step2b2/gate.json. The serving check then accepts the nine adapters on it.
2. test_a_round_to_nearest_base_fails_naming_the_adapters: a round-to-nearest base of v1.3, built here with the
   runtime's quantize_rtn (no clip search) and write_int4_base, fails the gate (the design measured mean -0.64, four
   adapters past -0.5 in engine mode); the failing adapters are named in the record and by the serving check, which
   refuses every adapter. The record is copied to results/step2b2/gate-rtn.json.

Each gate measures both bases in full (every published test row of nine adapters, in engine and template mode, plus
calibration refits and the determinism check): about an hour each. The test sets and calibration sets are the
catalogue's published ones (downloaded through the Hugging Face cache if not there yet).

Run on datigator (GPU memory about 16 GB), alone on the GPU, from nohup:
  results/step2b2/task7/run_gate_cuda.sh"""

from __future__ import annotations

import glob
import json
import shutil
from pathlib import Path

import pytest

pytestmark = [pytest.mark.cuda, pytest.mark.slow]

REPO = Path(__file__).resolve().parents[3]
BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
INT4_BASE = Path("~/jeff-2b-scratch/2b2/int4-base/ll-v12-final-int4-gptq").expanduser()
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
ADAPTERS = {"emotion": "jeff.language.emotion", "ground": "jeff.retrieval.ground", "guard": "jeff.safety.guard",
            "legal-clauses": "jeff.legal.legal-clauses", "nav": "jeff.voice.nav", "spam": "jeff.safety.spam",
            "support-intents": "jeff.support.support-intents", "tools": "jeff.routing.tools",
            "triage": "jeff.support.triage"}
RESULTS = REPO / "results/step2b2"


def folders() -> dict[str, Path]:
    """The nine v1.3 adapters' weights folders, by catalogue ID; skips where there is no GPU or no datigator data."""
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    missing = [str(path) for path in (BASE, INT4_BASE) if not path.exists()]
    if missing:
        pytest.skip(f"needs datigator's v1.3 base and its INT4 folder; missing here: {missing}")
    found = {}
    for name, adapter_id in ADAPTERS.items():
        matches = glob.glob(CHECKPOINTS.format(name=name))
        assert len(matches) == 1, f"expected one v1.3 {name} checkpoint, found {matches}"
        found[adapter_id] = Path(matches[0])
    return found


def linked(folder: Path, into: Path) -> Path:
    """A folder of links to `folder`'s files: the same INT4 base, with room for its own gate.json."""
    into.mkdir()
    for item in folder.iterdir():
        (into / item.name).symlink_to(item)
    return into


def gate(base: Path, dirs: dict[str, Path]) -> tuple[int, dict]:
    from jeff.cli import main

    paths = [argument for adapter_id, folder in dirs.items() for argument in ("--path", f"{adapter_id}={folder}")]
    code = main(["runtime", "gate", "--base", str(base), "--reference", str(BASE), "--adapters", *dirs, *paths,
                 "--catalogue", str(REPO / "catalogue")])
    return code, json.loads((base / "gate.json").read_text())


def versions() -> dict[str, str]:
    from jeff.catalogue import Catalogue
    catalogue = Catalogue.from_dir(REPO / "catalogue")
    return {adapter_id: catalogue.adapter(adapter_id).version for adapter_id in ADAPTERS.values()}


def test_the_v13_int4_base_passes_the_gate(tmp_path: Path) -> None:
    dirs = folders()
    from jeff.gate import check_served, int4_identity

    base = linked(INT4_BASE, tmp_path / "int4")
    code, record = gate(base, dirs)
    RESULTS.mkdir(parents=True, exist_ok=True)
    shutil.copy(base / "gate.json", RESULTS / "gate.json")
    assert record["passed"], (record["overall_failures"],
                              {key: entry["failures"] for key, entry in record["adapters"].items()})
    assert code == 0
    assert sorted(record["adapters"]) == sorted(f"{a}@{v}" for a, v in versions().items())
    for key, entry in record["adapters"].items():
        agreement = entry["agreement"]
        assert agreement["compared"] == min(1000, agreement["fast_rows"]), key
        assert agreement["compared"] >= 0.9 * min(1000, entry["engine_mode"]["rows"]), key
        assert entry["engine_mode"]["rows"] > 0 and entry["template_mode"]["rows"] > 0, key
        assert all("int4" in fitted for fitted in entry["calibration"].values()), key
    assert record["determinism"]["identical_runs"] and record["determinism"]["failures"] == []
    assert record["machine"]["gpu"] and record["machine"]["driver"]
    identity = int4_identity(base)
    assert identity is not None
    check_served(identity, versions())  # what jeff runtime serve --base checks at start


def test_a_round_to_nearest_base_fails_naming_the_adapters(tmp_path: Path) -> None:
    import importlib.metadata

    from jeff.errors import EngineError
    from jeff.gate import check_served, int4_identity
    from jeff_runtime.engine.int4 import write_int4_base
    from jeff_runtime.engine.quantize import quantize_rtn

    dirs = folders()
    rtn = write_int4_base(BASE, tmp_path / "int4-rtn", quantize_rtn(BASE), method="rtn", source_id="jeff-base",
                          source_version="v1.3",
                          calibration={"rows_per_adapter": 0, "rule": "none: round to nearest uses no rows",
                                       "rows": {}, "data": {}, "arithmetic": "none"},
                          gptq=None, tool={"jeff_runtime": importlib.metadata.version("jeff-runtime")}).folder
    code, record = gate(rtn, dirs)
    RESULTS.mkdir(parents=True, exist_ok=True)
    shutil.copy(rtn / "gate.json", RESULTS / "gate-rtn.json")
    failing = sorted(key for key, entry in record["adapters"].items() if not entry["passed"])
    print(f"\nround to nearest: mean change {record['mean_change_points']}, failing {failing}, overall "
          f"{record['overall_failures']}")
    assert code == 1 and not record["passed"]
    assert failing, "the design measured four adapters past -0.5 points on a round-to-nearest base"
    for key in failing:
        assert any("accuracy change" in reason for reason in record["adapters"][key]["failures"]), key
    assert record["determinism"]["failures"] == [] and record["determinism"]["reproduces_folder"]
    identity = int4_identity(rtn)
    assert identity is not None
    with pytest.raises(EngineError) as refused:
        check_served(identity, versions())
    assert "failed its gate" in str(refused.value)
    for key in failing:
        assert key in str(refused.value)
