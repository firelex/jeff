"""Field names with spaces: a manifest's context and input keys may be any printable text (as the adapter was trained,
for example "counterparty name"); callers use each field's code name (counterparty_name) everywhere, and the prompt
keeps the exact key."""

import copy
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from jeff import contract
from jeff.binding import bind, build_row
from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import ContractError, ManifestError, MissingContext, UnknownContext
from jeff.evaluate import split_state
from jeff.listing import function_listing
from jeff.manifest import Manifest, code_name_of, manifest_from_dict
from jeff_runtime.engine.model import decision_messages

from served import CATALOGUE, Mode, fake, jeff_server

SPAM = CATALOGUE / "jeff.safety.spam" / "manifest.yaml"
SPACED = [{"key": "counterparty name", "description": "Who the message is from.", "required": True},
          {"key": "Deal ID", "description": "The deal the message is about.", "required": False}]
GIVEN = {"counterparty_name": "Acme Ltd", "deal_id": "D-7"}


def spam_raw(context: list[dict[str, Any]] = SPACED, input_field: dict[str, Any] | None = None) -> dict[str, Any]:
    raw = yaml.safe_load(SPAM.read_text())
    raw["context"] = copy.deepcopy(context)
    if input_field is not None:
        raw["input"] = input_field
    return raw


def spaced() -> Manifest:
    return manifest_from_dict(spam_raw(), SPAM)


def spaced_catalogue(tmp_path: Path) -> Path:
    """A copy of the fixture catalogue whose spam adapter has the spaced context fields."""
    folder = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, folder)
    (folder / "jeff.safety.spam" / "manifest.yaml").write_text(yaml.safe_dump(spam_raw()))
    return folder


@pytest.mark.parametrize(("key", "code"), [
    ("channel", "channel"), ("counterparty name", "counterparty_name"), ("Deal ID", "deal_id"),
    ("outreach  log -- v2", "outreach_log_v2"), ("_odd_", "odd"), ("clause type?", "clause_type"),
    ("company_", "company_"), ("a__b", "a__b"), ("x9_", "x9_"),
])
def test_a_valid_key_is_its_own_code_name_and_others_are_lower_cased_with_runs_as_one_underscore(
        key: str, code: str) -> None:
    assert code_name_of(key) == code


def test_each_field_gets_its_code_name() -> None:
    manifest = spaced()
    assert [(field.key, field.code_name) for field in manifest.context] == [("counterparty name", "counterparty_name"),
                                                                             ("Deal ID", "deal_id")]
    assert (manifest.input.key, manifest.input.code_name) == ("message", "message")


def test_a_declared_code_name_is_used() -> None:
    raw = spam_raw([{"key": "日付", "code_name": "trade_date", "description": "The trade date.", "required": True}])
    assert manifest_from_dict(raw, SPAM).context[0].code_name == "trade_date"


def test_a_key_with_no_usable_code_name_must_declare_one() -> None:
    for key in ("日付", "2nd party"):
        raw = spam_raw([{"key": key, "description": "A field.", "required": True}])
        with pytest.raises(ManifestError, match=f"{key}.*code_name"):
            manifest_from_dict(raw, SPAM)


def test_two_fields_with_one_code_name_are_refused_naming_both() -> None:
    raw = spam_raw([{"key": "counterparty name", "description": "A.", "required": True},
                    {"key": "Counterparty-Name", "description": "B.", "required": True}])
    with pytest.raises(ManifestError, match="counterparty_name") as refused:
        manifest_from_dict(raw, SPAM)
    assert "counterparty name" in str(refused.value) and "Counterparty-Name" in str(refused.value)


def test_a_context_field_and_the_input_with_one_code_name_are_refused() -> None:
    raw = spam_raw([{"key": "Message", "description": "A.", "required": True}])
    with pytest.raises(ManifestError, match="message"):
        manifest_from_dict(raw, SPAM)


@pytest.mark.parametrize("key", [" leading", "trailing ", "new\nline", "ends in a newline\n", "tab\there",
                                 "bell\x07", "c1 control\x85", "x" * 61, "",
                                 # Unicode line and paragraph separators, anywhere
                                 "line\u2028separator", "paragraph\u2029separator", "ends in one\u2028",
                                 # any whitespace character at either end, not only the ASCII space
                                 "trailing no-break space\u00a0", "\u00a0leading no-break space", "em space\u2003",
                                 "\u3000ideographic space", "narrow no-break\u202f", "ogham\u1680",
                                 "math space\u205f", "\u2000en quad", "hair space\u200a", "byte order mark\ufeff"])
def test_the_schema_refuses_keys_that_are_not_printable_text_of_1_to_60_characters(key: str) -> None:
    with pytest.raises(ContractError):
        manifest_from_dict(spam_raw([{"key": key, "code_name": "field", "description": "A.", "required": True}]), SPAM)
    with pytest.raises(ContractError):
        manifest_from_dict(spam_raw(input_field={"key": key, "code_name": "field", "description": "A."}), SPAM)


