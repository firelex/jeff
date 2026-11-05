"""Reading test sets and scoring functions on them: used by `jeff test`, `jeff calibrate` and the accuracy check.

Two ways to run a published test set:
- template mode (`score` on `template_rows`): each row is rebuilt as the Call a user would make, so the request uses
  the manifest's instructions and label texts. Rows whose shape the manifest does not describe are skipped and counted;
  a row that maps to no function raises ManifestError naming it (tests.function_by must place every row).
- engine mode (`engine_score`): each row's own state and question go to the engine unchanged. This is how the
  published accuracy was measured."""

import json
import math
import os
import random
import re
import time
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from jeff.binding import bind
from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.decision import Decision, Value
from jeff.engine import Engine
from jeff.errors import EngineError, JeffError
from jeff.manifest import FunctionSpec, Manifest, function_of_test_row

HF = re.compile(r"^hf://(datasets/)?([^/@]+/[^/@]+)@([^/]+)/(.+)$")


# JEFF_DATA_MIRROR: local copies of Hugging Face datasets at one revision each, read in place of downloading
DATA_MIRROR = "JEFF_DATA_MIRROR"
MIRROR_FORM = "<owner>/<repo>@<revision>=<local folder>"
_MIRROR = re.compile(r"^([^/@=;\s]+/[^/@=;\s]+)@([^/@=;\s]+)=(.+)$")


def data_mirrors() -> dict[tuple[str, str], Path]:
    """The JEFF_DATA_MIRROR setting: (dataset, revision) -> its local folder; empty when the variable is not set. A
    value is one or more mappings `<owner>/<repo>@<revision>=<local folder>` separated by ';'. A malformed value, a
    dataset and revision mapped twice, or a folder that does not exist raises JeffError naming the expected form."""
    value = os.environ.get(DATA_MIRROR)
    if value is None:
        return {}
    mirrors: dict[tuple[str, str], Path] = {}
    parsed = [(part, _MIRROR.fullmatch(part.strip())) for part in value.split(";")]
    for part, match in parsed:
        if match is None:
            raise JeffError(f"{DATA_MIRROR} must be {MIRROR_FORM}, several separated by ';' (for example "
                            f"teamsuzie/jeff-data@main=/mnt/d/jeff-data); got the part {part!r} of {value!r}")
    for repo, revision, folder in (match.groups() for _, match in parsed if match is not None):
        if (repo, revision) in mirrors:
            raise JeffError(f"{DATA_MIRROR} maps {repo}@{revision} twice; each {MIRROR_FORM} names one dataset and "
                            "revision once")
        mirrors[(repo, revision)] = Path(folder)
    for (repo, revision), folder in mirrors.items():
        if not folder.is_dir():
            raise JeffError(f"{DATA_MIRROR} maps {repo}@{revision} to {folder}, which is not a folder")
    return mirrors


def _mirrored(uri: str) -> Path | None:
    """The local file a dataset URI is read from when JEFF_DATA_MIRROR maps its dataset and exact revision; None for
    any other URI or a plain path. A file missing from the mirror raises, naming the local path and the URI."""
    match = HF.match(uri)
    if not match:
        return None
    dataset, repo, revision, name = match.groups()
    mirrors = data_mirrors()
    if not dataset or (repo, revision) not in mirrors:
        return None
    local = mirrors[(repo, revision)] / name
    if not local.is_file():
        raise JeffError(f"{local} does not exist: {DATA_MIRROR} maps {repo}@{revision} to "
                        f"{mirrors[(repo, revision)]}, so {uri} is read from there and never downloaded")
    return local


def mirror_read(uri: str) -> dict[str, str] | None:
    """Where the rows of `uri` are read when that is not the place it names: for a dataset URI that JEFF_DATA_MIRROR
    maps, {"path": the local file, "sha256": its SHA-256}, which records keep beside the hf:// URI (data_mirror), so
    a reader elsewhere can tell that the bytes came from a copy of a revision that may since have moved. None when
    the URI is read from where it names (downloaded from Hugging Face, or a plain path)."""
    import hashlib
    local = _mirrored(uri)
    if local is None:
        return None
    return {"path": str(local), "sha256": hashlib.sha256(local.read_bytes()).hexdigest()}


