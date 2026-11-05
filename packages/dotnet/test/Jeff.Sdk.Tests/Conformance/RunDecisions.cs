using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

/// <summary>The fixtures' scripted engine: the n-th predict answers the n-th recorded predict, and must be asked for
/// exactly its adapter and rows (compared as MessagePack bytes).</summary>
internal sealed class ScriptedEngine : IEngine
{
    private readonly IReadOnlySet<string> served;
    private readonly Queue<JsonObject> script;

    public ScriptedEngine(IEnumerable<string> served, BaseFormat baseFormat, JsonArray predicts)
    {
        this.served = served.ToHashSet(StringComparer.Ordinal);
        BaseFormat = baseFormat;
        script = new Queue<JsonObject>(predicts.Select(predict => predict!.AsObject()));
    }

    public List<string> Mismatches { get; } = new();

    public BaseFormat BaseFormat { get; }

    public int Remaining
    {
        get
        {
            lock (script) return script.Count;
        }
    }

    public Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default) => Task.FromResult(served);

    public async Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default)
    {
        await Task.CompletedTask;
        JsonObject next;
        lock (script)
        {
            if (!script.TryDequeue(out next!)) throw new InvalidOperationException($"The script has no answer left for a predict of {adapterId}");
        }
        string recordedAdapter = next["adapter"]!.GetValue<string>();
        if (recordedAdapter != adapterId) Mismatches.Add($"asked {adapterId}, recorded {recordedAdapter}");
        var packed = Fixtures.ToBase64(MsgPack.Encode(new JsonArray(rows.Select(row => (JsonNode?)row.ToJson()).ToArray())));
        if (packed != next["rows_msgpack"]!.GetValue<string>()) Mismatches.Add($"rows of {adapterId} differ from the recording");
        var probabilities = next["probabilities"]!.AsArray().Select(row => (IReadOnlyList<double>)Fixtures.Probabilities(row)).ToList();
        return new EngineResult(probabilities, (JsonObject)Json.Canonical(next["timing"])!, null);
    }

    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

internal static class RunDecisions
{
    private static Ask Asker(string step, string answer, JsonArray questions, List<Question> asked) => (question, _) =>
    {
        lock (asked)
        {
            asked.Add(question);
            questions.Add(new JsonObject { ["step"] = step, ["question"] = question.ToJson() });
        }
        return Task.FromResult<string?>(answer);
    };

    /// <summary>A fixture policy: a string as a named policy (any other than return and error is refused by the client),
    /// or {escalate: [{function} | {llm} | {human}], budget_ms?}.</summary>
    public static Policy PolicyFrom(JsonNode? spec, JsonArray questions, List<Question> asked)
    {
        if (Json.KindOf(spec) == JsonKind.String) return Policy.Named(spec!.GetValue<string>());
        var body = spec!.AsObject();
        var steps = body["escalate"]!.AsArray().Select(item => item!.AsObject()).Select(step =>
            step.ContainsKey("function") ? (Step)Steps.Function(step["function"]!.GetValue<string>())
            : step.ContainsKey("llm") ? Steps.Llm(Asker("llm", step["llm"]!.GetValue<string>(), questions, asked))
            : step.ContainsKey("human") ? Steps.Human(Asker("human", step["human"]!.GetValue<string>(), questions, asked))
            : throw new InvalidOperationException($"Unknown escalation step {Json.Write(step)}")).ToList();
        TimeSpan? budget = body.ContainsKey("budget_ms") ? TimeSpan.FromMilliseconds(JsonRead.Number(body, "budget_ms", "a fixture policy")) : null;
        return Policy.Escalate(steps, budget);
    }

    public static List<Call> CallsOf(JsonNode? calls) => calls!.AsArray().Select(item => item!.AsObject())
        .Select(call => new Call(call["function"]!.GetValue<string>(), call["input"], call["context"] as JsonObject, call["labels"])).ToList();

    private static JsonArray Array(IEnumerable<Decision> decisions) => new(decisions.Select(decision => (JsonNode?)decision.ToJson()).ToArray());

