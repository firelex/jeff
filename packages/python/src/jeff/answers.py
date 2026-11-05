"""Each question's answer from its option probabilities, as jeff-server's /v1/systemone gives it (ported from
jeff-server's jeff.model: options, describe and answer). Pure: no model, no numpy.

A question is a choice (pick one of the criteria keys), a score (a point on the criteria list, lowest first) or a
noul (the probability that the answer is yes). The option keys and descriptions here are the ones the engine lists
in the prompt (jeff_runtime.engine.model.options), in the same order."""

import json
from collections.abc import Sequence
from typing import Any

from jeff.decision import checked_probabilities


def describe(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)


def options(question: dict[str, Any]) -> tuple[list[str], list[Any]]:
    """The question's option keys and descriptions, in the order they are listed. A noul question's pair is
    false, true; "true_first" (set only by jeff.orders for the reversed pass) lists true first."""
    if question["type"] == "choice":
        criteria = question["criteria"]
        return list(criteria), [key if value is None else f"{key}: {describe(value)}"
                                for key, value in criteria.items()]
    if question["type"] == "score":
        return [str(i) for i in range(len(question["criteria"]))], list(question["criteria"])
    if question["type"] != "noul":
        raise ValueError(f"Unknown question type {question['type']!r}; use choice, score or noul")
    criteria = question.get("criteria") or {}
    keys = ["false", "true"]
    descriptions = [criteria.get("false") or "No / false", criteria.get("true") or "Yes / true"]
    if question.get("true_first"):
        return keys[::-1], descriptions[::-1]
    return keys, descriptions


def answer(question: dict[str, Any], probabilities: Sequence[float]) -> dict[str, Any]:
    """The answer to one question. The probabilities (one per option, in the listed order) must fit the engine's
    contract (jeff.decision.checked_probabilities: exactly one per option, each finite from 0 to 1, summing to
    within SUM_TOLERANCE of 1) and are refused with EngineError, never repaired, when they do not; a valid reply is
    divided by its sum. choice: the most likely key, every key's probability, and a confidence from 0 (no better
    than a uniform guess) to 1 (certain). score: the expected level (0 is the first), each level's probability, the
    legend, and a confidence of 1 minus the expected distance from the most likely level over that distance for a
    uniform spread. noul: the probability of true."""
    keys, descriptions = options(question)
    values = checked_probabilities([float(value) for value in probabilities], len(keys),
                                   f"a {question['type']} question")
    if question["type"] == "noul":
        return {"type": "noul", "noul": values[keys.index("true")]}
    best = max(range(len(values)), key=values.__getitem__)
    distribution = dict(zip(keys, values, strict=True))
    if question["type"] == "choice":
        confidence = 1.0 if len(values) == 1 else (values[best] - 1 / len(values)) / (1 - 1 / len(values))
        return {"type": "choice", "probabilities": distribution, "choice": keys[best],
                "confidence": max(0.0, min(1.0, confidence))}
    if len(values) < 2:
        raise ValueError("Score questions require at least two levels.")
    distance = sum(probability * abs(i - best) for i, probability in enumerate(values))
    midpoint = (len(values) - 1) / 2
    baseline = sum(abs(i - midpoint) for i in range(len(values))) / len(values)
    return {"type": "score", "probabilities": distribution, "legend": dict(zip(keys, descriptions, strict=True)),
            "score": sum(i * probability for i, probability in enumerate(values)),
            "confidence": max(0.0, 1.0 - distance / baseline)}