def resolve(uri: str) -> Path:
    """`hf://<owner>/<repo>@<revision>/<file>` (a model repo) or `hf://datasets/<owner>/<repo>@<revision>/<file>` (a
    dataset) -> the cached download of that one file; a plain path as it is. A dataset URI whose dataset and exact
    revision JEFF_DATA_MIRROR maps (data_mirrors) is the file in that local folder instead, and nothing is
    downloaded: a file missing there raises, naming the local path and the URI. Every reader of test and calibration
    rows goes through here; a record of such rows notes a mirror read with mirror_read."""
    local = _mirrored(uri)
    if local is not None:
        return local
    match = HF.match(uri)
    if not match:
        path = Path(uri)
        if not path.exists():
            raise JeffError(f"Test file {uri} does not exist")
        return path
    dataset, repo, revision, name = match.groups()
    import huggingface_hub
    return Path(huggingface_hub.hf_hub_download(repo, name, revision=revision,
                                                repo_type="dataset" if dataset else None))


@dataclass(frozen=True)
class TestRow:
    __test__ = False  # not a pytest test class

    id: str
    function: str
    call: Call
    expected: Value


def calibration_set(manifest: Manifest) -> str | None:
    """The adapter's calibration set: tests.calibration, else tests.internal_calibration (private data the
    maintainers can read); None when neither is set. `jeff calibrate` and `jeff runtime gate` both take their
    calibration rows by this one rule."""
    uri = manifest.tests.get("calibration") or manifest.tests.get("internal_calibration")
    return None if uri is None else str(uri)


def set_uri(manifest: Manifest, which: Literal["test", "calibration"]) -> str:
    """The published set, else the internal one (private data the maintainers can read); for the calibration set,
    calibration_set's rule."""
    published, internal = ("published", "internal") if which == "test" else ("calibration", "internal_calibration")
    if which == "calibration":
        uri = calibration_set(manifest)
    else:
        uri = manifest.tests[published] or manifest.tests.get(internal)
    if uri is None:
        raise JeffError(f"{manifest.id} has no {which} set: tests.{published} and tests.{internal} are both empty")
    return str(uri)


def read_rows(manifest: Manifest, which: Literal["test", "calibration"], sample: int | None = None,
              seed: int = 0) -> list[dict[str, Any]]:
    """The rows of the test (or calibration) set as written; with `sample`, a fixed-seed random sample of them."""
    path = resolve(set_uri(manifest, which))
    raws = [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]
    if not raws:
        raise JeffError(f"{manifest.id}: {path} has no rows")
    if sample is not None and sample < len(raws):
        raws = random.Random(seed).sample(raws, sample)
    return raws


def _labels(function: FunctionSpec, criteria: Any) -> tuple[Any, str | None]:
    """The labels= a caller would pass to ask the row's question, or the reason the manifest does not describe it."""
    spec = function.labels
    if function.type == "noul":
        return None, None
    if function.type == "score":
        return None, None if isinstance(criteria, list) and len(criteria) == len(spec.levels) else "score levels differ"
    if not isinstance(criteria, dict):
        return None, "choice options are not an object"
    if spec.source == "fixed":
        if set(criteria) == set(spec.fixed):
            labels = None
        elif spec.subset and criteria and set(criteria) < set(spec.fixed):
            labels = [key for key in spec.fixed if key in criteria]
        else:
            return None, "fixed options differ from the manifest"
        if any(text != spec.fixed[key] for key, text in criteria.items()):
            return None, "fixed option texts differ from the manifest"
        return labels, None
    if spec.source == "caller":
        if any(key not in criteria for key in spec.fixed_first):
            return None, "fixed_first options missing"
        return {key: text for key, text in criteria.items() if key not in spec.fixed_first}, None
    if spec.source == "subset":
        if any(key not in spec.fixed for key in criteria) or any(key not in criteria for key in spec.required):
            return None, "subset options differ from the manifest"
        return list(criteria), None
    if set(criteria) != set(spec.keys):
        return None, "describe keys differ from the manifest"
    return dict(criteria), None


def split_state(function: FunctionSpec, state: Any) -> tuple[dict[str, Any], Any] | None:
    """A request's state split into (context by code name, as a caller passes it, input), by the function's own
    context and input (or its adapter's). With form text the state must be a string, which is the whole input (no
    context); otherwise None. With form fields the state is keyed by the exact field keys, and this is None when the
    state is not an object, has no input field, has a context field the function does not describe, or is missing a
    required context field. Used both to rebuild test rows as Calls and to build golden cases from an adapter's
    example.json."""
    spec = function.input
    if spec.form == "text":
        return ({}, state) if isinstance(state, str) else None
    if not isinstance(state, dict) or spec.key not in state:
        return None
    context = {key: value for key, value in state.items() if key != spec.key}
    known = {field.key for field in function.context}
    if set(context) - known or any(field.required and field.key not in context for field in function.context):
        return None
    code_of = {field.key: field.code_name for field in function.context}
    return {code_of[key]: value for key, value in context.items()}, state[spec.key]


