using System.Diagnostics.CodeAnalysis;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>One step of a dispatch: the router that decided, the target it chose, and how confident it was.</summary>
/// <param name="Router">The router function's ID.</param>
/// <param name="Chose">The target the router chose (a router or a function).</param>
/// <param name="Confidence">The router's confidence in that choice.</param>
public sealed record RouteStep(string Router, string Chose, double Confidence);

/// <summary>One earlier attempt in an escalated decision: who decided, what they answered, how confident, and why the
/// escalation went on past it.</summary>
/// <param name="DecidedBy">The function (ID@version) or step ("llm" or "human") that answered.</param>
/// <param name="Value">The value it answered.</param>
/// <param name="Confidence">Its confidence, or null when it has none (an llm or human step).</param>
/// <param name="Reason">Why its answer was not taken (for example "abstained (confidence 0.41 &lt; threshold 0.6)").</param>
public sealed record EscalationRecord(string DecidedBy, JsonValue Value, double? Confidence, string Reason);

/// <summary>The Decision every function returns (port of jeff/decision.py). Value is a string (choice), a bool (noul)
/// or an int (score); Probabilities keeps the option order, number-like option keys included. Treat it as immutable:
/// a changed copy is a `with` expression. Probabilities and Timing are the two mutable values a decision holds: each
/// decision keeps its own copy of both (the map or object it is given is copied, and so is every `with` copy's), so
/// a change to one decision's never reaches another's.</summary>
public sealed record Decision
{
    private readonly OrderedMap<double> probabilities = null!;
    private readonly JsonObject timing = null!;

    /// <summary>An empty decision for an object initialiser; every required property must be set.</summary>
    public Decision()
    {
    }

    /// <summary>The copy a `with` expression makes, with its own Probabilities and Timing.</summary>
    [SetsRequiredMembers]
    private Decision(Decision original)
    {
        Value = original.Value;
        Confidence = original.Confidence;
        Abstained = original.Abstained;
        Calibrated = original.Calibrated;
        Interface = original.Interface;
        Function = original.Function;
        Version = original.Version;
        Probabilities = original.Probabilities;
        Score = original.Score;
        Route = original.Route;
        DecidedBy = original.DecidedBy;
        Escalations = original.Escalations;
        Timing = original.Timing;
        EvidenceRefs = original.EvidenceRefs;
    }

    /// <summary>The answer: a string (choice), a bool (noul, the yes-or-no interfaces) or a whole number (score).</summary>
    public required JsonValue Value { get; init; }
    /// <summary>The probability of the answer (calibrated when Calibrated), or null when the decider gives none.</summary>
    public required double? Confidence { get; init; }
    /// <summary>True when the confidence is below the function's calibrated threshold, or the answer is one of its abstain labels.</summary>
    public required bool Abstained { get; init; }
    /// <summary>True when the function has a calibration for the base the engine serves.</summary>
    public required bool Calibrated { get; init; }
    /// <summary>The interface the function implements (classify, route, rate, detect, ...).</summary>
    public required string Interface { get; init; }
    /// <summary>The full ID of the function that decided.</summary>
    public required string Function { get; init; }
    /// <summary>The version of that function's adapter.</summary>
    public required string Version { get; init; }
    /// <summary>The probability of every option, in option order (number-like keys included); a copy of its own.</summary>
    public required OrderedMap<double> Probabilities
    {
        get => probabilities;
        [MemberNotNull(nameof(probabilities))]
        init => probabilities = new OrderedMap<double>(value);
    }
    /// <summary>For a score function, the expected level: the sum of each level's position (0 for the first) times its probability; else null.</summary>
    public required double? Score { get; init; }
    /// <summary>The routers a dispatch went through to reach the function, in order; empty for a direct call.</summary>
    public required IReadOnlyList<RouteStep> Route { get; init; }
    /// <summary>Who gave the answer: a function as ID@version, or "llm" or "human" for the escalation step that answered.</summary>
    public required string DecidedBy { get; init; }
    /// <summary>The earlier attempts of an escalated decision, in order; empty when none.</summary>
    public required IReadOnlyList<EscalationRecord> Escalations { get; init; }
    /// <summary>How long the decision took, as the engine or server reports it (for example model_ms); a copy of its own.</summary>
    public required JsonObject Timing
    {
        get => timing;
        [MemberNotNull(nameof(timing))]
        init => timing = (JsonObject)Json.Canonical(value)!;
    }
    /// <summary>References to the evidence the decision rests on, when the decider gives any; else empty.</summary>
    public required IReadOnlyList<string> EvidenceRefs { get; init; }

