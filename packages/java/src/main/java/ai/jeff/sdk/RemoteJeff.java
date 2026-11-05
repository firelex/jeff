package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.SequencedMap;

/** Jeff over a server (port of jeff/remote.py), from {@link Jeff#remote(String, String, RemoteOptions)}. It keeps a
 * local catalogue, so binding, labels and policies are worked out as in local mode, and each operation is one request
 * (at most 256 calls). A policy the server can run ({@code "return"}, {@code "error"}, function steps only) is sent
 * with the request; one with an llm, human or remote step runs here on the decision the server returns. Connecting
 * reads {@code GET /v1/functions} once and refuses a server serving another version of an adapter the local catalogue
 * has. */
public class RemoteJeff extends Jeff {
    /** The most calls one request carries (contract/schemas/decide_request); more are refused, never split. */
    static final int MAX_CALLS = 256;

    private final Transport transport;
    private final String url;

    RemoteJeff(Parts parts, Transport transport, String url) {
        super(parts);
        this.transport = transport;
        this.url = url;
    }

    static RemoteJeff connect(Transport transport, Catalogue catalogue, String url) {
        ServedFunctions served = ServedFunctions.read(transport, catalogue, url);
        return new RemoteJeff(parts(served, catalogue), transport, url);
    }

    Transport transport() {
        return transport;
    }

    /** The server's address, as the client calls it.
     *
     * @return the address */
    public String url() {
        return url;
    }

    static JeffError.ContractError versionMismatch(String adapterId, String served, String local) {
        return new JeffError.ContractError(adapterId + ": the server serves version " + served + ", this client's catalogue has "
            + local + "; pass catalogue= with the server's manifests");
    }

    static void checkBatch(int count, String what) {
        if (count > MAX_CALLS) {
            throw new JeffError.InvalidRequest(count + " " + what + " in one request: a Jeff server answers at most " + MAX_CALLS
                + " per request; send them in batches of at most " + MAX_CALLS);
        }
    }

    /** The wire form of a policy the server can run: "return", "error", or {escalate: [function refs], budget_ms?};
     * null when the policy has an llm, human or remote step, which only this process can run. */
    static Object serverPolicy(Policy policy) {
        if (Policy.RETURN.equals(policy) || Policy.ERROR.equals(policy)) return ((Policy.Named) policy).name();
        if (!(policy instanceof Escalate escalate)) throw Escalation.unknownPolicy(policy);
        List<Object> refs = new ArrayList<>();
        for (Step step : escalate.steps()) {
            if (step instanceof Step.AskStep || step instanceof Step.RemoteStep) return null;
            if (!(step instanceof Step.FunctionStep function)) throw Escalation.notAStep(step);
            refs.add(function.ref());
        }
        Map<String, Object> wire = new LinkedHashMap<>();
        wire.put("escalate", refs);
        if (escalate.budgetMs() != null) wire.put("budget_ms", escalate.budgetMs());
        return wire;
    }

    /** serverPolicy with every function step named by the full ID the local catalogue resolves, so the server runs
     * exactly the steps this client checked. */
    Object wirePolicy(Policy policy) {
        Object wire = serverPolicy(policy);
        if (wire instanceof Map<?, ?> escalate) {
            Map<String, Object> resolved = new LinkedHashMap<>(Values.map(escalate));
            List<Object> ids = new ArrayList<>();
            for (Object ref : Values.list(escalate.get("escalate"))) ids.add(catalogue().function((String) ref).id());
            resolved.put("escalate", ids);
            return resolved;
        }
        return wire;
    }

    static Object labelsToWire(Object labels) {
        if (labels instanceof List<?> keys) return new ArrayList<>(keys);
        return new LinkedHashMap<>((Map<?, ?>) labels);
    }

    /** A call as the server takes it, naming the function by the ID the local catalogue resolved, and its context by
     * the code names binding accepted. */
    private static Map<String, Object> callToWire(Call call, Binding binding) {
        Map<String, Object> wire = new LinkedHashMap<>();
        wire.put("function", binding.function().id());
        wire.put("input", call.input());
        if (call.context() != null) wire.put("context", new LinkedHashMap<>(call.context()));
        if (call.labels() != null) wire.put("labels", labelsToWire(call.labels()));
        return wire;
    }

    /** A decision the server answered, checked against contract/schemas/decision before it is built. */
    static Decision serverDecision(Map<String, Object> data, String url) {
        if (!Contract.isValid("decision", data)) {
            throw new JeffError.ContractError("A decision from " + url + " does not match contract/schemas/decision: "
                + Contract.firstProblem("decision", data));
        }
        return Decision.fromJson(data);
    }

    /** Closes the client's connections to the server; a request still in flight fails with the closed error. */
    @Override
    public void close() {
        transport.close();
    }

