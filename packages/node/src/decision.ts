/** The Decision every function returns (port of jeff/decision.py). */

import { type Binding, optionKeys } from "./binding.js";
import { EngineError } from "./errors.js";
import { pyFloat, pySum } from "./pyformat.js";

export type Value = string | boolean | number;

export interface RouteStep { readonly router: string; readonly chose: string; readonly confidence: number }
export interface EscalationRecord {
  readonly decidedBy: string;
  readonly value: Value;
  readonly confidence: number | null;
  readonly reason: string;
}

/** A decision as the contract's schema writes it (snake_case), on the wire and in fixtures. */
export interface DecisionJSON {
  value: Value;
  confidence: number | null;
  abstained: boolean;
  calibrated: boolean;
  interface: string;
  function: string;
  version: string;
  probabilities: Record<string, number>;
  score: number | null;
  route: Array<{ router: string; chose: string; confidence: number }>;
  decided_by: string;
  escalations: Array<{ decided_by: string; value: Value; confidence: number | null; reason: string }>;
  timing: Record<string, unknown>;
  evidence_refs: string[];
}

export interface DecisionFields {
  readonly value: Value;
  readonly confidence: number | null;
  readonly abstained: boolean;
  readonly calibrated: boolean;
  readonly interface: string;
  readonly function: string;
  readonly version: string;
  readonly probabilities: Readonly<Record<string, number>>;
  readonly score: number | null;
  readonly route: readonly RouteStep[];
  readonly decidedBy: string;
  readonly escalations: readonly EscalationRecord[];
  readonly timing: Readonly<Record<string, unknown>>;
  readonly evidenceRefs: readonly string[];
}

/** `probabilities` is a plain object keyed by option label. When an option label looks like a whole number
 * (`"2"`, `"10"`), JavaScript always enumerates that key before every other key, in ascending numeric order,
 * regardless of where the engine reported it -- so iterating this object (`Object.keys`, `for...in`,
 * `JSON.stringify`) does not necessarily list the options in the function's own option order the way Python's dict
 * does. The values themselves are unaffected; read the function's option order (binding.optionKeys) when the order
 * matters, never this object's own key order. */
export class Decision implements DecisionFields {
  readonly value: Value;
  readonly confidence: number | null;
  readonly abstained: boolean;
  readonly calibrated: boolean;
  readonly interface: string;
  readonly function: string;
  readonly version: string;
  readonly probabilities: Readonly<Record<string, number>>;
  readonly score: number | null;
  readonly route: readonly RouteStep[];
  readonly decidedBy: string;
  readonly escalations: readonly EscalationRecord[];
  readonly timing: Readonly<Record<string, unknown>>;
  readonly evidenceRefs: readonly string[];

  constructor(fields: DecisionFields) {
    this.value = fields.value;
    this.confidence = fields.confidence;
    this.abstained = fields.abstained;
    this.calibrated = fields.calibrated;
    this.interface = fields.interface;
    this.function = fields.function;
    this.version = fields.version;
    this.probabilities = { ...fields.probabilities };
    this.score = fields.score;
    this.route = fields.route.map((step) => ({ ...step }));
    this.decidedBy = fields.decidedBy;
    this.escalations = fields.escalations.map((item) => ({ ...item }));
    this.timing = { ...fields.timing };
    this.evidenceRefs = [...fields.evidenceRefs];
    Object.freeze(this);
  }

  /** A copy with some fields changed (Python's dataclasses.replace). */
  with(changes: Partial<DecisionFields>): Decision {
    return new Decision({ ...this, ...changes });
  }

  toJSON(): DecisionJSON {
    return {
      value: this.value, confidence: this.confidence, abstained: this.abstained, calibrated: this.calibrated,
      interface: this.interface, function: this.function, version: this.version,
      probabilities: { ...this.probabilities }, score: this.score,
      route: this.route.map((step) => ({ router: step.router, chose: step.chose, confidence: step.confidence })),
      decided_by: this.decidedBy,
      escalations: this.escalations.map((item) => ({ decided_by: item.decidedBy, value: item.value,
        confidence: item.confidence, reason: item.reason })),
      timing: { ...this.timing }, evidence_refs: [...this.evidenceRefs],
    };
  }

