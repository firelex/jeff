using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class DecisionTests
{
    private static readonly Catalogue Shipped = Catalogue.Shipped();

    private static JsonObject Timing() => new();

    [Fact]
    public void ATieGoesToTheFirstOptionInOptionOrderAlsoForKeysThatLookLikeNumbers()
    {
        var raw = (JsonObject)Json.Canonical(Shipped.Adapter("jeff.safety.spam").Raw)!;
        raw["functions"]!["message_type"]!["labels"]!["fixed"] = new JsonObject { ["2"] = "Two", ["1"] = "One", ["legit"] = "Fine" };
        var manifest = Manifest.FromDict(raw, "test");
        var fn = manifest.Functions["message_type"];
        var decision = Decisions.Make(Bindings.Bind(manifest, fn, new JsonObject { ["channel"] = "sms" }, null, null), [0.4, 0.4, 0.2], Timing());
        Assert.Equal("2", decision.Value.GetValue<string>());
        Assert.Equal(new[] { "2", "1", "legit" }, decision.Probabilities.Keys);
        Assert.Equal("{\"2\":0.4,\"1\":0.4,\"legit\":0.2}", Json.Write(decision.ToJson()["probabilities"]));
    }

    [Fact]
    public void ProbabilitiesAreNormalisedWithPythonsSum()
    {
        var fn = Shipped.Function("triage.urgency");
        var binding = Bindings.Bind(Shipped.ManifestOf(fn), fn, new JsonObject { ["company"] = "c", ["channel"] = "e" }, null, fn.Calibration);
        var decision = Decisions.Make(binding, [0.2, 0.2, 0.2, 0.2, 0.2], Timing());
        Assert.Equal(0L, decision.Value.GetValue<long>());
        Assert.Equal(0.2, decision.Confidence);
        Assert.Equal(2.0, decision.Score!.Value, 12);
    }

    // The messages below were printed by the Python SDK (jeff.decision.make_decision): the first broken rule is named,
    // the count, then the values outside 0 to 1, then the sum; nothing is ever repaired.
    [Fact]
    public void AnEngineAnswerThatBreaksTheContractIsRefusedNamingTheFirstBrokenRule()
    {
        var fn = Shipped.Function("spam.is_spam");
        var binding = Bindings.Bind(Shipped.ManifestOf(fn), fn, new JsonObject { ["channel"] = "sms" }, null, fn.Calibration);
        string Refusal(double[] probabilities) => Assert.Throws<EngineError>(() => Decisions.Make(binding, probabilities, Timing())).Message;
        Assert.Equal("jeff.safety.spam.is_spam: the engine returned 1 probabilities for 2 options", Refusal([0.5]));
        Assert.Equal("jeff.safety.spam.is_spam: the engine returned 3 probabilities for 2 options", Refusal([double.NaN, 2.0, 0.1]));
        Assert.Equal("jeff.safety.spam.is_spam: the engine returned invalid probabilities [nan, inf]; expected each to be a finite number "
            + "from 0 to 1, which [nan, inf] are not", Refusal([double.NaN, double.PositiveInfinity]));
        Assert.Equal("jeff.safety.spam.is_spam: the engine returned invalid probabilities [1.5, -inf]; expected each to be a finite number "
            + "from 0 to 1, which [1.5, -inf] are not", Refusal([1.5, double.NegativeInfinity]));
        Assert.Equal("jeff.safety.spam.is_spam: the engine returned invalid probabilities [1.0000001, 0.0]; expected each to be a finite "
            + "number from 0 to 1, which [1.0000001] are not", Refusal([1.0000001, 0.0]));
        Assert.Equal("jeff.safety.spam.is_spam: the engine returned invalid probabilities [0.0, 0.0]; expected their sum to be within "
            + "0.0001 of 1, got 0.0", Refusal([0.0, 0.0]));
        Assert.Equal("jeff.safety.spam.is_spam: the engine returned invalid probabilities [0.6, 0.6]; expected their sum to be within "
            + "0.0001 of 1, got 1.2", Refusal([0.6, 0.6]));
    }

    [Fact]
    public void AReplyWithinTheToleranceIsDividedByItsSumAndOneJustOutsideIsRefused()
    {
        var fn = Shipped.Function("spam.is_spam");
        var binding = Bindings.Bind(Shipped.ManifestOf(fn), fn, new JsonObject { ["channel"] = "sms" }, null, fn.Calibration);
        // Python: {'false': 0.24998750062496877, 'true': 0.7500124993750313}, each divided by sum([0.25, 0.75005])
        var decision = Decisions.Make(binding, [0.25, 0.75005], Timing());
        Assert.Equal(new[] { 0.24998750062496877, 0.7500124993750313 }, decision.Probabilities.Values);
        // 0.25 + 0.7501 is within 1e-4 of 1 in float64 (Python accepts it); 0.25 + 0.7502 is not
        Decisions.Make(binding, [0.25, 0.7501], Timing());
        Assert.Equal(-0.0, Decisions.Make(binding, [-0.0, 1.0], Timing()).Probabilities["false"]);
        Assert.Throws<EngineError>(() => Decisions.Make(binding, [0.25, 0.7502], Timing()));
    }

    [Fact]
    public void ADecisionSurvivesItsJsonAndLowConfidenceCarriesIt()
    {
        var fn = Shipped.Function("spam.is_spam");
        var binding = Bindings.Bind(Shipped.ManifestOf(fn), fn, new JsonObject { ["channel"] = "sms" }, null, fn.Calibration);
        var decision = Decisions.Make(binding, [0.47, 0.53], new JsonObject { ["path"] = "fake" });
        JsonAssert.Equal(decision.ToJson(), Decision.FromJson(decision.ToJson()).ToJson());
        var error = Errors.LowConfidenceError(decision);
        Assert.Equal("jeff.safety.spam.is_spam abstained (confidence 0.53); on_low_confidence='error'", error.Message);
        JsonAssert.Equal(decision.ToJson(), error.Decision.ToJson());
        Assert.Throws<ContractError>(() => new LowConfidence("x").Decision);
    }

    [Fact]
    public void AChangedCopyLeavesTheOriginalAlone()
    {
        var fn = Shipped.Function("spam.is_spam");
        var binding = Bindings.Bind(Shipped.ManifestOf(fn), fn, new JsonObject { ["channel"] = "sms" }, null, fn.Calibration);
        var decision = Decisions.Make(binding, [0.2, 0.8], Timing());
        var routed = decision with { Route = [new RouteStep("jeff.core.router.route", "spam", 0.9)] };
        Assert.Empty(decision.Route);
        Assert.Equal("spam", routed.ToJson()["route"]![0]!["chose"]!.GetValue<string>());
    }

    [Fact]
    public void ADecisionsProbabilitiesAndTimingAreItsOwnInEveryCopy()
    {
        var fn = Shipped.Function("spam.is_spam");
        var binding = Bindings.Bind(Shipped.ManifestOf(fn), fn, new JsonObject { ["channel"] = "sms" }, null, fn.Calibration);
        var timing = new JsonObject { ["path"] = "fake" };
        var decision = Decisions.Make(binding, [0.2, 0.8], timing);
        var copy = decision with { Route = [new RouteStep("jeff.core.router.route", "spam", 0.9)] };
        copy.Probabilities.Set("true", 0.5);
        copy.Timing["path"] = "changed";
        timing["path"] = "changed by the caller";
        Assert.Equal(0.8, decision.Probabilities["true"]);
        Assert.Equal("fake", decision.Timing["path"]!.GetValue<string>());
        var given = new OrderedMap<double>();
        given.Set("true", 1);
        var built = decision with { Probabilities = given };
        given.Set("true", 0);
        Assert.Equal(1, built.Probabilities["true"]);
    }

    [Fact]
    public void ADecisionReadWithARouteStepOrEscalationThatIsNotAnObjectIsAContractError()
    {
        var fn = Shipped.Function("spam.is_spam");
        var binding = Bindings.Bind(Shipped.ManifestOf(fn), fn, new JsonObject { ["channel"] = "sms" }, null, fn.Calibration);
        var json = Decisions.Make(binding, [0.2, 0.8], Timing()).ToJson();
        json["route"] = new JsonArray("jeff.core.router.route");
        Assert.Equal("a decision's 'route' item 0 is not a route step object (got 'jeff.core.router.route')",
            Assert.Throws<ContractError>(() => Decision.FromJson(json)).Message);
        json["route"] = new JsonArray();
        json["escalations"] = new JsonArray(new JsonObject { ["decided_by"] = "human", ["value"] = true, ["confidence"] = null, ["reason"] = "r" }, 5);
        json = (JsonObject)Json.Canonical(json)!;
        Assert.Equal("a decision's 'escalations' item 1 is not an escalation record object (got 5)",
            Assert.Throws<ContractError>(() => Decision.FromJson(json)).Message);
        var received = new LowConfidence("x", new JsonObject { ["decision"] = json });
        Assert.Throws<ContractError>(() => received.Decision);
    }

    [Fact]
    public void TwoDecisionsWithTheSameContentAreEqualAndOneThatDiffersIsNot()
    {
        Decision Make(double model) => new()
        {
            Value = JsonValue.Create("spam"), Confidence = 0.9, Abstained = false, Calibrated = true, Interface = "detect", Function = "jeff.safety.spam.spam",
            Version = "1.0.0", Probabilities = new OrderedMap<double>([new("spam", 0.9), new("not_spam", 0.1)]), Score = null, Route = [],
            DecidedBy = "jeff.safety.spam.spam@1.0.0", Escalations = [], Timing = new JsonObject { ["model_ms"] = model }, EvidenceRefs = [],
        };
        Assert.Equal(Make(1.5), Make(1.5));
        Assert.True(Make(1.5) == Make(1.5));
        Assert.Equal(Make(1.5).GetHashCode(), Make(1.5).GetHashCode());
        Assert.NotEqual(Make(1.5), Make(2.5));
        Assert.NotEqual(Make(1.5), Make(1.5) with { Confidence = 0.8 });
    }
}
