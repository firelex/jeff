import difflib
import json
import shutil
from pathlib import Path

import pytest

from jeff.calibrate import fit, write_block
from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import CalibrationError
from jeff.manifest import load_manifest
from jeff.testing import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
BLOCK = {"data": "x", "rows": 10, "target_precision": 0.9, "threshold": 0.7, "precision_at_threshold": 0.92,
         "coverage_at_threshold": 0.8}


def test_fit_finds_lowest_threshold_reaching_target() -> None:
    rows = [(0.99, True), (0.95, True), (0.9, True), (0.8, False), (0.7, True), (0.6, False), (0.5, False)]
    threshold, precision, coverage = fit(rows, 0.75)
    assert threshold == 0.7 and precision == pytest.approx(0.8) and coverage == pytest.approx(5 / 7)


def test_unreachable_target_is_an_error() -> None:
    with pytest.raises(CalibrationError, match="best precision"):
        fit([(0.9, False), (0.8, True)], 0.99)


def test_write_block_keeps_the_manifest_valid(tmp_path: Path) -> None:
    folder = tmp_path / "jeff.support.triage"
    shutil.copytree(FIXTURES / "jeff.support.triage", folder)
    block = {"data": "x", "rows": 10, "target_precision": 0.9, "threshold": 0.7, "precision_at_threshold": 0.92,
             "coverage_at_threshold": 0.8}
    write_block(folder / "manifest.yaml", "route", block)
    manifest = load_manifest(folder / "manifest.yaml")
    assert manifest.functions["route"].calibration.threshold == 0.7
    assert list(manifest.functions) == ["route", "urgency", "needs_human"]


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def _assert_only_one_line_replaced(original: list[str], updated: list[str], old_line: str) -> None:
    """The whole file must differ only where the calibration node was: exactly one contiguous replace op, and the
    replaced region in the original is exactly the single `old_line`."""
    changes = [op for op in difflib.SequenceMatcher(None, original, updated, autojunk=False).get_opcodes()
               if op[0] != "equal"]
    assert len(changes) == 1, changes
    tag, i1, i2, _j1, _j2 = changes[0]
    assert tag == "replace"
    assert i2 - i1 == 1
    assert original[i1].strip() == old_line


def test_write_block_when_calibration_is_not_the_last_key(tmp_path: Path) -> None:
    """A function whose `calibration: null` comes before another field (not last, unlike every real manifest
    today) must still only have that one line replaced; the field after it must survive untouched."""
    folder = tmp_path / "jeff.support.triage"
    shutil.copytree(FIXTURES / "jeff.support.triage", folder)
    manifest_path = folder / "manifest.yaml"
    text = manifest_path.read_text(encoding="utf-8")
    before = "    abstain_labels: []\n    calibration: null\nroutes_to: []"
    after = "    calibration: null\n    abstain_labels: []\nroutes_to: []"
    assert text.count(before) == 1
    manifest_path.write_text(text.replace(before, after), encoding="utf-8")
    original = _lines(manifest_path)

    write_block(manifest_path, "needs_human", BLOCK)

    updated = _lines(manifest_path)
    _assert_only_one_line_replaced(original, updated, "calibration: null")
    manifest = load_manifest(manifest_path)
    assert manifest.functions["needs_human"].calibration.threshold == 0.7
    assert "    abstain_labels: []\n" in updated  # the field that used to follow calibration is still there


def test_write_block_keeps_a_comment_line_after_calibration(tmp_path: Path) -> None:
    folder = tmp_path / "jeff.support.triage"
    shutil.copytree(FIXTURES / "jeff.support.triage", folder)
    manifest_path = folder / "manifest.yaml"
    text = manifest_path.read_text(encoding="utf-8")
    marker = "    calibration: null\n  urgency:"
    assert text.count(marker) == 1
    manifest_path.write_text(text.replace(marker, "    calibration: null\n    # pending calibration\n  urgency:"),
                             encoding="utf-8")
    original = _lines(manifest_path)

    write_block(manifest_path, "route", BLOCK)

    updated = _lines(manifest_path)
    assert "    # pending calibration\n" in updated
    _assert_only_one_line_replaced(original, updated, "calibration: null")
    manifest = load_manifest(manifest_path)
    assert manifest.functions["route"].calibration.threshold == 0.7


def test_write_block_refuses_when_calibration_is_already_a_block(tmp_path: Path) -> None:
    folder = tmp_path / "jeff.support.triage"
    shutil.copytree(FIXTURES / "jeff.support.triage", folder)
    manifest_path = folder / "manifest.yaml"
    before = manifest_path.read_text(encoding="utf-8")

    with pytest.raises(CalibrationError, match="not a plain"):
        write_block(manifest_path, "urgency", BLOCK)

    assert manifest_path.read_text(encoding="utf-8") == before


