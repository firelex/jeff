/** The Jeff client: bind functions, decide in batches grouped by adapter, the interface shorthands, dispatch (port of
 * jeff/client.py). Every method that reaches an engine is async. */

import { type Binding, type Call, type Context, type Labels, type Row, bind, buildRow } from "./binding.js";
import { Catalogue } from "./catalogue.js";
import { type Decision, makeDecision } from "./decision.js";
import { type BaseFormat, type Engine, type EngineResult, formatIdOf, serves } from "./engine.js";
import { EngineError, InterfaceMismatch, InvalidRequest, notInstalled } from "./errors.js";
import { type Policy, applyPolicy, checkPolicy } from "./escalation.js";
import { type CodeFunction, type Graph, type GraphResult, graphFromDict, isGraph, loadGraph, runGraph } from "./graph.js";
import { type Calibration, type FunctionSpec, calibrationFor } from "./manifest.js";
import { pyRepr } from "./pyformat.js";
import { checkRouters, dispatch } from "./routing.js";
import { SocketEngine } from "./socket-engine.js";
import type { RemoteJeff, RemoteOptions, TokenSource } from "./remote.js";
import type { JSONValue } from "./types.js";

export interface CallOptions {
  context?: Context | null;
  labels?: Labels | null;
  /** What happens when the decision abstains: "return" it (when not given), raise LowConfidence ("error"), or
   * escalate([...]). */
  onLowConfidence?: Policy;
}
export interface ShorthandOptions extends CallOptions {
  /** The function to call; without it, the call is routed (dispatch) to a function implementing the interface. */
  function?: string;
}
export interface DispatchOptions extends CallOptions {
  interface: string;
  /** The router to start from; jeff.core.router when not given. */
  router?: string;
}

export interface LocalOptions {
  /** How long to wait for each reply from the engine, in milliseconds. */
  timeoutMs: number;
  /** The manifests calls are bound with; the catalogue shipped with the package when not given. */
  catalogue?: Catalogue;
}

/** The longest timeout Node's timers keep (2**31 - 1 ms, about 24.8 days); a longer one fires after 1 ms. */
export const MAX_TIMEOUT_MS = 2147483647;

export function checkTimeoutMs(value: unknown): void {
  if (typeof value !== "number" || !Number.isFinite(value) || value <= 0) {
    throw new InvalidRequest(`timeoutMs must be a finite number of milliseconds above 0; got ${pyRepr(value)}`);
  }
  if (value > MAX_TIMEOUT_MS) {
    throw new InvalidRequest(`timeoutMs must be at most ${MAX_TIMEOUT_MS} milliseconds (about 24.8 days), the longest `
      + `timeout Node keeps; got ${pyRepr(value)}`);
  }
}

export interface JeffParts {
  readonly engine: Engine;
  readonly catalogue: Catalogue;
  readonly formatId: string | null;
  readonly exactKeys: Map<string, Map<string, string>>;
}

/** What a Jeff needs, read once from its engine: the base format (picks each function's calibration) and the exact
 * keys of every installed router. */
export async function jeffParts(engine: Engine, catalogue: Catalogue): Promise<JeffParts> {
  const formatId = formatIdOf(engine.baseFormat);
  const exactKeys = checkRouters(catalogue, await engine.installed());
  return { engine, catalogue, formatId, exactKeys };
}

export class BoundFunction {
  protected constructor(readonly jeff: Jeff, protected readonly template: Call, protected readonly policy: Policy,
    protected readonly bound: Binding) {}

  /** Binds once, now: context, labels and the policy are checked here and the binding is reused by every call. */
  static async create(jeff: Jeff, template: Call, policy: Policy): Promise<BoundFunction> {
    const bound = await jeff.bindCall(template);
    await checkPolicy(jeff, bound.function.interface, policy);
    return new BoundFunction(jeff, template, policy, bound);
  }

  protected callOf(input: JSONValue): Call {
    return { function: this.template.function, input, context: this.template.context, labels: this.template.labels };
  }

  async call(input: JSONValue): Promise<Decision> {
    return (await this.many([input]))[0] as Decision;
  }

