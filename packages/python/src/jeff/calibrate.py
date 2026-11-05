"""Per-function calibration: the confidence threshold at which measured precision reaches a target (spec Section 7)."""

import json
import os
import tempfile
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from jeff.client import Jeff
from jeff.errors import CalibrationError
from jeff.manifest import Calibration, FunctionSpec, Manifest, load_manifest, manifest_from_dict


def fit(rows: list[tuple[float, bool]], target_precision: float) -> tuple[float, float, float]:
    if not rows:
        raise CalibrationError("No calibration rows")
    ordered = sorted(rows, key=lambda item: -item[0])
    best: tuple[float, float, float] | None = None
    best_precision = 0.0
    right = 0
    for index, (confidence, ok) in enumerate(ordered):
        right += ok
        if index + 1 < len(ordered) and ordered[index + 1][0] == confidence:
            continue  # ties at one confidence are taken together
        precision = right / (index + 1)
        best_precision = max(best_precision, precision)
        if precision >= target_precision:
            best = (confidence, precision, (index + 1) / len(ordered))
    if best is None:
        raise CalibrationError(f"No threshold reaches precision {target_precision}; the best precision is "
                               f"{best_precision:.3f}")
    return best


def block(rows: list[tuple[float, bool]], target_precision: float, data: str, *,
          data_mirror: dict[str, str] | None) -> dict[str, Any]:
    """A manifest function's `calibration:` block, fitted on `rows` ((confidence, correct) pairs, one per
    calibration row of that function) read from `data`. `data_mirror` is jeff.evaluate.mirror_read(data): when the
    rows were read from a JEFF_DATA_MIRROR copy, the block records that file and its SHA-256 beside `data`; None
    adds nothing. CalibrationError when no threshold reaches the target."""
    threshold, precision, coverage = fit(rows, target_precision)
    fitted = {"data": data, "rows": len(rows), "target_precision": target_precision, "threshold": round(threshold, 6),
              "precision_at_threshold": round(precision, 6), "coverage_at_threshold": round(coverage, 6)}
    return fitted if data_mirror is None else {"data": data, "data_mirror": dict(data_mirror),
                                               **{key: value for key, value in fitted.items() if key != "data"}}


def format_block(rows: list[tuple[float, bool]], target_precision: float, data: str, excluded: str, *,
                 data_mirror: dict[str, str] | None) -> dict[str, Any]:
    """A function's calibration_by_format entry for one INT4 base: block() fitted on that base's (confidence,
    correct) pairs, plus `excluded`, which rows were left out of the fit and why."""
    fitted = block(rows, target_precision, data, data_mirror=data_mirror)
    mirror = {} if data_mirror is None else {"data_mirror": fitted["data_mirror"]}
    return {"data": fitted["data"], **mirror, "rows": fitted["rows"], "excluded": excluded,
            **{key: fitted[key] for key in ("target_precision", "threshold", "precision_at_threshold",
                                            "coverage_at_threshold")}}


@dataclass(frozen=True)
class Int4Base:
    """What `jeff calibrate --base` reads from an INT4 base folder (decision_config.json's quantization and the
    record it names): its format_id, and the calibration rows the quantiser saw, by adapter (id@version -> row ids).
    training_rows: the adapters (id@version) whose quantiser rows were drawn from training rows (the record's
    calibration.training_rows: {"path", "sha256"} of each training file), which the quantiser checked are not in the
    calibration set; calibration_sha256: the SHA-256 of each such adapter's calibration set as the quantiser checked
    it (calibration.not_in_calibration_sets)."""
    folder: Path
    format_id: str
    quantiser_rows: dict[str, list[str]]
    training_rows: dict[str, dict[str, str]] = field(default_factory=dict)
    calibration_sha256: dict[str, str] = field(default_factory=dict)


