"""The INT4 gate (plan 2b-2 Task 7, design Section 6), without a GPU: its pass/fail arithmetic on synthetic counts
(jeff.gate.judge, agreement_counts, calibration_error), and the serving check against a synthetic gate.json
(jeff.gate.check_served): a passing gate, a failing one, a missing adapter, another version, another base's gate,
no gate at all. The measurement itself (jeff.gate_run) is tested on the GPU: runtime/python/tests/test_gate_cuda.py."""

import json
import re
from fractions import Fraction
from pathlib import Path
from typing import Any

import pytest

from jeff.errors import EngineError
from jeff.gate import (AdapterMeasures, Agreement, Counts, agreement_counts, calibration_error, check_served,
                       int4_identity, judge)
from jeff.testing import FAKE_FORMAT_ID, fake_gate, fake_int4_base

PASSING_AGREEMENT = Agreement(compared=1000, agreeing=995, raw_agreeing=993, exact_ties=2, largest_difference=0.05)


def measures(change_rows: int, *, template: Counts | None = None, agreement: Agreement = PASSING_AGREEMENT,
             misses: dict[str, str] | None = None, rows: int = 10000) -> AdapterMeasures:
    """An adapter whose INT4 base gets `change_rows` more rows right than the reference (negative: fewer) out of
    `rows`, in engine mode, and in template mode too unless `template` is given."""
    engine = Counts(rows, 9000, 9000 + change_rows)
    return AdapterMeasures(engine, template if template is not None else engine, agreement, misses or {})


# ---- the arithmetic


def test_the_designs_gptq_numbers_pass() -> None:
    """Design Section 1: GPTQ on v1.3 costs -0.22 points on average, worst triage -0.39."""
    verdict = judge({"triage@1": measures(-39), "emotion@1": measures(-5), "nav@1": measures(-22)}, [])
    assert verdict.passed and verdict.overall == [] and all(not reasons for reasons in verdict.adapters.values())
    assert verdict.mean_change == {"engine": Fraction(-22, 100), "template": Fraction(-22, 100)}


def test_an_adapter_at_exactly_minus_half_a_point_passes_and_one_row_less_fails() -> None:
    assert judge({"a@1": measures(-50), "b@1": measures(50)}, []).passed
    verdict = judge({"a@1": measures(-51), "b@1": measures(51)}, [])
    assert not verdict.passed and verdict.overall == []
    assert verdict.adapters == {"a@1": ["engine-mode accuracy change -0.510 points, below -0.500",
                                        "template-mode accuracy change -0.510 points, below -0.500"],
                                "b@1": []}


def test_the_mean_fails_the_set_when_no_adapter_does() -> None:
    verdict = judge({"a@1": measures(-40), "b@1": measures(-40)}, [])
    assert not verdict.passed and verdict.adapters == {"a@1": [], "b@1": []}
    assert verdict.overall == ["mean engine-mode accuracy change -0.400 points over 2 adapters, below -0.300",
                               "mean template-mode accuracy change -0.400 points over 2 adapters, below -0.300"]


def test_the_mean_at_exactly_minus_three_tenths_passes() -> None:
    assert judge({"a@1": measures(-30), "b@1": measures(-30)}, []).passed


def test_template_mode_is_judged_on_its_own() -> None:
    verdict = judge({"a@1": measures(0, template=Counts(1000, 900, 894)), "b@1": measures(0)}, [])
    assert verdict.adapters["a@1"] == ["template-mode accuracy change -0.600 points, below -0.500"]
    assert verdict.overall == []  # template mean -0.3 exactly: passes
    assert not verdict.passed


def test_a_round_to_nearest_base_fails_naming_the_adapters_over_the_limit() -> None:
    """Design Section 1: round to nearest costs -0.64 on average, four adapters over -0.5."""
    changes = {"emotion": -90, "ground": -40, "guard": -30, "legal-clauses": -110, "nav": -45, "spam": -30,
               "support-intents": -70, "tools": -50, "triage": -111}
    verdict = judge({f"{name}@1.3.0": measures(change) for name, change in changes.items()}, [])
    assert not verdict.passed
    failing = sorted(key for key, reasons in verdict.adapters.items() if reasons)
    assert failing == ["emotion@1.3.0", "legal-clauses@1.3.0", "support-intents@1.3.0", "triage@1.3.0"]
    assert verdict.overall[0].startswith("mean engine-mode accuracy change -0.640 points over 9 adapters")


