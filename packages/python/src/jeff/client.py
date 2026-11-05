"""The Jeff client: bind functions, decide in batches grouped by adapter, and the interface shorthands."""

import copy
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from jeff.binding import JSON, Binding, Labels, bind, build_row
from jeff.catalogue import Catalogue
from jeff.decision import Decision, make_decision
from jeff.engine import Engine, format_id_of, serves
from jeff.errors import EngineError, InterfaceMismatch, NotInstalled
from jeff.escalation import Policy, check_policy
from jeff.manifest import Calibration, FunctionSpec, calibration_for
from jeff.routing import DEFAULT_ROUTER

if TYPE_CHECKING:
    from pathlib import Path

    from jeff.graph import Graph, GraphResult
    from jeff.log import DecisionLog
    from jeff.remote import RemoteJeff, TokenSource

# Called with each function just before it is decided (bound for a call, a bound function or a graph node; each
# router and the leaf a dispatch reaches; each escalation step's function). It raises (for example NotLicensed) to
# refuse the function; the engine then never sees a row for it.
type Authorise = Callable[[FunctionSpec], None]


@dataclass(frozen=True)
class Call:
    function: str
    input: JSON
    context: Mapping[str, JSON] | None = None
    labels: Labels | None = None


class BoundFunction:
    def __init__(self, jeff: "Jeff", call_template: Call, on_low_confidence: Policy) -> None:
        self._jeff, self._template, self._policy = jeff, call_template, on_low_confidence
        # bind once, now: context, labels and the policy are checked here and the binding is reused by every call
        self._bound = jeff._binding(call_template)
        check_policy(jeff, self._bound.function.interface, on_low_confidence)

    def _call(self, input: JSON) -> Call:
        return Call(self._template.function, input, self._template.context, self._template.labels)

    def __call__(self, input: JSON) -> Decision:
        return self.many([input])[0]

    def many(self, inputs: Sequence[JSON]) -> list[Decision]:
        calls = [self._call(item) for item in inputs]
        decisions = self._jeff._decide_bound([(call, self._bound) for call in calls])
        return [self._jeff._apply(call, decision, self._policy)
                for call, decision in zip(calls, decisions, strict=True)]

    def close(self) -> None:
        """Releases what this bound function holds on a server: nothing here (local mode, and remote mode over
        HTTP); over the channel (Jeff.remote(..., channel=True)) its handle."""

    def __enter__(self) -> "BoundFunction":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class Jeff:
    def __init__(self, engine: Engine, catalogue: Catalogue, log: "DecisionLog | None" = None,
                 authorise: Authorise | None = None) -> None:
        self.engine, self.catalogue, self.log, self.authorise = engine, catalogue, log, authorise
        # The engine's base format, read once here and never per call: it picks each function's calibration
        # (calibration_of) when the function is bound.
        self.base_format = dict(engine.base_format)
        self.format_id = format_id_of(self.base_format)
        # function id -> its resolved calibration, filled at each function's first binding (calibration_of), so a
        # decide adds one dictionary lookup; a refusal is not kept, and is raised again at the next binding
        self._calibrations: dict[str, Calibration | None] = {}
        from jeff.routing import check_routers
        # router id -> the exact keys its tree refuses (routing.exact_keys_of), worked out here for every installed
        # router (and at the first dispatch for a router installed later), so a dispatch only looks its keys up
        self._exact_keys: dict[str, dict[str, str]] = check_routers(catalogue, engine.installed())

    def with_authorise(self, authorise: Authorise | None) -> "Jeff":
        """A view of this Jeff with its own authorise hook, sharing the engine, catalogue and log: a server holding
        one Jeff for every request builds one view per request, so each caller's hook stays with that request and
        the shared Jeff is never changed."""
        view = copy.copy(self)
        view.authorise = authorise
        return view

    @staticmethod
    def local(adapters: Sequence[str], **options: Any) -> "Jeff":
        from jeff.local import local
        return local(adapters, **options)

    @staticmethod
    def connect(socket_path: "Path | str", *, timeout_s: float, catalogue: Catalogue | None = None,
                log: "DecisionLog | None" = None) -> "Jeff":
        from jeff.socket_engine import SocketEngine
        engine = SocketEngine(socket_path, timeout_s=timeout_s)
        try:
            return Jeff(engine, catalogue if catalogue is not None else Catalogue.shipped(), log=log)
        except Exception:
            engine.close()
            raise

    @staticmethod
    def remote(url: str, token: "TokenSource", *, timeout_s: float, catalogue: Catalogue | None = None,
               channel: bool = False) -> "RemoteJeff":
        """A Jeff over the Jeff server at `url`, with this class's surface (jeff.remote.RemoteJeff). `token` is the
        bearer token: a string, a callable returning the current one, or None only for a server with no
        credentials. Over HTTP the callable is called before each request. With `channel` (the server's low-latency
        WebSocket channel, where bound functions are handles: jeff.channel_client) it is called at auth, in the
        refresh window before the token's exp (until it returns a token expiring later, which it must before exp, or
        the channel closes with Unauthorised) and for each GET /v1/functions; a string token is never refreshed.
        `catalogue` holds the manifests calls are bound with (None: the catalogue shipped with jeff); connecting
        refuses a server that serves another version of any of them."""
        from jeff.remote import remote
        return remote(url, token, timeout_s=timeout_s, catalogue=catalogue, channel=channel)

    def calibration_of(self, function: FunctionSpec) -> Calibration | None:
        """The calibration `function` decides with on this Jeff's engine: ManifestError for a calibrated function
        with no block for the INT4 base the engine serves. Resolved once per function (resolve_calibration_of) and
        kept."""
        if function.id in self._calibrations:
            return self._calibrations[function.id]
        calibration = self.resolve_calibration_of(function)
        self._calibrations[function.id] = calibration
        return calibration

    def resolve_calibration_of(self, function: FunctionSpec) -> Calibration | None:
        """calibration_of, uncached: jeff.manifest.calibration_for the engine's base format, from the catalogue's
        manifest (a RemoteJeff resolves from the server's listing instead)."""
        return calibration_for(function, self.format_id)

    def _binding(self, call: Call) -> Binding:
        function = self.catalogue.function(call.function)
        # Every function is bound here before it is decided, so this is the one place the hook is called, and the
        # one place its calibration on the engine's base format is chosen (or refused).
        if self.authorise is not None:
            self.authorise(function)
        manifest = self.catalogue.manifest_of(function)
        if not serves(self.engine, manifest.id):
            raise NotInstalled(function.id)
        return bind(manifest, function, call.context, call.labels, calibration=self.calibration_of(function))

    def decide(self, calls: Sequence[Call]) -> list[Decision]:
        return self._decide_bound([(call, self._binding(call)) for call in calls])

    def _decide_bound(self, bound: Sequence[tuple[Call, Binding]]) -> list[Decision]:
        calls = [call for call, _ in bound]
        bindings = [binding for _, binding in bound]
        groups: dict[str, list[int]] = {}
        for index, binding in enumerate(bindings):
            groups.setdefault(binding.manifest.id, []).append(index)
        results: list[Decision | None] = [None] * len(calls)
        for adapter_id, indices in groups.items():
            rows = [build_row(bindings[i], calls[i].input) for i in indices]
            output = self.engine.predict(adapter_id, rows)
            if len(output.probabilities) != len(rows):
                raise EngineError(f"The engine answered {len(output.probabilities)} of {len(rows)} rows for {adapter_id}")
            for i, probabilities in zip(indices, output.probabilities, strict=True):
                results[i] = make_decision(bindings[i], probabilities, output.timing)
        decisions = [decision for decision in results if decision is not None]
        if self.log is not None:
            for call, decision in zip(calls, decisions, strict=True):
                self.log.write(call, decision)
        return decisions

    def function(self, ref: str, *, context: Mapping[str, JSON] | None = None,
                 labels: Labels | None = None, on_low_confidence: Policy = "return") -> BoundFunction:
        return BoundFunction(self, Call(ref, None, context, labels), on_low_confidence)

    def _apply(self, call: Call, decision: Decision, policy: Policy) -> Decision:
        from jeff.escalation import apply_policy
        return apply_policy(self, call, decision, policy)

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
        return self._apply(call, self.decide([call])[0], on_low_confidence)

    def dispatch(self, input: JSON, *, interface: str, context: Mapping[str, JSON] | None = None,
                 labels: Labels | None = None, router: str = DEFAULT_ROUTER,
                 on_low_confidence: Policy = "return") -> Decision:
        from jeff.routing import dispatch
        return dispatch(self, input, interface=interface, context=context, labels=labels, router=router,
                        on_low_confidence=on_low_confidence)

    def classify(self, input: JSON, *, function: str | None = None, context: Mapping[str, JSON] | None = None,
                 labels: Labels | None = None, on_low_confidence: Policy = "return") -> Decision:
        return self._interface_call("classify", input, function, context, labels, on_low_confidence)

    def route(self, input: JSON, *, function: str | None = None, context: Mapping[str, JSON] | None = None,
              labels: Labels | None = None, on_low_confidence: Policy = "return") -> Decision:
        return self._interface_call("route", input, function, context, labels, on_low_confidence)

    def rate(self, input: JSON, *, function: str | None = None, context: Mapping[str, JSON] | None = None,
             labels: Labels | None = None, on_low_confidence: Policy = "return") -> Decision:
        return self._interface_call("rate", input, function, context, labels, on_low_confidence)

    def detect(self, input: JSON, *, function: str | None = None, context: Mapping[str, JSON] | None = None,
               labels: Labels | None = None, on_low_confidence: Policy = "return") -> Decision:
        return self._interface_call("detect", input, function, context, labels, on_low_confidence)

    def is_supported(self, input: JSON, *, function: str | None = None, context: Mapping[str, JSON] | None = None,
                     labels: Labels | None = None, on_low_confidence: Policy = "return") -> Decision:
        return self._interface_call("is_supported", input, function, context, labels, on_low_confidence)

    def equivalent(self, input: JSON, *, function: str | None = None, context: Mapping[str, JSON] | None = None,
                   labels: Labels | None = None, on_low_confidence: Policy = "return") -> Decision:
        return self._interface_call("equivalent", input, function, context, labels, on_low_confidence)

    def requires_escalation(self, input: JSON, *, function: str | None = None,
                            context: Mapping[str, JSON] | None = None, labels: Labels | None = None,
                            on_low_confidence: Policy = "return") -> Decision:
        return self._interface_call("requires_escalation", input, function, context, labels, on_low_confidence)

    def run_graph(self, graph: "Graph | Path | str", input: JSON, params: Mapping[str, JSON],
                  code: Mapping[str, Any] | None = None, policies: Mapping[str, Policy] | None = None) -> "GraphResult":
        from pathlib import Path
        from jeff.graph import Graph, load_graph, run_graph
        loaded = graph if isinstance(graph, Graph) else load_graph(Path(graph))
        return run_graph(self, loaded, input, params, code, policies)