def test_the_schema_allows_inner_whitespace_other_than_line_and_paragraph_separators() -> None:
    for key in ("counterparty\u00a0name", "deal\u2003id", "a\u3000b"):
        manifest = manifest_from_dict(spam_raw([{"key": key, "code_name": "field", "description": "A.",
                                                 "required": True}]), SPAM)
        assert manifest.context[0].key == key


def test_the_schema_allows_a_60_character_key_with_inner_spaces_and_punctuation() -> None:
    key = "Counterparty name (legal entity, as on the contract) – v2 ok"
    assert len(key) == 60
    manifest = manifest_from_dict(spam_raw([{"key": key, "description": "A.", "required": True}]), SPAM)
    assert manifest.context[0].code_name == "counterparty_name_legal_entity_as_on_the_contract_v2_ok"


@pytest.mark.parametrize("code", ["Counterparty", "1st", "a b", ""])
def test_a_declared_code_name_must_be_a_code_name(code: str) -> None:
    with pytest.raises(ContractError):
        manifest_from_dict(spam_raw([{"key": "counterparty name", "code_name": code, "description": "A.",
                                      "required": True}]), SPAM)


def test_callers_bind_by_code_name_and_the_prompt_keeps_the_exact_keys() -> None:
    manifest = spaced()
    function = manifest.functions["message_type"]
    row = build_row(bind(manifest, function, GIVEN, None, calibration=None), "WIN NOW")
    assert row["state"] == {"counterparty name": "Acme Ltd", "Deal ID": "D-7", "message": "WIN NOW"}
    prompt = json.dumps(decision_messages(row, ["A", "B", "C"]), ensure_ascii=False)
    assert '\\"counterparty name\\": \\"Acme Ltd\\"' in prompt and "counterparty_name" not in prompt


def test_the_state_follows_the_manifest_order_whatever_order_the_caller_used() -> None:
    manifest = spaced()
    given = {"deal_id": "D-7", "counterparty_name": "Acme Ltd"}
    row = build_row(bind(manifest, manifest.functions["message_type"], given, None, calibration=None), "x")
    assert list(row["state"]) == ["counterparty name", "Deal ID", "message"]


def test_the_exact_key_is_refused_naming_the_code_name() -> None:
    manifest = spaced()
    with pytest.raises(UnknownContext, match="'counterparty name'.*counterparty_name"):
        bind(manifest, manifest.functions["message_type"], {"counterparty name": "Acme Ltd"}, None,
             calibration=None)


def test_unknown_and_missing_fields_are_named_by_code_name() -> None:
    manifest = spaced()
    function = manifest.functions["message_type"]
    with pytest.raises(UnknownContext, match="colour.*counterparty_name, deal_id"):
        bind(manifest, function, {**GIVEN, "colour": "red"}, None, calibration=None)
    with pytest.raises(MissingContext, match="counterparty_name") as missing:
        bind(manifest, function, {"deal_id": "D-7"}, None, calibration=None)
    assert missing.value.fields == ["counterparty_name"]


def test_a_test_rows_state_is_split_into_context_by_code_name() -> None:
    state = {"counterparty name": "Acme Ltd", "Deal ID": "D-7", "message": "WIN NOW"}
    function = spaced().functions["message_type"]
    assert split_state(function, state) == (GIVEN, "WIN NOW")
    assert split_state(function, {"counterparty_name": "Acme Ltd", "message": "WIN NOW"}) is None


def test_the_mcp_tool_takes_context_by_code_name() -> None:
    from jeff import mcp
    manifest = spaced()
    function = manifest.functions["message_type"]
    schema = mcp._input_schema(function)["properties"]["context"]
    assert list(schema["properties"]) == ["counterparty_name", "deal_id"]
    assert schema["required"] == ["counterparty_name"]
    description = mcp._description(manifest, function)
    assert "counterparty_name (required): Who the message is from." in description
    assert "deal_id (optional)" in description


def test_the_function_listing_gives_each_fields_code_name(tmp_path: Path) -> None:
    jeff = Jeff(fake(installed={"jeff.safety.spam"}), Catalogue.from_dir(spaced_catalogue(tmp_path)))
    listing = function_listing(jeff)
    contract.validate("functions_response", listing, "the listing")
    listed = listing["functions"][0]
    assert listed["context"] == [{**SPACED[0], "code_name": "counterparty_name"}, {**SPACED[1], "code_name": "deal_id"}]
    assert listed["input"] == {"key": "message", "code_name": "message", "description": "The message.", "form": "fields"}


def test_a_remote_caller_passes_code_names_and_the_engine_reads_the_exact_keys(tmp_path: Path, mode: Mode) -> None:
    catalogue = spaced_catalogue(tmp_path)
    engine = fake(installed={"jeff.safety.spam"})
    with jeff_server(engine, catalogue) as url, mode.remote(url, catalogue=catalogue) as client:
        decision = client.function("spam.message_type", context=GIVEN)("WIN NOW")
        assert decision.value == "legitimate"
        with pytest.raises(UnknownContext, match="counterparty_name"):
            client.function("spam.message_type", context={"counterparty name": "Acme Ltd"})("WIN NOW")
    [(_, rows)] = engine.calls
    assert rows[0]["state"] == {"counterparty name": "Acme Ltd", "Deal ID": "D-7", "message": "WIN NOW"}


