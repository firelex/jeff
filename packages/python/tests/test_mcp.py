"""jeff.mcp: one MCP tool per function the Jeff can call now, plus jeff_dispatch and jeff_list_functions, driven
here with the MCP SDK's in-process client (both protocol eras: the initialize handshake and the 2026 per-request
form); `jeff mcp --socket` serves the same tools over stdio, tested as a subprocess against a test engine socket."""

import copy
import json
import queue
import shutil
import subprocess
import sys
import threading
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

import anyio
import jsonschema
import pytest
import yaml
from mcp import Client
from mcp.server.lowlevel import Server

from jeff.catalogue import Catalogue
from jeff.cli import is_mcp_sdk
from jeff.client import Call, Jeff
from jeff.errors import ContractError, Unauthorised
from jeff.listing import function_listing
from jeff.mcp import build_mcp_server, check_tool_names, serve_stdio, tool_name
from jeff_runtime.testing import Running

from served import CATALOGUE, CONTEXT, FIXTURE_ADAPTERS, ROUTED, ROUTERS, TEAMS, fake, jeff_server, local, \
    remote, without_timing

FUNCTION_TOOLS = {"safety_spam_message_type", "support_triage_route", "support_triage_urgency",
                  "support_triage_needs_human"}
OWN_TOOLS = {"jeff_dispatch", "jeff_list_functions"}


@pytest.fixture(params=["legacy", "auto"])
def era(request: pytest.FixtureRequest) -> str:
    """legacy: the initialize handshake (protocol 2025-11-25 and before); auto: the 2026-07-28 per-request form."""
    return str(request.param)


def run[T](server: Server, era: str, work: Callable[[Client], Awaitable[T]]) -> T:
    async def main() -> T:
        async with Client(server, mode=era) as client:
            return await work(client)

    return anyio.run(main)


def text_of(result: Any) -> str:
    assert len(result.content) == 1 and result.content[0].type == "text"
    return str(result.content[0].text)


def test_tool_names_drop_jeff_and_replace_dots_and_dashes() -> None:
    assert tool_name("jeff.support.triage.route") == "support_triage_route"
    assert tool_name("jeff.support.support-intents.intent") == "support_support_intents_intent"


def test_tools_are_one_per_installed_function_plus_dispatch_and_listing(era: str) -> None:
    jeff = local(fake())
    tools = {tool.name: tool for tool in run(build_mcp_server(lambda: jeff), era, lambda c: c.list_tools()).tools}
    assert set(tools) == FUNCTION_TOOLS | OWN_TOOLS

    route = tools["support_triage_route"]
    assert route.description is not None
    for text in ("Which team should handle this message?", "jeff.support.triage.route", "route",
                 "The ticket or email as received.", "Not for any of these teams", "abstained"):
        assert text in route.description, text
    schema = route.input_schema
    assert schema["type"] == "object" and schema["additionalProperties"] is False
    assert set(schema["required"]) == {"input", "context", "labels"}
    assert schema["properties"]["context"]["required"] == ["company", "channel"]
    assert set(schema["properties"]["context"]["properties"]) == {"company", "channel"}
    assert schema["properties"]["labels"]["type"] == "object"
    assert schema["properties"]["labels"]["maxProperties"] == 40
    assert schema["properties"]["on_low_confidence"]["enum"] == ["return", "error"]

    spam = tools["safety_spam_message_type"]
    assert "labels" not in spam.input_schema["properties"]  # fixed labels: the caller passes none
    assert set(spam.input_schema["required"]) == {"input", "context"}
    for label in ("legitimate", "spam", "phishing", "A scam after personal details or money"):
        assert spam.description is not None and label in spam.description

    urgency = tools["support_triage_urgency"]
    assert urgency.description is not None and "Handle immediately" in urgency.description

    dispatch = tools["jeff_dispatch"].input_schema
    assert set(dispatch["required"]) == {"input", "interface"}
    assert "rate" in dispatch["properties"]["interface"]["enum"]
    assert tools["jeff_list_functions"].input_schema["properties"] == {}


def test_a_tool_call_returns_the_whole_decision_as_text_and_structured_content(era: str) -> None:
    engine = fake()
    jeff = local(engine)
    arguments: dict[str, Any] = {"input": "My wardrobe arrived broken", "context": CONTEXT, "labels": TEAMS}
    result = run(build_mcp_server(lambda: jeff), era, lambda c: c.call_tool("support_triage_route", arguments))
    assert not result.is_error
    decision = json.loads(text_of(result))
    assert decision == result.structured_content
    expected = local(fake()).decide([Call("triage.route", arguments["input"], CONTEXT, TEAMS)])[0]
    del decision["timing"]
    assert decision == without_timing(expected)
    assert decision["abstained"] is False


