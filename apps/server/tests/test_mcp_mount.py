"""/mcp: Jeff's MCP tools over streamable HTTP, behind the same credentials, licences and rate limits as /v1, driven
with the MCP SDK's own client against a real uvicorn (both protocol eras: the initialize handshake and the 2026
per-request form)."""

import json
import shutil
import threading
import time
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import anyio
import httpx
import httpx2
import pytest
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from jeff.errors import ContractError
from jeff.testing import FakeEngine

from jeff_server.app import create_app
from jeff_server.auth import RateLimiter
from jeff_server.settings import ServerSettings

from server_testing import free_port, proprietary, token

ROOT = Path(__file__).resolve().parents[3]
CATALOGUE = ROOT / "packages" / "python" / "tests" / "fixtures" / "catalogue"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
SPAM = {"input": "WIN NOW", "context": {"channel": "email"}}
URGENCY = {"input": "It is on fire", "context": CONTEXT}
TOOLS = {"safety_spam_message_type", "support_triage_route", "support_triage_urgency", "support_triage_needs_human",
         "jeff_dispatch", "jeff_list_functions"}


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return [0.0, 0.5, 0.5, 0.0, 0.0] if "unsure" in json.dumps(row["state"]) else [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def fake(installed: set[str] | None = None) -> FakeEngine:
    return FakeEngine(answers, installed if installed is not None else {"jeff.safety.spam", "jeff.support.triage"})


@pytest.fixture(params=["legacy", "auto"])
def era(request: pytest.FixtureRequest) -> str:
    return str(request.param)


@contextmanager
def serving(engine: FakeEngine, **options: Any) -> Iterator[tuple[str, Any]]:
    """create_app over `engine`, served by uvicorn on a free port in a thread; yields the base URL and the app,
    and stops the server (checked stopped) on exit."""
    options.setdefault("catalogue", CATALOGUE)
    app = create_app(ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                                    **options), engine=engine)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"uvicorn did not start on port {port}")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}", app
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn did not stop"


def mcp[T](url: str, era: str, work: Callable[[Client], Awaitable[T]], bearer: str | None = None,
           sent: list[str] | None = None, statuses: list[int] | None = None) -> T:
    """Runs `work` with an MCP client of `era` connected to `url`/mcp; with `sent`, appends the JSON-RPC method of
    every HTTP request the client sends ("GET" or "DELETE" for a request without a body); with `statuses`, the HTTP
    status of every response."""

    async def record(request: httpx2.Request) -> None:
        assert sent is not None
        body = request.content
        sent.append(json.loads(body)["method"] if body else request.method)

    async def status(response: httpx2.Response) -> None:
        assert statuses is not None
        statuses.append(response.status_code)

    async def main() -> T:
        headers = {"Authorization": f"Bearer {bearer}"} if bearer is not None else {}
        hooks: dict[str, list[Any]] = {}
        if sent is not None:
            hooks["request"] = [record]
        if statuses is not None:
            hooks["response"] = [status]
        async with httpx2.AsyncClient(headers=headers, timeout=10, event_hooks=hooks) as http:
            async with Client(streamable_http_client(f"{url}/mcp", http_client=http), mode=era) as client:
                return await work(client)

    return anyio.run(main)


def text_of(result: Any) -> str:
    assert len(result.content) == 1 and result.content[0].type == "text"
    return str(result.content[0].text)


def test_tools_are_listed_and_called_over_http(era: str) -> None:
    with serving(fake()) as (url, _):
        async def work(client: Client) -> tuple[set[str], Any]:
            listed = {tool.name for tool in (await client.list_tools()).tools}
            return listed, await client.call_tool("support_triage_urgency", URGENCY)

        listed, called = mcp(url, era, work)
    assert listed == TOOLS
    assert not called.is_error
    decision = json.loads(text_of(called))
    assert decision == called.structured_content
    assert decision["function"] == "jeff.support.triage.urgency" and decision["value"] == 2


def test_an_abstaining_tool_call_returns_the_whole_decision_with_abstained_true(era: str) -> None:
    """Review Focus 5, first half."""
    with serving(fake()) as (url, _):
        result = mcp(url, era, lambda c: c.call_tool("support_triage_urgency", {**URGENCY, "input": "unsure"}))
    assert not result.is_error
    decision = json.loads(text_of(result))
    assert decision["abstained"] is True and decision == result.structured_content
    assert set(decision) >= {"value", "confidence", "abstained", "calibrated", "probabilities", "decided_by"}


