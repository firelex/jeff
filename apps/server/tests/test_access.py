"""Private adapters (jeff_server.access): an adapter whose manifest's access is "owner" answers only the owner, one
whose access lists accounts answers the owner and those accounts (an account key, or the hub playground acting for
the account), and a caller not admitted is told nothing: every door answers it exactly as for a name the server does
not have, and every listing leaves the adapter out.

The catalogue here is the routers fixture with access added, over real engine processes (jeff_runtime.testing.
Running) reached through SocketEngine and the EngineRouter, in accounts mode:
- jeff.private.secret (alias secret, a copy of spam): the owner only;
- jeff.core.router and jeff.support.router: the owner and acct1 (the core router routes to the support router, to
  spam and to triage, each at least as open);
- jeff.safety.spam and jeff.support.triage: everyone.

Each endpoint is checked with the owner, acct1 (allowed), acct2 (another account), the playground on its own and the
playground acting for acct1 and for acct2."""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from jeff.catalogue import Catalogue
from jeff.errors import ManifestError
from jeff.manifest import manifest_from_dict
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine
from jeff_runtime.testing import Running

from jeff_server import access
from jeff_server.app import create_app
from jeff_server.auth import ALL_ADAPTERS, API_KEY_CALLER, Caller
from jeff_server.router import EngineRouter
from jeff_server.settings import AccountsSettings, ServerSettings

from accounts_testing import (HUB_URL, KEY, OTHER_ACCOUNTS_KEY, OWNER_KEY, PLAYGROUND_KEY, SECRET, Hub, answers,
                              bearer)
from channel_testing import assert_error, start

