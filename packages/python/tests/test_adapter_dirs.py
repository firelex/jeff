"""jeff.adapter_dirs: finding adapter folders under the engine's --adapters-dir roots."""

import os
from pathlib import Path

import pytest

from jeff.adapter_dirs import check_base, scan
from jeff.errors import ManifestError
from jeff.testing import fake_adapter_folder


def test_finds_nested_folders(tmp_path: Path) -> None:
    deep = fake_adapter_folder(tmp_path / "apps/support/intents/deep-pick", "jeff.support.deep-pick")
    top = fake_adapter_folder(tmp_path / "triage-copy", "jeff.support.copy")
    (tmp_path / "apps/notes").mkdir()  # a folder with no adapter in it is fine
    found = scan([tmp_path])
    assert sorted(found) == ["jeff.support.copy", "jeff.support.deep-pick"]
    assert found["jeff.support.deep-pick"].folder == deep and found["jeff.support.copy"].folder == top
    assert found["jeff.support.copy"].manifest.id == "jeff.support.copy"
    names = [name for name, _size, _mtime in found["jeff.support.copy"].stamp]
    assert names == ["adapter_config.json", "adapter_model.safetensors", "readout.safetensors", "manifest.yaml"]


def test_a_folder_needs_both_the_manifest_and_the_adapter_config(tmp_path: Path) -> None:
    fake_adapter_folder(tmp_path / "half", "jeff.support.half")
    (tmp_path / "half/adapter_config.json").unlink()
    assert scan([tmp_path]) == {}


def test_a_duplicate_id_names_both_folders(tmp_path: Path) -> None:
    first = fake_adapter_folder(tmp_path / "one", "jeff.support.same")
    second = fake_adapter_folder(tmp_path / "two/deeper", "jeff.support.same")
    with pytest.raises(ManifestError, match="jeff.support.same") as excinfo:
        scan([tmp_path])
    assert str(first) in str(excinfo.value) and str(second) in str(excinfo.value)


def test_a_duplicate_id_across_two_roots_is_refused(tmp_path: Path) -> None:
    fake_adapter_folder(tmp_path / "a/x", "jeff.support.same")
    fake_adapter_folder(tmp_path / "b/x", "jeff.support.same")
    with pytest.raises(ManifestError, match="jeff.support.same"):
        scan([tmp_path / "a", tmp_path / "b"])


def test_dot_folders_are_skipped(tmp_path: Path) -> None:
    fake_adapter_folder(tmp_path / ".staging/jeff.support.half-copied", "jeff.support.half-copied")
    fake_adapter_folder(tmp_path / "apps/.tmp-copy", "jeff.support.other")
    assert scan([tmp_path]) == {}


def test_a_missing_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match=str(tmp_path / "nope")):
        scan([tmp_path / "nope"])


def test_an_empty_root_finds_nothing(tmp_path: Path) -> None:
    assert scan([tmp_path]) == {}


def test_an_invalid_manifest_names_the_folder(tmp_path: Path) -> None:
    folder = fake_adapter_folder(tmp_path / "broken", "jeff.support.broken")
    (folder / "manifest.yaml").write_text("id: jeff.support.broken\n")
    with pytest.raises(ManifestError, match=str(folder)):
        scan([tmp_path])


def test_the_stamp_changes_when_the_readout_is_rewritten(tmp_path: Path) -> None:
    folder = fake_adapter_folder(tmp_path / "x", "jeff.support.x")
    before = scan([tmp_path])["jeff.support.x"].stamp
    assert scan([tmp_path])["jeff.support.x"].stamp == before  # nothing changed, same stamp
    readout = folder / "readout.safetensors"
    readout.write_bytes(b"readout")  # same size; only the modification time moves
    info = readout.stat()
    os.utime(readout, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000))
    after = scan([tmp_path])["jeff.support.x"].stamp
    assert after != before
    assert [entry for entry in after if entry[0] != "readout.safetensors"] == \
        [entry for entry in before if entry[0] != "readout.safetensors"]


def test_check_base_names_both_bases(tmp_path: Path) -> None:
    fake_adapter_folder(tmp_path / "x", "jeff.support.x", base_version="v9.9")
    found = scan([tmp_path])
    with pytest.raises(ManifestError, match=r"jeff-base@v9\.9.*jeff-base@v1\.3"):
        check_base(found, ("jeff-base", "v1.3"))
    check_base(found, ("jeff-base", "v9.9"))


def test_an_unreadable_folder_is_refused(tmp_path: Path) -> None:
    fake_adapter_folder(tmp_path / "ok", "jeff.support.ok")
    locked = tmp_path / "locked"
    fake_adapter_folder(locked / "inner", "jeff.support.inner")
    locked.chmod(0o000)
    try:
        with pytest.raises(ManifestError, match=str(locked)):
            scan([tmp_path])
    finally:
        locked.chmod(0o700)


def test_symlinked_folders_are_followed(tmp_path: Path) -> None:
    real = fake_adapter_folder(tmp_path / "store/x-v2", "jeff.support.x")
    root = tmp_path / "adapters"
    root.mkdir()
    (root / "x").symlink_to(real, target_is_directory=True)
    found = scan([root])
    assert list(found) == ["jeff.support.x"] and found["jeff.support.x"].folder == root / "x"


def test_a_symlink_loop_is_refused(tmp_path: Path) -> None:
    fake_adapter_folder(tmp_path / "apps/x", "jeff.support.x")
    (tmp_path / "apps/back").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ManifestError, match="loop"):
        scan([tmp_path])


def test_two_found_adapters_sharing_an_alias_are_refused(tmp_path: Path) -> None:
    import yaml

    for name in ("one", "two"):
        folder = fake_adapter_folder(tmp_path / name, f"jeff.support.{name}")
        raw = yaml.safe_load((folder / "manifest.yaml").read_text())
        raw["aliases"] = ["shared"]
        (folder / "manifest.yaml").write_text(yaml.safe_dump(raw))
    with pytest.raises(ManifestError, match=r"shared.*jeff\.support\.one.*jeff\.support\.two"):
        scan([tmp_path])
