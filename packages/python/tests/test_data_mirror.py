"""JEFF_DATA_MIRROR: private data read from a local copy, offline. A value `<owner>/<repo>@<revision>=<local folder>`
(several separated by ';') maps an hf://datasets URI of exactly that dataset and revision to `<local folder>/<file>`:
nothing is downloaded, and a file missing from the mirror is an error naming the local path and the URI, never a
download. Another revision of the same dataset, and model repos, are not mapped (they go to Hugging Face as before).
A malformed value raises when it is read. No test here touches the network: the download function is replaced."""

import json
from pathlib import Path
from typing import Any

import huggingface_hub
import pytest

from jeff.errors import JeffError
from jeff.evaluate import read_rows, resolve
from jeff.manifest import load_manifest

URI = "hf://datasets/teamsuzie/jeff-data@main/apps/soc/test.jsonl"


@pytest.fixture
def downloads(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    """Every call to huggingface_hub.hf_hub_download, which answers a fixed path instead of downloading."""
    calls: list[tuple[Any, ...]] = []

    def fake(repo: str, name: str, **options: Any) -> str:
        calls.append((repo, name, options.get("revision"), options.get("repo_type")))
        return "/downloaded/" + name
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", fake)
    return calls


def mirror(tmp_path: Path, *files: str) -> Path:
    folder = tmp_path / "jeff-data"
    for name in files:
        (folder / name).parent.mkdir(parents=True, exist_ok=True)
        (folder / name).write_text('{"id": "r1"}\n')
    return folder


def test_a_mapped_dataset_and_revision_resolve_to_the_local_copy(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                  downloads: list) -> None:
    folder = mirror(tmp_path, "apps/soc/test.jsonl")
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    assert resolve(URI) == folder / "apps/soc/test.jsonl"
    assert downloads == []


def test_several_mappings_separated_by_semicolons(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  downloads: list) -> None:
    main, other = mirror(tmp_path / "a", "apps/soc/test.jsonl"), mirror(tmp_path / "b", "x/calibration.jsonl")
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={main};owner/other@v2={other}")
    assert resolve(URI) == main / "apps/soc/test.jsonl"
    assert resolve("hf://datasets/owner/other@v2/x/calibration.jsonl") == other / "x/calibration.jsonl"
    assert downloads == []


def test_a_file_missing_from_the_mirror_is_an_error_not_a_download(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                    downloads: list) -> None:
    folder = mirror(tmp_path, "apps/other/test.jsonl")
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    with pytest.raises(JeffError) as error:
        resolve(URI)
    assert str(folder / "apps/soc/test.jsonl") in str(error.value) and URI in str(error.value)
    assert downloads == []


def test_another_revision_of_the_dataset_is_not_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       downloads: list) -> None:
    folder = mirror(tmp_path, "apps/soc/test.jsonl")
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    pinned = "hf://datasets/teamsuzie/jeff-data@5459129a/apps/soc/test.jsonl"
    assert resolve(pinned) == Path("/downloaded/apps/soc/test.jsonl")
    assert downloads == [("teamsuzie/jeff-data", "apps/soc/test.jsonl", "5459129a", "dataset")]


def test_a_model_repo_uri_is_not_mapped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: list) -> None:
    folder = mirror(tmp_path, "apps/soc/test.jsonl")
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    resolve("hf://teamsuzie/jeff-data@main/apps/soc/test.jsonl")
    assert downloads == [("teamsuzie/jeff-data", "apps/soc/test.jsonl", "main", None)]


def test_without_the_setting_uris_download_as_before(monkeypatch: pytest.MonkeyPatch, downloads: list) -> None:
    monkeypatch.delenv("JEFF_DATA_MIRROR", raising=False)
    assert resolve(URI) == Path("/downloaded/apps/soc/test.jsonl")
    assert downloads == [("teamsuzie/jeff-data", "apps/soc/test.jsonl", "main", "dataset")]


@pytest.mark.parametrize("value", ["", "teamsuzie/jeff-data=/data", "teamsuzie@main=/data", "teamsuzie/jeff-data@main",
                                   "teamsuzie/jeff-data@main=", "teamsuzie/jeff-data@main=/data;",
                                   "teamsuzie/jeff-data@main=/a;teamsuzie/jeff-data@main=/b"])
