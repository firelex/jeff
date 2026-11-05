/** What happens when a decision abstains: return it, raise, or escalate through explicit steps (port of
 * jeff/escalation.py). An escalated decision says who answered and lists every earlier attempt; a failing step
 * raises EscalationFailed. */

import { type Call, buildRow, optionKeys } from "./binding.js";
import type { Jeff } from "./client.js";
import type { Decision, EscalationRecord, Value } from "./decision.js";
import {
  EscalationBudgetExceeded, EscalationFailed, InterfaceMismatch, InvalidPolicy, UnknownFunction, escalationFailed,
  lowConfidence, notInstalled,
} from "./errors.js";
import { pyFixed, pyFloat, pyRepr, pyTypeName } from "./pyformat.js";
import type { JSONValue } from "./types.js";

/** What an llm or human step is shown: the function, its instructions, the state the model read (named fields, or the
 * plain input string for a text input) and the option keys it may answer with, each with its description.
 *
 * `state` and `options` are plain objects, and a JavaScript object lists keys that look like whole numbers (such as
 * "10") before all others. When you build a prompt from a question, build it from `stateEntries` and `optionEntries`:
 * they hold the same fields in the order the model was trained on and reads them (the Python SDK's order). */
export interface Question {
  readonly function: string;
  readonly instructions: string;
  readonly state: Record<string, JSONValue> | string;
  readonly options: Record<string, string | null>;
  /** The state's fields in the order the model reads them; null when the state is a plain string. */
  readonly stateEntries: Array<[string, JSONValue]> | null;
  /** The options in option order: each key the step may answer with, and its description. */
  readonly optionEntries: Array<[string, string | null]>;
}

export type Ask = (question: Question) => string | Promise<string>;

export interface Decider {
  decide(calls: Call[]): Promise<Decision[]>;
}

export class FunctionStep { readonly name = "function"; constructor(readonly ref: string) {} }
export class RemoteStep { readonly name = "remote"; constructor(readonly client: Decider) {} }
export class AskStep { constructor(readonly ask: Ask, readonly name: "llm" | "human") {} }
export type Step = FunctionStep | RemoteStep | AskStep;

export class Escalate {
  constructor(readonly steps: readonly Step[], readonly budgetMs: number | null) {}
}

export type Policy = "return" | "error" | Escalate;

export function functionStep(ref: string): FunctionStep { return new FunctionStep(ref); }
export function remote(client: Decider): RemoteStep { return new RemoteStep(client); }
export function llm(ask: Ask): AskStep { return new AskStep(ask, "llm"); }
export function human(ask: Ask): AskStep { return new AskStep(ask, "human"); }

/** budgetMs: the longest the whole escalation may take; without it, no limit. */
export function escalate(steps: readonly Step[], options: { budgetMs?: number } = {}): Escalate {
  if (steps.length === 0) throw new InvalidPolicy("escalate() needs at least one step");
  return new Escalate([...steps], options.budgetMs === undefined ? null : options.budgetMs);
}

export function unknownPolicy(policy: unknown): InvalidPolicy {
  return new InvalidPolicy(`Unknown on_low_confidence policy ${pyRepr(policy)}; use 'return', 'error' or escalate(...)`);
}

function notAStep(step: unknown): InvalidPolicy {
  return new InvalidPolicy(`escalate() got ${pyRepr(step)}, which is not a step; use functionStep(...), remote(...), `
    + "llm(...) or human(...)");
}

/** Checks a policy before anything is decided: every function step is in the catalogue, installed, and implements
 * the interface the call is answered through. */
export async function checkPolicy(jeff: Jeff, iface: string, policy: unknown): Promise<void> {
  if (policy === "return" || policy === "error") return;
  if (!(policy instanceof Escalate)) throw unknownPolicy(policy);
  for (const step of policy.steps) {
    if (step instanceof RemoteStep || step instanceof AskStep) continue;
    if (!(step instanceof FunctionStep)) throw notAStep(step);
    let spec;
    try {
      spec = jeff.catalogue.function(step.ref);
    } catch (error) {
      if (error instanceof UnknownFunction) {
        throw new InvalidPolicy(`Escalation step function_step(${pyRepr(step.ref)}): ${error.message}`);
      }
      throw error;
    }
    if (!(await jeff.engine.installed()).has(spec.adapterId)) throw notInstalled(spec.id, "it is an escalation step");
    if (spec.interface !== iface) throw new InterfaceMismatch(`Escalation step ${spec.id} implements ${spec.interface}, not ${iface}`);
  }
}

function reason(decision: Decision, threshold: number | null): string {
  if (threshold !== null && decision.confidence !== null && decision.confidence < threshold) {
    return `abstained (confidence ${pyFixed(decision.confidence, 3)} < threshold ${pyFloat(threshold)})`;
  }
  return "abstained (abstain label)";
}

