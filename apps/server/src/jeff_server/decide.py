"""POST /v1/decide, GET /v1/functions and GET /v1/graphs.

/v1/decide runs the in-process SDK's planner (one jeff.Jeff over the server's engine and catalogue, rebuilt when the
engines' adapters change: jeff_server.catalogue_refresh): binding, routing, graphs and escalation are the SDK's, so a
request answers exactly what the same calls answer in-process. Every route here needs the caller's credentials and
admission by the rate limiter (jeff_server.auth.V1_DEPENDENCIES). A request to /v1/decide is checked in this order: the
body against contract/schemas/decide_request (422 InvalidRequest naming the first problem), then each policy with the
caller's licences for its escalation steps and, for calls, each call's function (403 NotLicensed before anything is
decided: for a dispatch before routing, for a graph before its first stage), then the planner; for an account
caller, its allowance is checked just before the engine is first asked (402 UsageLimitReached, so after every 403
and NotInstalled), and every engine reply's input tokens are counted on its account.
The planner runs on a per-request view of the shared Jeff carrying the caller's licence check as its authorise hook,
so the leaf a dispatch reaches, an escalation step and a graph node are each also checked just before they are
decided.

Every request is planned on the caller's view of the catalogue (jeff_server.access): an adapter whose manifest's access
does not admit the caller is not in it, so naming one (as a function, a dispatch's router, an escalation step or a
graph node) is answered exactly as an unknown name, and GET /v1/functions and GET /v1/graphs leave it out (a
registered graph with a node calling one is not registered for that caller).

The channel (jeff_server.channel) answers its decide, dispatch and graph frames, and the calls on its handles, with
the same checks and planner functions as this route (check_body, check_dispatch, caller_view, answer), so every door
gives the same answer and the same errors."""

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from fastapi import APIRouter, Request
from starlette.concurrency import run_in_threadpool

from jeff import contract
from jeff.client import Call, Jeff
from jeff.errors import GraphError, InvalidRequest
from jeff.escalation import Policy, apply_policy, check_policy, escalate, function_step
from jeff.graph import Graph, graph_from_dict
from jeff.protocol import MAX_BODY
from jeff.listing import function_listing
from jeff.routing import DEFAULT_ROUTER, check_router_reaches

from jeff_server.access import visible_graphs, visible_jeff
from jeff_server.auth import V1_DEPENDENCIES, Caller, licence_check, request_caller

if TYPE_CHECKING:
    from jeff_server.catalogue_refresh import CatalogueRefresher

router = APIRouter(dependencies=V1_DEPENDENCIES)


@dataclass(frozen=True)
class RegisteredGraph:
    graph: Graph
    document: dict[str, Any]


def load_graphs(folder: Path | None) -> dict[str, RegisteredGraph]:
    """Every *.yaml graph in `folder`, by graph id. No folder means no registered graphs (the documented default of
    JEFF_GRAPHS). A missing folder, a file that is not a YAML mapping, an invalid graph, a graph with code nodes (the server cannot run the caller's
    code) or two graphs with one id raise GraphError."""
    if folder is None:
        return {}
    if not folder.is_dir():
        raise GraphError(f"JEFF_GRAPHS={folder} is not a folder; point it at a folder of graph .yaml files")
    graphs: dict[str, RegisteredGraph] = {}
    for path in sorted(folder.glob("*.yaml")):
        try:
            document = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as error:
            raise GraphError(f"{path} is not valid YAML: {error}") from error
        if not isinstance(document, dict):
            raise GraphError(f"{path} is not a mapping (a graph document); it holds {type(document).__name__}")
        graph = graph_from_dict(document, str(path))
        _refuse_code_nodes(graph, str(path))
        if graph.id in graphs:
            raise GraphError(f"{path}: graph {graph.id} is already registered by another file in {folder}")
        graphs[graph.id] = RegisteredGraph(graph, document)
    return graphs


def _refuse_code_nodes(graph: Graph, where: str) -> None:
    coded = [name for name, node in graph.nodes.items() if node.code is not None]
    if coded:
        raise GraphError(f"{where}: graph {graph.id} has code node(s) {', '.join(coded)}; the server cannot run "
                         "your code. Run a graph with code nodes through the SDK's Jeff.run_graph, where the code "
                         "runs in your process")


def policy_from_wire(raw: str | Mapping[str, Any]) -> Policy:
    """A schema-valid on_low_confidence value as the SDK's policy: escalation over HTTP has function steps only."""
    if isinstance(raw, str):
        return raw  # type: ignore[return-value]  # the schema allows only "return" and "error"
    return escalate(*(function_step(ref) for ref in raw["escalate"]), budget_ms=raw.get("budget_ms"))


SDK_ONLY_STEPS = ("llm", "human", "remote")


