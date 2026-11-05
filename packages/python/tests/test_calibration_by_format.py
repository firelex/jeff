"""Calibration per base format (plan 2b-2 Task 6, design Section 5): a function's calibration_by_format entry for an
INT4 base's format_id, chosen once per binding from the engine's base format; refused at bind when an INT4 engine's
format has no entry; `jeff calibrate --base <INT4 folder>` fitting on the calibration set minus the quantiser's rows
and writing the entry, refusing to overwrite per format. The engine is a FakeEngine reporting a base_format."""

import difflib
import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from jeff import contract
from jeff.calibrate import read_int4_base, write_format_block
from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.errors import CalibrationError, ContractError, EngineError, JeffError, ManifestError
from jeff.listing import function_listing
from jeff.manifest import FormatCalibration, load_manifest
from jeff.testing import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
TRIAGE = "jeff.support.triage"
FORMAT_ID = "jeff-base@v1.3+int4-g64-gptq.0123abcd"
OTHER_FORMAT_ID = "jeff-base@v1.3+int4-g64-gptq.feedf00d"
INT4 = {"format": "int4-g64", "format_id": FORMAT_ID}
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
# triage.urgency in the fixture: calibration threshold 0.6 at target precision 0.9 (the bfloat16 base's block)
ENTRY = {"data": "fixture", "rows": 98, "excluded": "2 rows the quantiser saw (quantization.json)",
         "target_precision": 0.9, "threshold": 0.8, "precision_at_threshold": 0.92, "coverage_at_threshold": 0.7}
LEVELS = ["No action needed", "Can wait a few days", "Handle within a day", "Handle within hours",
          "Handle immediately"]


def catalogue_copy(tmp_path: Path, by_format: dict[str, Any] | None = None) -> Path:
    """The fixture catalogue in tmp_path, with triage.urgency given these calibration_by_format entries."""
    root = tmp_path / "catalogue"
    shutil.copytree(FIXTURES, root)
    if by_format is not None:
        path = root / TRIAGE / "manifest.yaml"
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        raw["functions"]["urgency"]["calibration_by_format"] = by_format
        path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    return root


def urgency_answer(adapter_id: str, row: dict[str, Any]) -> list[float]:
    """0.7 on "Handle within a day" for urgency: above the bfloat16 threshold 0.6, below the INT4 entry's 0.8."""
    if row["question"]["type"] == "score":
        return [0.0, 0.1, 0.7, 0.2, 0.0]
    return [0.3, 0.7]


def jeff_on(root: Path, base_format: dict[str, str] | None = None) -> Jeff:
    return Jeff(FakeEngine(urgency_answer, {TRIAGE}, base_format=base_format), Catalogue.from_dir(root))


def urgency(jeff: Jeff) -> Any:
    return jeff.decide([Call("triage.urgency", "My wardrobe is broken", CONTEXT)])[0]


# ---- the manifest


def test_the_manifest_reads_each_formats_entry(tmp_path: Path) -> None:
    root = catalogue_copy(tmp_path, {FORMAT_ID: ENTRY})
    function = Catalogue.from_dir(root).function("triage.urgency")
    assert function.calibration is not None and function.calibration.threshold == 0.6
    assert function.calibration_by_format == {FORMAT_ID: FormatCalibration(**ENTRY)}
    assert Catalogue.from_dir(root).function("triage.needs_human").calibration_by_format == {}


def test_entries_on_a_function_uncalibrated_on_bfloat16_are_refused(tmp_path: Path) -> None:
    root = catalogue_copy(tmp_path)
    path = root / TRIAGE / "manifest.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["functions"]["needs_human"]["calibration_by_format"] = {FORMAT_ID: ENTRY}
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    with pytest.raises(ManifestError, match="needs_human.*calibration null.*uncalibrated in every format"):
        load_manifest(path)


def test_an_entry_must_say_what_it_excluded(tmp_path: Path) -> None:
    entry = {key: value for key, value in ENTRY.items() if key != "excluded"}
    with pytest.raises(JeffError, match="excluded"):
        load_manifest(catalogue_copy(tmp_path, {FORMAT_ID: entry}) / TRIAGE / "manifest.yaml")


# ---- deciding


def test_an_int4_engine_decides_with_its_formats_threshold(tmp_path: Path) -> None:
    jeff = jeff_on(catalogue_copy(tmp_path, {FORMAT_ID: ENTRY}), INT4)
    assert jeff.format_id == FORMAT_ID
    decision = urgency(jeff)
    assert decision.confidence == pytest.approx(0.7)
    assert decision.calibrated is True and decision.abstained is True  # 0.7 < the INT4 threshold 0.8


