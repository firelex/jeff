"""import_jeffhub.py still builds the committed catalogue/*/manifest.yaml from the same inputs: jeffhub's manifests,
the open adapters' files and the private test sets, read from this machine's Hugging Face cache with no network, and
the jeff-data file listing saved in fixtures/jeff_data_listing.json. The calibration blocks `jeff calibrate` filled in
after the import are not the importer's output, so they are set back to null before comparing. The test is skipped,
saying why, only when jeffhub's manifests or a cached file are not on the machine."""

import copy
import json
from functools import partial
from pathlib import Path
from typing import Any

import pytest
import yaml
from huggingface_hub import hf_hub_download, snapshot_download
from huggingface_hub.errors import LocalEntryNotFoundError

import import_jeffhub

CATALOGUE = Path(__file__).resolve().parents[2] / "catalogue"
COMMITTED = {path.parent.name: path for path in sorted(CATALOGUE.glob("*/manifest.yaml"))}
# The adapters import_jeffhub.py imports when run with no names (its main()).
NAMES = sorted(path.name for path in import_jeffhub.JEFFHUB.iterdir()
               if (path / "manifest.yaml").exists() and path.name not in import_jeffhub.PENDING) \
    if import_jeffhub.JEFFHUB.is_dir() else []
DATA_FILES = json.loads((Path(__file__).parent / "fixtures" / "jeff_data_listing.json").read_text())


def without_calibration(manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = copy.deepcopy(manifest)
    for function in manifest["functions"].values():
        function["calibration"] = None
    return manifest


@pytest.mark.hf_cache
def test_import_jeffhub_rebuilds_the_committed_catalogue(monkeypatch: pytest.MonkeyPatch) -> None:
    if not NAMES:
        pytest.skip(f"jeffhub's adapter manifests are not on this machine ({import_jeffhub.JEFFHUB})")
    monkeypatch.setattr(import_jeffhub, "snapshot_download", partial(snapshot_download, local_files_only=True))
    monkeypatch.setattr(import_jeffhub, "hf_hub_download", partial(hf_hub_download, local_files_only=True))
    built = {}
    for name in NAMES:
        try:
            manifest = import_jeffhub.build(name, DATA_FILES)
        except LocalEntryNotFoundError as error:
            pytest.skip(f"{name}: a file it needs is not in this machine's Hugging Face cache ({error}); run "
                        "scripts/import_jeffhub.py once with network to fill the cache")
        built[manifest["id"]] = yaml.safe_load(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True,
                                                              width=120))
    assert sorted(built) == sorted(COMMITTED)
    for adapter_id, manifest in built.items():
        committed = yaml.safe_load(COMMITTED[adapter_id].read_text())
        assert manifest == without_calibration(committed), adapter_id
