using System.Text.Json.Nodes;
using Jeff.Sdk.Testing;

namespace Jeff.Sdk.Tests;

public class TestingTests
{
    // The expected values below were printed by the Python SDK (jeff.testing.fake_tokens, jeff.evaluate.golden_problems);
    // fake_tokens of SpamRow is 22 (json.dumps splits into 22 space-separated words).
    private static readonly Row SpamRow = new(
        JsonAssert.Parse("{\"channel\": \"sms\", \"message\": \"Win  a prize\\tnow\\n café x\"}")!,
        new RowQuestion(QuestionType.Noul, "Is this spam?", JsonAssert.Parse("{\"false\": \"Not spam.\", \"true\": \"Spam.\"}")));

    [Fact]
    public async Task FakeEngineAnswersFromTheFunctionRecordsItsCallsAndRefusesAnAdapterItDoesNotServe()
    {
        var engine = new FakeEngine((_, _) => [0.25, 0.75], ["jeff.safety.spam"]);
        var result = await engine.PredictAsync("jeff.safety.spam", [SpamRow]);
        Assert.Equal(new[] { 0.25, 0.75 }, result.Probabilities[0]);
        JsonAssert.Equal(JsonAssert.Parse("{\"path\": \"fake\", \"model_ms\": 0.0, \"format\": \"bf16\"}"), result.Timing);
        Assert.Equal(new[] { 22L }, result.InputTokens!);
        Assert.Single(engine.Calls);
        var refused = await Assert.ThrowsAsync<NotInstalled>(() => engine.PredictAsync("jeff.support.triage", [SpamRow]));
        Assert.Equal(Errors.NotInstalledError("jeff.support.triage", "FakeEngine has not loaded it").Message, refused.Message);
        Assert.Equal(22L, Golden.FakeTokens(SpamRow));
    }

    [Fact]
    public void PythonDumpsWritesWhatJsonDumpsWrites() =>
        // json.dumps(["\x7f é 😀\t", float("nan"), float("inf"), -0.0, 10**20, {"a": None}]) in Python
        Assert.Equal("[\"\\u007f \\u00e9 \\ud83d\\ude00\\t\", NaN, Infinity, -0.0, 100000000000000000000, {\"a\": null}]",
            Golden.PythonDumps(new JsonArray("\u007f é 😀\t", double.NaN, double.PositiveInfinity, -0.0,
                Json.Whole(System.Numerics.BigInteger.Pow(10, 20)), new JsonObject { ["a"] = null })));

    [Fact]
    public void FakeEngineRefusesAMalformedBaseFormat() =>
        Assert.Throws<EngineError>(() => new FakeEngine((_, _) => [], [], new BaseFormat("int4-g64")));

    private sealed class CountingEngine : IEngine
    {
        public readonly List<bool> Asked = new();
        public BaseFormat BaseFormat { get; } = new(Engines.Bf16);
        public Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default)
        {
            Asked.Add(refresh);
            return Task.FromResult<IReadOnlySet<string>>(refresh ? new HashSet<string> { "a" } : new HashSet<string>());
        }
        public Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default) =>
            throw new InvalidOperationException("not called");
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    [Fact]
    public async Task ServesAsksACachedListOnceMoreWithRefresh()
    {
        var engine = new CountingEngine();
        Assert.True(await Engines.ServesAsync(engine, "a"));
        Assert.Equal(new[] { false, true }, engine.Asked);
    }

    private static Decision Sure() => new()
    {
        Value = JsonValue.Create(true), Confidence = 0.6, Abstained = false, Calibrated = true, Interface = "i", Function = "f", Version = "1",
        Probabilities = new OrderedMap<double>(), Score = null, Route = [], DecidedBy = "x", Escalations = [], Timing = new JsonObject(),
        EvidenceRefs = [],
    };

    private static GoldenCase Case(string expected, double tolerance) => new("n", new Call("f", "x"), (JsonObject)JsonAssert.Parse(expected)!,
        JsonValue.Create(tolerance));

    [Fact]
    public void GoldenProblemsWordEachDifferenceAsPythonDoes()
    {
        Assert.Equal(new[]
        {
            "value True, expected 1", "confidence 0.6, expected 0.9 +/- 0.05",
            "abstained False, expected True (the recorded confidence is more than 0.05 from the threshold 0.7)",
        }, Golden.Problems(Case("{\"value\": 1, \"abstained\": true, \"confidence\": 0.9}", 0.05), Sure(), 0.7));
        Assert.Equal(new[] { "abstained False, expected True (the recorded confidence is more than 0.05 from the threshold None)" },
            Golden.Problems(Case("{\"value\": true, \"abstained\": true, \"confidence\": 0.62}", 0.05), Sure(), null));
        Assert.Empty(Golden.Problems(Case("{\"value\": true, \"abstained\": true, \"confidence\": 0.62}", 0.05), Sure(), 0.65));
    }
}