function record(jeff: Jeff, decision: Decision): EscalationRecord {
  const calibration = jeff.calibrationOf(jeff.catalogue.function(decision.function));
  return { decidedBy: decision.decidedBy, value: decision.value, confidence: decision.confidence,
    reason: reason(decision, calibration ? calibration.threshold : null) };
}

/** The question an llm or human step is shown, and its option keys in option order (Question.options is an object,
 * which would list number-like keys first). */
async function questionOf(jeff: Jeff, call: Call): Promise<[Question, string[]]> {
  const binding = await jeff.bindCall(call);
  const fn = binding.function;
  const row = buildRow(binding, call.input);
  let options: Array<[string, string | null]>;
  if (fn.type === "choice") {
    if (binding.criteria === null) throw new Error(`the binding of choice function ${fn.id} has no criteria`);
    const criteria = binding.criteria;
    options = optionKeys(binding).map((key) => {
      const caller = binding.wireToCaller.get(key);
      if (caller === undefined) throw new Error(`the binding of ${fn.id} has no caller key for option ${key}`);
      if (!criteria.has(key)) throw new Error(`the binding of ${fn.id} has no description for option ${key}`);
      return [caller, criteria.get(key) as string | null];
    });
  } else if (fn.type === "score") {
    options = fn.labels.levels.map((level, index) => [String(index), level]);
  } else {
    // the wording the engine shows the model when a noul function does not describe its answers
    const described = fn.labels.fixed;
    options = [["true", described.has("true") ? described.get("true") as string | null : "Yes / true"],
      ["false", described.has("false") ? described.get("false") as string | null : "No / false"]];
  }
  const stateEntries = typeof row.state === "string" ? null : [...row.state];
  const state = stateEntries === null ? row.state as string : Object.fromEntries(stateEntries);
  return [{ function: fn.id, instructions: fn.instructions, state, options: Object.fromEntries(options), stateEntries,
    optionEntries: options.map(([key, description]) => [key, description] as [string, string | null]) },
    options.map(([key]) => key)];
}

function converted(type: string, answer: string): Value {
  if (type === "noul") return answer === "true";
  if (type === "score") return Number.parseInt(answer, 10);
  return answer;
}

async function runStep(jeff: Jeff, call: Call, original: Decision, step: Step): Promise<Decision> {
  if (step instanceof FunctionStep) {
    const spec = jeff.catalogue.function(step.ref);
    if (spec.interface !== original.interface) {
      throw new InterfaceMismatch(`Escalation step ${spec.id} implements ${spec.interface}, not ${original.interface}`);
    }
    const [decision] = await jeff.decide([{ function: step.ref, input: call.input, context: call.context, labels: call.labels }]);
    return decision as Decision;
  }
  if (step instanceof RemoteStep) return (await step.client.decide([call]))[0] as Decision;
  const [question, keys] = await questionOf(jeff, call);
  const started = performance.now();
  const answer: unknown = await step.ask(question);
  const elapsed = Math.round((performance.now() - started) * 1000) / 1000;
  if (typeof answer !== "string" || !keys.includes(answer)) {
    throw escalationFailed(step.name, `answered ${pyRepr(answer)}, which is not one of ${pyRepr(keys)}`);
  }
  const fn = jeff.catalogue.function(call.function);
  return original.with({ value: converted(fn.type, answer), confidence: null, abstained: false, calibrated: false,
    probabilities: {}, score: null, decidedBy: step.name, escalations: [], timing: { path: step.name, ms: elapsed } });
}

export async function applyPolicy(jeff: Jeff, call: Call, decision: Decision, policy: Policy): Promise<Decision> {
  if (!decision.abstained || policy === "return") return decision;
  if (policy === "error") throw lowConfidence(decision);
  if (!(policy instanceof Escalate)) throw unknownPolicy(policy);
  const started = performance.now();
  const attempts = [record(jeff, decision)];
  let latest = decision;
  for (const step of policy.steps) {
    try {
      latest = await runStep(jeff, call, decision, step);
    } catch (error) {
      if (error instanceof EscalationFailed) throw error;
      const name = error instanceof Error ? error.name : pyTypeName(error);
      const message = error instanceof Error ? error.message : String(error);
      throw escalationFailed(step.name, `${name}: ${message}`);
    }
    if (policy.budgetMs !== null && performance.now() - started > policy.budgetMs) {
      throw new EscalationBudgetExceeded(`Escalation of ${decision.function} took longer than ${pyRepr(policy.budgetMs)} ms`);
    }
    if (!latest.abstained) return latest.with({ route: decision.route, escalations: attempts });
    attempts.push(record(jeff, latest));
  }
  return latest.with({ route: decision.route, escalations: attempts.slice(0, -1) });
}