ROOT = Path(__file__).resolve().parents[3]
ROUTERS = ROOT / "packages" / "python" / "tests" / "fixtures" / "routers"
SECRET_ID = "jeff.private.secret"
ROUTER_IDS = {"jeff.core.router", "jeff.support.router"}
PUBLIC_IDS = {"jeff.safety.spam", "jeff.support.triage"}
ALL_IDS = {SECRET_ID} | ROUTER_IDS | PUBLIC_IDS
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
SECRET_CALL = {"function": "secret.message_type", "input": "WIN NOW", "context": {"channel": "email"}}
SPAM_CALL = {"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "email"}}
ROUTER_CALL = {"function": "jeff.support.router.function", "input": "My wardrobe is broken",
               "labels": ["jeff.support.triage.urgency", "jeff.support.triage.route"]}
DISPATCH = {"dispatch": {"input": "My wardrobe is broken", "interface": "rate", "context": CONTEXT}}

# Every caller, by name: its headers, and the adapters it may use.
CALLERS: dict[str, tuple[dict[str, str], set[str]]] = {
    "owner": (bearer(OWNER_KEY), ALL_IDS),
    "acct1": (bearer(KEY), ROUTER_IDS | PUBLIC_IDS),
    "acct2": (bearer(OTHER_ACCOUNTS_KEY), PUBLIC_IDS),
    "playground": (bearer(PLAYGROUND_KEY), PUBLIC_IDS),
    "playground for acct1": (bearer(PLAYGROUND_KEY, **{"X-Jeff-Account": "acct1"}), ROUTER_IDS | PUBLIC_IDS),
    "playground for acct2": (bearer(PLAYGROUND_KEY, **{"X-Jeff-Account": "acct2"}), PUBLIC_IDS),
}
NOT_FOR_SECRET = [name for name, (_, allowed) in CALLERS.items() if SECRET_ID not in allowed]
NOT_FOR_ROUTERS = [name for name, (_, allowed) in CALLERS.items() if not ROUTER_IDS <= allowed]
FOR_ROUTERS = [name for name, (_, allowed) in CALLERS.items() if ROUTER_IDS <= allowed]


def headers(name: str) -> dict[str, str]:
    return CALLERS[name][0]


def private_catalogue(folder: Path) -> Path:
    catalogue = folder / "catalogue"
    shutil.copytree(ROUTERS, catalogue)
    secret = yaml.safe_load((ROUTERS / "jeff.safety.spam" / "manifest.yaml").read_text())
    secret.update(id=SECRET_ID, aliases=["secret"], access="owner",
                  hub={"summary": "An owner-only copy of the spam adapter."})
    (catalogue / SECRET_ID).mkdir()
    (catalogue / SECRET_ID / "manifest.yaml").write_text(yaml.safe_dump(secret, sort_keys=False))
    (catalogue / SECRET_ID / "playground-examples.json").write_text(json.dumps({"adapter": SECRET_ID, "examples": [
        {"name": "a secret example", "call": {**SECRET_CALL, "function": f"{SECRET_ID}.message_type"},
         "expected": "spam"}]}))
    for router in ROUTER_IDS:
        path = catalogue / router / "manifest.yaml"
        manifest = yaml.safe_load(path.read_text())
        manifest["access"] = {"accounts": ["acct1"]}
        path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return catalogue


def graph(graph_id: str, function: str) -> dict[str, Any]:
    return {"graph": graph_id, "version": "1.0.0", "input": "message", "params": ["channel"],
            "nodes": {"check": {"call": function, "context": {"channel": {"param": "channel"}}}},
            "outputs": ["check"]}


@pytest.fixture(scope="module")
def http(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    folder = tmp_path_factory.mktemp("access")
    catalogue = private_catalogue(folder)
    graphs = folder / "graphs"
    graphs.mkdir()
    (graphs / "public.yaml").write_text(yaml.safe_dump(graph("jeff.graphs.public", "spam.message_type")))
    (graphs / "secret.yaml").write_text(yaml.safe_dump(graph("jeff.graphs.secret", "secret.message_type")))
    server = Running(FakeEngine(answers, set(ALL_IDS)), fixed={adapter: Path("/weights") / adapter
                                                                for adapter in ALL_IDS})
    engine = SocketEngine(server.path, timeout_s=5)
    token_file = folder / "accounts-token"
    token_file.write_text(SECRET + "\n")
    token_file.chmod(0o600)
    accounts = AccountsSettings(url=HUB_URL, token_file=token_file, token=SECRET, max_stale_s=300.0,
                                usage_file=folder / "usage.jsonl", bypass_file=None, bypass=None)
    settings = ServerSettings(engine_sockets=(server.path,), model_name="jeff-v1.3", catalogue=catalogue,
                              api_key=OWNER_KEY, rate_limit=(1000.0, 8), accounts=accounts, graphs=graphs)
    hub = Hub()
    app = create_app(settings, engine=EngineRouter([engine]),
                     accounts_http=httpx.Client(transport=httpx.MockTransport(hub.handler)))
    try:
        with TestClient(app) as client:
            app.state.accounts.refresh()
            yield client
    finally:
        engine.close()
        server.close()


def error_of(response: httpx.Response) -> tuple[int, str, str]:
    body = response.json()["error"]
    return response.status_code, body["type"], body["message"]


def same_as_unknown(response: httpx.Response, unknown: httpx.Response, hidden: str, made_up: str) -> None:
    """The refusal of a hidden name is the refusal of a name the server does not have, word for word (the names
    swapped)."""
    status, kind, message = error_of(response)
    assert (status, kind, message.replace(hidden, made_up)) == error_of(unknown), (response.text, unknown.text)


# ---- listings ------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", CALLERS)
def test_functions_lists_only_the_adapters_the_caller_may_use(http: TestClient, name: str) -> None:
    response = http.get("/v1/functions", headers=headers(name))
    assert response.status_code == 200, response.text
    listed = {function["id"].rsplit(".", 1)[0] for function in response.json()["functions"]}
    assert listed == CALLERS[name][1]


@pytest.mark.parametrize("name", CALLERS)
def test_adapters_lists_only_the_adapters_the_caller_may_use(http: TestClient, name: str) -> None:
    response = http.get("/v1/adapters", headers=headers(name))
    assert response.status_code == 200, response.text
    assert {adapter["id"] for adapter in response.json()["adapters"]} == CALLERS[name][1]


@pytest.mark.parametrize("name", CALLERS)
def test_models_lists_only_the_adapters_the_caller_may_use(http: TestClient, name: str) -> None:
    response = http.get("/v1/models", headers=headers(name))
    assert response.status_code == 200, response.text
    names = {model["name"] for model in response.json()["models"]}
    assert ("secret" in names) == (name == "owner")
    assert "spam" in names


@pytest.mark.parametrize("name", CALLERS)
def test_graphs_leaves_out_a_graph_calling_an_adapter_the_caller_may_not_use(http: TestClient, name: str) -> None:
    response = http.get("/v1/graphs", headers=headers(name))
    assert response.status_code == 200, response.text
    listed = {item["graph"] for item in response.json()["graphs"]}
    assert listed == ({"jeff.graphs.public", "jeff.graphs.secret"} if name == "owner" else {"jeff.graphs.public"})


@pytest.mark.parametrize("name", CALLERS)
def test_health_lists_only_the_adapters_the_caller_may_use(http: TestClient, name: str) -> None:
    response = http.get("/health", headers=headers(name))
    assert response.status_code == 200, response.text
    assert [set(engine["adapters"]) for engine in response.json()["engines"]] == [CALLERS[name][1]]


def test_health_without_credentials_lists_no_private_adapter(http: TestClient) -> None:
    response = http.get("/health")
    assert response.status_code == 200, response.text
    assert [set(engine["adapters"]) for engine in response.json()["engines"]] == [PUBLIC_IDS]


def test_health_with_credentials_that_are_not_valid_is_401(http: TestClient) -> None:
    assert http.get("/health", headers=bearer("jeff_live_" + "z" * 52)).status_code == 401


@pytest.mark.parametrize("name", CALLERS)
def test_the_consoles_examples_leave_out_an_adapter_the_caller_may_not_use(http: TestClient, name: str) -> None:
    response = http.get("/ui/examples", headers=headers(name))
    assert response.status_code == 200, response.text
    functions = {example["call"]["function"] for example in response.json()["examples"]}
    assert (f"{SECRET_ID}.message_type" in functions) == (name == "owner")


# ---- /v1/decide ----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", CALLERS)
def test_a_call_answers_only_a_caller_the_adapter_admits(http: TestClient, name: str) -> None:
    for call, adapter in ((SECRET_CALL, SECRET_ID), (ROUTER_CALL, "jeff.support.router"), (SPAM_CALL, None)):
        response = http.post("/v1/decide", json={"calls": [call]}, headers=headers(name))
        if adapter is None or adapter in CALLERS[name][1]:
            assert response.status_code == 200, response.text
            continue
        made_up = {**call, "function": call["function"].replace("secret", "unknownx").replace("support.router",
                                                                                              "support.unknownx")}
        unknown = http.post("/v1/decide", json={"calls": [made_up]}, headers=headers(name))
        hidden_name = "secret" if adapter == SECRET_ID else "support.router"
        same_as_unknown(response, unknown, hidden_name, made_up["function"].split(".")[0]
                        if adapter == SECRET_ID else "support.unknownx")
        assert error_of(response)[:2] == (404, "UnknownFunction")


@pytest.mark.parametrize("name", NOT_FOR_SECRET)
def test_a_call_by_the_full_id_or_with_a_version_is_refused_as_unknown(http: TestClient, name: str) -> None:
    for function in (f"{SECRET_ID}.message_type", f"{SECRET_ID}.message_type@1.3"):
        response = http.post("/v1/decide", json={"calls": [{**SECRET_CALL, "function": function}]},
                             headers=headers(name))
        unknown = http.post("/v1/decide", json={"calls": [{**SECRET_CALL, "function": function.replace(
            "private.secret", "private.unknownx")}]}, headers=headers(name))
        same_as_unknown(response, unknown, "private.secret", "private.unknownx")


@pytest.mark.parametrize("name", CALLERS)
def test_a_dispatch_through_a_private_router_answers_only_a_caller_it_admits(http: TestClient, name: str) -> None:
    response = http.post("/v1/decide", json=DISPATCH, headers=headers(name))
    if name in FOR_ROUTERS:
        assert response.status_code == 200, response.text
        return
    unknown = http.post("/v1/decide", json={"dispatch": {**DISPATCH["dispatch"], "router": "jeff.core.unknownx"}},
                        headers=headers(name))
    same_as_unknown(response, unknown, "jeff.core.router", "jeff.core.unknownx")


@pytest.mark.parametrize("name", NOT_FOR_SECRET)
def test_an_escalation_step_naming_a_private_adapter_is_refused_as_unknown(http: TestClient, name: str) -> None:
    def body(step: str) -> dict[str, Any]:
        return {"calls": [SPAM_CALL], "on_low_confidence": {"escalate": [step]}}
    response = http.post("/v1/decide", json=body("secret.message_type"), headers=headers(name))
    unknown = http.post("/v1/decide", json=body("unknownx.message_type"), headers=headers(name))
    same_as_unknown(response, unknown, "secret", "unknownx")
    owner = http.post("/v1/decide", json=body("secret.message_type"), headers=headers("owner"))
    assert owner.status_code == 200, owner.text


@pytest.mark.parametrize("name", CALLERS)
def test_an_inline_graph_reaching_a_private_adapter_answers_only_a_caller_it_admits(http: TestClient,
                                                                                    name: str) -> None:
    def body(function: str) -> dict[str, Any]:
        return {"graph": {"inline": graph("jeff.graphs.inline", function)}, "input": "WIN NOW",
                "params": {"channel": "email"}}
    response = http.post("/v1/decide", json=body("secret.message_type"), headers=headers(name))
    if name == "owner":
        assert response.status_code == 200, response.text
        return
    unknown = http.post("/v1/decide", json=body("unknownx.message_type"), headers=headers(name))
    same_as_unknown(response, unknown, "secret", "unknownx")


@pytest.mark.parametrize("name", CALLERS)
def test_a_registered_graph_reaching_a_private_adapter_is_not_registered_for_a_caller_it_does_not_admit(
        http: TestClient, name: str) -> None:
    def body(graph_id: str) -> dict[str, Any]:
        return {"graph": {"id": graph_id}, "input": "WIN NOW", "params": {"channel": "email"}}
    response = http.post("/v1/decide", json=body("jeff.graphs.secret"), headers=headers(name))
    public = http.post("/v1/decide", json=body("jeff.graphs.public"), headers=headers(name))
    assert public.status_code == 200, public.text
    if name == "owner":
        assert response.status_code == 200, response.text
        return
    unknown = http.post("/v1/decide", json=body("jeff.graphs.unknownx"), headers=headers(name))
    same_as_unknown(response, unknown, "jeff.graphs.secret", "jeff.graphs.unknownx")
    assert "jeff.graphs.secret" not in unknown.text  # the registered graphs it names leave it out


# ---- /v1/systemone -------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("name", CALLERS)
def test_systemone_answers_a_private_model_only_for_a_caller_it_admits(http: TestClient, name: str) -> None:
    def body(model: str) -> dict[str, Any]:
        return {"model": model, "state": {"message": "WIN NOW"},
                "questions": {"q": {"type": "noul", "instructions": "Is this spam?"}}}
    response = http.post("/v1/systemone", json=body("secret"), headers=headers(name))
    if name == "owner":
        assert response.status_code == 200, response.text
        return
    unknown = http.post("/v1/systemone", json=body("unknownx"), headers=headers(name))
    same_as_unknown(response, unknown, "secret", "unknownx")
    assert response.json()["detail"] == json.loads(unknown.text.replace("unknownx", "secret"))["detail"]
    assert SECRET_ID not in response.text


# ---- MCP -----------------------------------------------------------------------------------------------------------

MCP_HEADERS = {"Accept": "application/json, text/event-stream", "MCP-Protocol-Version": "2025-06-18"}


def mcp(http: TestClient, name: str, method: str, params: dict[str, Any]) -> dict[str, Any]:
    response = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                         headers={**MCP_HEADERS, **headers(name)})
    assert response.status_code == 200, response.text
    return dict(response.json())


@pytest.mark.parametrize("name", CALLERS)
def test_mcp_lists_only_the_tools_of_adapters_the_caller_may_use(http: TestClient, name: str) -> None:
    tools = {tool["title"] for tool in mcp(http, name, "tools/list", {})["result"]["tools"] if "title" in tool}
    assert {title.rsplit(".", 1)[0] for title in tools} == CALLERS[name][1]


@pytest.mark.parametrize("name", CALLERS)
def test_an_mcp_tool_call_answers_a_private_tool_only_for_a_caller_it_admits(http: TestClient, name: str) -> None:
    owner_tools = {tool["title"]: tool["name"] for tool in mcp(http, "owner", "tools/list", {})["result"]["tools"]
                   if "title" in tool}
    tool = owner_tools["jeff.private.secret.message_type"]
    arguments = {"input": "WIN NOW", "context": {"channel": "email"}}
    result = mcp(http, name, "tools/call", {"name": tool, "arguments": arguments})["result"]
    if name == "owner":
        assert result["isError"] is False, result
        return
    unknown = mcp(http, name, "tools/call", {"name": tool.replace("secret", "unknownx"),
                                             "arguments": arguments})["result"]
    assert result["isError"] is True
    error, made_up = result["structuredContent"]["error"], unknown["structuredContent"]["error"]
    assert (error["type"], error["message"].replace("secret", "unknownx")) == (made_up["type"], made_up["message"])
    assert error["type"] == "UnknownFunction"


# ---- the channel ---------------------------------------------------------------------------------------------------

CHANNEL_CALLERS = ["owner", "acct1", "acct2", "playground"]  # the channel takes no X-Jeff-Account


def bearer_of(name: str) -> str:
    return headers(name)["Authorization"].removeprefix("Bearer ")


@pytest.mark.parametrize("name", CHANNEL_CALLERS)
def test_the_channel_binds_and_decides_a_private_adapter_only_for_a_caller_it_admits(http: TestClient,
                                                                                     name: str) -> None:
    with http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, bearer_of(name))
        bind = {"op": "bind", "id": 2, "function": "secret.message_type", "context": {"channel": "email"}}
        decide = {"op": "decide", "id": 3, "calls": [SECRET_CALL]}
        dispatch = {"op": "dispatch", "id": 4, **DISPATCH}
        if name == "owner":
            channel.result(bind)
            channel.result(decide)
            channel.result(dispatch)
            return
        for message, made_up in ((bind, {**bind, "function": "unknownx.message_type"}),
                                 (decide, {**decide, "calls": [{**SECRET_CALL, "function": "unknownx.message_type"}]})):
            refused = assert_error(channel.request(message), "UnknownFunction")
            unknown = assert_error(channel.request(made_up), "UnknownFunction")
            assert refused["message"].replace("secret", "unknownx") == unknown["message"]
        if name in FOR_ROUTERS:
            channel.result(dispatch)
            return
        refused = assert_error(channel.request(dispatch), "UnknownFunction")
        unknown = assert_error(channel.request({**dispatch, "dispatch": {**DISPATCH["dispatch"],
                                                                         "router": "jeff.core.unknownx"}}),
                               "UnknownFunction")
        assert refused["message"].replace("jeff.core.router", "jeff.core.unknownx") == unknown["message"]