def test_an_abstaining_call_returns_the_whole_decision_with_abstained_true(era: str) -> None:
    """Review Focus 5: an abstained decision is a result, not an error."""
    jeff = local(fake())
    arguments = {"input": "unsure", "context": CONTEXT}
    result = run(build_mcp_server(lambda: jeff), era, lambda c: c.call_tool("support_triage_urgency", arguments))
    assert not result.is_error
    decision = json.loads(text_of(result))
    assert decision["abstained"] is True and decision["function"] == "jeff.support.triage.urgency"
    assert decision == result.structured_content


def test_on_low_confidence_error_makes_an_abstention_a_low_confidence_tool_error(era: str) -> None:
    jeff = local(fake())
    arguments = {"input": "unsure", "context": CONTEXT, "on_low_confidence": "error"}
    result = run(build_mcp_server(lambda: jeff), era, lambda c: c.call_tool("support_triage_urgency", arguments))
    assert result.is_error
    assert text_of(result).startswith("LowConfidence: ")
    assert result.structured_content["error"]["type"] == "LowConfidence"
    assert result.structured_content["error"]["details"]["decision"]["abstained"] is True


@pytest.mark.parametrize(("arguments", "error"), [
    ({"context": CONTEXT, "labels": TEAMS}, "InvalidRequest: "),  # no input
    ({"input": "x", "context": CONTEXT, "labels": TEAMS, "extra": 1}, "InvalidRequest: "),
    ({"input": "x", "context": CONTEXT, "labels": TEAMS, "on_low_confidence": "maybe"}, "InvalidRequest: "),
    ({"input": "x", "context": "a shop", "labels": TEAMS}, "InvalidRequest: "),
    ({"input": "x", "labels": TEAMS}, "MissingContext: "),  # context and labels are checked by binding, as on REST
    ({"input": "x", "context": {**CONTEXT, "colour": "red"}, "labels": TEAMS}, "UnknownContext: "),
    ({"input": "x", "context": CONTEXT}, "MissingLabels: "),
    ({"input": "x", "context": CONTEXT, "labels": {"other": "Mine"}}, "InvalidLabels: "),
    ({"input": "x", "context": CONTEXT, "labels": {f"team{index}": "A team" for index in range(41)}},
     "TooManyLabels: "),
])
def test_bad_arguments_are_tool_errors_naming_the_error_type(arguments: dict[str, Any], error: str) -> None:
    jeff = local(fake())
    result = run(build_mcp_server(lambda: jeff), "legacy", lambda c: c.call_tool("support_triage_route", arguments))
    assert result.is_error and text_of(result).startswith(error), text_of(result)


def test_an_unknown_tool_is_an_unknown_function_tool_error() -> None:
    jeff = local(fake())
    result = run(build_mcp_server(lambda: jeff), "legacy", lambda c: c.call_tool("support_triage_nope", {}))
    assert result.is_error and text_of(result).startswith("UnknownFunction: ")
    assert "support_triage_nope" in text_of(result)


def test_a_served_set_change_shows_at_the_next_listing_and_a_dropped_tool_is_not_installed(era: str) -> None:
    engine = fake()
    jeff = local(engine)

    async def work(client: Client) -> tuple[set[str], set[str], Any]:
        before = {tool.name for tool in (await client.list_tools()).tools}
        engine.remove_adapter("jeff.safety.spam")
        after = {tool.name for tool in (await client.list_tools()).tools}
        called = await client.call_tool("safety_spam_message_type", {"input": "WIN", "context": {"channel": "sms"}})
        return before, after, called

    before, after, called = run(build_mcp_server(lambda: jeff), era, work)
    assert "safety_spam_message_type" in before and "safety_spam_message_type" not in after
    assert called.is_error and text_of(called).startswith("NotInstalled: ")


def test_jeff_for_request_is_called_for_every_request() -> None:
    jeffs = [local(fake()), local(fake(installed={"jeff.safety.spam"}))]
    calls: list[int] = []

    def jeff_for_request() -> Jeff:
        calls.append(1)
        return jeffs[min(len(calls), 2) - 1]

    async def work(client: Client) -> tuple[set[str], set[str]]:
        first = {tool.name for tool in (await client.list_tools()).tools}
        second = {tool.name for tool in (await client.list_tools()).tools}
        return first, second

    first, second = run(build_mcp_server(jeff_for_request), "legacy", work)
    assert first == FUNCTION_TOOLS | OWN_TOOLS
    assert second == {"safety_spam_message_type"} | OWN_TOOLS


