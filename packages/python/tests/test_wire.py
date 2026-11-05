import pytest

from jeff.decision import Decision
from jeff.errors import (Busy, EngineError, EscalationFailed, InputTooLong, LowConfidence, MissingContext,
                         NotInstalled, UsageLimitReached)
from jeff.wire import error_from_wire, error_to_wire

DECISION = Decision(value=2, confidence=0.4, abstained=True, calibrated=True, interface="rate",
                    function="jeff.support.triage.urgency", version="1.3.0",
                    probabilities={"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.2, "4": 0.1}, score=2.0, route=(),
                    decided_by="jeff.support.triage.urgency@1.3.0", escalations=(), timing={"path": "mlx"})


@pytest.mark.parametrize("error", [
    NotInstalled("jeff.support.triage.route", "router X routes to it"),
    MissingContext("jeff.support.triage.route", ["company", "channel"]),
    EscalationFailed("human", "TimeoutError: queue down"),
    InputTooLong("Question branch exceeds the 8192-token limit; no input was truncated."),
    Busy("The engine queue is full (4096 rows)", 1.5),
    UsageLimitReached("This account has used its 50,000,000 input tokens for the period ending 2026-12-03 10:00 UTC; "
                      "calls resume then", "u_1", 50_000_123, 50_000_000, "2026-12-03T10:00:00Z"),
])
def test_round_trip_keeps_class_message_and_attributes(error: Exception) -> None:
    body = error_to_wire(error)  # type: ignore[arg-type]
    assert body["type"] == type(error).__name__ and body["message"] == str(error)
    back = error_from_wire(body)
    assert type(back) is type(error) and str(back) == str(error) and vars(back) == vars(error)


def test_low_confidence_carries_its_decision() -> None:
    back = error_from_wire(error_to_wire(LowConfidence(DECISION)))
    assert isinstance(back, LowConfidence) and back.decision == DECISION


def test_unknown_type_is_an_engine_error_naming_it() -> None:
    back = error_from_wire({"type": "Gremlins", "message": "boom", "details": {}})
    assert isinstance(back, EngineError) and "Gremlins" in str(back) and "boom" in str(back)


def test_wire_body_is_plain_data() -> None:
    import json
    json.dumps(error_to_wire(LowConfidence(DECISION)))


def test_python_internal_attributes_such_as_notes_are_not_details() -> None:
    error = NotInstalled("jeff.support.triage.route", "router X routes to it")
    error.add_note("seen while serving request 7")
    body = error_to_wire(error)
    assert "__notes__" not in body["details"]
    assert body["details"] == {"function_id": "jeff.support.triage.route"}