def test_a_bfloat16_engine_is_unchanged_by_the_entries(tmp_path: Path) -> None:
    for root in (catalogue_copy(tmp_path / "with", {FORMAT_ID: ENTRY}), catalogue_copy(tmp_path / "without")):
        jeff = jeff_on(root)
        assert jeff.format_id is None
        decision = urgency(jeff)
        assert decision.calibrated is True and decision.abstained is False  # 0.7 >= the bfloat16 threshold 0.6
        assert jeff.calibration_of(jeff.catalogue.function("triage.urgency")).threshold == 0.6


def test_a_bound_function_keeps_its_formats_threshold(tmp_path: Path) -> None:
    jeff = jeff_on(catalogue_copy(tmp_path, {FORMAT_ID: ENTRY}), INT4)
    bound = jeff.function("triage.urgency", context=CONTEXT)
    assert bound("My wardrobe is broken").abstained is True


def test_a_missing_block_for_the_served_int4_format_is_refused_when_bound(tmp_path: Path) -> None:
    root = catalogue_copy(tmp_path, {OTHER_FORMAT_ID: ENTRY})
    jeff = jeff_on(root, INT4)
    expected = (r"jeff\.support\.triage\.urgency has no calibration for the INT4 base this engine serves "
                rf"\({re.escape(FORMAT_ID)}\).*{re.escape(OTHER_FORMAT_ID)}.*`jeff calibrate "
                r"jeff\.support\.triage\.urgency --base <the INT4 base folder>` \(for an adapter served from an "
                r"adapter folder, `jeff calibrate <that folder> --base <the INT4 base folder>`")
    with pytest.raises(ManifestError, match=expected):
        jeff.function("triage.urgency", context=CONTEXT)
    with pytest.raises(ManifestError, match=expected):
        urgency(jeff)
    assert jeff.engine.calls == []  # type: ignore[attr-defined]  # refused before any row reached the engine


def test_a_function_with_no_entries_is_refused_on_int4(tmp_path: Path) -> None:
    jeff = jeff_on(catalogue_copy(tmp_path), INT4)
    expected = rf"\({re.escape(FORMAT_ID)}\); it is calibrated on the bfloat16 base and on no INT4 base"
    with pytest.raises(ManifestError, match=expected):
        urgency(jeff)


def test_an_uncalibrated_function_stays_uncalibrated_on_int4(tmp_path: Path) -> None:
    jeff = jeff_on(catalogue_copy(tmp_path), INT4)
    decision = jeff.decide([Call("triage.needs_human", "My wardrobe is broken", CONTEXT)])[0]
    assert decision.calibrated is False and decision.abstained is False


def test_a_bfloat16_format_with_a_format_id_is_refused() -> None:
    with pytest.raises(JeffError, match="bfloat16 but carries a format_id"):
        FakeEngine(urgency_answer, {TRIAGE}, base_format={"format": "bf16", "format_id": FORMAT_ID})
    with pytest.raises(JeffError, match="names no format_id"):
        FakeEngine(urgency_answer, {TRIAGE}, base_format={"format": "int4-g64"})


def test_the_listing_names_the_base_format_and_each_functions_entries(tmp_path: Path) -> None:
    listing = function_listing(jeff_on(catalogue_copy(tmp_path, {FORMAT_ID: ENTRY}), INT4))
    contract.validate("functions_response", listing, "listing")
    assert listing["base_format"] == INT4
    by_id = {function["id"]: function for function in listing["functions"]}
    assert by_id["jeff.support.triage.urgency"]["calibration_by_format"] == {FORMAT_ID: ENTRY}
    assert "calibration_by_format" not in by_id["jeff.support.triage.needs_human"]


# ---- remote mode: the server's thresholds


def listing_stub(listings: list[dict[str, Any]]) -> Any:
    """A server whose GET /v1/functions answers each of `listings` in turn (the last one from then on)."""
    from starlette.applications import Starlette
    from starlette.responses import JSONResponse
    from starlette.routing import Route

    answered = [0]

    async def functions(request: Any) -> JSONResponse:
        answer = listings[min(answered[0], len(listings) - 1)]
        answered[0] += 1
        return JSONResponse(answer)

    return Starlette(routes=[Route("/v1/functions", functions)])


def server_listing(root: Path, by_format: dict[str, Any] | None) -> dict[str, Any]:
    """GET /v1/functions as an INT4 server whose triage.urgency has these calibration_by_format entries lists it."""
    return function_listing(jeff_on(catalogue_copy(root, by_format), INT4))