def read_int4_base(folder: Path) -> Int4Base:
    """The INT4 base at `folder` (written by `jeff runtime quantize`). CalibrationError for a folder that is not an
    INT4 base: a bfloat16 base (no quantization in its decision_config.json) is calibrated without --base."""
    config_path = folder / "decision_config.json"
    if not config_path.is_file():
        raise CalibrationError(f"--base {folder} has no decision_config.json; give the folder of an INT4 base, as "
                               "jeff runtime quantize writes it")
    config = _json_object(config_path)
    if "quantization" not in config:
        raise CalibrationError(f"--base {folder} is a bfloat16 base (its decision_config.json has no quantization); "
                               "its thresholds are the functions' calibration blocks, which jeff calibrate writes "
                               "without --base")
    quantization = config["quantization"]
    missing = [key for key in ("format_id", "record") if not isinstance(quantization, dict) or key not in quantization]
    if missing:
        raise CalibrationError(f"--base {folder}: decision_config.json's quantization has no {', '.join(missing)}; "
                               "it is not an INT4 base as jeff runtime quantize writes it")
    record_path = folder / quantization["record"]
    if not record_path.is_file():
        raise CalibrationError(f"--base {folder}: the record its decision_config.json names, {record_path}, is missing")
    record = _json_object(record_path)
    calibration = record.get("calibration")
    rows = calibration.get("rows") if isinstance(calibration, dict) else None
    if not isinstance(rows, dict) or not all(isinstance(ids, list) for ids in rows.values()):
        raise CalibrationError(f"--base {folder}: {record_path} has no calibration.rows mapping each adapter "
                               "(id@version) to the list of row ids the quantiser saw")
    training, sha256s = _training_record(folder, record_path, calibration)
    return Int4Base(folder, quantization["format_id"], {key: [str(row_id) for row_id in ids] for key, ids in
                                                        rows.items()}, training, sha256s)


def _sha256_text(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _training_record(folder: Path, record_path: Path, calibration: dict[str, Any]
                     ) -> tuple[dict[str, dict[str, str]], dict[str, str]]:
    """The record's training_rows and the calibration-set SHA-256 of each of its adapters. CalibrationError naming
    what is wrong for a contradictory record: training_rows present without the training-rows rule (or the rule
    without training_rows), an adapter in training_rows that calibration.rows does not list, a malformed entry, or a
    training adapter with no calibration-set SHA-256 in not_in_calibration_sets."""
    from jeff_runtime.engine.quantize import TRAINING_RULE

    where = f"--base {folder}: {record_path}"
    present = "training_rows" in calibration
    by_rule = calibration.get("rule") == TRAINING_RULE
    if present and not by_rule:
        raise CalibrationError(f"{where} has calibration.training_rows, but its calibration.rule is not the "
                               "training-rows rule this jeff writes; the record contradicts itself")
    if by_rule and not present:
        raise CalibrationError(f"{where} has the training-rows rule as calibration.rule, but no "
                               "calibration.training_rows naming each adapter's training file")
    if not present:
        return {}, {}
    training = calibration["training_rows"]
    if not isinstance(training, dict):
        raise CalibrationError(f"{where}'s calibration.training_rows is not a mapping of adapter (id@version) to its "
                               "training rows file")
    extra = sorted(set(training) - set(calibration["rows"]))
    if extra:
        raise CalibrationError(f"{where}'s calibration.training_rows names {', '.join(extra)}, which calibration.rows "
                               "does not list")
    for key, entry in training.items():
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str) or not _sha256_text(
                entry.get("sha256")):
            raise CalibrationError(f"{where}'s calibration.training_rows[{key}] is not a path and a SHA-256")
    sets = calibration.get("not_in_calibration_sets")
    if not isinstance(sets, dict):
        raise CalibrationError(f"{where} has calibration.training_rows but no calibration.not_in_calibration_sets "
                               "(the calibration sets the training rows were checked against)")
    sha256s: dict[str, str] = {}
    for key in training:
        entry = sets.get(key)
        if not isinstance(entry, dict) or not _sha256_text(entry.get("sha256")):
            raise CalibrationError(f"{where}'s calibration.not_in_calibration_sets has no SHA-256 for {key}")
        sha256s[key] = entry["sha256"]
    return {key: {"path": entry["path"], "sha256": entry["sha256"]} for key, entry in training.items()}, sha256s


