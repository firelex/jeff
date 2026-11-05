"""Remote mode: local mode's surface (jeff.Jeff) over a Jeff HTTP server.

RemoteJeff keeps a local catalogue, so binding, labels, policies and escalation questions are worked out exactly as in
local mode, and replaces every place where Jeff would call an engine with one request to the server. Connecting reads
GET /v1/functions once and refuses a server that serves another version of an adapter the local catalogue has (labels
would otherwise be mapped by the wrong manifest).

Each operation is one POST /v1/decide: decide(calls), a bound function's call or many(), an interface shorthand,
dispatch, and a graph with no code nodes (any other graph runs here, one request per stage that has model nodes). A
policy the server can run ("return", "error", or escalation through function steps only) is sent with the request; a
policy with an llm, human or remote step is run here, on the decision the server returns, because only this process
holds those callables (each function step then costs one more request).

With channel=True the same operations go over the server's WebSocket channel instead (jeff.channel_client), and a
bound function is a handle there."""

import math
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import TracebackType
from typing import Any, Protocol, cast

import httpx

from jeff import contract
from jeff.binding import JSON, Binding, Labels
from jeff.catalogue import Catalogue
from jeff.client import BoundFunction, Call, Jeff
from jeff.decision import Decision
from jeff.engine import EngineResult
from jeff.errors import (BackendUnavailable, ContractError, DeadlineExceeded, EngineError, InterfaceMismatch,
                         InvalidPolicy, InvalidRequest, JeffError, LowConfidence, NotInstalled, Unauthorised)
from jeff.escalation import AskStep, Escalate, FunctionStep, Policy, RemoteStep, check_policy
from jeff.graph import Graph, GraphResult, _check, _run_checked, graph_to_dict, load_graph
from jeff.manifest import Calibration, FormatCalibration, FunctionSpec, resolve_calibration
from jeff.routing import DEFAULT_ROUTER, _start, leaf_call
from jeff.wire import error_from_wire

# The bearer token: a string, a callable returning the current token, or None for a server with no credentials
# configured. Over HTTP the callable is called before every request, so it can refresh. Over the channel
# (channel=True) it is called at auth, in each refresh window (from jeff.channel_client.REFRESH_BEFORE_S seconds before
# the token's exp, again every REFRESH_RETRY_S seconds while it returns a token that does not expire later) and for each
# GET /v1/functions, not before each request; it must return a later-expiring token before exp, or the channel closes
# with Unauthorised at exp.
type TokenSource = str | Callable[[], str] | None

# The wire form of a policy the server runs (contract/schemas/decide_request.schema.json, $defs/policy).
type WirePolicy = str | dict[str, Any]

# The most calls one request carries: /v1/decide's calls and a channel decide (256 each), and a channel call_many's
# inputs (contract/schemas/decide_request and socket). More are refused here, never split into several requests.
MAX_CALLS = 256

REQUEST_ID_HEADER = "x-request-id"
BODY_EXCERPT = 300  # characters of a non-Jeff error body quoted in the EngineError


class Transport(Protocol):
    """How RemoteJeff reaches the server: one round trip per method. Bodies and answers are the JSON shapes of
    contract/schemas/decide_request and decide_response."""

    def decide_calls(self, calls: list[dict[str, Any]], policy: WirePolicy) -> list[dict[str, Any]]:
        """The decisions for `calls`, in order, with `policy` applied by the server."""
        ...

    def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        """The routed decision for a {"dispatch": ..., "on_low_confidence": ...} body."""
        ...

    def graph(self, body: dict[str, Any]) -> dict[str, Any]:
        """The graph result for a {"graph": ..., "input", "params", ...} body."""
        ...

    def functions(self) -> dict[str, Any]:
        """The server's GET /v1/functions answer: every function it serves and the base format its engines serve
        (contract/schemas/functions_response)."""
        ...

    def close(self) -> None: ...