    /** The calibration the server decides {@code fn} with, from its listing; NotInstalled for a function it does not list. */
    @Override
    Calibration resolveCalibrationOf(FunctionSpec fn) {
        ServedFunctions.Listed listed = ((ServedFunctions) engine()).calibrations().get(fn.id());
        if (listed == null) throw JeffError.notInstalled(fn.id(), "the server at " + url + " does not list it");
        return Manifests.resolveCalibration(fn.id(), listed.calibration(), listed.byFormat(), formatId());
    }

    /** One request for these calls; none for no calls (as local mode answers [] without the engine). */
    private List<Decision> post(List<Bound> bound, Object policy) {
        if (bound.isEmpty()) return List.of();
        checkBatch(bound.size(), "calls");
        List<Map<String, Object>> calls = new ArrayList<>();
        for (Bound item : bound) calls.add(callToWire(item.call(), item.binding()));
        List<Decision> decisions = new ArrayList<>();
        for (Map<String, Object> answered : transport.decideCalls(calls, policy)) decisions.add(serverDecision(answered, url));
        return List.copyOf(decisions);
    }

    static GraphResult serverGraphResult(Map<String, Object> data, String url) {
        if (!Contract.isValid("graph_result", data)) {
            throw new JeffError.ContractError("A graph result from " + url + " does not match contract/schemas/graph_result: "
                + Contract.firstProblem("graph_result", data));
        }
        return GraphResult.fromJson(data);
    }

    @Override
    List<Decision> decideBound(List<Bound> bound) {
        return post(bound, "return");
    }

    /** Already-bound calls in one request: with the policy when the server can run it, else with "return" and the
     * policy then run here on each decision. */
    List<Decision> decidePolicy(List<Bound> bound, Policy policy) {
        Object wire = wirePolicy(policy);
        if (wire != null) return post(bound, wire);
        List<Decision> decisions = post(bound, "return");
        List<Decision> out = new ArrayList<>();
        for (int index = 0; index < bound.size(); index++) out.add(applyPolicy(bound.get(index).call(), decisions.get(index), policy));
        return List.copyOf(out);
    }

    /** Binds a function here, once (context, labels and policy checked); each call is then one request.
     *
     * @param ref the function's reference
     * @param options its context, labels and policy
     * @return a {@link RemoteBoundFunction}
     * @throws JeffError.UnknownFunction when the catalogue has no such function
     * @throws JeffError.NotInstalled when the server does not serve it
     * @throws JeffError the binding's errors ({@link JeffError.MissingContext}, {@link JeffError.InvalidLabels},
     *     {@link JeffError.InvalidPolicy}, ...) */
    @Override
    public BoundFunction function(String ref, CallOptions options) {
        return RemoteBoundFunction.create(this, new Call(ref, null, options.context(), options.labels()), options.onLowConfidence());
    }

    @Override
    Decision interfaceCall(String iface, Object input, String function, CallOptions options) {
        Policy policy = options.onLowConfidence();
        if (function == null) return dispatch(input, iface, DEFAULT_ROUTER, options);
        FunctionSpec spec = catalogue().function(function);
        if (!spec.interfaceName().equals(iface)) {
            throw new JeffError.InterfaceMismatch(spec.id() + " implements " + spec.interfaceName() + ", not " + iface
                + "; call jeff." + spec.interfaceName());
        }
        Escalation.checkPolicy(this, iface, policy);
        Call call = new Call(function, input, options.context(), options.labels());
        return decidePolicy(List.of(new Bound(call, bindCall(call))), policy).get(0);
    }

    /** Routes the call on the server, in one request, from a router to the function that should answer it.
     *
     * @param input the input
     * @param iface the interface the answer must come through
     * @param router the router to start from (never null)
     * @param options its context, labels and policy
     * @return the decision of the function the routers chose, with the route they took
     * @throws JeffError.LowConfidence when a router abstains, or the decision abstains under {@link Policy#ERROR}
     * @throws JeffError.InvalidPolicy when the policy is not one the SDK knows
     * @throws JeffError.ContractError when the server answers in a form the contract does not allow
     * @throws JeffError the server's named errors ({@link JeffError.Unauthorised}, {@link JeffError.RateLimited},
     *     {@link JeffError.Busy}, ...) and the transport's ({@link JeffError.BackendUnavailable},
     *     {@link JeffError.DeadlineExceeded}) */
    @Override
    public Decision dispatch(Object input, String iface, String router, CallOptions options) {
        Objects.requireNonNull(router, "router");
        Policy policy = options.onLowConfidence();
        Escalation.checkPolicy(this, iface, policy);
        String routerId = Routing.startRouter(this, router);
        Object wire = wirePolicy(policy);
        Map<String, Object> request = new LinkedHashMap<>();
        request.put("input", input);
        request.put("interface", iface);
        request.put("router", routerId);
        if (options.context() != null) request.put("context", new LinkedHashMap<>(options.context()));
        if (options.labels() != null) request.put("labels", labelsToWire(options.labels()));
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("dispatch", request);
        body.put("on_low_confidence", wire == null ? "return" : wire);
        Decision decision = serverDecision(transport.dispatch(body), url);
        if (wire != null || !decision.abstained()) return decision;
        // The policy has a step only this process can run. As in local mode, a router that abstains raises; an
        // abstained leaf is escalated with the call dispatch made to it.
        FunctionSpec fn = catalogue().function(decision.function());
        if (catalogue().manifestOf(fn).isRouter()) throw JeffError.lowConfidence(decision);
        return applyPolicy(Routing.leafCall(fn, input, options.context(), options.labels()), decision, policy);
    }

