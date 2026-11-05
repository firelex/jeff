"""`jeff runtime gate` up to the GPU work (plan 2b-2 Task 7), on the tiny synthetic base of test_int4.py: every
refusal that needs no GPU comes first, naming what is wrong (an existing gate.json, a bfloat16 --base, a --reference
that is not the INT4 base's source, adapters of another base, no adapter the quantiser saw, no GPU). Also the
helpers the measurement uses: a row's confidence as make_decision computes it, and the template-mode decisions that
list rows refused as too long. The measurement itself runs on the GPU: test_gate_cuda.py."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import torch
import yaml

from jeff.catalogue import Catalogue
from jeff.errors import EngineError, InputTooLong, JeffError, ManifestError
from jeff.gate_run import _decide_listing_too_long, confidence_of, run_gate
from jeff.manifest import load_manifest
from jeff_runtime.engine.int4 import write_int4_base
from test_int4 import GPTQ, matrices, source  # noqa: F401 (pytest fixtures)

REPO = Path(__file__).resolve().parents[3]
TRIAGE = "jeff.support.triage"
# The catalogue adapter the tests gate against this file's INT4 fixture base, made from jeff-base v1.4 (the shipped
# catalogue's only base).
GATED = TRIAGE


@pytest.fixture
def int4(tmp_path: Path, source: Path, matrices: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> Path:  # noqa: F811
    """An INT4 base of the tiny source, recorded as jeff-base@v1.4 quantised on triage's rows (GATED)."""
    calibration = {"rows_per_adapter": 1, "rule": "test", "rows": {f"{GATED}@1.4.0": ["triage:cal:1"]},
                   "data": {f"{GATED}@1.4.0": "unused"}, "arithmetic": "test"}
    written = write_int4_base(source, tmp_path / "int4", matrices, method="gptq", source_id="jeff-base",
                              source_version="v1.4", calibration=calibration, gptq=GPTQ, tool={"jeff_runtime": "test"})
    assert written.folder == tmp_path / "int4"  # the folder renamed into place
    return written.folder


def adapters(*names: str, base_version: str | None = None, root: Path | None = None) -> list[tuple[Any, Path]]:
    """The catalogue's manifests of `names` (their weights are never read here); with base_version, each copied into
    a folder under `root` with its base version changed."""
    found = []
    for name in names:
        path = REPO / "catalogue" / name / "manifest.yaml"
        if base_version is not None:
            assert root is not None
            data = yaml.safe_load(path.read_text())
            data["base"]["version"] = base_version
            path = root / name / "manifest.yaml"
            path.parent.mkdir(parents=True)
            path.write_text(yaml.safe_dump(data))
        found.append((load_manifest(path), Path("/unused")))
    return found


def gate(base: Path, reference: Path, named: list[tuple[Any, Path]]) -> dict[str, Any]:
    return run_gate(base, reference, named, Catalogue.from_dir(REPO / "catalogue"), log=lambda line: None)


def test_an_existing_gate_is_never_overwritten(int4: Path, source: Path) -> None:  # noqa: F811
    (int4 / "gate.json").write_text("{}")
    with pytest.raises(JeffError, match=f"{int4 / 'gate.json'} already exists: .* move it aside"):
        gate(int4, source, adapters(TRIAGE))


def test_the_base_must_be_int4_and_the_reference_its_bfloat16_source(int4: Path, source: Path,  # noqa: F811
                                                                      tmp_path: Path) -> None:
    with pytest.raises(JeffError, match="is a bfloat16 base"):
        gate(source, source, adapters(TRIAGE))
    with pytest.raises(JeffError, match="--reference .* is an INT4 base"):
        gate(int4, int4, adapters(TRIAGE))
    other = tmp_path / "other"
    other.mkdir()
    for item in source.iterdir():
        (other / item.name).write_bytes(item.read_bytes())
    (other / "model.safetensors").write_bytes((source / "model.safetensors").read_bytes() + b" ")
    with pytest.raises(JeffError, match="is not the base .* was quantised from"):
        gate(int4, other, adapters(TRIAGE))


