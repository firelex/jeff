"""contract/golden/step1.json: one case per catalogue function, recorded by scripts/record_golden.py. Every mode
must reproduce it: local mode against the real model (slow), remote mode by answering exactly what local mode answers
over the same engine (fast)."""

import json
from collections import defaultdict
from pathlib import Path

import pytest

from jeff import Call, Jeff
from jeff.catalogue import Catalogue
from jeff.contract import shipped_dir
from jeff.evaluate import golden_problems
from jeff.testing import FakeEngine

from served import fake, hashed, jeff_server, local, remote

GOLDEN = json.loads((shipped_dir() / "contract" / "golden" / "step1.json").read_text())
CASES = GOLDEN["cases"]
FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"


def by_adapter() -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for case in CASES:
        groups[case["call"]["function"].rsplit(".", 1)[0]].append(case)
    return groups


def test_golden_file_matches_its_shape_with_one_case_per_function() -> None:
    for case in CASES:
        for key in ("name", "source", "call", "expected", "tolerance"):
            assert key in case, f"{case}: missing {key}"
        assert "function" in case["call"], case["name"]
        for key in ("value", "abstained", "confidence"):
            assert key in case["expected"], case["name"]
        assert case["source"] == "example.json" or case["source"].startswith("test row "), case["name"]
    functions = [case["call"]["function"] for case in CASES]
    assert len(functions) == len(set(functions)), "a function has more than one golden case"
    catalogue = Catalogue.shipped()
    known = {function.id for manifest in catalogue.manifests() for function in manifest.functions.values()}
    assert set(functions) == known
    assert "abstained may differ" in GOLDEN["description"]


def urgency_case(confidence: float, abstained: bool) -> dict:
    return {"name": "urgency", "call": {"function": "triage.urgency", "input": "x",
                                        "context": {"company": "c", "channel": "e"}},
            "expected": {"value": 2, "abstained": abstained, "confidence": confidence}, "tolerance": 0.03}


def test_golden_rule_lets_abstained_differ_only_near_the_threshold() -> None:
    """triage.urgency's threshold in the fixture is 0.6; the engine answers level 2 with confidence 0.59."""
    jeff = Jeff(FakeEngine(lambda a, r: [0.0, 0.41, 0.59, 0.0, 0.0], {"jeff.support.triage"}),
                Catalogue.from_dir(FIXTURES))
    [decision] = jeff.decide([Call(**urgency_case(0.61, False)["call"])])
    assert decision.abstained is True
    # recorded 0.61 (not abstained) is within the tolerance of the threshold: the side may flip
    assert golden_problems(urgency_case(0.61, False), decision, 0.6) == []
    # recorded far from the threshold: abstained must match
    problems = golden_problems(urgency_case(0.615, False), decision, 0.58)
    assert len(problems) == 1 and "abstained" in problems[0]
    # value and confidence are always checked
    assert any("confidence" in p for p in golden_problems(urgency_case(0.65, False), decision, 0.6))
    assert any("value" in p for p in golden_problems({**urgency_case(0.59, True), "expected": {
        "value": 3, "abstained": True, "confidence": 0.59}}, decision, 0.6))


def test_golden_calls_through_remote_mode_equal_local_mode() -> None:
    """Every golden call through Jeff.remote (one POST /v1/decide) answers exactly what local mode answers over the
    same FakeEngine and the shipped catalogue."""
    catalogue = shipped_dir() / "catalogue"
    installed = {manifest.id for manifest in Catalogue.from_dir(catalogue).manifests()}
    calls = [Call(**case["call"]) for case in CASES]
    expected = local(fake(installed, hashed), catalogue).decide(calls)
    with jeff_server(fake(installed, hashed), catalogue) as url, remote(url, catalogue=catalogue) as client:
        got = client.decide(calls)
    assert got == expected
    assert len(got) == len(CASES)


@pytest.mark.slow
@pytest.mark.parametrize("adapter", sorted(by_adapter()))
def test_golden_cases(adapter: str) -> None:
    jeff = Jeff.local([adapter])
    cases = by_adapter()[adapter]
    decisions = jeff.decide([Call(**case["call"]) for case in cases])
    for case, decision in zip(cases, decisions, strict=True):
        calibration = jeff.catalogue.function(case["call"]["function"]).calibration
        assert golden_problems(case, decision, calibration.threshold if calibration else None) == [], case["name"]
