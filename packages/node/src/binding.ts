/** Binding a function to its context and labels, and building the row the model reads (port of jeff/binding.py).
 * Context and labels come before the input, so one binding gives one fixed prompt prefix the engine can reuse. The
 * row's state and criteria are Maps: their keys keep the trained order even when they look like numbers. */

import type { QuestionType } from "./contract.js";
import { InvalidLabels, InvalidRequest, TooManyLabels, UnknownContext, missingContext, missingLabels } from "./errors.js";
import type { Calibration, FunctionSpec, InputField, Manifest } from "./manifest.js";
import { PY_SPACE, pyRepr, pySorted, pyTypeName } from "./pyformat.js";
import type { JSONValue } from "./types.js";

export type Context = Readonly<Record<string, JSONValue>>;
/** Key -> description (caller and describe labels), or a list of keys (subset labels). */
export type Labels = Readonly<Record<string, string | null>> | readonly string[];

export interface Call {
  readonly function: string;
  readonly input: JSONValue;
  readonly context?: Context | null;
  readonly labels?: Labels | null;
}

export interface RowQuestion {
  type: QuestionType;
  instructions: string;
  criteria?: Map<string, string | null> | string[];
}

export interface Row {
  state: Map<string, JSONValue> | string;
  question: RowQuestion;
}

export interface Binding {
  readonly function: FunctionSpec;
  readonly manifest: Manifest;
  readonly context: ReadonlyMap<string, JSONValue>;
  readonly criteria: ReadonlyMap<string, string | null> | null;
  readonly wireToCaller: ReadonlyMap<string, string>;
  /** The threshold block on the engine's base format, or null when the function is uncalibrated there. */
  readonly calibration: Calibration | null;
}

// Python's re: \s and \d match Unicode whitespace and decimal digits in a str pattern.
const D = "\\p{Nd}";
const NUMBER_LIKE = new RegExp(`^[${PY_SPACE}]*[+-]?(${D}+\\.?${D}*|\\.${D}+)([eE][+-]?${D}+)?[${PY_SPACE}]*$`, "u");

function isObject(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) && !(value instanceof Map);
}

/** The caller's context, given by code name, keyed by each field's exact key, in the order of the function's fields. */
function contextOf(fn: FunctionSpec, given: Context | null | undefined): Map<string, JSONValue> {
  const values: Context = given ?? {};
  const names = Object.keys(values);
  if (fn.input.form === "text" && names.length > 0) {
    throw new UnknownContext(`${fn.id} takes plain text only (its input ${fn.input.codeName} is the whole state the `
      + "adapter sees), so it has no context fields; do not pass context "
      + `(got ${pySorted(names).join(", ")})`);
  }
  const known = new Set(fn.context.map((field) => field.codeName));
  const unknown = pySorted(names.filter((name) => !known.has(name)));
  if (unknown.length > 0) {
    const codeOf = new Map(fn.context.map((field) => [field.key, field.codeName]));
    const exact = unknown.filter((key) => codeOf.has(key)).map((key) => `${pyRepr(key)} as ${codeOf.get(key)}`);
    throw new UnknownContext(`${fn.id} has no context field(s) ${unknown.join(", ")}; its fields are `
      + `${fn.context.map((field) => field.codeName).join(", ") || "none"}`
      + (exact.length > 0 ? `; context is given by code name, so pass ${exact.join(", ")}` : ""));
  }
  const missing = fn.context.filter((field) => field.required && !Object.hasOwn(values, field.codeName))
    .map((field) => field.codeName);
  if (missing.length > 0) throw missingContext(fn.id, missing);
  const bound = new Map<string, JSONValue>();
  for (const field of fn.context) {
    if (Object.hasOwn(values, field.codeName)) bound.set(field.key, values[field.codeName] as JSONValue);
  }
  return bound;
}

/** labels= given as a list of key strings; anything else is InvalidLabels with `refusal`. */
function labelKeys(labels: Labels, refusal: string): string[] {
  if (!Array.isArray(labels) || !labels.every((key) => typeof key === "string")) throw new InvalidLabels(refusal);
  return [...labels];
}

type Criteria = [Map<string, string | null>, Map<string, string>];

function identity(keys: Iterable<string>): Map<string, string> {
  return new Map([...keys].map((key) => [key, key]));
}