  static fromJSON(data: DecisionJSON): Decision {
    return new Decision({
      value: data.value, confidence: data.confidence, abstained: data.abstained, calibrated: data.calibrated,
      interface: data.interface, function: data.function, version: data.version, probabilities: data.probabilities,
      score: data.score,
      route: data.route.map((step) => ({ router: step.router, chose: step.chose, confidence: step.confidence })),
      decidedBy: data.decided_by,
      escalations: data.escalations.map((item) => ({ decidedBy: item.decided_by, value: item.value,
        confidence: item.confidence, reason: item.reason })),
      timing: data.timing, evidenceRefs: data.evidence_refs,
    });
  }
}

/** How far from 1 the sum of an engine reply's probabilities may be (contract/socket.md, "The probabilities of a
 * predict"): the engine's float32 softmax over at most 255 options is off by at most about 1.5e-5, and its own tests
 * hold it to 1e-4. SUM_TOLERANCE in jeff/decision.py. */
export const SUM_TOLERANCE = 1e-4;

/** The decision for one row, refusing with EngineError, never repairing, a reply that breaks the engine's contract:
 * the wrong number of probabilities, any that is not a finite number from 0 to 1, or a sum more than SUM_TOLERANCE
 * from 1. A valid reply: probabilities normalised with Python's sum, the winner the first best option in option
 * order (a Map, never an object's key order, which puts number-like keys first), abstained below the calibration
 * threshold or on an abstain label. */
export function makeDecision(binding: Binding, probabilities: readonly number[],
  timing: Readonly<Record<string, unknown>>): Decision {
  const fn = binding.function;
  const manifest = binding.manifest;
  const keys = optionKeys(binding);
  if (probabilities.length !== keys.length) {
    throw new EngineError(`${fn.id}: the engine returned ${probabilities.length} probabilities for ${keys.length} options`);
  }
  const shown = `[${probabilities.map(pyFloat).join(", ")}]`;
  const offending = probabilities.filter((p) => !(Number.isFinite(p) && p >= 0 && p <= 1));
  if (offending.length > 0) {
    throw new EngineError(`${fn.id}: the engine returned invalid probabilities ${shown}; expected each to be a finite `
      + `number from 0 to 1, which [${offending.map(pyFloat).join(", ")}] are not`);
  }
  const total = pySum(probabilities);
  if (Math.abs(total - 1) > SUM_TOLERANCE) {
    throw new EngineError(`${fn.id}: the engine returned invalid probabilities ${shown}; expected their sum to be `
      + `within ${pyFloat(SUM_TOLERANCE)} of 1, got ${pyFloat(total)}`);
  }
  const normalised = probabilities.map((p) => p / total);
  const caller = new Map<string, number>();
  keys.forEach((key, index) => {
    const name = binding.wireToCaller.get(key);
    if (name === undefined) throw new EngineError(`${fn.id}: the binding has no caller key for option ${key}`);
    caller.set(name, normalised[index] as number);
  });
  let value: Value;
  let confidence: number;
  let score: number | null = null;
  if (fn.type === "noul") {
    const yes = caller.get("true") as number;
    value = yes >= 0.5;
    confidence = value ? yes : caller.get("false") as number;
  } else if (fn.type === "score") {
    let best = 0;
    normalised.forEach((p, index) => { if (p > (normalised[best] as number)) best = index; });
    value = best;
    confidence = normalised[best] as number;
    score = pySum(normalised.map((p, index) => index * p));
  } else {
    let bestKey: string | null = null;
    let bestP = -Infinity;
    for (const [key, p] of caller) if (bestKey === null || p > bestP) { bestKey = key; bestP = p; }
    value = bestKey as string;
    confidence = bestP;
  }
  const calibration = binding.calibration;
  const abstained = (calibration !== null && confidence < calibration.threshold)
    || (typeof value === "string" && fn.abstainLabels.includes(value));
  return new Decision({
    value, confidence, abstained, calibrated: calibration !== null, interface: fn.interface, function: fn.id,
    version: manifest.version, probabilities: Object.fromEntries(caller), score, route: [],
    decidedBy: `${fn.id}@${manifest.version}`, escalations: [], timing: { ...timing }, evidenceRefs: [],
  });
}
