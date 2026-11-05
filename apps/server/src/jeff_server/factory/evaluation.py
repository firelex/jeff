"""Calibration and evaluation of a trained adapter.

calibrate: the temperature fitted on the calibration split only (hard-label log loss, fit_temperature below), written
           into the adapter's decision_config.json; the trainer's own fit on the same split is recorded next to it.
evaluate:  accuracy, accuracy per option and calibration error (ECE, 10 equal-width confidence bins) on a test file,
           with 95% bootstrap intervals over items. The held-out synthetic test measures agreement with the teacher
           on new families, not truth; an uploaded independent test measures against the owner's labels.

The scoring itself (score) runs in a separate process, jeff_server.factory.score_job, through the one engine
(jeff_runtime.LocalEngine), so the worker's own memory never holds a model. calibrate and evaluate take the scorer as
an argument: the worker passes score, tests a fake one. fit_temperature, label_index, options, evaluate_logits and
metrics are jeff-server's (jeff.evaluate), cut down to what the factory uses, in plain Python."""

import json
import math
import random
import subprocess
import sys
import tempfile
from collections import defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

BOOTSTRAP = 1000
SYNTHETIC = "held-out synthetic test (teacher labels)"
INDEPENDENT = "independent test (your labels)"

# score(base, adapter, rows) -> {"logits": one list of raw logits per row, in the row's option order, "seconds"}
type Scorer = Callable[[str, Path, list[dict[str, Any]]], dict[str, Any]]
type Label = str | int | bool


