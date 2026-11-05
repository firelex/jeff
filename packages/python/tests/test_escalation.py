import time
from pathlib import Path

import pytest

from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.errors import (EscalationBudgetExceeded, EscalationFailed, InterfaceMismatch, InvalidPolicy, LowConfidence,
                         NotInstalled)
from jeff.escalation import Question, escalate, function_step, human, llm, remote
from jeff.testing import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
UNSURE = [0.0, 0.5, 0.5, 0.0, 0.0]      # below urgency's 0.6 threshold
SURE = [0.0, 0.0, 0.95, 0.05, 0.0]


def jeff_with(levels: list[float]) -> Jeff:
    return Jeff(FakeEngine(lambda adapter, row: levels, {"jeff.support.triage"}), Catalogue.from_dir(FIXTURES))


def test_confident_answer_skips_escalation() -> None:
    called = []
    decision = jeff_with(SURE).rate("x", function="triage.urgency", context=CONTEXT,
                                    on_low_confidence=escalate(human(lambda q: called.append(q) or "4")))
    assert decision.value == 2 and called == []


def test_remote_step_answers_and_records_the_attempt() -> None:
    sure = jeff_with(SURE)
    decision = jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT,
                                      on_low_confidence=escalate(remote(sure)))
    assert decision.value == 2 and decision.abstained is False
    assert len(decision.escalations) == 1
    assert decision.escalations[0].decided_by == "jeff.support.triage.urgency@1.3.0"
    assert "abstained" in decision.escalations[0].reason


def test_llm_answer_is_held_to_the_options_and_has_no_confidence() -> None:
    seen: list[Question] = []

    def ask(question: Question) -> str:
        seen.append(question)
        return "3"

    decision = jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT,
                                      on_low_confidence=escalate(llm(ask)))
    assert decision.value == 3 and decision.confidence is None and decision.decided_by == "llm"
    assert seen[0].options["4"] == "Handle immediately"
    assert seen[0].state["message"] == "x"


def test_answer_outside_the_options_fails() -> None:
    with pytest.raises(EscalationFailed, match="'7'"):
        jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT,
                               on_low_confidence=escalate(llm(lambda q: "7")))


def test_a_failing_step_raises_never_returns_the_low_answer() -> None:
    def broken(question: Question) -> str:
        raise TimeoutError("queue down")

    with pytest.raises(EscalationFailed, match="human.*queue down"):
        jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT,
                               on_low_confidence=escalate(human(broken)))


def test_all_steps_abstain_returns_last_with_history() -> None:
    decision = jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT,
                                      on_low_confidence=escalate(function_step("triage.urgency")))
    assert decision.abstained is True and len(decision.escalations) == 1


def test_budget() -> None:
    def slow(question: Question) -> str:
        time.sleep(0.05)
        return "2"

    with pytest.raises(EscalationBudgetExceeded):
        jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT,
                               on_low_confidence=escalate(human(slow), budget_ms=10))


def test_error_policy() -> None:
    with pytest.raises(LowConfidence):
        jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT, on_low_confidence="error")


def test_unknown_policy_raises_invalid_policy() -> None:
    with pytest.raises(InvalidPolicy):
        jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT, on_low_confidence="retry")


def test_an_invalid_policy_is_refused_even_when_the_answer_is_confident() -> None:
    jeff = jeff_with(SURE)
    with pytest.raises(InvalidPolicy, match="eror"):
        jeff.rate("x", function="triage.urgency", context=CONTEXT, on_low_confidence="eror")  # type: ignore[arg-type]
    assert jeff.engine.calls == []  # type: ignore[attr-defined]
    with pytest.raises(InvalidPolicy, match="eror"):
        jeff.function("triage.urgency", context=CONTEXT, on_low_confidence="eror")  # type: ignore[arg-type]


def test_an_unknown_function_step_is_refused_at_bind_and_call_time() -> None:
    jeff = jeff_with(SURE)
    policy = escalate(function_step("nope.zzz"))
    with pytest.raises(InvalidPolicy, match="nope.zzz"):
        jeff.function("triage.urgency", context=CONTEXT, on_low_confidence=policy)
    with pytest.raises(InvalidPolicy, match="nope.zzz"):
        jeff.rate("x", function="triage.urgency", context=CONTEXT, on_low_confidence=policy)
    assert jeff.engine.calls == []  # type: ignore[attr-defined]


def test_a_function_step_must_be_installed_and_implement_the_same_interface() -> None:
    jeff = jeff_with(SURE)
    with pytest.raises(NotInstalled, match="jeff.safety.spam"):
        jeff.function("triage.urgency", context=CONTEXT, on_low_confidence=escalate(function_step("spam")))
    with pytest.raises(InterfaceMismatch, match="needs_human.*requires_escalation.*rate"):
        jeff.function("triage.urgency", context=CONTEXT,
                      on_low_confidence=escalate(function_step("triage.needs_human")))


def test_a_step_that_is_not_a_step_is_refused() -> None:
    with pytest.raises(InvalidPolicy, match="step"):
        jeff_with(SURE).function("triage.urgency", context=CONTEXT,
                                 on_low_confidence=escalate("triage.urgency"))  # type: ignore[arg-type]


@pytest.mark.parametrize("make_step,path", [(llm, "llm"), (human, "human")])
def test_asked_answers_are_uncalibrated_with_their_own_timing(make_step, path: str) -> None:
    decision = jeff_with(UNSURE).rate("x", function="triage.urgency", context=CONTEXT,
                                      on_low_confidence=escalate(make_step(lambda q: "1")))
    assert decision.decided_by == path and decision.calibrated is False
    assert set(decision.timing) == {"path", "ms"} and decision.timing["path"] == path
    assert decision.timing["ms"] >= 0
