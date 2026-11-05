using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>One node of a graph: a model call (Call) or a code function (Code), the nodes it waits for (After), its
/// context (code name to the param it takes its value from, in document order), the param its labels come from, its
/// condition (graph.schema.json's condition: all, any, not, or node with is, in, abstained or confidence_below), and its
/// policy name ("return" or "error").</summary>
public sealed record GraphNode(string Name, string? Call, string? Code, IReadOnlyList<string> After, OrderedMap<string> Context, string? Labels,
    JsonObject? When, string OnLowConfidence);

/// <summary>A decision graph; Nodes in document order (names that look like numbers included), Order the order the
/// nodes run in (each after its dependencies).</summary>
public sealed record Graph(string Id, string Version, string Input, IReadOnlyList<string> Params, OrderedMap<GraphNode> Nodes,
    IReadOnlyList<string> Outputs, IReadOnlyList<string> Order);

/// <summary>One node's result as contract/schemas/graph_result writes it: {decision} for a model node, {skipped: true,
/// reason} for a skipped one, {code} for what a code function returned.</summary>
public abstract record NodeResult
{
    private protected NodeResult()
    {
    }

    /// <summary>The result as contract/schemas/graph_result writes a node: {"decision": ...}, {"skipped": true, "reason": ...} or {"code": ...}.</summary>
    public abstract JsonObject ToJson();

    /// <summary>Reads a node result written by ToJson; GraphError when it holds none of decision, skipped or code.</summary>
    public static NodeResult FromJson(JsonObject data)
    {
        if (data.ContainsKey("decision")) return new DecisionResult(Decision.FromJson(JsonRead.Object(data, "decision", "a graph result node")));
        if (data.ContainsKey("skipped"))
        {
            if (Json.KindOf(data["reason"]) != JsonKind.String)
            {
                throw new GraphError($"A skipped graph result node needs its 'reason' string (got {PyFormat.Repr(data["reason"])})");
            }
            return new SkippedResult(data["reason"]!.GetValue<string>());
        }
        if (data.ContainsKey("code")) return new CodeResult(Json.Canonical(data["code"]));
        throw new GraphError($"A graph result node must hold 'decision', 'skipped' or 'code'; got keys {PyFormat.Repr(PyFormat.Sorted(data.Select(pair => pair.Key)))}");
    }
}

/// <summary>A model node's result: its decision.</summary>
/// <param name="Decision">The decision the node's function made.</param>
public sealed record DecisionResult(Decision Decision) : NodeResult
{
    /// <summary>The result as {"decision": ...}.</summary>
    public override JsonObject ToJson() => new() { ["decision"] = Decision.ToJson() };
}

/// <summary>A node that did not run: its condition was false, or a node it depends on was skipped.</summary>
/// <param name="Reason">Why the node was skipped.</param>
public sealed record SkippedResult(string Reason) : NodeResult
{
    /// <summary>The result as {"skipped": true, "reason": ...}.</summary>
    public override JsonObject ToJson() => new() { ["skipped"] = true, ["reason"] = Reason };
}

/// <summary>A code node's result: the value its function returned.</summary>
/// <param name="Value">The value the code function returned.</param>
public sealed record CodeResult(JsonNode? Value) : NodeResult
{
    /// <summary>The result as {"code": ...}.</summary>
    public override JsonObject ToJson() => new() { ["code"] = Json.Canonical(Value) };

    /// <summary>Equal when both values are the same JSON, int versus float included.</summary>
    public bool Equals(CodeResult? other) => other is not null && Json.StrictEquals(Value, other.Value);

    /// <summary>A hash of the value's JSON text, consistent with Equals.</summary>
    public override int GetHashCode() => Json.Write(Value).GetHashCode(StringComparison.Ordinal);
}

/// <summary>A graph's results: every node in the order it got its result, the outputs, and how many stages ran.</summary>
public sealed class GraphResult(OrderedMap<NodeResult> nodes, OrderedMap<NodeResult> outputs, long stages)
{
    /// <summary>Every node's result, in the order the nodes got them.</summary>
    public OrderedMap<NodeResult> Nodes { get; } = nodes;

