"""Plain-text input (input.form text): some adapters were trained on a state that is one plain string, not an object of
named fields. Jeff sends the caller's input unwrapped, as trained; such a manifest has no context, and a call that
passes context is refused."""

import copy
import shutil
import string
from pathlib import Path
from typing import Any

import pytest
import yaml

from jeff import contract, mcp
from jeff.binding import bind, build_row
from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import ContractError, InvalidRequest, ManifestError, UnknownContext
from jeff.evaluate import split_state, template_rows
from jeff.listing import function_listing
from jeff.manifest import Manifest, manifest_from_dict
from jeff_runtime.engine.model import PROMPT_LAYOUTS, decision_messages

from served import CATALOGUE, Mode, fake, jeff_server

SPAM = CATALOGUE / "jeff.safety.spam" / "manifest.yaml"
PUZZLE = "Lotte has the yellow boat and Felix the green one. They switch boats. Felix has the"
TEXT_INPUT = {"key": "puzzle", "description": "The puzzle, as one text.", "form": "text"}


def text_raw(**input_changes: Any) -> dict[str, Any]:
    raw = yaml.safe_load(SPAM.read_text())
    raw["context"] = []
    raw["input"] = {**TEXT_INPUT, **input_changes}
    return raw


def text_manifest() -> Manifest:
    return manifest_from_dict(text_raw(), SPAM)


def text_catalogue(tmp_path: Path) -> Path:
    folder = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, folder)
    (folder / "jeff.safety.spam" / "manifest.yaml").write_text(yaml.safe_dump(text_raw()))
    return folder


def test_input_form_is_required() -> None:
    raw = text_raw()
    del raw["input"]["form"]
    with pytest.raises(ContractError, match="form"):
        manifest_from_dict(raw, SPAM)
    with pytest.raises(ContractError):
        manifest_from_dict(text_raw(form="plain"), SPAM)


def test_a_text_input_is_read_with_its_form() -> None:
    manifest = text_manifest()
    assert (manifest.input.key, manifest.input.code_name, manifest.input.form) == ("puzzle", "puzzle", "text")
    assert manifest.context == ()


def test_a_text_input_with_context_fields_is_refused() -> None:
    raw = text_raw()
    raw["context"] = [{"key": "channel", "description": "Where it arrived.", "required": False}]
    with pytest.raises(ManifestError, match="form text .* no context fields, but it has \\['channel'\\]"):
        manifest_from_dict(raw, SPAM)


def test_the_state_is_the_input_string_unwrapped() -> None:
    manifest = text_manifest()
    function = manifest.functions["message_type"]
    row = build_row(bind(manifest, function, None, None, calibration=None), PUZZLE)
    assert row["state"] == PUZZLE
    # an empty context is no context
    assert build_row(bind(manifest, function, {}, None, calibration=None), PUZZLE)["state"] == PUZZLE


def test_a_text_input_must_be_a_string() -> None:
    manifest = text_manifest()
    binding = bind(manifest, manifest.functions["message_type"], None, None, calibration=None)
    with pytest.raises(InvalidRequest, match="takes plain text: its input puzzle must be a string, got dict"):
        build_row(binding, {"puzzle": PUZZLE})


def test_a_call_with_context_is_refused() -> None:
    manifest = text_manifest()
    with pytest.raises(UnknownContext, match="takes plain text only .* do not pass context \\(got channel\\)"):
        bind(manifest, manifest.functions["message_type"], {"channel": "email"}, None, calibration=None)


def test_a_fields_input_still_wraps_the_input_under_its_key() -> None:
    raw = yaml.safe_load(SPAM.read_text())
    manifest = manifest_from_dict(raw, SPAM)
    binding = bind(manifest, manifest.functions["message_type"], {"channel": "sms"}, None, calibration=None)
    assert build_row(binding, PUZZLE)["state"] == {"channel": "sms", "message": PUZZLE}