def test_jeff_list_functions_answers_the_v1_functions_listing(era: str) -> None:
    jeff = local(fake())
    result = run(build_mcp_server(lambda: jeff), era, lambda c: c.call_tool("jeff_list_functions", {}))
    assert not result.is_error
    assert json.loads(text_of(result)) == result.structured_content == function_listing(jeff)
    assert {item["id"] for item in result.structured_content["functions"]} == {
        "jeff.safety.spam.message_type", "jeff.support.triage.route", "jeff.support.triage.urgency",
        "jeff.support.triage.needs_human"}


def test_jeff_dispatch_equals_local_dispatch(era: str) -> None:
    jeff = local(fake(ROUTED), ROUTERS)
    arguments = {"input": "My wardrobe is broken", "interface": "rate", "context": CONTEXT}
    result = run(build_mcp_server(lambda: jeff), era, lambda c: c.call_tool("jeff_dispatch", arguments))
    assert not result.is_error, text_of(result)
    decision = json.loads(text_of(result))
    expected = local(fake(ROUTED), ROUTERS).dispatch("My wardrobe is broken", interface="rate", context=CONTEXT)
    del decision["timing"]
    assert decision == without_timing(expected)
    assert decision["route"]


def test_jeff_dispatch_from_a_router_that_does_not_lead_to_the_interface_is_invalid() -> None:
    jeff = local(fake(ROUTED), ROUTERS)
    arguments = {"input": "x", "interface": "classify", "router": "jeff.support.router", "context": CONTEXT}
    result = run(build_mcp_server(lambda: jeff), "legacy", lambda c: c.call_tool("jeff_dispatch", arguments))
    assert result.is_error and text_of(result).startswith("InvalidRequest: ")
    assert "does not lead to any function implementing classify" in text_of(result)


def test_other_exceptions_are_not_turned_into_tool_errors() -> None:
    def broken(adapter: str, row: dict[str, Any]) -> list[float]:
        raise ZeroDivisionError("a bug in the engine")

    jeff = local(fake(answer=broken))
    server = build_mcp_server(lambda: jeff)

    async def main() -> None:
        async with Client(server, mode="legacy", raise_exceptions=True) as client:
            await client.call_tool("safety_spam_message_type", {"input": "WIN", "context": {"channel": "sms"}})

    with pytest.raises(BaseException) as raised:
        anyio.run(main)
    assert "a bug in the engine" in repr(raised.value) or any(
        "a bug in the engine" in repr(error) for error in getattr(raised.value, "exceptions", ()))


def test_a_remote_jeff_serves_the_same_tools_over_a_server() -> None:
    with jeff_server(fake()) as url, remote(url) as client:
        result = run(build_mcp_server(lambda: client), "legacy",
                     lambda c: c.call_tool("support_triage_urgency", {"input": "It is on fire", "context": CONTEXT}))
        tools = run(build_mcp_server(lambda: client), "legacy", lambda c: c.list_tools()).tools
    assert {tool.name for tool in tools} == FUNCTION_TOOLS | OWN_TOOLS
    assert not result.is_error and json.loads(text_of(result))["function"] == "jeff.support.triage.urgency"


# --- jeff mcp over stdio -------------------------------------------------------------------------------------------

def lines_of(stream: Any) -> "queue.Queue[str]":
    lines: queue.Queue[str] = queue.Queue()

    def read() -> None:
        for line in stream:
            lines.put(line)

    threading.Thread(target=read, daemon=True).start()
    return lines


