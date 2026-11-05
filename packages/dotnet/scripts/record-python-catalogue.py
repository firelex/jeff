"""Re-records the expectations in packages/dotnet/test/Jeff.Sdk.Tests/Data/python-catalogue.json with the Python SDK.

The inputs (manifest edits, references, code-name keys, version pairs, base lookups, regex patterns) stay as they are;
every expected result is recomputed from what the Python SDK does with today's shipped catalogue. Run it after the
catalogue changes (after scripts/sync-shipped.sh), from the repository root:

    uv run --package jeff python packages/dotnet/scripts/record-python-catalogue.py
"""

import copy
import json
import platform
import re
from datetime import date
from pathlib import Path

import yaml

from jeff.catalogue import Catalogue
from jeff.contract import shipped_dir
from jeff.errors import JeffError
from jeff.ids import version_matches
from jeff.manifest import code_name_of, manifest_from_dict

DATA = Path(__file__).resolve().parents[1] / "test" / "Jeff.Sdk.Tests" / "Data" / "python-catalogue.json"


def error_of(error: JeffError) -> dict:
    return {"error": {"type": type(error).__name__, "message": str(error)}}


def apply(raw: dict, ops: list) -> None:
    for op in ops:
        node = raw
        for part in op[1][:-1]:
            node = node[part]
        if op[0] == "set":
            node[op[1][-1]] = copy.deepcopy(op[2])
        else:
            del node[op[1][-1]]


def main() -> None:
    recorded = json.loads(DATA.read_text(encoding="utf-8"))
    catalogue = Catalogue.shipped()
    raw_of = {path.parent.name: yaml.safe_load(path.read_text(encoding="utf-8"))
              for path in sorted((shipped_dir() / "catalogue").glob("*/manifest.yaml"))}

    for item in recorded["manifests"]:
        raw = copy.deepcopy(raw_of[item["adapter"]])
        apply(raw, item["ops"])
        try:
            manifest = manifest_from_dict(raw, Path("test"))
        except JeffError as error:
            item["expect"] = error_of(error)
            continue
        codes = [[field.key, field.code_name] for field in manifest.context] + [[manifest.input.key, manifest.input.code_name]]
        item["expect"] = {"ok": {"functions": list(manifest.functions), "codes": codes}}

    for item in recorded["refs"]:
        try:
            found = catalogue.function(item["ref"]).id if item["op"] == "function" else catalogue.adapter(item["ref"]).id
            item["expect"] = {"ok": found}
        except JeffError as error:
            item["expect"] = error_of(error)

    recorded["codes"] = {key: code_name_of(key) for key in recorded["codes"]}
    recorded["versions"] = [[wanted, actual, version_matches(wanted, actual)] for wanted, actual, _ in recorded["versions"]]
    for item in recorded["bases"]:
        try:
            item["expect"] = {"ok": catalogue.base(item["base"], item["version"])}
        except JeffError as error:
            item["expect"] = error_of(error)
    recorded["base_versions"] = [list(pair) for pair in catalogue.base_versions()]
    recorded["implementing"] = {interface: [spec.id for spec in catalogue.implementing(interface)]
                                for interface in recorded["implementing"]}
    recorded["names"] = sorted(catalogue.names())
    recorded["order"] = [manifest.id for manifest in catalogue.manifests()]

    for item in recorded["patterns"]:
        item.pop("message", None)
        try:
            re.compile(item["pattern"])
            item["python"] = "accepted"
        except re.error as error:
            item["python"], item["message"] = "refused", str(error)
        except OverflowError as error:
            item["python"], item["message"] = "OverflowError", str(error)

    description = re.sub(r"Python \d+\.\d+\.\d+\)", f"Python {platform.python_version()})", recorded["description"])
    recorded["description"] = re.sub(r"recorded on \d{4}-\d{2}-\d{2}", f"recorded on {date.today().isoformat()}", description)
    DATA.write_text(json.dumps(recorded, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