def test_the_adapters_must_be_of_the_source_base_and_include_one_the_quantiser_saw(
        int4: Path, source: Path, tmp_path: Path) -> None:  # noqa: F811
    """jeff.safety.spam stands in for "some other adapter on the quantiser's base (v1.4) that it did not see"."""
    with pytest.raises(ManifestError, match=f"{TRIAGE} \\(jeff-base@v1.2\\) trained on another base than "
                                            "jeff-base@v1.4"):
        gate(int4, source, adapters(TRIAGE, base_version="v1.2", root=tmp_path / "adapters"))
    with pytest.raises(ManifestError, match=f"None of the adapters the quantiser saw \\({GATED}@1.4.0\\) is among "
                                            "--adapters"):
        gate(int4, source, adapters("jeff.safety.spam"))
    with pytest.raises(ManifestError, match="at least one adapter"):
        gate(int4, source, [])


def test_the_gate_needs_an_nvidia_gpu(int4: Path, source: Path,  # noqa: F811
                                      monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: False)
    with pytest.raises(JeffError, match="needs an NVIDIA GPU"):
        gate(int4, source, adapters(GATED))
    assert not (int4 / "gate.json").exists()


def test_confidence_is_make_decisions() -> None:
    assert confidence_of({"type": "noul"}, [0.75, 0.25]) == (False, pytest.approx(0.75))
    assert confidence_of({"type": "noul"}, [0.25, 0.75]) == (True, pytest.approx(0.75))
    assert confidence_of({"type": "choice", "criteria": {"a": "", "b": "", "c": ""}},
                         [0.2, 0.6, 0.2]) == ("b", pytest.approx(0.6))


@pytest.mark.parametrize("probabilities", [[0.3, 0.1], [0.5], [-0.1, 1.1], [float("nan"), 1.0]])
def test_confidence_of_refuses_a_reply_that_breaks_the_engines_contract(probabilities: list[float]) -> None:
    """Rescaling raw engine probabilities would hide exactly the broken INT4 output the gate exists to catch, so a
    reply that breaks the engine's contract (wrong count, a value outside 0 to 1, or a sum too far from 1) is
    refused with EngineError, never rescaled."""
    with pytest.raises(EngineError, match="the engine returned"):
        confidence_of({"type": "noul"}, probabilities)


def test_rows_refused_as_too_long_are_listed_not_decided() -> None:
    from jeff.client import Call
    from jeff.evaluate import TestRow

    class Stub:
        def decide(self, calls: list[Call]) -> list[str]:
            if any(call.input == "long" for call in calls):
                raise InputTooLong("too long")
            return [f"decided {call.input}" for call in calls]

    rows = [TestRow(f"r{i}", "f", Call("f", "long" if i in (3, 20) else str(i), None, None), True) for i in range(40)]
    decided, refused = _decide_listing_too_long(Stub(), rows)
    assert refused == ["r3", "r20"]
    assert [row.id for row, _ in decided] == [f"r{i}" for i in range(40) if i not in (3, 20)]
    assert all(decision == f"decided {row.call.input}" for row, decision in decided)


