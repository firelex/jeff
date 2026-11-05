"""Every error Jeff raises. All are JeffError subclasses and every message names what is missing or wrong."""

from typing import Any

__all__ = ["BackendUnavailable", "Busy", "CalibrationError", "Conflict", "ContractError", "DeadlineExceeded",
           "EngineError", "EscalationBudgetExceeded", "EscalationFailed", "FactoryOff", "GraphError", "InputTooLong",
           "InterfaceMismatch",
           "InvalidLabels", "InvalidPolicy", "InvalidReference", "InvalidRequest", "JeffError", "LowConfidence",
           "ManifestError", "MissingContext", "MissingLabels", "NotInstalled", "NotLicensed", "NotReady",
           "ProprietaryWeights", "ProtocolMismatch", "RateLimited", "RuntimeMissing", "TooManyHandles", "TooManyLabels",
           "Unauthorised", "UnknownContext", "UnknownFunction", "UsageLimitReached"]


class JeffError(Exception):
    """Base class of every Jeff error. request_id: the id a Jeff server gave the request that failed (its
    x-request-id), set by the remote client; None for an error raised in this process."""

    request_id: str | None = None


class ContractError(JeffError):
    """A document does not match its contract schema."""


class ManifestError(JeffError):
    """An adapter manifest is invalid or inconsistent with the base or the interface catalogue."""


class InvalidReference(JeffError):
    """A reference is not of the form jeff.<domain>.<name>[.<function>][@<version>] or <alias>[.<function>]."""


class UnknownFunction(JeffError):
    """No function or adapter in the catalogue has this ID, alias or version."""


class NotInstalled(JeffError):
    def __init__(self, function_id: str, why: str = "") -> None:
        adapter = function_id.rsplit(".", 1)[0] if function_id.count(".") >= 3 else function_id
        super().__init__(f"{function_id} is not installed{(': ' + why) if why else ''}. Install it with: jeff pull {adapter}")
        self.function_id = function_id


class MissingContext(JeffError):
    def __init__(self, function_id: str, fields: list[str]) -> None:
        super().__init__(f"{function_id} needs context field(s) {', '.join(fields)}; pass them in context=")
        self.function_id, self.fields = function_id, fields


class MissingLabels(JeffError):
    def __init__(self, function_id: str) -> None:
        super().__init__(f"{function_id} takes its labels from the caller; pass labels=")
        self.function_id = function_id


class InvalidLabels(JeffError):
    """Labels given where the function's labels are fixed, or keys that collide or are malformed."""


class TooManyLabels(JeffError):
    """More labels than the function was trained on."""


class TooManyHandles(JeffError):
    """A channel connection already holds the most bound-function handles it may (1,024); release one first."""


class UnknownContext(JeffError):
    """Context fields the function does not have."""


class InterfaceMismatch(JeffError):
    """A function was called through an interface it does not implement."""


class LowConfidence(JeffError):
    def __init__(self, decision: Any) -> None:
        super().__init__(f"{decision.function} abstained (confidence {decision.confidence}); on_low_confidence='error'")
        self.decision = decision


class EscalationFailed(JeffError):
    def __init__(self, step: str, reason: str) -> None:
        super().__init__(f"Escalation step {step} failed: {reason}")
        self.step = step


class EscalationBudgetExceeded(JeffError):
    """The escalation ran past its budget_ms."""


class InvalidPolicy(JeffError):
    """on_low_confidence is not 'return', 'error', or an escalate(...)."""


class GraphError(JeffError):
    """A decision graph is invalid or cannot run."""


class RuntimeMissing(JeffError):
    """Local mode needs the jeff-runtime package."""


class ProprietaryWeights(JeffError):
    def __init__(self, adapter_id: str) -> None:
        super().__init__(f"{adapter_id} is proprietary: its weights download only through JeffHub with a licence "
                         "(`jeff login`, then `jeff pull`), or pass its folder in paths=")
        self.adapter_id = adapter_id


class CalibrationError(JeffError):
    """Calibration cannot reach the target or has no data."""


class EngineError(JeffError):
    """The engine returned output that breaks its contract (wrong number of probabilities, invalid values)."""


class InputTooLong(JeffError):
    """The request is longer than the engine's token limit; it was refused, never truncated."""


class BackendUnavailable(JeffError):
    """The requested engine backend cannot run on this machine (no NVIDIA GPU, or mlx not installed)."""


class Busy(JeffError):
    """The engine queue is full; retry after `retry_after_s` seconds (the engine's estimate of when there will be
    room; it travels over the wire with the error)."""

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class DeadlineExceeded(JeffError):
    """The request's deadline passed before it ran."""


class ProtocolMismatch(JeffError):
    """The two ends of the engine socket speak different protocol versions."""


class NotReady(JeffError):
    """The engine is still starting."""


class Unauthorised(JeffError):
    """Missing or invalid credentials."""


class NotLicensed(JeffError):
    """The caller's token does not include this proprietary function's licence."""


class RateLimited(JeffError):
    """The caller exceeded its rate limit; retry after `retry_after_s` seconds (it travels over the wire with the
    error, and a server sends it as Retry-After)."""

    def __init__(self, message: str, retry_after_s: float) -> None:
        super().__init__(message)
        self.retry_after_s = retry_after_s


class UsageLimitReached(JeffError):
    """The account has used its input-token allowance for the current billing period (HTTP 402). Calls resume when
    the next period starts, at `period_end` (ISO 8601, UTC). `used` and `allowance` are input tokens; `account` is
    the account's id on the hub. It travels over the wire with these four details."""

    def __init__(self, message: str, account: str, used: int, allowance: int, period_end: str) -> None:
        super().__init__(message)
        self.account, self.used, self.allowance, self.period_end = account, used, allowance, period_end


class InvalidRequest(JeffError):
    """The request does not match the API schema."""


class Conflict(JeffError):
    """The request does not fit the current state of the factory run (for example, approving a spec twice, or
    queueing a step for a run that already has a job waiting)."""


class FactoryOff(JeffError):
    """The factory is off on this server (JEFF_FACTORY=off), or the request needs a part of it this server does not
    run."""
