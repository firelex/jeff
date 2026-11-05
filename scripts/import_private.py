"""Build <out>/<id>/manifest.yaml for private adapters in teamsuzie/jeff-adapters, one per hand-written
catalogue-imports/<name>.yaml (catalogue-imports/README.md). Hugging Face is only read: the adapter folder is listed
(its weight files must be there; they are never downloaded), its decision_config.json and README.md are downloaded,
and so are the test.jsonl and calibration.jsonl of the same path in the dataset teamsuzie/jeff-data. With
--adapters-root the adapter folders are read from a folder on disk instead (adapters not on Hugging Face, served from
an engine's --adapters-dir), and the manifest's weights are `local`. Each function's instructions and labels are
derived from its test rows; every rule the rows alone do not settle is written into the manifest's hub.notes. The
import stops, naming the adapter and the function, when the rows and the import file disagree, when a row of either
set maps to no function or is one the manifest cannot describe, or when the calibration and test sets share a row.
Nothing is ever uploaded."""

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from catalogue_import_common import choice_labels, function_of, instructions_from_rows, read_rows
from jeff import contract
from jeff.binding import bind, build_row
from jeff.errors import JeffError, ManifestError
from jeff.evaluate import resolve, template_rows
from jeff.manifest import Manifest, StateShape, manifest_from_dict, request_fields, request_shape

ROOT = Path(__file__).resolve().parents[1]
IMPORTS = ROOT / "catalogue-imports"
BASES = ROOT / "catalogue" / "bases.yaml"
ADAPTER_REPO, DATA_REPO, REVISION = "teamsuzie/jeff-adapters", "teamsuzie/jeff-data", "main"
# The files of an adapter folder (docs/storage.md); a folder without them (a full model, for example) is refused.
ADAPTER_FILES = ("adapter_config.json", "adapter_model.safetensors", "decision_config.json", "readout.safetensors")
FIELDS = ("path", "id", "aliases", "version", "function_by", "context", "input", "functions")
# Optional fields: adapter_path, the adapter folder when it is not at `path` (for example its folder under
# --adapters-root); data_revision, the jeff-data revision its rows are read at when not main (an adapter trained on a
# data version that main has since replaced); kinds and id_patterns, the manifest's tests.kinds and tests.id_patterns
# (for the source_kind and id_pattern criteria); access, the manifest's access (owner, or {accounts: [account ids]}:
# who may call the adapter on a Jeff server, contract/schemas/manifest.schema.json).
OPTIONAL = ("adapter_path", "data_revision", "kinds", "id_patterns", "access")
# tests.function_by: one of these, or a list of the criteria (all but only), each at most once
FUNCTION_BY = ("only", "id_suffix", "question_type", "source_kind", "state_shape", "id_pattern")
INPUT_FORMS = ("fields", "text")
# what a function of an import file may say besides its interface: its own request shape (context and input, both or
# neither) and, for fixed choice labels whose order the rows do not settle, the labels in order and whether a caller
# may offer a subset of them
FUNCTION_PARTS = ("interface", "context", "input", "labels", "option_order")
LICENCE = {"kind": "proprietary", "adapter": "Scissero proprietary"}

# fetch(repo, path, repo_type, revision) -> the downloaded file; listing(repo, repo_type, revision) -> every file
# path in the repo -> its LFS SHA-256 (None for a file not stored in LFS). Injectable so the tests run on a local fake
# tree.
Fetch = Callable[[str, str, str, str], Path]
Listing = Callable[[str, str, str], dict[str, str | None]]


def hf_fetch(repo: str, path: str, repo_type: str, revision: str) -> Path:
    """One file of a Hugging Face repo, through jeff.evaluate.resolve (the one reader of hf:// files): a dataset file
    at a revision JEFF_DATA_MIRROR maps is read from the local copy, with no download."""
    prefix = "datasets/" if repo_type == "dataset" else ""
    return resolve(f"hf://{prefix}{repo}@{revision}/{path}")


class HubAdapters:
    """Adapter folders in teamsuzie/jeff-adapters: listed and read on Hugging Face; the weights are proprietary."""

    label = ADAPTER_REPO

    def __init__(self, fetch: Fetch, listing: Listing) -> None:
        self.fetch, self.listing = fetch, listing

    def files(self, path: str) -> set[str] | None:
        """The files directly in the folder `path`, or None when there is no such folder."""
        inside = {file[len(path) + 1:] for file in self.listing(ADAPTER_REPO, "model", REVISION)
                  if file.startswith(f"{path}/")}
        return {file for file in inside if "/" not in file} if inside else None

    def read(self, path: str, file: str) -> str:
        return self.fetch(ADAPTER_REPO, f"{path}/{file}", "model", REVISION).read_text(encoding="utf-8")

    def where(self, path: str) -> str:
        return f"{self.label} {path}"

    def weights(self, path: str) -> dict[str, Any]:
        return {"proprietary": {"repo": ADAPTER_REPO, "path": path}}

    def source(self, path: str) -> str:
        return f"{ADAPTER_REPO}/{path}"


