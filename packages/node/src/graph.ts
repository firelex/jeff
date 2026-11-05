/** Decision graphs: functions wired together with declarative conditions, run in batched stages (port of
 * jeff/graph.py, spec Section 10). */

import type { Binding, Call, Labels } from "./binding.js";
import type { Jeff } from "./client.js";
import { bigintProblem, firstBigint, readYaml, toPlain, validate } from "./contract.js";
import { Decision, type DecisionJSON } from "./decision.js";
import { ContractError, GraphError, JeffError, NotLicensed, UnknownFunction, notInstalled } from "./errors.js";
import { type Policy, checkPolicy } from "./escalation.js";
import { pyEquals, pyRepr, pySorted } from "./pyformat.js";
import type { JSONValue } from "./types.js";

/** A condition as graph.schema.json writes it: {all: [...]}, {any: [...]}, {not: ...}, {node, is}, {node, in},
 * {node, abstained} or {node, confidence_below}. */
export type Condition = Record<string, unknown>;

export interface GraphNode {
  readonly name: string;
  readonly call: string | null;
  readonly code: string | null;
  readonly after: readonly string[];
  /** Context code name -> the param it takes its value from, in the document's order. */
  readonly context: ReadonlyMap<string, string>;
  readonly labels: string | null;
  readonly when: Condition | null;
  readonly onLowConfidence: string;
}

export interface Graph {
  readonly id: string;
  readonly version: string;
  readonly input: string;
  readonly params: readonly string[];
  /** Node name -> node, in the document's order (a Map: node names may look like numbers). */
  readonly nodes: ReadonlyMap<string, GraphNode>;
  readonly outputs: readonly string[];
  readonly order: readonly string[];
}

export class Skipped {
  constructor(readonly reason: string) {}
  toJSON(): { skipped: true; reason: string } { return { skipped: true, reason: this.reason }; }
}

export type NodeResult = Decision | Skipped | JSONValue;

/** One node's result as contract/schemas/graph_result.schema.json writes it: {decision} for a model node,
 * {skipped: true, reason} for a skipped node, {code} for what a code node returned. */
export function resultToJSON(result: NodeResult): Record<string, unknown> {
  if (result instanceof Decision) return { decision: result.toJSON() };
  if (result instanceof Skipped) return result.toJSON();
  return { code: result };
}

export function resultFromJSON(data: Record<string, unknown>): NodeResult {
  if ("decision" in data) return Decision.fromJSON(data.decision as DecisionJSON);
  if ("skipped" in data) {
    if (typeof data.reason !== "string") throw new GraphError(`A skipped graph result node needs its 'reason' string (got ${pyRepr(data.reason)})`);
    return new Skipped(data.reason);
  }
  if ("code" in data) return data.code as JSONValue;
  throw new GraphError(`A graph result node must hold 'decision', 'skipped' or 'code'; got keys ${pyRepr(pySorted(Object.keys(data)))}`);
}

export class GraphResult {
  constructor(readonly nodes: ReadonlyMap<string, NodeResult>, readonly outputs: ReadonlyMap<string, NodeResult>,
    readonly stages: number) {}

  /** The result as graph_result.schema.json writes it; nodes and outputs are plain objects, which list number-like
   * node names first (the Maps nodes and outputs keep the order the nodes got their results). */
  toJSON(): { nodes: Record<string, unknown>; outputs: Record<string, unknown>; stages: number } {
    const json = (results: ReadonlyMap<string, NodeResult>) =>
      Object.fromEntries([...results].map(([name, result]) => [name, resultToJSON(result)]));
    return { nodes: json(this.nodes), outputs: json(this.outputs), stages: this.stages };
  }

  static fromJSON(data: { nodes: Record<string, Record<string, unknown>>; outputs: Record<string, Record<string, unknown>>;
    stages: number }): GraphResult {
    const read = (results: Record<string, Record<string, unknown>>) =>
      new Map(Object.entries(results).map(([name, item]) => [name, resultFromJSON(item)]));
    return new GraphResult(read(data.nodes), read(data.outputs), data.stages);
  }
}

/** A code node's function. `nodes` holds every result so far as a plain object: iterating it lists number-like node
 * names first, so do not read stage order from it; the stage order is the graph's `order`. */
export type CodeFunction = (args: { input: JSONValue; params: Record<string, JSONValue>; nodes: Record<string, NodeResult> })
  => JSONValue | Promise<JSONValue>;