    /** A graph with no code nodes, whose node policies the server can run, is one request carrying the graph inline
     * (each node's function by the ID the local catalogue resolves); any other graph runs here stage by stage.
     * Everything local mode checks before the first decision is checked here first, without a request.
     *
     * @param graph the graph
     * @param input the graph's input
     * @param params the graph's params (an empty map for none)
     * @param code the code nodes' functions, by name (an empty map for none)
     * @param policies a policy per node (an empty map for none)
     * @return every node's result, the outputs and the stages run
     * @throws JeffError.GraphError when a param, code function or policy is missing or unknown, or a node cannot run
     * @throws JeffError.ContractError when the server answers in a form the contract does not allow
     * @throws JeffError the server's named errors ({@link JeffError.Unauthorised}, {@link JeffError.RateLimited},
     *     {@link JeffError.Busy}, ...) and the transport's ({@link JeffError.BackendUnavailable},
     *     {@link JeffError.DeadlineExceeded}) */
    @Override
    public GraphResult runGraph(Graph graph, Object input, Map<String, ?> params, Map<String, CodeFunction> code,
                                Map<String, Policy> policies) {
        Objects.requireNonNull(graph, "graph");
        Objects.requireNonNull(params, "params (an empty map for a graph that declares none)");
        Objects.requireNonNull(code, "code (an empty map for a graph with no code nodes)");
        Objects.requireNonNull(policies, "policies (an empty map to use the policies the graph document gives)");
        SequencedMap<String, Graphs.ModelNode> model = Graphs.check(this, graph, input, params, code, policies);
        Map<String, Object> wire = new LinkedHashMap<>();
        boolean runsHere = graph.nodes().values().stream().anyMatch(node -> node.code() != null);
        for (Map.Entry<String, Policy> entry : policies.entrySet()) {
            Object policy = wirePolicy(entry.getValue());
            wire.put(entry.getKey(), policy);
            if (policy == null) runsHere = true;
        }
        if (runsHere) return Graphs.runChecked(this, graph, input, params, code, model);
        Map<String, Object> inline = Graphs.toMap(graph);
        Map<String, Object> nodes = Values.map(inline.get("nodes"));
        for (Map.Entry<String, Graphs.ModelNode> entry : model.entrySet()) {
            Values.map(nodes.get(entry.getKey())).put("call", entry.getValue().binding().function().id());
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("graph", Map.of("inline", inline));
        body.put("input", input);
        body.put("params", new LinkedHashMap<>(params));
        if (!wire.isEmpty()) body.put("on_low_confidence_by_node", wire);
        return serverGraphResult(transport.graph(body), url);
    }

    /** Runs a graph registered on the server, by id, in one request.
     *
     * @param graphId the graph's id on the server
     * @param input the graph's input
     * @param params the graph's params (an empty map for none)
     * @param policies a policy per node, each one the server can run (an empty map for none)
     * @return every node's result, the outputs and the stages run
     * @throws JeffError.InvalidPolicy when a node's policy has an llm, human or remote step
     * @throws JeffError.ContractError when the server answers in a form the contract does not allow
     * @throws JeffError the server's named errors ({@link JeffError.Unauthorised}, {@link JeffError.RateLimited},
     *     {@link JeffError.Busy}, ...) and the transport's ({@link JeffError.BackendUnavailable},
     *     {@link JeffError.DeadlineExceeded}) */
    public GraphResult runServerGraph(String graphId, Object input, Map<String, ?> params, Map<String, Policy> policies) {
        Objects.requireNonNull(graphId, "graphId");
        Objects.requireNonNull(params, "params (an empty map for a graph that declares none)");
        Objects.requireNonNull(policies, "policies (an empty map for the policies the registered graph gives)");
        Map<String, Object> byNode = new LinkedHashMap<>();
        for (Map.Entry<String, Policy> entry : policies.entrySet()) {
            Object wire = wirePolicy(entry.getValue());
            if (wire == null) {
                throw new JeffError.InvalidPolicy("Node " + entry.getKey() + ": a graph registered on the server runs there, and "
                    + "the server cannot call llm, human or remote escalation steps; use 'return', 'error' or "
                    + "escalate([Step.function(...), ...]) for this node");
            }
            byNode.put(entry.getKey(), wire);
        }
        Map<String, Object> body = new LinkedHashMap<>();
        body.put("graph", Map.of("id", graphId));
        body.put("input", input);
        body.put("params", new LinkedHashMap<>(params));
        if (!byNode.isEmpty()) body.put("on_low_confidence_by_node", byNode);
        return serverGraphResult(transport.graph(body), url);
    }
}