def test_remote_mode_takes_the_servers_threshold_for_the_servers_format(tmp_path: Path) -> None:
    from served import TIMEOUT_S, serving

    served = {**ENTRY, "threshold": 0.85}
    with serving(listing_stub([server_listing(tmp_path / "server", {FORMAT_ID: served})])) as url:
        # the client's own catalogue has no INT4 entry at all: the server's listing is what counts
        with Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(FIXTURES)) as client:
            assert client.base_format == INT4 and client.format_id == FORMAT_ID
            function = client.catalogue.function("triage.urgency")
            assert client.calibration_of(function) == FormatCalibration(**served)
            client.function("triage.urgency", context=CONTEXT)  # binds
            needs_human = client.catalogue.function("triage.needs_human")
            assert client.calibration_of(needs_human) is None


def test_remote_mode_refuses_a_function_the_server_has_no_threshold_for(tmp_path: Path) -> None:
    from served import TIMEOUT_S, serving

    client_root = catalogue_copy(tmp_path / "client", {FORMAT_ID: ENTRY})  # the client's catalogue has one
    with serving(listing_stub([server_listing(tmp_path / "server", {OTHER_FORMAT_ID: ENTRY})])) as url:
        with Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(client_root)) as client:
            with pytest.raises(ManifestError, match=rf"urgency has no calibration .*\({re.escape(FORMAT_ID)}\)"):
                client.function("triage.urgency", context=CONTEXT)


def test_remote_mode_refuses_a_server_whose_base_format_changed(tmp_path: Path) -> None:
    from served import TIMEOUT_S, serving

    first = server_listing(tmp_path / "server", {FORMAT_ID: ENTRY})
    second = {**first, "base_format": {"format": "bf16"}}
    with serving(listing_stub([first, second])) as url:
        with Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(FIXTURES)) as client:
            with pytest.raises(ContractError, match="now serves a base of format .*bf16.*Connect again"):
                client.engine.installed(refresh=True)


def test_a_jeff_resolves_each_functions_calibration_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The calibration is resolved at a function's first binding and kept, so a decide adds a dictionary lookup."""
    import jeff.client as client_module

    decider = jeff_on(catalogue_copy(tmp_path, {FORMAT_ID: ENTRY}), INT4)
    resolved: list[str] = []
    real = client_module.calibration_for

    def counting(function: Any, format_id: str | None) -> Any:
        resolved.append(function.id)
        return real(function, format_id)

    monkeypatch.setattr(client_module, "calibration_for", counting)
    for _ in range(3):
        assert urgency(decider).abstained is True
    assert resolved == ["jeff.support.triage.urgency"]


# ---- write_format_block


def _inserted_only(original: list[str], updated: list[str]) -> list[str]:
    """The lines `updated` adds to `original`; fails when any original line was changed or removed."""
    changes = [op for op in difflib.SequenceMatcher(None, original, updated, autojunk=False).get_opcodes()
               if op[0] != "equal"]
    assert all(tag == "insert" for tag, *_ in changes), changes
    return [line for _, _, _, j1, j2 in changes for line in updated[j1:j2]]


def test_the_writer_adds_the_map_and_then_more_formats_inserting_lines_only(tmp_path: Path) -> None:
    path = catalogue_copy(tmp_path) / TRIAGE / "manifest.yaml"
    original = path.read_text(encoding="utf-8").splitlines(keepends=True)
    write_format_block(path, "urgency", FORMAT_ID, ENTRY)
    first = path.read_text(encoding="utf-8").splitlines(keepends=True)
    added = _inserted_only(original, first)
    assert added[0] == "    calibration_by_format:\n" and added[1] == f"      {FORMAT_ID}:\n"
    assert load_manifest(path).functions["urgency"].calibration_by_format == {FORMAT_ID: FormatCalibration(**ENTRY)}

    other = {**ENTRY, "threshold": 0.55}
    write_format_block(path, "urgency", OTHER_FORMAT_ID, other)
    added = _inserted_only(first, path.read_text(encoding="utf-8").splitlines(keepends=True))
    assert added[0] == f"      {OTHER_FORMAT_ID}:\n"
    function = load_manifest(path).functions["urgency"]
    assert function.calibration_by_format == {FORMAT_ID: FormatCalibration(**ENTRY),
                                              OTHER_FORMAT_ID: FormatCalibration(**other)}
    assert function.calibration is not None and function.calibration.threshold == 0.6  # bfloat16 block untouched