    /// <summary>The results of the graph's outputs, in the order the graph lists them.</summary>
    public OrderedMap<NodeResult> Outputs { get; } = outputs;

    /// <summary>How many batched stages ran.</summary>
    public long Stages { get; } = stages;

    private static JsonObject Results(OrderedMap<NodeResult> results)
    {
        var json = new JsonObject();
        foreach (var (name, result) in results) json[name] = result.ToJson();
        return json;
    }

    /// <summary>The result as contract/schemas/graph_result writes it: {"nodes", "outputs", "stages"}.</summary>
    public JsonObject ToJson() => new() { ["nodes"] = Results(Nodes), ["outputs"] = Results(Outputs), ["stages"] = Stages };

    /// <summary>Reads a graph result written by ToJson; GraphError for a node that is not a result, ContractError for a missing field.</summary>
    public static GraphResult FromJson(JsonObject data)
    {
        static OrderedMap<NodeResult> Read(JsonObject results) =>
            new(results.Select(pair => new KeyValuePair<string, NodeResult>(pair.Key,
                NodeResult.FromJson(pair.Value as JsonObject ?? throw new GraphError($"The graph result node {pair.Key} is not an object (got {PyFormat.Repr(pair.Value)})")))));
        return new GraphResult(Read(JsonRead.Object(data, "nodes", "a graph result")), Read(JsonRead.Object(data, "outputs", "a graph result")),
            JsonRead.Long(data, "stages", "a graph result"));
    }
}

/// <summary>What a code node's function is given: the graph's input, its params, and every result so far in the order
/// the nodes got them.</summary>
public sealed record CodeCall(JsonNode? Input, JsonObject Params, OrderedMap<NodeResult> Nodes);

/// <summary>A code node's function: given the graph's input, params and the results so far, it returns the node's value. An
/// exception it throws is a GraphError naming the node.</summary>
public delegate Task<JsonNode?> CodeFunction(CodeCall call, CancellationToken cancellationToken);

/// <summary>A model node's call, binding and policy, as Graphs.CheckAsync resolved them.</summary>
internal sealed record ModelNode(Call Call, Binding Binding, Policy Policy);

/// <summary>Decision graphs: functions wired together with declarative conditions, run in batched stages (port of
/// jeff/graph.py). No walk here overflows the stack: a document is measured without recursion and refused past
/// MaxDepth levels, the dependency order and a condition's nodes are found with explicit stacks, and reading a
/// hand-built condition stops with a GraphError when the thread's stack runs low.</summary>
public static class Graphs
{
    /// <summary>The deepest a graph document may be nested (objects and lists inside one another). Python's own reader
    /// stops (RecursionError) at about 166 levels of nested 'not' conditions, so this accepts every document Python reads.</summary>
    public const int MaxDepth = 256;

    private static readonly string[] Fields = ["id", "version", "input", "params", "nodes", "outputs", "order"];

    /// <summary>The nodes a condition reads, found with an explicit stack. A part that is not a condition's shape (only
    /// a hand-built condition can hold one; the schema refuses it in a document) is a GraphError naming node `nodeName`,
    /// where Python stops with a KeyError or TypeError.</summary>
    private static HashSet<string> ConditionNodes(string nodeName, JsonObject? condition)
    {
        var found = new HashSet<string>(StringComparer.Ordinal);
        if (condition is null) return found;
        var pending = new Stack<JsonObject>();
        pending.Push(condition);
        while (pending.Count > 0)
        {
            var part = pending.Pop();
            if (part.ContainsKey("all") || part.ContainsKey("any"))
            {
                // Python: condition.get("all", condition.get("any", []))
                if ((part.ContainsKey("all") ? part["all"] : part["any"]) is not JsonArray items || !items.All(item => item is JsonObject))
                {
                    throw UnreadableCondition(nodeName, part);
                }
                foreach (var item in items) pending.Push(item!.AsObject());
            }
            else if (part.ContainsKey("not"))
            {
                pending.Push(part["not"] as JsonObject ?? throw UnreadableCondition(nodeName, part));
            }
            else if (part["node"] is JsonValue node && Json.KindOf(node) == JsonKind.String) found.Add(node.GetValue<string>());
            else throw UnreadableCondition(nodeName, part);
        }
        return found;
    }