  async many(inputs: readonly JSONValue[]): Promise<Decision[]> {
    const calls = inputs.map((input) => this.callOf(input));
    const decisions = await this.jeff.decideBound(calls.map((call) => [call, this.bound] as const));
    const out: Decision[] = [];
    for (const [index, call] of calls.entries()) out.push(await this.jeff.applyPolicy(call, decisions[index] as Decision, this.policy));
    return out;
  }

  /** Releases what this bound function holds on a server: nothing here; a channel handle in ChannelJeff. */
  async close(): Promise<void> {}
}

export class Jeff {
  readonly engine: Engine;
  readonly catalogue: Catalogue;
  readonly baseFormat: BaseFormat;
  readonly formatId: string | null;
  /** Router ID -> the exact keys its tree refuses (routing.exactKeysOf). */
  readonly exactKeys: Map<string, Map<string, string>>;
  private readonly calibrations = new Map<string, Calibration | null>();

  protected constructor(parts: JeffParts) {
    this.engine = parts.engine;
    this.catalogue = parts.catalogue;
    this.baseFormat = { ...parts.engine.baseFormat };
    this.formatId = parts.formatId;
    this.exactKeys = parts.exactKeys;
  }

  static async create(engine: Engine, catalogue: Catalogue): Promise<Jeff> {
    return new Jeff(await jeffParts(engine, catalogue));
  }

  /** A Jeff over the engine already running on this machine at `socketPath` (start it with `jeff runtime serve`).
   * It never starts an engine and never falls back to a server: no engine there is an EngineError naming the path
   * and the command that starts one. */
  static async local(socketPath: string, options: LocalOptions): Promise<Jeff> {
    checkTimeoutMs(options.timeoutMs);
    const engine = await SocketEngine.connect(socketPath, { timeoutMs: options.timeoutMs });
    try {
      return await Jeff.create(engine, options.catalogue ?? Catalogue.shipped());
    } catch (error) {
      await engine.close();
      throw error;
    }
  }

  /** A Jeff over the Jeff server at `url` (our API or your own); `token` is sent as a bearer token (a function is
   * called before every request). The module is loaded here, not at the top of this file, because remote.ts extends
   * this class. */
  static async remote(url: string, token: TokenSource, options: RemoteOptions): Promise<RemoteJeff> {
    const { remote } = await import("./remote.js");
    return remote(url, token, options);
  }

  /** The calibration `fn` decides with on this Jeff's engine, resolved once per function. */
  calibrationOf(fn: FunctionSpec): Calibration | null {
    if (this.calibrations.has(fn.id)) return this.calibrations.get(fn.id) as Calibration | null;
    const calibration = this.resolveCalibrationOf(fn);
    this.calibrations.set(fn.id, calibration);
    return calibration;
  }

  resolveCalibrationOf(fn: FunctionSpec): Calibration | null {
    return calibrationFor(fn, this.formatId);
  }

  /** The one place a call is bound: its function resolved, its adapter checked as served, its calibration chosen. */
  async bindCall(call: Call): Promise<Binding> {
    const fn = this.catalogue.function(call.function);
    const manifest = this.catalogue.manifestOf(fn);
    if (!(await serves(this.engine, manifest.id))) throw notInstalled(fn.id);
    return bind(manifest, fn, call.context, call.labels, this.calibrationOf(fn));
  }

  async decide(calls: readonly Call[]): Promise<Decision[]> {
    const bound: Array<readonly [Call, Binding]> = [];
    for (const call of calls) bound.push([call, await this.bindCall(call)]);
    return this.decideBound(bound);
  }

