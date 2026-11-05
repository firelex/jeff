/** Routing: walk a tree of router functions to the function that should answer (port of jeff/routing.py). A
 * router's labels are subset labels over its routes_to: at each level dispatch offers only the targets that implement
 * the requested interface, or lead to one that does. */

import type { Call, Context, Labels } from "./binding.js";
import type { Catalogue } from "./catalogue.js";
import type { DispatchOptions, Jeff } from "./client.js";
import type { Decision, RouteStep } from "./decision.js";
import { serves } from "./engine.js";
import { EngineError, ManifestError, UnknownContext, UnknownFunction, lowConfidence, notInstalled } from "./errors.js";
import { checkPolicy } from "./escalation.js";
import type { FunctionSpec } from "./manifest.js";
import { pyRepr } from "./pyformat.js";
import type { JSONValue } from "./types.js";

export const MAX_DEPTH = 8;
export const DEFAULT_ROUTER = "jeff.core.router";

export function resolveTarget(catalogue: Catalogue, routerId: string, target: string): ["router" | "function", string] {
  try {
    const manifest = catalogue.adapter(target);
    if (manifest.isRouter) return ["router", manifest.id];
    return ["function", catalogue.function(target).id];
  } catch (error) {
    if (error instanceof UnknownFunction) throw new ManifestError(`Router ${routerId} routes to ${target}: ${error.message}`);
    throw error;
  }
}

export function interfacesReached(catalogue: Catalogue, routerId: string, path: readonly string[] = []): Set<string> {
  if (path.includes(routerId)) throw new ManifestError(`The routing tree has a loop: ${[...path, routerId].join(" -> ")}`);
  const reached = new Set<string>();
  for (const target of catalogue.adapter(routerId).routesTo) {
    for (const iface of targetInterfaces(catalogue, routerId, target, [...path, routerId])) reached.add(iface);
  }
  return reached;
}

function targetInterfaces(catalogue: Catalogue, routerId: string, target: string, path: readonly string[]): Set<string> {
  const [kind, targetId] = resolveTarget(catalogue, routerId, target);
  if (kind === "router") return interfacesReached(catalogue, targetId, path);
  return new Set([catalogue.function(targetId).interface]);
}

/** Every installed router's targets resolve, are installed and form no loop; returns each one's exact keys. */
export function checkRouters(catalogue: Catalogue, installed: ReadonlySet<string>): Map<string, Map<string, string>> {
  const exactKeys = new Map<string, Map<string, string>>();
  for (const manifest of catalogue.manifests()) {
    if (!manifest.isRouter || !installed.has(manifest.id)) continue;
    for (const target of manifest.routesTo) {
      const [kind, targetId] = resolveTarget(catalogue, manifest.id, target);
      const adapterId = kind === "router" ? targetId : catalogue.function(targetId).adapterId;
      if (!installed.has(adapterId)) throw notInstalled(target, `router ${manifest.id} routes to it`);
    }
    interfacesReached(catalogue, manifest.id);
    exactKeys.set(manifest.id, exactKeysOf(catalogue, manifest.id));
  }
  return exactKeys;
}

function reachableAdapters(catalogue: Catalogue, routerId: string): string[] {
  const seen: string[] = [];
  const pending = [routerId];
  while (pending.length > 0) {
    const adapterId = pending.shift() as string;
    if (seen.includes(adapterId)) continue;
    seen.push(adapterId);
    const manifest = catalogue.adapter(adapterId);
    for (const target of manifest.routesTo) {
      const [kind, targetId] = resolveTarget(catalogue, manifest.id, target);
      pending.push(kind === "router" ? targetId : catalogue.function(targetId).adapterId);
    }
  }
  return seen;
}

/** Exact key -> code name of every context field of every function reachable from the router whose key differs
 * from its code name. */
export function exactKeysOf(catalogue: Catalogue, routerId: string): Map<string, string> {
  const exact = new Map<string, string>();
  for (const adapterId of reachableAdapters(catalogue, routerId)) {
    for (const fn of catalogue.adapter(adapterId).functions.values()) {
      for (const field of fn.context) {
        if (field.key !== field.codeName && !exact.has(field.key)) exact.set(field.key, field.codeName);
      }
    }
  }
  return exact;
}

export function refuseExactKeys(exactKeys: ReadonlyMap<string, string>, routerId: string, context: Context): void {
  const misnamed = Object.keys(context).filter((key) => exactKeys.has(key));
  if (misnamed.length > 0) {
    throw new UnknownContext("context is given by code name; pass "
      + misnamed.map((key) => `${pyRepr(key)} as ${exactKeys.get(key)}`).join(", ")
      + ` (fields of adapters reached from router ${routerId})`);
  }
}