def quantiser_rows_of(int4: Int4Base, manifest: Manifest, raws: list[dict[str, Any]], uri: str, *,
                      published: bool, path: Path) -> tuple[set[str], bool]:
    """The ids (as strings) of the adapter's calibration rows the quantiser saw (listed in the INT4 base's
    quantization.json), and whether the record names this adapter at all. `raws`: the calibration rows about to be
    fitted on, read from `uri` (the local file `path`); published: whether they are the adapter's published
    calibration set, which must then hold every listed row. CalibrationError when the record lists the adapter under
    another version, or when the published set and the record disagree.

    For an adapter whose quantiser rows came from its training rows (the record's training_rows), none of them may be
    in the rows fitted on: the published calibration set must be the very file the quantiser checked (its SHA-256 as
    recorded); other data (--data, --split-test) is checked here against the recorded training file (its SHA-256
    verified first), by id and by state."""
    key = f"{manifest.id}@{manifest.version}"
    others = [name for name in int4.quantiser_rows if name != key and name.rsplit("@", 1)[0] == manifest.id]
    if others:
        raise CalibrationError(f"{int4.folder}'s quantization.json records the quantiser's rows of "
                               f"{', '.join(others)}, and the catalogue's {manifest.id} is version "
                               f"{manifest.version}: the INT4 base was quantised on another version's rows; "
                               "calibrate the version it records, or quantise again")
    recorded = key in int4.quantiser_rows
    rows = set(int4.quantiser_rows[key]) if recorded else set()
    found = {str(raw["id"]) for raw in raws} & rows
    if key in int4.training_rows:
        _check_not_training_rows(int4, key, rows, raws, uri, published=published, path=path)
        return rows, recorded
    if published and found != rows:
        raise CalibrationError(f"{int4.folder}'s quantization.json lists {len(rows)} rows of {key} the quantiser "
                               f"saw, and {len(found)} of them are in its calibration set {uri}; the record and the "
                               "set disagree")
    return rows, recorded


