"""Several request shapes per adapter: some adapters were trained on more than one input layout (for example the open
ground adapter's (sources, answer) and (sources, question)). Each layout is its own function, which declares its
own context and input; a function that declares neither uses the adapter's. Test rows are mapped to their function by
the state_shape criterion: the row's state keys, in order, are exactly the function's context keys and then its input
key, or the state is a plain string for a function whose input form is text."""

import copy
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
import yaml

from jeff import contract, mcp
from jeff.binding import bind, build_row
from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import ContractError, ManifestError, MissingContext, UnknownContext
from jeff.evaluate import split_state, template_rows
from jeff.gen import python_module
from jeff.listing import function_listing
from jeff.manifest import Manifest, StateShape, function_key_of_test_row, function_of_test_row, manifest_from_dict

from served import CATALOGUE, Mode, fake, jeff_server

SPAM = CATALOGUE / "jeff.safety.spam" / "manifest.yaml"
THREAD_CONTEXT = [{"key": "thread owner", "description": "Who started the thread.", "required": True}]
THREAD_INPUT = {"key": "thread", "description": "The whole thread.", "form": "fields"}
NOTE_INPUT = {"key": "note", "description": "A short note, as one text.", "form": "text"}


def shaped_raw() -> dict[str, Any]:
    """The spam fixture with three request shapes: message_type keeps the adapter's (channel, message);
    thread_type takes ("thread owner", thread); note_type takes a plain-text note."""
    raw = yaml.safe_load(SPAM.read_text())
    base = raw["functions"]["message_type"]
    raw["functions"]["thread_type"] = {**copy.deepcopy(base), "instructions": "What kind of thread is this?",
                                       "context": copy.deepcopy(THREAD_CONTEXT), "input": dict(THREAD_INPUT)}
    raw["functions"]["note_type"] = {**copy.deepcopy(base), "instructions": "What kind of note is this?",
                                     "context": [], "input": dict(NOTE_INPUT)}
    raw["tests"]["function_by"] = "state_shape"
    return raw


def shaped() -> Manifest:
    return manifest_from_dict(shaped_raw(), SPAM)


def shaped_catalogue(tmp_path: Path) -> Path:
    folder = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, folder)
    (folder / "jeff.safety.spam" / "manifest.yaml").write_text(yaml.safe_dump(shaped_raw()))
    return folder


def row(row_id: str, state: Any, kind: str | None = None) -> dict[str, Any]:
    raw = shaped_raw()
    out: dict[str, Any] = {"id": row_id, "state": state, "label": "spam",
                           "question": {"type": "choice", "instructions": "",
                                        "criteria": copy.deepcopy(raw["functions"]["message_type"]["labels"]["fixed"])}}
    if kind is not None:
        out["source"] = {"kind": kind}
    return out


# --- the format -------------------------------------------------------------------------------------------------


def test_a_function_that_declares_context_must_declare_input_and_the_other_way_round() -> None:
    raw = shaped_raw()
    del raw["functions"]["thread_type"]["input"]
    with pytest.raises(ContractError, match="input"):
        manifest_from_dict(raw, SPAM)
    raw = shaped_raw()
    del raw["functions"]["thread_type"]["context"]
    with pytest.raises(ContractError, match="context"):
        manifest_from_dict(raw, SPAM)


def test_each_function_has_its_own_fields_or_the_adapters() -> None:
    manifest = shaped()
    message, thread, note = (manifest.functions[key] for key in ("message_type", "thread_type", "note_type"))
    assert message.context == manifest.context and message.input == manifest.input
    assert [(field.key, field.code_name) for field in thread.context] == [("thread owner", "thread_owner")]
    assert (thread.input.key, thread.input.form) == ("thread", "fields")
    assert note.context == () and (note.input.key, note.input.form) == ("note", "text")
    # the adapter's own fields are unchanged
    assert [field.key for field in manifest.context] == ["channel"] and manifest.input.key == "message"


def test_a_functions_own_text_input_with_context_is_refused() -> None:
    raw = shaped_raw()
    raw["functions"]["note_type"]["context"] = copy.deepcopy(THREAD_CONTEXT)
    with pytest.raises(ManifestError, match="function note_type: input 'note' has form text .* \\['thread owner'\\]"):
        manifest_from_dict(raw, SPAM)


def test_a_functions_own_fields_are_checked_like_the_adapters() -> None:
    raw = shaped_raw()
    raw["functions"]["thread_type"]["input"]["key"] = "thread owner"
    with pytest.raises(ManifestError, match="function thread_type: context keys must be unique and differ from the "
                                            "input key"):
        manifest_from_dict(raw, SPAM)
    raw = shaped_raw()
    raw["functions"]["thread_type"]["context"].append({"key": "Thread Owner", "description": "x", "required": False})
    with pytest.raises(ManifestError, match="function thread_type: fields share a code name.*thread_owner"):
        manifest_from_dict(raw, SPAM)