    private static GraphError UnreadableCondition(string nodeName, JsonObject part) =>
        new($"The condition of node {nodeName} has a part graph.schema.json does not allow: {PyFormat.Repr(part)}");

    /// <summary>A copy of `value` made canonical inside a graph's reading or writing: a value too deep for the stack the
    /// thread has left (or one Json.Canonical refuses) is a GraphError naming `where`, with Json.Canonical's error as
    /// its cause.</summary>
    private static JsonNode? CanonicalIn(JsonNode? value, string where)
    {
        try
        {
            return Json.Canonical(value);
        }
        catch (InvalidRequest error)
        {
            throw new GraphError($"{where}: {error.Message}", null, error);
        }
    }

    /// <summary>The nodes `node` depends on: the ones it runs after and the ones its condition reads. GraphError for a hand-built condition of the wrong shape.</summary>
    public static HashSet<string> Dependencies(GraphNode node)
    {
        var dependencies = ConditionNodes(node.Name, node.When);
        dependencies.UnionWith(node.After);
        return dependencies;
    }

    /// <summary>How deeply `value` nests objects and lists (a scalar is 0), measured without recursion.</summary>
    private static int DepthOf(JsonNode? value)
    {
        int deepest = 0;
        var pending = new Stack<(JsonNode? Node, int Depth)>();
        pending.Push((value, 1));
        while (pending.Count > 0)
        {
            var (node, depth) = pending.Pop();
            IEnumerable<JsonNode?> children;
            if (node is JsonObject obj) children = obj.Select(pair => pair.Value);
            else if (node is JsonArray array) children = array;
            else continue;
            deepest = Math.Max(deepest, depth);
            if (deepest > MaxDepth) return deepest;
            foreach (var child in children) pending.Push((child, depth + 1));
        }
        return deepest;
    }

    private static GraphError TooDeep(string what) =>
        new($"{what} is nested more than {MaxDepth} levels deep (objects and lists inside one another); a graph document is never that deep");

    /// <summary>The order the nodes run in: Python's depth-first visit (nodes in document order, each one's
    /// dependencies in sorted order), walked with an explicit stack so a long chain never overflows the stack.</summary>
    private static List<string> DependencyOrder(OrderedMap<GraphNode> nodes, string where)
    {
        var order = new List<string>();
        var done = new Dictionary<string, bool>(StringComparer.Ordinal);
        var path = new List<(string Name, List<string> Dependencies, int Next)>();
        void Enter(string name)
        {
            done[name] = false;
            path.Add((name, PyFormat.Sorted(Dependencies(nodes[name])), 0));
        }
        foreach (var root in nodes.Keys)
        {
            if (done.ContainsKey(root)) continue;
            Enter(root);
            while (path.Count > 0)
            {
                var (name, dependencies, next) = path[^1];
                if (next == dependencies.Count)
                {
                    path.RemoveAt(path.Count - 1);
                    done[name] = true;
                    order.Add(name);
                    continue;
                }
                path[^1] = (name, dependencies, next + 1);
                string dependency = dependencies[next];
                if (done.TryGetValue(dependency, out bool finished))
                {
                    if (finished) continue;
                    throw new GraphError($"{where}: the nodes form a cycle: {string.Join(" -> ", path.Select(frame => frame.Name).Append(dependency))}");
                }
                Enter(dependency);
            }
        }
        return order;
    }

