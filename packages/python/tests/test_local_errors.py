"""Jeff.local() error paths, without weights: the engine module and the downloads are replaced."""

import shutil
import sys
import types
from pathlib import Path
from typing import Any

import huggingface_hub
import pytest
import yaml

from jeff import Jeff
from jeff.catalogue import Catalogue
from jeff.errors import EngineError, ManifestError, ProprietaryWeights, RuntimeMissing
from jeff_runtime.local_engine import check_prefix_pool_mb

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"


@pytest.fixture
def engine(monkeypatch: pytest.MonkeyPatch) -> list[tuple[Any, ...]]:
    made: list[tuple[Any, ...]] = []

    class LocalEngine:
        base_format = {"format": "bf16"}

        def __init__(self, base: Path, adapters: dict[str, Path], backend: str, prepare_workers: int) -> None:
            assert prepare_workers == 1  # Jeff.local prepares on the caller's own thread
            made.append((base, adapters, backend))

        def installed(self, refresh: bool = False) -> set[str]:
            return set(made[-1][1])

    monkeypatch.setitem(sys.modules, "jeff_runtime", types.SimpleNamespace(
        LocalEngine=LocalEngine, select_backend=lambda backend: backend, check_prefix_pool_mb=check_prefix_pool_mb))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, revision: f"/cache/{repo}@{revision}")
    return made


def catalogue_with(tmp_path: Path, change: dict[str, Any]) -> Catalogue:
    folder = tmp_path / "catalogue"
    shutil.copytree(FIXTURES, folder)
    path = folder / "jeff.safety.spam" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    raw.update(change)
    path.write_text(yaml.safe_dump(raw))
    bases = yaml.safe_load((folder / "bases.yaml").read_text())
    bases["jeff-base"]["v9"] = {"repo": "x/base", "revision": "v9"}
    (folder / "bases.yaml").write_text(yaml.safe_dump(bases))
    return Catalogue.from_dir(folder)


def test_paths_keys_are_resolved_by_alias(engine: list, tmp_path: Path) -> None:
    Jeff.local(["triage"], catalogue=Catalogue.from_dir(FIXTURES), paths={"triage": tmp_path})
    base, adapters, backend = engine[-1]
    assert adapters == {"jeff.support.triage": tmp_path}
    assert base == Path("/cache/mstrasser/jeff-base@v1.3") and backend == "auto"


def test_paths_key_not_requested_is_refused(engine: list, tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="spam"):
        Jeff.local(["triage"], catalogue=Catalogue.from_dir(FIXTURES), paths={"spam": tmp_path})


def test_paths_key_unknown_is_refused(engine: list, tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="nosuch"):
        Jeff.local(["triage"], catalogue=Catalogue.from_dir(FIXTURES), paths={"nosuch": tmp_path})


def test_no_adapters_is_refused(engine: list) -> None:
    with pytest.raises(ManifestError, match="at least one"):
        Jeff.local([], catalogue=Catalogue.from_dir(FIXTURES))


def test_mixed_bases_are_refused(engine: list, tmp_path: Path) -> None:
    catalogue = catalogue_with(tmp_path, {"base": {"id": "jeff-base", "version": "v9", "max_options": 252}})
    with pytest.raises(ManifestError, match="different bases"):
        Jeff.local(["triage", "spam"], catalogue=catalogue)


def test_proprietary_weights_need_a_path(engine: list, tmp_path: Path) -> None:
    catalogue = catalogue_with(tmp_path, {"licence": {"kind": "proprietary", "adapter": "Commercial"},
                                          "weights": {"proprietary": {"repo": "teamsuzie/jeff-adapters",
                                                                      "path": "spam"}}})
    with pytest.raises(ProprietaryWeights, match="jeff.safety.spam"):
        Jeff.local(["spam"], catalogue=catalogue)
    Jeff.local(["spam"], catalogue=catalogue, paths={"spam": tmp_path})
    assert engine[-1][1] == {"jeff.safety.spam": tmp_path}


def test_runtime_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "jeff_runtime", None)
    with pytest.raises(RuntimeMissing, match=r"jeff-runtime\[mac\]"):
        Jeff.local(["triage"], catalogue=Catalogue.from_dir(FIXTURES))


def test_a_single_string_is_refused_with_a_hint(engine: list) -> None:
    with pytest.raises(ManifestError, match=r"list.*\['triage'\]"):
        Jeff.local("triage", catalogue=Catalogue.from_dir(FIXTURES))  # type: ignore[arg-type]


def test_resolve_weights_returns_folders_without_building_an_engine(engine: list) -> None:
    from jeff.local import resolve_weights

    base, folders = resolve_weights(Catalogue.from_dir(FIXTURES), ["triage"], None)
    assert base == Path("/cache/mstrasser/jeff-base@v1.3")
    assert folders == {"jeff.support.triage": Path("/cache/mstrasser/jeff-adapter-triage@v1.3")}
    assert engine == []  # no LocalEngine was constructed


def test_local_weights_need_a_path(engine: list, tmp_path: Path) -> None:
    """weights.local: the weights are the folder holding the manifest; nothing can download them."""
    catalogue = catalogue_with(tmp_path, {"licence": {"kind": "proprietary", "adapter": "Commercial"},
                                          "weights": {"local": {}}})
    with pytest.raises(ProprietaryWeights, match="jeff.safety.spam"):
        Jeff.local(["spam"], catalogue=catalogue)
    Jeff.local(["spam"], catalogue=catalogue, paths={"spam": tmp_path})
    assert engine[-1][1] == {"jeff.safety.spam": tmp_path}


