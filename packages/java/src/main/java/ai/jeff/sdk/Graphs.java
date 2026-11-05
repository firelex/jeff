package ai.jeff.sdk;

import java.math.BigDecimal;
import java.math.BigInteger;
import java.nio.file.Path;
import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Collections;
import java.util.Deque;
import java.util.Iterator;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.SequencedMap;
import java.util.Set;
import java.util.TreeSet;
import java.util.concurrent.CancellationException;

/** Decision graphs: functions wired together with declarative conditions, run in batched stages (port of
 * jeff/graph.py).
 *
 * <p>The dependency walk and the cycle search use their own stacks, so a long chain of nodes is ordered on any
 * thread; a deep condition is read on a thread with a large stack. Where the Python SDK
 * stops with a KeyError or TypeError on a Graph built by hand (not read from a document), this code throws a
 * GraphError naming what is wrong. */
public final class Graphs {
    private Graphs() {}

    private static JeffError.GraphError graphError(String message, Throwable cause) {
        JeffError.GraphError error = new JeffError.GraphError(message);
        if (cause != null) error.initCause(cause);
        return error;
    }

    /** A part of node {@code node}'s condition that is not a condition's shape (only a hand-built GraphNode has one; Python
     * stops there with a KeyError or TypeError); {@code graphId} is null where the graph is not known. */
    private static JeffError.GraphError unreadable(String graphId, String node, Object part) {
        String what = "the condition of node " + node + " has a part graph.schema.json does not allow: " + PyFormat.repr(part);
        return new JeffError.GraphError(graphId == null ? Character.toUpperCase(what.charAt(0)) + what.substring(1) : graphId + ": " + what);
    }

    /** The nodes a condition reads, walked with its own stack in Python's order (an {@code all} before an {@code any}). */
    private static Set<String> conditionNodes(String node, Map<String, Object> condition) {
        Set<String> found = new LinkedHashSet<>();
        if (condition == null) return found;
        Deque<Object> parts = new ArrayDeque<>();
        parts.push(condition);
        while (!parts.isEmpty()) {
            Object part = parts.pop();
            if (!(part instanceof Map<?, ?> map)) throw unreadable(null, node, part);
            if (map.containsKey("all") || map.containsKey("any")) {
                if (!(map.get(map.containsKey("all") ? "all" : "any") instanceof List<?> items)) throw unreadable(null, node, part);
                for (int index = items.size() - 1; index >= 0; index--) parts.push(items.get(index));
            } else if (map.containsKey("not")) {
                parts.push(map.get("not"));
            } else if (map.get("node") instanceof String name) {
                found.add(name);
            } else {
                throw unreadable(null, node, part);
            }
        }
        return found;
    }

    /** The nodes a node waits on.
     *
     * @param node the node
     * @return those its condition reads and those it runs after */
    public static Set<String> dependencies(GraphNode node) {
        Set<String> found = conditionNodes(node.name(), node.when());
        found.addAll(node.after());
        return found;
    }

    /** One node of the cycle search: its name and the dependencies it has still to visit. */
    private record Visit(String name, Iterator<String> next) {}

    /** The order the dependencies give, as Python's recursive visit gives it (each node's dependencies sorted, then the
     * node), with Python's cycle path; walked with its own stack. */
    private static List<String> order(String where, Map<String, GraphNode> nodes) {
        List<String> order = new ArrayList<>();
        Map<String, Boolean> done = new LinkedHashMap<>();
        List<Visit> stack = new ArrayList<>();
        for (String root : nodes.keySet()) {
            if (done.containsKey(root)) continue;
            done.put(root, false);
            stack.add(new Visit(root, PyFormat.sorted(dependencies(nodes.get(root))).iterator()));
            while (!stack.isEmpty()) {
                Visit top = stack.getLast();
                if (!top.next().hasNext()) {
                    done.put(top.name(), true);
                    order.add(top.name());
                    stack.removeLast();
                    continue;
                }
                String dependency = top.next().next();
                Boolean state = done.get(dependency);
                if (Boolean.TRUE.equals(state)) continue;
                if (Boolean.FALSE.equals(state)) {
                    List<String> path = new ArrayList<>(stack.stream().map(Visit::name).toList());
                    path.add(dependency);
                    throw new JeffError.GraphError(where + ": the nodes form a cycle: " + String.join(" -> ", path));
                }
                done.put(dependency, false);
                stack.add(new Visit(dependency, PyFormat.sorted(dependencies(nodes.get(dependency))).iterator()));
            }
        }
        return order;
    }