# ---- the catalogue checks ------------------------------------------------------------------------------------------

def manifest(adapter_id: str, access_value: object = None, routes_to: list[str] | None = None) -> Any:
    raw = yaml.safe_load((ROUTERS / "jeff.core.router" / "manifest.yaml").read_text()) if routes_to is not None \
        else yaml.safe_load((ROUTERS / "jeff.safety.spam" / "manifest.yaml").read_text())
    raw["id"], raw["aliases"] = adapter_id, []
    if routes_to is not None:
        raw["routes_to"] = routes_to
        raw["functions"]["domain"]["labels"]["fixed"] = {target: "A target" for target in routes_to}
    if access_value is not None:
        raw["access"] = access_value
    return manifest_from_dict(raw, Path(f"{adapter_id}/manifest.yaml"))


def catalogue_of(*manifests: Any) -> Catalogue:
    return Catalogue(manifests, yaml.safe_load((ROUTERS / "bases.yaml").read_text()))


@pytest.mark.parametrize(("router", "target"), [
    (None, "owner"), (None, {"accounts": ["acct1"]}), ({"accounts": ["acct1"]}, "owner"),
    ({"accounts": ["acct1", "acct2"]}, {"accounts": ["acct1"]})])
def test_a_router_routing_to_an_adapter_of_narrower_access_is_refused(router: object, target: object) -> None:
    catalogue = catalogue_of(manifest("jeff.test.router", router, ["jeff.test.leaf"]),
                             manifest("jeff.test.leaf", target))
    with pytest.raises(ManifestError, match=r"router jeff\.test\.router routes to jeff\.test\.leaf, whose access"):
        access.check_access(catalogue)


