using System.Collections.Concurrent;
using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>What a call or a bound function is given besides its input: context, labels and the policy on low confidence.</summary>
public class CallOptions
{
    /// <summary>The context fields, by code name.</summary>
    public JsonObject? Context { get; init; }

    /// <summary>A JSON array of keys (subset labels) or an object of key to description (caller and describe labels).</summary>
    public JsonNode? Labels { get; init; }

    /// <summary>What happens when the decision abstains; Policy.Return (return it) when not given, as in Python.</summary>
    public Policy? OnLowConfidence { get; init; }
}

/// <summary>The options of an interface shorthand (ClassifyAsync, RouteAsync, ...): the call options and, optionally, the function.</summary>
public class ShorthandOptions : CallOptions
{
    /// <summary>The function to call; without it the call is routed (dispatch) to a function implementing the interface.</summary>
    public string? Function { get; init; }
}

/// <summary>The options of DispatchAsync: the interface to reach, the router to start from, and the call options.</summary>
public sealed class DispatchOptions : CallOptions
{
    /// <summary>The interface the call must reach (classify, route, ...).</summary>
    public required string Interface { get; init; }

    /// <summary>The router to start from; Routing.DefaultRouter (jeff.core.router) when not given, as in Python.</summary>
    public string? Router { get; init; }
}

/// <summary>How JeffClient.LocalAsync connects to the engine on this machine: the timeout and the catalogue.</summary>
public sealed class LocalOptions
{
    /// <summary>How long to wait for the engine's hello and for each reply.</summary>
    public required TimeSpan Timeout { get; init; }

    /// <summary>The manifests calls are bound with; the catalogue shipped with the package when not given.</summary>
    public Catalogue? Catalogue { get; init; }
}

internal static class Timeouts
{
    /// <summary>The longest timeout .NET's timers keep exactly (2**31 - 1 ms, about 24.8 days).</summary>
    public static readonly TimeSpan Longest = TimeSpan.FromMilliseconds(int.MaxValue);

    public static void Check(TimeSpan timeout)
    {
        if (timeout <= TimeSpan.Zero) throw new InvalidRequest($"Timeout must be a positive length of time; got {PyFormat.Millis(timeout)} ms");
        if (timeout > Longest)
        {
            throw new InvalidRequest($"Timeout must be at most {int.MaxValue} milliseconds (about 24.8 days), the longest .NET's timers keep; "
                + $"got {PyFormat.Millis(timeout)} ms");
        }
    }
}

/// <summary>What a client needs, read once from its engine: the base format (picks each function's calibration) and the
/// exact keys of every installed router.</summary>
internal sealed record JeffParts(IEngine Engine, Catalogue Catalogue, string? FormatId, ConcurrentDictionary<string, OrderedMap<string>> ExactKeys);

/// <summary>A function bound once (context, labels and policy checked when it is made); every call reuses the binding.</summary>
public class BoundFunction : IAsyncDisposable
{
    private protected BoundFunction(JeffClient client, Call template, Policy policy, Binding bound)
    {
        Client = client;
        Template = template;
        Policy = policy;
        Bound = bound;
    }

    /// <summary>The client the function was bound on.</summary>
    public JeffClient Client { get; }

    internal Binding Bound { get; }

    private protected Call Template { get; }

    private protected Policy Policy { get; }

    internal static async Task<BoundFunction> CreateAsync(JeffClient client, Call template, Policy policy, CancellationToken cancellationToken)
    {
        var bound = await client.BindCallAsync(template, cancellationToken).ConfigureAwait(false);
        await Escalation.CheckPolicyAsync(client, bound.Function.Interface, policy, cancellationToken).ConfigureAwait(false);
        return new BoundFunction(client, template, policy, bound);
    }

    private protected Call CallOf(JsonNode? input) => new(Template.Function, input, Template.Context, Template.Labels);

    /// <summary>Decides one input with the bound context, labels and policy. EngineError for a broken engine reply; LowConfidence
    /// when the policy is Policy.Error and the decision abstains.</summary>
    public virtual async Task<Decision> CallAsync(JsonNode? input, CancellationToken cancellationToken = default) =>
        (await ManyAsync([input], cancellationToken).ConfigureAwait(false))[0];

