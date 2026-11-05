package ai.jeff.sdk;

import java.util.ArrayDeque;
import java.util.ArrayList;
import java.util.Deque;
import java.util.LinkedHashMap;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Map;
import java.util.Set;

/** Walking a tree of router functions to the function that should answer (port of jeff/routing.py). A router's
 * labels are subset labels over its routes_to: at each level dispatch offers only the targets that implement the
 * requested interface, or lead to one that does. */
final class Routing {
    static final int MAX_DEPTH = 8;

    private Routing() {}

    private record Target(boolean router, String id) {}

    private static Target resolveTarget(Catalogue catalogue, String routerId, String target) {
        try {
            Manifest manifest = catalogue.adapter(target);
            if (manifest.isRouter()) return new Target(true, manifest.id());
            return new Target(false, catalogue.function(target).id());
        } catch (JeffError.UnknownFunction error) {
            JeffError.ManifestError refused = new JeffError.ManifestError("Router " + routerId + " routes to " + target + ": "
                + error.getMessage());
            refused.initCause(error);
            throw refused;
        }
    }

    private static Set<String> interfacesReached(Catalogue catalogue, String routerId, List<String> path) {
        if (path.contains(routerId)) {
            List<String> loop = new ArrayList<>(path);
            loop.add(routerId);
            throw new JeffError.ManifestError("The routing tree has a loop: " + String.join(" -> ", loop));
        }
        List<String> next = new ArrayList<>(path);
        next.add(routerId);
        Set<String> reached = new LinkedHashSet<>();
        for (String target : catalogue.adapter(routerId).routesTo()) reached.addAll(targetInterfaces(catalogue, routerId, target, next));
        return reached;
    }

    private static Set<String> targetInterfaces(Catalogue catalogue, String routerId, String target, List<String> path) {
        Target resolved = resolveTarget(catalogue, routerId, target);
        if (resolved.router()) return interfacesReached(catalogue, resolved.id(), path);
        return Set.of(catalogue.function(resolved.id()).interfaceName());
    }

    /** Every installed router's targets resolve, are installed and form no loop; returns each one's exact keys. */
    static Map<String, Map<String, String>> checkRouters(Catalogue catalogue, Set<String> installed) {
        Map<String, Map<String, String>> exactKeys = new LinkedHashMap<>();
        for (Manifest manifest : catalogue.manifests()) {
            if (!manifest.isRouter() || !installed.contains(manifest.id())) continue;
            for (String target : manifest.routesTo()) {
                Target resolved = resolveTarget(catalogue, manifest.id(), target);
                String adapterId = resolved.router() ? resolved.id() : catalogue.function(resolved.id()).adapterId();
                if (!installed.contains(adapterId)) throw JeffError.notInstalled(target, "router " + manifest.id() + " routes to it");
            }
            interfacesReached(catalogue, manifest.id(), List.of());
            exactKeys.put(manifest.id(), exactKeysOf(catalogue, manifest.id()));
        }
        return exactKeys;
    }

    private static List<String> reachableAdapters(Catalogue catalogue, String routerId) {
        List<String> seen = new ArrayList<>();
        Deque<String> pending = new ArrayDeque<>(List.of(routerId));
        while (!pending.isEmpty()) {
            String adapterId = pending.removeFirst();
            if (seen.contains(adapterId)) continue;
            seen.add(adapterId);
            Manifest manifest = catalogue.adapter(adapterId);
            for (String target : manifest.routesTo()) {
                Target resolved = resolveTarget(catalogue, manifest.id(), target);
                pending.addLast(resolved.router() ? resolved.id() : catalogue.function(resolved.id()).adapterId());
            }
        }
        return seen;
    }

    /** Exact key -> code name of every context field of every function reachable from the router whose key differs
     * from its code name. */
    static Map<String, String> exactKeysOf(Catalogue catalogue, String routerId) {
        Map<String, String> exact = new LinkedHashMap<>();
        for (String adapterId : reachableAdapters(catalogue, routerId)) {
            for (FunctionSpec fn : catalogue.adapter(adapterId).functions().values()) {
                for (ContextField field : fn.context()) {
                    if (!field.key().equals(field.codeName()) && !exact.containsKey(field.key())) exact.put(field.key(), field.codeName());
                }
            }
        }
        return exact;
    }

    static void refuseExactKeys(Map<String, String> exactKeys, String routerId, Map<String, ?> context) {
        List<String> misnamed = new ArrayList<>();
        for (String key : context.keySet()) if (exactKeys.containsKey(key)) misnamed.add(PyFormat.str(key) + " as " + exactKeys.get(key));
        if (!misnamed.isEmpty()) {
            throw new JeffError.UnknownContext("context is given by code name; pass " + String.join(", ", misnamed)
                + " (fields of adapters reached from router " + routerId + ")");
        }
    }