@pytest.mark.parametrize(("router", "target"), [
    (None, None), ("owner", None), ("owner", "owner"), ("owner", {"accounts": ["acct1"]}),
    ({"accounts": ["acct1"]}, None), ({"accounts": ["acct1"]}, {"accounts": ["acct1", "acct2"]})])
def test_a_router_routing_to_an_adapter_at_least_as_open_is_accepted(router: object, target: object) -> None:
    access.check_access(catalogue_of(manifest("jeff.test.router", router, ["jeff.test.leaf"]),
                                     manifest("jeff.test.leaf", target)))


def test_an_account_id_ending_in_a_newline_is_refused() -> None:
    """The schema's pattern is matched with re.search, whose $ also matches before a final newline."""
    with pytest.raises(ManifestError, match=r"access\.accounts\[1\] is not an account id"):
        access.access_of(manifest("jeff.test.leaf", {"accounts": ["acct1", "acct2\n"]}))


def test_who_an_access_admits() -> None:
    owner_only, for_acct1 = access.Access(frozenset()), access.Access(frozenset({"acct1"}))
    acct1, acct2 = Caller("account", "acct1", ALL_ADAPTERS), Caller("account", "acct2", ALL_ADAPTERS)
    playground, hub = Caller("playground", "playground", ALL_ADAPTERS), Caller("hub", "acct1", frozenset({"x"}))
    assert [owner_only.admits(caller) for caller in (API_KEY_CALLER, acct1, playground, hub)] == [
        True, False, False, False]
    assert [for_acct1.admits(caller) for caller in (API_KEY_CALLER, acct1, acct2, playground, hub)] == [
        True, True, False, False, False]


def test_a_view_is_built_once_per_catalogue_and_hidden_set() -> None:
    catalogue = catalogue_of(manifest("jeff.test.leaf", "owner"), manifest("jeff.test.open"))
    acct1, acct2 = Caller("account", "acct1", ALL_ADAPTERS), Caller("account", "acct2", ALL_ADAPTERS)
    view = access.visible_catalogue(acct1, catalogue)
    assert view is access.visible_catalogue(acct2, catalogue)
    assert {item.id for item in view.manifests()} == {"jeff.test.open"}
    assert access.visible_catalogue(API_KEY_CALLER, catalogue) is catalogue