def test_the_writer_refuses_to_overwrite_a_formats_entry(tmp_path: Path) -> None:
    path = catalogue_copy(tmp_path, {FORMAT_ID: ENTRY}) / TRIAGE / "manifest.yaml"
    before = path.read_text(encoding="utf-8")
    with pytest.raises(CalibrationError, match=f"already calibrated on {re.escape(FORMAT_ID)}"):
        write_format_block(path, "urgency", FORMAT_ID, {**ENTRY, "threshold": 0.5})
    assert path.read_text(encoding="utf-8") == before


def test_the_writer_refuses_a_function_uncalibrated_on_bfloat16(tmp_path: Path) -> None:
    path = catalogue_copy(tmp_path) / TRIAGE / "manifest.yaml"
    before = path.read_text(encoding="utf-8")
    with pytest.raises(CalibrationError, match="calibration null"):
        write_format_block(path, "needs_human", FORMAT_ID, ENTRY)
    assert path.read_text(encoding="utf-8") == before


def test_the_writer_refuses_a_map_it_cannot_place_an_entry_in(tmp_path: Path) -> None:
    path = catalogue_copy(tmp_path) / TRIAGE / "manifest.yaml"
    text = path.read_text(encoding="utf-8")
    flow = "{ " + ", ".join(f"{key}: {json.dumps(value)}" for key, value in ENTRY.items()) + " }"
    marker = "  urgency:\n"
    assert text.count(marker) == 1
    path.write_text(text.replace(marker, f"{marker}    calibration_by_format: {{ {OTHER_FORMAT_ID}: {flow} }}\n"),
                    encoding="utf-8")
    load_manifest(path)  # a valid manifest, written in flow style
    before = path.read_text(encoding="utf-8")
    with pytest.raises(CalibrationError, match="not a block mapping"):
        write_format_block(path, "urgency", FORMAT_ID, ENTRY)
    assert path.read_text(encoding="utf-8") == before


# ---- jeff calibrate --base


def int4_folder(tmp_path: Path, quantiser_rows: list[str], format_id: str = FORMAT_ID,
                recorded_as: str = f"{TRIAGE}@1.3.0") -> Path:
    """A stand-in INT4 base folder: what jeff calibrate reads of one (no weights; the engine is a FakeEngine). Its
    record lists `quantiser_rows` under `recorded_as` (an adapter id@version)."""
    folder = tmp_path / "int4-base"
    folder.mkdir(parents=True)
    (folder / "decision_config.json").write_text(json.dumps({"quantization": {
        "format": "int4-g64", "format_id": format_id, "method": "gptq",
        "source_base": {"weights_sha256": {"model.safetensors": "0" * 64}}, "record": "quantization.json"}}))
    (folder / "quantization.json").write_text(json.dumps({"calibration": {
        "rows_per_adapter": len(quantiser_rows), "rows": {recorded_as: quantiser_rows}}}))
    return folder


def calibrate_setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                    engine_format: dict[str, str] | None = None) -> tuple[Path, Path, list[Path]]:
    """A catalogue copy, urgency rows u0..u5 whose first two (the quantiser's, in the INT4 folders below) the fake
    engine answers wrongly and the rest rightly, and make_jeff replaced by a FakeEngine reporting the base format of
    the folder it is asked to load (or `engine_format`). Returns the catalogue, the data file and the list of bases
    make_jeff was asked to load."""
    import jeff.cli as cli

    root = catalogue_copy(tmp_path)
    raws = [{"id": f"u{i}-urgency", "state": {"message": "x", **CONTEXT},
             "question": {"type": "score", "instructions": "x", "criteria": LEVELS}, "label": 0 if i < 2 else 4}
            for i in range(6)]
    data = tmp_path / "calibration.jsonl"
    data.write_text("\n".join(json.dumps(raw) for raw in raws), encoding="utf-8")
    loaded: list[Path] = []

    def make_jeff(adapters: list[str], backend: str, base: Path, catalogue: Catalogue | None,
                  paths: dict[str, Path] | None) -> Jeff:
        loaded.append(base)
        quantization = json.loads((base / "decision_config.json").read_text())["quantization"]
        served = engine_format if engine_format is not None else {"format": quantization["format"],
                                                                  "format_id": quantization["format_id"]}
        engine = FakeEngine(lambda adapter_id, row: [0.0, 0.0, 0.0, 0.0, 1.0], {TRIAGE}, base_format=served)
        return Jeff(engine, Catalogue.from_dir(root))

    monkeypatch.setattr(cli, "make_jeff", make_jeff)
    return root, data, loaded


def calibrate(root: Path, data: Path, base: Path, *more: str) -> int:
    import jeff.cli as cli

    return cli.main(["calibrate", "triage.urgency", "--catalogue", str(root), "--data", str(data),
                     "--target-precision", "0.9", "--base", str(base), *more])