function conditionNodes(condition: Condition | null): Set<string> {
  if (condition === null) return new Set();
  if ("all" in condition || "any" in condition) {
    const items = ("all" in condition ? condition.all : condition.any) as Condition[];
    return new Set(items.flatMap((item) => [...conditionNodes(item)]));
  }
  if ("not" in condition) return conditionNodes(condition.not as Condition);
  return new Set([condition.node as string]);
}

export function dependencies(node: GraphNode): Set<string> {
  return new Set([...conditionNodes(node.when), ...node.after]);
}

export function isGraph(value: unknown): value is Graph {
  return value !== null && typeof value === "object" && (value as Graph).nodes instanceof Map
    && Array.isArray((value as Graph).order);
}

/** A mapping of a graph document (a Map when read from YAML, a plain object otherwise) in its own order. */
function entriesOf(mapping: unknown): Array<[string, unknown]> {
  if (mapping instanceof Map) return [...mapping].map(([key, value]) => [String(key), value]);
  return Object.entries(mapping as Record<string, unknown>);
}

function fieldOf(mapping: unknown, key: string): unknown {
  return mapping instanceof Map ? mapping.get(key) : (mapping as Record<string, unknown>)[key];
}

function hasField(mapping: unknown, key: string): boolean {
  return mapping instanceof Map ? mapping.has(key) : Object.hasOwn(mapping as object, key);
}

/** A graph document (graph.schema.json) as a Graph. Pass the document as Maps (readYaml) to keep the order of node
 * names that look like numbers, which a plain object puts first. */
export function graphFromDict(raw: unknown, where: string): Graph {
  const huge = firstBigint(raw);
  if (huge !== null) throw new GraphError(`${where}: ${bigintProblem(huge)}`);
  const doc = toPlain(raw) as Record<string, unknown>;
  try {
    validate("graph", doc, where);
  } catch (error) {
    if (error instanceof ContractError) throw new GraphError(error.message);
    throw error;
  }
  const params = doc.params as string[];
  const rawNodes = entriesOf(fieldOf(raw, "nodes"));
  const names = new Set(rawNodes.map(([name]) => name));
  const nodes = new Map<string, GraphNode>();
  for (const [name, body] of rawNodes) {
    const context = new Map(hasField(body, "context")
      ? entriesOf(fieldOf(body, "context")).map(([field, ref]) => [field, fieldOf(ref, "param") as string])
      : []);
    const node: GraphNode = {
      name,
      call: hasField(body, "call") ? fieldOf(body, "call") as string : null,
      code: hasField(body, "code") ? fieldOf(body, "code") as string : null,
      after: hasField(body, "after") ? [...(fieldOf(body, "after") as string[])] : [],
      context,
      labels: hasField(body, "labels") ? fieldOf(fieldOf(body, "labels"), "param") as string : null,
      when: hasField(body, "when") ? toPlain(fieldOf(body, "when")) as Condition : null,
      onLowConfidence: hasField(body, "on_low_confidence") ? fieldOf(body, "on_low_confidence") as string : "return",
    };
    nodes.set(name, node);
    const unknown = pySorted([...dependencies(node)].filter((other) => !names.has(other)));
    if (unknown.length > 0) throw new GraphError(`${where}: node ${name} refers to unknown node(s) ${unknown.join(", ")}`);
    const used = new Set([...context.values(), ...(node.labels !== null ? [node.labels] : [])]);
    const undeclared = pySorted([...used].filter((param) => !params.includes(param)));
    if (undeclared.length > 0) throw new GraphError(`${where}: node ${name} uses undeclared param(s) ${undeclared.join(", ")}`);
  }
  const outputs = doc.outputs as string[];
  const missingOutputs = pySorted(new Set(outputs.filter((name) => !nodes.has(name))));
  if (missingOutputs.length > 0) throw new GraphError(`${where}: outputs name unknown node(s) ${missingOutputs.join(", ")}`);
  const order: string[] = [];
  const state = new Map<string, "active" | "done">();
  const visit = (name: string, path: readonly string[]): void => {
    if (state.get(name) === "done") return;
    if (state.get(name) === "active") throw new GraphError(`${where}: the nodes form a cycle: ${[...path, name].join(" -> ")}`);
    state.set(name, "active");
    for (const dependency of pySorted(dependencies(nodes.get(name) as GraphNode))) visit(dependency, [...path, name]);
    state.set(name, "done");
    order.push(name);
  };
  for (const name of nodes.keys()) visit(name, []);
  return { id: doc.graph as string, version: doc.version as string, input: doc.input as string, params: [...params], nodes,
    outputs: [...outputs], order };
}