def test_a_malformed_setting_raises_naming_the_expected_form(value: str, monkeypatch: pytest.MonkeyPatch,
                                                              downloads: list) -> None:
    monkeypatch.setenv("JEFF_DATA_MIRROR", value)
    with pytest.raises(JeffError, match="JEFF_DATA_MIRROR.*<owner>/<repo>@<revision>=<local folder>"):
        resolve(URI)
    assert downloads == []


def test_a_mirror_folder_that_does_not_exist_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    downloads: list) -> None:
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={tmp_path / 'nowhere'}")
    with pytest.raises(JeffError, match="JEFF_DATA_MIRROR.*nowhere.*not a folder"):
        resolve(URI)


def test_jeff_test_reads_a_manifests_rows_from_the_mirror(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                           downloads: list) -> None:
    """The readers of test and calibration rows (jeff test, jeff calibrate, the gate, quantize) go through resolve:
    a v1.4 manifest's hf:// sets are read from the mirror, unchanged in the manifest."""
    root = Path(__file__).resolve().parents[3]
    manifest = load_manifest(root / "results/v14-manifests/jeff.security.soc/manifest.yaml")
    folder = mirror(tmp_path)
    for split in ("test", "calibration"):
        (folder / "apps/soc").mkdir(parents=True, exist_ok=True)
        (folder / f"apps/soc/{split}.jsonl").write_text(json.dumps({"id": f"{split}-1"}) + "\n")
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    assert [row["id"] for row in read_rows(manifest, "test")] == ["test-1"]
    assert [row["id"] for row in read_rows(manifest, "calibration")] == ["calibration-1"]
    assert downloads == []


# What is recorded: the hf:// path stays; when its rows were read from the mirror, the local file and its sha256 are
# recorded beside it (data_mirror). Without a mirror nothing is added.

TRIAGE_CALIBRATION = "hf://datasets/teamsuzie/jeff-data@main/apps/triage/calibration.jsonl"
TRIAGE_TEST = "hf://datasets/teamsuzie/jeff-data@main/apps/triage/test.jsonl"


def test_mirror_read_names_the_local_file_and_its_sha256_only_for_a_mirrored_uri(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: list) -> None:
    import hashlib

    from jeff.evaluate import mirror_read
    folder = mirror(tmp_path, "apps/soc/test.jsonl")
    local = folder / "apps/soc/test.jsonl"
    monkeypatch.delenv("JEFF_DATA_MIRROR", raising=False)
    assert mirror_read(URI) is None
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    assert mirror_read(URI) == {"path": str(local), "sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
    assert mirror_read("hf://datasets/teamsuzie/jeff-data@5459129a/apps/soc/test.jsonl") is None
    assert mirror_read(str(local)) is None
    assert downloads == []


def _calibrate_on(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, uri: str) -> Path:
    """jeff calibrate triage.needs_human with its tests.calibration set to `uri`; the manifest path."""
    import yaml

    import jeff.cli as cli
    from test_calibrate import calibration_setup

    root, _ = calibration_setup(tmp_path, monkeypatch)
    manifest_path = root / "jeff.support.triage" / "manifest.yaml"
    raw = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))
    raw["tests"]["calibration"] = uri
    manifest_path.write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    assert cli.main(["calibrate", "triage.needs_human", "--catalogue", str(root), "--target-precision", "0.95"]) == 0
    return manifest_path


def test_jeff_calibrate_records_the_mirror_beside_the_hf_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                              downloads: list) -> None:
    import hashlib

    import yaml
    from test_calibrate import calibration_setup
    folder = tmp_path / "jeff-data"
    (folder / "apps/triage").mkdir(parents=True)
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    # the mirror holds the rows calibration_setup writes
    _, data = calibration_setup(tmp_path / "rows", monkeypatch)
    (folder / "apps/triage/calibration.jsonl").write_bytes(data.read_bytes())
    manifest_path = _calibrate_on(tmp_path / "run", monkeypatch, TRIAGE_CALIBRATION)
    local = folder / "apps/triage/calibration.jsonl"
    block = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))["functions"]["needs_human"]["calibration"]
    assert block["data"] == TRIAGE_CALIBRATION
    assert block["data_mirror"] == {"path": str(local), "sha256": hashlib.sha256(local.read_bytes()).hexdigest()}
    calibration = load_manifest(manifest_path).functions["needs_human"].calibration
    assert calibration is not None and calibration.data_mirror == block["data_mirror"]
    assert downloads == []