def _file_sha256(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _check_not_training_rows(int4: Int4Base, key: str, ids: set[str], raws: list[dict[str, Any]], uri: str, *,
                             published: bool, path: Path) -> None:
    """CalibrationError unless the rows about to be fitted on are free of the quantiser's training rows of `key`
    (quantiser_rows_of)."""
    from jeff_runtime.engine.quantize import state_key

    record = f"{int4.folder}'s quantization.json"
    if published:
        recorded = int4.calibration_sha256[key]
        if _file_sha256(path) != recorded:
            raise CalibrationError(f"{key}: its calibration set {uri} ({path}) is not the file the quantiser checked its "
                                   f"training rows against ({record} records SHA-256 {recorded}); the calibration "
                                   "data changed since quantisation, so it may now hold rows the quantiser saw: "
                                   "quantise again, or calibrate on the recorded file")
        return
    entry = int4.training_rows[key]
    training = Path(entry["path"])
    if not training.is_file():
        raise CalibrationError(f"{key}: the quantiser's training rows file {training} ({record}) is not on this "
                               f"machine, so {uri} cannot be checked against the rows the quantiser saw")
    if _file_sha256(training) != entry["sha256"]:
        raise CalibrationError(f"{key}: the training rows file {training} is not the one the quantiser drew from "
                               f"(SHA-256 {entry['sha256']} in {record}); cannot check {uri} against its rows")
    seen = [row for row in (json.loads(line) for line in training.read_text(encoding="utf-8").split("\n")
                            if line.strip()) if str(row["id"]) in ids]
    missing = ids - {str(row["id"]) for row in seen}
    if missing:
        raise CalibrationError(f"{key}: {len(missing)} of the quantiser's rows ({', '.join(sorted(missing)[:3])}) are "
                               f"not in its training rows file {training}; the record and the file disagree")
    states = {state_key(row["state"]) for row in seen}
    same_id = sorted(str(raw["id"]) for raw in raws if str(raw["id"]) in ids)
    same_state = sorted(str(raw["id"]) for raw in raws if "state" in raw and state_key(raw["state"]) in states)
    if same_id or same_state:
        problems = []
        if same_id:
            problems.append(f"{len(same_id)} with the id of a row the quantiser saw ({', '.join(same_id[:5])})")
        if same_state:
            problems.append(f"{len(same_state)} with the state of a row the quantiser saw ({', '.join(same_state[:5])})")
        raise CalibrationError(f"{key}: {uri} holds rows the quantiser saw (drawn from {training}): "
                               f"{'; '.join(problems)}; calibrate on data without them")


def excluded_reason(left_out: int, recorded: bool) -> str:
    """A calibration_by_format entry's `excluded`: which of a function's rows were left out of its fit, and why."""
    if left_out:
        return f"{left_out} rows the quantiser saw (quantization.json)"
    if recorded:
        return ("none in this calibration data (none of this function's rows is among the quantiser's rows listed "
                "in quantization.json)")
    return "none: quantization.json records no rows of this adapter"


def _json_object(path: Path) -> dict[str, Any]:
    """A JSON file holding an object; CalibrationError naming the file otherwise."""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise CalibrationError(f"{path} is not valid JSON: {error}") from error
    if not isinstance(loaded, dict):
        raise CalibrationError(f"{path} does not hold a JSON object")
    return loaded


def _dump(node: dict[str, Any], indent: int) -> list[str]:
    """`node` as YAML lines, re-indented so its top-level keys land at column `indent`. A fresh YAML() (default
    block style, wide enough never to wrap these short values) keeps this independent of the manifest's own
    formatting."""
    yaml = YAML()
    yaml.width = 4096
    buffer = StringIO()
    yaml.dump(node, buffer)
    return [(" " * indent + line) if line.strip() else line for line in buffer.getvalue().splitlines(keepends=True)]


def _dump_block(block: dict[str, Any], indent: int) -> list[str]:
    """`{"calibration": block}` as YAML lines, `calibration:` at column `indent`."""
    return _dump({"calibration": dict(block)}, indent)


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to `path` via a temp file + rename, so a crash mid-write never leaves a half-written manifest.
    The rewritten file keeps the original's permission bits (mkstemp creates the temporary file 0600)."""
    mode = path.stat().st_mode & 0o7777
    descriptor, temp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp_name, mode)
        os.replace(temp_name, path)
    except Exception:
        os.unlink(temp_name)
        raise


def write_block(manifest_path: Path, function_key: str, block: dict[str, Any]) -> None:
    """Replace one function's `calibration:` node with the given block, touching no other line of the manifest.

    To guarantee that, this only ever replaces a single physical line: `calibration: null` written exactly that
    way, with nothing else on the line. Anything else at that key (already a block, a trailing comment, or any
    shape other than the bare scalar `null`) is refused with `CalibrationError` *before* anything is written, since
    there would then be no safe way to tell where the node ends without risking deleting unrelated content (a
    comment, or a field that happens to come after `calibration` in this function) — this is deliberately stricter
    than "find the next key" boundary-guessing, which silently ate following content when calibration was not a
    function's last key.

    The new manifest text is parsed and validated (`manifest_from_dict`) in memory before anything touches disk,
    and the write itself is atomic (temp file + rename), so a bad block or a bug here can never leave a broken
    manifest.yaml on disk."""
    text = manifest_path.read_text(encoding="utf-8")
    document = YAML().load(text)
    functions = document.get("functions")
    if functions is None or function_key not in functions:
        raise CalibrationError(f"{manifest_path} has no function {function_key}")
    body = functions[function_key]
    if "calibration" not in body.lc.data:
        raise CalibrationError(f"{manifest_path}: function {function_key} has no calibration field")
    line_number = body.lc.data["calibration"][0]
    lines = text.splitlines(keepends=True)
    line = lines[line_number]
    indent = len(line) - len(line.lstrip(" "))
    if line.strip() != "calibration: null":
        raise CalibrationError(f"{manifest_path}: function {function_key}'s calibration line is not a plain "
                               "'calibration: null' (it may already be calibrated, or have a comment on the same "
                               "line); refusing rather than guess which lines around it are safe to replace")
    replacement = _dump_block(block, indent)
    new_text = "".join(lines[:line_number] + replacement + lines[line_number + 1 :])
    manifest_from_dict(yaml.safe_load(new_text), manifest_path)  # validate in memory before writing anything
    _atomic_write(manifest_path, new_text)
    load_manifest(manifest_path)  # defensive: re-validate what actually landed on disk


