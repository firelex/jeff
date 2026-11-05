using System.Diagnostics;
using System.Globalization;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>Anything that decides calls: a JeffClient, or another one used as a remote escalation step.</summary>
public interface IDecider
{
    /// <summary>Decides each call; the decisions come back in call order.</summary>
    /// <returns>One decision per call.</returns>
    Task<IReadOnlyList<Decision>> DecideAsync(IReadOnlyList<Call> calls, CancellationToken cancellationToken = default);
}

/// <summary>An llm or human escalation step: given the question, it returns one of the question's option keys.</summary>
public delegate Task<string?> Ask(Question question, CancellationToken cancellationToken);

/// <summary>What an llm or human step is shown: the function, its instructions, the state the model read (named fields
/// in the order the model reads them, or the plain input string for a text input), and the option keys it may answer
/// with, each with its description, in option order. Build a prompt from StateEntries and OptionEntries: they keep the
/// order the Python SDK shows, number-like keys ("10") included.</summary>
public sealed record Question(string Function, string Instructions, JsonNode State, IReadOnlyList<KeyValuePair<string, JsonNode?>>? StateEntries,
    IReadOnlyList<KeyValuePair<string, JsonNode?>> OptionEntries)
{
    /// <summary>The option keys, each mapped to its description, as a JSON object (the order of OptionEntries).</summary>
    public JsonObject Options
    {
        get
        {
            var options = new JsonObject();
            foreach (var (key, description) in OptionEntries) options[key] = Json.Canonical(description);
            return options;
        }
    }

    /// <summary>The question as the Python SDK shows it: {"function", "instructions", "state", "options"}.</summary>
    public JsonObject ToJson() => new()
    {
        ["function"] = Function, ["instructions"] = Instructions, ["state"] = Json.Canonical(State), ["options"] = Options,
    };
}

/// <summary>One step of an escalation policy: a function, another decider (remote), or an llm or human step.</summary>
public abstract record Step
{
    private protected Step(string name) => Name = name;

    /// <summary>"function", "remote", "llm" or "human", as escalation records and errors name the step.</summary>
    public string Name { get; }
}

/// <summary>An escalation step that asks another function of the catalogue.</summary>
public sealed record FunctionStep : Step
{
    internal FunctionStep(string reference) : base("function") => Ref = reference;

    /// <summary>The function the step asks, as given (resolved by the catalogue when the policy is checked).</summary>
    public string Ref { get; }
}

/// <summary>An escalation step that asks another decider (for example a remote client) to decide the same call.</summary>
public sealed record RemoteStep : Step
{
    internal RemoteStep(IDecider client) : base("remote") => Client = client;

    /// <summary>The decider the step asks.</summary>
    public IDecider Client { get; }
}

/// <summary>An llm or human escalation step: it is shown the question and answers with one of its option keys.</summary>
public sealed record AskStep : Step
{
    internal AskStep(Ask ask, string name) : base(name) => Ask = ask;

    /// <summary>The function that answers the question.</summary>
    public Ask Ask { get; }
}

/// <summary>Makes the steps of an escalation policy (Python's function_step, remote, llm and human).</summary>
public static class Steps
{
    /// <summary>A step that asks the function `reference` (port of function_step).</summary>
    public static FunctionStep Function(string reference) => new(reference);

    /// <summary>A step that asks `client` to decide the same call (port of remote).</summary>
    public static RemoteStep Remote(IDecider client) => new(client);

    /// <summary>An llm step: `ask` is shown the question and returns an option key (port of llm).</summary>
    public static AskStep Llm(Ask ask) => new(ask, "llm");

    /// <summary>A human step: `ask` is shown the question and returns an option key (port of human).</summary>
    public static AskStep Human(Ask ask) => new(ask, "human");
}

/// <summary>What happens when a decision abstains: return it (Return, the default), raise LowConfidence (Error), or
/// escalate through explicit steps. Named holds any other name, which every operation refuses as Python does.</summary>
public abstract record Policy
{
    private protected Policy()
    {
    }

    /// <summary>Return the abstained decision as it is (the default).</summary>
    public static readonly Policy Return = new NamedPolicy("return");

    /// <summary>Raise LowConfidence, carrying the decision that abstained.</summary>
    public static readonly Policy Error = new NamedPolicy("error");

    /// <summary>The policy with this name; any name other than "return" and "error" is refused with InvalidPolicy when it is used, as in Python.</summary>
    public static Policy Named(string name) => new NamedPolicy(name);

    /// <summary>Escalate through these steps in order, with no time limit; InvalidPolicy when there are none.</summary>
    public static EscalatePolicy Escalate(params Step[] steps) => Escalate(steps, null);

    /// <summary>budget: the longest the whole escalation may take; without it, no limit.</summary>
    public static EscalatePolicy Escalate(IReadOnlyList<Step> steps, TimeSpan? budget)
    {
        if (steps.Count == 0) throw new InvalidPolicy("escalate() needs at least one step");
        Escalation.RefuseNonSteps(steps);
        return new EscalatePolicy(steps.ToList(), budget);
    }
}