    static String startRouter(Jeff jeff, String router) {
        Manifest manifest;
        try {
            manifest = jeff.catalogue().adapter(router);
        } catch (JeffError.UnknownFunction error) {
            JeffError.UnknownFunction refused = new JeffError.UnknownFunction("There is no router " + router
                + " in the catalogue, so this call cannot be routed; name the function to call with function=");
            refused.initCause(error);
            throw refused;
        }
        if (!Jeff.serves(jeff.engine(), manifest.id())) {
            throw JeffError.notInstalled(manifest.id(), "there is no router installed to route this call; install it, or name "
                + "the function to call with function=");
        }
        return manifest.id();
    }

    /** The call dispatch makes to the chosen function: the caller's context fields it has, and the caller's labels
     * unless its labels are fixed with no caller subset. */
    static Call leafCall(FunctionSpec leaf, Object input, Map<String, ?> context, Object labels) {
        Map<String, ?> given = context == null ? Map.of() : context;
        Map<String, Object> leafContext = new LinkedHashMap<>();
        for (ContextField field : leaf.context()) {
            if (given.containsKey(field.codeName())) leafContext.put(field.codeName(), given.get(field.codeName()));
        }
        boolean takesLabels = !leaf.labels().source().equals("fixed") || Boolean.TRUE.equals(leaf.labels().subset());
        return new Call(leaf.id(), input, leafContext, takesLabels ? labels : null);
    }

    static Decision dispatch(Jeff jeff, Object input, String iface, String router, CallOptions options) {
        Policy policy = options.onLowConfidence();
        Escalation.checkPolicy(jeff, iface, policy);
        Map<String, ?> given = options.context() == null ? Map.of() : options.context();
        List<Decision.RouteStep> route = new ArrayList<>();
        String current = startRouter(jeff, router);
        if (!given.isEmpty()) {
            Map<String, String> exactKeys = jeff.exactKeys().computeIfAbsent(current, id -> exactKeysOf(jeff.catalogue(), id));
            refuseExactKeys(exactKeys, current, given);
        }
        for (int depth = 0; depth < MAX_DEPTH; depth++) {
            Manifest manifest = jeff.catalogue().adapter(current);
            if (!Jeff.serves(jeff.engine(), manifest.id())) {
                // the first router was checked as served by startRouter, so a router not served was chosen by another
                if (route.isEmpty()) throw new IllegalStateException("router " + manifest.id() + " is not served, and no router chose it");
                throw JeffError.notInstalled(manifest.id(), "router " + route.getLast().router() + " chose it");
            }
            if (!manifest.isRouter()) throw new JeffError.ManifestError(manifest.id() + " is not a router");
            List<String> offered = new ArrayList<>();
            for (String target : manifest.routesTo()) {
                if (targetInterfaces(jeff.catalogue(), manifest.id(), target, List.of(manifest.id())).contains(iface)) offered.add(target);
            }
            if (offered.isEmpty()) {
                throw new JeffError.ManifestError("Router " + manifest.id() + " has no target that implements or leads to " + iface);
            }
            FunctionSpec fn = manifest.functions().firstEntry().getValue();
            Map<String, Object> routerContext = new LinkedHashMap<>();
            for (ContextField field : fn.context()) {
                if (given.containsKey(field.codeName())) routerContext.put(field.codeName(), given.get(field.codeName()));
            }
            Decision decision = jeff.decide(List.of(new Call(fn.id(), input, routerContext.isEmpty() ? null : routerContext,
                offered))).get(0);
            if (decision.confidence() == null) {
                throw new JeffError.EngineError("Router " + fn.id() + " returned a decision without a confidence");
            }
            route.add(new Decision.RouteStep(fn.id(), PyFormat.toStr(decision.value()), decision.confidence()));
            if (decision.abstained()) {
                Decision withRoute = decision.withRoute(route);
                if (Policy.RETURN.equals(policy)) return withRoute;
                throw JeffError.lowConfidence(withRoute);
            }
            Target target = resolveTarget(jeff.catalogue(), manifest.id(), PyFormat.toStr(decision.value()));
            if (target.router()) {
                current = target.id();
                continue;
            }
            Call call = leafCall(jeff.catalogue().function(target.id()), input, given, options.labels());
            Decision answered = jeff.decide(List.of(call)).get(0);
            return jeff.applyPolicy(call, answered.withRoute(route), policy);
        }
        throw new JeffError.ManifestError("Routing from " + router + " went deeper than " + MAX_DEPTH + " levels");
    }
}
