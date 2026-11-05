"""What happens when a decision abstains: return it, raise, or escalate through explicit steps (spec Section 8).
Every escalated Decision says who answered and lists every earlier attempt; a failing step raises."""

import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, Literal, Protocol

from jeff.binding import build_row, option_keys
from jeff.decision import Decision, EscalationRecord, Value
from jeff.errors import (EscalationBudgetExceeded, EscalationFailed, InterfaceMismatch, InvalidPolicy, LowConfidence,
                         NotInstalled, UnknownFunction)

if TYPE_CHECKING:
    from jeff.client import Call, Jeff


@dataclass(frozen=True)
class Question:
    """What a human or llm escalation step is shown: the function, its instructions, the state the model read and
    the option keys it may answer with (each with its description, or None). `state` is an object of named fields
    (the context and the input under their exact keys) for an input of form fields, and the plain input string
    itself for an input of form text."""
    function: str
    instructions: str
    state: dict[str, Any] | str
    options: dict[str, str | None]


class Decider(Protocol):
    def decide(self, calls: list["Call"]) -> list[Decision]: ...


@dataclass(frozen=True)
class FunctionStep:
    ref: str
    name: str = "function"


@dataclass(frozen=True)
class RemoteStep:
    client: Decider
    name: str = "remote"


@dataclass(frozen=True)
class AskStep:
    ask: Callable[[Question], str]
    name: str  # "llm" or "human"


type Step = FunctionStep | RemoteStep | AskStep


@dataclass(frozen=True)
class Escalate:
    steps: tuple[Step, ...]
    budget_ms: float | None


type Policy = Literal["return", "error"] | Escalate


def function_step(ref: str) -> FunctionStep:
    return FunctionStep(ref)


def remote(client: Decider) -> RemoteStep:
    return RemoteStep(client)


def llm(ask: Callable[[Question], str]) -> AskStep:
    return AskStep(ask, "llm")


def human(ask: Callable[[Question], str]) -> AskStep:
    return AskStep(ask, "human")


def escalate(*steps: Step, budget_ms: float | None = None) -> Escalate:
    if not steps:
        raise InvalidPolicy("escalate() needs at least one step")
    return Escalate(tuple(steps), budget_ms)


def check_policy(jeff: "Jeff", interface: str, policy: object) -> None:
    """Check an on_low_confidence policy before anything is decided, so a typo or a broken escalation step fails on
    every call, not only on the first one that abstains. `interface` is the interface the call is answered through;
    every function step must be in the catalogue, installed, and implement that same interface."""
    if isinstance(policy, str) and policy in ("return", "error"):
        return
    if not isinstance(policy, Escalate):
        raise InvalidPolicy(f"Unknown on_low_confidence policy {policy!r}; use 'return', 'error' or escalate(...)")
    for step in policy.steps:
        if isinstance(step, (RemoteStep, AskStep)):
            continue
        if not isinstance(step, FunctionStep):
            raise InvalidPolicy(f"escalate() got {step!r}, which is not a step; use function_step(...), remote(...), "
                                "llm(...) or human(...)")
        try:
            spec = jeff.catalogue.function(step.ref)
        except UnknownFunction as error:
            raise InvalidPolicy(f"Escalation step function_step({step.ref!r}): {error}") from error
        # The caller's authorise hook (licences) sees every step up front, so a refused step stops the call, the
        # dispatch or the graph before anything is decided; first, as in Jeff._binding, so a caller without the
        # licence never learns whether the step's adapter is installed.
        if jeff.authorise is not None:
            jeff.authorise(spec)
        if spec.adapter_id not in jeff.engine.installed():
            raise NotInstalled(spec.id, "it is an escalation step")
        if spec.interface != interface:
            raise InterfaceMismatch(f"Escalation step {spec.id} implements {spec.interface}, not {interface}")


def _reason(decision: Decision, threshold: float | None) -> str:
    if threshold is not None and decision.confidence is not None and decision.confidence < threshold:
        return f"abstained (confidence {decision.confidence:.3f} < threshold {threshold})"
    return "abstained (abstain label)"