/// <summary>A policy given by name: "return" or "error".</summary>
/// <param name="Name">The policy's name.</param>
public sealed record NamedPolicy(string Name) : Policy;

/// <summary>Escalate through steps in order until one gives a confident answer.</summary>
/// <param name="Steps">The steps, in the order they are tried.</param>
/// <param name="Budget">The longest the whole escalation may take (EscalationBudgetExceeded past it); null for no limit.</param>
public sealed record EscalatePolicy(IReadOnlyList<Step> Steps, TimeSpan? Budget) : Policy;

/// <summary>Port of jeff/escalation.py: an escalated decision says who answered and lists every earlier attempt; a
/// failing step raises EscalationFailed.</summary>
internal static class Escalation
{
    /// <summary>A null where a step belongs is refused as Python's check_policy refuses a None step. Policy.Escalate
    /// checks it; an EscalatePolicy built with its constructor is checked again when it is used.</summary>
    internal static void RefuseNonSteps(IReadOnlyList<Step> steps)
    {
        if (steps.Any(step => step is null))
        {
            throw new InvalidPolicy("escalate() got None, which is not a step; use Steps.Function(...), Steps.Remote(...), Steps.Llm(...) or Steps.Human(...)");
        }
    }

    public static InvalidPolicy UnknownPolicy(Policy? policy) =>
        new($"Unknown on_low_confidence policy {(policy is NamedPolicy named ? PyFormat.Str(named.Name) : "None")}; use 'return', 'error' or escalate(...)");

    /// <summary>Checks a policy before anything is decided: every function step is in the catalogue, installed, and
    /// implements the interface the call is answered through.</summary>
    public static async Task CheckPolicyAsync(JeffClient jeff, string iface, Policy? policy, CancellationToken cancellationToken)
    {
        if (policy is NamedPolicy { Name: "return" or "error" }) return;
        if (policy is not EscalatePolicy escalate) throw UnknownPolicy(policy);
        RefuseNonSteps(escalate.Steps);
        foreach (var step in escalate.Steps)
        {
            if (step is not FunctionStep functionStep) continue;
            FunctionSpec spec;
            try
            {
                spec = jeff.Catalogue.Function(functionStep.Ref);
            }
            catch (UnknownFunction error)
            {
                throw new InvalidPolicy($"Escalation step function_step({PyFormat.Str(functionStep.Ref)}): {error.Message}");
            }
            if (!(await jeff.Engine.InstalledAsync(false, cancellationToken).ConfigureAwait(false)).Contains(spec.AdapterId))
            {
                throw Errors.NotInstalledError(spec.Id, "it is an escalation step");
            }
            if (spec.Interface != iface) throw new InterfaceMismatch($"Escalation step {spec.Id} implements {spec.Interface}, not {iface}");
        }
    }

    private static string Reason(Decision decision, double? threshold) =>
        threshold is { } limit && decision.Confidence is { } confidence && confidence < limit
            ? $"abstained (confidence {PyFormat.Fixed(confidence, 3)} < threshold {PyFormat.Float(limit)})"
            : "abstained (abstain label)";

    private static EscalationRecord Record(JeffClient jeff, Decision decision)
    {
        var calibration = jeff.CalibrationOf(jeff.Catalogue.Function(decision.Function));
        return new EscalationRecord(decision.DecidedBy, decision.Value, decision.Confidence, Reason(decision, calibration?.Threshold));
    }

    /// <summary>The question an llm or human step is shown, and its option keys in option order.</summary>
    private static async Task<(Question Question, List<string> Keys)> QuestionOfAsync(JeffClient jeff, Call call, CancellationToken cancellationToken)
    {
        var binding = await jeff.BindCallAsync(call, cancellationToken).ConfigureAwait(false);
        var fn = binding.Function;
        var row = Bindings.BuildRow(binding, call.Input);
        List<KeyValuePair<string, JsonNode?>> options;
        if (fn.Type == QuestionType.Choice)
        {
            var criteria = binding.Criteria ?? throw new InvalidOperationException($"the binding of choice function {fn.Id} has no criteria");
            options = Bindings.OptionKeys(binding).Select(key => new KeyValuePair<string, JsonNode?>(
                binding.WireToCaller.TryGetValue(key, out var caller) ? caller : throw new InvalidOperationException($"the binding of {fn.Id} has no caller key for option {key}"),
                Json.Canonical(criteria[key]))).ToList();
        }
        else if (fn.Type == QuestionType.Score)
        {
            options = fn.Labels.Levels.Select((level, index) =>
                new KeyValuePair<string, JsonNode?>(index.ToString(CultureInfo.InvariantCulture), JsonValue.Create(level))).ToList();
        }
        else
        {
            // the wording the engine shows the model when a noul function does not describe its answers
            var described = fn.Labels.Fixed;
            options =
            [
                new("true", described.TryGetValue("true", out var yes) ? Json.Canonical(yes) : JsonValue.Create("Yes / true")),
                new("false", described.TryGetValue("false", out var no) ? Json.Canonical(no) : JsonValue.Create("No / false")),
            ];
        }
        var stateEntries = row.State is JsonObject state ? state.Select(pair => new KeyValuePair<string, JsonNode?>(pair.Key, Json.Canonical(pair.Value))).ToList() : null;
        var question = new Question(fn.Id, fn.Instructions, Json.Canonical(row.State)!, stateEntries, options);
        return (question, options.Select(option => option.Key).ToList());
    }