def server_policy(policy: Policy) -> WirePolicy | None:
    """The wire form of a policy the server can run: "return", "error", or {"escalate": [function refs],
    "budget_ms"?}; None when the policy has an llm, human or remote step, which only this process can run."""
    if isinstance(policy, str) and policy in ("return", "error"):
        return policy
    if not isinstance(policy, Escalate):
        raise InvalidPolicy(f"Unknown on_low_confidence policy {policy!r}; use 'return', 'error' or escalate(...)")
    refs: list[str] = []
    for step in policy.steps:
        if isinstance(step, (AskStep, RemoteStep)):
            return None
        if not isinstance(step, FunctionStep):
            raise InvalidPolicy(f"escalate() got {step!r}, which is not a step; use function_step(...), remote(...), "
                                "llm(...) or human(...)")
        refs.append(step.ref)
    wire: dict[str, Any] = {"escalate": refs}
    if policy.budget_ms is not None:
        wire["budget_ms"] = policy.budget_ms
    return wire


def read_token(source: TokenSource, url: str) -> str | None:
    """The current token: the string, or what the callable returns now (called once per use); None when there is no
    token. Unauthorised when it is not a non-empty string. Both transports (RestTransport, and the channel's
    jeff.channel_client.ChannelTransport) read their token through this."""
    if source is None:
        return None
    token = source() if callable(source) else source
    if not isinstance(token, str) or not token:
        raise Unauthorised(f"The token for {url} must be a non-empty string; "
                           f"{'the token callable returned' if callable(source) else 'got'} {token!r}")
    return token