    /// <summary>A graph document (graph.schema.json) as a Graph; `where` names it in every message.</summary>
    public static Graph FromDict(JsonNode? raw, string where)
    {
        if (DepthOf(raw) > MaxDepth) throw TooDeep(where);
        var document = CanonicalIn(raw, where);
        try
        {
            Contract.Validate("graph", document, where);
        }
        catch (ContractError error)
        {
            throw new GraphError(error.Message, null, error);
        }
        var doc = document!.AsObject();
        var parameters = JsonRead.Strings(doc, "params", where);
        var rawNodes = JsonRead.Object(doc, "nodes", where);
        var names = rawNodes.Select(pair => pair.Key).ToHashSet(StringComparer.Ordinal);
        var nodes = new OrderedMap<GraphNode>();
        foreach (var (name, value) in rawNodes)
        {
            var body = value!.AsObject();
            var context = new OrderedMap<string>();
            if (body["context"] is JsonObject fields)
            {
                foreach (var (field, reference) in fields) context.Set(field, JsonRead.String(reference!.AsObject(), "param", where));
            }
            var node = new GraphNode(name,
                body.ContainsKey("call") ? JsonRead.String(body, "call", where) : null,
                body.ContainsKey("code") ? JsonRead.String(body, "code", where) : null,
                body.ContainsKey("after") ? JsonRead.Strings(body, "after", where) : [],
                context,
                body.ContainsKey("labels") ? JsonRead.String(JsonRead.Object(body, "labels", where), "param", where) : null,
                body["when"] is JsonObject when ? (JsonObject)CanonicalIn(when, where)! : null,
                body.ContainsKey("on_low_confidence") ? JsonRead.String(body, "on_low_confidence", where) : "return");
            nodes.Set(name, node);
            var unknown = PyFormat.Sorted(Dependencies(node).Where(other => !names.Contains(other)));
            if (unknown.Count > 0) throw new GraphError($"{where}: node {name} refers to unknown node(s) {string.Join(", ", unknown)}");
            var used = context.Values.Concat(node.Labels is { } labels ? new[] { labels } : Array.Empty<string>()).ToHashSet(StringComparer.Ordinal);
            var undeclared = PyFormat.Sorted(used.Where(param => !parameters.Contains(param)));
            if (undeclared.Count > 0) throw new GraphError($"{where}: node {name} uses undeclared param(s) {string.Join(", ", undeclared)}");
        }
        var outputs = JsonRead.Strings(doc, "outputs", where);
        var missingOutputs = PyFormat.Sorted(outputs.Where(name => !nodes.ContainsKey(name)).Distinct(StringComparer.Ordinal));
        if (missingOutputs.Count > 0) throw new GraphError($"{where}: outputs name unknown node(s) {string.Join(", ", missingOutputs)}");
        var order = DependencyOrder(nodes, where);
        return new Graph(JsonRead.String(doc, "graph", where), JsonRead.String(doc, "version", where), JsonRead.String(doc, "input", where),
            parameters, nodes, outputs, order);
    }

    private static JsonObject NodeToDict(string graphId, string key, GraphNode node)
    {
        if (node.Name != key)
        {
            throw new GraphError($"{graphId}: the node under {PyFormat.Str(key)} is named {node.Name}; a graph document names each node by its key only");
        }
        if (node.Call is not null && node.Code is not null)
        {
            throw new GraphError($"{graphId}: node {key} has both call and code; a node is a model call or a code function");
        }
        if (node.Call is null && node.Code is null) throw new GraphError($"{graphId}: node {key} has neither call nor code");
        if (node.OnLowConfidence is not ("return" or "error"))
        {
            throw new GraphError($"{graphId}: node {key} has on_low_confidence {PyFormat.Str(node.OnLowConfidence)}; a graph document holds only "
                + "'return' or 'error' (pass other policies in policies=)");
        }
        var body = node.Call is not null ? new JsonObject { ["call"] = node.Call } : new JsonObject { ["code"] = node.Code };
        if (node.After.Count > 0) body["after"] = Json.Strings(node.After);
        if (node.Context.Count > 0)
        {
            var context = new JsonObject();
            foreach (var (field, param) in node.Context) context[field] = new JsonObject { ["param"] = param };
            body["context"] = context;
        }
        if (node.Labels is not null) body["labels"] = new JsonObject { ["param"] = node.Labels };
        if (node.When is not null)
        {
            if (DepthOf(node.When) > MaxDepth) throw TooDeep($"{graphId}: the condition of node {key}");
            body["when"] = CanonicalIn(node.When, $"{graphId}: the condition of node {key}");
        }
        if (node.OnLowConfidence != "return") body["on_low_confidence"] = node.OnLowConfidence;
        return body;
    }