def test_existing_manifests_have_no_function_fields_and_mean_what_they_meant() -> None:
    raw = yaml.safe_load(SPAM.read_text())
    manifest = manifest_from_dict(raw, SPAM)
    function = manifest.functions["message_type"]
    assert function.context is manifest.context and function.input is manifest.input


# --- binding ----------------------------------------------------------------------------------------------------


def test_binding_uses_the_functions_own_fields() -> None:
    manifest = shaped()
    thread = manifest.functions["thread_type"]
    binding = bind(manifest, thread, {"thread_owner": "Ann"}, None, calibration=None)
    assert build_row(binding, "hi all")["state"] == {"thread owner": "Ann", "thread": "hi all"}
    with pytest.raises(UnknownContext, match="has no context field\\(s\\) channel; its fields are thread_owner"):
        bind(manifest, thread, {"channel": "email", "thread_owner": "Ann"}, None, calibration=None)
    with pytest.raises(MissingContext, match="thread_owner"):
        bind(manifest, thread, None, None, calibration=None)
    with pytest.raises(UnknownContext, match="'thread owner' as thread_owner"):
        bind(manifest, thread, {"thread owner": "Ann"}, None, calibration=None)


def test_a_text_function_of_a_fields_adapter_sends_its_input_unwrapped_and_refuses_context() -> None:
    manifest = shaped()
    note = manifest.functions["note_type"]
    assert build_row(bind(manifest, note, None, None, calibration=None), "buy now")["state"] == "buy now"
    with pytest.raises(UnknownContext, match="takes plain text only .* \\(got channel\\)"):
        bind(manifest, note, {"channel": "email"}, None, calibration=None)


def test_a_function_without_its_own_fields_binds_as_before() -> None:
    manifest = shaped()
    binding = bind(manifest, manifest.functions["message_type"], {"channel": "sms"}, None, calibration=None)
    assert build_row(binding, "hi")["state"] == {"channel": "sms", "message": "hi"}


# --- mapping test rows by state_shape ---------------------------------------------------------------------------


def test_state_shape_is_the_context_keys_then_the_input_key_or_text() -> None:
    from jeff.manifest import state_shape
    manifest = shaped()
    assert state_shape(manifest.functions["message_type"]) == ("channel", "message")
    assert state_shape(manifest.functions["thread_type"]) == ("thread owner", "thread")
    assert state_shape(manifest.functions["note_type"]) == "text"


def test_rows_map_to_the_function_whose_shape_their_state_has() -> None:
    manifest = shaped()
    assert function_of_test_row(manifest, row("r1", {"channel": "sms", "message": "hi"})).key == "message_type"
    assert function_of_test_row(manifest, row("r2", {"thread owner": "Ann", "thread": "hi"})).key == "thread_type"
    assert function_of_test_row(manifest, row("r3", "buy now")).key == "note_type"


def test_a_row_of_no_shape_is_refused_naming_it() -> None:
    manifest = shaped()
    for state in ({"message": "hi"}, {"channel": "sms", "message": "hi", "extra": 1},
                  {"message": "hi", "channel": "sms"}, ["a list"]):  # a missing, extra or reordered key; no object
        with pytest.raises(ManifestError, match="test row r9 matches no function: after state_shape"):
            function_of_test_row(manifest, row("r9", state))


def test_two_functions_of_one_shape_need_another_criterion() -> None:
    raw = shaped_raw()
    raw["functions"]["other_type"] = copy.deepcopy(raw["functions"]["thread_type"])
    manifest = manifest_from_dict(raw, SPAM)
    with pytest.raises(ManifestError, match="test row r2 matches several functions \\(thread_type, other_type\\)"):
        function_of_test_row(manifest, row("r2", {"thread owner": "Ann", "thread": "hi"}))
    raw["tests"]["function_by"] = ["source_kind", "state_shape"]
    raw["tests"]["kinds"] = {"thread": "thread_type", "other": "other_type", "message": "message_type",
                             "note": "note_type"}
    manifest = manifest_from_dict(raw, SPAM)
    assert function_of_test_row(manifest, row("r2", {"thread owner": "Ann", "thread": "hi"}, "other")).key \
        == "other_type"
    # source_kind names a function whose shape the state does not have
    with pytest.raises(ManifestError, match="test row r4 matches no function: after state_shape"):
        function_of_test_row(manifest, row("r4", "buy now", "thread"))


