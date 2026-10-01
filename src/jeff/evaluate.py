"""Accuracy and calibration on fixed Choice/Noul examples, with paired comparisons."""

import argparse
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
from typing import NotRequired, TypedDict, cast

import numpy as np
from numpy.typing import NDArray

from jeff.orders import check_orders, restore_order, reverse_row
from jeff.types import Example, JSONValue, Label, Question


ECE_BINS = 15
CALIBRATION_MARGIN = 0.010
type FloatArray = NDArray[np.float64]
type Infer = Callable[[Sequence[Example]], Sequence[Sequence[float]]]


class Prediction(TypedDict):
    id: str
    suite: str
    dataset: str
    family: str
    label: Label
    options: list[Label]
    probabilities: list[float]
    prediction: Label
    correct: bool
    confidence: float
    temperature: float
    logits: NotRequired[list[float]]
    reversed_logits: NotRequired[list[float]]  # answering twice: the reversed pass, put back in the option order
    soft_target: NotRequired[list[float]]
    answer: NotRequired[dict[str, JSONValue]]
    model: NotRequired[str]
    provider: NotRequired[str]
    request_id: NotRequired[str]
    latency_ms: NotRequired[float]
    normalization_total: NotRequired[float]
    created: NotRequired[str]
    request_sha256: NotRequired[str]


class ReliabilityBin(TypedDict):
    lower: float
    upper: float
    count: int
    confidence: float | None
    accuracy: float | None


class Metrics(TypedDict):
    count: int
    accuracy: float
    ece: float
    brier: float
    nll: float
    reliability: list[ReliabilityBin]
    zero_probability_count: int
    soft_count: NotRequired[int]
    soft_nll: NotRequired[float]
    soft_brier: NotRequired[float]


class Effect(TypedDict):
    local: float
    reference: float
    difference: float
    difference_ci95: list[float]


class Comparison(TypedDict):
    count: int
    families: int
    effects: dict[str, Effect]
    by_suite: dict[str, dict[str, Effect]]
    empirical_target_met: bool
    statistically_supported: bool
    calibration_margin: float
    bootstrap_replicates: int
    bootstrap_seed: int


def options(question: Question) -> list[Label]:
    if question["type"] == "noul":
        return [False, True]
    if question["type"] == "choice":
        return list(question["criteria"])
    if question["type"] == "score":
        # Levels 0..n-1 in their given order, as jeff.model.options builds the served prompt (never shuffled); the hard
        # label is the level's index (an int), a soft target a list with one probability per level.
        return list(range(len(question["criteria"])))
    raise ValueError(f"Unknown question type {question['type']!r}")


def hard_label(row: Example) -> Label:
    """Soft SFT targets never silently replace the evaluation reference label."""
    label = row.get("label", row["target"])
    if not isinstance(label, (str, int, bool)):
        raise ValueError(f"An explicit hard label is required: {row['id']}")
    return label


def label_index(labels: Sequence[Label], label: Label) -> int:
    for index, value in enumerate(labels):
        if type(value) is type(label) and value == label:
            return index
    raise ValueError(f"Reference or prediction {label!r} is not an option")