# The largest request body /v1/decide, /v1/systemone and PUT /v1/adapters/served read: 50 MiB (jeff.protocol.MAX_BODY,
# also the largest channel frame). The largest legitimate
# body is a /v1/systemone request with four images (the schema's maxItems) at the schema's longest data URL,
# 12,000,000 characters each (an 8 MB image, MAX_IMAGE_BYTES, is about 10.7 million base64 characters): 48,000,000
# bytes, leaving about 4.4 MB for the state and questions, which the engine's token limit keeps far smaller. A
# /v1/decide request of 256 calls each at that token limit also fits. Larger is refused before it is read whole.
MAX_BODY_BYTES = MAX_BODY


async def read_body(request: Request) -> object:
    """The request body as JSON (parse_body), read in chunks and refused with InvalidRequest as soon as it is longer
    than MAX_BODY_BYTES: a Content-Length over the cap is refused before reading anything, and a body sent without
    one (chunked) is refused at the first chunk that passes the cap, so an oversized body is never held whole."""
    limit = MAX_BODY_BYTES
    too_large = f"The request body is larger than {limit} bytes, the most this server reads"
    declared = request.headers.get("content-length")
    if declared is not None:
        if not (declared.isascii() and declared.isdigit()):
            raise InvalidRequest(f"The Content-Length header {declared!r} is not a number of bytes")
        if int(declared) > limit:
            raise InvalidRequest(f"{too_large} (Content-Length: {declared})")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise InvalidRequest(too_large)
        chunks.append(chunk)
    return parse_body(b"".join(chunks))


def parse_body(raw: bytes) -> object:
    """A request body as JSON; InvalidRequest when it is not UTF-8, not JSON, or nested too deeply to read."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise InvalidRequest(f"The request body is not valid UTF-8: {error}") from error
    try:
        return json.loads(text)
    except json.JSONDecodeError as error:
        raise InvalidRequest(f"The request body is not valid JSON: {error}") from error
    except RecursionError as error:
        raise InvalidRequest("The request body is JSON nested too deeply to read") from error


def _policies(body: Mapping[str, Any]) -> list[tuple[str, object]]:
    """Every on_low_confidence value in a body of any shape, with where it is."""
    found: list[tuple[str, object]] = []
    if "on_low_confidence" in body:
        found.append(("/on_low_confidence", body["on_low_confidence"]))
    by_node = body.get("on_low_confidence_by_node")
    if isinstance(by_node, Mapping):
        found.extend((f"/on_low_confidence_by_node/{name}", policy) for name, policy in by_node.items())
    return found


def refuse_sdk_only_steps(body: object) -> None:
    """An escalation step that is not a function reference (an LLM, a person, another server) can only run in the
    SDK, which holds the caller's callable; say so rather than reporting a schema type error."""
    if not isinstance(body, Mapping):
        return
    for where, policy in _policies(body):
        steps = policy.get("escalate") if isinstance(policy, Mapping) else None
        if not isinstance(steps, list):
            continue
        for index, step in enumerate(steps):
            if not isinstance(step, str) or step in SDK_ONLY_STEPS:
                raise InvalidRequest(
                    "Escalation on the server takes function references only (such as "
                    f"\"jeff.support.triage.urgency\"); got {json.dumps(step)[:100]} (at {where}/escalate/{index}). "
                    "llm, human and remote steps run in the SDK, which calls them after the server returns the "
                    "abstained decision")


def check_body(body: object) -> dict[str, Any]:
    refuse_sdk_only_steps(body)
    try:
        valid = contract.is_valid("decide_request", body)
        problem = None if valid else contract.first_problem("decide_request", body)
    except RecursionError as error:
        raise InvalidRequest("The request body is JSON nested too deeply to check") from error
    if problem is not None:
        raise InvalidRequest(f"The request body does not match POST /v1/decide: {problem}")
    assert isinstance(body, dict)  # the schema's three shapes are all objects
    check_interface(body)
    return body


def check_interface(body: dict[str, Any]) -> None:
    """A dispatch must name one of the contract's interfaces (the schema only says it is a string)."""
    if "dispatch" in body:
        interfaces = contract.interfaces()
        interface = body["dispatch"]["interface"]
        if interface not in interfaces:
            raise InvalidRequest(f"dispatch names interface {interface!r}, which is not one of "
                                 f"{', '.join(interfaces)} (at /dispatch/interface)")


def check_dispatch(jeff: Jeff, body: dict[str, Any]) -> None:
    """The router a dispatch starts from must lead to a function implementing the requested interface; otherwise
    the SDK would only find out partway down the tree (ManifestError)."""
    if "dispatch" not in body:
        return
    raw = body["dispatch"]
    check_router_reaches(jeff.catalogue, raw.get("router", DEFAULT_ROUTER), raw["interface"],
                         direct="call the function directly with 'calls'")


def _call(raw: Mapping[str, Any]) -> Call:
    return Call(raw["function"], raw["input"], raw.get("context"), raw.get("labels"))