    private static Task<Decision> Shorthand(JeffClient jeff, string iface, JsonNode? input, ShorthandOptions options) => iface switch
    {
        "classify" => jeff.ClassifyAsync(input, options),
        "route" => jeff.RouteAsync(input, options),
        "rate" => jeff.RateAsync(input, options),
        "detect" => jeff.DetectAsync(input, options),
        "is_supported" => jeff.IsSupportedAsync(input, options),
        "equivalent" => jeff.EquivalentAsync(input, options),
        "requires_escalation" => jeff.RequiresEscalationAsync(input, options),
        _ => throw new InvalidOperationException($"No shorthand for interface {iface}"),
    };

    public static async Task<JsonNode?> RunOpAsync(JeffClient jeff, string op, JsonObject args, JsonArray questions, List<Question> asked)
    {
        var policy = args.ContainsKey("policy") ? PolicyFrom(args["policy"], questions, asked) : Policy.Return;
        var context = args["context"] as JsonObject;
        var labels = args["labels"];
        switch (op)
        {
            case "decide":
                return Array(await jeff.DecideAsync(CallsOf(args["calls"])));
            case "function":
                var bound = await jeff.FunctionAsync(args["ref"]!.GetValue<string>(), new CallOptions { Context = context, Labels = labels, OnLowConfidence = policy });
                return Array(await bound.ManyAsync(args["inputs"]!.AsArray().ToList()));
            case "shorthand":
                return (await Shorthand(jeff, args["interface"]!.GetValue<string>(), args["input"], new ShorthandOptions
                {
                    Function = args["function"] is JsonValue function ? function.GetValue<string>() : null, Context = context, Labels = labels,
                    OnLowConfidence = policy,
                })).ToJson();
            case "dispatch":
                return (await jeff.DispatchAsync(args["input"], new DispatchOptions
                {
                    Interface = args["interface"]!.GetValue<string>(), Context = context, Labels = labels,
                    Router = args["router"] is JsonValue router ? router.GetValue<string>() : null, OnLowConfidence = policy,
                })).ToJson();
            case "graph":
                var codeCalls = new JsonArray();
                var code = new Dictionary<string, CodeFunction>(StringComparer.Ordinal);
                if (args["code"] is JsonObject functions)
                {
                    foreach (var (codeName, value) in functions)
                    {
                        string name = codeName;
                        var answer = Json.Canonical(value);
                        code[name] = (codeCall, _) =>
                        {
                            var nodes = new JsonObject();
                            foreach (var (node, result) in codeCall.Nodes) nodes[node] = result.ToJson();
                            codeCalls.Add(new JsonObject
                            {
                                ["code"] = name, ["input"] = Json.Canonical(codeCall.Input), ["params"] = Json.Canonical(codeCall.Params), ["nodes"] = nodes,
                            });
                            return Task.FromResult(Json.Canonical(answer));
                        };
                    }
                }
                var policies = new Dictionary<string, Policy>(StringComparer.Ordinal);
                if (args["policies"] is JsonObject nodePolicies)
                {
                    foreach (var (nodeName, spec) in nodePolicies) policies[nodeName] = PolicyFrom(spec, questions, asked);
                }
                var graph = Graphs.FromDict(args["graph"], "the conformance graph");
                var graphResult = await jeff.RunGraphAsync(graph, args["input"], args["params"]!.AsObject(), code, policies);
                return new JsonObject { ["graph_result"] = graphResult.ToJson(), ["code_calls"] = codeCalls };
            default:
                throw new InvalidOperationException($"RunOpAsync does not know op {op}");
        }
    }

    public static async Task<(JsonObject Got, ScriptedEngine Engine, JsonArray Questions)> ReplayAsync(JsonObject item)
    {
        var engine = new ScriptedEngine(JsonRead.Strings(item, "installed", "a case"), BaseFormat.FromJson(item["base_format"]!.AsObject(), "base_format"),
            item["predicts"]!.AsArray());
        var questions = new JsonArray();
        var asked = new List<Question>();
        JsonObject got;
        try
        {
            var jeff = await JeffClient.CreateAsync(engine, Fixtures.CatalogueFor(item["catalogue"]!.GetValue<string>()));
            got = new JsonObject
            {
                ["result"] = Fixtures.Normalised(await RunOpAsync(jeff, item["op"]!.GetValue<string>(), item["args"]!.AsObject(), questions, asked)),
            };
        }
        catch (JeffError error)
        {
            got = new JsonObject { ["error"] = Fixtures.WireOf(error) };
        }
        return (got, engine, questions);
    }
}
