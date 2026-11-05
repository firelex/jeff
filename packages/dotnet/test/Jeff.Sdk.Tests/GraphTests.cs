using System.Text.Json.Nodes;
using Jeff.Sdk.Testing;

namespace Jeff.Sdk.Tests;

public class GraphTests
{
    private static JsonObject Doc() => (JsonObject)JsonAssert.Parse("""
        {"graph": "jeff.graphs.t", "version": "1.0.0", "input": "message", "params": ["channel"],
         "nodes": {"kind": {"call": "spam.message_type", "context": {"channel": {"param": "channel"}}},
                   "flag": {"code": "flag", "when": {"node": "kind", "in": ["spam", true]}}},
         "outputs": ["kind", "flag"]}
        """)!;

    [Fact]
    public void AGraphDocumentReadsBackTheSameAfterToDict()
    {
        var graph = Graphs.FromDict(Doc(), "t");
        Assert.Equal(new[] { "kind", "flag" }, graph.Order);
        JsonAssert.Equal(Doc(), Graphs.ToDict(graph));
    }

    [Fact]
    public void ANodeNamingAnUnknownNodeOrParamIsRefused()
    {
        var bad = Doc();
        bad["nodes"]!["flag"]!["after"] = new JsonArray("ghost");
        Assert.Equal("t: node flag refers to unknown node(s) ghost", Assert.Throws<GraphError>(() => Graphs.FromDict(bad, "t")).Message);
        var undeclared = Doc();
        undeclared["params"] = new JsonArray();
        Assert.Equal("t: node kind uses undeclared param(s) channel", Assert.Throws<GraphError>(() => Graphs.FromDict(undeclared, "t")).Message);
    }

    private static JsonObject NumberLike(params string[] names)
    {
        var nodes = new JsonObject();
        foreach (var name in names) nodes[name] = new JsonObject { ["code"] = $"c{name}" };
        return new JsonObject
        {
            ["graph"] = "jeff.graphs.t", ["version"] = "1.0.0", ["input"] = "message", ["params"] = new JsonArray(), ["nodes"] = nodes,
            ["outputs"] = new JsonArray(names[0]),
        };
    }

    [Fact]
    public void NodeNamesThatLookLikeNumbersKeepTheirOrderAndWriteBack()
    {
        var graph = Graphs.FromDict(NumberLike("b", "10", "2"), "t");
        Assert.Equal(new[] { "b", "10", "2" }, graph.Nodes.Keys);
        Assert.Equal(new[] { "b", "10", "2" }, graph.Order);
        Assert.Equal(new[] { "b", "10", "2" }, Graphs.ToDict(graph)["nodes"]!.AsObject().Select(pair => pair.Key));
    }

    [Fact]
    public void AGraphWhoseOrderIsNotTheOrderItsDependenciesGiveCannotBeWritten()
    {
        var graph = Graphs.FromDict(Doc(), "t") with { Order = ["flag", "kind"] };
        Assert.Equal("jeff.graphs.t cannot be written as a graph document that reads back the same: its order flag, kind is not the order its "
            + "dependencies give (kind, flag)", Assert.Throws<GraphError>(() => Graphs.ToDict(graph)).Message);
    }

    [Fact]
    public void ANodeWithAPolicyTheDocumentCannotHoldIsRefusedByToDict()
    {
        var graph = Graphs.FromDict(Doc(), "t");
        var nodes = graph.Nodes.Copy();
        nodes.Set("kind", nodes["kind"] with { OnLowConfidence = "escalate" });
        Assert.Equal("jeff.graphs.t: node kind has on_low_confidence 'escalate'; a graph document holds only 'return' or 'error' (pass other "
            + "policies in policies=)", Assert.Throws<GraphError>(() => Graphs.ToDict(graph with { Nodes = nodes })).Message);
    }