    private static JsonValue Converted(QuestionType type, string answer) => type switch
    {
        QuestionType.Noul => JsonValue.Create(answer == "true"),
        QuestionType.Score => JsonValue.Create(long.Parse(answer, NumberStyles.None, CultureInfo.InvariantCulture)),
        _ => JsonValue.Create(answer),
    };

    private static async Task<Decision> RunStepAsync(JeffClient jeff, Call call, Decision original, Step step, CancellationToken cancellationToken)
    {
        switch (step)
        {
            case FunctionStep functionStep:
                var spec = jeff.Catalogue.Function(functionStep.Ref);
                if (spec.Interface != original.Interface)
                {
                    throw new InterfaceMismatch($"Escalation step {spec.Id} implements {spec.Interface}, not {original.Interface}");
                }
                return (await jeff.DecideAsync([new Call(functionStep.Ref, call.Input, call.Context, call.Labels)], cancellationToken).ConfigureAwait(false))[0];
            case RemoteStep remote:
                var answered = await remote.Client.DecideAsync([call], cancellationToken).ConfigureAwait(false);
                // Python takes the first decision (IndexError when there is none); one call has exactly one decision
                if (answered.Count != 1) throw new ContractError($"The remote step's client answered {answered.Count} decisions for 1 call");
                return answered[0];
            case AskStep ask:
                var (question, keys) = await QuestionOfAsync(jeff, call, cancellationToken).ConfigureAwait(false);
                long started = Stopwatch.GetTimestamp();
                string? answer = await ask.Ask(question, cancellationToken).ConfigureAwait(false);
                double elapsed = Math.Round(Stopwatch.GetElapsedTime(started).TotalMilliseconds, 3, MidpointRounding.ToEven);
                if (answer is null || !keys.Contains(answer))
                {
                    throw Errors.EscalationFailedError(ask.Name, $"answered {PyFormat.ReprOrNone(answer)}, which is not one of {PyFormat.Repr(keys)}");
                }
                var fn = jeff.Catalogue.Function(call.Function);
                return original with
                {
                    Value = Converted(fn.Type, answer), Confidence = null, Abstained = false, Calibrated = false, Probabilities = new OrderedMap<double>(),
                    Score = null, DecidedBy = ask.Name, Escalations = [], Timing = new JsonObject { ["path"] = ask.Name, ["ms"] = elapsed },
                };
            default:
                throw new InvalidOperationException($"Unknown kind of step {step.GetType().Name}");
        }
    }

    public static async Task<Decision> ApplyPolicyAsync(JeffClient jeff, Call call, Decision decision, Policy? policy, CancellationToken cancellationToken)
    {
        if (!decision.Abstained || policy is NamedPolicy { Name: "return" }) return decision;
        if (policy is NamedPolicy { Name: "error" }) throw Errors.LowConfidenceError(decision);
        if (policy is not EscalatePolicy escalate) throw UnknownPolicy(policy);
        RefuseNonSteps(escalate.Steps);
        long started = Stopwatch.GetTimestamp();
        var attempts = new List<EscalationRecord> { Record(jeff, decision) };
        var latest = decision;
        foreach (var step in escalate.Steps)
        {
            try
            {
                latest = await RunStepAsync(jeff, call, decision, step, cancellationToken).ConfigureAwait(false);
            }
            // the caller's own cancellation goes through as it is; anything else a step throws, its own timeout included
            // (Python's TimeoutError), is that step failing
            catch (Exception error) when (error is not EscalationFailed && !(error is OperationCanceledException && cancellationToken.IsCancellationRequested))
            {
                // as Python's `raise EscalationFailed(step.name, f"{type(error).__name__}: {error}") from error`
                throw Errors.EscalationFailedError(step.Name, Errors.Described(error), error);
            }
            if (escalate.Budget is { } budget && Stopwatch.GetElapsedTime(started) > budget)
            {
                throw new EscalationBudgetExceeded($"Escalation of {decision.Function} took longer than {PyFormat.Millis(budget)} ms");
            }
            if (!latest.Abstained) return latest with { Route = decision.Route, Escalations = attempts.ToList() };
            attempts.Add(Record(jeff, latest));
        }
        return latest with { Route = decision.Route, Escalations = attempts.Take(attempts.Count - 1).ToList() };
    }
}