def test_jeff_mcp_over_an_engine_socket_answers_tools_list_over_stdio(tmp_path: Path) -> None:
    engine = Running(fake(), fixed={adapter: CATALOGUE / adapter for adapter in FIXTURE_ADAPTERS})
    stderr = (tmp_path / "stderr.txt").open("w")  # a file, so the process can never block on a full pipe
    process = subprocess.Popen(
        [sys.executable, "-m", "jeff.cli", "mcp", "--socket", str(engine.path), "--timeout-s", "5",
         "--catalogue", str(CATALOGUE)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True)
    try:
        assert process.stdin is not None and process.stdout is not None
        replies = lines_of(process.stdout)

        def send(message: dict[str, Any]) -> None:
            assert process.stdin is not None
            process.stdin.write(json.dumps(message) + "\n")
            process.stdin.flush()

        send({"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                         "clientInfo": {"name": "test", "version": "0"}}})
        assert json.loads(replies.get(timeout=20))["id"] == 1
        send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        send({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = json.loads(replies.get(timeout=20))
        assert listed["id"] == 2, listed
        assert {tool["name"] for tool in listed["result"]["tools"]} == FUNCTION_TOOLS | OWN_TOOLS
        send({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
              "params": {"name": "safety_spam_message_type",
                         "arguments": {"input": "WIN NOW", "context": {"channel": "email"}}}})
        called = json.loads(replies.get(timeout=20))
        assert called["id"] == 3 and called["result"]["isError"] is False, called
        assert called["result"]["structuredContent"]["function"] == "jeff.safety.spam.message_type"
    finally:
        process.terminate()
        process.wait(timeout=10)
        stderr.close()
        engine.close()
    assert process.returncode is not None


def test_jeff_mcp_needs_exactly_one_of_socket_and_server(capsys: pytest.CaptureFixture[str]) -> None:
    from jeff.cli import main

    with pytest.raises(SystemExit):
        main(["mcp", "--timeout-s", "5"])
    with pytest.raises(SystemExit):
        main(["mcp", "--socket", "/tmp/x.sock", "--server", "http://127.0.0.1:1", "--timeout-s", "5"])
    with pytest.raises(SystemExit):
        main(["mcp", "--socket", "/tmp/x.sock", "--token-env", "JEFF_TOKEN", "--timeout-s", "5"])
    capsys.readouterr()


def test_jeff_mcp_reads_the_token_from_the_named_variable_and_refuses_a_missing_one(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from jeff.cli import main

    monkeypatch.delenv("JEFF_TEST_MISSING_TOKEN", raising=False)
    assert main(["mcp", "--server", "http://127.0.0.1:1", "--token-env", "JEFF_TEST_MISSING_TOKEN",
                 "--timeout-s", "5"]) == 1
    assert "JEFF_TEST_MISSING_TOKEN" in capsys.readouterr().err


def test_jeff_mcp_passes_the_named_variables_token_to_the_server_connection(
        monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    from jeff.cli import main
    from jeff.errors import BackendUnavailable

    seen: dict[str, Any] = {}

    def connect(url: str, token: Any, **options: Any) -> Any:
        seen.update(url=url, token=token, **options)
        raise BackendUnavailable("stopped here by the test")

    monkeypatch.setenv("JEFF_TEST_TOKEN", "the-secret")
    monkeypatch.setattr(Jeff, "remote", staticmethod(connect))
    assert main(["mcp", "--server", "http://127.0.0.1:1", "--token-env", "JEFF_TEST_TOKEN", "--timeout-s", "5"]) == 1
    assert seen["url"] == "http://127.0.0.1:1" and seen["timeout_s"] == 5.0
    assert "stopped here by the test" in capsys.readouterr().err
    token = seen["token"]
    assert callable(token) and token() == "the-secret"  # read on each use: a refreshed variable is picked up
    monkeypatch.setenv("JEFF_TEST_TOKEN", "the-refreshed-secret")
    assert token() == "the-refreshed-secret"
    monkeypatch.delenv("JEFF_TEST_TOKEN")
    with pytest.raises(Unauthorised, match="JEFF_TEST_TOKEN"):
        token()


def test_only_the_mcp_sdk_missing_is_reported_as_the_missing_extra() -> None:
    assert is_mcp_sdk("mcp") and is_mcp_sdk("mcp.server.lowlevel") and is_mcp_sdk("mcp_types")
    assert not is_mcp_sdk("mcpx") and not is_mcp_sdk("jsonschema") and not is_mcp_sdk(None)


# --- schemas and tool names ----------------------------------------------------------------------------------------

def test_every_tool_input_schema_is_a_valid_json_schema(era: str) -> None:
    jeff = local(fake(ROUTED), ROUTERS)
    tools = run(build_mcp_server(lambda: jeff), era, lambda c: c.list_tools()).tools
    assert len(tools) > 2
    for tool in tools:
        jsonschema.Draft202012Validator.check_schema(tool.input_schema)


def variant(tmp_path: Path, *changes: tuple[str, Callable[[dict[str, Any]], None]]) -> Catalogue:
    """A copy of the fixture catalogue with each (adapter, change) applied to that adapter's manifest; a change may
    also set a new "id", which renames the adapter's folder."""
    folder = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, folder)
    for adapter, change in changes:
        path = folder / adapter / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        change(manifest)
        path.write_text(yaml.safe_dump(manifest))
        if manifest["id"] != adapter:
            path.parent.rename(folder / manifest["id"])
    return Catalogue.from_dir(folder)


def test_a_manifest_with_only_optional_context_does_not_require_context(tmp_path: Path) -> None:
    def optional(manifest: dict[str, Any]) -> None:
        manifest["context"] = [{**field, "required": False} for field in manifest["context"]]

    catalogue = variant(tmp_path, ("jeff.safety.spam", optional))
    jeff = Jeff(fake(), catalogue)
    tools = {tool.name: tool for tool in run(build_mcp_server(lambda: jeff), "legacy", lambda c: c.list_tools()).tools}
    schema = tools["safety_spam_message_type"].input_schema
    assert schema["required"] == ["input"] and schema["properties"]["context"]["required"] == []
    result = run(build_mcp_server(lambda: jeff), "legacy",
                 lambda c: c.call_tool("safety_spam_message_type", {"input": "WIN NOW"}))
    assert not result.is_error, text_of(result)


def renamed(adapter_id: str, key: str | None = None) -> Callable[[dict[str, Any]], None]:
    def change(manifest: dict[str, Any]) -> None:
        manifest["id"], manifest["aliases"] = adapter_id, []
        if key is not None:
            manifest["functions"] = {key: next(iter(manifest["functions"].values()))}

    return change


def test_two_functions_with_one_tool_name_are_refused_naming_both(tmp_path: Path) -> None:
    catalogue = variant(tmp_path, ("jeff.safety.spam", renamed("jeff.a-b.c", "d")),
                        ("jeff.support.triage", renamed("jeff.a.b-c", "d")))
    with pytest.raises(ContractError, match="a_b_c_d") as refused:
        check_tool_names(catalogue)
    assert "jeff.a-b.c.d" in str(refused.value) and "jeff.a.b-c.d" in str(refused.value)


def test_a_function_named_like_jeffs_own_tool_is_refused(tmp_path: Path) -> None:
    """jeff.jeff.list.functions is jeff_list_functions. (jeff_dispatch cannot clash: a function's tool name has at
    least three parts, adapter area, adapter name and function key.)"""
    catalogue = variant(tmp_path, ("jeff.safety.spam", renamed("jeff.jeff.list", "functions")))
    with pytest.raises(ContractError, match="jeff.jeff.list.functions.*jeff_list_functions"):
        check_tool_names(catalogue)
    jeff = Jeff(fake(installed={"jeff.support.triage"}), catalogue)
    with pytest.raises(ContractError, match="jeff_list_functions"):
        serve_stdio(jeff)  # refused before stdin and stdout are touched


def test_jeff_mcp_refuses_a_catalogue_with_clashing_tool_names_at_startup(
        tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from jeff.cli import main

    variant(tmp_path, ("jeff.safety.spam", renamed("jeff.jeff.list", "functions")))
    engine = Running(fake(installed={"jeff.support.triage"}),
                     fixed={"jeff.support.triage": CATALOGUE / "jeff.support.triage"})
    try:
        assert main(["mcp", "--socket", str(engine.path), "--timeout-s", "5",
                     "--catalogue", str(tmp_path / "catalogue")]) == 1
    finally:
        engine.close()
    assert "jeff_list_functions" in capsys.readouterr().err


def test_fixed_labels_with_a_subset_take_an_optional_list_of_their_keys() -> None:
    from jeff import mcp
    from jeff.manifest import manifest_from_dict
    spam = Catalogue.from_dir(CATALOGUE).adapter("jeff.safety.spam")
    raw = copy.deepcopy(spam.raw)
    raw["functions"]["message_type"]["labels"]["subset"] = True
    manifest = manifest_from_dict(raw, spam.path)
    schema = mcp._input_schema(manifest.functions["message_type"])
    assert "labels" not in schema["required"]
    assert schema["properties"]["labels"]["items"] == {"enum": ["legitimate", "spam", "phishing"]}
    assert schema["properties"]["labels"]["minItems"] == 1 and schema["properties"]["labels"]["uniqueItems"] is True
    assert "omit 'labels' to offer them all" in schema["properties"]["labels"]["description"]
