using System.Text.Json.Nodes;
using Jeff.Sdk.Testing;
using Jeff.Sdk.Tests.Conformance;

namespace Jeff.Sdk.Tests;

public class ClientTests
{
    private static JsonObject Ctx() => new() { ["company"] = "A shop.", ["channel"] = "email" };
    private static readonly double[] Unsure = [0.3, 0.2, 0.2, 0.2, 0.1];

    private static Ask Answering(string answer, List<Question> asked) => (question, _) =>
    {
        asked.Add(question);
        return Task.FromResult<string?>(answer);
    };

    [Fact]
    public async Task ARemoteStepAsksAnotherClientWithTheSameCall()
    {
        var catalogue = Catalogue.Shipped();
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => Unsure, ["jeff.support.triage"]), catalogue);
        var other = await JeffClient.CreateAsync(new FakeEngine((_, _) => [0, 0, 0, 0.95, 0.05], ["jeff.support.triage"]), catalogue);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx(), OnLowConfidence = Policy.Escalate(Steps.Remote(other)) });
        var decision = await bound.CallAsync("Hm");
        Assert.Equal(3L, decision.Value.GetValue<long>());
        Assert.Equal(new[] { $"jeff.support.triage.urgency@{catalogue.Adapter("jeff.support.triage").Version}" }, decision.Escalations.Select(item => item.DecidedBy));
    }

    [Fact]
    public async Task AnEscalationPastItsBudgetIsRefused()
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => Unsure, ["jeff.support.triage"]), Catalogue.Shipped());
        var slow = Steps.Llm(async (_, cancellationToken) =>
        {
            await Task.Delay(30, cancellationToken);
            return "2";
        });
        var bound = await jeff.FunctionAsync("triage.urgency",
            new CallOptions { Context = Ctx(), OnLowConfidence = Policy.Escalate([slow], TimeSpan.FromMilliseconds(5)) });
        var error = await Assert.ThrowsAsync<EscalationBudgetExceeded>(() => bound.CallAsync("Hm"));
        Assert.Equal("Escalation of jeff.support.triage.urgency took longer than 5 ms", error.Message);
    }

    [Fact]
    public void AContextFieldGivenByItsExactKeyIsRefusedBeforeRouting()
    {
        var exact = new OrderedMap<string>();
        exact.Set("counterparty name", "counterparty_name");
        var error = Assert.Throws<UnknownContext>(() => Routing.RefuseExactKeys(exact, "jeff.core.router", new JsonObject { ["counterparty name"] = "Acme" }));
        Assert.Equal("context is given by code name; pass 'counterparty name' as counterparty_name (fields of adapters reached from router "
            + "jeff.core.router)", error.Message);
        Assert.Empty(Routing.ExactKeysOf(Catalogue.Shipped(), "jeff.safety.spam"));
    }

    private static JsonObject SpacedContext() => new() { ["ten"] = "x", ["deal_id"] = "D-7", ["counterparty_name"] = "Acme Ltd" };

    [Fact]
    public async Task AQuestionsStateEntriesKeepTheOrderTheModelReadsNumberLikeKeysIncluded()
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => [0.5, 0.5], ["jeff.safety.spam"]), Fixtures.CatalogueFor("spaced"));
        var asked = new List<Question>();
        var bound = await jeff.FunctionAsync("spam.is_spam",
            new CallOptions { Context = SpacedContext(), OnLowConfidence = Policy.Escalate(Steps.Human(Answering("false", asked))) });
        Assert.Equal("human", (await bound.CallAsync("Please sign")).DecidedBy);
        var expected = new[] { "counterparty name", "Deal ID", "10", "message" };
        Assert.Equal(expected, asked[0].StateEntries!.Select(entry => entry.Key));
        Assert.Equal(expected, asked[0].State.AsObject().Select(pair => pair.Key));
    }

    [Fact]
    public async Task AQuestionsOptionEntriesKeepOptionOrderNumberLikeKeysIncluded()
    {
        var raw = (JsonObject)Json.Canonical(Fixtures.File("catalogues.json")["variants"]!["spaced"]![0])!;
        raw["functions"]!["message_type"]!["labels"]!["fixed"] = new JsonObject { ["other"] = "None of these", ["10"] = "Ten" };
        raw["functions"]!["message_type"]!["abstain_labels"] = new JsonArray("other");
        var catalogue = Catalogue.Shipped().Replacing([Manifest.FromDict(raw, "test/number-like-labels/jeff.safety.spam")]);
        Assert.Equal(new[] { "other", "10" }, catalogue.Function("spam.message_type").Labels.Fixed.Keys);
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => [0.9, 0.1], ["jeff.safety.spam"]), catalogue);
        var asked = new List<Question>();
        var bound = await jeff.FunctionAsync("spam.message_type",
            new CallOptions { Context = SpacedContext(), OnLowConfidence = Policy.Escalate(Steps.Human(Answering("10", asked))) });
        Assert.Equal("10", (await bound.CallAsync("Please sign")).Value.GetValue<string>());
        Assert.Equal(new[] { "other", "10" }, asked[0].OptionEntries.Select(entry => entry.Key));
        Assert.Equal("{\"other\":\"None of these\",\"10\":\"Ten\"}", Json.Write(asked[0].Options));
    }

    [Fact]
    public async Task AnUnknownPolicyNameIsRefusedAsPythonRefusesIt()
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => Unsure, ["jeff.support.triage"]), Catalogue.Shipped());
        var error = await Assert.ThrowsAsync<InvalidPolicy>(() =>
            jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx(), OnLowConfidence = Policy.Named("maybe") }));
        Assert.Equal("Unknown on_low_confidence policy 'maybe'; use 'return', 'error' or escalate(...)", error.Message);
        Assert.Equal("escalate() needs at least one step", Assert.Throws<InvalidPolicy>(() => Policy.Escalate()).Message);
    }

    private sealed class DownEngine : IEngine
    {
        public bool Down = true;
        public BaseFormat BaseFormat { get; } = new(Engines.Bf16);
        public Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default) =>
            Task.FromResult<IReadOnlySet<string>>(new HashSet<string> { "jeff.support.triage", "jeff.safety.spam" });
        public async Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default)
        {
            await Task.CompletedTask;
            if (Down) throw new EngineError($"{adapterId} is down");
            return new EngineResult(rows.Select(_ => (IReadOnlyList<double>)[0.5, 0.5, 0, 0, 0]).ToList(), new JsonObject(), null);
        }
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    [Fact]
    public async Task APredictThatFailsIsReportedBeforeALaterAdaptersRowsFailToBuildAsPythonMeetsThem()
    {
        // the "text" catalogue's spam functions take plain text, so a number as input fails when its row is built
        var engine = new DownEngine();
        var jeff = await JeffClient.CreateAsync(engine, Fixtures.CatalogueFor("text"));
        var urgency = new Call("triage.urgency", "Hm", Ctx());
        var bound = new List<BoundCall>
        {
            new(urgency, await jeff.BindCallAsync(urgency)),
            new(new Call("spam.is_spam", 5), await jeff.BindCallAsync(new Call("spam.is_spam", "x"))),
        };
        Assert.Equal("jeff.support.triage is down", (await Assert.ThrowsAsync<EngineError>(() => jeff.DecideBoundAsync(bound))).Message);
        engine.Down = false;
        await Assert.ThrowsAsync<InvalidRequest>(() => jeff.DecideBoundAsync(bound));
    }

    [Fact]
    public async Task OneContextObjectServesManyCallsAndStaysTheCallers()
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => [0.1, 0.1, 0.6, 0.1, 0.1], ["jeff.support.triage"]), Catalogue.Shipped());
        var context = Ctx();
        var mine = new JsonObject { ["context"] = context };
        var calls = new[] { new Call("triage.urgency", "a", context), new Call("triage.urgency", "b", context) };
        var decisions = await jeff.DecideAsync(calls);
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = context });
        await bound.ManyAsync([JsonValue.Create("c"), JsonValue.Create("d")]);
        Assert.Equal(2, decisions.Count);
        Assert.Same(mine, context.Parent);
        Assert.Equal("A shop.", context["company"]!.GetValue<string>());
    }

    private static async Task<EscalationFailed> FailedEscalation(Step step)
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => Unsure, ["jeff.support.triage"]), Catalogue.Shipped());
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx(), OnLowConfidence = Policy.Escalate(step) });
        return await Assert.ThrowsAsync<EscalationFailed>(() => bound.CallAsync("Hm"));
    }

    // Python: "Escalation step llm failed: ValueError: the model is down"; .NET names its own exception class
    [Fact]
    public async Task AStepThatThrowsIsAnEscalationFailedNamingTheExceptionsClassAsPythonDoes()
    {
        var thrown = new InvalidOperationException("the model is down");
        var error = await FailedEscalation(Steps.Llm((_, _) => throw thrown));
        Assert.Equal("Escalation step llm failed: InvalidOperationException: the model is down", error.Message);
        Assert.Equal("llm", error.Step);
        Assert.Same(thrown, error.InnerException);
        var jeffError = await FailedEscalation(Steps.Human((_, _) => throw Errors.NotInstalledError("jeff.x.y.z", "a test")));
        Assert.Equal("Escalation step human failed: NotInstalled: jeff.x.y.z is not installed: a test. Install it with: jeff pull jeff.x.y", jeffError.Message);
    }

    [Fact]
    public async Task AnAskAnsweringNothingOrAFailingRemoteStepIsAnEscalationFailedAsInPython()
    {
        Assert.Equal("Escalation step llm failed: answered None, which is not one of ['0', '1', '2', '3', '4']",
            (await FailedEscalation(Steps.Llm((_, _) => Task.FromResult<string?>(null)))).Message);
        Assert.Equal("Escalation step remote failed: InvalidOperationException: remote is down",
            (await FailedEscalation(Steps.Remote(new Answering_((_, _) => throw new InvalidOperationException("remote is down"))))).Message);
        // Python fails with IndexError here; a decider must answer exactly one decision for the one call it is given
        Assert.Equal("Escalation step remote failed: ContractError: The remote step's client answered 0 decisions for 1 call",
            (await FailedEscalation(Steps.Remote(new Answering_((_, _) => Task.FromResult<IReadOnlyList<Decision>>([]))))).Message);
    }

    private sealed class Answering_(Func<IReadOnlyList<Call>, CancellationToken, Task<IReadOnlyList<Decision>>> decide) : IDecider
    {
        public Task<IReadOnlyList<Decision>> DecideAsync(IReadOnlyList<Call> calls, CancellationToken cancellationToken = default) => decide(calls, cancellationToken);
    }

    [Fact]
    public async Task ACancelledEscalationIsCancelledNotAFailedStep()
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => Unsure, ["jeff.support.triage"]), Catalogue.Shipped());
        using var cancel = new CancellationTokenSource();
        var waiting = Steps.Human(async (_, token) =>
        {
            cancel.Cancel();
            await Task.Delay(Timeout.Infinite, token);
            return "1";
        });
        var bound = await jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx(), OnLowConfidence = Policy.Escalate(waiting) });
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => bound.CallAsync("Hm", cancel.Token));
    }

    [Fact]
    public async Task ARouterThatRoutesToTheBaseIsRefusedWhenTheClientIsMadeAsInPython()
    {
        var raw = (JsonObject)Json.Canonical(Fixtures.File("catalogues.json")["variants"]!["routed"]![0])!;
        raw["routes_to"]!.AsArray().Add("base");
        raw["functions"]!["domain"]!["labels"]!["fixed"]!["base"] = "The base model";
        var catalogue = Fixtures.CatalogueFor("routed").Replacing([Manifest.FromDict(raw, "x/jeff.core.router")]);
        var installed = catalogue.Manifests().Select(manifest => manifest.Id).Append("base").ToList();
        var error = await Assert.ThrowsAsync<ManifestError>(() => JeffClient.CreateAsync(new FakeEngine((_, _) => [1.0], installed), catalogue));
        Assert.Equal("Router jeff.core.router routes to base: No adapter 'base' in the catalogue", error.Message);
    }

    [Fact]
    public async Task AnUnknownStepInAPolicyBuiltDirectlyIsRefusedBeforeDeciding()
    {
        var jeff = await JeffClient.CreateAsync(new FakeEngine((_, _) => Unsure, ["jeff.support.triage"]), Catalogue.Shipped());
        var error = await Assert.ThrowsAsync<InvalidPolicy>(() =>
            jeff.FunctionAsync("triage.urgency", new CallOptions { Context = Ctx(), OnLowConfidence = new EscalatePolicy([null!], null) }));
        Assert.StartsWith("escalate() got None, which is not a step", error.Message);
        Assert.Throws<InvalidPolicy>(() => Policy.Escalate([null!], null));
    }

    private sealed class FailingEngine : IEngine
    {
        public BaseFormat BaseFormat { get; } = new(Engines.Bf16);
        public Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default) =>
            Task.FromResult<IReadOnlySet<string>>(new HashSet<string> { "jeff.support.triage", "jeff.safety.spam" });
        public async Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default)
        {
            // the later adapter fails first, so the order reported is the adapters' order, not the order of failing
            await Task.Delay(adapterId == "jeff.support.triage" ? 30 : 0, cancellationToken);
            throw new EngineError($"unobserved-check {adapterId} failed");
        }
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    [Fact]
    public async Task WhenEveryPredictFailsTheFirstAdaptersFailureIsReportedAndNoneIsLeftUnobserved()
    {
        var unobserved = new List<string>();
        EventHandler<UnobservedTaskExceptionEventArgs> watch = (_, args) =>
        {
            if (args.Exception.InnerExceptions.Any(error => error.Message.StartsWith("unobserved-check", StringComparison.Ordinal)))
            {
                lock (unobserved) unobserved.Add(args.Exception.Message);
            }
        };
        TaskScheduler.UnobservedTaskException += watch;
        try
        {
            var jeff = await JeffClient.CreateAsync(new FailingEngine(), Catalogue.Shipped());
            var calls = new[] { new Call("triage.urgency", "a", Ctx()), new Call("spam.is_spam", "b", new JsonObject { ["channel"] = "sms" }) };
            Assert.Equal("unobserved-check jeff.support.triage failed", (await Assert.ThrowsAsync<EngineError>(() => jeff.DecideAsync(calls))).Message);
            GC.Collect();
            GC.WaitForPendingFinalizers();
            GC.Collect();
            Assert.Empty(unobserved);
        }
        finally
        {
            TaskScheduler.UnobservedTaskException -= watch;
        }
    }

    [Fact]
    public void TimeoutsArePositiveAndAtMostTheLongestDotNetsTimersKeep()
    {
        Timeouts.Check(TimeSpan.FromMilliseconds(1));
        Timeouts.Check(Timeouts.Longest);
        Assert.Equal("Timeout must be a positive length of time; got 0 ms", Assert.Throws<InvalidRequest>(() => Timeouts.Check(TimeSpan.Zero)).Message);
        Assert.Equal("Timeout must be at most 2147483647 milliseconds (about 24.8 days), the longest .NET's timers keep; got 2147483648 ms",
            Assert.Throws<InvalidRequest>(() => Timeouts.Check(Timeouts.Longest + TimeSpan.FromMilliseconds(1))).Message);
    }

    // Python: an llm callable raising TimeoutError gives "Escalation step llm failed: TimeoutError: ..."
    [Fact]
    public async Task AStepsOwnTimeoutIsAFailedStepWhenTheCallerDidNotCancel()
    {
        var error = await FailedEscalation(Steps.Llm((_, _) => throw new TaskCanceledException("The request timed out")));
        Assert.Equal("Escalation step llm failed: TaskCanceledException: The request timed out", error.Message);
        var remote = await FailedEscalation(Steps.Remote(new Answering_((_, _) => throw new OperationCanceledException("The server took too long"))));
        Assert.Equal("Escalation step remote failed: OperationCanceledException: The server took too long", remote.Message);
    }

    private sealed class ThrowingEngine : IEngine
    {
        public int Settled;
        public BaseFormat BaseFormat { get; } = new(Engines.Bf16);
        public Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default) =>
            Task.FromResult<IReadOnlySet<string>>(new HashSet<string> { "jeff.support.triage", "jeff.safety.spam" });
        public Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default)
        {
            // against IEngine's contract, the second adapter throws instead of returning a faulted task
            if (adapterId == "jeff.safety.spam") throw new EngineError("spam is down");
            return Answer(rows);
        }
        private async Task<EngineResult> Answer(IReadOnlyList<Row> rows)
        {
            await Task.Delay(20);
            Interlocked.Increment(ref Settled);
            return new EngineResult([[0.5, 0.5, 0, 0, 0], [0.5, 0.5, 0, 0, 0]], new JsonObject(), null);
        }
        public ValueTask DisposeAsync() => ValueTask.CompletedTask;
    }

    [Fact]
    public async Task AnEngineThatThrowsInsteadOfFailingItsTaskIsReportedInAdapterOrderAsPythonMeetsIt()
    {
        var engine = new ThrowingEngine();
        var jeff = await JeffClient.CreateAsync(engine, Catalogue.Shipped());
        var calls = new[] { new Call("triage.urgency", "a", Ctx()), new Call("spam.is_spam", "b", new JsonObject { ["channel"] = "sms" }) };
        // Python reports the first adapter's reply of 2 rows for 1 before it ever asks the second adapter
        Assert.Equal("The engine answered 2 of 1 rows for jeff.support.triage", (await Assert.ThrowsAsync<EngineError>(() => jeff.DecideAsync(calls))).Message);
        Assert.Equal(1, engine.Settled);
    }

}