def write_format_block(manifest_path: Path, function_key: str, format_id: str, entry: dict[str, Any]) -> None:
    """Add one function's calibration_by_format entry for the INT4 base `format_id`, touching no other line of the
    manifest: only new lines are inserted, never any replaced. Without a calibration_by_format map yet, the map with
    this one entry goes just before the function's `calibration:` line, at its indent; with one, the entry goes just
    before the map's first entry, at that entry's indent.

    Refused with CalibrationError, before anything is written: an entry for `format_id` already there (the refusal
    to overwrite applies per format; remove that entry to refit), a function whose calibration is null (it stays
    uncalibrated in every format), and a calibration_by_format map not written as a block mapping. As in
    write_block, the new text is checked in memory before anything touches disk (it must parse to exactly the old
    manifest plus this entry, and be a valid manifest), and the write is atomic."""
    text = manifest_path.read_text(encoding="utf-8")
    document = YAML().load(text)
    functions = document.get("functions")
    if functions is None or function_key not in functions:
        raise CalibrationError(f"{manifest_path} has no function {function_key}")
    body = functions[function_key]
    if "calibration" not in body:
        raise CalibrationError(f"{manifest_path}: function {function_key} has no calibration field")
    if body["calibration"] is None:
        raise CalibrationError(f"{manifest_path}: function {function_key} has calibration null; a function "
                               "uncalibrated on the bfloat16 base stays uncalibrated in every format")
    lines = text.splitlines(keepends=True)
    if "calibration_by_format" in body:
        existing = body["calibration_by_format"]
        if format_id in existing:
            raise CalibrationError(f"{manifest_path}: function {function_key} is already calibrated on {format_id}; "
                                   "remove that calibration_by_format entry to refit it")
        if not isinstance(existing, CommentedMap) or existing.fa.flow_style() or not existing:
            raise CalibrationError(f"{manifest_path}: function {function_key}'s calibration_by_format is not a block "
                                   "mapping of entries; refusing rather than guess where a new entry may go")
        line_number, indent = existing.lc.data[next(iter(existing))][:2]
        inserted = _dump({format_id: dict(entry)}, indent)
    else:
        line_number, indent = body.lc.data["calibration"][:2]
        inserted = _dump({"calibration_by_format": {format_id: dict(entry)}}, indent)
    new_text = "".join(lines[:line_number] + inserted + lines[line_number:])
    expected = yaml.safe_load(text)
    expected["functions"][function_key].setdefault("calibration_by_format", {})[format_id] = dict(entry)
    written = yaml.safe_load(new_text)
    if written != expected:
        raise CalibrationError(f"{manifest_path}: adding {function_key}'s entry for {format_id} would change other "
                               "content of the manifest; nothing was written")
    manifest_from_dict(written, manifest_path)  # validate in memory before writing anything
    _atomic_write(manifest_path, new_text)
    load_manifest(manifest_path)  # defensive: re-validate what actually landed on disk


class MeasuringJeff(Jeff):
    """A Jeff whose decisions carry no threshold, for jeff calibrate: it measures each row's confidence, which a
    threshold does not change, and on an INT4 base the functions it calibrates have no threshold for that format
    yet (a plain Jeff refuses to bind them there)."""

    def calibration_of(self, function: FunctionSpec) -> Calibration | None:
        return None