    [Fact]
    public async Task ACodeFunctionReturningNullStoresItAsPythonStoresNone()
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => [], []), Catalogue.Shipped());
        var graph = Graphs.FromDict(JsonAssert.Parse("""
            {"graph": "jeff.graphs.code", "version": "1.0.0", "input": "message", "params": [], "nodes": {"value": {"code": "makeValue"}},
             "outputs": ["value"]}
            """), "t");
        var result = await jeff.RunGraphAsync(graph, "hi", new JsonObject(),
            new Dictionary<string, CodeFunction> { ["makeValue"] = (_, _) => Task.FromResult<JsonNode?>(null) });
        Assert.Equal(new CodeResult(null), result.Outputs["value"]);
        Assert.Equal("{\"nodes\":{\"value\":{\"code\":null}},\"outputs\":{\"value\":{\"code\":null}},\"stages\":1}", Json.Write(result.ToJson()));
    }

    [Fact]
    public async Task CodeAndPoliciesMustNameRealThings()
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => [1, 0, 0], ["jeff.safety.spam"]), Catalogue.Shipped());
        var graph = Graphs.FromDict(Doc(), "t");
        var parameters = new JsonObject { ["channel"] = "sms" };
        Assert.Equal("jeff.graphs.t needs code function(s) flag; pass them in code=",
            (await Assert.ThrowsAsync<GraphError>(() => jeff.RunGraphAsync(graph, "hi", parameters))).Message);
        Assert.Equal("jeff.graphs.t needs param(s) channel", (await Assert.ThrowsAsync<GraphError>(() => jeff.RunGraphAsync(graph, "hi", new JsonObject()))).Message);
        Assert.StartsWith("jeff.graphs.t: policies= names node(s) ghost, which the graph does not have",
            (await Assert.ThrowsAsync<GraphError>(() => jeff.RunGraphAsync(graph, "hi", parameters, null,
                new Dictionary<string, Policy> { ["ghost"] = Policy.Error }))).Message);
    }

    // Every expected message below was printed by the Python SDK (jeff.graph) for the same document.

    private static JsonObject CodeGraph(JsonObject nodes, params string[] outputs) => new()
    {
        ["graph"] = "jeff.graphs.t", ["version"] = "1.0.0", ["input"] = "message", ["params"] = new JsonArray(), ["nodes"] = nodes,
        ["outputs"] = Json.Strings(outputs.Length > 0 ? outputs : [nodes.First().Key]),
    };

    private static Task<JeffClient> CodeOnlyClient() => JeffClient.CreateAsync(new FakeEngine((_, _) => [], []), Catalogue.Shipped());

    private static Dictionary<string, CodeFunction> Returning(params (string Name, JsonNode? Value)[] functions) =>
        functions.ToDictionary(function => function.Name, function => (CodeFunction)((_, _) => Task.FromResult(Json.Canonical(function.Value))),
            StringComparer.Ordinal);

    [Fact]
    public void ACycleIsNamedAlongThePathPythonWalks()
    {
        var numberLike = (JsonObject)JsonAssert.Parse("""
            {"b": {"code": "b", "after": ["10"]}, "10": {"code": "x", "after": ["2"]}, "2": {"code": "y", "after": ["b"]}}
            """)!;
        Assert.Equal("t: the nodes form a cycle: b -> 10 -> 2 -> b", Assert.Throws<GraphError>(() => Graphs.FromDict(CodeGraph(numberLike), "t")).Message);
        var late = (JsonObject)JsonAssert.Parse("""
            {"z": {"code": "z"}, "a": {"code": "a", "after": ["z", "c"]}, "b": {"code": "b", "after": ["a"]}, "c": {"code": "c", "after": ["b"]}}
            """)!;
        Assert.Equal("t: the nodes form a cycle: a -> c -> b -> a", Assert.Throws<GraphError>(() => Graphs.FromDict(CodeGraph(late), "t")).Message);
        var throughCondition = (JsonObject)JsonAssert.Parse("""
            {"a": {"code": "a", "when": {"not": {"node": "a", "is": true}}}}
            """)!;
        Assert.Equal("t: the nodes form a cycle: a -> a", Assert.Throws<GraphError>(() => Graphs.FromDict(CodeGraph(throughCondition), "t")).Message);
    }

    [Fact]
    public void NumberLikeNamesRunInTheOrderTheirDependenciesGiveSortedAsStrings()
    {
        var graph = Graphs.FromDict(CodeGraph((JsonObject)JsonAssert.Parse("""
            {"b": {"code": "b", "after": ["10", "2"]}, "10": {"code": "x", "after": ["1a"]}, "2": {"code": "y"}, "1a": {"code": "z"}}
            """)!, "b", "10"), "t");
        Assert.Equal(new[] { "1a", "10", "2", "b" }, graph.Order);
        Assert.Equal(new[] { "b", "10", "2", "1a" }, graph.Nodes.Keys);
    }

    [Fact]
    public void AConditionTheSchemaRefusesListsEveryAlternativeInPythonsOrder()
    {
        var bad = Doc();
        bad["nodes"]!["flag"]!["when"] = new JsonObject { ["node"] = "kind" };
        Assert.Equal("t does not match the graph schema:\n  at /nodes/flag/when: {'node': 'kind'} is not valid under any of the given schemas\n"
            + "    - Additional properties are not allowed ('node' was unexpected)\n    - 'all' is a required property\n"
            + "    - Additional properties are not allowed ('node' was unexpected)\n    - 'any' is a required property\n"
            + "    - Additional properties are not allowed ('node' was unexpected)\n    - 'not' is a required property\n"
            + "    - 'is' is a required property\n    - 'in' is a required property\n    - 'abstained' is a required property\n"
            + "    - 'confidence_below' is a required property", Assert.Throws<GraphError>(() => Graphs.FromDict(bad, "t")).Message);
        var both = Doc();
        both["nodes"]!["flag"] = new JsonObject { ["call"] = "x", ["code"] = "y" };
        Assert.StartsWith("t does not match the graph schema:\n  at /nodes/flag: {'call': 'x', 'code': 'y'} is valid under each of",
            Assert.Throws<GraphError>(() => Graphs.FromDict(both, "t")).Message);
    }

    [Fact]
    public void ADocumentTooDeepToReadIsAGraphErrorNotAStackOverflow()
    {
        JsonObject condition = new() { ["node"] = "kind", ["is"] = "spam" };
        for (int level = 0; level < 100_000; level++) condition = new JsonObject { ["not"] = condition };
        var deep = Doc();
        deep["nodes"]!["flag"]!["when"] = condition;
        Assert.Equal($"t is nested more than {Graphs.MaxDepth} levels deep (objects and lists inside one another); a graph document is never that deep",
            Assert.Throws<GraphError>(() => Graphs.FromDict(deep, "t")).Message);
        // a hand-built condition that deep is refused when it is written or read
        var graph = Graphs.FromDict(Doc(), "t");
        var nodes = graph.Nodes.Copy();
        nodes.Set("flag", nodes["flag"] with { When = condition });
        Assert.StartsWith("jeff.graphs.t: the condition of node flag is nested more than",
            Assert.Throws<GraphError>(() => Graphs.ToDict(graph with { Nodes = nodes })).Message);
        // one Python reads (160 nested 'not's) is read here too
        JsonObject readable = new() { ["node"] = "kind", ["is"] = "spam" };
        for (int level = 0; level < 160; level++) readable = new JsonObject { ["not"] = readable };
        var ok = Doc();
        ok["nodes"]!["flag"]!["when"] = readable;
        Assert.Equal(new[] { "kind", "flag" }, Graphs.FromDict(ok, "t").Order);
    }

    private static Exception? OnSmallStack(Action action)
    {
        Exception? caught = null;
        var thread = new Thread(() =>
        {
            try
            {
                action();
            }
            catch (Exception error)
            {
                caught = error;
            }
        }, 256 * 1024);
        thread.Start();
        thread.Join();
        return caught;
    }

    [Fact]
    public void ADocumentInsideTheDepthLimitIsAGraphErrorOnAThreadWithASmallStack()
    {
        JsonObject condition = new() { ["node"] = "kind", ["is"] = "spam" };
        for (int level = 0; level < 250; level++) condition = new JsonObject { ["not"] = condition };
        var deep = Doc();
        deep["nodes"]!["flag"]!["when"] = condition;
        var reading = Assert.IsType<GraphError>(OnSmallStack(() => Graphs.FromDict(deep, "t")));
        Assert.Contains(StackGuard.TooDeep, reading.Message);
        Assert.NotNull(reading.InnerException);
        var graph = Graphs.FromDict(Doc(), "t");
        var nodes = graph.Nodes.Copy();
        nodes.Set("flag", nodes["flag"] with { When = condition });
        var writing = Assert.IsType<GraphError>(OnSmallStack(() => Graphs.ToDict(graph with { Nodes = nodes })));
        Assert.Contains(StackGuard.TooDeep, writing.Message);
    }

    [Fact]
    public void AHandBuiltConditionWithNoNodeToReadIsAGraphErrorWhenItsDependenciesAreAsked()
    {
        var node = Graphs.FromDict(Doc(), "t").Nodes["flag"] with { When = (JsonObject)JsonAssert.Parse("""{"any": [{"is": 1}]}""")! };
        Assert.Equal("The condition of node flag has a part graph.schema.json does not allow: {'is': 1}",
            Assert.Throws<GraphError>(() => Graphs.Dependencies(node)).Message);
        Assert.Equal("The condition of node flag has a part graph.schema.json does not allow: {'not': 'x'}",
            Assert.Throws<GraphError>(() => Graphs.Dependencies(node with { When = new JsonObject { ["not"] = "x" } })).Message);
    }

    [Fact]
    public async Task AHandBuiltConditionTooDeepToHoldIsAGraphError()
    {
        var jeff = await CodeOnlyClient();
        JsonObject condition = new() { ["node"] = "a", ["is"] = 1 };
        for (int level = 0; level < 100_000; level++) condition = new JsonObject { ["not"] = condition };
        var graph = Graphs.FromDict(CodeGraph((JsonObject)JsonAssert.Parse("""{"a": {"code": "ca"}, "b": {"code": "cb", "when": {"node": "a", "is": 1}}}""")!), "t");
        var nodes = graph.Nodes.Copy();
        nodes.Set("b", nodes["b"] with { When = condition });
        var error = await Assert.ThrowsAsync<GraphError>(() => jeff.RunGraphAsync(graph with { Nodes = nodes }, "hi", new JsonObject(),
            Returning(("ca", 1), ("cb", 2))));
        Assert.Equal("jeff.graphs.t: the condition of node b is nested too deeply for the stack this thread has left", error.Message);
    }

    [Fact]
    public async Task ALongChainIsOrderedAndRunWithoutRecursion()
    {
        const int length = 20_000;
        var nodes = new JsonObject();
        for (int index = 0; index < length; index++)
        {
            nodes[$"n{index}"] = index < length - 1 ? new JsonObject { ["code"] = "c", ["after"] = new JsonArray($"n{index + 1}") } : new JsonObject { ["code"] = "c" };
        }
        var graph = Graphs.FromDict(CodeGraph(nodes, "n0"), "t");
        Assert.Equal($"n{length - 1}", graph.Order[0]);
        Assert.Equal("n0", graph.Order[^1]);
        var cycle = CodeGraph((JsonObject)nodes.DeepClone(), "n0");
        cycle["nodes"]![$"n{length - 1}"]!["after"] = new JsonArray("n0");
        var message = Assert.Throws<GraphError>(() => Graphs.FromDict(cycle, "t")).Message;
        Assert.StartsWith("t: the nodes form a cycle: n0 -> n1 -> n2", message);
        Assert.EndsWith($"n{length - 1} -> n0", message);
        var shortChain = new JsonObject();
        for (int index = 0; index < 300; index++)
        {
            shortChain[$"n{index}"] = index < 299 ? new JsonObject { ["code"] = "c", ["after"] = new JsonArray($"n{index + 1}") } : new JsonObject { ["code"] = "c" };
        }
        var result = await (await CodeOnlyClient()).RunGraphAsync(Graphs.FromDict(CodeGraph(shortChain, "n0"), "t"), "hi", new JsonObject(), Returning(("c", 1)));
        Assert.Equal(300, result.Stages);
    }

    [Fact]
    public async Task ConditionsCompareAsPythonCompares()
    {
        var graph = Graphs.FromDict(CodeGraph((JsonObject)JsonAssert.Parse("""
            {"a": {"code": "ca"}, "b": {"code": "cb", "when": {"node": "a", "is": true}}, "c": {"code": "cc", "when": {"node": "a", "in": [true, "x"]}},
             "d": {"code": "cd", "when": {"node": "a", "is": 2}}}
            """)!, "a", "b", "c", "d"), "t");
        var result = await (await CodeOnlyClient()).RunGraphAsync(graph, "hi", new JsonObject(),
            Returning(("ca", 1.0), ("cb", "yes"), ("cc", JsonAssert.Parse("""{"k": [1, 2]}""")), ("cd", false)));
        JsonAssert.Equal(JsonAssert.Parse("""
            {"nodes": {"a": {"code": 1.0}, "d": {"skipped": true, "reason": "condition not met"}, "b": {"code": "yes"}, "c": {"code": {"k": [1, 2]}}},
             "outputs": {"a": {"code": 1.0}, "b": {"code": "yes"}, "c": {"code": {"k": [1, 2]}}, "d": {"skipped": true, "reason": "condition not met"}},
             "stages": 2}
            """), result.ToJson());
        Assert.Equal(result.ToJson().ToJsonString(), GraphResult.FromJson(result.ToJson()).ToJson().ToJsonString());
    }

    [Fact]
    public async Task AModelConditionOnACodeNodeIsRefusedInPythonsWords()
    {
        var graph = Graphs.FromDict(CodeGraph((JsonObject)JsonAssert.Parse("""
            {"a": {"code": "ca"}, "b": {"code": "cb", "when": {"node": "a", "abstained": true}}}
            """)!), "t");
        var jeff = await CodeOnlyClient();
        Assert.Equal("Condition on a needs a model decision; a is a code node", (await Assert.ThrowsAsync<GraphError>(() =>
            jeff.RunGraphAsync(graph, "hi", new JsonObject(), Returning(("ca", 1), ("cb", 2))))).Message);
    }

    [Fact]
    public async Task AFailingCodeFunctionIsAGraphErrorWithTheFailureAsItsCause()
    {
        var jeff = await CodeOnlyClient();
        var graph = Graphs.FromDict(CodeGraph((JsonObject)JsonAssert.Parse("""{"value": {"code": "makeValue"}}""")!), "t");
        var failure = new InvalidOperationException("no ticket system");
        var error = await Assert.ThrowsAsync<GraphError>(() => jeff.RunGraphAsync(graph, "hi", new JsonObject(),
            new Dictionary<string, CodeFunction> { ["makeValue"] = (_, _) => throw failure }));
        Assert.Equal("jeff.graphs.t: code function makeValue (node value) failed: no ticket system", error.Message);
        Assert.Same(failure, error.InnerException);
        // a cancellation is let through as it is
        await Assert.ThrowsAsync<OperationCanceledException>(() => jeff.RunGraphAsync(graph, "hi", new JsonObject(),
            new Dictionary<string, CodeFunction> { ["makeValue"] = (_, _) => throw new OperationCanceledException() }));
    }

    [Fact]
    public async Task AHandBuiltGraphPythonWouldStopOnWithAKeyErrorGetsANamedGraphError()
    {
        var jeff = await CodeOnlyClient();
        var graph = Graphs.FromDict(CodeGraph((JsonObject)JsonAssert.Parse("""{"a": {"code": "ca"}}""")!), "t");
        Assert.Equal("jeff.graphs.t: its order names node ghost, which the graph does not have", (await Assert.ThrowsAsync<GraphError>(() =>
            jeff.RunGraphAsync(graph with { Order = ["a", "ghost"] }, "hi", new JsonObject(), Returning(("ca", 1))))).Message);
        Assert.Equal("jeff.graphs.t: outputs name unknown node(s) ghost", (await Assert.ThrowsAsync<GraphError>(() =>
            jeff.RunGraphAsync(graph with { Outputs = ["a", "ghost"] }, "hi", new JsonObject(), Returning(("ca", 1))))).Message);
        var waiting = graph with { Nodes = new OrderedMap<GraphNode>([new("a", graph.Nodes["a"] with { After = ["a"] })]) };
        Assert.Equal("jeff.graphs.t makes no progress: node(s) a wait on each other or on nodes the graph does not have",
            (await Assert.ThrowsAsync<GraphError>(() => jeff.RunGraphAsync(waiting, "hi", new JsonObject(), Returning(("ca", 1))))).Message);
        // Python: `1 in "x"`, a TypeError
        var two = Graphs.FromDict(CodeGraph((JsonObject)JsonAssert.Parse("""{"a": {"code": "ca"}, "b": {"code": "cb", "when": {"node": "a", "is": 1}}}""")!), "t");
        var nodes = two.Nodes.Copy();
        nodes.Set("b", nodes["b"] with { When = new JsonObject { ["node"] = "a", ["in"] = "x" } });
        Assert.Equal("jeff.graphs.t: the condition of node b has a part graph.schema.json does not allow: {'node': 'a', 'in': 'x'}",
            (await Assert.ThrowsAsync<GraphError>(() => jeff.RunGraphAsync(two with { Nodes = nodes }, "hi", new JsonObject(),
                Returning(("ca", 1), ("cb", 2))))).Message);
    }

    [Fact]
    public async Task AGraphFileIsReadAsPyYamlReadsIt()
    {
        string folder = Path.Combine(Path.GetTempPath(), $"jeff-graph-{Guid.NewGuid():N}");
        Directory.CreateDirectory(folder);
        try
        {
            string path = Path.Combine(folder, "g.yaml");
            File.WriteAllText(path, "graph: jeff.graphs.file\nversion: 1.0.0\ninput: message\nparams: []\nnodes:\n  '10': {code: ten}\n  '2': {code: two, after: ['10']}\n"
                + "outputs: ['2']\n");
            var graph = Graphs.Load(path);
            Assert.Equal(new[] { "10", "2" }, graph.Nodes.Keys);
            var result = await (await CodeOnlyClient()).RunGraphAsync(path, "hi", new JsonObject(), Returning(("ten", 10), ("two", 2)));
            Assert.Equal("{\"nodes\":{\"10\":{\"code\":10},\"2\":{\"code\":2}},\"outputs\":{\"2\":{\"code\":2}},\"stages\":2}", Json.Write(result.ToJson()));
        }
        finally
        {
            Directory.Delete(folder, true);
        }
    }
}
