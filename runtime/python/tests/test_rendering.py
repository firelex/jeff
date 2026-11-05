"""The engine writes a row's numbers for the model by their value alone (contract/socket.md, "How the engine writes a
row's numbers for the model"): 189 and 189.0 give the same model text, so every SDK's call gives the same answer.
Checked against contract/conformance/rendering.json, whose rows keep the way the client wrote each number, and over
the socket's MessagePack, which carries 189 as an integer and 189.0 as a float."""

import json
import math
from pathlib import Path
from typing import Any, cast

import msgpack  # type: ignore[import-untyped]
import pytest

from jeff_runtime.engine.model import decision_messages, describe
from jeff_runtime.engine.types import DecisionInput

REPO = Path(__file__).resolve().parents[3]
CASES = json.loads((REPO / "contract" / "conformance" / "rendering.json").read_text())["cases"]


def model_text(row: dict[str, Any], codes: list[str], layout: str) -> str:
    """The text of the model's user message for a row."""
    content = decision_messages(cast(DecisionInput, row), codes, layout)[1]["content"]
    return str(cast(list[dict[str, Any]], content)[-1]["text"])


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_every_way_of_writing_a_number_gives_the_contract_text(case: dict[str, Any]) -> None:
    for written in case["rows_json"]:
        assert model_text(json.loads(written), case["codes"], case["layout"]) == case["text"], written


def packable(value: Any) -> bool:
    """Whether MessagePack can hold the value: its integers lie in -2**63..2**64-1 (an SDK refuses any other)."""
    if isinstance(value, bool) or not isinstance(value, (int, dict, list)):
        return True
    if isinstance(value, int):
        return -2**63 <= value < 2**64
    return all(packable(item) for item in (value.values() if isinstance(value, dict) else value))


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_a_row_sent_over_the_socket_gives_the_contract_text(case: dict[str, Any]) -> None:
    rows = [json.loads(written) for written in case["rows_json"]]
    assert any(packable(row) for row in rows)
    for parsed in filter(packable, rows):
        row = msgpack.unpackb(msgpack.packb(parsed), raw=False)
        assert model_text(row, case["codes"], case["layout"]) == case["text"], parsed


def test_the_refund_case_covers_both_forms_the_sdks_sent() -> None:
    """The case behind the rule: Python sent the amount as 189.0, Node, Rust, C# and Java as 189."""
    rows = [json.loads(written) for written in CASES[0]["rows_json"]]
    amounts = [row["state"]["proposed_call"]["arguments"]["amount"] for row in rows]
    assert {type(amount) for amount in amounts} == {int, float} and set(amounts) == {189}
    assert '"amount": 189,' in CASES[0]["text"]


def test_describe_leaves_text_booleans_and_fractions_alone() -> None:
    assert describe("189.0") == "189.0"
    assert describe({"paid": True, "count": 1.0, "share": 0.25}) == '{"paid": true, "count": 1, "share": 0.25}'
    assert describe([2.0, [3.0, {"x": -0.0}]]) == "[2, [3, {\"x\": 0}]]"


def test_a_number_that_is_not_finite_is_written_as_before() -> None:
    """No whole-number rule applies to NaN or infinity (JSON has neither; Python's json writes them as NaN and
    Infinity, and the engine keeps that)."""
    assert describe({"a": math.inf, "b": -math.inf}) == '{"a": Infinity, "b": -Infinity}'
    assert describe([math.nan]) == "[NaN]"