def _record(jeff: "Jeff", decision: Decision) -> EscalationRecord:
    calibration = jeff.calibration_of(jeff.catalogue.function(decision.function))
    return EscalationRecord(decision.decided_by, decision.value, decision.confidence,
                            _reason(decision, calibration.threshold if calibration else None))


def _question(jeff: "Jeff", call: "Call") -> tuple[Question, dict[str, str | None]]:
    binding = jeff._binding(call)
    function = binding.function
    row = build_row(binding, call.input)
    if function.type == "choice":
        options = {binding.wire_to_caller[key]: (binding.criteria or {})[key] for key in option_keys(binding)}
    elif function.type == "score":
        options = {str(index): level for index, level in enumerate(function.labels.levels)}
    else:
        # the same wording the engine shows the model when a noul function does not describe its answers
        # (jeff_runtime.engine.model.options)
        described = function.labels.fixed
        options = {"true": described["true"] if "true" in described else "Yes / true",
                   "false": described["false"] if "false" in described else "No / false"}
    return Question(function.id, function.instructions, row["state"], options), options


def _convert(function_type: str, answer: str) -> Value:
    if function_type == "noul":
        return answer == "true"
    if function_type == "score":
        return int(answer)
    return answer


def _run_step(jeff: "Jeff", call: "Call", original: Decision, step: Step) -> Decision:
    from jeff.client import Call
    if isinstance(step, FunctionStep):
        spec = jeff.catalogue.function(step.ref)
        if spec.interface != original.interface:
            raise InterfaceMismatch(f"Escalation step {spec.id} implements {spec.interface}, not {original.interface}")
        return jeff.decide([Call(step.ref, call.input, call.context, call.labels)])[0]
    if isinstance(step, RemoteStep):
        return step.client.decide([call])[0]
    question, options = _question(jeff, call)
    started = time.perf_counter()
    answer = step.ask(question)
    elapsed = round((time.perf_counter() - started) * 1000, 3)
    if not isinstance(answer, str) or answer not in options:
        raise EscalationFailed(step.name, f"answered {answer!r}, which is not one of {list(options)}")
    function = jeff.catalogue.function(call.function)
    return replace(original, value=_convert(function.type, answer), confidence=None, abstained=False,
                   calibrated=False, probabilities={}, score=None, decided_by=step.name, escalations=(),
                   timing={"path": step.name, "ms": elapsed})


def apply_policy(jeff: "Jeff", call: "Call", decision: Decision, policy: Policy) -> Decision:
    if not decision.abstained or policy == "return":
        return decision
    if policy == "error":
        raise LowConfidence(decision)
    if not isinstance(policy, Escalate):
        raise InvalidPolicy(f"Unknown on_low_confidence policy {policy!r}; use 'return', 'error' or escalate(...)")
    started = time.perf_counter()
    attempts = [_record(jeff, decision)]
    latest = decision
    for step in policy.steps:
        # This process's own authorise hook refusing a function step is the caller's licence, not a failing step:
        # it is checked here, outside the try below, so it stays NotLicensed. A NotLicensed raised inside the step
        # (a remote server refusing, the caller's own callable) is that step failing.
        if isinstance(step, FunctionStep) and jeff.authorise is not None:
            try:
                spec = jeff.catalogue.function(step.ref)
            except UnknownFunction as error:
                raise EscalationFailed(step.name, f"{type(error).__name__}: {error}") from error
            jeff.authorise(spec)
        try:
            latest = _run_step(jeff, call, decision, step)
        except EscalationFailed:
            raise
        except Exception as error:  # noqa: BLE001 — re-raised as EscalationFailed with the cause chained
            raise EscalationFailed(step.name, f"{type(error).__name__}: {error}") from error
        if policy.budget_ms is not None and (time.perf_counter() - started) * 1000 > policy.budget_ms:
            raise EscalationBudgetExceeded(f"Escalation of {decision.function} took longer than {policy.budget_ms} ms")
        if not latest.abstained:
            final = replace(latest, route=decision.route, escalations=tuple(attempts))
            if jeff.log is not None:
                jeff.log.write(call, final)
            return final
        attempts.append(_record(jeff, latest))
    final = replace(latest, route=decision.route, escalations=tuple(attempts[:-1]))
    if jeff.log is not None:
        jeff.log.write(call, final)
    return final