def make_prediction(
    row: Example, probabilities: Sequence[float], *, prediction: Label | None = None,
    temperature: float = 1.0,
) -> Prediction:
    labels = options(row["question"])
    values = [float(value) for value in probabilities]
    if len(values) != len(labels) or any(not math.isfinite(p) or not 0 <= p <= 1 for p in values):
        raise ValueError(f"Invalid probability vector: {row['id']}")
    if not math.isclose(sum(values), 1, abs_tol=1e-8):
        raise ValueError(f"Probabilities must be normalized: {row['id']}")
    label = hard_label(row)
    label_index(labels, label)
    chosen = labels[max(range(len(labels)), key=values.__getitem__)] if prediction is None else prediction
    chosen_index = label_index(labels, chosen)
    family = row.get("family")
    dataset = row["source"].get("dataset", row["suite"])
    if not isinstance(family, str) or not family or not isinstance(dataset, str):
        raise ValueError(f"Missing dataset/family provenance: {row['id']}")
    result: Prediction = {
        "id": row["id"], "suite": row["suite"], "dataset": dataset, "family": family,
        "label": label, "options": labels, "probabilities": values, "prediction": chosen,
        "correct": type(chosen) is type(label) and chosen == label,
        "confidence": values[chosen_index], "temperature": temperature,
    }
    human = row["source"].get("human_distribution")
    soft: list[float] | None = None
    if isinstance(human, dict):
        keys = [str(value).lower() if isinstance(value, bool) else str(value) for value in labels]
        if set(human) != set(keys):
            raise ValueError(f"Human distribution does not match options: {row['id']}")
        soft = [float(cast(float, human[key])) for key in keys]
    elif isinstance(row["target"], list):
        soft = [float(value) for value in row["target"]]
    if soft is not None:
        if len(soft) != len(labels) or any(not math.isfinite(p) or not 0 <= p <= 1 for p in soft) or not math.isclose(sum(soft), 1, abs_tol=1e-6):
            raise ValueError(f"Invalid soft distribution: {row['id']}")
        result["soft_target"] = soft
    return result


def scaled_softmax(row: Example, values: Sequence[float], temperature: float) -> tuple[FloatArray, FloatArray]:
    """The row's valid logits and their probabilities at this temperature."""
    count = len(options(row["question"]))
    raw = np.asarray(values[:count], dtype=np.float64)
    if len(raw) != count or not np.isfinite(raw).all():
        raise ValueError(f"Missing or nonfinite valid logits: {row['id']}")
    shifted = (raw - raw.max()) / temperature
    probabilities = np.exp(shifted)
    probabilities /= probabilities.sum()
    return raw, probabilities


def evaluate_logits(
    rows: Sequence[Example], logits: Sequence[Sequence[float]], temperature: float = 1.0,
    reversed_logits: Sequence[Sequence[float]] | None = None,
) -> list[Prediction]:
    """reversed_logits (answering twice) are each row's logits with its options reversed (jeff.orders.reverse_row);
    the probabilities of the two orders are averaged per option."""
    if len(rows) != len(logits) or not math.isfinite(temperature) or temperature <= 0:
        raise ValueError("Logit rows must match examples and temperature must be positive")
    if reversed_logits is not None and len(reversed_logits) != len(rows):
        raise ValueError("Reversed-order logit rows must match examples")
    result: list[Prediction] = []
    for index, (row, values) in enumerate(zip(rows, logits, strict=True)):
        raw, probabilities = scaled_softmax(row, values, temperature)
        if reversed_logits is None:
            prediction = make_prediction(row, probabilities.tolist(), temperature=temperature)
        else:
            raw_reversed, probabilities_reversed = scaled_softmax(row, reversed_logits[index], temperature)
            restored = np.asarray(restore_order(row["question"], probabilities_reversed.tolist()), dtype=np.float64)
            prediction = make_prediction(row, ((probabilities + restored) / 2).tolist(), temperature=temperature)
            prediction["reversed_logits"] = restore_order(row["question"], raw_reversed.tolist())
        prediction["logits"] = raw.tolist()
        result.append(prediction)
    validate_coverage(rows, result)
    return result


def predict_local(rows: Sequence[Example], infer: Infer, temperature: float = 1.0, orders: int = 1) -> list[Prediction]:
    """The model adapter returns raw logits in the supplied row/option order. Two orders also score every row with
    its options reversed and average the two probability distributions (jeff.orders)."""
    check_orders(orders)
    if orders == 1:
        return evaluate_logits(rows, infer(rows), temperature)
    return evaluate_logits(rows, infer(rows), temperature, infer([reverse_row(row) for row in rows]))