def test_one_kind_with_text_and_named_field_rows_maps_to_several_functions_split_by_shape() -> None:
    """long-contracts' nli kind holds both plain-text and named-field rows, and its named-field rows have the shape of
    another kind's (nli-evidence), so only source_kind and state_shape together tell all three apart: tests.kinds maps
    the one kind to the list of its functions, and state_shape picks among them."""
    raw = shaped_raw()
    raw["functions"]["other_type"] = copy.deepcopy(raw["functions"]["thread_type"])
    raw["tests"]["function_by"] = ["source_kind", "state_shape"]
    raw["tests"]["kinds"] = {"passage": ["thread_type", "note_type"], "evidence": "other_type",
                             "message": "message_type"}
    manifest = manifest_from_dict(raw, SPAM)
    assert function_of_test_row(manifest, row("r5", "buy now", "passage")).key == "note_type"
    assert function_of_test_row(manifest, row("r6", {"thread owner": "A", "thread": "t"}, "passage")).key \
        == "thread_type"
    assert function_of_test_row(manifest, row("r7", {"thread owner": "A", "thread": "t"}, "evidence")).key \
        == "other_type"
    # a kind mapped to several functions with source_kind alone could never leave one: refused when it loads
    with pytest.raises(ManifestError, match="tests.kinds maps \\['passage'\\] to several functions each, but "
                                            "tests.function_by is source_kind alone"):
        manifest_from_dict({**raw, "tests": {**raw["tests"], "function_by": "source_kind"}}, SPAM)
    # with another criterion before it, the kind list can still be narrowed: accepted
    manifest_from_dict({**raw, "tests": {**raw["tests"], "function_by": ["state_shape", "source_kind"]}}, SPAM)
    raw["tests"]["kinds"]["passage"] = ["thread_type", "no_such"]
    with pytest.raises(ManifestError, match="tests.kinds names \\['no_such'\\]"):
        manifest_from_dict(raw, SPAM)
    raw["tests"]["kinds"]["passage"] = []
    with pytest.raises(ContractError):
        manifest_from_dict(raw, SPAM)


def test_state_shape_combines_with_question_type() -> None:
    raw = shaped_raw()
    raw["tests"]["function_by"] = ["question_type", "state_shape"]
    manifest = manifest_from_dict(raw, SPAM)
    assert function_of_test_row(manifest, row("r1", {"channel": "sms", "message": "hi"})).key == "message_type"


def test_the_criterion_is_usable_without_a_manifest_as_the_importer_maps_rows() -> None:
    types = {"message_type": "choice", "note_type": "choice"}
    shapes: dict[str, StateShape] = {"message_type": ("channel", "message"), "note_type": "text"}
    assert function_key_of_test_row("imp", "state_shape", types, row("r1", "hi"), None, shapes, None) == "note_type"
    with pytest.raises(ManifestError, match="imp: tests.function_by uses state_shape, but no request shapes"):
        function_key_of_test_row("imp", "state_shape", types, row("r1", "hi"), None, None, None)


def test_test_rows_of_every_shape_are_rebuilt_as_calls() -> None:
    manifest = shaped()
    assert split_state(manifest.functions["thread_type"], {"thread owner": "Ann", "thread": "hi"}) \
        == ({"thread_owner": "Ann"}, "hi")
    assert split_state(manifest.functions["note_type"], "buy now") == ({}, "buy now")
    rows = [row("r1", {"channel": "sms", "message": "hi"}), row("r2", {"thread owner": "Ann", "thread": "t"}),
            row("r3", "buy now")]
    rebuilt, skipped = template_rows(manifest, rows)
    assert skipped == {}
    assert [(item.function, item.call.input, item.call.context) for item in rebuilt] == [
        ("jeff.safety.spam.message_type", "hi", {"channel": "sms"}),
        ("jeff.safety.spam.thread_type", "t", {"thread_owner": "Ann"}),
        ("jeff.safety.spam.note_type", "buy now", {})]


# --- what callers see -------------------------------------------------------------------------------------------


def test_the_listing_shows_each_functions_own_fields(tmp_path: Path) -> None:
    jeff = Jeff(fake(installed={"jeff.safety.spam"}), Catalogue.from_dir(shaped_catalogue(tmp_path)))
    listing = function_listing(jeff)
    contract.validate("functions_response", listing, "the listing")
    listed = {entry["id"].rsplit(".", 1)[1]: entry for entry in listing["functions"]}
    assert listed["message_type"]["context"] == [{"key": "channel", "description": "Where the message arrived.",
                                                  "required": True, "code_name": "channel"}]
    assert listed["message_type"]["input"] == {"key": "message", "description": "The message.", "form": "fields",
                                               "code_name": "message"}
    assert listed["thread_type"]["context"] == [{**THREAD_CONTEXT[0], "code_name": "thread_owner"}]
    assert listed["thread_type"]["input"] == {**THREAD_INPUT, "code_name": "thread"}
    assert listed["note_type"]["context"] == [] and listed["note_type"]["input"] == {**NOTE_INPUT, "code_name": "note"}


