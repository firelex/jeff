"""Write catalogue/<id>/manifest.yaml for the open adapters at the version the API serves (spec
2026-11-01-public-hub-design.md, Section 3.3): each served v1.4 manifest in results/v14-manifests/ named in
catalogue-imports/open-set.yaml, made open and pointed at its public Hugging Face repository. The hub prose of the
adapter's v1.3 manifest (catalogue/<id>, if any) is kept, and so is its licence text (licence.adapter), which for
some adapters records restrictions on the data they were trained on; an adapter with no v1.3 manifest is Apache-2.0. Nothing is uploaded: the release that creates the public
repositories is a separate step the owner approves."""

import copy
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
SERVED_DIR = ROOT / "results/v14-manifests"
CATALOGUE = ROOT / "catalogue"
OPEN_SET = ROOT / "catalogue-imports/open-set.yaml"
REVISION = "v1.4"
HUB_PRIVATE_KEYS = ("notes", "source")
# From the v1.3 hub, only this prose still applies at v1.4: everything else there (its own version, base,
# huggingface revision, data card, provenance, submission...) describes the v1.3 release. The rest of the result's
# hub comes from the served v1.4 hub (minus HUB_PRIVATE_KEYS).
HUB_V13_PROSE_KEYS = ("title", "summary", "task", "kind", "maintainer")


def open_manifest(served: dict[str, Any], v13: dict[str, Any] | None, public_name: str) -> dict[str, Any]:
    if "/" in public_name or not public_name:
        raise ValueError(f"{served['id']}: the public name {public_name!r} must be a bare name such as 'entails'")
    repo = f"mstrasser/jeff-adapter-{public_name}"
    files = f"hf://{repo}@{REVISION}"
    out = copy.deepcopy(served)
    # An open adapter's short name, the one Jeff.local takes, is the last part of its ID (jeff.routing.router-open-v14
    # -> router-open-v14), and the hub refuses an open adapter without that alias. A served manifest that lacks it
    # (its aliases were chosen before it was opened) gets it here.
    short_name = served["id"].rsplit(".", 1)[1]
    if short_name not in out["aliases"]:
        out["aliases"].append(short_name)
    # A v1.3 manifest's licence text stays: it may record that the training data restricts use (for example
    # non-commercial only). Only an adapter released for the first time at v1.4 has no such text and is Apache-2.0.
    out["licence"] = {"kind": "open", "adapter": "Apache-2.0" if v13 is None else v13["licence"]["adapter"]}
    out["weights"] = {"open": {"repo": repo, "revision": REVISION}}
    out["tests"] = {"published": f"{files}/test.jsonl", "calibration": f"{files}/calibration.jsonl",
                    "function_by": served["tests"]["function_by"]}
    for function in out["functions"].values():
        calibration = function.get("calibration")
        if calibration is not None:
            calibration["data"] = f"{files}/calibration.jsonl"
            calibration.pop("data_mirror", None)
    hub = {key: value for key, value in served["hub"].items() if key not in HUB_PRIVATE_KEYS}
    if v13 is not None:
        hub.update({key: value for key, value in v13["hub"].items() if key in HUB_V13_PROSE_KEYS})
    out["hub"] = hub
    return out


def main() -> None:
    open_set = yaml.safe_load(OPEN_SET.read_text())
    for adapter_id, public_name in open_set["v14"].items():
        served_path = SERVED_DIR / adapter_id / "manifest.yaml"
        if not served_path.is_file():
            raise SystemExit(f"{adapter_id}: {served_path} is missing")
        served = yaml.safe_load(served_path.read_text())
        target = CATALOGUE / adapter_id / "manifest.yaml"
        v13 = yaml.safe_load(target.read_text()) if target.is_file() else None
        manifest = open_manifest(served, v13, public_name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=120))
        print(f"{adapter_id} {manifest['version']} -> {target.relative_to(ROOT)}")
    for adapter_id in open_set["v13_only"]:
        if not (CATALOGUE / adapter_id / "manifest.yaml").is_file():
            raise SystemExit(f"{adapter_id}: listed as v1.3 only but catalogue/{adapter_id}/manifest.yaml is missing")


if __name__ == "__main__":
    main()