def fit_temperature(logits: Sequence[Sequence[float]], target_indices: Sequence[int]) -> float:
    """Fit one scalar on the separate temperature fold using hard-label NLL."""
    if not logits or len(logits) != len(target_indices):
        raise ValueError("Temperature fitting needs nonempty matching logits and hard labels")
    width = max(map(len, logits))
    values = np.full((len(logits), width), -np.inf, dtype=np.float64)
    for index, (row, target) in enumerate(zip(logits, target_indices, strict=True)):
        if not row or not 0 <= target < len(row) or not np.isfinite(row).all():
            raise ValueError("Temperature fitting requires finite, unpadded valid logits")
        values[index, :len(row)] = row
    values -= values.max(axis=1, keepdims=True)
    target_logits = values[np.arange(len(values)), np.asarray(target_indices)]

    def loss(log_temperature: float) -> float:
        inverse = math.exp(-log_temperature)
        return float(np.mean(np.log(np.exp(values * inverse).sum(axis=1)) - target_logits * inverse))

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


def metrics(rows: Sequence[Prediction]) -> Metrics:
    if not rows:
        raise ValueError("Cannot score an empty evaluation")
    bins: list[list[Prediction]] = [[] for _ in range(ECE_BINS)]
    brier, nll, soft_brier, soft_nll = [], [], [], []
    for row in rows:
        index = label_index(row["options"], row["label"])
        probabilities = row["probabilities"]
        nll.append(-math.log(max(probabilities[index], 1e-12)))
        brier.append(sum((p - int(i == index)) ** 2 for i, p in enumerate(probabilities)))
        bins[min(ECE_BINS - 1, int(row["confidence"] * ECE_BINS))].append(row)
        if "soft_target" in row:
            soft = row["soft_target"]
            soft_brier.append(sum((p - q) ** 2 for p, q in zip(probabilities, soft, strict=True)))
            soft_nll.append(-sum(q * math.log(max(p, 1e-12)) for p, q in zip(probabilities, soft, strict=True)))
    reliability: list[ReliabilityBin] = []
    ece = 0.0
    for index, group in enumerate(bins):
        confidence = sum(row["confidence"] for row in group) / len(group) if group else None
        accuracy = sum(row["correct"] for row in group) / len(group) if group else None
        if confidence is not None and accuracy is not None:
            ece += len(group) / len(rows) * abs(confidence - accuracy)
        reliability.append({"lower": index / ECE_BINS, "upper": (index + 1) / ECE_BINS,
                            "count": len(group), "confidence": confidence, "accuracy": accuracy})
    result: Metrics = {
        "count": len(rows), "accuracy": sum(row["correct"] for row in rows) / len(rows),
        "ece": ece, "brier": sum(brier) / len(rows), "nll": sum(nll) / len(rows),
        "reliability": reliability,
        "zero_probability_count": sum(p == 0 for row in rows for p in row["probabilities"]),
    }
    if soft_nll:
        result.update({"soft_count": len(soft_nll), "soft_nll": sum(soft_nll) / len(soft_nll), "soft_brier": sum(soft_brier) / len(soft_brier)})
    return result


def calibration_ok(local: Metrics, reference: Metrics, margin: float = CALIBRATION_MARGIN) -> bool:
    return local["ece"] <= reference["ece"] + margin and local["brier"] <= reference["brier"] + margin


def selection_key(raw: Metrics, calibrated: Metrics, step: int) -> tuple[float, float, int]:
    """Minimize this key among checkpoints passing calibration_ok: the lowest development loss (NLL before calibration),
    then the higher calibrated accuracy, then the earlier step. Loss uses every answer's probability, so it moves far
    less between checks than accuracy on a development set of about 1,500 rows, where the highest of many noisy
    accuracies mostly picks noise."""
    return raw["nll"], -calibrated["accuracy"], step


def validate_coverage(rows: Sequence[Example], predictions: Sequence[Prediction]) -> None:
    expected = {row["id"]: row for row in rows}
    actual = {row["id"]: row for row in predictions}
    if len(expected) != len(rows) or len(actual) != len(predictions) or expected.keys() != actual.keys():
        raise ValueError("Require exactly one successful prediction for every fixed evaluation ID")
    for identifier, row in expected.items():
        saved = actual[identifier]
        rebuilt = make_prediction(row, saved["probabilities"], prediction=saved["prediction"], temperature=saved["temperature"])
        for field in ("suite", "dataset", "family", "label", "options", "correct", "confidence"):
            if saved[field] != rebuilt[field]:
                raise ValueError(f"Saved prediction differs from its evaluation example: {identifier}/{field}")


