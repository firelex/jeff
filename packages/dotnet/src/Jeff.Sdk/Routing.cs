using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>Walking a tree of router functions to the function that should answer (port of jeff/routing.py). A
/// router's labels are subset labels over its routes_to: at each level dispatch offers only the targets that implement
/// the requested interface, or lead to one that does.</summary>
public static class Routing
{
    /// <summary>The most routers a dispatch goes through; deeper is a ManifestError (a tree that loops or is too deep).</summary>
    public const int MaxDepth = 8;
    /// <summary>The router a dispatch starts from when none is given, as in Python.</summary>
    public const string DefaultRouter = "jeff.core.router";

    internal static (string Kind, string Id) ResolveTarget(Catalogue catalogue, string routerId, string target)
    {
        try
        {
            var manifest = catalogue.Adapter(target);
            return manifest.IsRouter ? ("router", manifest.Id) : ("function", catalogue.Function(target).Id);
        }
        catch (UnknownFunction error)
        {
            throw new ManifestError($"Router {routerId} routes to {target}: {error.Message}");
        }
    }

    internal static HashSet<string> InterfacesReached(Catalogue catalogue, string routerId, IReadOnlyList<string>? path = null)
    {
        var walked = path ?? [];
        if (walked.Contains(routerId)) throw new ManifestError($"The routing tree has a loop: {string.Join(" -> ", walked.Append(routerId))}");
        var reached = new HashSet<string>(StringComparer.Ordinal);
        foreach (var target in catalogue.Adapter(routerId).RoutesTo) reached.UnionWith(TargetInterfaces(catalogue, routerId, target, walked.Append(routerId).ToList()));
        return reached;
    }

    private static HashSet<string> TargetInterfaces(Catalogue catalogue, string routerId, string target, IReadOnlyList<string> path)
    {
        var (kind, targetId) = ResolveTarget(catalogue, routerId, target);
        return kind == "router" ? InterfacesReached(catalogue, targetId, path) : new HashSet<string>(StringComparer.Ordinal) { catalogue.Function(targetId).Interface };
    }

    /// <summary>Every installed router's targets resolve, are installed and form no loop; returns each one's exact keys.</summary>
    internal static ConcurrentDictionary<string, OrderedMap<string>> CheckRouters(Catalogue catalogue, IReadOnlySet<string> installed)
    {
        var exactKeys = new ConcurrentDictionary<string, OrderedMap<string>>(StringComparer.Ordinal);
        foreach (var manifest in catalogue.Manifests())
        {
            if (!manifest.IsRouter || !installed.Contains(manifest.Id)) continue;
            foreach (var target in manifest.RoutesTo)
            {
                var (kind, targetId) = ResolveTarget(catalogue, manifest.Id, target);
                string adapterId = kind == "router" ? targetId : catalogue.Function(targetId).AdapterId;
                if (!installed.Contains(adapterId)) throw Errors.NotInstalledError(target, $"router {manifest.Id} routes to it");
            }
            InterfacesReached(catalogue, manifest.Id);
            exactKeys[manifest.Id] = ExactKeysOf(catalogue, manifest.Id);
        }
        return exactKeys;
    }

    private static List<string> ReachableAdapters(Catalogue catalogue, string routerId)
    {
        var seen = new List<string>();
        var pending = new Queue<string>([routerId]);
        while (pending.Count > 0)
        {
            string adapterId = pending.Dequeue();
            if (seen.Contains(adapterId)) continue;
            seen.Add(adapterId);
            var manifest = catalogue.Adapter(adapterId);
            foreach (var target in manifest.RoutesTo)
            {
                var (kind, targetId) = ResolveTarget(catalogue, manifest.Id, target);
                pending.Enqueue(kind == "router" ? targetId : catalogue.Function(targetId).AdapterId);
            }
        }
        return seen;
    }

    /// <summary>Exact key to code name of every context field of every function reachable from the router whose key
    /// differs from its code name.</summary>
    internal static OrderedMap<string> ExactKeysOf(Catalogue catalogue, string routerId)
    {
        var exact = new OrderedMap<string>();
        foreach (var adapterId in ReachableAdapters(catalogue, routerId))
        {
            foreach (var fn in catalogue.Adapter(adapterId).Functions.Values)
            {
                foreach (var field in fn.Context)
                {
                    if (field.Key != field.CodeName && !exact.ContainsKey(field.Key)) exact.Set(field.Key, field.CodeName);
                }
            }
        }
        return exact;
    }

    internal static void RefuseExactKeys(OrderedMap<string> exactKeys, string routerId, JsonObject context)
    {
        var misnamed = context.Select(pair => pair.Key).Where(exactKeys.ContainsKey).ToList();
        if (misnamed.Count > 0)
        {
            throw new UnknownContext("context is given by code name; pass "
                + string.Join(", ", misnamed.Select(key => $"{PyFormat.Str(key)} as {exactKeys[key]}"))
                + $" (fields of adapters reached from router {routerId})");
        }
    }