function nodeToDict(graphId: string, key: string, node: GraphNode): Record<string, unknown> {
  if (node.name !== key) {
    throw new GraphError(`${graphId}: the node under ${pyRepr(key)} is named ${node.name}; a graph document names each node `
      + "by its key only");
  }
  if (node.call !== null && node.code !== null) {
    throw new GraphError(`${graphId}: node ${key} has both call and code; a node is a model call or a code function`);
  }
  if (node.call === null && node.code === null) throw new GraphError(`${graphId}: node ${key} has neither call nor code`);
  if (node.onLowConfidence !== "return" && node.onLowConfidence !== "error") {
    throw new GraphError(`${graphId}: node ${key} has on_low_confidence ${pyRepr(node.onLowConfidence)}; a graph document `
      + "holds only 'return' or 'error' (pass other policies in policies=)");
  }
  const body: Record<string, unknown> = node.call !== null ? { call: node.call } : { code: node.code };
  if (node.after.length > 0) body.after = [...node.after];
  if (node.context.size > 0) body.context = Object.fromEntries([...node.context].map(([field, param]) => [field, { param }]));
  if (node.labels !== null) body.labels = { param: node.labels };
  if (node.when !== null) body.when = structuredClone(node.when);
  if (node.onLowConfidence !== "return") body.on_low_confidence = node.onLowConfidence;
  return body;
}

/** Python's == between two values built from a graph: mappings (Maps or objects) equal whatever their key order,
 * sequences in order, scalars as pyEquals compares them. */
function sameValue(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    return Array.isArray(a) && Array.isArray(b) && a.length === b.length && a.every((item, index) => sameValue(item, b[index]));
  }
  const isMapping = (value: unknown) => value !== null && typeof value === "object";
  if (isMapping(a) || isMapping(b)) {
    if (!isMapping(a) || !isMapping(b)) return false;
    const x = new Map(entriesOf(a));
    const y = new Map(entriesOf(b));
    return x.size === y.size && [...x].every(([key, value]) => y.has(key) && sameValue(value, y.get(key)));
  }
  return pyEquals(a, b);
}

function sameNode(a: GraphNode, b: GraphNode): boolean {
  return a.name === b.name && a.call === b.call && a.code === b.code && sameValue(a.after, b.after)
    && sameValue(a.context, b.context) && a.labels === b.labels && sameValue(a.when, b.when)
    && a.onLowConfidence === b.onLowConfidence;
}

const GRAPH_FIELDS = ["id", "version", "input", "params", "nodes", "outputs", "order"] as const;

function sameField(field: (typeof GRAPH_FIELDS)[number], a: Graph, b: Graph): boolean {
  if (field === "nodes") {
    return a.nodes.size === b.nodes.size
      && [...a.nodes].every(([key, node]) => b.nodes.has(key) && sameNode(node, b.nodes.get(key) as GraphNode));
  }
  return sameValue(a[field], b[field]);
}

/** A key a plain object lists before every other key, in ascending numeric order (an array index). */
function isArrayIndex(key: string): boolean {
  const number = Number(key);
  return Number.isInteger(number) && number >= 0 && number < 2 ** 32 - 1 && String(number) === key;
}

/** A plain-object document lists number-like node names first, in numeric order: a graph whose node order that
 * would change cannot be written as one. */
function refuseUnkeepableOrder(graph: Graph): void {
  const names = [...graph.nodes.keys()];
  const objectOrder = Object.keys(Object.fromEntries(names.map((name) => [name, true])));
  if (objectOrder.every((name, index) => name === names[index])) return;
  const numberLike = names.filter(isArrayIndex);
  throw new GraphError(`${graph.id}: node name(s) ${numberLike.join(", ")} look like numbers, and a plain object cannot `
    + `keep their order (${names.join(", ")}); the graph can still be read and run from a Map document (loadGraph), `
    + "but this SDK cannot write it back");
}

/** The graph as the document graphFromDict reads (graph.schema.json): graphFromDict(graphToDict(graph)) is the same
 * graph. A Graph the document cannot hold exactly (a node with both or neither of call and code, a policy other than
 * 'return' or 'error', a node keyed by another name, an order other than the one its dependencies give) is a
 * GraphError naming what cannot be written. */