    /** A graph document (contract/schemas/graph.schema.json) as a Graph, its nodes in document order.
     *
     * @param raw the document (parsed YAML or JSON)
     * @param where where it came from, named in every error
     * @return the graph
     * @throws JeffError.GraphError naming {@code where} for anything wrong with it (its shape, a node that names an
     *     unknown node, a cycle) */
    public static Graph fromMap(Object raw, String where) {
        Object copy;
        try {
            copy = Values.plain(raw);
        } catch (IllegalArgumentException error) {
            throw graphError(where + ": " + error.getMessage(), error);
        }
        try {
            Contract.validate("graph", copy, where);
        } catch (JeffError.ContractError error) {
            throw graphError(error.getMessage(), error);
        }
        Map<String, Object> doc = Values.map(copy);
        List<String> params = Values.strings(doc.get("params"));
        Map<String, Object> rawNodes = Values.map(doc.get("nodes"));
        Map<String, GraphNode> nodes = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : rawNodes.entrySet()) {
            String name = entry.getKey();
            Map<String, Object> body = Values.map(entry.getValue());
            SequencedMap<String, String> context = new LinkedHashMap<>();
            if (body.containsKey("context")) {
                for (Map.Entry<String, Object> field : Values.map(body.get("context")).entrySet()) {
                    context.put(field.getKey(), (String) Values.map(field.getValue()).get("param"));
                }
            }
            GraphNode node = new GraphNode(name, (String) body.get("call"), (String) body.get("code"),
                body.containsKey("after") ? Values.strings(body.get("after")) : List.of(), context,
                body.containsKey("labels") ? (String) Values.map(body.get("labels")).get("param") : null,
                body.containsKey("when") ? Values.map(body.get("when")) : null,
                body.containsKey("on_low_confidence") ? (String) body.get("on_low_confidence") : "return");
            nodes.put(name, node);
            List<String> unknown = PyFormat.sorted(dependencies(node).stream().filter(other -> !rawNodes.containsKey(other)).toList());
            if (!unknown.isEmpty()) {
                throw new JeffError.GraphError(where + ": node " + name + " refers to unknown node(s) " + String.join(", ", unknown));
            }
            Set<String> used = new LinkedHashSet<>(context.values());
            if (node.labels() != null) used.add(node.labels());
            List<String> undeclared = PyFormat.sorted(used.stream().filter(param -> !params.contains(param)).toList());
            if (!undeclared.isEmpty()) {
                throw new JeffError.GraphError(where + ": node " + name + " uses undeclared param(s) " + String.join(", ", undeclared));
            }
        }
        List<String> outputs = Values.strings(doc.get("outputs"));
        List<String> missing = PyFormat.sorted(new LinkedHashSet<>(outputs.stream().filter(name -> !nodes.containsKey(name)).toList()));
        if (!missing.isEmpty()) throw new JeffError.GraphError(where + ": outputs name unknown node(s) " + String.join(", ", missing));
        return new Graph((String) doc.get("graph"), (String) doc.get("version"), (String) doc.get("input"), params,
            Values.frozenMap(nodes), outputs, order(where, nodes));
    }

    private static Map<String, Object> nodeToMap(String graphId, String key, GraphNode node) {
        if (!node.name().equals(key)) {
            throw new JeffError.GraphError(graphId + ": the node under " + PyFormat.repr(key) + " is named " + node.name()
                + "; a graph document names each node by its key only");
        }
        if (node.call() != null && node.code() != null) {
            throw new JeffError.GraphError(graphId + ": node " + key + " has both call and code; a node is a model call or a code function");
        }
        if (node.call() == null && node.code() == null) throw new JeffError.GraphError(graphId + ": node " + key + " has neither call nor code");
        if (!"return".equals(node.onLowConfidence()) && !"error".equals(node.onLowConfidence())) {
            throw new JeffError.GraphError(graphId + ": node " + key + " has on_low_confidence " + PyFormat.repr(node.onLowConfidence())
                + "; a graph document holds only 'return' or 'error' (pass other policies in policies=)");
        }
        Map<String, Object> body = new LinkedHashMap<>();
        if (node.call() != null) body.put("call", node.call());
        else body.put("code", node.code());
        if (!node.after().isEmpty()) body.put("after", new ArrayList<>(node.after()));
        if (!node.context().isEmpty()) {
            Map<String, Object> context = new LinkedHashMap<>();
            for (Map.Entry<String, String> field : node.context().entrySet()) {
                Map<String, Object> param = new LinkedHashMap<>();
                param.put("param", field.getValue());
                context.put(field.getKey(), param);
            }
            body.put("context", context);
        }
        if (node.labels() != null) {
            Map<String, Object> param = new LinkedHashMap<>();
            param.put("param", node.labels());
            body.put("labels", param);
        }
        if (node.when() != null) body.put("when", Values.plain(node.when()));
        if (!node.onLowConfidence().equals("return")) body.put("on_low_confidence", node.onLowConfidence());
        return body;
    }

    /** The graph as the document fromMap reads: fromMap(toMap(graph)) is the same graph. A Graph the document cannot
     * hold exactly (a node with both or neither of call and code, a policy other than 'return' or 'error', a node keyed
     * by another name, an order other than the one its dependencies give) is a GraphError naming what cannot be
     * written.
     *
     * @param graph the graph
     * @return the document
     * @throws JeffError.GraphError when no document reads back as this graph */
    public static Map<String, Object> toMap(Graph graph) {
        Map<String, Object> nodes = new LinkedHashMap<>();
        for (Map.Entry<String, GraphNode> entry : graph.nodes().entrySet()) {
            nodes.put(entry.getKey(), nodeToMap(graph.id(), entry.getKey(), entry.getValue()));
        }
        Map<String, Object> raw = new LinkedHashMap<>();
        raw.put("graph", graph.id());
        raw.put("version", graph.version());
        raw.put("input", graph.input());
        raw.put("params", new ArrayList<>(graph.params()));
        raw.put("nodes", nodes);
        raw.put("outputs", new ArrayList<>(graph.outputs()));
        Graph rebuilt = fromMap(raw, "graph " + graph.id());
        List<String> differing = new ArrayList<>();
        if (!Objects.equals(rebuilt.id(), graph.id())) differing.add("id");
        if (!Objects.equals(rebuilt.version(), graph.version())) differing.add("version");
        if (!Objects.equals(rebuilt.input(), graph.input())) differing.add("input");
        if (!rebuilt.params().equals(graph.params())) differing.add("params");
        if (!rebuilt.nodes().equals(graph.nodes())) differing.add("nodes");
        if (!rebuilt.outputs().equals(graph.outputs())) differing.add("outputs");
        if (!rebuilt.order().equals(graph.order())) differing.add("order");
        if (!differing.isEmpty()) {
            String detail = differing.equals(List.of("order"))
                ? "its order " + String.join(", ", graph.order()) + " is not the order its dependencies give ("
                    + String.join(", ", rebuilt.order()) + ")"
                : "field(s) " + String.join(", ", differing) + " differ";
            throw new JeffError.GraphError(graph.id() + " cannot be written as a graph document that reads back the same: " + detail);
        }
        return raw;
    }

    /** Reads and checks a graph file (Python's load_graph).
     *
     * @param path the YAML (or JSON) file
     * @return the graph
     * @throws JeffError.GraphError for anything wrong with the graph
     * @throws JeffError.ManifestError when the file is not valid YAML
     * @throws java.io.UncheckedIOException when it cannot be read */
    public static Graph load(Path path) {
        return fromMap(Yaml.read(path), path.toString());
    }

    // ---- running

    /** Python's {@code a < b} for a float and a JSON number (bool as 0 or 1), compared exactly. */
    private static boolean below(double value, Object bound) {
        BigDecimal limit;
        if (bound instanceof Boolean flag) limit = flag ? BigDecimal.ONE : BigDecimal.ZERO;
        else if (bound instanceof Long || bound instanceof Integer) limit = BigDecimal.valueOf(((Number) bound).longValue());
        else if (bound instanceof BigInteger big) limit = new BigDecimal(big);
        else if (bound instanceof Double number) {
            if (Double.isNaN(number) || Double.isNaN(value)) return false;
            if (Double.isInfinite(number) || Double.isInfinite(value)) return value < number;
            limit = new BigDecimal(number);
        } else {
            throw new IllegalStateException("below() takes a JSON number, not " + bound);
        }
        if (Double.isNaN(value)) return false;
        if (Double.isInfinite(value)) return value < 0;
        return new BigDecimal(value).compareTo(limit) < 0;
    }

    private static boolean number(Object value) {
        return value instanceof Boolean || value instanceof Long || value instanceof Integer || value instanceof BigInteger
            || value instanceof Double;
    }

    /** A node result's value for {@code is} and {@code in}: a decision's value or what a code node returned. */
    private static Object valueOf(NodeResult result) {
        if (result instanceof NodeResult.Decided decided) return decided.decision().value();
        if (result instanceof NodeResult.Code code) return code.value();
        return result; // a Skipped result, which (as Python's Skipped object) equals no JSON value
    }

    /** Whether node {@code node}'s condition holds for the results so far, read as Python's _holds reads it. */
    private static boolean holds(String graphId, String node, Map<String, Object> condition, Map<String, NodeResult> results) {
        return Nesting.run(Nesting.shallow(condition), () -> holdsHere(graphId, node, condition, results));
    }

    private static boolean holdsHere(String graphId, String node, Object part, Map<String, NodeResult> results) {
        if (!(part instanceof Map<?, ?> raw)) throw unreadable(graphId, node, part);
        Map<String, Object> condition = Values.map(raw);
        if (condition.containsKey("all")) {
            if (!(condition.get("all") instanceof List<?> items)) throw unreadable(graphId, node, part);
            for (Object item : items) if (!holdsHere(graphId, node, item, results)) return false;
            return true;
        }
        if (condition.containsKey("any")) {
            if (!(condition.get("any") instanceof List<?> items)) throw unreadable(graphId, node, part);
            for (Object item : items) if (holdsHere(graphId, node, item, results)) return true;
            return false;
        }
        if (condition.containsKey("not")) return !holdsHere(graphId, node, condition.get("not"), results);
        if (!(condition.get("node") instanceof String name)) throw unreadable(graphId, node, part);
        NodeResult result = results.get(name);
        if (result == null) {
            throw new JeffError.GraphError(graphId + ": the condition of node " + node + " reads node " + name
                + ", which has no result yet");
        }
        if (condition.containsKey("is")) return PyFormat.pyEquals(valueOf(result), condition.get("is"));
        if (condition.containsKey("in")) {
            if (!(condition.get("in") instanceof List<?> items)) throw unreadable(graphId, node, part);
            for (Object item : items) if (PyFormat.pyEquals(valueOf(result), item)) return true;
            return false;
        }
        if (!(result instanceof NodeResult.Decided decided)) {
            throw new JeffError.GraphError("Condition on " + name + " needs a model decision; " + name + " is a code node");
        }
        // Python: `result.abstained is condition["abstained"]`, true only for the same bool
        if (condition.containsKey("abstained")) return Boolean.valueOf(decided.decision().abstained()).equals(condition.get("abstained"));
        // Python: `result.confidence is not None and result.confidence < condition["confidence_below"]`
        Double confidence = decided.decision().confidence();
        if (confidence == null) return false;
        if (!number(condition.get("confidence_below"))) throw unreadable(graphId, node, part);
        return below(confidence, condition.get("confidence_below"));
    }

    /** Each model node's call, binding and policy, as check resolved them. */
    record ModelNode(Call call, Binding binding, Policy policy) {}

    /** Everything that can be checked before the first decision: params, code and policies name real things, and
     * every model node resolves in the catalogue, is installed, binds its context and labels, and has a valid policy. */
    static SequencedMap<String, ModelNode> check(Jeff jeff, Graph graph, Object input, Map<String, ?> params,
                                                 Map<String, CodeFunction> code, Map<String, Policy> policies) {
        List<String> missing = graph.params().stream().filter(name -> !params.containsKey(name)).toList();
        if (!missing.isEmpty()) throw new JeffError.GraphError(graph.id() + " needs param(s) " + String.join(", ", missing));
        List<String> unknownPolicies = PyFormat.sorted(policies.keySet().stream().filter(name -> !graph.nodes().containsKey(name)).toList());
        if (!unknownPolicies.isEmpty()) {
            throw new JeffError.GraphError(graph.id() + ": policies= names node(s) " + String.join(", ", unknownPolicies)
                + ", which the graph does not have (its nodes: " + String.join(", ", graph.nodes().keySet()) + ")");
        }
        Set<String> usedCode = new TreeSet<>(PyFormat::compareCodePoints);
        for (GraphNode node : graph.nodes().values()) if (node.code() != null) usedCode.add(node.code());
        List<String> unknownCode = PyFormat.sorted(code.keySet().stream().filter(name -> !usedCode.contains(name)).toList());
        if (!unknownCode.isEmpty()) {
            String used = usedCode.isEmpty() ? "none" : String.join(", ", usedCode);
            throw new JeffError.GraphError(graph.id() + ": code= names function(s) " + String.join(", ", unknownCode)
                + ", which no node uses (code nodes use: " + used + ")");
        }
        List<String> missingCode = usedCode.stream().filter(name -> !code.containsKey(name)).toList();
        if (!missingCode.isEmpty()) {
            throw new JeffError.GraphError(graph.id() + " needs code function(s) " + String.join(", ", missingCode) + "; pass them in code=");
        }
        SequencedMap<String, ModelNode> model = new LinkedHashMap<>();
        for (String name : graph.order()) {
            GraphNode node = graph.nodes().get(name);
            if (node == null) throw new JeffError.GraphError(graph.id() + ": its order names node " + name + ", which the graph does not have");
            if (node.call() == null) {
                if (node.code() == null) throw new JeffError.GraphError(graph.id() + ": node " + name + " has neither call nor code");
                continue;
            }
            FunctionSpec fn;
            try {
                fn = jeff.catalogue().function(node.call());
            } catch (JeffError.UnknownFunction error) {
                throw graphError(graph.id() + ": node " + name + " calls " + node.call() + ": " + error.getMessage(), error);
            }
            if (!jeff.engine().installed().contains(fn.adapterId())) {
                throw JeffError.notInstalled(fn.id(), "node " + name + " of graph " + graph.id() + " calls it");
            }
            // Python: `params[node.labels] if node.labels else None` (an empty param name gives no labels)
            String labelsParam = node.labels() != null && !node.labels().isEmpty() ? node.labels() : null;
            Set<String> used = new LinkedHashSet<>(node.context().values());
            if (labelsParam != null) used.add(labelsParam);
            List<String> notGiven = PyFormat.sorted(used.stream().filter(param -> !params.containsKey(param)).toList());
            if (!notGiven.isEmpty()) {
                throw new JeffError.GraphError(graph.id() + ": node " + name + " uses param(s) " + String.join(", ", notGiven)
                    + " that were not given");
            }
            Map<String, Object> context = new LinkedHashMap<>();
            for (Map.Entry<String, String> field : node.context().entrySet()) context.put(field.getKey(), params.get(field.getValue()));
            Object labels = labelsParam != null ? params.get(labelsParam) : null;
            Call call = new Call(node.call(), input, node.context().isEmpty() ? null : context, labels);
            Binding binding;
            try {
                binding = jeff.bindCall(call);
            } catch (JeffError.NotLicensed error) {
                throw error; // a refused licence is the caller's, not a problem with the graph
            } catch (JeffError error) {
                throw graphError(graph.id() + ": node " + name + " (" + fn.id() + "): " + error.getMessage(), error);
            }
            Policy policy = policies.containsKey(name) ? policies.get(name) : Policy.named(node.onLowConfidence());
            Escalation.checkPolicy(jeff, fn.interfaceName(), policy);
            model.put(name, new ModelNode(call, binding, policy));
        }
        return model;
    }

    /** What a code function returned, as a JSON value; a failure (or a value that is not JSON) is a GraphError naming
     * the function and its node, with the failure as its cause. A cancellation is let through as itself. */
    private static Object runCode(Graph graph, String name, String codeName, CodeFunction function, CodeCall call) {
        Object value;
        try {
            value = function.run(call);
        } catch (CancellationException error) {
            throw error;
        } catch (RuntimeException error) {
            throw graphError(graph.id() + ": code function " + codeName + " (node " + name + ") failed: "
                + (error.getMessage() == null ? JeffError.describe(error) : error.getMessage()), error);
        }
        try {
            return Values.plain(value);
        } catch (IllegalArgumentException error) {
            throw graphError(graph.id() + ": code function " + codeName + " (node " + name + ") returned a "
                + value.getClass().getName() + ", which is not a JSON value (" + error.getMessage() + ")", error);
        }
    }

    /** Runs a graph whose model nodes check resolved: each stage is every node whose dependencies have results, its
     * model nodes decided in one batch, then its code nodes called with every result so far. */
    static GraphResult runChecked(Jeff jeff, Graph graph, Object input, Map<String, ?> params, Map<String, CodeFunction> code,
                                  SequencedMap<String, ModelNode> model) {
        SequencedMap<String, NodeResult> results = new LinkedHashMap<>();
        Map<String, Object> paramsCopy = Collections.unmodifiableMap(new LinkedHashMap<String, Object>(params));
        long stages = 0;
        while (results.size() < graph.nodes().size()) {
            // check found every name of the order among the nodes
            List<String> ready = graph.order().stream().filter(name -> !results.containsKey(name)
                && dependencies(graph.nodes().get(name)).stream().allMatch(results::containsKey)).toList();
            if (ready.isEmpty()) {
                List<String> waiting = PyFormat.sorted(graph.nodes().keySet().stream().filter(name -> !results.containsKey(name)).toList());
                throw new JeffError.GraphError(graph.id() + " makes no progress: node(s) " + String.join(", ", waiting)
                    + " wait on each other or on nodes the graph does not have");
            }
            stages += 1;
            List<String> batch = new ArrayList<>();
            List<String> coded = new ArrayList<>();
            for (String name : ready) {
                GraphNode node = graph.nodes().get(name);
                List<String> skipped = PyFormat.sorted(dependencies(node).stream()
                    .filter(dependency -> results.get(dependency) instanceof NodeResult.Skipped).toList());
                if (!skipped.isEmpty()) results.put(name, new NodeResult.Skipped("depends on skipped node " + skipped.get(0)));
                else if (node.when() != null && !holds(graph.id(), name, node.when(), results)) results.put(name, new NodeResult.Skipped("condition not met"));
                else if (model.containsKey(name)) batch.add(name);
                else if (node.code() != null) coded.add(name);
                else throw new JeffError.GraphError(graph.id() + ": node " + name + " has neither call nor code");
            }
            if (!batch.isEmpty()) {
                List<Jeff.Bound> bound = new ArrayList<>();
                for (String name : batch) bound.add(new Jeff.Bound(model.get(name).call(), model.get(name).binding()));
                List<Decision> decisions = jeff.decideBound(bound);
                for (int index = 0; index < batch.size(); index++) {
                    ModelNode node = model.get(batch.get(index));
                    results.put(batch.get(index), new NodeResult.Decided(jeff.applyPolicy(node.call(), decisions.get(index), node.policy())));
                }
            }
            for (String name : coded) {
                String codeName = graph.nodes().get(name).code();
                CodeCall call = new CodeCall(input, paramsCopy, Values.frozenMap(results));
                results.put(name, new NodeResult.Code(runCode(graph, name, codeName, code.get(codeName), call)));
            }
        }
        // Python reads each output's result here (a KeyError for a Graph built by hand whose outputs name no node)
        List<String> unknownOutputs = PyFormat.sorted(new LinkedHashSet<>(graph.outputs().stream().filter(name -> !results.containsKey(name)).toList()));
        if (!unknownOutputs.isEmpty()) {
            throw new JeffError.GraphError(graph.id() + ": outputs name unknown node(s) " + String.join(", ", unknownOutputs));
        }
        SequencedMap<String, NodeResult> outputs = new LinkedHashMap<>();
        for (String name : graph.outputs()) outputs.put(name, results.get(name));
        return new GraphResult(results, outputs, stages);
    }

    /** Checks the graph, then runs it. */
    static GraphResult run(Jeff jeff, Graph graph, Object input, Map<String, ?> params, Map<String, CodeFunction> code,
                           Map<String, Policy> policies) {
        Objects.requireNonNull(graph, "graph");
        Objects.requireNonNull(params, "params (an empty map for a graph that declares none)");
        Objects.requireNonNull(code, "code (an empty map for a graph with no code nodes)");
        Objects.requireNonNull(policies, "policies (an empty map to use the policies the graph document gives)");
        return runChecked(jeff, graph, input, params, code, check(jeff, graph, input, params, code, policies));
    }
}