    private static bool SameContext(OrderedMap<string> a, OrderedMap<string> b) =>
        a.Count == b.Count && a.All(pair => b.TryGetValue(pair.Key, out var param) && param == pair.Value);

    private static bool SameNode(GraphNode a, GraphNode b) =>
        a.Name == b.Name && a.Call == b.Call && a.Code == b.Code && a.After.SequenceEqual(b.After, StringComparer.Ordinal)
        && SameContext(a.Context, b.Context) && a.Labels == b.Labels && PyFormat.PyEquals(a.When, b.When) && a.OnLowConfidence == b.OnLowConfidence;

    private static bool SameField(string field, Graph a, Graph b) => field switch
    {
        "id" => a.Id == b.Id,
        "version" => a.Version == b.Version,
        "input" => a.Input == b.Input,
        "params" => a.Params.SequenceEqual(b.Params, StringComparer.Ordinal),
        "outputs" => a.Outputs.SequenceEqual(b.Outputs, StringComparer.Ordinal),
        "order" => a.Order.SequenceEqual(b.Order, StringComparer.Ordinal),
        _ => a.Nodes.Count == b.Nodes.Count && a.Nodes.All(pair => b.Nodes.TryGetValue(pair.Key, out var other) && SameNode(pair.Value, other)),
    };

    /// <summary>The graph as the document FromDict reads: FromDict(ToDict(graph)) is the same graph. A Graph the document
    /// cannot hold exactly is a GraphError naming what cannot be written.</summary>
    public static JsonObject ToDict(Graph graph)
    {
        var nodes = new JsonObject();
        foreach (var (key, node) in graph.Nodes) nodes[key] = NodeToDict(graph.Id, key, node);
        var raw = new JsonObject
        {
            ["graph"] = graph.Id, ["version"] = graph.Version, ["input"] = graph.Input, ["params"] = Json.Strings(graph.Params), ["nodes"] = nodes,
            ["outputs"] = Json.Strings(graph.Outputs),
        };
        var rebuilt = FromDict(raw, $"graph {graph.Id}");
        var differing = Fields.Where(field => !SameField(field, rebuilt, graph)).ToList();
        if (differing.Count > 0)
        {
            string detail = differing is ["order"]
                ? $"its order {string.Join(", ", graph.Order)} is not the order its dependencies give ({string.Join(", ", rebuilt.Order)})"
                : $"field(s) {string.Join(", ", differing)} differ";
            throw new GraphError($"{graph.Id} cannot be written as a graph document that reads back the same: {detail}");
        }
        return raw;
    }

    /// <summary>The graph in a graph file (YAML, read as PyYAML's safe_load reads it).</summary>
    public static Graph Load(string path) => FromDict(PyYaml.Load(File.ReadAllText(path), path), path);

    private static JsonNode? ValueOf(NodeResult result) => result switch
    {
        DecisionResult decided => decided.Decision.Value,
        CodeResult coded => coded.Value,
        _ => throw new InvalidOperationException("a skipped node has no value; its dependents are skipped before any condition is read"),
    };

    /// <summary>A part of a hand-built condition that graph.schema.json does not allow, where Python stops with a
    /// KeyError or TypeError.</summary>
    private static GraphError BadCondition(string graphId, string nodeName, JsonObject part) =>
        new($"{graphId}: the condition of node {nodeName} has a part graph.schema.json does not allow: {PyFormat.Repr(part)}");

