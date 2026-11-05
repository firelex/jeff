import filecmp
from pathlib import Path
from typing import Any

import pytest

from jeff import contract
from jeff.errors import ContractError, JeffError

ROOT = Path(__file__).resolve().parents[3]


def same_tree(a: Path, b: Path) -> bool:
    comparison = filecmp.dircmp(a, b)
    if comparison.left_only or comparison.right_only or comparison.diff_files or comparison.funny_files:
        return False
    return all(same_tree(a / name, b / name) for name in comparison.common_dirs)


def test_shipped_copy_matches_the_repository() -> None:
    for name in ("contract", "catalogue"):
        assert same_tree(ROOT / name, contract.shipped_dir() / name), f"run: uv run python scripts/sync_shipped.py ({name})"


def test_every_schema_file_is_loaded_and_registered() -> None:
    files = {path.name.removesuffix(".schema.json") for path in (ROOT / "contract" / "schemas").glob("*.schema.json")}
    assert files == set(contract.SCHEMAS)
    for name in contract.SCHEMAS:
        assert contract.load_schema(name)["$id"] == f"https://jeff.dev/schemas/{name}.schema.json"


def test_first_problem_follows_nested_one_of_to_the_closest_alternative() -> None:
    problem = contract.first_problem("decide_request", {"calls": [{"function": "f", "input": "x"}],
                                                        "on_low_confidence": "maybe"})
    assert "'maybe' is not one of ['return', 'error']" in problem and "/on_low_confidence" in problem


def test_first_problem_prefers_the_alternative_the_document_matched_furthest() -> None:
    """Three calls with a bad label each are closer to the calls shape (problems deep inside it) than to the
    dispatch shape (one missing key and one unexpected key, both at the top)."""
    calls = [{"function": "f", "input": "x", "labels": 5}] * 3
    problem = contract.first_problem("decide_request", {"calls": calls})
    assert "/calls/0/labels" in problem and "unexpected" not in problem


def test_first_problem_can_judge_against_one_definition() -> None:
    """A channel call frame without its input, judged against the whole socket schema, comes closest to some other
    shape; judged against the call definition, the line names the missing field."""
    frame = {"op": "call", "id": 3, "handle": 1}
    assert "'input' is a required property" in contract.first_problem("socket", frame, "call")
    with pytest.raises(ContractError, match="no definition 'nope'"):
        contract.first_problem("socket", frame, "nope")


def test_interfaces_have_question_types() -> None:
    found = contract.interfaces()
    assert found["classify"].question_type == "choice"
    assert found["rate"].question_type == "score"
    assert found["detect"].question_type == "noul"
    assert found["is_supported"].values == ("supported", "partly_supported", "contradicted", "unsupported")
    assert found["classify"].values is None


def test_validate_accepts_a_minimal_decision() -> None:
    contract.validate("decision", {
        "value": "k1", "confidence": 0.9, "abstained": False, "calibrated": True, "interface": "route",
        "function": "jeff.support.triage.route", "version": "1.3.0", "probabilities": {"k1": 0.9, "other": 0.1},
        "score": None, "route": [], "decided_by": "jeff.support.triage.route@1.3.0", "escalations": [],
        "timing": {}, "evidence_refs": []}, "test")


def test_validate_names_every_problem_and_where() -> None:
    with pytest.raises(ContractError) as error:
        contract.validate("decision", {"value": "k1"}, "my-decision")
    message = str(error.value)
    assert "my-decision" in message and "confidence" in message and "abstained" in message


def test_errors_share_a_base_class() -> None:
    assert issubclass(ContractError, JeffError)


def test_validate_can_check_one_definition() -> None:
    contract.validate("socket", {"id": 3, "ok": True, "result": {}}, "reply", "ok_reply")
    with pytest.raises(ContractError, match="ok_reply in the socket schema"):
        contract.validate("socket", {"id": 3, "ok": False, "result": {}}, "reply", "ok_reply")


HASH = "a" * 64
KEYS = {"accounts": [{"account": "AbC123", "allowance": 50_000_000, "period_start": "2026-11-03T10:00:00Z",
                      "period_end": "2026-12-03T10:00:00Z"}],
        "keys": [{"hash": HASH, "account": "AbC123"}],
        "playground": {"hash": "b" * 64, "runs_per_second": 5, "period_start": "2026-11-01T00:00:00Z",
                       "period_end": "2026-12-01T00:00:00Z"}}
USAGE = {"accounts": [{"account": "AbC123", "period_start": "2026-11-03T10:00:00Z", "input_tokens": 1234}],
         "keys": [{"hash": HASH, "last_used": "2026-11-03T11:02:09Z"}]}


def test_the_accounts_documents_match_their_schemas() -> None:
    assert contract.is_valid("accounts_keys", KEYS)
    assert contract.is_valid("accounts_usage", USAGE)


@pytest.mark.parametrize("broken", [
    {**KEYS, "keys": [{"hash": "A" * 64, "account": "AbC123"}]},                    # upper-case hex
    {**KEYS, "keys": [{"hash": HASH}]},                                                # no account
    {**KEYS, "accounts": [{**KEYS["accounts"][0], "allowance": 0}]},                  # allowance must be above 0
    {**KEYS, "accounts": [{**KEYS["accounts"][0], "period_end": "2026-12-03T10:00:00.000Z"}]},  # milliseconds
    {**KEYS, "playground": {**KEYS["playground"], "runs_per_second": 0}},
    {key: value for key, value in KEYS.items() if key != "playground"},
    {**KEYS, "extra": 1},
])
def test_a_broken_key_list_is_refused(broken: dict[str, Any]) -> None:
    assert not contract.is_valid("accounts_keys", broken)


@pytest.mark.parametrize("broken", [
    {**USAGE, "accounts": [{**USAGE["accounts"][0], "input_tokens": -1}]},
    {**USAGE, "keys": [{"hash": HASH, "last_used": "yesterday"}]},
    {"accounts": []},
])
def test_a_broken_usage_report_is_refused(broken: dict[str, Any]) -> None:
    assert not contract.is_valid("accounts_usage", broken)