@pytest.mark.parametrize(("agreeing", "passes"), [(990, True), (989, False)])
def test_agreement_needs_99_percent_of_the_compared_rows(agreeing: int, passes: bool) -> None:
    agreement = Agreement(1000, agreeing, agreeing, 0, 0.1)
    verdict = judge({"a@1": measures(0, agreement=agreement)}, [])
    assert verdict.passed is passes
    if not passes:
        assert verdict.adapters["a@1"] == ["agreement 989/1000 (98.90%), below 99%"]


def test_an_adapter_with_no_row_on_the_fast_path_fails() -> None:
    verdict = judge({"a@1": measures(0, agreement=Agreement(0, 0, 0, 0, 0.0))}, [])
    assert verdict.adapters["a@1"] == ["agreement: no test row took the fast path"]


def test_a_refit_that_misses_its_target_fails_the_adapter_naming_the_function() -> None:
    verdict = judge({"a@1": measures(0, misses={"a.route": "No threshold reaches precision 0.95"})}, [])
    assert verdict.adapters["a@1"] == ["calibration refit: a.route No threshold reaches precision 0.95"]
    assert not verdict.passed


def test_a_determinism_failure_fails_the_set() -> None:
    verdict = judge({"a@1": measures(0)}, ["two runs differ"])
    assert not verdict.passed and verdict.overall == ["determinism: two runs differ"]


def test_counts_need_rows_and_judge_needs_adapters() -> None:
    with pytest.raises(EngineError, match="at least one row"):
        Counts(0, 0, 0).change()
    with pytest.raises(EngineError, match="at least one adapter"):
        judge({}, [])


def test_agreement_counts_with_the_tie_rule() -> None:
    pairs = [([0.2, 0.8], [0.3, 0.7]),        # same top option
             ([0.5, 0.5], [0.49, 0.51]),      # an exact tie in the standard path: either option agrees
             ([0.4, 0.4, 0.2], [0.3, 0.3, 0.4]),  # a tie, but the fast path picks a third option
             ([0.6, 0.4], [0.4, 0.6])]        # a plain disagreement
    counted = agreement_counts(pairs)
    assert (counted.compared, counted.agreeing, counted.raw_agreeing, counted.exact_ties) == (4, 2, 1, 2)
    assert counted.largest_difference == pytest.approx(0.2)
    with pytest.raises(EngineError, match="2 probabilities and the fast path 3"):
        agreement_counts([([0.5, 0.5], [0.2, 0.3, 0.5])])


def test_calibration_error() -> None:
    assert calibration_error([(0.95, True)] * 19 + [(0.95, False)]) == pytest.approx(0.0)
    # two bins: 0.9-confident rows all right (gap 0.1), 0.55-confident rows half right (gap 0.05)
    assert calibration_error([(0.9, True)] * 2 + [(0.55, True), (0.55, False)]) == pytest.approx(0.075)
    with pytest.raises(EngineError):
        calibration_error([])


# ---- serving: gate.json against the base and the adapters

TRIAGE = "jeff.support.triage"
SPAM = "jeff.safety.spam"


@pytest.fixture
def base(tmp_path: Path) -> Path:
    return fake_int4_base(tmp_path / "int4")


def identity(folder: Path):  # type: ignore[no-untyped-def]
    found = int4_identity(folder)
    assert found is not None
    return found


def test_a_passing_gate_serves_its_adapters(base: Path) -> None:
    fake_gate(base, {f"{TRIAGE}@1.3.0": [], f"{SPAM}@1.3.0": []})
    check_served(identity(base), {TRIAGE: "1.3.0", SPAM: "1.3.0"})
    check_served(identity(base), {TRIAGE: "1.3.0"})  # any subset
    check_served(identity(base), {})  # the base alone


def test_no_gate_is_refused_naming_the_adapter_and_the_command(base: Path) -> None:
    with pytest.raises(EngineError) as refused:
        check_served(identity(base), {TRIAGE: "1.3.0"})
    message = str(refused.value)
    assert "has no gate.json" in message and f"{TRIAGE}@1.3.0" in message
    assert f"jeff runtime gate --base {base} --reference" in message and f"--adapters {TRIAGE}" in message


def test_the_base_alone_is_refused_without_a_gate(base: Path) -> None:
    with pytest.raises(EngineError, match="To serve the base alone on it"):
        check_served(identity(base), {})