def test_the_record_from_synthetic_measurements(int4: Path, source: Path,  # noqa: F811
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """run_gate end to end with the GPU work replaced: each base's measurements (gate_run._measure) and the
    determinism check are synthetic, so the record's assembly is checked here: rows refused as too long on either
    base left out of both, the changes, the agreement with its tie rule, the refits, the verdict and gate.json."""
    import jeff.gate_run as gate_run
    from jeff.gate import check_served, int4_identity

    rows = {f"t{i}": {"id": f"t{i}", "state": {}, "question": {"type": "noul"}, "label": True} for i in range(10)}
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(gate_run, "machine", lambda: {"gpu": "Test GPU", "driver": "1", "cuda": "13", "torch": "x"})
    monkeypatch.setattr(gate_run, "read_rows", lambda manifest, which: list(rows.values()))
    monkeypatch.setattr(gate_run, "_determinism", lambda *args, **kwargs: {"identical_runs": True, "failures": []})

    def measured(folder: Path, *args: Any, int4: Any, log: Any) -> gate_run.Measured:
        result = gate_run.Measured({"format": "bf16"} if int4 is None else {"format": "int4-g64"})
        right = 9 if int4 is None else 8  # of the 9 rows scored: t9 is too long on the INT4 base
        result.engine[GATED] = {f"t{i}": ([0.1, 0.9] if i < right else [0.9, 0.1], "fast") for i in range(9)}
        result.too_long[GATED] = {"engine": [] if int4 is None else ["t9"], "template": []}
        if int4 is None:
            result.engine[GATED]["t9"] = ([0.1, 0.9], "fast")
        result.template[GATED] = {f"t{i}": (f"{GATED}.needs_human", True) for i in range(10)}
        result.skipped[GATED] = {"maps to no function": 2}
        if int4 is not None:
            # t0 an exact tie the fast path breaks the other way (agrees); t1 and t8 plain disagreements
            result.standard[GATED] = {"t0": [0.5, 0.5], "t1": [0.9, 0.1],
                                      **{f"t{i}": [0.1, 0.9] for i in range(2, 9)}}
            result.engine[GATED]["t0"] = ([0.4, 0.6], "fast")
            result.calibration[GATED] = {f"{GATED}.needs_human": {"target_precision": 0.95, "bfloat16_threshold": 0.7,
                                                              "too_long": [], "int4": {"threshold": 0.71}}}
        return result

    monkeypatch.setattr(gate_run, "_measure", measured)
    record = gate(int4, source, adapters(GATED))
    entry = record["adapters"][f"{GATED}@1.4.0"]
    engine = entry["engine_mode"]
    assert (engine["rows"], engine["too_long"], engine["reference_correct"], engine["int4_correct"]) == \
        (9, ["t9"], 9, 8)
    assert engine["change_points"] == pytest.approx(-100 / 9, abs=1e-4)
    assert entry["template_mode"]["change_points"] == 0
    assert entry["template_mode"]["skipped"] == {"maps to no function": 2}
    agreement = entry["agreement"]
    assert (agreement["compared"], agreement["agreeing"], agreement["raw_agreeing"], agreement["exact_ties"]) == \
        (9, 7, 6, 1)
    assert agreement["disagreements"] == [{"id": "t1", "fast_top": 1, "standard_top": 0, "standard_margin": 0.8},
                                          {"id": "t8", "fast_top": 0, "standard_top": 1, "standard_margin": 0.8}]
    assert not entry["passed"] and not record["passed"]
    assert entry["failures"][0].startswith("engine-mode accuracy change -11.111 points")
    assert "agreement 7/9 (77.78%), below 99%" in entry["failures"]
    assert record["base"]["format_id"] == int4_identity(int4).format_id  # type: ignore[union-attr]
    assert json.loads((int4 / "gate.json").read_text()) == record
    with pytest.raises(JeffError, match="failed its gate"):
        check_served(int4_identity(int4), {GATED: "1.4.0"})  # type: ignore[arg-type]


def test_a_gate_that_appears_while_the_gate_runs_is_never_overwritten(int4: Path, source: Path,  # noqa: F811
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    import jeff.gate_run as gate_run

    rows = {"t0": {"id": "t0", "state": {}, "question": {"type": "noul"}, "label": True}}
    monkeypatch.setattr(torch.cuda, "is_available", lambda: True)
    monkeypatch.setattr(gate_run, "machine", lambda: {"gpu": "Test GPU", "driver": "1", "cuda": "13", "torch": "x"})
    monkeypatch.setattr(gate_run, "read_rows", lambda manifest, which: list(rows.values()))

    def measured(folder: Path, *args: Any, int4: Any, log: Any) -> gate_run.Measured:
        result = gate_run.Measured({"format": "bf16"})
        result.engine[GATED] = {"t0": ([0.1, 0.9], "fast")}
        result.too_long[GATED] = {"engine": [], "template": []}
        result.template[GATED] = {"t0": (f"{GATED}.needs_human", True)}
        result.skipped[GATED] = {}
        result.standard[GATED] = {"t0": [0.1, 0.9]}
        result.calibration[GATED] = {}
        return result

    def other_run_finishes(*args: Any, **kwargs: Any) -> dict[str, Any]:
        (int4 / "gate.json").write_text("the other run's record")
        return {"identical_runs": True, "failures": []}

    monkeypatch.setattr(gate_run, "_measure", measured)
    monkeypatch.setattr(gate_run, "_determinism", other_run_finishes)
    with pytest.raises(JeffError, match="appeared while this gate ran"):
        gate(int4, source, adapters(GATED))
    assert (int4 / "gate.json").read_text() == "the other run's record"
    assert not list(int4.glob(".gate.json.*"))  # no temporary file left behind


@pytest.mark.parametrize(("sets", "used"), [
    ({"calibration": None, "internal_calibration": "data"}, "data"),
    ({"calibration": "data", "internal_calibration": "missing"}, "data"),
])
def test_the_refit_takes_the_calibration_set_by_jeff_calibrates_rule(
        tmp_path: Path, sets: dict[str, str | None], used: str) -> None:
    """Step 4 reads tests.calibration, else tests.internal_calibration (jeff.evaluate.calibration_set), as jeff
    calibrate does (test_calibrate), and leaves out the quantiser's rows of it."""
    from jeff.calibrate import MeasuringJeff, read_int4_base
    from jeff.evaluate import calibration_set
    from jeff.gate_run import Measured, _calibration
    from jeff.testing import FakeEngine

    root = tmp_path / "catalogue"
    shutil.copytree(REPO / "packages/python/tests/fixtures/catalogue", root)
    levels = ["No action needed", "Can wait a few days", "Handle within a day", "Handle within hours",
              "Handle immediately"]
    data = tmp_path / "calibration.jsonl"
    data.write_text("\n".join(json.dumps({
        "id": f"u{i}-urgency", "state": {"message": "x", "company": "A shop.", "channel": "email"},
        "question": {"type": "score", "instructions": "x", "criteria": levels}, "label": 4}) for i in range(5)))
    paths = {"data": str(data), "missing": str(tmp_path / "missing.jsonl")}
    manifest_path = root / TRIAGE / "manifest.yaml"
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw["tests"].update({key: None if value is None else paths[value] for key, value in sets.items()})
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    manifest = load_manifest(manifest_path)
    assert calibration_set(manifest) == paths[used]

    format_id = "jeff-base@v1.3+int4-g64-gptq.0123abcd"
    folder = tmp_path / "int4"
    folder.mkdir()
    (folder / "decision_config.json").write_text(json.dumps({"quantization": {
        "format": "int4-g64", "format_id": format_id, "method": "gptq",
        "source_base": {"weights_sha256": {"model.safetensors": "0" * 64}}, "record": "quantization.json"}}))
    (folder / "quantization.json").write_text(json.dumps({"calibration": {
        "rows_per_adapter": 1, "rows": {f"{TRIAGE}@1.3.0": ["u0-urgency"]}}}))
    engine = FakeEngine(lambda adapter_id, row: [0.0, 0.0, 0.0, 0.0, 1.0], {TRIAGE},
                        base_format={"format": "int4-g64", "format_id": format_id})
    measured = Measured(base_format=engine.base_format)
    _calibration(MeasuringJeff(engine, Catalogue.from_dir(root)), read_int4_base(folder), {TRIAGE: manifest}, measured)
    entry = measured.calibration[TRIAGE][f"{TRIAGE}.urgency"]["int4"]
    assert (entry["data"], entry["rows"]) == (paths[used], 4)  # u0, the quantiser's row, left out