    /// <summary>Decides every input in one batch per adapter, with the bound context, labels and policy; the decisions come back
    /// in input order.</summary>
    public virtual async Task<IReadOnlyList<Decision>> ManyAsync(IReadOnlyList<JsonNode?> inputs, CancellationToken cancellationToken = default)
    {
        var calls = inputs.Select(CallOf).ToList();
        var decisions = await Client.DecideBoundAsync(calls.Select(call => new BoundCall(call, Bound)).ToList(), cancellationToken).ConfigureAwait(false);
        var answered = new List<Decision>(calls.Count);
        for (int index = 0; index < calls.Count; index++)
        {
            answered.Add(await Client.ApplyPolicyAsync(calls[index], decisions[index], Policy, cancellationToken).ConfigureAwait(false));
        }
        return answered;
    }

    /// <summary>Releases what this bound function holds on a server: nothing here; a channel handle in ChannelBoundFunction.</summary>
    public virtual ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

/// <summary>The Jeff client (port of jeff/client.py): bind functions, decide in batches grouped by adapter, the
/// interface shorthands, dispatch, graphs. Every method that reaches an engine is asynchronous.</summary>
public class JeffClient : IDecider, IAsyncDisposable
{
    private readonly ConcurrentDictionary<string, Calibration?> calibrations = new(StringComparer.Ordinal);

    private protected JeffClient(JeffParts parts)
    {
        Engine = parts.Engine;
        Catalogue = parts.Catalogue;
        BaseFormat = parts.Engine.BaseFormat;
        FormatId = parts.FormatId;
        ExactKeys = parts.ExactKeys;
    }

    /// <summary>The engine the client decides with: a SocketEngine, a server's listing, or your own IEngine.</summary>
    public IEngine Engine { get; }

    /// <summary>The manifests calls are bound with.</summary>
    public Catalogue Catalogue { get; }

    /// <summary>The base weight format the engine reported when the client was made.</summary>
    public BaseFormat BaseFormat { get; }

    /// <summary>The INT4 base's format_id, which picks each function's INT4 calibration; null for a bfloat16 base.</summary>
    public string? FormatId { get; }

    /// <summary>Router ID to the exact keys its tree refuses (Routing.ExactKeysOf).</summary>
    internal ConcurrentDictionary<string, OrderedMap<string>> ExactKeys { get; }

    internal static async Task<JeffParts> PartsAsync(IEngine engine, Catalogue catalogue, CancellationToken cancellationToken = default)
    {
        string? formatId = Engines.FormatIdOf(engine.BaseFormat);
        var exactKeys = Routing.CheckRouters(catalogue, await engine.InstalledAsync(false, cancellationToken).ConfigureAwait(false));
        return new JeffParts(engine, catalogue, formatId, exactKeys);
    }

    /// <summary>A client over the Jeff server at `url` (our API or your own); `token` is sent as a bearer token (a
    /// TokenSource.From function is called before every request); null only for a server with no credentials.</summary>
    public static Task<RemoteJeffClient> RemoteAsync(string url, TokenSource? token, RemoteOptions options, CancellationToken cancellationToken = default) =>
        RemoteJeffClient.ConnectAsync(url, token, options, cancellationToken);

    /// <summary>A client over any engine (a FakeEngine in tests, your own IEngine).</summary>
    public static async Task<JeffClient> CreateAsync(IEngine engine, Catalogue catalogue, CancellationToken cancellationToken = default) =>
        new(await PartsAsync(engine, catalogue, cancellationToken).ConfigureAwait(false));

    /// <summary>A client over the engine already running on this machine at `socketPath` (start it with
    /// `jeff runtime serve`). It never starts an engine and never falls back to a server: no engine there is an
    /// EngineError naming the path and the command that starts one.</summary>
    public static async Task<JeffClient> LocalAsync(string socketPath, LocalOptions options, CancellationToken cancellationToken = default)
    {
        Timeouts.Check(options.Timeout);
        var engine = await SocketEngine.ConnectAsync(socketPath, options.Timeout, cancellationToken).ConfigureAwait(false);
        try
        {
            return await CreateAsync(engine, options.Catalogue ?? Catalogue.Shipped(), cancellationToken).ConfigureAwait(false);
        }
        catch
        {
            await engine.DisposeAsync().ConfigureAwait(false);
            throw;
        }
    }