class RestTransport:
    """The Transport over HTTP: one httpx.Client (connections kept alive) for every request. Every non-2xx answer
    is raised as the JeffError its body names (jeff.wire.error_from_wire), with the response's x-request-id as the
    error's request_id attribute (None if the response carried none)."""

    def __init__(self, url: str, token: TokenSource, timeout_s: float) -> None:
        if not url.startswith(("http://", "https://")):
            raise InvalidRequest(f"The server URL {url!r} must start with http:// or https://")
        if not (token is None or isinstance(token, str) or callable(token)):
            raise InvalidRequest(f"token must be a string, a callable returning one, or None; got {type(token).__name__}")
        self.url = url.rstrip("/")
        self.timeout_s = timeout_s
        self._token = token
        self.client = httpx.Client(base_url=self.url, timeout=timeout_s)

    def _headers(self) -> dict[str, str]:
        token = read_token(self._token, self.url)
        return {} if token is None else {"Authorization": f"Bearer {token}"}

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None,
                 schema: contract.SchemaName | None = None) -> dict[str, Any]:
        """One request; the answer's JSON object, checked against `schema` when given. Every error raised after the
        server answered carries the response's x-request-id as request_id."""
        if self.client.is_closed:
            raise InvalidRequest(f"This remote client of {self.url} is closed; connect again with Jeff.remote")
        headers = self._headers()
        try:
            response = self.client.request(method, path, json=body, headers=headers)
        except (httpx.ConnectError, httpx.ConnectTimeout) as error:
            raise BackendUnavailable(f"Cannot reach the Jeff server at {self.url}: {error}") from error
        except httpx.TimeoutException as error:
            raise DeadlineExceeded(f"The Jeff server at {self.url} did not answer {method} {path} within "
                                   f"{self.timeout_s} s; the server may still have run the request") from error
        except httpx.TransportError as error:
            raise BackendUnavailable(f"The connection to the Jeff server at {self.url} failed during {method} "
                                     f"{path}: {type(error).__name__}: {error}") from error
        except httpx.RequestError as error:
            raise EngineError(f"The answer of the Jeff server at {self.url} to {method} {path} could not be read: "
                              f"{type(error).__name__}: {error}") from error
        try:
            return self._answer(method, path, response, schema)
        except JeffError as error:
            error.request_id = response.headers.get(REQUEST_ID_HEADER)
            raise

    def _answer(self, method: str, path: str, response: httpx.Response,
                schema: contract.SchemaName | None) -> dict[str, Any]:
        if not response.is_success:
            raise self._error(method, path, response)
        try:
            answer = response.json()
        except ValueError as error:
            raise EngineError(f"The Jeff server at {self.url} answered {method} {path} with status "
                              f"{response.status_code} and a body that is not JSON: "
                              f"{response.text[:BODY_EXCERPT]}") from error
        if not isinstance(answer, dict):
            raise EngineError(f"The Jeff server at {self.url} answered {method} {path} with JSON that is not an "
                              f"object: {response.text[:BODY_EXCERPT]}")
        if schema is not None and not contract.is_valid(schema, answer):
            raise ContractError(f"The answer of {self.url} to {method} {path} does not match "
                                f"contract/schemas/{schema}: {contract.first_problem(schema, answer)}")
        return answer

    def _error(self, method: str, path: str, response: httpx.Response) -> JeffError:
        try:
            body = response.json()
        except ValueError:
            body = None
        error = body.get("error") if isinstance(body, dict) else None
        if isinstance(error, dict) and isinstance(error.get("type"), str) and isinstance(error.get("message"), str):
            details = error.get("details")
            decision = details.get("decision") if isinstance(details, dict) else None
            if decision is not None and not contract.is_valid("decision", decision):
                return ContractError(f"The {error['type']} error the Jeff server at {self.url} answered {method} "
                                     f"{path} with carries a decision that does not match "
                                     f"contract/schemas/decision: {contract.first_problem('decision', decision)}")
            return error_from_wire(error)
        return EngineError(f"The Jeff server at {self.url} answered {method} {path} with status "
                           f"{response.status_code} and a body that is not a Jeff error: "
                           f"{response.text[:BODY_EXCERPT]}")

    def _decide(self, body: dict[str, Any], key: str) -> Any:
        answer = self._request("POST", "/v1/decide", body, "decide_response")
        if key not in answer:
            raise ContractError(f"The answer of {self.url} to POST /v1/decide has {sorted(answer)}, not {key!r}")
        return answer[key]

    def decide_calls(self, calls: list[dict[str, Any]], policy: WirePolicy) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = self._decide({"calls": calls, "on_low_confidence": policy}, "decisions")
        if len(decisions) != len(calls):
            raise ContractError(f"{self.url} answered {len(decisions)} decisions for {len(calls)} calls")
        return decisions

    def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        decision: dict[str, Any] = self._decide(body, "decision")
        return decision

    def graph(self, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._decide(body, "graph_result")
        return result

    def functions(self) -> dict[str, Any]:
        answer: dict[str, Any] = self._request("GET", "/v1/functions", schema="functions_response")
        return answer

    def close(self) -> None:
        self.client.close()


class ServedFunctions:
    """The engine a RemoteJeff holds: the adapters the server serves and the base format its engines serve, read from
    GET /v1/functions when it is built and again on installed(refresh=True). Each read checks that every served
    adapter the local catalogue also has is the same version, and that the base format has not changed since the
    first (the functions bound here chose their calibration for it). It never predicts: RemoteJeff sends every
    decision to the server."""

    def __init__(self, transport: Transport, catalogue: Catalogue, url: str) -> None:
        self._transport, self._catalogue, self._url = transport, catalogue, url
        answer = self._transport.functions()
        self.base_format: dict[str, str] = dict(answer["base_format"])
        # function id -> (its bfloat16 calibration, its calibration_by_format entries), as the server lists them:
        # the thresholds the server decides with, which RemoteJeff.resolve_calibration_of reads
        self.calibrations: dict[str, tuple[Calibration | None, dict[str, FormatCalibration]]] = {}
        self._installed = self._read(answer)

    def _read(self, answer: dict[str, Any]) -> set[str]:
        if answer["base_format"] != self.base_format:
            raise ContractError(f"The server at {self._url} now serves a base of format {answer['base_format']}, not "
                                f"{self.base_format} as when this client connected; the functions bound here use "
                                "that format's calibration. Connect again")
        served: dict[str, str] = {}
        calibrations: dict[str, tuple[Calibration | None, dict[str, FormatCalibration]]] = {}
        for function in answer["functions"]:
            adapter_id = function["id"].rsplit(".", 1)[0]
            served[adapter_id] = function["adapter_version"]
            block = function["calibration"]
            calibrations[function["id"]] = (
                Calibration(**block) if block is not None else None,
                {format_id: FormatCalibration(**entry)
                 for format_id, entry in function.get("calibration_by_format", {}).items()})
        self.calibrations = calibrations
        local = {manifest.id: manifest.version for manifest in self._catalogue.manifests()}
        for adapter_id, version in served.items():
            if adapter_id in local and local[adapter_id] != version:
                raise version_mismatch(adapter_id, version, local[adapter_id])
        return set(served)

    def installed(self, refresh: bool = False) -> set[str]:
        if refresh:
            self._installed = self._read(self._transport.functions())
        return set(self._installed)

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        raise EngineError(f"RemoteJeff sends every decision to the server at {self._url}; nothing may call its "
                          f"engine's predict (asked for {len(rows)} row(s) of {adapter_id})")


def check_batch(count: int, what: str) -> None:
    """InvalidRequest when `count` calls or inputs are more than one request may carry (MAX_CALLS)."""
    if count > MAX_CALLS:
        raise InvalidRequest(f"{count} {what} in one request: a Jeff server answers at most {MAX_CALLS} per request; "
                             f"send them in batches of at most {MAX_CALLS}")


def version_mismatch(adapter_id: str, served: str, local: str) -> ContractError:
    """The error for a server serving another version of an adapter than the local catalogue has: labels would be
    mapped by the wrong manifest."""
    return ContractError(f"{adapter_id}: the server serves version {served}, this client's catalogue has {local}; "
                         "pass catalogue= with the server's manifests")


def _call_to_wire(call: Call, binding: Binding) -> dict[str, Any]:
    """A call as the server takes it, naming the function by the ID the local catalogue resolved, so the server
    answers exactly the function this client bound."""
    wire: dict[str, Any] = {"function": binding.function.id, "input": call.input}
    if call.context is not None:
        wire["context"] = dict(call.context)
    if call.labels is not None:
        wire["labels"] = _labels_to_wire(call.labels)
    return wire


def _labels_to_wire(labels: Labels) -> dict[str, str | None] | list[str]:
    return dict(labels) if isinstance(labels, Mapping) else list(labels)


class RemoteBoundFunction(BoundFunction):
    """A bound function of a RemoteJeff: bound here once (context, labels and policy checked), each call or many()
    one request."""

    def many(self, inputs: Sequence[JSON]) -> list[Decision]:
        jeff = cast("RemoteJeff", self._jeff)
        return jeff._decide_policy([(self._call(item), self._bound) for item in inputs], self._policy)


class RemoteJeff(Jeff):
    """Jeff over a server. Overrides every method that would reach an engine so each operation is one request:
    _decide_bound (decide, and escalation function steps run here), function (a RemoteBoundFunction),
    _interface_call (the shorthands with function=), dispatch, run_graph (a code-free graph in one request, any
    other graph stage by stage), plus run_server_graph. Close it (or use it as a context manager) to close the
    connection."""

    def __init__(self, transport: Transport, catalogue: Catalogue, url: str) -> None:
        self.transport = transport
        self.url = url
        super().__init__(ServedFunctions(transport, catalogue, url), catalogue)

    def close(self) -> None:
        self.transport.close()

    def resolve_calibration_of(self, function: FunctionSpec) -> Calibration | None:
        """The calibration the server decides `function` with: its blocks as the server lists them (GET
        /v1/functions), for the server's base format, with the same refusal as local mode when the served INT4 format
        has no entry. NotInstalled for a function the server does not list."""
        served = cast("ServedFunctions", self.engine)
        if function.id not in served.calibrations:
            raise NotInstalled(function.id, f"the server at {self.url} does not list it")
        calibration, by_format = served.calibrations[function.id]
        return resolve_calibration(function.id, calibration, by_format, self.format_id)

    def __enter__(self) -> "RemoteJeff":
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None,
                 traceback: TracebackType | None) -> None:
        self.close()

    def _wire_policy(self, policy: Policy) -> WirePolicy | None:
        """server_policy(policy) with each function step named by the ID the local catalogue resolves it to, as calls
        are, so the server runs exactly the steps this client checked."""
        wire = server_policy(policy)
        if isinstance(wire, dict):
            return {**wire, "escalate": [self.catalogue.function(ref).id for ref in wire["escalate"]]}
        return wire

    def _post(self, bound: Sequence[tuple[Call, Binding]], policy: WirePolicy) -> list[Decision]:
        """One request for these calls; none for no calls (as local mode, which answers [] without the engine)."""
        if not bound:
            return []
        check_batch(len(bound), "calls")
        answered = self.transport.decide_calls([_call_to_wire(call, binding) for call, binding in bound], policy)
        return [Decision.from_json(item) for item in answered]

    def _decide_bound(self, bound: Sequence[tuple[Call, Binding]]) -> list[Decision]:
        return self._post(bound, "return")

    def _decide_policy(self, bound: Sequence[tuple[Call, Binding]], policy: Policy) -> list[Decision]:
        """Already-bound calls in one request: with the policy when the server can run it, else with "return" and
        the policy then run here on each decision."""
        wire = self._wire_policy(policy)
        if wire is not None:
            return self._post(bound, wire)
        decisions = self._post(bound, "return")
        return [self._apply(call, decision, policy) for (call, _), decision in zip(bound, decisions, strict=True)]

    def function(self, ref: str, *, context: Mapping[str, JSON] | None = None,
                 labels: Labels | None = None, on_low_confidence: Policy = "return") -> BoundFunction:
        return RemoteBoundFunction(self, Call(ref, None, context, labels), on_low_confidence)

    def _interface_call(self, interface: str, input: JSON, function: str | None, context: Mapping[str, JSON] | None,
                        labels: Labels | None, on_low_confidence: Policy) -> Decision:
        if function is None:
            return self.dispatch(input, interface=interface, context=context, labels=labels,
                                 on_low_confidence=on_low_confidence)
        spec = self.catalogue.function(function)
        if spec.interface != interface:
            raise InterfaceMismatch(f"{spec.id} implements {spec.interface}, not {interface}; call jeff.{spec.interface}")
        check_policy(self, interface, on_low_confidence)
        call = Call(function, input, context, labels)
        return self._decide_policy([(call, self._binding(call))], on_low_confidence)[0]

    def dispatch(self, input: JSON, *, interface: str, context: Mapping[str, JSON] | None = None,
                 labels: Labels | None = None, router: str = DEFAULT_ROUTER,
                 on_low_confidence: Policy = "return") -> Decision:
        check_policy(self, interface, on_low_confidence)
        router_id = _start(self, router)
        wire = self._wire_policy(on_low_confidence)
        request: dict[str, Any] = {"input": input, "interface": interface, "router": router_id}
        if context is not None:
            request["context"] = dict(context)
        if labels is not None:
            request["labels"] = _labels_to_wire(labels)
        decision = Decision.from_json(self.transport.dispatch(
            {"dispatch": request, "on_low_confidence": wire if wire is not None else "return"}))
        if wire is not None or not decision.abstained:
            return decision
        # The policy has a step only this process can run. As in local mode, a router that abstains raises; an
        # abstained leaf is escalated with the call dispatch made to it.
        function = self.catalogue.function(decision.function)
        if self.catalogue.manifest_of(function).is_router:
            raise LowConfidence(decision)
        return self._apply(leaf_call(function, input, context, labels), decision, on_low_confidence)

    def run_graph(self, graph: Graph | Path | str, input: JSON, params: Mapping[str, JSON],
                  code: Mapping[str, Any] | None = None, policies: Mapping[str, Policy] | None = None) -> GraphResult:
        """Run a graph (a Graph or a graph file) and return what local mode returns for the same graph and engine.
        Everything local mode checks before the first decision (params, code=, policies=, every node's function,
        binding and policy) is checked here first, once, without a request.

        A graph with no code nodes, whose every node policy the server can run ("return", "error", or escalation
        through function steps only), is one POST /v1/decide carrying the graph inline, with each node's function
        named by the ID the local catalogue resolves, and runs entirely on the server. Any other graph runs here,
        stage by stage: one request per stage that has model nodes (policy "return"), each node's policy then run in
        this process (each escalation function step one more request), and code nodes called in this process."""
        loaded = graph if isinstance(graph, Graph) else load_graph(Path(graph))
        code_functions, node_policies = dict(code or {}), dict(policies or {})
        model = _check(self, loaded, input, params, code_functions, node_policies)
        wire = {name: self._wire_policy(policy) for name, policy in node_policies.items()}
        has_code = any(node.code is not None for node in loaded.nodes.values())
        if has_code or any(policy is None for policy in wire.values()):
            return _run_checked(self, loaded, input, params, code_functions, model)
        inline = graph_to_dict(loaded)
        for name, (_, binding, _) in model.items():
            inline["nodes"][name]["call"] = binding.function.id
        body: dict[str, Any] = {"graph": {"inline": inline}, "input": input, "params": dict(params)}
        if wire:
            body["on_low_confidence_by_node"] = wire
        return GraphResult.from_json(self.transport.graph(body))

    def run_server_graph(self, graph_id: str, input: JSON, params: Mapping[str, JSON],
                         policies: Mapping[str, Policy] | None = None) -> GraphResult:
        """Run a graph registered on the server, by id, in one request. Every node policy must be one the server can
        run: the graph runs entirely there."""
        by_node: dict[str, WirePolicy] = {}
        for name, policy in (policies or {}).items():
            wire = self._wire_policy(policy)
            if wire is None:
                raise InvalidPolicy(f"Node {name}: a graph registered on the server runs there, and the server cannot "
                                    "call llm, human or remote escalation steps; use 'return', 'error' or "
                                    "escalate(function_step(...), ...) for this node")
            by_node[name] = wire
        body: dict[str, Any] = {"graph": {"id": graph_id}, "input": input, "params": dict(params)}
        if by_node:
            body["on_low_confidence_by_node"] = by_node
        return GraphResult.from_json(self.transport.graph(body))


def remote(url: str, token: TokenSource, *, timeout_s: float, catalogue: Catalogue | None,
           channel: bool) -> RemoteJeff:
    """Jeff.remote: connect to the server at `url` (reads GET /v1/functions once); with `channel`, over the server's
    low-latency channel (jeff.channel_client), where bound functions are handles on the server."""
    if isinstance(timeout_s, bool) or not isinstance(timeout_s, (int, float)) or not math.isfinite(timeout_s) \
            or timeout_s <= 0:
        raise InvalidRequest(f"timeout_s must be a finite number of seconds above 0; got {timeout_s!r}")
    shipped = catalogue if catalogue is not None else Catalogue.shipped()
    if channel:
        from jeff.channel_client import connect
        return connect(url, token, timeout_s, shipped)
    transport = RestTransport(url, token, timeout_s)
    try:
        return RemoteJeff(transport, shipped, transport.url)
    except BaseException:
        transport.close()
        raise