class FolderAdapters:
    """Adapter folders under a folder on disk (--adapters-root): adapters that are not on Hugging Face, served from an
    engine's --adapters-dir, so the weights are `local` (the folder holding the manifest). The folder may hold only
    the small files (a copy of decision_config.json and README.md) when `listing` names every file of the real
    folders (one path per line, relative to the root, as `find <root> -type f -printf '%P\\n'` writes it)."""

    def __init__(self, root: Path, listing: Path | None) -> None:
        if not root.is_dir():
            sys.exit(f"--adapters-root {root} is not a folder")
        self.root, self.label = root, f"--adapters-root {root}"
        if listing is None:
            self.listed = {str(file.relative_to(root)) for file in root.rglob("*") if file.is_file()}
        else:
            if not listing.is_file():
                sys.exit(f"--adapters-listing {listing} is not a file")
            self.listed = {line.strip() for line in listing.read_text(encoding="utf-8").splitlines() if line.strip()}

    def files(self, path: str) -> set[str] | None:
        inside = {file[len(path) + 1:] for file in self.listed if file.startswith(f"{path}/")}
        return {file for file in inside if "/" not in file} if inside else None

    def read(self, path: str, file: str) -> str:
        target = self.root / path / file
        if not target.is_file():
            sys.exit(f"{target} is missing: --adapters-root must hold each adapter folder's {file}")
        return target.read_text(encoding="utf-8")

    def where(self, path: str) -> str:
        return f"{self.label} {path}"

    def weights(self, path: str) -> dict[str, Any]:
        return {"local": {}}

    def source(self, path: str) -> str:
        return f"local adapter folder {path} (not on Hugging Face)"


def hf_listing(repo: str, repo_type: str, revision: str) -> dict[str, str | None]:
    from huggingface_hub import HfApi
    from huggingface_hub.hf_api import RepoFile

    return {item.path: item.lfs.sha256 if item.lfs else None
            for item in HfApi().list_repo_tree(repo, repo_type=repo_type, revision=revision, recursive=True)
            if isinstance(item, RepoFile)}


def data_uri(path: str, file: str, revision: str = REVISION) -> str:
    return f"hf://datasets/{DATA_REPO}@{revision}/{path}/{file}"


def load_import(name: str, imports: Path) -> dict[str, Any]:
    file = imports / f"{name}.yaml"
    if not file.is_file():
        sys.exit(f"{name}: no import file {file}")
    spec = yaml.safe_load(file.read_text(encoding="utf-8"))
    where = f"{name}: {file}"
    if not isinstance(spec, dict):
        sys.exit(f"{where}: not a mapping of fields")
    missing = [field for field in FIELDS if field not in spec]
    if missing:
        sys.exit(f"{where}: missing {', '.join(missing)} (every field is required: {', '.join(FIELDS)})")
    unknown = [field for field in spec if field not in FIELDS + OPTIONAL]
    if unknown:
        sys.exit(f"{where}: unknown fields {unknown} (the fields are {', '.join(FIELDS)}, and optionally "
                 f"{', '.join(OPTIONAL)})")
    for field in ("adapter_path", "data_revision"):
        if field in spec and (not isinstance(spec[field], str) or not spec[field]):
            sys.exit(f"{where}: {field} must be a non-empty string")
    for field in ("kinds", "id_patterns"):
        if field in spec and (not isinstance(spec[field], dict) or not spec[field]):
            sys.exit(f"{where}: {field} must be a non-empty mapping (it becomes the manifest's tests.{field})")
    for field in ("path", "id", "version"):
        if not isinstance(spec[field], str) or not spec[field]:
            sys.exit(f"{where}: {field} must be a non-empty string")
    if not isinstance(spec["aliases"], list) or not all(isinstance(alias, str) and alias for alias in spec["aliases"]):
        sys.exit(f"{where}: aliases must be a list of non-empty strings")
    function_by = spec["function_by"]
    criteria = FUNCTION_BY[1:]
    if isinstance(function_by, list):
        if not function_by or len(set(map(str, function_by))) != len(function_by) \
                or any(criterion not in criteria for criterion in function_by):
            sys.exit(f"{where}: function_by as a list must hold one or more of {', '.join(criteria)}, each at most "
                     f"once; got {function_by!r}")
    elif function_by not in FUNCTION_BY:
        sys.exit(f"{where}: function_by {function_by!r} is not one of {', '.join(FUNCTION_BY)} (or a list of "
                 f"{', '.join(criteria)})")
    request_fields_of(where, "", spec)
    if not isinstance(spec["functions"], dict) or not spec["functions"]:
        sys.exit(f"{where}: functions must be a non-empty mapping of function key -> {{interface: <name>}}")
    for key, body in spec["functions"].items():
        if not isinstance(body, dict) or not isinstance(body.get("interface"), str) \
                or any(part not in FUNCTION_PARTS for part in body):
            sys.exit(f"{where}: function {key} must be {{interface: <name>}}, optionally with its own context and "
                     f"input, labels and option_order (the parts are {', '.join(FUNCTION_PARTS)})")
        if "option_order" in body and body["option_order"] != "shuffled":
            sys.exit(f"{where}: function {key}: option_order must be 'shuffled' (training offered the options in "
                     f"varying orders from row to row), or left out; got {body['option_order']!r}")
        if ("context" in body) != ("input" in body):
            sys.exit(f"{where}: function {key} declares {'context' if 'context' in body else 'input'} without "
                     f"{'input' if 'context' in body else 'context'}; a function declares its own context and input "
                     "together, or neither")
        if "input" in body:
            request_fields_of(where, f"functions.{key}.", body)
        if "labels" in body:
            labels = body["labels"]
            if not isinstance(labels, dict) or sorted(labels) != ["fixed", "subset"] \
                    or not isinstance(labels["fixed"], list) or not labels["fixed"] \
                    or not all(isinstance(label, str) and label for label in labels["fixed"]) \
                    or len(set(labels["fixed"])) != len(labels["fixed"]) or not isinstance(labels["subset"], bool):
                sys.exit(f"{where}: function {key}: labels must be {{fixed: [label keys in order], subset: true or "
                         "false}, the fixed labels in the order the manifest lists them and whether a caller may "
                         "offer a subset of them")
    if spec["function_by"] == "only" and len(spec["functions"]) != 1:
        sys.exit(f"{where}: function_by only needs exactly one function; it has {list(spec['functions'])}")
    uses = "id_pattern" in (function_by if isinstance(function_by, list) else [function_by])
    if uses != ("id_patterns" in spec):
        sys.exit(f"{where}: id_patterns goes with the id_pattern criterion: give both or neither")
    if uses and list(spec["id_patterns"]) != list(spec["functions"]):
        sys.exit(f"{where}: id_patterns must name every function, in the functions' order ({list(spec['functions'])}); "
                 f"it names {list(spec['id_patterns'])}")
    return spec