    /// <summary>A client over the Jeff engine running for this user on this machine, started first when none is
    /// (contract/local-engine.md): `adapters` are adapter IDs or aliases, bound with the catalogue shipped with the
    /// package. Starting needs uv (its uvx runs the engine, jeff-runtime of this package's version) and a Mac with
    /// Apple silicon or Linux with an NVIDIA GPU; the engine is shared by every program of this user and stops by itself
    /// 5 minutes after the last one closes. InvalidRequest for bad options; UnknownFunction, ManifestError or
    /// ProprietaryWeights for the adapters; BackendUnavailable for another machine; RuntimeMissing when uv is missing;
    /// EngineError when the engine cannot be started; NotInstalled when the running engine cannot load an adapter.
    /// To use an engine you start yourself, give its socket path instead (the other LocalAsync).</summary>
    public static Task<JeffClient> LocalAsync(IEnumerable<string> adapters, StartOptions? options = null, CancellationToken cancellationToken = default) =>
        LocalEngine.LocalAsync(adapters, options, EngineEnvironment.Current(), cancellationToken);

    /// <summary>The calibration `fn` decides with on this client's engine, resolved once per function.</summary>
    internal Calibration? CalibrationOf(FunctionSpec fn) => calibrations.GetOrAdd(fn.Id, _ => ResolveCalibrationOf(fn));

    private protected virtual Calibration? ResolveCalibrationOf(FunctionSpec fn) => Manifest.CalibrationFor(fn, FormatId);

    /// <summary>The one place a call is bound: its function resolved, its adapter checked as served, its calibration chosen.</summary>
    internal async Task<Binding> BindCallAsync(Call call, CancellationToken cancellationToken = default)
    {
        var fn = Catalogue.Function(call.Function);
        var manifest = Catalogue.ManifestOf(fn);
        if (!await Engines.ServesAsync(Engine, manifest.Id, cancellationToken).ConfigureAwait(false)) throw Errors.NotInstalledError(fn.Id);
        return Bindings.Bind(manifest, fn, call.Context, call.Labels, CalibrationOf(fn));
    }

