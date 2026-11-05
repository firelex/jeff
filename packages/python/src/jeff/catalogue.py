"""The catalogue: every known adapter manifest, looked up by ID, alias, version or interface."""

from collections.abc import Iterable
from pathlib import Path
from typing import Any

import yaml

from jeff.contract import shipped_dir
from jeff.errors import ManifestError, UnknownFunction
from jeff.ids import parse_ref, version_matches
from jeff.manifest import FunctionSpec, Manifest, load_manifest


class Catalogue:
    def __init__(self, manifests: Iterable[Manifest], bases: dict[str, Any]) -> None:
        self._by_id: dict[str, Manifest] = {}
        self._by_alias: dict[str, str] = {}
        for manifest in manifests:
            for name in (manifest.id, *manifest.aliases):
                if name in self._by_id or name in self._by_alias:
                    raise ManifestError(f"{manifest.path}: the name {name} is already used by another adapter")
            self._by_id[manifest.id] = manifest
            for alias in manifest.aliases:
                self._by_alias[alias] = manifest.id
        self._bases = bases

    @classmethod
    def from_dir(cls, path: Path) -> "Catalogue":
        bases_file = path / "bases.yaml"
        if not bases_file.exists():
            raise ManifestError(f"{bases_file} is missing")
        manifests = [load_manifest(item) for item in sorted(path.glob("*/manifest.yaml"))]
        return cls(manifests, yaml.safe_load(bases_file.read_text(encoding="utf-8")))

    @classmethod
    def shipped(cls) -> "Catalogue":
        return cls.from_dir(shipped_dir() / "catalogue")

    def replacing(self, manifests: Iterable[Manifest]) -> "Catalogue":
        """A catalogue with the same bases holding `manifests` too, each in place of this catalogue's manifest of
        the same ID (an adapter folder an engine serves instead of the catalogue's download). A name (ID or alias)
        that two different adapters would then share is a ManifestError, as for any catalogue."""
        merged = dict(self._by_id)
        merged.update((manifest.id, manifest) for manifest in manifests)
        return Catalogue(merged.values(), self._bases)

    def without(self, adapter_ids: Iterable[str]) -> "Catalogue":
        """A catalogue with the same bases holding every manifest but those of `adapter_ids` (IDs, not aliases), so
        their IDs and aliases are unknown in it. An ID the catalogue does not have is a ManifestError."""
        dropped = set(adapter_ids)
        missing = sorted(dropped - set(self._by_id))
        if missing:
            raise ManifestError(f"Catalogue.without names {', '.join(missing)}, which the catalogue does not have")
        return Catalogue((manifest for adapter_id, manifest in self._by_id.items() if adapter_id not in dropped),
                         self._bases)

    def names(self) -> set[str]:
        """Every adapter ID and alias in the catalogue."""
        return set(self._by_id) | set(self._by_alias)

    def manifests(self) -> list[Manifest]:
        return list(self._by_id.values())

    def adapter(self, ref: str) -> Manifest:
        parsed = parse_ref(ref)
        name = self._by_alias.get(parsed.adapter, parsed.adapter)
        if name not in self._by_id:
            raise UnknownFunction(f"No adapter {parsed.adapter!r} in the catalogue")
        manifest = self._by_id[name]
        if parsed.version and not version_matches(parsed.version, manifest.version):
            raise UnknownFunction(f"{manifest.id}@{parsed.version} is not in the catalogue; it has {manifest.version}")
        return manifest

    def function(self, ref: str) -> FunctionSpec:
        parsed = parse_ref(ref)
        manifest = self.adapter(ref)
        if parsed.key is None:
            if len(manifest.functions) != 1:
                raise UnknownFunction(f"{manifest.id} has {len(manifest.functions)} functions "
                                      f"({', '.join(manifest.functions)}); name one, e.g. {manifest.id}."
                                      f"{next(iter(manifest.functions))}")
            return next(iter(manifest.functions.values()))
        if parsed.key not in manifest.functions:
            raise UnknownFunction(f"{manifest.id} has no function {parsed.key!r}; its functions are "
                                  f"{', '.join(manifest.functions)}")
        return manifest.functions[parsed.key]

    def manifest_of(self, function: FunctionSpec) -> Manifest:
        return self._by_id[function.adapter_id]

    def implementing(self, interface: str) -> list[FunctionSpec]:
        return [spec for manifest in self._by_id.values() for spec in manifest.functions.values()
                if spec.interface == interface and not manifest.is_router]

    def base_versions(self) -> list[tuple[str, str]]:
        """Every (base id, version) bases.yaml lists."""
        return [(base_id, version) for base_id, versions in self._bases.items() for version in versions]

    def base(self, base_id: str, version: str) -> dict[str, str]:
        versions = self._bases.get(base_id)
        if not versions or version not in versions:
            raise ManifestError(f"Base {base_id} {version} is not in bases.yaml (known: {self._bases})")
        return dict(versions[version])
