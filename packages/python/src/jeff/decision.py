"""The Decision every function returns (spec Section 7)."""

import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from jeff.binding import Binding, option_keys
from jeff.errors import EngineError

type Value = str | bool | int


@dataclass(frozen=True)
class RouteStep:
    router: str
    chose: str
    confidence: float


@dataclass(frozen=True)
class EscalationRecord:
    decided_by: str
    value: Value
    confidence: float | None
    reason: str


@dataclass(frozen=True)
class Decision:
    value: Value
    confidence: float | None
    abstained: bool
    calibrated: bool
    interface: str
    function: str
    version: str
    probabilities: dict[str, float]
    score: float | None
    route: tuple[RouteStep, ...]
    decided_by: str
    escalations: tuple[EscalationRecord, ...]
    timing: dict[str, Any]
    evidence_refs: tuple[str, ...] = field(default=())

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["route"] = [asdict(step) for step in self.route]
        data["escalations"] = [asdict(item) for item in self.escalations]
        data["evidence_refs"] = list(self.evidence_refs)
        return data

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Decision":
        values = dict(data)
        values["route"] = tuple(RouteStep(**step) for step in data["route"])
        values["escalations"] = tuple(EscalationRecord(**item) for item in data["escalations"])
        values["evidence_refs"] = tuple(data["evidence_refs"])
        return cls(**values)


SUM_TOLERANCE = 1e-4
"""How far from 1 the sum of an engine reply's probabilities may be (contract/socket.md, "The probabilities of a
predict"). The engine's softmax runs in float32 over at most 255 options, so a sum is off by at most about
255 * 2**-24 (1.5e-5); the engine's own tests hold it to 1e-4 (runtime/python/tests/test_local_engine.py)."""


def checked_probabilities(probabilities: Sequence[float], option_count: int, name: str) -> list[float]:
    """The one shared check every caller that turns raw engine probabilities into an answer uses (contract/socket.md,
    "The probabilities of a predict"; owner's decision, 3 November 2026): a reply that breaks the engine's contract
    is refused with EngineError, naming `name` (the function or question the reply was for), never repaired. Refused,
    in order: the wrong number of probabilities; any that is not a finite number from 0 to 1 (listing the offending
    values); a sum more than SUM_TOLERANCE from 1. A valid reply is divided by its sum."""
    if len(probabilities) != option_count:
        raise EngineError(f"{name}: the engine returned {len(probabilities)} probabilities for {option_count} options")
    offending = [p for p in probabilities if not (math.isfinite(p) and 0 <= p <= 1)]
    if offending:
        raise EngineError(f"{name}: the engine returned invalid probabilities {list(probabilities)}; expected "
                          f"each to be a finite number from 0 to 1, which {offending} are not")
    total = sum(probabilities)
    if abs(total - 1) > SUM_TOLERANCE:
        raise EngineError(f"{name}: the engine returned invalid probabilities {list(probabilities)}; expected "
                          f"their sum to be within {SUM_TOLERANCE} of 1, got {total}")
    return [p / total for p in probabilities]


def make_decision(binding: Binding, probabilities: Sequence[float], timing: Mapping[str, Any]) -> Decision:
    """The Decision for one row from the engine's probabilities, one per option in option order. A reply that breaks
    the engine's contract is refused with EngineError, never repaired (checked_probabilities). A valid reply is
    divided by its sum."""
    function, manifest = binding.function, binding.manifest
    keys = option_keys(binding)
    normalised = checked_probabilities(probabilities, len(keys), function.id)
    caller = {binding.wire_to_caller[key]: p for key, p in zip(keys, normalised, strict=True)}
    score: float | None = None
    value: Value
    if function.type == "noul":
        value = caller["true"] >= 0.5
        confidence = caller["true"] if value else caller["false"]
    elif function.type == "score":
        best = max(range(len(normalised)), key=normalised.__getitem__)
        value, confidence = best, normalised[best]
        score = sum(index * p for index, p in enumerate(normalised))
    else:
        best_key = max(caller, key=caller.__getitem__)
        value, confidence = best_key, caller[best_key]
    calibration = binding.calibration  # the engine's base format's block (Jeff._binding)
    abstained = (calibration is not None and confidence < calibration.threshold) or \
        (isinstance(value, str) and value in function.abstain_labels)
    return Decision(value=value, confidence=confidence, abstained=abstained, calibrated=calibration is not None,
                    interface=function.interface, function=function.id, version=manifest.version,
                    probabilities=caller, score=score, route=(), decided_by=f"{function.id}@{manifest.version}",
                    escalations=(), timing=dict(timing))