    /// <summary>Decides the calls, one engine request per adapter sent together so the engine can batch them; the decisions come
    /// back in call order and no policy runs (an abstained decision is returned). UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; EngineError for a broken engine reply.</summary>
    public virtual async Task<IReadOnlyList<Decision>> DecideAsync(IReadOnlyList<Call> calls, CancellationToken cancellationToken = default)
    {
        var bound = new List<BoundCall>(calls.Count);
        foreach (var call in calls) bound.Add(new BoundCall(call, await BindCallAsync(call, cancellationToken).ConfigureAwait(false)));
        return await DecideBoundAsync(bound, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>Bound calls, one predict per adapter (in the order the adapters are first met), sent together so the
    /// engine can batch them; decisions in call order. A failure is reported as Python, which handles one adapter after
    /// another, meets it: each earlier adapter's predict and decisions come before a later adapter's rows are built.</summary>
    internal virtual async Task<IReadOnlyList<Decision>> DecideBoundAsync(IReadOnlyList<BoundCall> bound, CancellationToken cancellationToken = default)
    {
        var groups = new OrderedMap<List<int>>();
        for (int index = 0; index < bound.Count; index++)
        {
            string adapterId = bound[index].Binding.Manifest.Id;
            if (groups.TryGetValue(adapterId, out var indices)) indices.Add(index);
            else groups.Set(adapterId, [index]);
        }
        var pending = new List<Task<EngineResult>>();
        ExceptionDispatchInfo? buildFailure = null;
        foreach (var (adapterId, indices) in groups)
        {
            List<Row> rows;
            try
            {
                rows = indices.Select(index => Bindings.BuildRow(bound[index].Binding, bound[index].Call.Input)).ToList();
            }
            catch (Exception error)
            {
                // reported below, after the earlier adapters' predicts (as Python meets it); the predicts already
                // started are awaited first, so none is left running
                buildFailure = ExceptionDispatchInfo.Capture(error);
                break;
            }
            pending.Add(StartPredict(adapterId, rows, cancellationToken));
        }
        // Every predict started settles before any failure is reported, so none is left running or unobserved; the
        // failure reported is then the one Python meets first, in adapter order (awaited one by one below).
        await ((Task)Task.WhenAll(pending)).ConfigureAwait(ConfigureAwaitOptions.SuppressThrowing);
        var results = new Decision[bound.Count];
        for (int group = 0; group < pending.Count; group++)
        {
            var (adapterId, indices) = groups[group];
            var output = await pending[group].ConfigureAwait(false);
            if (output.Probabilities.Count != indices.Count)
            {
                throw new EngineError($"The engine answered {output.Probabilities.Count} of {indices.Count} rows for {adapterId}");
            }
            for (int row = 0; row < indices.Count; row++)
            {
                results[indices[row]] = Decisions.Make(bound[indices[row]].Binding, output.Probabilities[row], output.Timing);
            }
        }
        buildFailure?.Throw();
        return results;
    }

    /// <summary>The engine's predict as a task: an engine that throws instead of returning a faulted task (against
    /// IEngine's contract) fails this task the same way, so its failure is reported in adapter order, as Python meets
    /// it, and the predicts already started are still awaited.</summary>
    private Task<EngineResult> StartPredict(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken)
    {
        try
        {
            return Engine.PredictAsync(adapterId, rows, cancellationToken);
        }
        catch (Exception error)
        {
            return Task.FromException<EngineResult>(error);
        }
    }

    /// <summary>Binds `reference` once with its context, labels and policy, all checked here, so every call of the bound function
    /// reuses the binding. UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; InvalidPolicy for a policy the function cannot use.</summary>
    public virtual Task<BoundFunction> FunctionAsync(string reference, CallOptions? options = null, CancellationToken cancellationToken = default) =>
        BoundFunction.CreateAsync(this, new Call(reference, null, options?.Context, options?.Labels), options?.OnLowConfidence ?? Policy.Return, cancellationToken);

    internal Task<Decision> ApplyPolicyAsync(Call call, Decision decision, Policy? policy, CancellationToken cancellationToken = default) =>
        Escalation.ApplyPolicyAsync(this, call, decision, policy, cancellationToken);

    internal virtual async Task<Decision> InterfaceCallAsync(string iface, JsonNode? input, ShorthandOptions? options, CancellationToken cancellationToken = default)
    {
        var policy = options?.OnLowConfidence ?? Policy.Return;
        if (options?.Function is null)
        {
            return await DispatchAsync(input, new DispatchOptions { Interface = iface, Context = options?.Context, Labels = options?.Labels, OnLowConfidence = policy },
                cancellationToken).ConfigureAwait(false);
        }
        var spec = Catalogue.Function(options.Function);
        if (spec.Interface != iface)
        {
            throw new InterfaceMismatch($"{spec.Id} implements {spec.Interface}, not {iface}; call jeff.{spec.Interface}");
        }
        await Escalation.CheckPolicyAsync(this, iface, policy, cancellationToken).ConfigureAwait(false);
        var call = new Call(options.Function, input, options.Context, options.Labels);
        var decision = (await DecideAsync([call], cancellationToken).ConfigureAwait(false))[0];
        return await ApplyPolicyAsync(call, decision, policy, cancellationToken).ConfigureAwait(false);
    }

    /// <summary>Routes the input through routers, starting from options.Router (jeff.core.router when not given), to a function
    /// implementing options.Interface, and returns that function's decision with the route taken. UnknownFunction or
    /// NotInstalled when the router is missing; ManifestError when a router has no target that implements or leads to the
    /// interface, or routing goes deeper than Routing.MaxDepth levels; LowConfidence when a router abstains; UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels for the leaf call.</summary>
    public virtual Task<Decision> DispatchAsync(JsonNode? input, DispatchOptions options, CancellationToken cancellationToken = default) =>
        Routing.DispatchAsync(this, input, options, cancellationToken);

    /// <summary>Chooses one label from a defined set for the input (the "classify" interface): with options.Function, that function
    /// decides (InterfaceMismatch when it implements another interface); without it, the call is dispatched to a function
    /// implementing the interface. UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; LowConfidence when the policy is Policy.Error and the decision abstains.</summary>
    public Task<Decision> ClassifyAsync(JsonNode? input, ShorthandOptions? options = null, CancellationToken cancellationToken = default) =>
        InterfaceCallAsync("classify", input, options, cancellationToken);

    /// <summary>Chooses which candidate (a team, a tool, a screen item or another function) should handle the input (the "route" interface): with options.Function, that function
    /// decides (InterfaceMismatch when it implements another interface); without it, the call is dispatched to a function
    /// implementing the interface. UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; LowConfidence when the policy is Policy.Error and the decision abstains.</summary>
    public Task<Decision> RouteAsync(JsonNode? input, ShorthandOptions? options = null, CancellationToken cancellationToken = default) =>
        InterfaceCallAsync("route", input, options, cancellationToken);

    /// <summary>Places the input on an ordered scale of levels (the "rate" interface): with options.Function, that function
    /// decides (InterfaceMismatch when it implements another interface); without it, the call is dispatched to a function
    /// implementing the interface. UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; LowConfidence when the policy is Policy.Error and the decision abstains.</summary>
    public Task<Decision> RateAsync(JsonNode? input, ShorthandOptions? options = null, CancellationToken cancellationToken = default) =>
        InterfaceCallAsync("rate", input, options, cancellationToken);

    /// <summary>Says whether the input is an instance of the thing the function detects (the "detect" interface): with options.Function, that function
    /// decides (InterfaceMismatch when it implements another interface); without it, the call is dispatched to a function
    /// implementing the interface. UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; LowConfidence when the policy is Policy.Error and the decision abstains.</summary>
    public Task<Decision> DetectAsync(JsonNode? input, ShorthandOptions? options = null, CancellationToken cancellationToken = default) =>
        InterfaceCallAsync("detect", input, options, cancellationToken);

    /// <summary>Judges whether an answer is supported by its sources, using only the sources (the "is_supported" interface): with options.Function, that function
    /// decides (InterfaceMismatch when it implements another interface); without it, the call is dispatched to a function
    /// implementing the interface. UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; LowConfidence when the policy is Policy.Error and the decision abstains.</summary>
    public Task<Decision> IsSupportedAsync(JsonNode? input, ShorthandOptions? options = null, CancellationToken cancellationToken = default) =>
        InterfaceCallAsync("is_supported", input, options, cancellationToken);

    /// <summary>Judges whether two records describe the same entity (the "equivalent" interface): with options.Function, that function
    /// decides (InterfaceMismatch when it implements another interface); without it, the call is dispatched to a function
    /// implementing the interface. UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; LowConfidence when the policy is Policy.Error and the decision abstains.</summary>
    public Task<Decision> EquivalentAsync(JsonNode? input, ShorthandOptions? options = null, CancellationToken cancellationToken = default) =>
        InterfaceCallAsync("equivalent", input, options, cancellationToken);

    /// <summary>Says whether a person must act on the input now (the "requires_escalation" interface): with options.Function, that function
    /// decides (InterfaceMismatch when it implements another interface); without it, the call is dispatched to a function
    /// implementing the interface. UnknownFunction, NotInstalled, MissingContext, UnknownContext, MissingLabels or InvalidLabels when the call cannot be bound; LowConfidence when the policy is Policy.Error and the decision abstains.</summary>
    public Task<Decision> RequiresEscalationAsync(JsonNode? input, ShorthandOptions? options = null, CancellationToken cancellationToken = default) =>
        InterfaceCallAsync("requires_escalation", input, options, cancellationToken);

    private protected static readonly IReadOnlyDictionary<string, CodeFunction> NoCode = new Dictionary<string, CodeFunction>(StringComparer.Ordinal);
    private protected static readonly IReadOnlyDictionary<string, Policy> NoPolicies = new Dictionary<string, Policy>(StringComparer.Ordinal);

    /// <summary>Runs a graph. code: the code nodes' functions by name (none when not given); policies: a policy per node,
    /// in place of the document's 'return' or 'error'.</summary>
    public virtual Task<GraphResult> RunGraphAsync(Graph graph, JsonNode? input, JsonObject parameters, IReadOnlyDictionary<string, CodeFunction>? code = null,
        IReadOnlyDictionary<string, Policy>? policies = null, CancellationToken cancellationToken = default) =>
        Graphs.RunAsync(this, graph, input, parameters, code ?? NoCode, policies ?? NoPolicies, cancellationToken);

    /// <summary>Runs the graph in a graph file (YAML).</summary>
    public Task<GraphResult> RunGraphAsync(string path, JsonNode? input, JsonObject parameters, IReadOnlyDictionary<string, CodeFunction>? code = null,
        IReadOnlyDictionary<string, Policy>? policies = null, CancellationToken cancellationToken = default) =>
        RunGraphAsync(Graphs.Load(path), input, parameters, code, policies, cancellationToken);

    /// <summary>Runs a graph document (graph.schema.json).</summary>
    public Task<GraphResult> RunGraphAsync(JsonObject document, JsonNode? input, JsonObject parameters, IReadOnlyDictionary<string, CodeFunction>? code = null,
        IReadOnlyDictionary<string, Policy>? policies = null, CancellationToken cancellationToken = default) =>
        RunGraphAsync(Graphs.FromDict(document, "the graph given to RunGraphAsync"), input, parameters, code, policies, cancellationToken);

    /// <summary>Closes the client's engine; later calls fail.</summary>
    public virtual ValueTask DisposeAsync() => Engine.DisposeAsync();
}