def score(base: str, adapter: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Logits for every row, from a separate process (jeff_server.factory.score_job)."""
    with tempfile.TemporaryDirectory(prefix="jeff-score-") as folder:
        rows_file, out = Path(folder) / "rows.jsonl", Path(folder) / "out.json"
        rows_file.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        result = subprocess.run([sys.executable, "-m", "jeff_server.factory.score_job", base, str(adapter),
                                 str(rows_file), str(out)], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"Scoring {len(rows)} rows failed (exit {result.returncode}):\n{result.stderr[-1500:]}")
        value: dict[str, Any] = json.loads(out.read_text())
    return {"logits": value["logits"], "seconds": value["seconds"]}


def options(question: dict[str, Any]) -> list[Label]:
    if question["type"] == "noul":
        return [False, True]
    if question["type"] == "choice":
        return list(question["criteria"])
    if question["type"] == "score":
        # Levels 0..n-1 in their given order, as the served prompt lists them (never shuffled); the hard label is the
        # level's index (an int).
        return list(range(len(question["criteria"])))
    raise ValueError(f"Unknown question type {question['type']!r}")


def label_index(labels: Sequence[Label], label: Label) -> int:
    for index, value in enumerate(labels):
        if type(value) is type(label) and value == label:
            return index
    raise ValueError(f"Reference or prediction {label!r} is not an option")


def scaled_softmax(row: dict[str, Any], values: Sequence[float], temperature: float) -> list[float]:
    """The row's probabilities at this temperature, from its valid logits."""
    count = len(options(row["question"]))
    raw = [float(value) for value in values[:count]]
    if len(raw) != count or not all(math.isfinite(value) for value in raw):
        raise ValueError(f"Missing or nonfinite valid logits: {row['id']}")
    top = max(raw)
    exponents = [math.exp((value - top) / temperature) for value in raw]
    total = sum(exponents)
    return [value / total for value in exponents]


def evaluate_logits(rows: Sequence[dict[str, Any]], logits: Sequence[Sequence[float]],
                    temperature: float) -> list[dict[str, Any]]:
    """One prediction per row: its label, options, probabilities, the option chosen (the most probable), whether it
    is right, and its confidence (the chosen option's probability)."""
    if len(rows) != len(logits) or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Logit rows must match examples and temperature must be positive")
    predictions = []
    for row, values in zip(rows, logits, strict=True):
        probabilities = scaled_softmax(row, values, temperature)
        labels = options(row["question"])
        label = row.get("label", row["target"])
        if not isinstance(label, (str, int, bool)):
            raise ValueError(f"An explicit hard label is required: {row['id']}")
        label_index(labels, label)
        chosen_index = max(range(len(labels)), key=probabilities.__getitem__)
        chosen = labels[chosen_index]
        predictions.append({"id": row["id"], "label": label, "options": labels, "probabilities": probabilities,
                            "prediction": chosen, "correct": type(chosen) is type(label) and chosen == label,
                            "confidence": probabilities[chosen_index], "temperature": temperature,
                            "logits": [float(value) for value in values[:len(labels)]]})
    return predictions


def fit_temperature(logits: Sequence[Sequence[float]], target_indices: Sequence[int]) -> float:
    """Fit one scalar on the separate temperature fold using hard-label NLL."""
    if not logits or len(logits) != len(target_indices):
        raise ValueError("Temperature fitting needs nonempty matching logits and hard labels")
    values: list[list[float]] = []
    for row, target in zip(logits, target_indices, strict=True):
        if not row or not 0 <= target < len(row) or not all(math.isfinite(value) for value in row):
            raise ValueError("Temperature fitting requires finite, unpadded valid logits")
        top = max(row)
        values.append([float(value) - top for value in row])
    target_logits = [row[target] for row, target in zip(values, target_indices, strict=True)]

    def loss(log_temperature: float) -> float:
        inverse = math.exp(-log_temperature)
        return sum(math.log(sum(math.exp(value * inverse) for value in row)) - target * inverse
                   for row, target in zip(values, target_logits, strict=True)) / len(values)

    # A broad, fixed interval avoids selecting temperature bounds after seeing dev results.
    low, high = math.log(0.05), math.log(20.0)
    ratio = (math.sqrt(5) - 1) / 2
    left, right = high - ratio * (high - low), low + ratio * (high - low)
    left_loss, right_loss = loss(left), loss(right)
    for _ in range(80):
        if left_loss <= right_loss:
            high, right, right_loss = right, left, left_loss
            left = high - ratio * (high - low)
            left_loss = loss(left)
        else:
            low, left, left_loss = left, right, right_loss
            right = low + ratio * (high - low)
            right_loss = loss(right)
    candidates = [math.log(0.05), (low + high) / 2, math.log(20.0), 0.0]
    return math.exp(min(candidates, key=loss))


def metrics(predictions: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Accuracy and log loss (the mean negative log probability of the right option)."""
    if not predictions:
        raise ValueError("Cannot score an empty evaluation")
    nll = [-math.log(max(p["probabilities"][label_index(p["options"], p["label"])], 1e-12)) for p in predictions]
    return {"count": len(predictions), "accuracy": sum(p["correct"] for p in predictions) / len(predictions),
            "nll": sum(nll) / len(predictions)}


def read_rows(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def scored_rows(scorer: Scorer, base: str, adapter: Path, rows: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    """The scorer's logits for `rows`, also kept in `out` next to the run's other files."""
    scored = scorer(base, adapter, [{"state": row["state"], "question": row["question"]} for row in rows])
    if len(scored["logits"]) != len(rows):
        raise RuntimeError(f"Scoring returned {len(scored['logits'])} rows of logits for {len(rows)} rows")
    out.write_text(json.dumps(scored))
    return scored


def calibrate(base: str, adapter: Path, calibration: Path, work: Path, scorer: Scorer) -> dict[str, Any]:
    rows = read_rows(calibration)
    scored = scored_rows(scorer, base, adapter, rows, work / "calibration-logits.json")
    targets = [label_index(options(row["question"]), row["label"]) for row in rows]
    fitted = fit_temperature(scored["logits"], targets)
    config_path = adapter / "decision_config.json"
    config = json.loads(config_path.read_text())
    trainer_temperature = config["temperature"]
    config["temperature"] = fitted
    config_path.write_text(json.dumps(config, indent=2) + "\n")
    return {"temperature": fitted, "trainer_temperature": trainer_temperature, "rows": len(rows),
            "seconds": round(scored["seconds"], 1)}


def calibration_error(predictions: list[Any], bins: int = 10) -> tuple[float, list[dict[str, Any]]]:
    """ECE with equal-width confidence bins (the spec asks for 10)."""
    groups: list[list[Any]] = [[] for _ in range(bins)]
    for prediction in predictions:
        groups[min(bins - 1, int(prediction["confidence"] * bins))].append(prediction)
    error, table = 0.0, []
    for index, group in enumerate(groups):
        confidence = sum(p["confidence"] for p in group) / len(group) if group else None
        accuracy = sum(p["correct"] for p in group) / len(group) if group else None
        if group:
            error += len(group) / len(predictions) * abs(confidence - accuracy)
        table.append({"lower": index / bins, "upper": (index + 1) / bins, "count": len(group),
                      "confidence": confidence, "accuracy": accuracy})
    return error, table


def call_of(row: dict[str, Any]) -> str | None:
    """The command a row belongs to: the generated request (synthetic test) or the uploaded call_id (independent)."""
    source = row["source"]
    if "scenario" in source:
        return f"scenario:{source['scenario']}"
    return source.get("call_id")


def whole_calls(rows: list[dict[str, Any]], predictions: list[Any]) -> dict[str, Any] | None:
    """Whole-call exact match: a command counts as right only if every question asked about it is answered right.
    Its confidence is the least certain of those answers (as the cookbook's dispatcher reports it); the calibration
    of that confidence against exact match is reported too. None when rows carry no call (an independent test set
    uploaded without call_id)."""
    groups: dict[str, list[Any]] = defaultdict(list)
    for row, prediction in zip(rows, predictions, strict=True):
        call = call_of(row)
        if call is None:
            return None
        groups[call].append(prediction)
    calls = [{"correct": all(p["correct"] for p in group), "confidence": min(p["confidence"] for p in group)}
             for group in groups.values()]
    exact = [float(call["correct"]) for call in calls]
    ece, reliability = calibration_error(calls)
    return {"calls": len(calls), "exact_match": sum(exact) / len(exact), "interval": interval(exact),
            "questions_per_call": round(len(rows) / len(calls), 2), "confidence_ece": ece,
            "confidence_reliability": reliability}


def interval(values: list[float], seed: int = 0) -> list[float]:
    rng = random.Random(seed)
    means = sorted(sum(rng.choices(values, k=len(values))) / len(values) for _ in range(BOOTSTRAP))
    return [means[int(BOOTSTRAP * 0.025)], means[int(BOOTSTRAP * 0.975) - 1]]


def evaluate(base: str, adapter: Path, test: Path, work: Path, label: str, scorer: Scorer) -> dict[str, Any]:
    rows = read_rows(test)
    scored = scored_rows(scorer, base, adapter, rows, work / f"{test.stem}-logits.json")
    temperature = json.loads((adapter / "decision_config.json").read_text())["temperature"]
    predictions = evaluate_logits(rows, scored["logits"], temperature)
    overall = metrics(predictions)
    ece, reliability = calibration_error(predictions)
    correct = [float(prediction["correct"]) for prediction in predictions]
    by_question: dict[str, list[Any]] = defaultdict(list)
    by_option: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for row, prediction in zip(rows, predictions, strict=True):
        qid = row["source"]["question_id"]
        by_question[qid].append(prediction)
        by_option[qid][str(prediction["label"])].append(float(prediction["correct"]))
    questions = {}
    for qid, group in by_question.items():
        values = [float(p["correct"]) for p in group]
        questions[qid] = {"rows": len(group), "accuracy": sum(values) / len(values), "interval": interval(values),
                          "ece": calibration_error(group)[0],
                          "by_option": {key: {"rows": len(v), "accuracy": sum(v) / len(v)}
                                        for key, v in sorted(by_option[qid].items())}}
    calls = whole_calls(rows, predictions)
    mistakes = [{"id": row["id"], "state": row["state"], "question_id": row["source"]["question_id"],
                 "label": prediction["label"], "prediction": prediction["prediction"],
                 "confidence": round(prediction["confidence"], 3)}
                for row, prediction in zip(rows, predictions, strict=True) if not prediction["correct"]][:20]
    return {"label": label, "rows": len(rows), "accuracy": overall["accuracy"], "interval": interval(correct),
            "ece": ece, "ece_bins": 10, "reliability": reliability, "log_loss": overall["nll"], "questions": questions,
            "temperature": temperature, "seconds": round(scored["seconds"], 1), "mistakes": mistakes,
            "calls": calls}