ROUTERS = CATALOGUE.parent / "routers"
ALL_ROUTED = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}


def spaced_routers(tmp_path: Path) -> Path:
    """A copy of the routing fixtures whose triage adapter's company field is 'company name' (company_name)."""
    folder = tmp_path / "routers"
    shutil.copytree(ROUTERS, folder)
    path = folder / "jeff.support.triage" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["context"][0]["key"] = "company name"
    path.write_text(yaml.safe_dump(raw))
    return folder


def first_option(adapter: str, row: dict[str, Any]) -> list[float]:
    if row["question"]["type"] == "score":
        return [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(row["question"]["criteria"]) if "criteria" in row["question"] else 2
    return [1.0] + [0.0] * (count - 1)


def test_dispatch_takes_code_names_and_refuses_an_exact_key_of_any_reached_adapter(tmp_path: Path) -> None:
    from jeff.testing import FakeEngine
    jeff = Jeff(FakeEngine(first_option, ALL_ROUTED), Catalogue.from_dir(spaced_routers(tmp_path)))
    decision = jeff.rate("My wardrobe is broken", context={"company_name": "A shop.", "channel": "email"})
    assert decision.function == "jeff.support.triage.urgency"
    assert jeff.engine.calls[-1][1][0]["state"] == {"company name": "A shop.", "channel": "email",  # type: ignore
                                                    "message": "My wardrobe is broken"}
    calls = len(jeff.engine.calls)  # type: ignore[attr-defined]
    with pytest.raises(UnknownContext, match="'company name' as company_name"):
        jeff.rate("My wardrobe is broken", context={"company name": "A shop.", "channel": "email"})
    assert len(jeff.engine.calls) == calls  # type: ignore[attr-defined]  # refused before any router ran


def test_dispatch_works_out_the_exact_keys_once_per_router_never_per_call(tmp_path: Path,
                                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """The exact keys a router's tree refuses depend only on the catalogue and the router, so they are worked out when
    the Jeff is built; a call only looks its context keys up in that set (the local loop stays fast)."""
    from jeff import routing
    from jeff.testing import FakeEngine
    jeff = Jeff(FakeEngine(first_option, ALL_ROUTED), Catalogue.from_dir(spaced_routers(tmp_path)))

    def walked(*args: Any) -> Any:
        raise AssertionError("dispatch walked the routing tree")

    monkeypatch.setattr(routing, "_reachable_adapters", walked)
    decision = jeff.rate("My wardrobe is broken", context={"company_name": "A shop.", "channel": "email"})
    assert decision.function == "jeff.support.triage.urgency"
    with pytest.raises(UnknownContext, match="'company name' as company_name"):
        jeff.rate("My wardrobe is broken", context={"company name": "A shop.", "channel": "email"})


def test_a_router_installed_after_the_jeff_was_built_gets_its_exact_keys_at_its_first_dispatch(
        tmp_path: Path) -> None:
    """An engine's adapters can change after the Jeff is built (another process installs a router): its exact keys
    are worked out once, at its first dispatch."""
    from jeff.testing import FakeEngine
    engine = FakeEngine(first_option, ALL_ROUTED - {"jeff.core.router", "jeff.support.router"})
    jeff = Jeff(engine, Catalogue.from_dir(spaced_routers(tmp_path)))
    engine._installed.update({"jeff.core.router", "jeff.support.router"})
    with pytest.raises(UnknownContext, match="'company name' as company_name"):
        jeff.rate("My wardrobe is broken", context={"company name": "A shop.", "channel": "email"})
    assert jeff.rate("My wardrobe is broken", context={"company_name": "A shop.", "channel": "email"}).function \
        == "jeff.support.triage.urgency"


def test_a_graph_node_takes_a_spaced_field_by_code_name_and_refuses_its_exact_key(tmp_path: Path) -> None:
    from jeff.errors import GraphError
    from jeff.graph import graph_from_dict

    def graph(field: str) -> Any:
        return graph_from_dict({"graph": "jeff.graphs.spaced", "version": "1.0.0", "input": "message",
                                "params": ["who"], "outputs": ["kind"],
                                "nodes": {"kind": {"call": "jeff.safety.spam.message_type",
                                                   "context": {field: {"param": "who"}}}}}, "test")

    engine = fake(installed={"jeff.safety.spam"})
    jeff = Jeff(engine, Catalogue.from_dir(spaced_catalogue(tmp_path)))
    result = jeff.run_graph(graph("counterparty_name"), "WIN NOW", {"who": "Acme Ltd"})
    assert result.nodes["kind"].value == "legitimate"  # type: ignore[union-attr]
    assert engine.calls[0][1][0]["state"] == {"counterparty name": "Acme Ltd", "message": "WIN NOW"}
    with pytest.raises((GraphError, UnknownContext), match="'counterparty name' as counterparty_name"):
        jeff.run_graph(graph("counterparty name"), "WIN NOW", {"who": "Acme Ltd"})