    /// <summary>Whether a condition holds, read in Python's order (all, any, not, then node with is, in, abstained or
    /// confidence_below).</summary>
    private static bool Holds(string graphId, string nodeName, JsonObject condition, OrderedMap<NodeResult> results)
    {
        if (StackGuard.Exhausted) throw new GraphError($"{graphId}: the condition of node {nodeName} {StackGuard.TooDeep}");
        foreach (var key in new[] { "all", "any" })
        {
            if (!condition.ContainsKey(key)) continue;
            if (condition[key] is not JsonArray items || !items.All(item => item is JsonObject)) throw BadCondition(graphId, nodeName, condition);
            return key == "all"
                ? items.All(item => Holds(graphId, nodeName, item!.AsObject(), results))
                : items.Any(item => Holds(graphId, nodeName, item!.AsObject(), results));
        }
        if (condition.ContainsKey("not"))
        {
            return condition["not"] is JsonObject negated ? !Holds(graphId, nodeName, negated, results) : throw BadCondition(graphId, nodeName, condition);
        }
        if (condition["node"] is not JsonValue nodeValue || Json.KindOf(nodeValue) != JsonKind.String) throw BadCondition(graphId, nodeName, condition);
        string name = nodeValue.GetValue<string>();
        var result = results[name];
        if (condition.ContainsKey("is")) return PyFormat.PyEquals(ValueOf(result), condition["is"]);
        if (condition.ContainsKey("in"))
        {
            // Python: `value in list`, each item compared with == (True == 1, 1 == 1.0)
            return condition["in"] is JsonArray options ? options.Any(item => PyFormat.PyEquals(ValueOf(result), item)) : throw BadCondition(graphId, nodeName, condition);
        }
        if (result is not DecisionResult decided) throw new GraphError($"Condition on {name} needs a model decision; {name} is a code node");
        if (condition.ContainsKey("abstained"))
        {
            // Python: `result.abstained is condition["abstained"]`: only the bools True and False can be identical to it
            var abstained = condition["abstained"];
            return Json.KindOf(abstained) == JsonKind.Bool && abstained!.GetValue<bool>() == decided.Decision.Abstained;
        }
        if (decided.Decision.Confidence is not { } confidence) return false;
        var below = condition["confidence_below"];
        if (Json.KindOf(below) is not (JsonKind.Bool or JsonKind.Int or JsonKind.Float)) throw BadCondition(graphId, nodeName, condition);
        return PyFormat.CompareNumbers(JsonValue.Create(confidence), below) < 0;
    }

