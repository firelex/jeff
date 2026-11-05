"""Adapter folders found on disk: an engine started with `--adapters-dir` serves any adapter whose folder sits under
one of those roots (the server's own copies, as on datigator's /mnt/d/jeff-adapters), next to the ones named on its
command line. `scan` runs at startup and again on every `reload`, so a folder dropped in (or rewritten) is picked up
without restarting the engine; it never runs on the predict path."""

import os
from collections.abc import Sequence
from pathlib import Path
from typing import NamedTuple

import yaml

from jeff.errors import ContractError, ManifestError
from jeff.manifest import Manifest, load_manifest

# The files whose size and modification time make up a folder's stamp: the weights an engine loads, and the
# manifest. A rewritten readout (say) changes the stamp, and `reload` then loads the adapter again.
WEIGHT_FILES = ("adapter_config.json", "adapter_model.safetensors", "decision_config.json", "readout.safetensors")
MANIFEST_FILE = "manifest.yaml"


Stamp = tuple[tuple[str, int, int], ...]


class Found(NamedTuple):
    """One adapter folder: its manifest's ID, the folder, the manifest, and its stamp, (relative file name, size,
    mtime_ns) of each weight file present and of manifest.yaml, in that order."""
    id: str
    folder: Path
    manifest: Manifest
    stamp: Stamp


def stamp_of(folder: Path) -> Stamp:
    stamp = []
    for name in (*WEIGHT_FILES, MANIFEST_FILE):
        path = folder / name
        if path.is_file():
            info = path.stat()
            stamp.append((name, info.st_size, info.st_mtime_ns))
    return tuple(stamp)


def _load(folder: Path) -> Manifest:
    try:
        return load_manifest(folder / MANIFEST_FILE)
    except (ManifestError, ContractError, yaml.YAMLError, OSError) as error:
        raise ManifestError(f"adapter folder {folder}: {error}") from error


def _unreadable(error: OSError) -> None:
    raise ManifestError(f"adapters dir: cannot read {error.filename} ({error.strerror}); an adapter folder there "
                        "would be missed")


def scan(roots: Sequence[Path]) -> dict[str, Found]:
    """Every adapter folder under `roots`, by adapter ID. A folder is an adapter when it holds both manifest.yaml
    and adapter_config.json; folders whose names start with '.' are skipped, with everything under them (publishing
    copies an adapter in under a dot name, then renames it into place, so a half-copied folder is never seen).
    Symlinked folders are followed (deploys link adapter folders in). ManifestError for a root that does not exist,
    a folder that cannot be read, a folder reached twice (a symlink loop, or two links or roots to one folder), an
    invalid manifest, two folders with the same ID, or two adapters sharing an alias."""
    found: dict[str, Found] = {}
    seen: dict[str, Path] = {}  # real path -> the path it was first reached by
    for root in roots:
        root = Path(root)
        if not root.is_dir():
            raise ManifestError(f"adapters dir {root} does not exist (or is not a folder)")
        for current, dirs, files in os.walk(root, onerror=_unreadable, followlinks=True):
            folder = Path(current)
            real = os.path.realpath(folder)
            if real in seen:
                raise ManifestError(f"adapters dir: {folder} is the same folder as {seen[real]} (a symlink loop, or "
                                    "two links or roots to one folder); each folder must be reached once")
            seen[real] = folder
            dirs[:] = sorted(name for name in dirs if not name.startswith("."))
            if MANIFEST_FILE not in files or "adapter_config.json" not in files:
                continue
            manifest = _load(folder)
            if manifest.id in found:
                raise ManifestError(f"two adapter folders give the ID {manifest.id}: {found[manifest.id].folder} "
                                    f"and {folder}; keep one")
            found[manifest.id] = Found(manifest.id, folder, manifest, stamp_of(folder))
    aliases: dict[str, Found] = {}
    for item in found.values():
        for alias in item.manifest.aliases:
            if alias in aliases:
                other = aliases[alias]
                raise ManifestError(f"the alias {alias} is used by both {other.id} ({other.folder}) and {item.id} "
                                    f"({item.folder}); give each adapter its own aliases")
            aliases[alias] = item
    return found


def trained_for(manifest: Manifest, base: tuple[str, str]) -> bool:
    """Whether the adapter of `manifest` was trained on `base` (base id, version): the rule an engine refuses an
    adapter by (check_base), and the one jeff-server lists adapters by (GET /v1/adapters lists only the adapters
    trained for the base its engines serve)."""
    return (manifest.base_id, manifest.base_version) == tuple(base)


def check_base(found: dict[str, Found], base: tuple[str, str]) -> None:
    """ManifestError naming the adapter and both bases when a found adapter was trained on a base other than
    `base` (base id, version), the one the engine has loaded."""
    for item in found.values():
        if not trained_for(item.manifest, base):
            theirs = (item.manifest.base_id, item.manifest.base_version)
            raise ManifestError(f"adapter folder {item.folder}: {item.id} is trained on base {theirs[0]}@{theirs[1]}, "
                                f"but this engine has loaded base {base[0]}@{base[1]}")