def request_fields_of(where: str, prefix: str, owner: dict[str, Any]) -> None:
    """Stop unless `owner` (the import file, or one of its functions) has a valid context list and input block."""
    if not isinstance(owner["context"], list):
        sys.exit(f"{where}: {prefix}context must be a list of {{key, description}}")
    for index, field in enumerate(owner["context"]):
        described(where, f"{prefix}context[{index}]", field, optional=("required", "code_name"))
        if not isinstance(field.get("required", True), bool):
            sys.exit(f"{where}: {prefix}context[{index}].required must be true or false")
    described(where, f"{prefix}input", owner["input"], optional=("form", "code_name"))
    if owner["input"].get("form") not in INPUT_FORMS:
        sys.exit(f"{where}: {prefix}input.form must be one of {', '.join(INPUT_FORMS)} (fields: the rows' state is an "
                 "object of named fields, the input last; text: the state is one plain string, the input itself), "
                 f"got {owner['input'].get('form')!r}")
    if owner["input"]["form"] == "text" and owner["context"]:
        sys.exit(f"{where}: {prefix}input.form text means the state is the plain input string, so {prefix}context "
                 "must be empty")


def described(where: str, label: str, field: Any, optional: tuple[str, ...] = ()) -> None:
    """Stop unless a context entry or the input is exactly {key, description}, both non-empty strings (and the
    `optional` parts: a context entry may say `required: false`)."""
    if not isinstance(field, dict):
        sys.exit(f"{where}: {label} must be {{key, description}}")
    for part in ("key", "description"):
        if part not in field:
            sys.exit(f"{where}: {label} is missing {part}")
        if not isinstance(field[part], str) or not field[part]:
            sys.exit(f"{where}: {label}.{part} must be a non-empty string")
    if "code_name" in field and (not isinstance(field["code_name"], str) or not field["code_name"]):
        sys.exit(f"{where}: {label}.code_name must be a non-empty string")
    unknown = [part for part in field if part not in ("key", "description", *optional)]
    if unknown:
        sys.exit(f"{where}: {label} has unknown fields {unknown} (only key, description"
                 f"{''.join(', ' + part for part in optional)})")


def adapter_folder(name: str, path: str, adapters: "HubAdapters | FolderAdapters") -> None:
    """Stop unless the folder holds every adapter file and a README.md (files directly in it, not in sub-folders)."""
    inside = adapters.files(path)
    if not inside:
        sys.exit(f"{name}: {adapters.label} has no folder {path}")
    missing = [file for file in (*ADAPTER_FILES, "README.md") if file not in inside]
    if missing:
        sys.exit(f"{name}: {adapters.where(path)} lacks {', '.join(missing)}, so it is not an adapter folder "
                 f"(it holds {', '.join(sorted(inside))})")


def base_of(name: str, config: dict[str, Any], listing: Listing) -> tuple[str, str]:
    """The base the adapter was trained on: the one in catalogue/bases.yaml whose model.safetensors has the SHA-256
    the adapter's decision_config.json records for its base checkpoint. A base on Hugging Face (repo, revision) is
    compared by the model_sha256 bases.yaml records for it when it records one (required for a base kept in a local
    folder, path), else by the LFS SHA-256 of the repo's model.safetensors at that revision."""
    try:
        sha = config["adapter"]["base_checkpoint"]["weights_sha256"]["model.safetensors"]
    except KeyError:
        sys.exit(f"{name}: decision_config.json has no adapter.base_checkpoint.weights_sha256.model.safetensors, so "
                 "its base cannot be identified")
    bases = yaml.safe_load(BASES.read_text(encoding="utf-8"))
    found = []
    for base_id, versions in bases.items():
        for version, where in versions.items():
            if "path" in where and "model_sha256" not in where:
                sys.exit(f"{name}: base {base_id} {version} is the local folder {where['path']}, but bases.yaml "
                         "records no model_sha256 to compare")
            if "model_sha256" in where:
                recorded = where["model_sha256"]
            else:
                files = listing(where["repo"], "model", where["revision"])
                if "model.safetensors" not in files:
                    sys.exit(f"{name}: base {base_id} {version} ({where['repo']}@{where['revision']}) has no "
                             "model.safetensors to compare")
                recorded = files["model.safetensors"]
            if recorded == sha:
                found.append((base_id, version))
    if len(found) != 1:
        sys.exit(f"{name}: its base weights (model.safetensors SHA-256 {sha}) match no base in catalogue/bases.yaml"
                 if not found else f"{name}: its base weights match several bases: {found}")
    return found[0]