def _statistics(row: Prediction) -> FloatArray:
    """Additive sufficient statistics allow exact ECE recomputation per bootstrap draw."""
    output = np.zeros(4 + 3 * ECE_BINS, dtype=np.float64)
    index = label_index(row["options"], row["label"])
    probabilities = row["probabilities"]
    output[:4] = [1, int(row["correct"]), sum((p - int(i == index)) ** 2 for i, p in enumerate(probabilities)),
                  -math.log(max(probabilities[index], 1e-12))]
    bucket = min(ECE_BINS - 1, int(row["confidence"] * ECE_BINS))
    output[4 + 3 * bucket:7 + 3 * bucket] = [1, row["confidence"], int(row["correct"])]
    return output


def _stat_metrics(values: FloatArray) -> dict[str, FloatArray]:
    return {"accuracy": values[..., 1] / values[..., 0], "brier": values[..., 2] / values[..., 0],
            "nll": values[..., 3] / values[..., 0],
            "ece": np.abs(values[..., 5::3] - values[..., 6::3]).sum(axis=-1) / values[..., 0]}


def compare(
    local: Sequence[Prediction], reference: Sequence[Prediction], *, replicates: int = 10000,
    seed: int = 20260920, margin: float = CALIBRATION_MARGIN,
) -> Comparison:
    """Paired family bootstrap preserves each suite's fixed contribution to the headline."""
    if replicates < 1000:
        raise ValueError("Use at least 1,000 paired bootstrap draws")
    left, right = {row["id"]: row for row in local}, {row["id"]: row for row in reference}
    if not left or len(left) != len(local) or len(right) != len(reference) or left.keys() != right.keys():
        raise ValueError("Comparison requires complete unique matching prediction IDs")
    suites = sorted({row["suite"] for row in local})
    suite_index = {suite: index for index, suite in enumerate(suites)}
    counts = np.asarray([sum(row["suite"] == suite for row in local) for suite in suites], dtype=np.float64)
    families: dict[tuple[str, str], dict[int, FloatArray]] = {}
    for identifier, row in left.items():
        other = right[identifier]
        if any(row[field] != other[field] for field in ("suite", "dataset", "family", "label", "options")):
            raise ValueError(f"Paired prediction provenance differs: {identifier}")
        group = families.setdefault((row["dataset"], row["family"]), {})
        index = suite_index[row["suite"]]
        group.setdefault(index, np.zeros((2, 4 + 3 * ECE_BINS), dtype=np.float64))
        group[index] += np.stack([_statistics(row), _statistics(other)])
    strata: defaultdict[tuple[int, ...], list[dict[int, FloatArray]]] = defaultdict(list)
    for key in sorted(families):
        family = families[key]
        strata[tuple(sorted(family))].append(family)
    rng = np.random.default_rng(seed)
    samples = np.zeros((replicates, len(suites), 2, 4 + 3 * ECE_BINS), dtype=np.float64)
    for membership in sorted(strata):
        clusters = strata[membership]
        values = np.stack([np.stack([cluster[index] for index in membership]) for cluster in clusters])
        flattened = values.reshape(len(clusters), -1)
        for start in range(0, replicates, 128):
            size = min(128, replicates - start)
            draws = rng.multinomial(len(clusters), np.full(len(clusters), 1 / len(clusters)), size=size)
            totals = (draws @ flattened).reshape(size, len(membership), 2, -1)
            for position, index in enumerate(membership):
                samples[start:start + size, index] += totals[:, position]
    # Whole-family draws vary record counts. Retain the predeclared task mixture.
    weights = counts[None, :, None, None] / samples[..., :1]
    pooled = (samples * weights).sum(axis=1)
    pooled_metrics = _stat_metrics(pooled)
    suite_metrics = _stat_metrics(samples)

    def effects(a: Sequence[Prediction], b: Sequence[Prediction], draws: Mapping[str, FloatArray]) -> dict[str, Effect]:
        a_metrics, b_metrics = metrics(a), metrics(b)
        result: dict[str, Effect] = {}
        for name in ("accuracy", "ece", "brier", "nll"):
            a_value = a_metrics[name]
            b_value = b_metrics[name]
            difference = draws[name][..., 0] - draws[name][..., 1]
            result[name] = {"local": a_value, "reference": b_value, "difference": a_value - b_value,
                            "difference_ci95": np.quantile(difference, [0.025, 0.975]).tolist()}
        return result

    overall = effects(local, reference, pooled_metrics)
    by_suite = {suite: effects([r for r in local if r["suite"] == suite], [r for r in reference if r["suite"] == suite],
                              {name: values[:, index] for name, values in suite_metrics.items()})
                for index, suite in enumerate(suites)}
    return {
        "count": len(local), "families": len(families), "effects": overall, "by_suite": by_suite,
        "empirical_target_met": overall["accuracy"]["difference"] > 0 and all(overall[name]["difference"] <= margin for name in ("ece", "brier")),
        "statistically_supported": overall["accuracy"]["difference_ci95"][0] > 0 and all(overall[name]["difference_ci95"][1] <= margin for name in ("ece", "brier")),
        "calibration_margin": margin, "bootstrap_replicates": replicates, "bootstrap_seed": seed,
    }