export function graphToDict(graph: Graph): Record<string, unknown> {
  refuseUnkeepableOrder(graph);
  const raw = {
    graph: graph.id, version: graph.version, input: graph.input, params: [...graph.params],
    nodes: Object.fromEntries([...graph.nodes].map(([key, node]) => [key, nodeToDict(graph.id, key, node)])),
    outputs: [...graph.outputs],
  };
  const rebuilt = graphFromDict(structuredClone(raw), `graph ${graph.id}`);
  const differing = GRAPH_FIELDS.filter((field) => !sameField(field, rebuilt, graph));
  if (differing.length > 0) {
    const detail = differing.length === 1 && differing[0] === "order"
      ? `its order ${graph.order.join(", ")} is not the order its dependencies give (${rebuilt.order.join(", ")})`
      : `field(s) ${differing.join(", ")} differ`;
    throw new GraphError(`${graph.id} cannot be written as a graph document that reads back the same: ${detail}`);
  }
  return raw;
}

export function loadGraph(path: string): Graph {
  return graphFromDict(readYaml(path), path);
}

function valueOf(result: NodeResult): unknown {
  return result instanceof Decision ? result.value : result;
}

function holds(condition: Condition, results: ReadonlyMap<string, NodeResult>): boolean {
  if ("all" in condition) return (condition.all as Condition[]).every((item) => holds(item, results));
  if ("any" in condition) return (condition.any as Condition[]).some((item) => holds(item, results));
  if ("not" in condition) return !holds(condition.not as Condition, results);
  const name = condition.node as string;
  const result = results.get(name) as NodeResult;
  if ("is" in condition) return pyEquals(valueOf(result), condition.is);
  if ("in" in condition) return (condition.in as unknown[]).some((item) => pyEquals(valueOf(result), item));
  if (!(result instanceof Decision)) throw new GraphError(`Condition on ${name} needs a model decision; ${name} is a code node`);
  if ("abstained" in condition) return result.abstained === condition.abstained;
  return result.confidence !== null && result.confidence < (condition.confidence_below as number);
}

/** Each model node's call, binding and policy, as checkGraph resolved them. */
export type ModelNodes = Map<string, readonly [Call, Binding, Policy]>;

/** Everything that can be checked before the first decision: params, code and policies name real things, and every
 * model node resolves in the catalogue, is installed, binds its context and labels, and has a valid policy. */
export async function checkGraph(jeff: Jeff, graph: Graph, input: JSONValue, params: Record<string, JSONValue>,
  code: Record<string, CodeFunction>, policies: Record<string, Policy>): Promise<ModelNodes> {
  const missing = graph.params.filter((name) => !Object.hasOwn(params, name));
  if (missing.length > 0) throw new GraphError(`${graph.id} needs param(s) ${missing.join(", ")}`);
  const unknownPolicies = pySorted(Object.keys(policies).filter((name) => !graph.nodes.has(name)));
  if (unknownPolicies.length > 0) {
    throw new GraphError(`${graph.id}: policies= names node(s) ${unknownPolicies.join(", ")}, which the graph does not have `
      + `(its nodes: ${[...graph.nodes.keys()].join(", ")})`);
  }
  const usedCode = new Set([...graph.nodes.values()].flatMap((node) => (node.code !== null ? [node.code] : [])));
  const unknownCode = pySorted(Object.keys(code).filter((name) => !usedCode.has(name)));
  if (unknownCode.length > 0) {
    const used = usedCode.size > 0 ? pySorted(usedCode).join(", ") : "none";
    throw new GraphError(`${graph.id}: code= names function(s) ${unknownCode.join(", ")}, which no node uses `
      + `(code nodes use: ${used})`);
  }
  const missingCode = pySorted([...usedCode].filter((name) => !Object.hasOwn(code, name)));
  if (missingCode.length > 0) throw new GraphError(`${graph.id} needs code function(s) ${missingCode.join(", ")}; pass them in code=`);
  const model: ModelNodes = new Map();
  for (const name of graph.order) {
    const node = graph.nodes.get(name) as GraphNode;
    if (node.call === null) {
      if (node.code === null) throw new GraphError(`${graph.id}: node ${name} has neither call nor code`);
      continue;
    }
    let fn;
    try {
      fn = jeff.catalogue.function(node.call);
    } catch (error) {
      if (error instanceof UnknownFunction) throw new GraphError(`${graph.id}: node ${name} calls ${node.call}: ${error.message}`);
      throw error;
    }
    if (!(await jeff.engine.installed()).has(fn.adapterId)) throw notInstalled(fn.id, `node ${name} of graph ${graph.id} calls it`);
    const context = Object.fromEntries([...node.context].map(([field, param]) => [field, params[param] as JSONValue]));
    // Python: `params[node.labels] if node.labels else None` (an empty param name gives no labels).
    const labels = node.labels ? params[node.labels] as unknown as Labels : null;
    const call: Call = { function: node.call, input, context: node.context.size > 0 ? context : null, labels };
    let binding: Binding;
    try {
      binding = await jeff.bindCall(call);
    } catch (error) {
      if (error instanceof NotLicensed) throw error; // a refused licence is the caller's, not a problem with the graph
      if (error instanceof JeffError) throw new GraphError(`${graph.id}: node ${name} (${fn.id}): ${error.message}`);
      throw error;
    }
    const policy = (Object.hasOwn(policies, name) ? policies[name] : node.onLowConfidence) as Policy;
    await checkPolicy(jeff, fn.interface, policy);
    model.set(name, [call, binding, policy]);
  }
  return model;
}