export async function startRouter(jeff: Jeff, router: string): Promise<string> {
  let manifest;
  try {
    manifest = jeff.catalogue.adapter(router);
  } catch (error) {
    if (error instanceof UnknownFunction) {
      throw new UnknownFunction(`There is no router ${router} in the catalogue, so this call cannot be routed; name the `
        + "function to call with function=");
    }
    throw error;
  }
  if (!(await serves(jeff.engine, manifest.id))) {
    throw notInstalled(manifest.id, "there is no router installed to route this call; install it, or name the function "
      + "to call with function=");
  }
  return manifest.id;
}

/** The call dispatch makes to the chosen function: the caller's context fields it has, and the caller's labels
 * unless its labels are fixed with no caller subset. */
export function leafCall(leaf: FunctionSpec, input: JSONValue, context: Context | null | undefined,
  labels: Labels | null | undefined): Call {
  const given = context ?? {};
  const leafContext = Object.fromEntries(leaf.context.filter((field) => Object.hasOwn(given, field.codeName))
    .map((field) => [field.codeName, given[field.codeName] as JSONValue]));
  const takesLabels = leaf.labels.source !== "fixed" || leaf.labels.subset === true;
  return { function: leaf.id, input, context: leafContext, labels: takesLabels ? labels ?? null : null };
}

export async function dispatch(jeff: Jeff, input: JSONValue, options: DispatchOptions): Promise<Decision> {
  const policy = options.onLowConfidence === undefined ? "return" : options.onLowConfidence;
  const iface = options.interface;
  const router = options.router === undefined ? DEFAULT_ROUTER : options.router;
  await checkPolicy(jeff, iface, policy);
  const given = options.context ?? {};
  const route: RouteStep[] = [];
  let current = await startRouter(jeff, router);
  if (Object.keys(given).length > 0) {
    let exactKeys = jeff.exactKeys.get(current);
    if (exactKeys === undefined) {
      exactKeys = exactKeysOf(jeff.catalogue, current);
      jeff.exactKeys.set(current, exactKeys);
    }
    refuseExactKeys(exactKeys, current, given);
  }
  for (let depth = 0; depth < MAX_DEPTH; depth += 1) {
    const manifest = jeff.catalogue.adapter(current);
    if (!(await serves(jeff.engine, manifest.id))) {
      const chooser = route.at(-1);
      // the first router was checked as served by startRouter, so a router that is not served was chosen by another
      if (chooser === undefined) throw new Error(`router ${manifest.id} is not served, and no router chose it`);
      throw notInstalled(manifest.id, `router ${chooser.router} chose it`);
    }
    if (!manifest.isRouter) throw new ManifestError(`${manifest.id} is not a router`);
    const offered = manifest.routesTo.filter((target) =>
      targetInterfaces(jeff.catalogue, manifest.id, target, [manifest.id]).has(iface));
    if (offered.length === 0) throw new ManifestError(`Router ${manifest.id} has no target that implements or leads to ${iface}`);
    const fn = [...manifest.functions.values()][0] as FunctionSpec;
    const routerContext = Object.fromEntries(fn.context.filter((field) => Object.hasOwn(given, field.codeName))
      .map((field) => [field.codeName, given[field.codeName] as JSONValue]));
    const [decision] = await jeff.decide([{ function: fn.id, input,
      context: Object.keys(routerContext).length > 0 ? routerContext : null, labels: offered }]) as [Decision];
    if (decision.confidence === null) throw new EngineError(`Router ${fn.id} returned a decision without a confidence`);
    route.push({ router: fn.id, chose: String(decision.value), confidence: decision.confidence });
    if (decision.abstained) {
      const withRoute = decision.with({ route });
      if (policy === "return") return withRoute;
      throw lowConfidence(withRoute);
    }
    const [kind, targetId] = resolveTarget(jeff.catalogue, manifest.id, String(decision.value));
    if (kind === "router") {
      current = targetId;
      continue;
    }
    const call = leafCall(jeff.catalogue.function(targetId), input, given, options.labels);
    const [answered] = await jeff.decide([call]) as [Decision];
    return jeff.applyPolicy(call, answered.with({ route }), policy);
  }
  throw new ManifestError(`Routing from ${router} went deeper than ${MAX_DEPTH} levels`);
}
