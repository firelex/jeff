import json
from pathlib import Path

import pytest
import yaml

from jeff import contract
from jeff.binding import bind
from jeff.catalogue import Catalogue
from jeff.decision import Decision, make_decision
from jeff.errors import EngineError
from jeff.manifest import manifest_from_dict

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}


@pytest.fixture
def catalogue() -> Catalogue:
    return Catalogue.from_dir(FIXTURES)


def bound(catalogue: Catalogue, ref: str, context=CONTEXT, labels=None):
    function = catalogue.function(ref)
    return bind(catalogue.manifest_of(function), function, context, labels,
                calibration=function.calibration)


def test_choice_value_is_the_callers_key(catalogue: Catalogue) -> None:
    binding = bound(catalogue, "triage.route", labels={"deliveries": "D", "refunds": "R"})
    decision = make_decision(binding, [0.1, 0.2, 0.7], {"model_ms": 2.0})
    assert decision.value == "refunds"
    assert decision.probabilities == {"other": pytest.approx(0.1), "deliveries": pytest.approx(0.2),
                                      "refunds": pytest.approx(0.7)}
    assert decision.confidence == pytest.approx(0.7)
    assert decision.calibrated is False and decision.abstained is False
    assert decision.decided_by == "jeff.support.triage.route@1.3.0"
    assert decision.interface == "route"


def test_noul_value_is_boolean(catalogue: Catalogue) -> None:
    decision = make_decision(bound(catalogue, "triage.needs_human"), [0.3, 0.7], {})
    assert decision.value is True and decision.confidence == pytest.approx(0.7)
    decision = make_decision(bound(catalogue, "triage.needs_human"), [0.8, 0.2], {})
    assert decision.value is False and decision.confidence == pytest.approx(0.8)


def test_score_value_is_index_and_score_is_expected_level(catalogue: Catalogue) -> None:
    decision = make_decision(bound(catalogue, "triage.urgency"), [0.0, 0.1, 0.6, 0.3, 0.0], {})
    assert decision.value == 2
    assert decision.score == pytest.approx(0.1 + 1.2 + 0.9)
    assert decision.confidence == pytest.approx(0.6)


def test_calibrated_function_abstains_below_threshold(catalogue: Catalogue) -> None:
    decision = make_decision(bound(catalogue, "triage.urgency"), [0.0, 0.45, 0.55, 0.0, 0.0], {})
    assert decision.calibrated is True and decision.abstained is True
    assert decision.value == 2


def test_probabilities_within_the_tolerance_are_renormalised_and_length_checked(catalogue: Catalogue) -> None:
    probabilities = [0.50004, 0.25, 0.25]  # a sum of 1.00004, within SUM_TOLERANCE of 1
    decision = make_decision(bound(catalogue, "spam", context={"channel": "sms"}), probabilities, {})
    assert decision.probabilities["legitimate"] == 0.50004 / sum(probabilities)
    with pytest.raises(EngineError, match="3 options"):
        make_decision(bound(catalogue, "spam", context={"channel": "sms"}), [1.0], {})


def test_a_valid_reply_is_divided_by_its_sum_exactly(catalogue: Catalogue) -> None:
    # The values a float32 softmax gives, summing to just under 1: each is divided by the sum, nothing else changes.
    probabilities = [0.10000000149011612, 0.20000000298023224, 0.699999988079071, 0.0, 0.0]
    decision = make_decision(bound(catalogue, "triage.urgency"), probabilities, {})
    total = sum(probabilities)
    assert list(decision.probabilities.values()) == [p / total for p in probabilities]
    assert decision.score == sum(index * p / total for index, p in enumerate(probabilities))


@pytest.mark.parametrize(("ref", "probabilities", "expected"), [
    ("triage.needs_human", [-0.1, 1.1], "which [-0.1, 1.1] are not"),
    ("triage.needs_human", [float("nan"), 0.5], "which [nan] are not"),
    ("triage.needs_human", [float("inf"), 0.0], "which [inf] are not"),
    ("triage.needs_human", [0.5, float("-inf")], "which [-inf] are not"),
    ("triage.urgency", [0.0, 0.0, 1.5, 0.0, 0.0], "which [1.5] are not"),
    ("triage.route", [1e308, 1e308, 0.0], "which [1e+308, 1e+308] are not"),  # a sum that overflows
    ("spam", [2.0, 1.0, 1.0], "which [2.0] are not"),
])
def test_a_probability_outside_zero_to_one_is_refused(catalogue: Catalogue, ref: str, probabilities: list[float],
                                                      expected: str) -> None:
    context = {"channel": "sms"} if ref == "spam" else CONTEXT
    labels = {"deliveries": "D", "refunds": "R"} if ref == "triage.route" else None
    binding = bound(catalogue, ref, context=context, labels=labels)
    with pytest.raises(EngineError) as raised:
        make_decision(binding, probabilities, {})
    assert str(raised.value) == (f"{binding.function.id}: the engine returned invalid probabilities {probabilities}; "
                                 f"expected each to be a finite number from 0 to 1, {expected}")


@pytest.mark.parametrize(("probabilities", "total"), [
    ([0.0, 0.0], "0.0"),
    ([0.5, 0.4], "0.9"),
    ([0.6, 0.4002], "1.0002"),
    ([0.9, 0.9], "1.8"),
])
def test_a_sum_outside_the_tolerance_is_refused(catalogue: Catalogue, probabilities: list[float], total: str) -> None:
    with pytest.raises(EngineError) as raised:
        make_decision(bound(catalogue, "triage.needs_human"), probabilities, {})
    assert str(raised.value) == (f"jeff.support.triage.needs_human: the engine returned invalid probabilities "
                                 f"{probabilities}; expected their sum to be within 0.0001 of 1, got {total}")


def test_abstain_via_labels(catalogue: Catalogue) -> None:
    # Load the triage manifest and modify it to set abstain_labels
    triage_path = FIXTURES / "jeff.support.triage" / "manifest.yaml"
    raw = yaml.safe_load(triage_path.read_text(encoding="utf-8"))
    raw["functions"]["route"]["abstain_labels"] = ["other"]
    manifest = manifest_from_dict(raw, triage_path)

    # Get the modified route function
    route_fn = manifest.functions["route"]

    # Test 1: when "other" is chosen (highest probability), abstained should be True
    binding = bind(manifest, route_fn, CONTEXT, labels={"deliveries": "D", "refunds": "R"},
                   calibration=None)
    decision = make_decision(binding, [0.7, 0.2, 0.1], {})  # probabilities: other=0.7, deliveries=0.2, refunds=0.1
    assert decision.value == "other"
    assert decision.abstained is True  # because value is in abstain_labels
    assert decision.calibrated is False  # no calibration on route

    # Test 2: when another label is chosen (highest probability), abstained should be False
    decision = make_decision(binding, [0.1, 0.7, 0.2], {})  # probabilities: other=0.1, deliveries=0.7, refunds=0.2
    assert decision.value == "deliveries"
    assert decision.abstained is False


def test_json_round_trip_matches_schema(catalogue: Catalogue) -> None:
    decision = make_decision(bound(catalogue, "triage.urgency"), [0.0, 0.1, 0.6, 0.3, 0.0], {"path": "standard"})
    data = json.loads(json.dumps(decision.to_json()))
    contract.validate("decision", data, "round trip")
    assert Decision.from_json(data) == decision