    internal static async Task<string> StartRouterAsync(JeffClient jeff, string router, CancellationToken cancellationToken)
    {
        Manifest manifest;
        try
        {
            manifest = jeff.Catalogue.Adapter(router);
        }
        catch (UnknownFunction)
        {
            throw new UnknownFunction($"There is no router {router} in the catalogue, so this call cannot be routed; name the function to call with function=");
        }
        if (!await Engines.ServesAsync(jeff.Engine, manifest.Id, cancellationToken).ConfigureAwait(false))
        {
            throw Errors.NotInstalledError(manifest.Id, "there is no router installed to route this call; install it, or name the function to call with function=");
        }
        return manifest.Id;
    }

    /// <summary>The call dispatch makes to the chosen function: the caller's context fields it has, and the caller's
    /// labels unless its labels are fixed with no caller subset.</summary>
    internal static Call LeafCall(FunctionSpec leaf, JsonNode? input, JsonObject? context, JsonNode? labels)
    {
        var leafContext = new JsonObject();
        if (context is not null)
        {
            foreach (var field in leaf.Context)
            {
                if (context.TryGetPropertyValue(field.CodeName, out var value)) leafContext[field.CodeName] = Json.Canonical(value);
            }
        }
        bool takesLabels = leaf.Labels.Source != "fixed" || leaf.Labels.Subset == true;
        return new Call(leaf.Id, input, leafContext, takesLabels ? labels : null);
    }

    internal static async Task<Decision> DispatchAsync(JeffClient jeff, JsonNode? input, DispatchOptions options, CancellationToken cancellationToken)
    {
        var policy = options.OnLowConfidence ?? Policy.Return;
        string iface = options.Interface;
        string router = options.Router ?? DefaultRouter;
        await Escalation.CheckPolicyAsync(jeff, iface, policy, cancellationToken).ConfigureAwait(false);
        var given = options.Context ?? new JsonObject();
        var route = new List<RouteStep>();
        string current = await StartRouterAsync(jeff, router, cancellationToken).ConfigureAwait(false);
        if (given.Count > 0) RefuseExactKeys(jeff.ExactKeys.GetOrAdd(current, id => ExactKeysOf(jeff.Catalogue, id)), current, given);
        for (int depth = 0; depth < MaxDepth; depth++)
        {
            var manifest = jeff.Catalogue.Adapter(current);
            if (!await Engines.ServesAsync(jeff.Engine, manifest.Id, cancellationToken).ConfigureAwait(false))
            {
                // the first router was checked as served by StartRouterAsync, so a router that is not served was chosen by another
                if (route.Count == 0) throw new InvalidOperationException($"router {manifest.Id} is not served, and no router chose it");
                throw Errors.NotInstalledError(manifest.Id, $"router {route[^1].Router} chose it");
            }
            if (!manifest.IsRouter) throw new ManifestError($"{manifest.Id} is not a router");
            var offered = manifest.RoutesTo.Where(target => TargetInterfaces(jeff.Catalogue, manifest.Id, target, [manifest.Id]).Contains(iface)).ToList();
            if (offered.Count == 0) throw new ManifestError($"Router {manifest.Id} has no target that implements or leads to {iface}");
            var fn = manifest.Functions.Values[0];
            var routerContext = new JsonObject();
            foreach (var field in fn.Context)
            {
                if (given.TryGetPropertyValue(field.CodeName, out var value)) routerContext[field.CodeName] = Json.Canonical(value);
            }
            var decision = (await jeff.DecideAsync([new Call(fn.Id, input, routerContext.Count > 0 ? routerContext : null, Json.Strings(offered))],
                cancellationToken).ConfigureAwait(false))[0];
            if (decision.Confidence is not { } confidence) throw new EngineError($"Router {fn.Id} returned a decision without a confidence");
            string chose = PyFormat.StrOf(decision.Value);
            route.Add(new RouteStep(fn.Id, chose, confidence));
            if (decision.Abstained)
            {
                var withRoute = decision with { Route = route.ToList() };
                if (policy is NamedPolicy { Name: "return" }) return withRoute;
                throw Errors.LowConfidenceError(withRoute);
            }
            var (kind, targetId) = ResolveTarget(jeff.Catalogue, manifest.Id, chose);
            if (kind == "router")
            {
                current = targetId;
                continue;
            }
            var call = LeafCall(jeff.Catalogue.Function(targetId), input, given, options.Labels);
            var answered = (await jeff.DecideAsync([call], cancellationToken).ConfigureAwait(false))[0];
            return await jeff.ApplyPolicyAsync(call, answered with { Route = route.ToList() }, policy, cancellationToken).ConfigureAwait(false);
        }
        throw new ManifestError($"Routing from {router} went deeper than {MaxDepth} levels");
    }
}