def readme_heading(name: str, readme: str) -> tuple[str, str]:
    """The README's title (its first '# ' line) and its first paragraph after it, for JeffHub."""
    lines = readme.splitlines()
    titles = [n for n, line in enumerate(lines) if line.startswith("# ")]
    if not titles:
        sys.exit(f"{name}: README.md has no '# ' title line")
    paragraph: list[str] = []
    for line in lines[titles[0] + 1:]:
        if line.strip():
            paragraph.append(line.strip())
        elif paragraph:
            break
    if not paragraph:
        sys.exit(f"{name}: README.md has no paragraph after its title")
    return lines[titles[0]][2:].strip(), " ".join(paragraph)


def own_request(spec: dict[str, Any], key: str) -> dict[str, Any]:
    """The context and input of function `key`: its own when the import file gives them, else the adapter's."""
    body = spec["functions"][key]
    return body if "input" in body else spec


def check_states(name: str, spec: dict[str, Any], rows: dict[str, list[dict]], notes: list[str]) -> None:
    """Check the state of every row (`rows`: each function's test and calibration rows) against its function's
    context and input (check_state): the rows of the functions that take the adapter's together, as one set, then
    each function that declares its own, alone."""
    shared = [row for key, mine in rows.items() if "input" not in spec["functions"][key] for row in mine]
    if shared:
        check_state(name, spec, shared, notes, "")
    for key, mine in rows.items():
        if "input" in spec["functions"][key]:
            check_state(f"{name}.{key}", spec["functions"][key], mine, notes, f"{key}: ")


def check_state(name: str, owner: dict[str, Any], rows: list[dict], notes: list[str], prefix: str) -> None:
    """With input.form text, every row's state is a plain string. With fields, every row's state keys are `owner`'s
    (the import file's, or a function's own) context and input, in this order with the input last; a context field
    marked `required: false` may be absent. Rows that differ in any other way stop it."""
    if owner["input"]["form"] == "text":
        named = [row["id"] for row in rows if not isinstance(row["state"], str)]
        if named:
            sys.exit(f"{name}: input.form is text, but {len(named)} rows (e.g. {named[0]}) have a state that is not "
                     "a plain string")
        return
    plain = [row["id"] for row in rows if not isinstance(row["state"], dict)]
    if plain:
        sys.exit(f"{name}: {len(plain)} rows (e.g. {plain[0]}) have a plain-text state, not named fields")
    shapes = Counter(tuple(row["state"]) for row in rows)
    optional = [field["key"] for field in owner["context"] if not field.get("required", True)]
    expected = [field["key"] for field in owner["context"]] + [owner["input"]["key"]]
    if not optional:
        if len(shapes) != 1:
            sys.exit(f"{name}: the rows mix state shapes: " + "; ".join(f"{list(shape)} in {count} rows"
                                                                       for shape, count in shapes.most_common()))
        shape = list(next(iter(shapes)))
        if shape != expected:
            sys.exit(f"{name}: the rows' state keys {shape} are not the import file's context and input {expected} "
                     "(in this order, the input last)")
        return
    for shape, count in shapes.items():
        if [key for key in expected if key in shape or key not in optional] != list(shape):
            sys.exit(f"{name}: {count} rows have the state keys {list(shape)}, which are not the import file's "
                     f"context and input {expected} in this order (the input last) with only the optional "
                     f"{optional} left out")
    for key in optional:
        absent = sum(count for shape, count in shapes.items() if key not in shape)
        if absent == 0:
            sys.exit(f"{name}: the import file marks context {key} required: false, but every row has it")
        notes.append(f"{prefix}context {key}: absent from {absent} of the {len(rows)} test and calibration rows, so "
                     "it is not required.")


def rows_by_function(name: str, source: str, spec: dict[str, Any], types: dict[str, str], rows: list[dict],
                     split: str) -> dict[str, list[dict]]:
    """Each function's rows of `split`, mapped by the import file's function_by (jeff.manifest's one mapping rule);
    every function needs rows of both splits."""
    mode = spec["function_by"]
    if mode == "id_suffix":
        named = {re.split(r"[-:]", row["id"])[-1] for row in rows}
        extra = sorted(named - set(types))
        if extra:
            sys.exit(f"{name}: the {split} rows have functions {extra} (the end of their ids) that are not in "
                     f"{source}")
    if mode == "question_type":
        extra = sorted({row["question"]["type"] for row in rows} - set(types.values()))
        if extra:
            sys.exit(f"{name}: the {split} rows ask {extra} questions, and no function in {source} has that type")
    questions = {key: {"type": kind} for key, kind in types.items()}
    shapes = {key: request_shape_of(name, spec, key) for key in types}
    found: dict[str, list[dict]] = {key: [] for key in types}
    for row in rows:
        try:
            key = function_of(name, row, questions, mode, shapes, spec.get("kinds"), spec.get("id_patterns"))
        except ManifestError as error:
            sys.exit(f"{name}: {error}")
        found[key].append(row)
    for key, mine in found.items():
        if not mine:
            sys.exit(f"{name}.{key}: no {split} rows")
    return found