def saved_orders(predictions: Sequence[Prediction]) -> int:
    """How many option orders saved predictions averaged: 2 when every row kept its reversed-pass logits."""
    twice = sum("reversed_logits" in row for row in predictions)
    if twice not in (0, len(predictions)):
        raise ValueError(f"{twice} of {len(predictions)} saved predictions averaged two option orders; expected all or none")
    return 2 if twice else 1


def read_rows(path: Path) -> list[Example]:
    with path.open(encoding="utf-8") as stream:
        return [cast(Example, json.loads(line)) for line in stream if line.strip()]


def read_predictions(path: Path) -> list[Prediction]:
    with path.open(encoding="utf-8") as stream:
        return [cast(Prediction, json.loads(line)) for line in stream if line.strip()]


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False, allow_nan=False) + "\n")


class Arguments(argparse.Namespace):
    data: Path
    output: Path
    predictions: Path | None
    reference: Path | None
    local: bool
    checkpoint: Path | None
    base_model: str | None
    revision: str | None
    calibration: Path | None
    batch_size: int
    temperature: float | None
    orders: int


def parse_arguments(argv: list[str] | None = None) -> Arguments:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True, help="JSON summary; predictions use the same stem")
    parser.add_argument("--predictions", type=Path, help="Recompute saved predictions without inference")
    parser.add_argument("--reference", type=Path, help="Paired reference predictions (for example another model's saved predictions)")
    parser.add_argument("--local", action="store_true", help="Evaluate the pinned Qwen base or a local checkpoint")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--base-model", help="Hugging Face ID of an untrained base to score (with --local and no --checkpoint)")
    parser.add_argument("--revision", help="40-character commit of --base-model")
    parser.add_argument("--calibration", type=Path, help="Fit the temperature on these rows, then score --data with it (untrained base only)")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--temperature", type=float, help="Override the checkpoint temperature; use 1 for raw results")
    parser.add_argument("--orders", type=int, choices=(1, 2), default=1,
                        help="1: score the options in the given order. 2: also score them reversed and average the two "
                             "probability distributions (reduces position bias; twice the cost)")
    args = parser.parse_args(argv, namespace=Arguments())
    if args.local == (args.predictions is not None):
        parser.error("Choose exactly one of --local or --predictions")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    if not args.local and (args.checkpoint is not None or args.temperature is not None or args.orders != 1):
        parser.error("--checkpoint, --temperature and --orders require --local")
    if bool(args.base_model) != bool(args.revision):
        parser.error("--base-model and --revision must be supplied together")
    if args.base_model and (not args.local or args.checkpoint is not None):
        parser.error("--base-model applies to --local without --checkpoint")
    if args.calibration is not None and (not args.local or args.checkpoint is not None or args.temperature is not None):
        parser.error("--calibration applies to --local without --checkpoint or --temperature")
    return args