def _template_row(manifest: Manifest, raw: dict[str, Any]) -> TestRow | str:
    # a row that maps to no function (or to several) is not skipped: function_of_test_row raises ManifestError
    # naming the row, as the importers do
    function = function_of_test_row(manifest, raw)
    state, question = raw["state"], raw["question"]
    if question["type"] != function.type:
        return "question type differs from the function"
    if function.input.form == "text" and not isinstance(state, str):
        return "state is not plain text"
    if function.input.form == "fields" and (not isinstance(state, dict) or function.input.key not in state):
        return "no input field"
    split = split_state(function, state)
    if split is None:
        return "state fields differ from the manifest"
    context, input = split
    labels, reason = _labels(function, question.get("criteria"))
    if reason is not None:
        return reason
    if function.type == "choice" and raw["label"] not in question["criteria"]:
        raise JeffError(f"{manifest.id}: test row {raw['id']} has label {raw['label']!r}, which is not one of its "
                        f"options {list(question['criteria'])}")
    # a row the manifest describes must bind; anything else is a bug (only the binding's checks matter here: the
    # row is decided later by a Jeff, which binds it again with its engine's calibration)
    bind(manifest, function, context, labels, calibration=None)
    return TestRow(raw["id"], function.id, Call(function.id, input, context, labels), raw["label"])


def template_rows(manifest: Manifest, raws: Sequence[dict[str, Any]]) -> tuple[list[TestRow], dict[str, int]]:
    """The rows rebuilt as Calls through the manifest, and how many were skipped for each reason."""
    rows: list[TestRow] = []
    skipped: Counter[str] = Counter()
    for raw in raws:
        result = _template_row(manifest, raw)
        if isinstance(result, str):
            skipped[result] += 1
        else:
            rows.append(result)
    return rows, dict(skipped)


@dataclass(frozen=True)
class PublishedSet:
    raw: list[dict[str, Any]]  # the sampled rows as written (engine mode)
    rows: list[TestRow]  # those the manifest describes, rebuilt as Calls (template mode)
    skipped: dict[str, int]  # rows not rebuilt, by reason


def published_set(manifest: Manifest, which: Literal["test", "calibration"], sample: int | None = None,
                  seed: int = 0) -> PublishedSet:
    raws = read_rows(manifest, which, sample, seed)
    rows, skipped = template_rows(manifest, raws)
    return PublishedSet(raws, rows, skipped)


def published_rows(catalogue: Catalogue, manifest: Manifest, which: Literal["test", "calibration"],
                   sample: int | None = None, seed: int = 0) -> list[TestRow]:
    """The template-mode rows only; use published_set for the skipped counts and the rows as written."""
    return published_set(catalogue.adapter(manifest.id), which, sample, seed).rows