def request_shape_of(name: str, spec: dict[str, Any], key: str) -> StateShape:
    """The request shape of function `key` (its own context and input, or the import file's), by jeff.manifest's one
    rule (request_fields, then request_shape): the state_shape criterion compares rows with it."""
    owner = own_request(spec, key)
    try:
        return request_shape(*request_fields(name, manifest_context(owner["context"]), manifest_input(owner["input"])))
    except ManifestError as error:
        sys.exit(f"{name}: {error}")


def manifest_context(context: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """An import file's context list as the manifest writes it (each field required unless it says otherwise)."""
    return [{"key": field["key"], **({"code_name": field["code_name"]} if "code_name" in field else {}),
             "description": field["description"], "required": field.get("required", True)} for field in context]


def manifest_input(input: dict[str, Any]) -> dict[str, Any]:
    return {"key": input["key"], **({"code_name": input["code_name"]} if "code_name" in input else {}),
            "description": input["description"], "form": input["form"]}


def same_criteria(name: str, key: str, rows: list[dict]) -> Any:
    texts = {json.dumps(row["question"].get("criteria"), sort_keys=False) for row in rows}
    if len(texts) != 1:
        sys.exit(f"{name}.{key}: its test rows use {len(texts)} different criteria; owner decides")
    return json.loads(texts.pop())


def labels_of(name: str, key: str, kind: str, rows: list[dict], max_options: int, notes: list[str],
              interface_ordered: bool) -> dict:
    """The labels a function's test rows show, by the rules of import_jeffhub (choice_labels), with two additions
    for adapters that have no example.json and no jeffhub manifest: fixed labels are listed in the one order every row
    offers them in, or by key when training shuffled them, and labels that look like a subset of one set are caller
    labels when that set is larger than the base allows. `interface_ordered`: the interface fixes the labels' order
    (is_supported, equivalent; interface_order says so), so no note on the rows' order is written here."""
    if kind == "score":
        return {"source": "fixed", "levels": same_criteria(name, key, rows)}
    if kind == "noul":
        criteria = same_criteria(name, key, rows)
        return {"source": "fixed", **({"fixed": criteria} if criteria else {})}
    criteria = [list(row["question"]["criteria"].items()) for row in rows]
    same_set = all(sorted(map(repr, item)) == sorted(map(repr, criteria[0])) for item in criteria)
    same_keys = all([k for k, _ in item] == [k for k, _ in criteria[0]] for item in criteria)
    texts: dict[str, set] = {}
    for item in criteria:
        for label, text in item:
            texts.setdefault(label, set()).add(text)
    always = sorted(label for label in texts if all(label in dict(item) for item in criteria))
    if not same_set and not same_keys and always and all(len(found) == 1 for found in texts.values()):
        if len(texts) <= max_options:
            sys.exit(f"{name}.{key}: every test row offers a subset of the same {len(texts)} labels, always "
                     f"including {always}, so the labels look like 'subset', but nothing documents their order; "
                     "owner decides")
        most = max(len(item) - len(always) for item in criteria)
        notes.append(f"{key}: the test rows offer {len(texts)} different labels in all, more than the base's "
                     f"{max_options} options, so they cannot be listed: the labels are 'caller' (the caller passes "
                     f"its own), with {always} first, which every row offers (training put them at varying "
                     f"positions); max {most} is the largest number of caller labels in the test rows.")
        return {"source": "caller", "fixed_first": {label: next(iter(texts[label])) for label in always},
                "caller_keys": None, "max": most}
    # fixed labels: in the one order every row offers them in when training used one (a prompt in another order is
    # one the adapter never saw), else by key (training shuffled them)
    labels = choice_labels(name, key, rows, dict(criteria[0]) if same_keys else dict(sorted(criteria[0])), notes, "")
    if labels["source"] == "fixed" and same_keys and not interface_ordered \
            and list(labels["fixed"]) != sorted(labels["fixed"]):
        notes.append(f"{key}: every test row offers the labels in the one order {list(labels['fixed'])}, so they are "
                     "listed in that order.")
    if labels["source"] == "fixed" and not same_keys:
        notes.append(f"{key}: every test row has the same {len(criteria[0])} labels in varying orders (training "
                     "shuffled them), so they are listed by key.")
    if labels["source"] == "caller":
        notes.append(f"{key}: max {labels['max']} is the largest number of caller labels in the test rows (nothing "
                     "documents a training limit).")
    return labels


def interface_order(name: str, key: str, values: tuple[str, ...] | None, labels: dict, notes: list[str]) -> dict:
    """An interface with fixed values (is_supported, equivalent) needs exactly those labels in its order, not by key.
    check_described then compares that order with every row's: rows trained in another order stop the import, unless
    the import file says training shuffled them (option_order: shuffled)."""
    if values is None:
        return labels
    if labels["source"] != "fixed" or sorted(labels.get("fixed", {})) != sorted(values):
        sys.exit(f"{name}.{key}: its test rows' labels {labels} are not the interface's values {list(values)}")
    notes.append(f"{key}: labels in the order of the interface's values {list(values)}.")
    return {"source": "fixed", "fixed": {value: labels["fixed"][value] for value in values}, "subset": False}


def instructions_of(name: str, key: str, rows: list[dict], notes: list[str]) -> str:
    """The function's instructions. Rows whose source marks how their instructions were written
    (`source.instructions`: canonical, or a rewording, or none) give the one text of the rows marked canonical, word
    for word; the others ask the same question in other words, or with none. Rows with no mark among marked ones (an
    adapter trained on older data with newer data added) must use exactly that canonical text, or the import stops.
    Where no row is marked, every row must use one text."""
    marks = Counter(str((row.get("source") or {}).get("instructions")) for row in rows
                    if "instructions" in (row.get("source") or {}))
    if not marks:
        return instructions_from_rows(name, key, rows)
    canonical = [row for row in rows if (row.get("source") or {}).get("instructions") == "canonical"]
    if not canonical:
        sys.exit(f"{name}.{key}: its test rows mark their instructions {sorted(marks)}, none canonical; owner decides")
    text = instructions_from_rows(name, key, canonical)
    unmarked = [row for row in rows if "instructions" not in (row.get("source") or {})]
    if unmarked:
        differ = [row["id"] for row in unmarked if row["question"].get("instructions") != text]
        if differ:
            sys.exit(f"{name}.{key}: {sum(marks.values())} of its {len(rows)} test rows mark how their instructions "
                     f"were written (source.instructions) and the others do not, and {len(differ)} of those (e.g. "
                     f"{differ[0]}) do not use the canonical text; owner decides")
        notes.append(f"{key}: {len(unmarked)} test rows do not mark their instructions (source.instructions); each "
                     "uses the canonical text word for word, so they are rebuilt exactly.")
    others = len(rows) - len(canonical) - len(unmarked)
    if others:
        notes.append(f"{key}: instructions are the one text of the {len(canonical)} test rows marked canonical "
                     f"(source.instructions); the other {others} ask the same question reworded or with no "
                     "instructions (as trained), and are rebuilt with the canonical text in template mode.")
    return text


def caller_max(name: str, key: str, labels: dict, calibration: list[dict], notes: list[str]) -> None:
    """Caller labels: `max` (the most caller labels the adapter was trained on) is the largest number any test or
    calibration row offers, since calibration rows may offer more than every test row does."""
    if labels["source"] != "caller":
        return
    most = max(len([label for label in row["question"]["criteria"] if label not in labels["fixed_first"]])
               for row in calibration)
    if most > labels["max"]:
        notes.append(f"{key}: the calibration rows offer up to {most} caller labels, more than the test rows' "
                     f"{labels['max']}, so max is {most}.")
        labels["max"] = most


def check_no_shared_rows(name: str, test: list[dict], calibration: list[dict]) -> None:
    """Stop when a calibration row is also a test row: the same id, or the same state and question."""
    def same(row: dict) -> str:
        return json.dumps([row["state"], row["question"]], sort_keys=True)
    ids = sorted({row["id"] for row in test} & {row["id"] for row in calibration})
    if ids:
        sys.exit(f"{name}: {len(ids)} row ids are in both the test and the calibration set (e.g. {ids[0]})")
    asked = {same(row) for row in test}
    shared = [row["id"] for row in calibration if same(row) in asked]
    if shared:
        sys.exit(f"{name}: {len(shared)} calibration rows ask a test row's state and question (e.g. {shared[0]})")


def check_described(name: str, manifest: dict[str, Any], source: str, splits: dict[str, list[dict]],
                    shuffled: set[str]) -> None:
    """Stop unless the manifest describes every test and calibration row: each maps to a function and is rebuilt as
    the call a user would make (the template mode jeff calibrate and the gate use), none skipped, with the row's exact
    state (the same keys in the same order, and values), the same options (each key and text) in the order the row
    was trained with, and the row's own instructions unless the row marks them as a rewording or as none
    (source.instructions other than canonical). `shuffled`: the functions whose import file says option_order:
    shuffled (training offered their options in varying orders); only their rows may be rebuilt in another order,
    and at least one of their rows must be, or the import file's word is wrong."""
    parsed = manifest_from_dict(manifest, Path(source))
    reordered: set[str] = set()
    for split, rows in splits.items():
        try:
            _, skipped = template_rows(parsed, rows)
        except JeffError as error:
            sys.exit(f"{name}: a {split} row cannot be rebuilt as a call through the manifest: {error}")
        if skipped:
            first = next(row for row in rows if template_rows(parsed, [row])[1])
            sys.exit(f"{name}: the manifest does not describe {sum(skipped.values())} of the {len(rows)} {split} "
                     f"rows: {skipped}; the first is {first['id']} ({next(iter(template_rows(parsed, [first])[1]))})")
        for raw, (key, rebuilt) in zip(rows, rebuilt_rows(parsed, rows), strict=True):
            trained, built = raw["question"].get("criteria"), rebuilt["question"].get("criteria")
            if (dict(trained) if isinstance(trained, dict) else trained) != built:
                sys.exit(f"{name}: {split} row {raw['id']} is rebuilt with options {built}, but it was trained with "
                         f"{trained} (each option key and text must be the same)")
            if isinstance(trained, dict) and list(trained) != list(built):
                if key not in shuffled:
                    sys.exit(f"{name}: {split} row {raw['id']} is rebuilt with the options in the order {list(built)}, "
                             f"but it was trained in the order {list(trained)}; the manifest must keep the order "
                             f"training used (if training shuffled the options of {key} from row to row, the import "
                             "file says so with option_order: shuffled)")
                reordered.add(key)
            if json.dumps(rebuilt["state"]) != json.dumps(raw["state"]):
                sys.exit(f"{name}: {split} row {raw['id']} is rebuilt with another state than it was trained with "
                         f"(keys {list_keys(rebuilt['state'])}, trained {list_keys(raw['state'])}; the keys, their "
                         "order and the values must be the same)")
            mark = (raw.get("source") or {}).get("instructions", "canonical")
            if mark == "canonical" and rebuilt["question"]["instructions"] != raw["question"].get("instructions"):
                sys.exit(f"{name}: {split} row {raw['id']} is rebuilt with the instructions "
                         f"{rebuilt['question']['instructions']!r}, but it was trained with "
                         f"{raw['question'].get('instructions')!r}")
    for key in sorted(shuffled - reordered):
        sys.exit(f"{name}.{key}: the import file says option_order: shuffled, but every test and calibration row "
                 "offers the options in the manifest's order; leave option_order out, so that order is checked")


def list_keys(state: Any) -> Any:
    return list(state) if isinstance(state, dict) else "a plain string"


def rebuilt_rows(manifest: Manifest, rows: list[dict]) -> list[tuple[str, dict[str, Any]]]:
    """For each row, its function key and the state and question Jeff sends, rebuilt through the manifest as the call
    a user would make."""
    calls, _ = template_rows(manifest, rows)
    out = []
    for call in (row.call for row in calls):
        key = call.function.rsplit(".", 1)[1]
        function = manifest.functions[key]
        out.append((key, build_row(bind(manifest, function, call.context, call.labels, calibration=None), call.input)))
    return out


def declared_labels(name: str, key: str, declared: dict[str, Any], rows: list[dict], notes: list[str]) -> dict:
    """Fixed choice labels the import file lists in order (rows that do not settle their order: training shuffled
    them and some rows offer a subset): every key the test rows offer is listed, every listed key is offered by some
    test row with one text, and a row offering fewer than all needs subset true."""
    order = declared["fixed"]
    texts: dict[str, set[str]] = {}
    for row in rows:
        for label, text in row["question"]["criteria"].items():
            texts.setdefault(label, set()).add(text)
    if sorted(texts) != sorted(order):
        sys.exit(f"{name}.{key}: the import file lists the labels {order}, but the test rows offer {sorted(texts)}")
    several = sorted(label for label, found in texts.items() if len(found) != 1)
    if several:
        sys.exit(f"{name}.{key}: the test rows give the labels {several} several texts, so they are not fixed labels")
    orders = {tuple(row["question"]["criteria"]) for row in rows}
    if len(orders) == 1 and list(next(iter(orders))) != order:
        sys.exit(f"{name}.{key}: every test row offers the labels in the order {list(next(iter(orders)))}, so that is "
                 f"their order, not the import file's {order}")
    offered = Counter(len(row["question"]["criteria"]) for row in rows)
    partial = sum(count for size, count in offered.items() if size < len(order))
    if partial and not declared["subset"]:
        sys.exit(f"{name}.{key}: {partial} test rows offer only some of the labels {order}, so the import file must "
                 "say subset: true")
    notes.append(f"{key}: the labels in the order the import file gives ({', '.join(order)}); the test rows offer them "
                 f"in {len(orders)} different orders (training shuffled them)"
                 + (f", and {partial} of the {len(rows)} test rows offer only some of them, so the caller may offer "
                    "a subset (subset true), shown in this order." if declared["subset"] else "."))
    return {"source": "fixed", "fixed": {label: next(iter(texts[label])) for label in order},
            "subset": declared["subset"]}


def build(name: str, source: str, spec: dict[str, Any], fetch: Fetch, listing: Listing,
          adapters: "HubAdapters | FolderAdapters | None" = None) -> dict[str, Any]:
    path = spec["path"]
    folder = spec.get("adapter_path", path)
    revision = spec.get("data_revision", REVISION)
    adapters = adapters if adapters is not None else HubAdapters(fetch, listing)
    adapter_folder(name, folder, adapters)
    config = json.loads(adapters.read(folder, "decision_config.json"))
    title, summary = readme_heading(name, adapters.read(folder, "README.md"))
    base_id, base_version = base_of(name, config, listing)
    data_files = listing(DATA_REPO, "dataset", revision)
    splits = {}
    for split in ("test", "calibration"):
        file = f"{path}/{split}.jsonl"
        if file not in data_files:
            sys.exit(f"{name}: {DATA_REPO}@{revision} has no {file}")
        splits[split] = read_rows(fetch(DATA_REPO, file, "dataset", revision))
    notes: list[str] = []
    check_no_shared_rows(name, splits["test"], splits["calibration"])
    interfaces = contract.interfaces()
    for key, body in spec["functions"].items():
        if body["interface"] not in interfaces:
            sys.exit(f"{name}.{key}: interface {body['interface']!r} is not in contract/interfaces.yaml "
                     f"({', '.join(interfaces)})")
    types: dict[str, str] = {key: interfaces[body["interface"]].question_type
                             for key, body in spec["functions"].items()}
    test_rows = rows_by_function(name, source, spec, types, splits["test"], "test")
    calibration_rows = rows_by_function(name, source, spec, types, splits["calibration"], "calibration")
    check_states(name, spec, {key: test_rows[key] + calibration_rows[key] for key in types}, notes)
    functions = {}
    for key, body in spec["functions"].items():
        mine = test_rows[key]
        asked = sorted({row["question"]["type"] for row in mine})
        if asked != [types[key]]:
            sys.exit(f"{name}.{key}: interface {body['interface']} compiles to {types[key]}, but its rows ask "
                     f"{', '.join(asked)}")
        instructions = instructions_of(name, key, mine, notes)
        if "labels" in body:
            if types[key] != "choice":
                sys.exit(f"{name}.{key}: the import file lists labels, but only choice functions have fixed labels "
                         f"to list (it compiles to {types[key]})")
            labels = declared_labels(name, key, body["labels"], mine, notes)
        else:
            labels = labels_of(name, key, types[key], mine, config["max_options"], notes,
                               interfaces[body["interface"]].values is not None)
        functions[key] = {"interface": body["interface"], "type": types[key], "instructions": instructions,
                          "labels": interface_order(name, key, interfaces[body["interface"]].values, labels, notes),
                          "abstain_labels": [], "calibration": None}
        if "input" in body:
            functions[key]["context"] = manifest_context(body["context"])
            functions[key]["input"] = manifest_input(body["input"])
        caller_max(name, key, functions[key]["labels"], calibration_rows[key], notes)
    notes.append(f"base: {base_id} {base_version}, identified by the SHA-256 of its model.safetensors, which the "
                 "adapter's decision_config.json records for its base checkpoint.")
    notes.append(f"tests: the test set is the private {data_uri(path, 'test.jsonl', revision)}, so tests.published "
                 "is null; the calibration of each function is filled in later by `jeff calibrate`.")
    if revision != REVISION:
        notes.append(f"data: read at {DATA_REPO} revision {revision}, the data version the adapter was trained on "
                     f"(main holds a later version of {path}).")
    manifest = {
        "id": spec["id"], "version": spec["version"], "aliases": spec["aliases"], "licence": dict(LICENCE),
        "weights": adapters.weights(folder),
        "base": {"id": base_id, "version": base_version, "max_options": config["max_options"]},
        "context": manifest_context(spec["context"]), "input": manifest_input(spec["input"]),
        "functions": functions, "routes_to": [],
        "tests": {"published": None, "calibration": None, "internal": data_uri(path, "test.jsonl", revision),
                  "internal_calibration": data_uri(path, "calibration.jsonl", revision),
                  "function_by": spec["function_by"],
                  **{field: spec[field] for field in ("kinds", "id_patterns") if field in spec}},
        "hub": {"title": title, "summary": summary, "source": adapters.source(folder), "notes": notes},
        **({"access": spec["access"]} if "access" in spec else {}),
    }
    try:
        manifest_from_dict(manifest, Path(source))
    except JeffError as error:  # ManifestError, or ContractError for a value the manifest schema refuses
        sys.exit(f"{name}: the manifest is not valid: {error}")
    check_described(name, manifest, source, splits,
                    {key for key, body in spec["functions"].items() if "option_order" in body})
    return manifest


def main(argv: list[str] | None = None, fetch: Fetch = hf_fetch, listing: Listing = hf_listing,
         imports: Path = IMPORTS) -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("names", nargs="+", help="import files <imports>/<name>.yaml")
    parser.add_argument("--out", required=True, type=Path, help="folder to write <id>/manifest.yaml into")
    parser.add_argument("--imports", type=Path, help="the folder of import files (default: catalogue-imports/)")
    parser.add_argument("--adapters-root", type=Path,
                        help="read the adapter folders from this folder on disk instead of teamsuzie/jeff-adapters "
                             "(each import file's adapter_path, else its path, under it); the weights are then local")
    parser.add_argument("--adapters-listing", type=Path,
                        help="with --adapters-root: a file naming every file under the real adapters root, one path "
                             "per line relative to it, when --adapters-root holds only copies of the small files")
    args = parser.parse_args(argv)
    if args.adapters_listing is not None and args.adapters_root is None:
        parser.error("--adapters-listing needs --adapters-root")
    adapters = FolderAdapters(args.adapters_root, args.adapters_listing) if args.adapters_root is not None else None
    for name in args.names:
        spec = load_import(name, args.imports if args.imports is not None else imports)
        manifest = build(name, f"{name}.yaml", spec, fetch, listing, adapters)
        target = args.out / manifest["id"] / "manifest.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=120), encoding="utf-8")
        print(f"{name} -> {target}")


if __name__ == "__main__":
    main()