function repeated(keys: string[]): string[] {
  return pySorted(new Set(keys.filter((key, index) => keys.indexOf(key) !== index)));
}

function fixedDescription(spec: FunctionSpec["labels"], key: string): string | null {
  const description = spec.fixed.get(key);
  if (description === undefined) throw new Error(`label ${pyRepr(key)} is not among the fixed labels`);
  return description;
}

function subsetCriteria(fn: FunctionSpec, labels: Labels): Criteria {
  const spec = fn.labels;
  const fixed = [...spec.fixed.keys()].join(", ");
  const keys = labelKeys(labels, `${fn.id} offers a choice of its own labels; pass labels= as a list of keys from ${fixed}`);
  if (keys.length === 0) throw missingLabels(fn.id);
  const twice = repeated(keys);
  if (twice.length > 0) throw new InvalidLabels(`${fn.id}: label(s) ${twice.join(", ")} given more than once`);
  const unknown = keys.filter((key) => !spec.fixed.has(key));
  if (unknown.length > 0) throw new InvalidLabels(`${fn.id}: label(s) ${unknown.join(", ")} are not among its labels ${fixed}`);
  const missing = spec.required.filter((key) => !keys.includes(key));
  if (missing.length > 0) throw new InvalidLabels(`${fn.id}: label(s) ${missing.join(", ")} must always be offered`);
  return [new Map(keys.map((key) => [key, fixedDescription(spec, key)])), identity(keys)];
}

/** Fixed labels with subset true: the caller's keys in the manifest's order, each with its fixed description. */
function fixedSubsetCriteria(fn: FunctionSpec, labels: Labels): Criteria {
  const spec = fn.labels;
  const fixed = [...spec.fixed.keys()].join(", ");
  const keys = labelKeys(labels, `${fn.id} has fixed labels and may offer some of them; pass labels= as a list of keys `
    + `from ${fixed}, or no labels= to offer them all`);
  if (keys.length === 0) {
    throw new InvalidLabels(`${fn.id}: labels= is an empty list; pass at least one key from ${fixed}, or no labels= to `
      + "offer them all");
  }
  const twice = repeated(keys);
  if (twice.length > 0) throw new InvalidLabels(`${fn.id}: label(s) ${twice.join(", ")} given more than once`);
  const unknown = keys.filter((key) => !spec.fixed.has(key));
  if (unknown.length > 0) throw new InvalidLabels(`${fn.id}: label(s) ${unknown.join(", ")} are not among its labels ${fixed}`);
  const offered = new Set(keys);
  const criteria = new Map([...spec.fixed].filter(([key]) => offered.has(key)));
  return [criteria, identity(criteria.keys())];
}

function describeCriteria(fn: FunctionSpec, labels: Labels): Criteria {
  const spec = fn.labels;
  if (!isObject(labels)) {
    throw new InvalidLabels(`${fn.id}: pass labels= as an object with exactly the keys ${spec.keys.join(", ")}, each with `
      + "its description");
  }
  const given = labels as Record<string, string | null>;
  const extra = Object.keys(given).filter((key) => !spec.keys.includes(key));
  const missing = spec.keys.filter((key) => !Object.hasOwn(given, key));
  if (extra.length > 0 || missing.length > 0) {
    throw new InvalidLabels(`${fn.id} takes exactly the labels ${spec.keys.join(", ")}`
      + (extra.length > 0 ? `; unknown: ${extra.join(", ")}` : "")
      + (missing.length > 0 ? `; missing: ${missing.join(", ")}` : ""));
  }
  return [new Map(spec.keys.map((key) => [key, given[key] as string | null])), identity(spec.keys)];
}

