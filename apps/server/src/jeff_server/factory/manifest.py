"""The manifest a factory adapter is published with, so its questions are callable as functions through Jeff,
/v1/decide and MCP, not only through /v1/systemone.

- ID jeff.factory.<spec name>, alias <spec name>, version 1.0.0; licence proprietary, the owner's; weights local
  (the folder holding the manifest, which an engine finds with --adapters-dir).
- context: the family's fields, all required; input: the changing field. A run whose requests are plain text (a
  family with no fields) is refused: the factory itself only builds manifests with named fields (input form fields),
  and with those Jeff would send {"<field>": text}, a different prompt from the plain text the adapter was trained on.
- One function per question, keyed by the question ID lower-cased with every character other than a-z and 0-9 as
  "_". The interface follows the question type (choice: classify, noul: detect, score: rate) and the labels are the
  question's options, fixed, worded exactly as the adapter was trained on them (spec.question_payload): a choice's
  option descriptions, a yes/no question's two answers, a score's "key: description" levels.
- A question asked only after another answer (`requires`) is published as a normal function; its condition is kept
  as text in hub.notes, and the caller decides when to ask it.
- calibration: per function, fitted on the run's own calibration split (jeff.calibrate.block) at a target precision
  of 0.95, from predictions of the requests as Jeff sends them (served_question). A function no threshold reaches
  stops the publish (CalibrationError).
- tests: the run's test and calibration splits, copied into the adapter folder (published_rows) with each row's id
  ending in its function key, so `jeff test` and `jeff calibrate` map every row to its function; nothing public."""

import re
from collections import defaultdict
from pathlib import Path
from typing import Any

from jeff.calibrate import block
from jeff.errors import CalibrationError, ManifestError
from jeff.manifest import function_key_of_test_row, manifest_from_dict

from jeff_server.factory import spec as spec_module

VERSION = "1.0.0"
TARGET_PRECISION = 0.95
INTERFACES = {"choice": "classify", "noul": "detect", "score": "rate"}
FUNCTION_KEY = re.compile(r"^[a-z][a-z0-9_]*$")
# The files the published adapter folder holds the run's splits in; the manifest's tests point at them.
TEST_FILE, CALIBRATION_FILE = "test.jsonl", "calibration.jsonl"


def adapter_id(spec: dict[str, Any]) -> str:
    return f"jeff.factory.{spec['name']}"


def function_keys(spec: dict[str, Any]) -> dict[str, str]:
    """Question ID -> function key. ManifestError naming the question for a key that does not start with a letter,
    or two questions whose keys are the same."""
    keys: dict[str, str] = {}
    owner: dict[str, str] = {}
    for question in spec["questions"]:
        qid = question["id"]
        key = "".join(character if "a" <= character <= "z" or "0" <= character <= "9" else "_"
                      for character in qid.lower())
        if not FUNCTION_KEY.fullmatch(key):
            raise ManifestError(f"Question {qid!r} becomes the function key {key!r}, which must start with a letter "
                                "(a-z); rename the question")
        if key in owner:
            raise ManifestError(f"Questions {owner[key]!r} and {qid!r} both become the function key {key!r} "
                                "(lower case, other characters as '_'); rename one of them")
        owner[key], keys[qid] = qid, key
    return keys


def check_named_fields(spec: dict[str, Any], rows: list[dict[str, Any]]) -> None:
    """ManifestError when the adapter was trained on plain-text requests: its family has no fields, or a row's
    state is not an object of named fields."""
    if not spec["family"]["fields"] or any(not isinstance(row["state"], dict) for row in rows):
        raise ManifestError(
            f"{spec['name']} cannot be published as functions: the adapter was trained on a plain-text request "
            "(its family has no named fields), but the factory only publishes adapters trained on named fields, so "
            f"Jeff would send {{\"{spec['live_field']['name']}\": <text>}}, a different prompt from the one the "
            "adapter was trained on. Give the family at least one field and train again")


def served_question(question: dict[str, Any]) -> dict[str, Any]:
    """The question exactly as Jeff sends it for the question's function (jeff.binding.build_row on the manifest
    built here): the canonical instruction and the options in the spec's order."""
    return spec_module.question_payload(question)


def labels(question: dict[str, Any]) -> dict[str, Any]:
    payload = served_question(question)
    if question["type"] == "score":
        return {"source": "fixed", "levels": payload["criteria"]}
    if question["type"] == "choice":
        # a family's choice question is always asked with all its options
        return {"source": "fixed", "fixed": payload["criteria"], "subset": False}
    return {"source": "fixed", "fixed": payload["criteria"]}


def condition(question: dict[str, Any], keys: dict[str, str]) -> str:
    parts = [f"{keys[item['question']]} is {item['equals']}" for item in question["requires"]]
    return (f"{keys[question['id']]} is asked only when " + " and ".join(parts)
            + (f" ({question['applies_when']})" if question["applies_when"] else "")
            + "; the caller decides when to ask it.")