def test_each_mcp_tool_takes_its_functions_own_fields() -> None:
    manifest = shaped()
    thread, note = manifest.functions["thread_type"], manifest.functions["note_type"]
    schema = mcp._input_schema(thread)
    assert list(schema["properties"]["context"]["properties"]) == ["thread_owner"]
    assert schema["properties"]["input"]["description"].startswith("The whole thread.")
    description = mcp._description(manifest, thread)
    assert "Input: The whole thread." in description and "thread_owner (required)" in description
    assert "channel" not in description
    schema = mcp._input_schema(note)
    assert schema["properties"]["input"]["type"] == "string" and "context" not in schema["properties"]
    assert "Context fields" not in mcp._description(manifest, note)


def test_jeff_gen_types_each_functions_own_context_and_input(tmp_path: Path) -> None:
    import subprocess
    source = python_module(Catalogue.from_dir(shaped_catalogue(tmp_path)), ["spam"])
    (tmp_path / "functions.py").write_text(source)
    (tmp_path / "use.py").write_text(
        "from functions import SafetySpamMessageType, SafetySpamNoteType, SafetySpamThreadType\n"
        "def thread(c: SafetySpamThreadType.Context, i: SafetySpamThreadType.Input) -> None: ...\n"
        "def message(c: SafetySpamMessageType.Context) -> None: ...\n"
        "def note(i: SafetySpamNoteType.Input) -> None: ...\n"
        "thread({'thread_owner': 'Ann'}, 'hi')\n"
        "message({'channel': 'sms'})\n"
        "note('buy now')\n"
        "thread({'channel': 'sms'}, 'hi')\n"  # line 8: not a thread_type field (and thread_owner is missing)
        "note({'note': 'x'})\n")  # line 9: a text input is a string
    result = subprocess.run([sys.executable, "-m", "mypy", "--strict", "use.py"], cwd=tmp_path, capture_output=True,
                            text=True)
    errors = [line for line in result.stdout.splitlines() if ": error:" in line]
    assert {line.split(":")[1] for line in errors} == {"8", "9"}, result.stdout


def test_a_remote_call_binds_each_function_with_its_own_fields(tmp_path: Path, mode: Mode) -> None:
    catalogue = shaped_catalogue(tmp_path)
    engine = fake(installed={"jeff.safety.spam"})
    with jeff_server(engine, catalogue) as url, mode.remote(url, catalogue=catalogue) as client:
        client.function("spam.thread_type", context={"thread_owner": "Ann"})("hi all")
        client.function("spam.note_type")("buy now")
        with pytest.raises(UnknownContext, match="channel"):
            client.function("spam.thread_type", context={"channel": "sms", "thread_owner": "Ann"})("hi")
    states = [rows[0]["state"] for _, rows in engine.calls]
    assert states == [{"thread owner": "Ann", "thread": "hi all"}, "buy now"]


ROUTERS = CATALOGUE.parent / "routers"
ALL_ROUTED = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}


def routers_with_own_fields(tmp_path: Path) -> Path:
    """The routing fixtures with spam.message_type declaring its own fields: ("sender name", message)."""
    folder = tmp_path / "routers"
    shutil.copytree(ROUTERS, folder)
    path = folder / "jeff.safety.spam" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["functions"]["message_type"]["context"] = [{"key": "sender name", "description": "Who sent it.",
                                                    "required": True}]
    raw["functions"]["message_type"]["input"] = copy.deepcopy(raw["input"])
    path.write_text(yaml.safe_dump(raw))
    return folder


def first_option(adapter: str, row: dict[str, Any]) -> list[float]:
    if row["question"]["type"] == "score":
        return [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(row["question"]["criteria"]) if "criteria" in row["question"] else 2
    return [1.0] + [0.0] * (count - 1)


def test_dispatch_gives_the_leaf_its_own_fields_and_refuses_their_exact_keys(tmp_path: Path) -> None:
    from jeff.testing import FakeEngine
    engine = FakeEngine(first_option, ALL_ROUTED)
    jeff = Jeff(engine, Catalogue.from_dir(routers_with_own_fields(tmp_path)))
    decision = jeff.classify("WIN NOW", context={"sender_name": "Ann", "channel": "email"})
    assert decision.function == "jeff.safety.spam.message_type"
    assert engine.calls[-1][1][0]["state"] == {"sender name": "Ann", "message": "WIN NOW"}
    calls = len(engine.calls)
    with pytest.raises(UnknownContext, match="'sender name' as sender_name"):
        jeff.classify("WIN NOW", context={"sender name": "Ann"})
    assert len(engine.calls) == calls  # refused before any router ran