    /// <summary>Everything that can be checked before the first decision: params, code and policies name real things,
    /// and every model node resolves in the catalogue, is installed, binds its context and labels, and has a valid policy.</summary>
    internal static async Task<OrderedMap<ModelNode>> CheckAsync(JeffClient jeff, Graph graph, JsonNode? input, JsonObject parameters,
        IReadOnlyDictionary<string, CodeFunction> code, IReadOnlyDictionary<string, Policy> policies, CancellationToken cancellationToken)
    {
        var missing = graph.Params.Where(name => !parameters.ContainsKey(name)).ToList();
        if (missing.Count > 0) throw new GraphError($"{graph.Id} needs param(s) {string.Join(", ", missing)}");
        var unknownPolicies = PyFormat.Sorted(policies.Keys.Where(name => !graph.Nodes.ContainsKey(name)));
        if (unknownPolicies.Count > 0)
        {
            throw new GraphError($"{graph.Id}: policies= names node(s) {string.Join(", ", unknownPolicies)}, which the graph does not have (its nodes: "
                + $"{string.Join(", ", graph.Nodes.Keys)})");
        }
        var usedCode = graph.Nodes.Values.Where(node => node.Code is not null).Select(node => node.Code!).ToHashSet(StringComparer.Ordinal);
        var unknownCode = PyFormat.Sorted(code.Keys.Where(name => !usedCode.Contains(name)));
        if (unknownCode.Count > 0)
        {
            string used = usedCode.Count > 0 ? string.Join(", ", PyFormat.Sorted(usedCode)) : "none";
            throw new GraphError($"{graph.Id}: code= names function(s) {string.Join(", ", unknownCode)}, which no node uses (code nodes use: {used})");
        }
        var missingCode = PyFormat.Sorted(usedCode.Where(name => !code.ContainsKey(name)));
        if (missingCode.Count > 0) throw new GraphError($"{graph.Id} needs code function(s) {string.Join(", ", missingCode)}; pass them in code=");
        var model = new OrderedMap<ModelNode>();
        foreach (var name in graph.Order)
        {
            var node = NodeInOrder(graph, name);
            if (node.Call is null)
            {
                if (node.Code is null) throw new GraphError($"{graph.Id}: node {name} has neither call nor code");
                continue;
            }
            FunctionSpec fn;
            try
            {
                fn = jeff.Catalogue.Function(node.Call);
            }
            catch (UnknownFunction error)
            {
                throw new GraphError($"{graph.Id}: node {name} calls {node.Call}: {error.Message}", null, error);
            }
            if (!(await jeff.Engine.InstalledAsync(false, cancellationToken).ConfigureAwait(false)).Contains(fn.AdapterId))
            {
                throw Errors.NotInstalledError(fn.Id, $"node {name} of graph {graph.Id} calls it");
            }
            // a hand-built Graph can name params its document would have declared (Python: a KeyError)
            var notGiven = PyFormat.Sorted(node.Context.Values.Concat(string.IsNullOrEmpty(node.Labels) ? Array.Empty<string>() : new[] { node.Labels })
                .Where(param => !parameters.ContainsKey(param)).Distinct(StringComparer.Ordinal));
            if (notGiven.Count > 0) throw new GraphError($"{graph.Id}: node {name} uses param(s) {string.Join(", ", notGiven)} that were not given");
            var context = new JsonObject();
            foreach (var (field, param) in node.Context) context[field] = Json.Canonical(parameters[param]);
            // Python: `params[node.labels] if node.labels else None` (an empty param name gives no labels)
            JsonNode? labels = string.IsNullOrEmpty(node.Labels) ? null : Json.Canonical(parameters[node.Labels]);
            var call = new Call(node.Call, input, node.Context.Count > 0 ? context : null, labels);
            Binding binding;
            try
            {
                binding = await jeff.BindCallAsync(call, cancellationToken).ConfigureAwait(false);
            }
            catch (JeffError error) when (error is not NotLicensed)
            {
                // a refused licence is the caller's, not a problem with the graph, so it is raised as it is
                throw new GraphError($"{graph.Id}: node {name} ({fn.Id}): {error.Message}", null, error);
            }
            var policy = policies.TryGetValue(name, out var given) ? given : Policy.Named(node.OnLowConfidence);
            await Escalation.CheckPolicyAsync(jeff, fn.Interface, policy, cancellationToken).ConfigureAwait(false);
            model.Set(name, new ModelNode(call, binding, policy));
        }
        return model;
    }

    /// <summary>The node an order name names; a hand-built Graph whose order names no node is a GraphError (Python: a KeyError).</summary>
    private static GraphNode NodeInOrder(Graph graph, string name) =>
        graph.Nodes.TryGetValue(name, out var node) ? node : throw new GraphError($"{graph.Id}: its order names node {name}, which the graph does not have");