def test_calibrate_base_fits_without_the_quantiser_rows_and_writes_the_formats_entry(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    folder = int4_folder(tmp_path, ["u0-urgency", "u1-urgency", "elsewhere-urgency"])
    path = root / TRIAGE / "manifest.yaml"
    assert calibrate(root, data, folder) == 0
    assert loaded == [folder]
    function = load_manifest(path).functions["urgency"]
    # u0 and u1 (answered wrongly) were left out: the four rows left are all right
    assert function.calibration_by_format == {FORMAT_ID: FormatCalibration(
        data=str(data), rows=4, excluded="2 rows the quantiser saw (quantization.json)", target_precision=0.9,
        threshold=1.0, precision_at_threshold=1.0, coverage_at_threshold=1.0)}
    assert function.calibration is not None and function.calibration.threshold == 0.6  # bfloat16 block untouched
    assert f"jeff.support.triage.urgency on {FORMAT_ID}: threshold 1.000" in capsys.readouterr().out


def test_calibrate_base_keeps_the_manifests_permission_bits(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, data, _ = calibrate_setup(tmp_path, monkeypatch)
    path = root / TRIAGE / "manifest.yaml"
    path.chmod(0o644)
    assert calibrate(root, data, int4_folder(tmp_path, ["u0-urgency", "u1-urgency"])) == 0
    assert load_manifest(path).functions["urgency"].calibration_by_format
    assert path.stat().st_mode & 0o7777 == 0o644


def test_calibrate_base_refuses_to_overwrite_per_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    root, data, _ = calibrate_setup(tmp_path, monkeypatch)
    assert calibrate(root, data, int4_folder(tmp_path / "first", ["u0-urgency", "u1-urgency"])) == 0
    path = root / TRIAGE / "manifest.yaml"
    before = path.read_text(encoding="utf-8")
    capsys.readouterr()
    assert calibrate(root, data, int4_folder(tmp_path / "again", ["u0-urgency", "u1-urgency"])) == 1
    assert f"already calibrated on {FORMAT_ID} (urgency)" in capsys.readouterr().err
    assert path.read_text(encoding="utf-8") == before
    # another INT4 base (another format_id) is calibrated beside it
    other = int4_folder(tmp_path / "other", ["u0-urgency", "u1-urgency"], format_id=OTHER_FORMAT_ID)
    assert calibrate(root, data, other) == 0
    function = load_manifest(path).functions["urgency"]
    assert set(function.calibration_by_format) == {FORMAT_ID, OTHER_FORMAT_ID}


def test_calibrate_base_refuses_a_bfloat16_folder(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    folder = tmp_path / "bf16-base"
    folder.mkdir()
    (folder / "decision_config.json").write_text(json.dumps({"temperature": 1.0}))
    assert calibrate(root, data, folder) == 1
    assert "is a bfloat16 base" in capsys.readouterr().err and loaded == []


def test_calibrate_base_refuses_a_function_uncalibrated_on_bfloat16(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli

    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    folder = int4_folder(tmp_path, [])
    assert cli.main(["calibrate", "triage.needs_human", "--catalogue", str(root), "--data", str(data),
                     "--target-precision", "0.9", "--base", str(folder)]) == 1
    assert "calibrate it without --base first" in capsys.readouterr().err and loaded == []


def test_calibrate_base_keeps_the_functions_target_precision(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                             capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli

    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    assert cli.main(["calibrate", "triage.urgency", "--catalogue", str(root), "--data", str(data),
                     "--target-precision", "0.95", "--base", str(int4_folder(tmp_path, []))]) == 1
    assert "differs from the bfloat16 calibration's target of urgency (0.9)" in capsys.readouterr().err
    assert loaded == []


def test_calibrate_base_refuses_an_engine_serving_another_format(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    root, data, _ = calibrate_setup(tmp_path, monkeypatch, engine_format={"format": "int4-g64",
                                                                          "format_id": OTHER_FORMAT_ID})
    assert calibrate(root, data, int4_folder(tmp_path, [])) == 1
    assert f"not {FORMAT_ID} as its decision_config.json names" in capsys.readouterr().err


def test_calibrate_base_refuses_a_record_its_calibration_set_does_not_hold(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli

    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    path = root / TRIAGE / "manifest.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["tests"]["calibration"] = str(data)  # the manifest's own calibration set, not --data
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    folder = int4_folder(tmp_path, ["u0-urgency", "u9-urgency"])
    assert cli.main(["calibrate", "triage.urgency", "--catalogue", str(root), "--target-precision", "0.9",
                     "--base", str(folder)]) == 1
    assert "lists 2 rows of jeff.support.triage@1.3.0 the quantiser saw, and 1 of them are in" in \
        capsys.readouterr().err
    assert loaded == []


def test_calibrate_base_refuses_a_record_of_another_version_of_the_adapter(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    folder = int4_folder(tmp_path, ["u0-urgency", "u1-urgency"], recorded_as=f"{TRIAGE}@1.2.0")
    assert calibrate(root, data, folder) == 1
    error = capsys.readouterr().err
    assert f"records the quantiser's rows of {TRIAGE}@1.2.0" in error and "is version 1.3.0" in error
    assert loaded == []


def only_right_rows(tmp_path: Path, data: Path) -> Path:
    """The data file without u0 and u1 (the rows the fake engine answers wrongly)."""
    kept = tmp_path / "right.jsonl"
    kept.write_text("\n".join(line for line in data.read_text().splitlines()
                              if json.loads(line)["id"] not in ("u0-urgency", "u1-urgency")))
    return kept


@pytest.mark.parametrize(("recorded_as", "excluded"), [
    (f"{TRIAGE}@1.3.0", "none in this calibration data (none of this function's rows is among the quantiser's rows "
                        "listed in quantization.json)"),
    ("jeff.safety.spam@1.3.0", "none: quantization.json records no rows of this adapter")])
def test_calibrate_base_says_why_nothing_was_excluded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                     recorded_as: str, excluded: str) -> None:
    root, data, _ = calibrate_setup(tmp_path, monkeypatch)
    folder = int4_folder(tmp_path, ["u0-urgency", "u1-urgency"], recorded_as=recorded_as)
    assert calibrate(root, only_right_rows(tmp_path, data), folder) == 0
    entry = load_manifest(root / TRIAGE / "manifest.yaml").functions["urgency"].calibration_by_format[FORMAT_ID]
    assert (entry.rows, entry.excluded) == (4, excluded)


@pytest.mark.parametrize(("change", "message"), [
    ({"decision_config.json": "not json"}, "decision_config.json is not valid JSON"),
    ({"decision_config.json": json.dumps({"quantization": {"format": "int4-g64"}})},
     "quantization has no format_id, record"),
    ({"quantization.json": None}, "quantization.json, is missing"),
    ({"quantization.json": json.dumps({"calibration": {"rows": ["u0-urgency"]}})}, "has no calibration.rows mapping"),
])
def test_calibrate_base_refuses_a_malformed_folder_naming_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str],
                                                            change: dict[str, str | None], message: str) -> None:
    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    folder = int4_folder(tmp_path, ["u0-urgency"])
    for name, text in change.items():
        if text is None:
            (folder / name).unlink()
        else:
            (folder / name).write_text(text)
    with pytest.raises(CalibrationError, match=re.escape(message)):
        read_int4_base(folder)
    assert calibrate(root, data, folder) == 1
    error = capsys.readouterr().err
    assert str(folder) in error and message in error, error
    assert loaded == []


# ---- jeff calibrate on an adapter folder (as JEFF_ADAPTER_DIRS serves, whose own manifest replaces the catalogue's)


def adapter_folder_copy(tmp_path: Path) -> Path:
    """The fixture's triage adapter as an adapter folder: its manifest.yaml and an adapter_config.json."""
    folder = tmp_path / "adapters" / "triage"
    shutil.copytree(FIXTURES / TRIAGE, folder)
    (folder / "adapter_config.json").write_text("{}")
    return folder


def recording_engine_options(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Wraps the make_jeff calibrate_setup installed to record the catalogue and paths jeff calibrate passes it."""
    import jeff.cli as cli

    fake = cli.make_jeff
    options: list[dict[str, Any]] = []

    def make_jeff(adapters: list[str], backend: str, base: Path, catalogue: Catalogue | None,
                  paths: dict[str, Path] | None) -> Jeff:
        options.append({"catalogue": catalogue, "paths": paths})
        return fake(adapters, backend, base, catalogue, paths)

    monkeypatch.setattr(cli, "make_jeff", make_jeff)
    return options


def test_calibrate_base_on_an_adapter_folder_writes_into_the_folders_manifest(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli

    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    options = recording_engine_options(monkeypatch)
    folder = adapter_folder_copy(tmp_path)
    int4 = int4_folder(tmp_path, ["u0-urgency", "u1-urgency"])
    catalogue_before = (root / TRIAGE / "manifest.yaml").read_text(encoding="utf-8")
    assert cli.main(["calibrate", str(folder), "--catalogue", str(root), "--data", str(data),
                     "--target-precision", "0.9", "--base", str(int4)]) == 0
    assert loaded == [int4]
    # the engine loads the folder's weights, and reads the folder's manifest in place of the catalogue's
    assert options[0]["paths"] == {TRIAGE: folder}
    assert options[0]["catalogue"].adapter(TRIAGE).path == folder / "manifest.yaml"
    function = load_manifest(folder / "manifest.yaml").functions["urgency"]
    assert function.calibration_by_format == {FORMAT_ID: FormatCalibration(
        data=str(data), rows=4, excluded="2 rows the quantiser saw (quantization.json)", target_precision=0.9,
        threshold=1.0, precision_at_threshold=1.0, coverage_at_threshold=1.0)}
    assert (root / TRIAGE / "manifest.yaml").read_text(encoding="utf-8") == catalogue_before  # never written
    out = capsys.readouterr()
    assert f"jeff.support.triage.urgency on {FORMAT_ID}: threshold 1.000" in out.out
    assert "route, needs_human uncalibrated on the bfloat16 base" in out.err


def test_calibrate_base_on_an_adapter_folder_refuses_to_overwrite(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    """The catalogue read for the base defaults to the shipped one; the refusal per format is the catalogue's."""
    import jeff.cli as cli

    _, data, _ = calibrate_setup(tmp_path, monkeypatch)
    folder = adapter_folder_copy(tmp_path)
    args = ["calibrate", str(folder), "--data", str(data), "--target-precision", "0.9", "--base"]
    assert cli.main([*args, str(int4_folder(tmp_path / "first", ["u0-urgency", "u1-urgency"]))]) == 0
    before = (folder / "manifest.yaml").read_text(encoding="utf-8")
    capsys.readouterr()
    assert cli.main([*args, str(int4_folder(tmp_path / "again", ["u0-urgency", "u1-urgency"]))]) == 1
    assert f"already calibrated on {FORMAT_ID} (urgency)" in capsys.readouterr().err
    assert (folder / "manifest.yaml").read_text(encoding="utf-8") == before


def test_calibrate_refuses_a_folder_that_is_not_an_adapter_folder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli

    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    folder = adapter_folder_copy(tmp_path)
    (folder / "adapter_config.json").unlink()
    assert cli.main(["calibrate", str(folder), "--catalogue", str(root), "--data", str(data),
                     "--target-precision", "0.9", "--base", str(int4_folder(tmp_path, []))]) == 1
    error = capsys.readouterr().err
    assert str(folder) in error and "holds manifest.yaml and adapter_config.json" in error
    assert loaded == []


def sha256_of(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def training_record(tmp_path: Path, folder: Path, calibration_file: Path, *, ids: tuple[str, ...] = ("t1", "t2"),
                    states: tuple[Any, ...] | None = None) -> Path:
    """Marks the stand-in INT4 base's quantiser rows of triage as drawn from a training file (written here, rows
    `ids` with their own states unless `states` gives them), checked against `calibration_file`. Returns the file."""
    from jeff_runtime.engine.quantize import TRAINING_RULE

    training = tmp_path / "train.jsonl"
    states = states or tuple({"message": f"training document {row_id}"} for row_id in ids)
    training.write_text("".join(json.dumps({"id": row_id, "state": state, "question": {"type": "noul"}}) + "\n"
                                for row_id, state in zip(ids, states, strict=True)))
    record = json.loads((folder / "quantization.json").read_text())
    key = f"{TRIAGE}@1.3.0"
    record["calibration"].update({
        "rule": TRAINING_RULE, "rows": {key: list(ids)},
        "training_rows": {key: {"path": str(training), "sha256": sha256_of(training)}},
        "not_in_calibration_sets": {key: {"data": str(calibration_file), "sha256": sha256_of(calibration_file)}}})
    (folder / "quantization.json").write_text(json.dumps(record))
    return training


def published_calibration(root: Path, data: Path) -> None:
    """Makes `data` the manifest's own calibration set (so jeff calibrate reads it as the published set)."""
    path = root / TRIAGE / "manifest.yaml"
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    raw["tests"]["calibration"] = str(data)
    path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")


def test_calibrate_base_fits_on_every_row_when_the_quantiser_saw_training_rows(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import jeff.cli as cli

    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    kept = only_right_rows(tmp_path, data)
    published_calibration(root, kept)
    folder = int4_folder(tmp_path, [])
    training_record(tmp_path, folder, kept)
    assert cli.main(["calibrate", "triage.urgency", "--catalogue", str(root), "--target-precision", "0.9",
                     "--base", str(folder)]) == 0
    entry = load_manifest(root / TRIAGE / "manifest.yaml").functions["urgency"].calibration_by_format[FORMAT_ID]
    assert entry.rows == 4 and entry.excluded.startswith("none in this calibration data")
    assert loaded == [folder]


def test_calibrate_base_refuses_a_published_calibration_set_changed_since_quantisation(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli

    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    kept = only_right_rows(tmp_path, data)
    published_calibration(root, kept)
    folder = int4_folder(tmp_path, [])
    training_record(tmp_path, folder, kept)
    kept.write_text(kept.read_text() + "\n")  # the same rows, another file: the record no longer vouches for it
    assert cli.main(["calibrate", "triage.urgency", "--catalogue", str(root), "--target-precision", "0.9",
                     "--base", str(folder)]) == 1
    assert "is not the file the quantiser checked its training rows against" in capsys.readouterr().err
    assert loaded == []


@pytest.mark.parametrize(("ids", "states", "message"), [
    (("t1", "u3-urgency"), None, "1 with the id of a row the quantiser saw \\(u3-urgency\\)"),
    # a training row's document under a new id: the same input to GPTQ
    (("t1", "t2"), ({"message": "unrelated"}, {"channel": "email", "message": "x", "company": "A furniture shop."}),
     "4 with the state of a row the quantiser saw"),
])
def test_calibrate_base_on_other_data_refuses_rows_the_quantiser_saw(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], ids: tuple[str, ...],
        states: tuple[Any, ...] | None, message: str) -> None:
    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    folder = int4_folder(tmp_path, [])
    training_record(tmp_path, folder, data, ids=ids, states=states)
    assert calibrate(root, only_right_rows(tmp_path, data), folder) == 1  # --data: checked against the training file
    assert re.search(message, capsys.readouterr().err)
    assert loaded == []


def test_calibrate_base_on_other_data_fits_when_it_has_none_of_the_rows_the_quantiser_saw(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, data, _ = calibrate_setup(tmp_path, monkeypatch)
    folder = int4_folder(tmp_path, [])
    training_record(tmp_path, folder, data)
    assert calibrate(root, only_right_rows(tmp_path, data), folder) == 0
    entry = load_manifest(root / TRIAGE / "manifest.yaml").functions["urgency"].calibration_by_format[FORMAT_ID]
    assert entry.rows == 4


@pytest.mark.parametrize(("change", "message"), [
    ("edit", "is not the one the quantiser drew from"),
    ("delete", "is not on this machine"),
])
def test_calibrate_base_on_other_data_refuses_without_the_recorded_training_file(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], change: str,
        message: str) -> None:
    root, data, loaded = calibrate_setup(tmp_path, monkeypatch)
    folder = int4_folder(tmp_path, [])
    training = training_record(tmp_path, folder, data)
    if change == "edit":
        training.write_text(training.read_text() + "\n")
    else:
        training.unlink()
    assert calibrate(root, only_right_rows(tmp_path, data), folder) == 1
    assert message in capsys.readouterr().err
    assert loaded == []


@pytest.mark.parametrize(("change", "message"), [
    ({"rule": "something else"}, "has calibration.training_rows, but its calibration.rule is not the training-rows "
                                 "rule"),
    ({"training_rows": None}, "has the training-rows rule as calibration.rule, but no calibration.training_rows"),
    ({"rows": {}}, "calibration.training_rows names jeff.support.triage@1.3.0, which calibration.rows does not list"),
    ({"not_in_calibration_sets": None}, "has calibration.training_rows but no calibration.not_in_calibration_sets"),
    ({"not_in_calibration_sets": {}}, "not_in_calibration_sets has no SHA-256 for jeff.support.triage@1.3.0"),
    ({"training_rows": {f"{TRIAGE}@1.3.0": {"path": "/x"}}}, "training_rows\\[jeff.support.triage@1.3.0\\] is not a "
                                                            "path and a SHA-256"),
])
def test_read_int4_base_refuses_a_contradictory_training_record(tmp_path: Path, change: dict[str, Any],
                                                                message: str) -> None:
    folder = int4_folder(tmp_path, [])
    data = tmp_path / "calibration.jsonl"
    data.write_text("{}\n")
    training_record(tmp_path, folder, data)
    record = json.loads((folder / "quantization.json").read_text())
    for key, value in change.items():
        if value is None:
            del record["calibration"][key]
        else:
            record["calibration"][key] = value
    (folder / "quantization.json").write_text(json.dumps(record))
    with pytest.raises(CalibrationError, match=message):
        read_int4_base(folder)