function choiceCriteria(fn: FunctionSpec, labels: Labels | null | undefined): Criteria {
  const spec = fn.labels;
  if (spec.source === "fixed") {
    if (labels === null || labels === undefined) return [new Map(spec.fixed), identity(spec.fixed.keys())];
    if (!spec.subset) throw new InvalidLabels(`${fn.id} has fixed labels (${[...spec.fixed.keys()].join(", ")}); do not pass labels=`);
    return fixedSubsetCriteria(fn, labels);
  }
  if (labels === null || labels === undefined) throw missingLabels(fn.id);
  if (spec.source === "subset") return subsetCriteria(fn, labels);
  if (spec.source === "describe") return describeCriteria(fn, labels);
  if (!isObject(labels)) throw new InvalidLabels(`${fn.id}: pass labels= as an object of key -> description`);
  const caller = Object.entries(labels as Record<string, string | null>);
  if (caller.length === 0) throw missingLabels(fn.id);
  if (spec.max !== null && caller.length > spec.max) {
    throw new TooManyLabels(`${fn.id} got ${caller.length} labels; it was trained on at most ${spec.max}`);
  }
  for (const [key] of caller) {
    if (spec.fixedFirst.has(key)) {
      throw new InvalidLabels(`${fn.id}: label ${pyRepr(key)} is reserved (always listed first: `
        + `${[...spec.fixedFirst.keys()].join(", ")}); rename yours`);
    }
    if (NUMBER_LIKE.test(key)) throw new InvalidLabels(`${fn.id}: label key ${pyRepr(key)} looks like a number; use a word`);
  }
  const criteria = new Map(spec.fixedFirst);
  const wireToCaller = identity(spec.fixedFirst.keys());
  caller.forEach(([key, description], index) => {
    const wire = spec.callerKeys !== null ? spec.callerKeys.replace("{n}", String(index + 1)) : key;
    if (criteria.has(wire)) throw new InvalidLabels(`${fn.id}: label ${pyRepr(key)} collides with option ${pyRepr(wire)}`);
    criteria.set(wire, description);
    wireToCaller.set(wire, key);
  });
  return [criteria, wireToCaller];
}

/** calibration: the block the function decides with on the engine's base format (calibrationFor), resolved before
 * binding, or null when it is uncalibrated there. */
export function bind(manifest: Manifest, fn: FunctionSpec, context: Context | null | undefined,
  labels: Labels | null | undefined, calibration: Calibration | null): Binding {
  const boundContext = contextOf(fn, context);
  if (fn.type === "choice") {
    const [criteria, wireToCaller] = choiceCriteria(fn, labels);
    return { function: fn, manifest, context: boundContext, criteria, wireToCaller, calibration };
  }
  if (labels !== null && labels !== undefined) throw new InvalidLabels(`${fn.id} is a ${fn.type} function; it takes no labels=`);
  return { function: fn, manifest, context: boundContext, criteria: null, wireToCaller: identity(optionKeysFor(fn)), calibration };
}

export function optionKeysFor(fn: FunctionSpec): string[] {
  if (fn.type === "noul") return ["false", "true"];
  if (fn.type === "score") return fn.labels.levels.map((_, index) => String(index));
  throw new Error("choice functions get their option keys from a binding");
}

export function optionKeys(binding: Binding): string[] {
  return binding.criteria !== null ? [...binding.criteria.keys()] : optionKeysFor(binding.function);
}

/** The state the model reads: form fields, the bound context then the input under its key; form text, the input. */
export function requestState(functionId: string, spec: InputField, context: ReadonlyMap<string, JSONValue>,
  input: JSONValue): Map<string, JSONValue> | string {
  if (spec.form === "fields") return new Map([...context, [spec.key, input]]);
  if (typeof input !== "string") {
    throw new InvalidRequest(`${functionId} takes plain text: its input ${spec.codeName} must be a string, got ${pyTypeName(input)}`);
  }
  return input;
}

export function buildRow(binding: Binding, input: JSONValue): Row {
  const fn = binding.function;
  const state = requestState(fn.id, fn.input, binding.context, input);
  const question: RowQuestion = { type: fn.type, instructions: fn.instructions };
  if (fn.type === "choice") {
    if (binding.criteria === null) throw new Error(`the binding of choice function ${fn.id} has no criteria`);
    question.criteria = new Map(binding.criteria);
  } else if (fn.type === "score") question.criteria = [...fn.labels.levels];
  else if (fn.labels.fixed.size > 0) question.criteria = new Map(fn.labels.fixed);
  return { state, question };
}

function plainOf(value: unknown): unknown {
  if (value instanceof Map) return Object.fromEntries([...value].map(([key, item]) => [key, plainOf(item)]));
  return value;
}

/** The row with its Maps as plain objects, for display and comparison (an object may reorder number-like keys). */
export function rowToJSON(row: Row): Record<string, unknown> {
  const question: Record<string, unknown> = { type: row.question.type, instructions: row.question.instructions };
  if (row.question.criteria !== undefined) question.criteria = plainOf(row.question.criteria);
  return { state: plainOf(row.state), question };
}