def test_cmd_calibrate_processes_functions_independently(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    """One function (`urgency`) can never reach the target (the fake engine always answers it wrong); the other
    (`needs_human`) always reaches it. `jeff calibrate` must still write needs_human's block, report urgency's
    failure, and exit non-zero."""
    import jeff.cli as cli

    root = tmp_path / "catalogue"
    shutil.copytree(FIXTURES, root)
    manifest_path = root / "jeff.support.triage" / "manifest.yaml"
    # the fixture's `urgency` already carries a block (used by other tests); start this adapter fully uncalibrated.
    text = manifest_path.read_text(encoding="utf-8")
    old = ("calibration: { data: fixture, rows: 100, target_precision: 0.9, threshold: 0.6, "
          "precision_at_threshold: 0.91, coverage_at_threshold: 0.8 }")
    assert old in text
    manifest_path.write_text(text.replace(old, "calibration: null"), encoding="utf-8")
    monkeypatch.setattr(cli, "_repo_catalogue_root", lambda: root)

    context = {"company": "A furniture shop.", "channel": "email"}
    levels = ["No action needed", "Can wait a few days", "Handle within a day", "Handle within hours",
             "Handle immediately"]
    raws = []
    for i in range(4):
        raws.append({"id": f"n{i}-needs_human", "state": {"message": "x", **context},
                     "question": {"type": "noul", "instructions": "x"}, "label": True})
        raws.append({"id": f"u{i}-urgency", "state": {"message": "x", **context},
                     "question": {"type": "score", "instructions": "x", "criteria": levels}, "label": 0})
    data = tmp_path / "calibration.jsonl"
    data.write_text("\n".join(json.dumps(raw) for raw in raws), encoding="utf-8")

    def answers(adapter_id: str, row: dict) -> list[float]:
        return [0.02, 0.98] if row["question"]["type"] == "noul" else [0.0, 0.0, 0.0, 0.0, 1.0]

    fake = FakeEngine(answers, {"jeff.support.triage"})
    monkeypatch.setattr(cli, "make_jeff",
                        lambda adapters, backend, base, catalogue, paths: Jeff(fake, Catalogue.from_dir(root)))

    exit_code = cli.main(["calibrate", "triage", "--data", str(data), "--target-precision", "0.95"])

    assert exit_code == 1
    captured = capsys.readouterr()
    assert "jeff.support.triage.urgency" in captured.err and "best precision" in captured.err

    manifest = load_manifest(manifest_path)
    assert manifest.functions["needs_human"].calibration is not None
    assert manifest.functions["needs_human"].calibration.precision_at_threshold == pytest.approx(1.0)
    assert manifest.functions["urgency"].calibration is None


def calibration_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """A tmp catalogue whose triage.urgency is calibrated (as in the fixture) and needs_human is not, a data file
    with needs_human rows, and a fake engine that always answers needs_human confidently right."""
    import jeff.cli as cli

    root = tmp_path / "catalogue"
    shutil.copytree(FIXTURES, root)
    context = {"company": "A furniture shop.", "channel": "email"}
    raws = [{"id": f"n{i}-needs_human", "state": {"message": "x", **context},
             "question": {"type": "noul", "instructions": "x"}, "label": True} for i in range(4)]
    data = tmp_path / "calibration.jsonl"
    data.write_text("\n".join(json.dumps(raw) for raw in raws), encoding="utf-8")
    fake = FakeEngine(lambda adapter_id, row: [0.02, 0.98], {"jeff.support.triage"})
    monkeypatch.setattr(cli, "make_jeff",
                        lambda adapters, backend, base, catalogue, paths: Jeff(fake, Catalogue.from_dir(root)))
    return root, data


def test_calibrate_one_function_from_a_given_catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli

    root, data = calibration_setup(tmp_path, monkeypatch)
    manifest_path = root / "jeff.support.triage" / "manifest.yaml"
    args = ["--catalogue", str(root), "--data", str(data), "--target-precision", "0.95"]
    assert cli.main(["calibrate", "triage", *args]) == 1  # urgency is already calibrated
    assert "already calibrated (urgency)" in capsys.readouterr().err
    assert cli.main(["calibrate", "triage.urgency", *args]) == 1
    assert cli.main(["calibrate", "triage.needs_human", *args]) == 0
    manifest = load_manifest(manifest_path)
    assert manifest.functions["needs_human"].calibration is not None
    assert manifest.functions["route"].calibration is None


def test_calibrate_without_a_checkout_asks_for_the_catalogue(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli

    _, data = calibration_setup(tmp_path, monkeypatch)
    monkeypatch.setattr(cli, "_repo_catalogue_root", lambda: tmp_path / "no-checkout" / "catalogue")
    assert cli.main(["calibrate", "triage.needs_human", "--data", str(data), "--target-precision", "0.95"]) == 1
    assert "--catalogue" in capsys.readouterr().err


def test_calibrate_refuses_a_decision_without_confidence(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    import dataclasses

    import jeff.cli as cli

    root, data = calibration_setup(tmp_path, monkeypatch)
    real_run = cli.run
    monkeypatch.setattr(cli, "run", lambda jeff, rows: [(row, dataclasses.replace(decision, confidence=None), ms)
                                                        for row, decision, ms in real_run(jeff, rows)])
    assert cli.main(["calibrate", "triage.needs_human", "--catalogue", str(root), "--data", str(data),
                     "--target-precision", "0.95"]) == 1
    assert "without a confidence" in capsys.readouterr().err


@pytest.mark.parametrize(("sets", "used"), [
    ({"calibration": None, "internal_calibration": "data"}, "data"),
    ({"calibration": "data", "internal_calibration": "missing"}, "data"),
])
def test_calibrate_takes_the_calibration_set_by_the_rule_the_gate_shares(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sets: dict[str, str | None], used: str) -> None:
    """Without --data, jeff calibrate reads tests.calibration, else tests.internal_calibration (jeff.evaluate.
    calibration_set, the rule jeff runtime gate's refit uses too: test_gate_run)."""
    import yaml

    import jeff.cli as cli
    from jeff.evaluate import calibration_set

    root, data = calibration_setup(tmp_path, monkeypatch)
    paths = {"data": str(data), "missing": str(tmp_path / "missing.jsonl")}
    manifest_path = root / "jeff.support.triage" / "manifest.yaml"
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw["tests"].update({key: None if value is None else paths[value] for key, value in sets.items()})
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert calibration_set(load_manifest(manifest_path)) == paths[used]
    assert cli.main(["calibrate", "triage.needs_human", "--catalogue", str(root), "--target-precision", "0.95"]) == 0
    calibration = load_manifest(manifest_path).functions["needs_human"].calibration
    assert calibration is not None and calibration.data == paths[used]
