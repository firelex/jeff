from pathlib import Path

import pytest

from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.errors import InterfaceMismatch, LowConfidence, NotInstalled
from jeff.testing import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
TEAMS = {"deliveries": "Late or damaged deliveries", "refunds": "Refunds and payments"}


def answers(adapter: str, row: dict) -> list[float]:
    question = row["question"]
    if question["type"] == "noul":
        return [0.2, 0.8]
    if question["type"] == "score":
        return [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"])
    return [1.0 if index == count - 1 else 0.0 for index in range(count)]  # always the last option


@pytest.fixture
def engine() -> FakeEngine:
    return FakeEngine(answers, installed={"jeff.support.triage", "jeff.safety.spam"})


@pytest.fixture
def jeff(engine: FakeEngine) -> Jeff:
    return Jeff(engine, Catalogue.from_dir(FIXTURES))


def test_decide_groups_rows_by_adapter_and_keeps_call_order(jeff: Jeff, engine: FakeEngine) -> None:
    decisions = jeff.decide([
        Call("triage.route", "Broken wardrobe", CONTEXT, TEAMS),
        Call("spam", "WIN A PRIZE", {"channel": "sms"}),
        Call("triage.urgency", "Broken wardrobe", CONTEXT),
    ])
    assert [d.function for d in decisions] == ["jeff.support.triage.route", "jeff.safety.spam.message_type",
                                               "jeff.support.triage.urgency"]
    assert decisions[0].value == "refunds" and decisions[1].value == "phishing" and decisions[2].value == 2
    assert [adapter for adapter, _ in engine.calls] == ["jeff.support.triage", "jeff.safety.spam"]
    assert len(engine.calls[0][1]) == 2


def test_not_installed_names_pull_command(engine: FakeEngine) -> None:
    jeff = Jeff(FakeEngine(answers, installed={"jeff.safety.spam"}), Catalogue.from_dir(FIXTURES))
    with pytest.raises(NotInstalled, match="jeff pull jeff.support.triage"):
        jeff.decide([Call("triage.urgency", "x", CONTEXT)])


def test_a_missing_adapter_is_checked_against_the_engine_once_before_not_installed() -> None:
    class Counting(FakeEngine):
        def __init__(self) -> None:
            super().__init__(answers, installed={"jeff.safety.spam"})
            self.refreshes = 0

        def installed(self, refresh: bool = False) -> set[str]:
            self.refreshes += refresh
            return super().installed(refresh)

    engine = Counting()
    jeff = Jeff(engine, Catalogue.from_dir(FIXTURES))
    jeff.decide([Call("spam", "WIN", {"channel": "sms"})])
    assert engine.refreshes == 0  # found in the cached list: no extra question to the engine
    with pytest.raises(NotInstalled):
        jeff.decide([Call("triage.urgency", "x", CONTEXT)])
    assert engine.refreshes == 1


def test_bound_function_reuses_its_binding(jeff: Jeff, engine: FakeEngine) -> None:
    route = jeff.function("triage.route", context=CONTEXT, labels=TEAMS)
    assert route("Where is my refund?").value == "refunds"
    decisions = route.many(["a", "b", "c"])
    assert len(decisions) == 3 and len(engine.calls[-1][1]) == 3


def test_shorthand_checks_interface(jeff: Jeff) -> None:
    assert jeff.rate("x", function="triage.urgency", context=CONTEXT).value == 2
    with pytest.raises(InterfaceMismatch, match="rate.*classify"):
        jeff.classify("x", function="triage.urgency", context=CONTEXT)


def test_on_low_confidence_error_raises_with_the_decision(jeff: Jeff) -> None:
    low = Jeff(FakeEngine(lambda adapter, row: [0.0, 0.5, 0.5, 0.0, 0.0], {"jeff.support.triage"}),
               Catalogue.from_dir(FIXTURES))
    with pytest.raises(LowConfidence) as error:
        low.rate("x", function="triage.urgency", context=CONTEXT, on_low_confidence="error")
    assert error.value.decision.abstained is True
    assert low.rate("x", function="triage.urgency", context=CONTEXT).abstained is True


def test_bound_function_binds_once(jeff: Jeff, engine: FakeEngine, monkeypatch: pytest.MonkeyPatch) -> None:
    route = jeff.function("triage.route", context=CONTEXT, labels=TEAMS)

    def no_more_binding(call: Call) -> None:
        raise AssertionError("bound again")

    monkeypatch.setattr(jeff, "_binding", no_more_binding)
    assert route("Where is my refund?").value == "refunds"
    assert [d.value for d in route.many(["a", "b"])] == ["refunds", "refunds"]