def test_both_prompt_layouts_render_a_plain_text_state_as_the_trainer_did() -> None:
    """The v1.4 trainer (datigator ~/v14-lane/trainer/src/jeff/model.py, SHA-256 8c5579d3..., the code_sha256 the
    formal-reasoning, knowledge-qa-v2 and long-contracts recipes record) puts a non-object state first in either
    layout: "State:\\n" + the text, then the question and options. The runtime must render exactly that."""
    manifest = text_manifest()
    row = build_row(bind(manifest, manifest.functions["message_type"], None, None, calibration=None), PUZZLE)
    codes = list(string.ascii_uppercase)
    expected = ("State:\n" + PUZZLE + "\n\nQuestion:\nWhat kind of message is this?\n\nOptions:\n"
                "A: legitimate: A normal message\nB: spam: Unwanted bulk or advertising\n"
                "C: phishing: A scam after personal details or money\n\nReturn only the letter code of the best option.")
    for layout in PROMPT_LAYOUTS:
        [_, user] = decision_messages(row, codes, layout)
        assert user["content"] == [{"type": "text", "text": expected}], layout


def test_test_rows_with_a_plain_text_state_are_rebuilt_as_calls() -> None:
    manifest = text_manifest()
    assert split_state(manifest.functions["message_type"], PUZZLE) == ({}, PUZZLE)
    assert split_state(manifest.functions["message_type"], {"puzzle": PUZZLE}) is None
    question = {"type": "choice", "instructions": "What kind of message is this?",
                "criteria": copy.deepcopy(manifest.raw["functions"]["message_type"]["labels"]["fixed"])}
    rows = [{"id": "t-1-message_type", "state": PUZZLE, "question": question, "label": "spam"},
            {"id": "t-2-message_type", "state": {"puzzle": PUZZLE}, "question": question, "label": "spam"}]
    rebuilt, skipped = template_rows(manifest, rows)
    assert [(row.id, row.call.input, row.call.context) for row in rebuilt] == [("t-1-message_type", PUZZLE, {})]
    assert skipped == {"state is not plain text": 1}


def test_the_listing_and_the_mcp_tool_say_the_input_is_plain_text(tmp_path: Path) -> None:
    jeff = Jeff(fake(installed={"jeff.safety.spam"}), Catalogue.from_dir(text_catalogue(tmp_path)))
    listing = function_listing(jeff)
    contract.validate("functions_response", listing, "the listing")
    listed = listing["functions"][0]
    assert listed["input"] == {**TEXT_INPUT, "code_name": "puzzle"} and listed["context"] == []
    manifest = text_manifest()
    schema = mcp._input_schema(manifest.functions["message_type"])
    assert schema["properties"]["input"] == {"type": "string", "description": "The puzzle, as one text. Plain text."}
    assert "context" not in schema["properties"]


def test_a_remote_call_sends_the_text_unwrapped_and_refuses_context(tmp_path: Path, mode: Mode) -> None:
    catalogue = text_catalogue(tmp_path)
    engine = fake(installed={"jeff.safety.spam"})
    with jeff_server(engine, catalogue) as url, mode.remote(url, catalogue=catalogue) as client:
        assert client.function("spam.message_type")(PUZZLE).value == "legitimate"
        with pytest.raises(UnknownContext, match="takes plain text only"):
            client.function("spam.message_type", context={"channel": "email"})(PUZZLE)
    [(_, rows)] = engine.calls
    assert rows[0]["state"] == PUZZLE


def test_an_escalated_question_of_a_text_function_has_the_text_as_its_state(tmp_path: Path) -> None:
    """Question.state is what the model read: for form text, the plain string (not an object of fields)."""
    from jeff.escalation import Question, escalate, human
    from jeff.testing import FakeEngine
    seen: list[Question] = []

    def ask(question: Question) -> str:
        seen.append(question)
        return "spam"

    unsure = FakeEngine(lambda adapter, row: [0.4, 0.3, 0.3], {"jeff.safety.spam"})
    decision = Jeff(unsure, Catalogue.from_dir(text_catalogue(tmp_path))).classify(
        PUZZLE, function="spam.message_type", on_low_confidence=escalate(human(ask)))
    assert decision.value == "spam" and decision.decided_by == "human"
    assert seen[0].state == PUZZLE
    import typing
    assert typing.get_type_hints(Question)["state"] == dict[str, Any] | str