    /// <summary>Equal when both decisions write the same JSON (ToJson), int versus float and key order included: the
    /// compiler's record equality would compare Probabilities and Timing by reference.</summary>
    public bool Equals(Decision? other) => other is not null && Json.StrictEquals(ToJson(), other.ToJson());

    /// <summary>A hash of the decision's JSON text, consistent with Equals.</summary>
    public override int GetHashCode() => Json.Write(ToJson()).GetHashCode(StringComparison.Ordinal);

    private static JsonNode? NumberOrNull(double? number) => number is { } value ? JsonValue.Create(value) : null;

    /// <summary>The decision as contract/schemas/decision writes it, in the Python SDK's field order.</summary>
    public JsonObject ToJson()
    {
        var probabilities = new JsonObject();
        foreach (var (key, value) in Probabilities) probabilities[key] = value;
        return new JsonObject
        {
            ["value"] = Json.Canonical(Value),
            ["confidence"] = NumberOrNull(Confidence),
            ["abstained"] = Abstained,
            ["calibrated"] = Calibrated,
            ["interface"] = Interface,
            ["function"] = Function,
            ["version"] = Version,
            ["probabilities"] = probabilities,
            ["score"] = NumberOrNull(Score),
            ["route"] = new JsonArray(Route.Select(step => (JsonNode?)new JsonObject
            {
                ["router"] = step.Router, ["chose"] = step.Chose, ["confidence"] = step.Confidence,
            }).ToArray()),
            ["decided_by"] = DecidedBy,
            ["escalations"] = new JsonArray(Escalations.Select(item => (JsonNode?)new JsonObject
            {
                ["decided_by"] = item.DecidedBy, ["value"] = Json.Canonical(item.Value), ["confidence"] = NumberOrNull(item.Confidence), ["reason"] = item.Reason,
            }).ToArray()),
            ["timing"] = Json.Canonical(Timing),
            ["evidence_refs"] = Json.Strings(EvidenceRefs),
        };
    }

    private static JsonValue ValueOf(JsonObject data, string key, string where) =>
        data[key] is JsonValue value && Json.KindOf(value) is JsonKind.String or JsonKind.Bool or JsonKind.Int
            ? (JsonValue)Json.Canonical(value)!
            : throw new ContractError($"{where} is missing its '{key}' value, a string, bool or int (got {PyFormat.Repr(data[key])})");

    /// <summary>Each item of a list the decision carries (route steps, escalation records), refused by name when it is
    /// not an object.</summary>
    private static IEnumerable<JsonObject> Items(JsonObject data, string key, string where, string item) =>
        JsonRead.Array(data, key, where).Select((node, index) => node as JsonObject
            ?? throw new ContractError($"{where}'s '{key}' item {index} is not {item} object (got {PyFormat.Repr(node)})"));

    private static double? OptionalNumber(JsonObject data, string key, string where) => data[key] is null ? null : JsonRead.Number(data, key, where);

    /// <summary>A decision read from its JSON (checked against contract/schemas/decision by whoever received it).</summary>
    public static Decision FromJson(JsonObject data)
    {
        const string where = "a decision";
        var probabilities = new OrderedMap<double>();
        var listed = JsonRead.Object(data, "probabilities", where);
        foreach (var (key, _) in listed) probabilities.Set(key, JsonRead.Number(listed, key, $"{where}'s probabilities"));
        return new Decision
        {
            Value = ValueOf(data, "value", where),
            Confidence = OptionalNumber(data, "confidence", where),
            Abstained = JsonRead.Bool(data, "abstained", where),
            Calibrated = JsonRead.Bool(data, "calibrated", where),
            Interface = JsonRead.String(data, "interface", where),
            Function = JsonRead.String(data, "function", where),
            Version = JsonRead.String(data, "version", where),
            Probabilities = probabilities,
            Score = OptionalNumber(data, "score", where),
            Route = Items(data, "route", where, "a route step")
                .Select(step => new RouteStep(JsonRead.String(step, "router", "a route step"), JsonRead.String(step, "chose", "a route step"),
                    JsonRead.Number(step, "confidence", "a route step"))).ToList(),
            DecidedBy = JsonRead.String(data, "decided_by", where),
            Escalations = Items(data, "escalations", where, "an escalation record")
                .Select(item => new EscalationRecord(JsonRead.String(item, "decided_by", "an escalation record"),
                    ValueOf(item, "value", "an escalation record"), OptionalNumber(item, "confidence", "an escalation record"),
                    JsonRead.String(item, "reason", "an escalation record"))).ToList(),
            Timing = (JsonObject)Json.Canonical(JsonRead.Object(data, "timing", where))!,
            EvidenceRefs = JsonRead.Strings(data, "evidence_refs", where),
        };
    }
}