/** Runs a graph whose model nodes checkGraph resolved: each stage is every node whose dependencies have results, its
 * model nodes decided in one batch, then its code nodes called with every result so far. */
export async function runChecked(jeff: Jeff, graph: Graph, input: JSONValue, params: Record<string, JSONValue>,
  code: Record<string, CodeFunction>, model: ModelNodes): Promise<GraphResult> {
  const results = new Map<string, NodeResult>();
  let stages = 0;
  while (results.size < graph.nodes.size) {
    const ready = graph.order.filter((name) => !results.has(name)
      && [...dependencies(graph.nodes.get(name) as GraphNode)].every((dependency) => results.has(dependency)));
    if (ready.length === 0) {
      const waiting = pySorted([...graph.nodes.keys()].filter((name) => !results.has(name)));
      throw new GraphError(`${graph.id} makes no progress: node(s) ${waiting.join(", ")} wait on each other or on `
        + "nodes the graph does not have");
    }
    stages += 1;
    const batch: string[] = [];
    const coded: Array<[string, string]> = [];
    for (const name of ready) {
      const node = graph.nodes.get(name) as GraphNode;
      const skipped = pySorted([...dependencies(node)].filter((dependency) => results.get(dependency) instanceof Skipped));
      if (skipped.length > 0) results.set(name, new Skipped(`depends on skipped node ${skipped[0]}`));
      else if (node.when !== null && !holds(node.when, results)) results.set(name, new Skipped("condition not met"));
      else if (model.has(name)) batch.push(name);
      else if (node.code !== null) coded.push([name, node.code]);
      else throw new GraphError(`${graph.id}: node ${name} has neither call nor code`);
    }
    if (batch.length > 0) {
      const decisions = await jeff.decideBound(batch.map((name) => {
        const [call, binding] = model.get(name) as readonly [Call, Binding, Policy];
        return [call, binding] as const;
      }));
      for (const [index, name] of batch.entries()) {
        const [call, , policy] = model.get(name) as readonly [Call, Binding, Policy];
        results.set(name, await jeff.applyPolicy(call, decisions[index] as Decision, policy));
      }
    }
    for (const [name, codeName] of coded) {
      const fn = code[codeName] as CodeFunction;
      const result = await fn({ input, params: { ...params }, nodes: Object.fromEntries(results) });
      // Python stores a code function's None as a legitimate result (binding.JSON includes None); `null` is its JS
      // equivalent and is stored the same way. `undefined` has no Python equivalent (JSON.stringify drops it, and it
      // is almost always a function that forgot to return), so it is refused here rather than stored or silently
      // turned into null.
      if (result === undefined) throw new GraphError(`${graph.id}: code function ${codeName} (node ${name}) returned undefined`);
      results.set(name, result);
    }
  }
  return new GraphResult(new Map(results), new Map(graph.outputs.map((name) => [name, results.get(name) as NodeResult])), stages);
}

export async function runGraph(jeff: Jeff, graph: Graph, input: JSONValue, params: Record<string, JSONValue>,
  code: Record<string, CodeFunction> = {}, policies: Record<string, Policy> = {}): Promise<GraphResult> {
  return runChecked(jeff, graph, input, params, code, await checkGraph(jeff, graph, input, params, code, policies));
}