def test_without_a_mirror_the_calibration_block_is_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import yaml
    monkeypatch.delenv("JEFF_DATA_MIRROR", raising=False)
    from test_calibrate import calibration_setup
    _, data = calibration_setup(tmp_path / "rows", monkeypatch)
    manifest_path = _calibrate_on(tmp_path / "run", monkeypatch, str(data))
    block = yaml.safe_load(manifest_path.read_text(encoding="utf-8"))["functions"]["needs_human"]["calibration"]
    assert set(block) == {"data", "rows", "target_precision", "threshold", "precision_at_threshold",
                          "coverage_at_threshold"}
    assert block["data"] == str(data)


def test_the_calibration_blocks_take_data_mirror_only_when_given() -> None:
    from jeff.calibrate import block, format_block
    pairs = [(0.9, True), (0.8, True), (0.4, False)]
    read = {"path": "/mnt/d/jeff-data/apps/x/calibration.jsonl", "sha256": "a" * 64}
    assert "data_mirror" not in block(pairs, 0.9, "hf://x", data_mirror=None)
    assert block(pairs, 0.9, "hf://x", data_mirror=read)["data_mirror"] == read
    assert "data_mirror" not in format_block(pairs, 0.9, "hf://x", "none", data_mirror=None)
    assert format_block(pairs, 0.9, "hf://x", "none", data_mirror=read)["data_mirror"] == read


def _quantiser_manifest(tmp_path: Path) -> Any:
    import yaml

    from jeff.manifest import manifest_from_dict
    source = Path(__file__).parent / "fixtures" / "catalogue" / "jeff.support.triage" / "manifest.yaml"
    raw = yaml.safe_load(source.read_text(encoding="utf-8"))
    raw["tests"].update({"published": TRIAGE_TEST, "calibration": TRIAGE_CALIBRATION})
    return manifest_from_dict(raw, source)


def _quantiser_rows(folder: Path) -> None:
    (folder / "apps/triage").mkdir(parents=True, exist_ok=True)
    for split, count in (("calibration", 310), ("test", 3)):
        rows = [{"id": f"{split}-{i}", "state": {"message": f"{split} {i}"},
                 "question": {"type": "noul", "instructions": "x"}, "label": True} for i in range(count)]
        (folder / f"apps/triage/{split}.jsonl").write_text("\n".join(json.dumps(row) for row in rows) + "\n")


def test_the_quantisation_record_keeps_the_hf_paths_and_names_the_mirror_files(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, downloads: list) -> None:
    import hashlib

    from jeff.cli import quantiser_calibration
    folder = tmp_path / "jeff-data"
    _quantiser_rows(folder)
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    manifest = _quantiser_manifest(tmp_path)
    rows, calibration = quantiser_calibration([manifest], 4)
    key = f"{manifest.id}@{manifest.version}"
    assert len(rows) == 4 and calibration["data"] == {key: TRIAGE_CALIBRATION}

    def read(split: str) -> dict[str, str]:
        local = folder / f"apps/triage/{split}.jsonl"
        return {"path": str(local), "sha256": hashlib.sha256(local.read_bytes()).hexdigest()}

    assert calibration["data_mirror"] == {key: read("calibration")}
    assert calibration["not_in_test_sets"][key] == {"data": TRIAGE_TEST, "sha256": read("test")["sha256"],
                                                    "data_mirror": read("test")}
    assert downloads == []


def test_without_a_mirror_the_quantisation_record_is_as_before(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                               downloads: list) -> None:
    import hashlib

    from jeff.cli import quantiser_calibration
    folder = tmp_path / "hub"
    _quantiser_rows(folder)
    monkeypatch.delenv("JEFF_DATA_MIRROR", raising=False)
    monkeypatch.setattr(huggingface_hub, "hf_hub_download",
                        lambda repo, name, **options: str(folder / name))
    manifest = _quantiser_manifest(tmp_path)
    _, calibration = quantiser_calibration([manifest], 4)
    key = f"{manifest.id}@{manifest.version}"
    assert set(calibration) == {"rows_per_adapter", "rule", "rows", "data", "not_in_test_sets", "arithmetic"}
    test_sha = hashlib.sha256((folder / "apps/triage/test.jsonl").read_bytes()).hexdigest()
    assert calibration["not_in_test_sets"][key] == {"data": TRIAGE_TEST, "sha256": test_sha}