    /// <summary>Runs a checked graph: each stage is every node whose dependencies have results, its model nodes decided
    /// in one batch, then its code nodes called with every result so far. A code function that fails is a GraphError
    /// naming it and its node, with its exception as the inner exception (a cancellation is let through as it is).</summary>
    internal static async Task<GraphResult> RunCheckedAsync(JeffClient jeff, Graph graph, JsonNode? input, JsonObject parameters,
        IReadOnlyDictionary<string, CodeFunction> code, OrderedMap<ModelNode> model, CancellationToken cancellationToken)
    {
        foreach (var name in graph.Order) NodeInOrder(graph, name);
        var results = new OrderedMap<NodeResult>();
        long stages = 0;
        while (results.Count < graph.Nodes.Count)
        {
            var ready = graph.Order.Where(name => !results.ContainsKey(name) && Dependencies(graph.Nodes[name]).All(results.ContainsKey)).ToList();
            if (ready.Count == 0)
            {
                var waiting = PyFormat.Sorted(graph.Nodes.Keys.Where(name => !results.ContainsKey(name)));
                throw new GraphError($"{graph.Id} makes no progress: node(s) {string.Join(", ", waiting)} wait on each other or on nodes the graph does not have");
            }
            stages++;
            var batch = new List<string>();
            var coded = new List<(string Name, string Code)>();
            foreach (var name in ready)
            {
                var node = graph.Nodes[name];
                var skipped = PyFormat.Sorted(Dependencies(node).Where(dependency => results[dependency] is SkippedResult));
                if (skipped.Count > 0) results.Set(name, new SkippedResult($"depends on skipped node {skipped[0]}"));
                else if (node.When is not null && !Holds(graph.Id, name, node.When, results)) results.Set(name, new SkippedResult("condition not met"));
                else if (model.ContainsKey(name)) batch.Add(name);
                else if (node.Code is not null) coded.Add((name, node.Code));
                else throw new GraphError($"{graph.Id}: node {name} has neither call nor code");
            }
            if (batch.Count > 0)
            {
                var decisions = await jeff.DecideBoundAsync(batch.Select(name => new BoundCall(model[name].Call, model[name].Binding)).ToList(),
                    cancellationToken).ConfigureAwait(false);
                for (int index = 0; index < batch.Count; index++)
                {
                    var node = model[batch[index]];
                    results.Set(batch[index], new DecisionResult(await jeff.ApplyPolicyAsync(node.Call, decisions[index], node.Policy, cancellationToken).ConfigureAwait(false)));
                }
            }
            foreach (var (name, codeName) in coded)
            {
                if (!code.TryGetValue(codeName, out var function))
                {
                    throw new GraphError($"{graph.Id} needs code function(s) {codeName}; pass them in code=");
                }
                JsonNode? returned;
                try
                {
                    returned = await function(new CodeCall(Json.Canonical(input), (JsonObject)Json.Canonical(parameters)!, results.Copy()), cancellationToken)
                        .ConfigureAwait(false);
                }
                catch (Exception error) when (error is not OperationCanceledException)
                {
                    throw new GraphError($"{graph.Id}: code function {codeName} (node {name}) failed: {error.Message}", null, error);
                }
                results.Set(name, new CodeResult(Json.Canonical(returned)));
            }
        }
        var unknownOutputs = PyFormat.Sorted(graph.Outputs.Where(name => !results.ContainsKey(name)).Distinct(StringComparer.Ordinal));
        if (unknownOutputs.Count > 0) throw new GraphError($"{graph.Id}: outputs name unknown node(s) {string.Join(", ", unknownOutputs)}");
        var outputs = new OrderedMap<NodeResult>(graph.Outputs.Select(name => new KeyValuePair<string, NodeResult>(name, results[name])));
        return new GraphResult(results.Copy(), outputs, stages);
    }

    /// <summary>Checks the graph, then runs it.</summary>
    internal static async Task<GraphResult> RunAsync(JeffClient jeff, Graph graph, JsonNode? input, JsonObject parameters,
        IReadOnlyDictionary<string, CodeFunction> code, IReadOnlyDictionary<string, Policy> policies, CancellationToken cancellationToken)
    {
        var ownParameters = (JsonObject)Json.Canonical(parameters)!;
        var model = await CheckAsync(jeff, graph, input, ownParameters, code, policies, cancellationToken).ConfigureAwait(false);
        return await RunCheckedAsync(jeff, graph, input, ownParameters, code, model, cancellationToken).ConfigureAwait(false);
    }
}