internal static class Decisions
{
    /// <summary>How far from 1 the sum of an engine reply's probabilities may be (contract/socket.md, "The
    /// probabilities of a predict"): the engine's float32 softmax over at most 255 options is off by at most about
    /// 1.5e-5, and its own tests hold it to 1e-4. SUM_TOLERANCE in jeff/decision.py.</summary>
    public const double SumTolerance = 1e-4;

    private static string Listed(IEnumerable<double> numbers) => "[" + string.Join(", ", numbers.Select(PyFormat.Float)) + "]";

    /// <summary>The decision for one row (port of jeff.decision.make_decision). A reply that breaks the engine's
    /// contract is refused with EngineError, never repaired, naming the first rule broken: the wrong number of
    /// probabilities, then any that is not a finite number from 0 to 1, then a sum (Python's sum) more than
    /// SumTolerance from 1. A valid reply is divided by its sum; the winner is the first best option in option order
    /// (a later equal one never wins); it abstained below the calibration threshold or on an abstain label.</summary>
    public static Decision Make(Binding binding, IReadOnlyList<double> probabilities, JsonObject timing)
    {
        var fn = binding.Function;
        var keys = Bindings.OptionKeys(binding);
        if (probabilities.Count != keys.Count)
        {
            throw new EngineError($"{fn.Id}: the engine returned {probabilities.Count} probabilities for {keys.Count} options");
        }
        var offending = probabilities.Where(p => !(double.IsFinite(p) && p >= 0 && p <= 1)).ToList();
        if (offending.Count > 0)
        {
            throw new EngineError($"{fn.Id}: the engine returned invalid probabilities {Listed(probabilities)}; expected each to be a "
                + $"finite number from 0 to 1, which {Listed(offending)} are not");
        }
        double total = PyFormat.Sum(probabilities);
        if (Math.Abs(total - 1) > SumTolerance)
        {
            throw new EngineError($"{fn.Id}: the engine returned invalid probabilities {Listed(probabilities)}; expected their sum to be "
                + $"within {PyFormat.Float(SumTolerance)} of 1, got {PyFormat.Float(total)}");
        }
        var normalised = probabilities.Select(p => p / total).ToList();
        var caller = new OrderedMap<double>();
        for (int index = 0; index < keys.Count; index++)
        {
            if (!binding.WireToCaller.TryGetValue(keys[index], out var name)) throw new EngineError($"{fn.Id}: the binding has no caller key for option {keys[index]}");
            caller.Set(name, normalised[index]);
        }
        JsonValue value;
        double confidence;
        double? score = null;
        if (fn.Type == QuestionType.Noul)
        {
            double yes = caller["true"];
            bool answer = yes >= 0.5;
            value = JsonValue.Create(answer);
            confidence = answer ? yes : caller["false"];
        }
        else if (fn.Type == QuestionType.Score)
        {
            int best = 0;
            for (int index = 0; index < normalised.Count; index++) if (normalised[index] > normalised[best]) best = index;
            value = JsonValue.Create((long)best);
            confidence = normalised[best];
            score = PyFormat.Sum(normalised.Select((p, index) => index * p));
        }
        else
        {
            string? bestKey = null;
            double bestP = double.NegativeInfinity;
            foreach (var (key, p) in caller)
            {
                if (bestKey is null || p > bestP)
                {
                    bestKey = key;
                    bestP = p;
                }
            }
            value = JsonValue.Create(bestKey!);
            confidence = bestP;
        }
        var calibration = binding.Calibration;
        bool abstained = (calibration is not null && confidence < calibration.Threshold)
            || (Json.KindOf(value) == JsonKind.String && fn.AbstainLabels.Contains(value.GetValue<string>()));
        return new Decision
        {
            Value = value, Confidence = confidence, Abstained = abstained, Calibrated = calibration is not null, Interface = fn.Interface,
            Function = fn.Id, Version = binding.Manifest.Version, Probabilities = caller, Score = score, Route = [],
            DecidedBy = $"{fn.Id}@{binding.Manifest.Version}", Escalations = [], Timing = (JsonObject)Json.Canonical(timing)!, EvidenceRefs = [],
        };
    }
}

public sealed partial class LowConfidence
{
    /// <summary>The decision that abstained, as the error's details carry it.</summary>
    public Decision Decision => Details["decision"] is JsonObject data
        ? Decision.FromJson(data)
        : throw new ContractError($"{Type}.details is missing its 'decision' object (got {PyFormat.Repr(Details["decision"])})");
}

internal static partial class Errors
{
    public static LowConfidence LowConfidenceError(Decision decision) =>
        new($"{decision.Function} abstained (confidence {PyFormat.ReprOrNone(decision.Confidence)}); on_low_confidence='error'",
            new JsonObject { ["decision"] = decision.ToJson() });
}