def _decide_calls(jeff: Jeff, body: dict[str, Any]) -> dict[str, Any]:
    policy = policy_from_wire(body.get("on_low_confidence", "return"))
    calls = [_call(item) for item in body["calls"]]
    # The request's one policy is checked against each interface its calls implement, once per interface, before
    # anything is decided, as the SDK's bound functions do.
    interfaces = dict.fromkeys(jeff.catalogue.function(call.function).interface for call in calls)
    for interface in interfaces:
        check_policy(jeff, interface, policy)
    # The fast path: every call's function is checked against the caller's licences before anything is decided, so a
    # refused batch never reaches the engine. (check_policy above has already checked the escalation steps; the hook
    # also runs as each function is bound.)
    if jeff.authorise is not None:
        for call in calls:
            jeff.authorise(jeff.catalogue.function(call.function))
    decisions = jeff.decide(calls)
    answered = [apply_policy(jeff, call, decision, policy) for call, decision in zip(calls, decisions, strict=True)]
    return {"decisions": [decision.to_json() for decision in answered]}


def _dispatch(jeff: Jeff, body: dict[str, Any]) -> dict[str, Any]:
    raw = body["dispatch"]
    options: dict[str, Any] = {"interface": raw["interface"], "context": raw.get("context"),
                               "labels": raw.get("labels"),
                               "on_low_confidence": policy_from_wire(body.get("on_low_confidence", "return"))}
    if "router" in raw:
        options["router"] = raw["router"]  # absent: Jeff.dispatch's own default, DEFAULT_ROUTER
    return {"decision": jeff.dispatch(raw["input"], **options).to_json()}


def _run_graph(jeff: Jeff, graphs: Mapping[str, RegisteredGraph], body: dict[str, Any]) -> dict[str, Any]:
    which = body["graph"]
    if "id" in which:
        if which["id"] not in graphs:
            raise GraphError(f"No graph {which['id']!r} is registered on this server; registered: "
                             f"{', '.join(graphs) or 'none'}")
        graph = graphs[which["id"]].graph
    else:
        graph = graph_from_dict(which["inline"], "the inline graph")
        _refuse_code_nodes(graph, "the inline graph")
    policies = {name: policy_from_wire(raw) for name, raw in body.get("on_low_confidence_by_node", {}).items()}
    return {"graph_result": jeff.run_graph(graph, body["input"], body["params"], policies=policies).to_json()}


def answer(jeff: Jeff, graphs: Mapping[str, RegisteredGraph], body: dict[str, Any]) -> dict[str, Any]:
    if "calls" in body:
        return _decide_calls(jeff, body)
    if "dispatch" in body:
        return _dispatch(jeff, body)
    return _run_graph(jeff, graphs, body)


@router.post("/v1/decide")
async def decide(request: Request) -> dict[str, Any]:
    body = check_body(await read_body(request))
    planner: "CatalogueRefresher" = request.app.state.planner
    shared = await planner.ready()
    caller, graphs = request_caller(request), request.app.state.graphs
    # The planner calls the engine synchronously (and, after a name it does not know, may ask the engines whether
    # their adapters changed and plan once more: CatalogueRefresher.retry_unknown); run it off the event loop.
    return await run_in_threadpool(planner.retry_unknown, shared, lambda jeff: plan_body(jeff, caller, graphs, body))


def plan_body(shared: Jeff, caller: Caller, graphs: Mapping[str, RegisteredGraph],
              body: dict[str, Any]) -> dict[str, Any]:
    """A checked /v1/decide body planned on the caller's view of `shared`: the dispatch's router check, then the
    answer, a graph by id among the registered graphs the caller may see."""
    view = caller_view(shared, caller)
    check_dispatch(view, body)
    if "graph" in body:
        graphs = visible_graphs(caller, shared.catalogue, graphs)
    return answer(view, graphs, body)


def caller_view(shared: Jeff, caller: Caller) -> Jeff:
    """A view of the shared Jeff for one request only, with the caller's view of the catalogue (jeff_server.access:
    without the adapters it may not use), carrying this caller's licence check and, for an account or the
    playground, its usage meter (jeff_server.accounts.Meter.view: the allowance is checked at the request's first
    engine request, and every engine reply's input tokens are counted). Build one per request, never keep one: its
    engine admits every engine request after its first. Nothing on the shared Jeff changes, so concurrent requests of
    different callers never see each other's licences or counts."""
    visible = visible_jeff(shared, caller)
    view = visible.with_authorise(licence_check(caller, visible.catalogue))
    return caller.meter.view(view) if caller.meter is not None else view


@router.get("/v1/functions")
async def functions(request: Request) -> dict[str, Any]:
    """jeff.listing.function_listing on the caller's view, which the MCP tool jeff_list_functions answers too."""
    shared = await request.app.state.planner.ready()
    return await run_in_threadpool(function_listing, visible_jeff(shared, request_caller(request)))


@router.get("/v1/graphs")
async def graphs(request: Request) -> dict[str, Any]:
    shared = await request.app.state.planner.ready()
    registered = visible_graphs(request_caller(request), shared.catalogue, request.app.state.graphs)
    return {"graphs": [item.document for item in registered.values()]}