def model_kwargs(args: Arguments) -> dict[str, str]:
    return {"base_model": args.base_model, "revision": cast(str, args.revision)} if args.base_model else {}


def main() -> None:
    args = parse_arguments()
    rows = read_rows(args.data)
    identity = {"dataset_sha256": hashlib.sha256(args.data.read_bytes()).hexdigest(), "ece_bins": ECE_BINS,
                "evaluator_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    if args.predictions is not None:
        predictions = read_predictions(args.predictions)
    else:
        import torch
        from jeff.models import load_decision_model

        destination = args.output.with_suffix(".predictions.jsonl")
        if destination.exists():
            raise FileExistsError(f"Refusing to overwrite an evaluation: {destination}")
        model = load_decision_model(checkpoint=args.checkpoint, **model_kwargs(args))
        scale = model.temperature if args.temperature is None else args.temperature

        @torch.inference_mode()
        def infer(batch_rows: Sequence[Example]) -> list[list[float]]:
            output: list[list[float]] = []
            for start in range(0, len(batch_rows), args.batch_size):
                batch = model.prepare(batch_rows[start:start + args.batch_size])
                logits: list[list[float]] = model(batch).cpu().tolist()
                output.extend(values[:count] for values, count in zip(logits, batch.counts, strict=True))
            return output

        if args.calibration is not None:
            calibration_rows = read_rows(args.calibration)
            targets = [label_index(options(row["question"]), hard_label(row)) for row in calibration_rows]
            calibration_logits = list(infer(calibration_rows))
            if args.orders == 2:  # one temperature for both orders: fit it on both passes, targets put in reverse too
                calibration_logits += infer([reverse_row(row) for row in calibration_rows])
                targets += [len(options(row["question"])) - 1 - target for row, target in zip(calibration_rows, targets)]
            scale = fit_temperature(calibration_logits, targets)
            identity["calibration_sha256"] = hashlib.sha256(args.calibration.read_bytes()).hexdigest()
        predictions = predict_local(rows, infer, scale, args.orders)
        identity.update({"model": model.base_model, "revision": model.revision, "temperature": scale, "orders": args.orders,
                         "checkpoint": str(args.checkpoint.resolve()) if args.checkpoint else None, "endpoint": "local"})
        if args.checkpoint is not None:
            identity["checkpoint_config_sha256"] = hashlib.sha256((args.checkpoint / "decision_config.json").read_bytes()).hexdigest()
        write_json(args.output.with_suffix(".manifest.json"), identity)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in predictions))
    validate_coverage(rows, predictions)
    result: dict[str, object] = {
        "created": datetime.now(timezone.utc).isoformat(), "dataset_sha256": identity["dataset_sha256"],
        "inference": identity if args.predictions is None else {"predictions_sha256": hashlib.sha256(args.predictions.read_bytes()).hexdigest()},
        "overall": metrics(predictions), "by_suite": {suite: metrics([row for row in predictions if row["suite"] == suite])
                                                     for suite in sorted({row["suite"] for row in predictions})},
        "ece_bins": ECE_BINS, "labels": "hard reference labels; soft diagnostics are separate",
        "orders": args.orders if args.predictions is None else saved_orders(predictions),
        "observed_models": sorted({row["model"] for row in predictions if "model" in row}),
        "observed_providers": sorted({row["provider"] for row in predictions if "provider" in row}),
    }
    if args.reference is not None:
        reference = read_predictions(args.reference)
        validate_coverage(rows, reference)
        result["comparison"] = compare(predictions, reference)
    write_json(args.output, result)
    print(json.dumps(result["overall"], indent=2))


if __name__ == "__main__":
    main()