def test_a_proprietary_tool_without_its_licence_is_a_not_licensed_tool_error(
        era: str, keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    """Review Focus 5, second half: over /mcp with a hub token lacking the licence."""
    private, public = keys
    engine = fake()
    with serving(engine, catalogue=proprietary(CATALOGUE, tmp_path, "jeff.support.triage"), hub_public_key=public) as (url, _):
        refused = mcp(url, era, lambda c: c.call_tool("support_triage_urgency", URGENCY), token(private))
        assert engine.calls == []  # refused before the engine saw a row
        opened = mcp(url, era, lambda c: c.call_tool("safety_spam_message_type", SPAM), token(private))
        licensed = mcp(url, era, lambda c: c.call_tool("support_triage_urgency", URGENCY),
                       token(private, licences=["jeff.support.triage"]))
    assert refused.is_error and text_of(refused).startswith("NotLicensed: ")
    assert "jeff.support.triage" in text_of(refused)
    assert refused.structured_content["error"]["type"] == "NotLicensed"
    assert not opened.is_error and not licensed.is_error


def test_mcp_without_credentials_is_401_with_a_request_id(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    _, public = keys
    with serving(fake(), hub_public_key=public, api_key="the-key") as (url, _):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        for headers in ({}, {"Authorization": "Bearer the-kez"}, {"Authorization": "the-key"}):
            response = httpx.post(f"{url}/mcp", json=body, headers=headers)
            assert response.status_code == 401, response.text
            assert response.headers["www-authenticate"] == "Bearer"
            assert response.headers["x-request-id"]
            assert response.json()["error"]["type"] == "Unauthorised"
        with pytest.raises(BaseException):  # the SDK's client gives up on a 401
            mcp(url, "legacy", lambda c: c.list_tools())
        listed = mcp(url, "legacy", lambda c: c.list_tools(), "the-key")
    assert {tool.name for tool in listed.tools} == TOOLS


def test_a_public_server_needs_credentials_on_mcp(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    with serving(fake(), public=True, hub_public_key=public, rate_limit=(100.0, 5)) as (url, _):
        response = httpx.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 401 and response.json()["error"]["type"] == "Unauthorised"
        listed = mcp(url, "legacy", lambda c: c.list_tools(), token(private))
    assert {tool.name for tool in listed.tools} == TOOLS


def test_every_mcp_response_carries_a_request_id_and_is_counted_under_its_route() -> None:
    with serving(fake()) as (url, app):
        initialize = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                      "params": {"protocolVersion": "2025-11-25", "capabilities": {},
                                 "clientInfo": {"name": "test", "version": "0"}}}
        response = httpx.post(f"{url}/mcp", json=initialize,
                              headers={"Accept": "application/json, text/event-stream"})
        assert response.status_code == 200, response.text
        assert response.headers["x-request-id"]
        assert httpx.put(f"{url}/mcp").status_code == 405
        counts = app.state.request_counts.snapshot()
    assert counts.get(("/mcp", 200)) == 1, counts
    assert counts[("/mcp", 405)] == 1


# Every HTTP request an MCP client of each era sends to call one tool twice: the handshake (or the 2026 discovery
# request), the call, the tools/list the SDK's client sends after the first call of a tool it has not seen listed (to
# check the result against the tool's output schema), and the second call.
TWO_CALLS = {"legacy": ["initialize", "notifications/initialized", "tools/call", "tools/list", "tools/call"],
             "auto": ["server/discover", "tools/call", "tools/list", "tools/call"]}


def test_every_mcp_request_counts_against_the_rate_limit_and_over_it_is_429(
        era: str, keys: tuple[Ed25519PrivateKey, Path]) -> None:
    """Every POST to /mcp is one request for the rate limiter, whatever MCP message it carries. With a bucket of
    exactly one request fewer than TWO_CALLS needs (and a clock that never refills it), the second tool call is
    refused with 429 RateLimited and Retry-After, as on /v1; another caller is still served and no slot stays held."""
    private, public = keys
    alice, bob = token(private, sub="alice"), token(private, sub="bob")

    async def two_calls(client: Client) -> list[Any]:
        return [await client.call_tool("safety_spam_message_type", SPAM) for _ in range(2)]

    with serving(fake(), hub_public_key=public, rate_limit=(1000.0, 5)) as (url, app):
        sent: list[str] = []
        unlimited = mcp(url, era, two_calls, alice, sent)
        assert sent == TWO_CALLS[era] and not any(result.is_error for result in unlimited)

        budget = len(TWO_CALLS[era]) - 1
        app.state.limiter = RateLimiter(float(budget), 5, clock=lambda: 0.0)
        sent, statuses = [], list[int]()
        with pytest.raises(BaseException) as refused:  # the SDK's client gives up on a refused HTTP request
            mcp(url, era, two_calls, alice, sent, statuses)
        assert sent == TWO_CALLS[era]
        assert statuses[-1] == 429 and all(code < 400 for code in statuses[:-1]), statuses
        assert "Server returned an error response" in _messages(refused.value)
        response = httpx.post(f"{url}/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              headers={"Authorization": f"Bearer {alice}",
                                       "Accept": "application/json, text/event-stream"})
        assert response.status_code == 429 and response.headers["retry-after"]
        assert response.json()["error"]["type"] == "RateLimited" and "alice" in response.json()["error"]["message"]
        assert response.headers["x-request-id"]
        other = mcp(url, era, lambda c: c.call_tool("safety_spam_message_type", SPAM), bob)
        in_flight = dict(app.state.limiter.in_flight)
    assert not other.is_error
    assert in_flight == {}


def _messages(error: BaseException) -> str:
    """The text of an error and, for an exception group (from the client's task groups), of every error in it."""
    inner = getattr(error, "exceptions", ())
    return " | ".join([repr(error), *(_messages(item) for item in inner)])


def test_the_rate_limit_slot_is_free_once_the_response_starts(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    """With one request in flight allowed, sequential requests never find the slot still held."""
    private, public = keys
    with serving(fake(), hub_public_key=public, rate_limit=(1000.0, 1)) as (url, _):
        results = mcp(url, "legacy", lambda c: _calls(c, 5), token(private))
    assert not any(result.is_error for result in results)


async def _calls(client: Client, count: int) -> list[Any]:
    return [await client.call_tool("safety_spam_message_type", SPAM) for _ in range(count)]


def test_mcp_requests_are_written_to_the_access_log(keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    private, public = keys
    log = tmp_path / "access.jsonl"
    with serving(fake(), hub_public_key=public, access_log=log) as (url, _):
        body = {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}
        accept = {"Accept": "application/json, text/event-stream"}
        assert httpx.post(f"{url}/mcp", json=body, headers=accept).status_code == 401
        signed_in = {**accept, "Authorization": f"Bearer {token(private, sub='alice')}"}
        assert httpx.post(f"{url}/mcp", json=body, headers=signed_in).status_code == 200
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["event"], e["user"], e["path"], e["status"], e["detail"]) for e in entries] == [
        ("api", None, "/mcp", 401, "POST"), ("api", "hub:alice", "/mcp", 200, "POST hub")]


def test_a_catalogue_with_clashing_tool_names_stops_the_server_starting(tmp_path: Path) -> None:
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    manifest = yaml.safe_load((catalogue / "jeff.safety.spam" / "manifest.yaml").read_text())
    manifest["id"], manifest["aliases"] = "jeff.jeff.list", []
    manifest["functions"] = {"functions": manifest["functions"]["message_type"]}
    (catalogue / "jeff.jeff.list").mkdir()
    (catalogue / "jeff.jeff.list" / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    app = create_app(ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                                    catalogue=catalogue), engine=fake())
    with pytest.raises(ContractError, match="jeff.jeff.list.functions.*jeff_list_functions"):
        with TestClient(app):
            pass


def test_a_served_set_change_shows_at_the_next_listing(era: str) -> None:
    engine = fake()
    with serving(engine) as (url, _):
        async def work(client: Client) -> tuple[set[str], set[str], Any]:
            before = {tool.name for tool in (await client.list_tools()).tools}
            engine.remove_adapter("jeff.safety.spam")
            after = {tool.name for tool in (await client.list_tools()).tools}
            return before, after, await client.call_tool("safety_spam_message_type", SPAM)

        before, after, called = mcp(url, era, work)
    assert before - after == {"safety_spam_message_type"}
    assert called.is_error and text_of(called).startswith("NotInstalled: ")


def test_jeff_list_functions_over_mcp_equals_v1_functions(era: str) -> None:
    with serving(fake()) as (url, _):
        listed = mcp(url, era, lambda c: c.call_tool("jeff_list_functions", {}))
        functions = httpx.get(f"{url}/v1/functions").json()
    assert not listed.is_error and listed.structured_content == functions


@pytest.mark.parametrize(("tool", "arguments"), [
    ("support_triage_urgency", {"input": "x"}),
    ("support_triage_urgency", {"input": "x", "context": {**CONTEXT, "colour": "red"}}),
    ("support_triage_urgency", {"input": "x", "context": CONTEXT, "labels": {"a": "A"}}),
    ("support_triage_route", {"input": "x", "context": CONTEXT}),
    ("support_triage_route", {"input": "x", "context": CONTEXT, "labels": {"other": "Mine"}}),
    ("support_triage_route", {"input": "x", "context": CONTEXT, "labels": {f"t{index}": "T" for index in range(41)}}),
    ("support_triage_route", {"input": "x", "context": CONTEXT, "labels": []}),
])
def test_a_tool_call_is_refused_with_the_error_type_v1_decide_gives_for_the_same_call(
        tool: str, arguments: dict[str, Any]) -> None:
    function = "jeff." + tool.replace("_", ".", 2)
    with serving(fake()) as (url, _):
        result = mcp(url, "legacy", lambda c: c.call_tool(tool, arguments))
        response = httpx.post(f"{url}/v1/decide", json={"calls": [{"function": function, **arguments}]})
    assert response.status_code >= 400
    assert result.is_error
    assert result.structured_content["error"]["type"] == response.json()["error"]["type"]