def test_a_failed_gate_serves_nothing(base: Path) -> None:
    fake_gate(base, {f"{TRIAGE}@1.3.0": ["engine-mode accuracy change -0.610 points, below -0.500"],
                     f"{SPAM}@1.3.0": []}, overall=["mean engine-mode accuracy change -0.640 points"])
    for adapters in ({SPAM: "1.3.0"}, {TRIAGE: "1.3.0"}, {}):
        with pytest.raises(EngineError) as refused:
            check_served(identity(base), adapters)
        message = str(refused.value)
        assert "failed its gate" in message and "-0.640" in message and f"{TRIAGE}@1.3.0: engine-mode" in message
        assert "jeff runtime quantize --method gptq" in message and "jeff runtime gate --base" in message
    with pytest.raises(EngineError, match=f"refusing {SPAM}@1.3.0"):
        check_served(identity(base), {SPAM: "1.3.0"})


@pytest.mark.parametrize("change", [
    lambda gate: gate.update(overall_failures=["mean engine-mode accuracy change -0.4"]),  # passed, yet a failure
    lambda gate: gate["adapters"][f"{TRIAGE}@1.3.0"].update(failures=["agreement 980/1000"]),  # entry and set say passed
    lambda gate: gate.update(passed=False),  # failed, with no failure recorded
    lambda gate: gate["adapters"][f"{TRIAGE}@1.3.0"].update(passed=False),  # entry failed with no failure
])
def test_a_gate_whose_verdict_disagrees_with_its_failures_is_refused(base: Path, change: Any) -> None:
    path = fake_gate(base, {f"{TRIAGE}@1.3.0": []})
    gate = json.loads(path.read_text())
    change(gate)
    path.write_text(json.dumps(gate))
    with pytest.raises(EngineError, match="disagrees with"):
        check_served(identity(base), {TRIAGE: "1.3.0"})


def test_an_adapter_the_gate_never_saw_is_refused(base: Path) -> None:
    fake_gate(base, {f"{SPAM}@1.3.0": []})
    with pytest.raises(EngineError) as refused:
        check_served(identity(base), {TRIAGE: "1.3.0", SPAM: "1.3.0"})
    message = str(refused.value)
    assert f"{TRIAGE}@1.3.0 is not in the gate of {FAKE_FORMAT_ID} (it gated {SPAM}@1.3.0)" in message
    assert f"move {base / 'gate.json'} aside and run: jeff runtime gate --base {base}" in message
    assert f"--adapters {SPAM} {TRIAGE}" in message  # the rerun gates every adapter to serve


def test_another_version_of_an_adapter_is_refused(base: Path) -> None:
    fake_gate(base, {f"{TRIAGE}@1.3.0": []})
    with pytest.raises(EngineError, match=re.escape(f"{TRIAGE}@1.4.0 is not in the gate of {FAKE_FORMAT_ID}, which "
                                                    f"gated {TRIAGE}@1.3.0 (another version)")):
        check_served(identity(base), {TRIAGE: "1.4.0"})


def test_the_gate_of_another_int4_base_is_refused(base: Path) -> None:
    other = "jeff-base@v1.3+int4-g64-gptq.feedf00d"
    fake_gate(base, {f"{TRIAGE}@1.3.0": []}, format_id=other, int4_sha256="feedf00d" * 8)
    with pytest.raises(EngineError, match=f"is the gate of {re.escape(other)} .*not of this base, "
                                          f"{re.escape(FAKE_FORMAT_ID)}"):
        check_served(identity(base), {TRIAGE: "1.3.0"})


def test_the_gate_of_a_base_with_the_same_name_but_other_weights_is_refused(base: Path) -> None:
    fake_gate(base, {f"{TRIAGE}@1.3.0": []}, int4_sha256="0123abcd" + "f" * 56)
    with pytest.raises(EngineError, match="belongs to another INT4 base"):
        check_served(identity(base), {TRIAGE: "1.3.0"})


@pytest.mark.parametrize("text", ["not json", "[]", '{"format": "jeff-int4-gate", "version": 2}',
                                  '{"format": "jeff-int4-gate", "version": 1, "passed": true}'])
def test_a_malformed_gate_is_refused(base: Path, text: str) -> None:
    (base / "gate.json").write_text(text)
    with pytest.raises(EngineError, match="gate.json"):
        check_served(identity(base), {TRIAGE: "1.3.0"})


def test_int4_identity(tmp_path: Path, base: Path) -> None:
    found = identity(base)
    assert (found.format_id, found.source, found.folder) == (FAKE_FORMAT_ID, ("jeff-base", "v1.3"), base)
    bf16 = tmp_path / "bf16"
    bf16.mkdir()
    (bf16 / "decision_config.json").write_text('{"format_version": 1}')
    assert int4_identity(bf16) is None
