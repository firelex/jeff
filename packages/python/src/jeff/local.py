"""Local mode: the engine runs in this process (jeff-runtime), no network after the weights are cached."""

from collections.abc import Mapping, Sequence
from pathlib import Path

from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import ManifestError, ProprietaryWeights, RuntimeMissing
from jeff.log import DecisionLog
from jeff.manifest import Manifest


def resolve_base(catalogue: Catalogue, base_id: str, base_version: str) -> Path:
    """The base model's folder: bases.yaml's `path` for a base kept only on this machine (not on Hugging Face), else
    its repo and revision downloaded through the Hub cache. ManifestError when bases.yaml does not list it, or when
    its path is not a folder here."""
    from huggingface_hub import snapshot_download

    base_ref = catalogue.base(base_id, base_version)
    if "path" in base_ref:
        folder = Path(base_ref["path"])
        if not folder.is_dir():
            raise ManifestError(f"Base {base_id} {base_version} is the local folder {folder} (bases.yaml), which is "
                                "not a folder on this machine")
        return folder
    return Path(snapshot_download(base_ref["repo"], revision=base_ref["revision"]))


def adapter_folder(manifest: Manifest, folder: Path | None) -> Path:
    """An adapter's weights folder: `folder` when given, else its open weights downloaded through the Hub cache.
    ProprietaryWeights for any other weights (proprietary, or local: a folder only an engine's --adapters-dir
    finds) when no folder was given."""
    from huggingface_hub import snapshot_download

    if folder is not None:
        return Path(folder)
    if "open" not in manifest.weights:
        raise ProprietaryWeights(manifest.id)
    weights = manifest.weights["open"]
    return Path(snapshot_download(weights["repo"], revision=weights["revision"]))


def resolve_weights(catalogue: Catalogue, adapters: Sequence[str], paths: Mapping[str, Path] | None,
                    base: Path | None = None) -> tuple[Path, dict[str, Path]]:
    """The base folder and every adapter's folder: paths= first, then the open weights downloaded through the Hub
    cache; the base is `base` when given (a folder of the adapters' base, such as an INT4 base made from it, which
    the engine checks against the adapters when it loads them), else the catalogue's, downloaded. Raises
    ManifestError for an unknown or mismatched adapter list, ProprietaryWeights for an adapter whose
    weights are not open (proprietary or local) and whose folder was not given in paths=. Does not touch the
    engine."""
    manifests = [catalogue.adapter(ref) for ref in adapters]
    if not manifests:
        raise ManifestError("Needs at least one adapter (none were given)")
    bases = {(m.base_id, m.base_version) for m in manifests}
    if len(bases) != 1:
        raise ManifestError(f"The adapters are trained on different bases {sorted(bases)}; load one base at a time")
    folders: dict[str, Path] = {}
    for key, folder in (paths or {}).items():
        named = [m for m in manifests if key == m.id or key in m.aliases]
        if not named:
            raise ManifestError(f"paths= names {key!r}, which is not one of the adapters to load "
                                f"({', '.join(m.id for m in manifests)}); use an adapter's ID or alias")
        if named[0].id in folders:
            raise ManifestError(f"paths= names {named[0].id} twice")
        folders[named[0].id] = Path(folder)
    for manifest in manifests:
        if manifest.id not in folders and "open" not in manifest.weights:
            raise ProprietaryWeights(manifest.id)
    base_folder = Path(base) if base is not None else resolve_base(catalogue, *bases.pop())
    for manifest in manifests:
        folders[manifest.id] = adapter_folder(manifest, folders.get(manifest.id))
    return base_folder, folders


def local(adapters: Sequence[str], *, catalogue: Catalogue | None = None, paths: Mapping[str, Path] | None = None,
          backend: str = "auto", log: DecisionLog | None = None, prefix_pool_mb: int | None = None,
          base: Path | None = None) -> Jeff:
    """A Jeff deciding in this process. prefix_pool_mb: the GPU memory, in MB, for cached prompt prefixes, required
    when the backend is the fast one (an NVIDIA GPU; for example 2048 holds 66 prefixes of 1,000 tokens) and refused
    on the others (jeff_runtime.local_engine.check_prefix_pool_mb). base: a base folder to load instead of the
    adapters' base from the catalogue (resolve_weights), for example an INT4 base made from it."""
    if isinstance(adapters, str):
        raise ManifestError(f"Jeff.local() takes a list of adapters, not a single string; write "
                            f"Jeff.local([{adapters!r}])")
    try:
        from jeff_runtime import LocalEngine, check_prefix_pool_mb, select_backend
    except ImportError as error:
        raise RuntimeMissing("Local mode needs the engine: pip install 'jeff-runtime[mac]' (Apple silicon) or "
                             "'jeff-runtime[cuda]' (NVIDIA)") from error

    chosen = select_backend(backend)  # type: ignore[arg-type]
    check_prefix_pool_mb(chosen, prefix_pool_mb)  # a missing or misplaced size is refused before any download
    catalogue = catalogue or Catalogue.shipped()
    base_folder, folders = resolve_weights(catalogue, adapters, paths, base)
    # A local Jeff serves exactly the adapters it was made with, so the fast backend's room for adapters (required
    # there, refused elsewhere) is that many.
    sized: dict[str, int] = {"max_adapters": len(folders)} if chosen == "fast" else {}
    if prefix_pool_mb is not None:  # checked above: given exactly when the backend is the fast one
        sized["prefix_pool_mb"] = prefix_pool_mb
    # called from the caller's own thread: one request prepared at a time, with one processor
    engine = LocalEngine(base_folder, folders, backend=chosen, prepare_workers=1, **sized)
    return Jeff(engine, catalogue, log=log)


def local_for_batches(adapters: Sequence[str], *, rows_per_pass: int, backend: str = "auto",
                      catalogue: Catalogue | None = None, base: Path | None = None,
                      paths: Mapping[str, Path] | None = None) -> Jeff:
    """Jeff.local for a batch tool (jeff test, jeff calibrate, scripts/record_golden.py) that hands the engine at most
    `rows_per_pass` rows at once. On the fast backend its prefix pool is prefix_pool_mb_for(rows_per_pass): the
    smallest pool that holds that many rows, each with its own prefix of the longest length, as the factory's score
    job sizes its own. On mlx and torch no size is passed, since there it is refused."""
    try:
        from jeff_runtime import select_backend
        from jeff_runtime.engine.prefix_pool import prefix_pool_mb_for
    except ImportError as error:
        raise RuntimeMissing("Local mode needs the engine: pip install 'jeff-runtime[mac]' (Apple silicon) or "
                             "'jeff-runtime[cuda]' (NVIDIA)") from error
    chosen = select_backend(backend)  # type: ignore[arg-type]
    return local(adapters, catalogue=catalogue, paths=paths, backend=chosen, base=base,
                 prefix_pool_mb=prefix_pool_mb_for(rows_per_pass) if chosen == "fast" else None)