def test_local_weights_take_no_fields(tmp_path: Path) -> None:
    from jeff.errors import ContractError

    with pytest.raises(ContractError, match="weights"):
        catalogue_with(tmp_path, {"weights": {"local": {"folder": "/x"}}})


def test_prefix_pool_mb_is_checked_before_any_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing prefix_pool_mb on the fast backend, or one given on another backend, is refused before any weights
    are looked up or downloaded."""
    downloads: list[str] = []
    monkeypatch.setitem(sys.modules, "jeff_runtime", types.SimpleNamespace(
        LocalEngine=None, select_backend=lambda backend: backend, check_prefix_pool_mb=check_prefix_pool_mb))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, revision: downloads.append(repo))
    catalogue = Catalogue.from_dir(FIXTURES)
    with pytest.raises(EngineError, match="needs --prefix-pool-mb"):
        Jeff.local(["triage"], catalogue=catalogue, backend="fast")
    with pytest.raises(EngineError, match="only applies to the fast backend"):
        Jeff.local(["triage"], catalogue=catalogue, backend="mlx", prefix_pool_mb=2048)
    assert downloads == []


def test_prefix_pool_mb_reaches_the_engine_only_when_given(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Jeff.local passes prefix_pool_mb to the engine on the fast backend, where it is required; on the others it is
    left out (it is refused there, test above), never given a default."""
    made: list[dict[str, Any]] = []

    class LocalEngine:
        base_format = {"format": "bf16"}

        def __init__(self, base: Path, adapters: dict[str, Path], **options: Any) -> None:
            made.append(options)

        def installed(self, refresh: bool = False) -> set[str]:
            return {"jeff.support.triage"}

    monkeypatch.setitem(sys.modules, "jeff_runtime", types.SimpleNamespace(
        LocalEngine=LocalEngine, select_backend=lambda backend: backend, check_prefix_pool_mb=check_prefix_pool_mb))
    catalogue = Catalogue.from_dir(FIXTURES)
    Jeff.local(["triage"], catalogue=catalogue, paths={"triage": tmp_path}, backend="fast", prefix_pool_mb=2048)
    Jeff.local(["triage"], catalogue=catalogue, paths={"triage": tmp_path}, backend="mlx")
    assert made == [{"backend": "fast", "prepare_workers": 1, "max_adapters": 1, "prefix_pool_mb": 2048},
                    {"backend": "mlx", "prepare_workers": 1}]


@pytest.mark.parametrize(("backend", "expected"), [
    ("fast", {"backend": "fast", "prepare_workers": 1, "max_adapters": 1, "prefix_pool_mb": "for 16 rows"}),
    ("mlx", {"backend": "mlx", "prepare_workers": 1}),
    ("torch", {"backend": "torch", "prepare_workers": 1}),
])
def test_make_jeff_sizes_the_prefix_pool_for_its_batches_only_on_the_fast_backend(
        backend: str, expected: dict[str, Any], monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """jeff test and jeff calibrate hand the engine at most jeff.evaluate.BATCH (16) rows at once, so on the fast
    backend their prefix pool is prefix_pool_mb_for(16), the smallest that holds 16 rows; on mlx and torch, where
    the size is refused, none is passed. "auto" resolving to each backend is what decides.

    Uses jeff.support.triage: this resolves the real shipped catalogue's base (on Hugging Face) and adapter through
    huggingface_hub.snapshot_download, mocked below."""
    from jeff import cli
    from jeff.evaluate import BATCH
    from jeff_runtime.engine.prefix_pool import prefix_pool_mb_for

    made: list[dict[str, Any]] = []

    class LocalEngine:
        base_format = {"format": "bf16"}

        def __init__(self, base: Path, adapters: dict[str, Path], **options: Any) -> None:
            made.append(options)

        def installed(self, refresh: bool = False) -> set[str]:
            return {"jeff.support.triage"}

    monkeypatch.setitem(sys.modules, "jeff_runtime", types.SimpleNamespace(
        LocalEngine=LocalEngine, select_backend=lambda requested: backend, check_prefix_pool_mb=check_prefix_pool_mb))
    monkeypatch.setattr(huggingface_hub, "snapshot_download", lambda repo, revision: f"/cache/{repo}@{revision}")
    cli.make_jeff(["jeff.support.triage"], "auto")
    assert BATCH == 16
    if "prefix_pool_mb" in expected:
        expected = {**expected, "prefix_pool_mb": prefix_pool_mb_for(16)}
    assert made == [expected]


def _base_at(tmp_path: Path, path: Path) -> Catalogue:
    """The fixture catalogue with its v1.3 base given as a folder on this machine (a base not on Hugging Face)."""
    folder = tmp_path / "catalogue"
    shutil.copytree(FIXTURES, folder)
    bases = yaml.safe_load((folder / "bases.yaml").read_text())
    bases["jeff-base"]["v1.3"] = {"path": str(path), "model_sha256": "a" * 64, "released": "2026-10-05"}
    (folder / "bases.yaml").write_text(yaml.safe_dump(bases))
    return Catalogue.from_dir(folder)


def test_a_base_kept_in_a_local_folder_is_used_without_a_download(engine: list, tmp_path: Path) -> None:
    from jeff.local import resolve_weights

    (tmp_path / "base").mkdir()
    base, _ = resolve_weights(_base_at(tmp_path, tmp_path / "base"), ["triage"], None)
    assert base == tmp_path / "base"


def test_a_local_base_folder_that_is_missing_is_refused(engine: list, tmp_path: Path) -> None:
    from jeff.local import resolve_weights

    with pytest.raises(ManifestError, match=r"jeff-base v1\.3 is the local folder .*nowhere.*not a folder"):
        resolve_weights(_base_at(tmp_path, tmp_path / "nowhere"), ["triage"], None)