def mine_rows(path: Path) -> list[TestRow]:
    """Customer cases: JSON lines {"id", "function", "input", "context"?, "labels"?, "expected"}."""
    rows = []
    for number, line in enumerate(Path(path).read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        raw = json.loads(line)
        missing = [key for key in ("id", "function", "input", "expected") if key not in raw]
        if missing:
            raise JeffError(f"{path}:{number} is missing {', '.join(missing)}")
        rows.append(TestRow(raw["id"], raw["function"], Call(raw["function"], raw["input"], raw.get("context"),
                                                           raw.get("labels")), raw["expected"]))
    return rows


# Rows handed to the engine at once by run, score and engine_score (one row per call): the most a jeff test or jeff
# calibrate pass holds, which sizes their engine's prefix pool (jeff.cli.make_jeff).
BATCH = 16


def wilson(correct: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        raise JeffError("No rows to score")
    p = correct / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return centre - half, centre + half


@dataclass(frozen=True)
class Report:
    function: str
    n: int
    correct: int
    accuracy: float
    interval: tuple[float, float]
    abstained: int
    accuracy_answered: float | None
    p50_ms: float
    p95_ms: float


def _report(function: str, items: Sequence[tuple[bool, bool, float]]) -> Report:
    """items: (correct, abstained, latency in ms) per row."""
    n = len(items)
    right = sum(ok for ok, _, _ in items)
    answered = [ok for ok, abstained, _ in items if not abstained]
    latencies = sorted(ms for _, _, ms in items)
    return Report(function, n, right, right / n, wilson(right, n), n - len(answered),
                  sum(answered) / len(answered) if answered else None,
                  latencies[n // 2], latencies[min(n - 1, int(n * 0.95))])


def same(value: Value, expected: Value) -> bool:
    """Equal and of the same type, so that True does not match 1."""
    return type(value) is type(expected) and value == expected


def golden_problems(case: Mapping[str, Any], decision: Decision, threshold: float | None) -> list[str]:
    """How a decision differs from a golden case (contract/golden/*.json); empty when it reproduces the case.
    `value` must be the same, and `confidence` within the case's tolerance. `abstained` must match too, except when
    the recorded confidence is within the tolerance of the function's threshold: a confidence that may legitimately
    move by the tolerance can then land on either side of the threshold."""
    expected, tolerance = case["expected"], case["tolerance"]
    problems: list[str] = []
    if not same(decision.value, expected["value"]):
        problems.append(f"value {decision.value!r}, expected {expected['value']!r}")
    if decision.confidence is None:
        problems.append("no confidence")
    elif abs(decision.confidence - expected["confidence"]) > tolerance:
        problems.append(f"confidence {decision.confidence}, expected {expected['confidence']} +/- {tolerance}")
    near_threshold = threshold is not None and abs(expected["confidence"] - threshold) <= tolerance
    if decision.abstained != expected["abstained"] and not near_threshold:
        problems.append(f"abstained {decision.abstained}, expected {expected['abstained']} (the recorded confidence "
                        f"is more than {tolerance} from the threshold {threshold})")
    return problems


def run(jeff: Jeff, rows: Sequence[TestRow], batch: int = BATCH) -> list[tuple[TestRow, Decision, float]]:
    out: list[tuple[TestRow, Decision, float]] = []
    for start in range(0, len(rows), batch):
        chunk = rows[start:start + batch]
        began = time.perf_counter()
        decisions = jeff.decide([row.call for row in chunk])
        each = (time.perf_counter() - began) * 1000 / len(chunk)
        out.extend((row, decision, each) for row, decision in zip(chunk, decisions, strict=True))
    return out


def correct(row: TestRow, decision: Decision) -> bool:
    return same(decision.value, row.expected)


def score(jeff: Jeff, rows: Sequence[TestRow], batch: int = BATCH) -> list[Report]:
    """Template mode: one Report per function."""
    groups: dict[str, list[tuple[bool, bool, float]]] = defaultdict(list)
    for row, decision, ms in run(jeff, rows, batch):
        groups[jeff.catalogue.function(row.function).id].append((correct(row, decision), decision.abstained, ms))
    return [_report(function, items) for function, items in groups.items()]


def predicted(question: dict[str, Any], probabilities: Sequence[float]) -> Value:
    """The answer a row's probabilities give: the most likely option (choice), true when p(true) >= 0.5 (noul), the
    most likely level's index (score)."""
    if question.get("true_first"):
        raise JeffError("Rows with true_first (the reversed pass of answering twice) are not scored")
    kind = question["type"]
    count = len(question["criteria"]) if kind in ("choice", "score") else 2
    if len(probabilities) != count:
        raise EngineError(f"The engine returned {len(probabilities)} probabilities for a {kind} question with "
                          f"{count} options")
    if kind == "noul":
        return probabilities[1] / sum(probabilities) >= 0.5  # options are false, true
    best = max(range(count), key=probabilities.__getitem__)
    return list(question["criteria"])[best] if kind == "choice" else best


def engine_score(engine: Engine, adapter_id: str, raws: Sequence[dict[str, Any]], batch: int = BATCH) -> Report:
    """Engine mode: each row's own state and question sent to the engine unchanged; one Report for the adapter."""
    items: list[tuple[bool, bool, float]] = []
    for start in range(0, len(raws), batch):
        chunk = raws[start:start + batch]
        began = time.perf_counter()
        output = engine.predict(adapter_id, [{"state": raw["state"], "question": raw["question"]} for raw in chunk])
        each = (time.perf_counter() - began) * 1000 / len(chunk)
        if len(output.probabilities) != len(chunk):
            raise EngineError(f"The engine answered {len(output.probabilities)} of {len(chunk)} rows for {adapter_id}")
        for raw, probabilities in zip(chunk, output.probabilities, strict=True):
            items.append((same(predicted(raw["question"], probabilities), raw["label"]), False, each))
    return _report(adapter_id, items)