def calibrations(spec: dict[str, Any], rows: list[dict[str, Any]], predictions: list[dict[str, Any]],
                 data: str) -> dict[str, dict[str, Any]]:
    """Question ID -> the function's calibration block, from the calibration rows and their predictions
    (jeff_server.factory.evaluation.evaluate_logits, in row order)."""
    if len(rows) != len(predictions):
        raise CalibrationError(f"{len(predictions)} predictions for {len(rows)} calibration rows")
    pairs: dict[str, list[tuple[float, bool]]] = defaultdict(list)
    for row, prediction in zip(rows, predictions, strict=True):
        pairs[row["source"]["question_id"]].append((prediction["confidence"], prediction["correct"]))
    fitted = {}
    for question in spec["questions"]:
        qid = question["id"]
        if qid not in pairs:
            raise CalibrationError(f"Question {qid}: the calibration split has no rows for it")
        try:
            # the factory reads the calibration rows it wrote itself, never through JEFF_DATA_MIRROR
            fitted[qid] = block(pairs[qid], TARGET_PRECISION, data, data_mirror=None)
        except CalibrationError as error:
            raise CalibrationError(f"Question {qid}: {error}") from error
    return fitted


def function_by(keys: dict[str, str]) -> str:
    return "only" if len(keys) == 1 else "id_suffix"


def published_rows(spec: dict[str, Any], rows: list[dict[str, Any]], where: str) -> list[dict[str, Any]]:
    """A split's rows as the published folder holds them: each id ending ":<function key>" in place of
    ":<question id>", so jeff.manifest.function_key_of_test_row (what `jeff test` uses) maps it. ManifestError
    naming the question when a row's id does not end in its question ID, or does not map to its function."""
    keys = function_keys(spec)
    types = {keys[question["id"]]: question["type"] for question in spec["questions"]}
    out = []
    for row in rows:
        qid = row["source"]["question_id"]
        if not row["id"].endswith(f":{qid}"):
            raise ManifestError(f"Question {qid}: row {row['id']} does not end in ':{qid}'")
        published = {**row, "id": row["id"][:-len(qid)] + keys[qid]}
        # function_by is only or id_suffix, so no kinds, request shapes or id patterns are needed (None)
        mapped = function_key_of_test_row(where, function_by(keys), types, published, None, None, None)
        if mapped != keys[qid]:
            raise ManifestError(f"Question {qid}: row {published['id']} maps to function {mapped}, not {keys[qid]}")
        out.append(published)
    return out


def build(spec: dict[str, Any], metrics: dict[str, Any], calibration_rows: list[dict[str, Any]],
          predictions: list[dict[str, Any]], base: dict[str, Any], *, test_rows: list[dict[str, Any]],
          folder: Path) -> dict[str, Any]:
    """The manifest, validated (jeff.manifest.manifest_from_dict) and returned as a dictionary to write.
    predictions: one per calibration row, of the row as Jeff sends it (served_question). base: {"id", "version"} of
    the base the engines serve, and "max_options" from the adapter's decision_config.json. folder: where the adapter
    is published; its TEST_FILE and CALIBRATION_FILE hold the published_rows of the run's splits."""
    check_named_fields(spec, [*test_rows, *calibration_rows])
    keys = function_keys(spec)
    calibration_path = str(folder / CALIBRATION_FILE)
    fitted = calibrations(spec, calibration_rows, predictions, calibration_path)
    functions = {keys[question["id"]]: {
        "interface": INTERFACES[question["type"]], "type": question["type"], "instructions": question["instruction"],
        "labels": labels(question), "abstain_labels": [], "calibration": fitted[question["id"]],
    } for question in spec["questions"]}
    raw = {
        "id": adapter_id(spec), "version": VERSION, "aliases": [spec["name"]],
        "licence": {"kind": "proprietary", "adapter": "owner"},
        "weights": {"local": {}},
        "base": {"id": base["id"], "version": base["version"], "max_options": base["max_options"]},
        "context": [{"key": field["name"], "description": field["description"], "required": True}
                    for field in spec["family"]["fields"]],
        "input": {"key": spec["live_field"]["name"], "description": spec["live_field"]["description"],
                  "form": "fields"},
        "functions": functions,
        "routes_to": [],
        "tests": {"published": None, "calibration": None, "internal": str(folder / TEST_FILE),
                  "internal_calibration": calibration_path, "function_by": function_by(keys)},
        "hub": {"name": spec["name"], "summary": spec["summary"], "kind": "factory",
                "notes": [condition(question, keys) for question in spec["questions"] if question.get("requires")],
                "metrics": {name: {"label": result["label"], "rows": result["rows"], "accuracy": result["accuracy"],
                                   "interval": result["interval"]}
                            for name, result in metrics.items() if name != "calibration"}},
    }
    manifest_from_dict(raw, Path(f"<manifest of {raw['id']}>"))
    return raw