  /** Bound calls, one predict per adapter (in the order the adapters are first met), sent together so the engine can
   * batch them; decisions in call order. A failure is reported as Python, which handles one adapter after another,
   * meets it first: each earlier adapter's predict and decisions come before a later adapter's rows are built. */
  async decideBound(bound: ReadonlyArray<readonly [Call, Binding]>): Promise<Decision[]> {
    const groups = new Map<string, number[]>();
    bound.forEach(([, binding], index) => groups.set(binding.manifest.id, [...(groups.get(binding.manifest.id) ?? []), index]));
    const pending: Array<Promise<EngineResult>> = [];
    let buildFailure: { error: unknown } | null = null;
    for (const [adapterId, indices] of groups) {
      let rows: Row[];
      try {
        rows = indices.map((index) => {
          const [call, binding] = bound[index] as readonly [Call, Binding];
          return buildRow(binding, call.input);
        });
      } catch (error) {
        buildFailure = { error };
        break;
      }
      pending.push(this.engine.predict(adapterId, rows));
    }
    const settled = await Promise.allSettled(pending);
    const results: Decision[] = new Array(bound.length);
    [...groups].slice(0, settled.length).forEach(([adapterId, indices], group) => {
      const outcome = settled[group] as PromiseSettledResult<EngineResult>;
      if (outcome.status === "rejected") throw outcome.reason;
      const output = outcome.value;
      if (output.probabilities.length !== indices.length) {
        throw new EngineError(`The engine answered ${output.probabilities.length} of ${indices.length} rows for ${adapterId}`);
      }
      indices.forEach((index, row) => {
        results[index] = makeDecision((bound[index] as readonly [Call, Binding])[1], output.probabilities[row] as number[], output.timing);
      });
    });
    if (buildFailure !== null) throw buildFailure.error;
    return results;
  }

  function(ref: string, options: CallOptions = {}): Promise<BoundFunction> {
    return BoundFunction.create(this, { function: ref, input: null, context: options.context, labels: options.labels },
      options.onLowConfidence === undefined ? "return" : options.onLowConfidence);
  }

  applyPolicy(call: Call, decision: Decision, policy: Policy): Promise<Decision> {
    return applyPolicy(this, call, decision, policy);
  }

  async interfaceCall(iface: string, input: JSONValue, options: ShorthandOptions): Promise<Decision> {
    const policy = options.onLowConfidence === undefined ? "return" : options.onLowConfidence;
    if (options.function === undefined) {
      return this.dispatch(input, { interface: iface, context: options.context, labels: options.labels, onLowConfidence: policy });
    }
    const spec = this.catalogue.function(options.function);
    if (spec.interface !== iface) {
      throw new InterfaceMismatch(`${spec.id} implements ${spec.interface}, not ${iface}; call jeff.${spec.interface}`);
    }
    await checkPolicy(this, iface, policy);
    const call: Call = { function: options.function, input, context: options.context, labels: options.labels };
    const [decision] = await this.decide([call]);
    return this.applyPolicy(call, decision as Decision, policy);
  }

  dispatch(input: JSONValue, options: DispatchOptions): Promise<Decision> {
    return dispatch(this, input, options);
  }

  classify(input: JSONValue, options: ShorthandOptions = {}) { return this.interfaceCall("classify", input, options); }
  route(input: JSONValue, options: ShorthandOptions = {}) { return this.interfaceCall("route", input, options); }
  rate(input: JSONValue, options: ShorthandOptions = {}) { return this.interfaceCall("rate", input, options); }
  detect(input: JSONValue, options: ShorthandOptions = {}) { return this.interfaceCall("detect", input, options); }
  isSupported(input: JSONValue, options: ShorthandOptions = {}) { return this.interfaceCall("is_supported", input, options); }
  equivalent(input: JSONValue, options: ShorthandOptions = {}) { return this.interfaceCall("equivalent", input, options); }
  requiresEscalation(input: JSONValue, options: ShorthandOptions = {}) {
    return this.interfaceCall("requires_escalation", input, options);
  }

  /** Runs a graph: a Graph, a graph file's path, or a graph document (as Maps to keep the order of node names that
   * look like numbers). code: the code nodes' functions by name; policies: a policy per node, in place of the
   * document's 'return' or 'error'. */
  runGraph(graph: Graph | string | Record<string, unknown> | ReadonlyMap<string, unknown>, input: JSONValue,
    params: Record<string, JSONValue>, code: Record<string, CodeFunction> = {},
    policies: Record<string, Policy> = {}): Promise<GraphResult> {
    return runGraph(this, Jeff.graphOf(graph), input, params, code, policies);
  }

  protected static graphOf(graph: Graph | string | Record<string, unknown> | ReadonlyMap<string, unknown>): Graph {
    if (typeof graph === "string") return loadGraph(graph);
    if (isGraph(graph)) return graph;
    return graphFromDict(graph, "the graph given to runGraph");
  }

  async close(): Promise<void> {
    await this.engine.close?.();
  }
}
