/** The contract shipped with the package: JSON Schemas, the interface catalogue and the error catalogue (copied from
 * the repository's contract/ by scripts/sync-shipped.ts). */

import { existsSync, readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { join } from "node:path";
import type { Ajv2020 as AjvType } from "ajv/dist/2020.js";
import type { ValidateFunction } from "ajv";
import { type Node as YamlNode, isAlias, isMap, isPair, isScalar, isSeq, parseDocument } from "yaml";
import { ContractError, ManifestError } from "./errors.js";
import { pyRepr } from "./pyformat.js";
import { shippedDir } from "./shipped.js";

const require = createRequire(import.meta.url);
const Ajv2020 = require("ajv/dist/2020.js") as typeof AjvType;

export const SCHEMAS = [
  "manifest", "decision", "graph", "graph_result", "socket", "decide_request", "decide_response",
  "functions_response", "graphs_response", "systemone_request", "systemone_response", "models_response",
  "served_request", "adapters_response", "health_response", "login_request", "login_response",
  "accounts_keys", "accounts_usage",
] as const;
export type SchemaName = (typeof SCHEMAS)[number];
export type QuestionType = "choice" | "noul" | "score";

const schemas = new Map<SchemaName, Record<string, unknown>>();
let registry: InstanceType<typeof AjvType> | null = null;
const validators = new Map<string, ValidateFunction>();

export function loadSchema(name: SchemaName): Record<string, unknown> {
  const cached = schemas.get(name);
  if (cached !== undefined) return cached;
  const path = join(shippedDir(), "contract", "schemas", `${name}.schema.json`);
  if (!existsSync(path)) throw new ContractError(`Schema ${path} is missing; run pnpm --filter jeff-sdk sync`);
  const schema = JSON.parse(readFileSync(path, "utf8")) as Record<string, unknown>;
  schemas.set(name, schema);
  return schema;
}

function ajv(): InstanceType<typeof AjvType> {
  if (registry === null) {
    const created = new Ajv2020({ strict: false, allErrors: true });
    for (const name of SCHEMAS) created.addSchema(loadSchema(name));
    registry = created;
  }
  return registry;
}

function validator(name: SchemaName, definition?: string): ValidateFunction {
  const key = definition === undefined ? name : `${name}#${definition}`;
  const cached = validators.get(key);
  if (cached !== undefined) return cached;
  const schema = loadSchema(name);
  if (definition !== undefined && !Object.hasOwn((schema.$defs ?? {}) as object, definition)) {
    throw new ContractError(`The ${name} schema has no definition ${pyRepr(definition)}`);
  }
  const ref = definition === undefined ? String(schema.$id) : `${String(schema.$id)}#/$defs/${definition}`;
  const found = ajv().getSchema(ref);
  if (found === undefined) throw new ContractError(`The ${name} schema could not be compiled (${ref})`);
  validators.set(key, found);
  return found;
}

export function validate(name: SchemaName, document: unknown, where: string, definition?: string): void {
  const check = validator(name, definition);
  if (check(document)) return;
  const lines = (check.errors ?? []).map((problem) => `  at ${problem.instancePath || "/"}: ${problem.message ?? "invalid"}`);
  const shape = definition === undefined ? `the ${name} schema` : `${definition} in the ${name} schema`;
  throw new ContractError(`${where} does not match ${shape}:\n${lines.join("\n")}`);
}

export function isValid(name: SchemaName, document: unknown, definition?: string): boolean {
  return validator(name, definition)(document) as boolean;
}

// ---- firstProblem: ports jeff.contract.first_problem (packages/python/src/jeff/contract.py) ----
//
// Python's jsonschema keeps a two-level error tree for a failed "oneOf": one error for the oneOf itself, whose
// `.context` holds, for each alternative that failed, the errors that alternative produced (not flattened further).
// Ajv's `errors` array is flat instead: every alternative's errors (and, if an alternative has its own nested oneOf,
// that nested oneOf's own sub-errors too) all land in one list. To make the same choices Python's algorithm makes
// (prefer the alternative the document matched furthest into, then the one with fewest problems, then the first
// declared; within an alternative, prefer the problem closest to the document's root), this validates each
// alternative on its own (by compiling the exact schema pointer for that branch) rather than trying to regroup
// Ajv's flat list after the fact.

interface SchemaProblem {
  readonly instancePath: string;
  readonly keyword: string;
  readonly message: string;
  readonly params: Record<string, unknown>;
}

interface ResolvedSchema {
  readonly schemaId: string;
  readonly pointer: string;
  readonly node: Record<string, unknown>;
}

const pointerValidators = new Map<string, ValidateFunction>();
let schemaNameById: Map<string, SchemaName> | null = null;

function schemaIdOf(name: SchemaName): string {
  return String(loadSchema(name).$id);
}

function nameForSchemaId(schemaId: string): SchemaName {
  if (schemaNameById === null) schemaNameById = new Map(SCHEMAS.map((candidate) => [schemaIdOf(candidate), candidate]));
  const found = schemaNameById.get(schemaId);
  if (found === undefined) throw new ContractError(`firstProblem: no shipped schema has $id ${pyRepr(schemaId)}`);
  return found;
}

function pointerSegments(pointer: string): string[] {
  if (pointer === "") return [];
  return pointer.split("/").slice(1).map((segment) => segment.replace(/~1/g, "/").replace(/~0/g, "~"));
}

/** The raw (JSON, not Ajv-compiled) schema node at a JSON Pointer within a shipped schema, one $ref away at most
 * resolved by the caller (nodeAt itself does not follow $ref). */
function nodeAt(schemaId: string, pointer: string): unknown {
  let node: unknown = loadSchema(nameForSchemaId(schemaId));
  for (const segment of pointerSegments(pointer)) {
    if (Array.isArray(node)) node = node[Number(segment)];
    else if (node !== null && typeof node === "object") node = (node as Record<string, unknown>)[segment];
    else node = undefined;
    if (node === undefined) throw new ContractError(`firstProblem: ${schemaId}#${pointer} does not exist (at ${pyRepr(segment)})`);
  }
  return node;
}

/** `nodeAt`, following `$ref` (same-schema or cross-schema) until the node has none, the way a schema author reading
 * the shape would: a oneOf alternative that is only a `{"$ref": ...}` is its target, not a one-property object. */
function deref(schemaId: string, pointer: string): ResolvedSchema {
  let id = schemaId;
  let ptr = pointer;
  let node = nodeAt(id, ptr) as Record<string, unknown>;
  for (let hops = 0; typeof node.$ref === "string"; hops += 1) {
    if (hops > 20) throw new ContractError(`firstProblem: ${schemaId}#${pointer} has more than 20 nested $refs`);
    const ref = node.$ref;
    const hashIndex = ref.indexOf("#");
    const idPart = hashIndex === -1 ? ref : ref.slice(0, hashIndex);
    id = idPart === "" ? id : idPart;
    ptr = hashIndex === -1 ? "" : ref.slice(hashIndex + 1);
    node = nodeAt(id, ptr) as Record<string, unknown>;
  }
  return { schemaId: id, pointer: ptr, node };
}

function compiledAt(schemaId: string, pointer: string): ValidateFunction {
  const key = `${schemaId}#${pointer}`;
  const cached = pointerValidators.get(key);
  if (cached !== undefined) return cached;
  const ref = pointer === "" ? schemaId : key;
  const found = ajv().getSchema(ref);
  if (found === undefined) throw new ContractError(`firstProblem: ${ref} could not be compiled`);
  pointerValidators.set(key, found);
  return found;
}

/** Every problem `value` has against the schema at (schemaId, pointer), with `prefix` (the path from the document's
 * true root to `value`) added to each one's instancePath, so paths stay comparable across a recursive descent. */
function problemsOf(schemaId: string, pointer: string, value: unknown, prefix: string): SchemaProblem[] {
  const check = compiledAt(schemaId, pointer);
  check(value);
  return (check.errors ?? []).map((error) => ({
    instancePath: prefix + error.instancePath,
    keyword: error.keyword,
    message: error.message ?? "invalid",
    params: (error.params ?? {}) as Record<string, unknown>,
  }));
}

function splitInstancePath(path: string): string[] {
  return pointerSegments(path);
}

/** Python's `list(map(str, error.absolute_path))` comparison: elementwise by string, a path that is a strict
 * prefix of another sorts first. */
function comparePaths(a: readonly string[], b: readonly string[]): number {
  const shared = Math.min(a.length, b.length);
  for (let index = 0; index < shared; index += 1) {
    const left = a[index] as string;
    const right = b[index] as string;
    if (left !== right) return left < right ? -1 : 1;
  }
  return a.length - b.length;
}

/** Of a group of problems (one oneOf alternative's own errors), the one closest to the document's root. */
function closestProblem(problems: readonly SchemaProblem[]): SchemaProblem {
  const first = problems[0];
  if (first === undefined) throw new ContractError("firstProblem: closestProblem was given no problems");
  let chosen = first;
  let chosenPath = splitInstancePath(first.instancePath);
  for (const problem of problems.slice(1)) {
    const path = splitInstancePath(problem.instancePath);
    if (comparePaths(path, chosenPath) < 0) {
      chosen = problem;
      chosenPath = path;
    }
  }
  return chosen;
}

/** Among a oneOf's alternatives (each already validated on its own), the one the document matched furthest into:
 * the deepest minimum problem depth wins, then the fewest problems, then the first declared (mirrors Python's
 * `min(by_alternative.values(), key=lambda errors: (-min(depth), len(errors)))`, which keeps the first alternative
 * on a tie because Python's min() never replaces the current winner with an equal one). */
function closestAlternative(branchProblems: readonly (readonly SchemaProblem[])[]): number {
  let bestIndex = -1;
  let bestDepth = -1;
  let bestCount = Infinity;
  branchProblems.forEach((problems, index) => {
    if (problems.length === 0) return;
    const depth = Math.min(...problems.map((problem) => splitInstancePath(problem.instancePath).length));
    if (bestIndex === -1 || depth > bestDepth || (depth === bestDepth && problems.length < bestCount)) {
      bestIndex = index;
      bestDepth = depth;
      bestCount = problems.length;
    }
  });
  if (bestIndex === -1) throw new ContractError("firstProblem: every alternative of a oneOf matched its document");
  return bestIndex;
}

function valueAt(value: unknown, path: readonly string[]): unknown {
  let current = value;
  for (const segment of path) {
    if (Array.isArray(current)) current = current[Number(segment)];
    else if (current !== null && typeof current === "object") current = (current as Record<string, unknown>)[segment];
    else return undefined;
  }
  return current;
}

/** The schema node that governs one data path below (schemaId, pointer), walking "properties"/"additionalProperties"
 * (object keys) and "items" (array elements) the way the document's shape was actually checked. */
function schemaAtDataPath(schemaId: string, pointer: string, path: readonly string[]): ResolvedSchema {
  let current = deref(schemaId, pointer);
  for (const segment of path) {
    const node = current.node;
    const properties = node.properties as Record<string, unknown> | undefined;
    if (/^\d+$/.test(segment) && node.items !== undefined) {
      current = deref(current.schemaId, `${current.pointer}/items`);
    } else if (properties !== undefined && Object.hasOwn(properties, segment)) {
      current = deref(current.schemaId, `${current.pointer}/properties/${segment}`);
    } else if (typeof node.additionalProperties === "object" && node.additionalProperties !== null) {
      current = deref(current.schemaId, `${current.pointer}/additionalProperties`);
    } else {
      throw new ContractError(`firstProblem: cannot resolve the schema for ${pyRepr(segment)} under ${current.schemaId}${current.pointer}`);
    }
  }
  return current;
}

function formatProblem(problem: SchemaProblem, instanceValue: unknown): string {
  let message = problem.message;
  if (problem.keyword === "additionalProperties") {
    message = `must NOT have additional properties ('${String(problem.params.additionalProperty)}' was unexpected)`;
  } else if (problem.keyword === "enum") {
    const allowed = problem.params.allowedValues as unknown[];
    message = `${pyRepr(instanceValue)} is not one of [${allowed.map((value) => pyRepr(value)).join(", ")}]`;
  } else if (problem.keyword === "const") {
    message = `${pyRepr(instanceValue)} is not equal to ${pyRepr(problem.params.allowedValue)}`;
  } else if ((problem.keyword === "maxItems" || problem.keyword === "maxProperties")
    && (Array.isArray(instanceValue) || (instanceValue !== null && typeof instanceValue === "object"))) {
    const kind = problem.keyword === "maxItems" ? "items" : "properties";
    const count = Array.isArray(instanceValue) ? instanceValue.length : Object.keys(instanceValue as object).length;
    message = `${count} ${kind}, at most ${problem.params.limit} are allowed`;
  } else if (message.length > 200) {
    message = `${message.slice(0, 200)}...`;
  }
  return problem.instancePath ? `${message} (at ${problem.instancePath})` : message;
}

/** Walks a failed schema the way Python's first_problem does: if this node is a oneOf, pick the alternative the
 * document matched furthest into (validating each alternative on its own) and recurse into it; otherwise pick this
 * node's own closest problem, and if that problem sits on a property whose schema is itself a oneOf, recurse into
 * that property's schema and value instead of reporting "not valid under any of the given schemas". */
function resolveProblem(schemaId: string, pointer: string, value: unknown, prefix: string): string {
  const { schemaId: sid, pointer: ptr, node } = deref(schemaId, pointer);
  const oneOf = node.oneOf;
  if (Array.isArray(oneOf)) {
    const branchProblems = oneOf.map((_, index) => problemsOf(sid, `${ptr}/oneOf/${index}`, value, prefix));
    const chosen = closestAlternative(branchProblems);
    return resolveProblem(sid, `${ptr}/oneOf/${chosen}`, value, prefix);
  }
  const problems = problemsOf(sid, ptr, value, prefix);
  if (problems.length === 0) throw new ContractError("firstProblem: resolveProblem reached a node that matches its value");
  const problem = closestProblem(problems);
  const prefixSegments = splitInstancePath(prefix);
  const relative = splitInstancePath(problem.instancePath).slice(prefixSegments.length);
  const subValue = valueAt(value, relative);
  if (relative.length > 0) {
    const target = schemaAtDataPath(sid, ptr, relative);
    if (Array.isArray(target.node.oneOf)) return resolveProblem(target.schemaId, target.pointer, subValue, problem.instancePath);
  }
  return formatProblem(problem, subValue);
}

/** One short line naming the first thing wrong with a document that does not match schema `name` (contract.py's
 * first_problem, ported: see the block comment above for why Ajv's flat error list cannot just be re-sorted). */
export function firstProblem(name: SchemaName, document: unknown, definition?: string): string {
  const check = validator(name, definition);
  if (check(document)) throw new ContractError(`first_problem was asked about a document that matches the ${name} schema`);
  const pointer = definition === undefined ? "" : `/$defs/${definition}`;
  return resolveProblem(schemaIdOf(name), pointer, document, "");
}

// ---- an unquoted YAML date resolves to PyYAML's exact string, not a JS Date ----
//
// PyYAML's implicit resolver for `tag:yaml.org,2002:timestamp` recognises two shapes: a bare `YYYY-MM-DD` (always
// exactly 2 digits for the month and day), which it parses into a `datetime.date`, or that same shape plus a time
// part, parsed into a `datetime.datetime`. A manifest only ever uses the first shape (e.g. a changelog's `date:`).
// The `yaml` package's own YAML-1.1 timestamp tag matches a superset of this (1-2 digit month/day even without a
// time part) and always resolves to a JS `Date`, which cannot represent what PyYAML read without a day of
// information loss or outright corruption: a `Date` has no distinct "this was a bare date" marker, a time part gets
// silently re-expressed in `Date`'s own (UTC) idea of a timestamp, an out-of-range day such as `2026-02-30` is
// silently normalised forward (the overflowing month and day roll into the next valid date) instead of being the
// error PyYAML raises for that string, and `Date.UTC`/the `Date` constructor add 1900 to a year below 100 (so
// `0099-01-01` would become 1999). None of that can be fixed after the fact from the resulting `Date` alone, so the
// tag itself is replaced: this never produces a `Date`, only the original source text once validated, and refuses
// (as a parse error, which readYaml turns into a ManifestError below) anything with a time part or a day that does
// not exist on the calendar, asking the caller to quote the value instead.
const YAML_TIMESTAMP = /^(?:[0-9]{4}-[0-9]{2}-[0-9]{2}|[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::[0-9]{2})?))?)$/;
const YAML_DATE_ONLY = /^([0-9]{4})-([0-9]{2})-([0-9]{2})$/;
const TIMESTAMP_TAG_ID = "tag:yaml.org,2002:timestamp";

const dateOnlyTag = {
  identify: (value: unknown) => typeof value === "string",
  default: true as const,
  tag: TIMESTAMP_TAG_ID,
  test: YAML_TIMESTAMP,
  resolve(source: string): string {
    const match = YAML_DATE_ONLY.exec(source);
    if (match === null) {
      throw new Error(`${pyRepr(source)} is a YAML timestamp with a time part; this SDK only reads a manifest date `
        + "as a bare YYYY-MM-DD, so quote it to keep it a plain string");
    }
    const [, yearText, monthText, dayText] = match;
    const year = Number(yearText);
    const month = Number(monthText);
    const day = Number(dayText);
    // setUTCFullYear (unlike the Date constructor or Date.UTC) sets the year exactly as given, with no special
    // case adding 1900 to a value below 100; it still normalises an out-of-range month or day forward, which the
    // round-trip check below then catches as an invalid date.
    const check = new Date(0);
    check.setUTCFullYear(year, month - 1, day);
    if (check.getUTCFullYear() !== year || check.getUTCMonth() !== month - 1 || check.getUTCDate() !== day) {
      throw new Error(`${pyRepr(source)} is not a real calendar date; quote it to keep it a plain string`);
    }
    return source;
  },
};

const BOOL_TAG_ID = "tag:yaml.org,2002:bool";
/** YAML 1.1's booleans as PyYAML's resolver reads them: the yaml package's 1.1 schema also reads y, Y, n and N as
 * booleans, where PyYAML (and the other SDKs) keep them strings. */
const boolTags = [
  { identify: (value: unknown) => value === true, default: true as const, tag: BOOL_TAG_ID,
    test: /^(?:yes|Yes|YES|true|True|TRUE|on|On|ON)$/, resolve: () => true },
  { identify: (value: unknown) => value === false, default: true as const, tag: BOOL_TAG_ID,
    test: /^(?:no|No|NO|false|False|FALSE|off|Off|OFF)$/, resolve: () => false },
];

// ---- numbers as PyYAML's resolver and constructor read them ----
//
// The yaml package's YAML 1.1 schema reads more as numbers than PyYAML does (08, 09 and 019 as decimals, 1e5 and
// 1.0e5 without a dot or an exponent sign as floats) and keeps integers past 2**53 as inexact doubles. These tags
// replace its int and float tags with PyYAML's patterns (yaml/resolver.py) and constructors (construct_yaml_int and
// construct_yaml_float in yaml/constructor.py). PyYAML tries the float pattern first, so these are listed float first.
const INT_TAG_ID = "tag:yaml.org,2002:int";
const FLOAT_TAG_ID = "tag:yaml.org,2002:float";
const PY_FLOAT = /^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$/;
const PY_INT = /^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$/;
/** What Python's float() reads, once PyYAML has removed underscores and lower-cased the text (for !!float). */
const PYTHON_FLOAT_TEXT = /^[-+]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:e[-+]?[0-9]+)?$/;

/** An integer as JavaScript holds it: a number while it is exact (within 2**53), else a bigint, so a large integer
 * stays the exact value PyYAML reads instead of the nearest double. */
function exactInteger(value: bigint): number | bigint {
  return value >= BigInt(Number.MIN_SAFE_INTEGER) && value <= BigInt(Number.MAX_SAFE_INTEGER) ? Number(value) : value;
}

/** construct_yaml_int: underscores dropped, a sign, then 0, 0b binary, 0x hexadecimal, a leading 0 octal, a: b
 * sexagesimal (base 60), else decimal. */
function pyInt(source: string): number | bigint {
  let text = source.replaceAll("_", "");
  let sign = 1n;
  if (text.startsWith("-")) sign = -1n;
  if (text.startsWith("-") || text.startsWith("+")) text = text.slice(1);
  if (!/^(?:0b[01]+|0x[0-9a-fA-F]+|[0-9]+(?::[0-9]+)*)$/.test(text)) {
    throw new Error(`${pyRepr(source)} is not an integer (invalid literal for int())`);
  }
  if (text === "0") return 0;
  if (text.startsWith("0b") || text.startsWith("0x")) return exactInteger(sign * BigInt(text));
  if (text.startsWith("0")) {
    if (!/^[0-7]+$/.test(text)) throw new Error(`${pyRepr(source)} is not an integer (invalid literal for int() with base 8)`);
    return exactInteger(sign * BigInt(`0o${text}`));
  }
  if (text.includes(":")) {
    let value = 0n;
    for (const digit of text.split(":")) value = value * 60n + BigInt(digit);
    return exactInteger(sign * value);
  }
  return exactInteger(sign * BigInt(text));
}

/** construct_yaml_float: underscores dropped, lower-cased, a sign, then .inf, .nan, a: b sexagesimal (base 60), else
 * Python's float(). Python's inf and nan are JavaScript's Infinity and NaN. */
function pyFloat(source: string): number {
  let text = source.replaceAll("_", "").toLowerCase();
  let sign = 1;
  if (text.startsWith("-")) sign = -1;
  if (text.startsWith("-") || text.startsWith("+")) text = text.slice(1);
  if (text === ".inf") return sign * Infinity;
  if (text === ".nan") return NaN;
  if (text.includes(":")) {
    let value = 0;
    for (const digit of text.split(":")) value = value * 60 + Number(digit);
    if (Number.isNaN(value)) throw new Error(`${pyRepr(source)} is not a float (could not convert string to float)`);
    return sign * value;
  }
  if (!PYTHON_FLOAT_TEXT.test(text)) throw new Error(`${pyRepr(source)} is not a float (could not convert string to float)`);
  return sign * Number(text);
}

const numberTags = [
  { identify: (value: unknown) => typeof value === "number" && !Number.isInteger(value), default: true as const,
    tag: FLOAT_TAG_ID, test: PY_FLOAT, resolve: pyFloat },
  { identify: (value: unknown) => typeof value === "bigint" || Number.isInteger(value), default: true as const,
    tag: INT_TAG_ID, test: PY_INT, resolve: pyInt },
];

/** The explicit !!int, !!float, !!bool and !!null as PyYAML constructs them, whatever the text (a tag with default
 * false and no test is what the yaml package uses for an explicit tag): construct_yaml_bool reads only yes, no, true,
 * false, on and off in any case, construct_yaml_null gives None for any text. */
const explicitTags = [
  { identify: () => false, default: false as const, tag: INT_TAG_ID, resolve: pyInt },
  { identify: () => false, default: false as const, tag: FLOAT_TAG_ID, resolve: pyFloat },
  { identify: () => false, default: false as const, tag: "tag:yaml.org,2002:bool", resolve(source: string): boolean {
    const value = ({ yes: true, no: false, true: true, false: false, on: true, off: false } as Record<string, boolean>)[
      source.toLowerCase()];
    if (value === undefined) throw new Error(`${pyRepr(source)} is not a boolean (yes, no, true, false, on or off)`);
    return value;
  } },
  { identify: () => false, default: false as const, tag: "tag:yaml.org,2002:null", resolve: () => null },
];

/** The explicit tags PyYAML's safe loader reads whose values are JSON values; any other tag (!foo, !!binary, !!set,
 * !!omap, !!pairs, !!timestamp, ...) is refused. */
const JSON_TAGS = new Set(["str", "int", "float", "bool", "null", "map", "seq"].map((name) => `tag:yaml.org,2002:${name}`));
const REPLACED_TAGS = new Set([TIMESTAMP_TAG_ID, "tag:yaml.org,2002:bool", INT_TAG_ID, FLOAT_TAG_ID]);

/** A YAML file as PyYAML's safe_load reads it (YAML 1.1: yes/no are booleans, y/n are not; a repeated key keeps its first place
 * and its last value; an unquoted date is the exact string PyYAML's datetime.date.isoformat() would give it, never
 * a JS Date — see the block comment above), with every mapping a Map so that keys keep their order even when they
 * look like numbers. */
export function readYaml(path: string): unknown {
  const text = readFileSync(path, "utf8");
  const document = parseDocument(text, {
    version: "1.1",
    uniqueKeys: false,
    customTags: (tags) => [...explicitTags, ...numberTags, ...tags.filter((tag) => typeof tag === "string" || !REPLACED_TAGS.has(tag.tag)),
      dateOnlyTag, ...boolTags],
  });
  if (document.errors.length > 0) throw new ManifestError(`${path} is not valid YAML: ${document.errors[0]?.message}`);
  const value = plainValue(document.contents, text, path);
  // a warning is something the yaml package read past (a tag it could not resolve, among others): PyYAML refuses those
  if (document.warnings.length > 0) throw new ManifestError(`${path} is not valid YAML: ${document.warnings[0]?.message}`);
  return value;
}

/** The most values aliases may add to one document, as the other SDKs count them. Each alias stands for the whole
 * value it names, so a few hundred bytes of nested aliases could otherwise stand for gigabytes once written out. */
export const ALIAS_EXPANSION_LIMIT = 1_000_000;

/** An anchor while its collection is being read (an alias to it then names a collection it is inside), or once read:
 * its value and the number of values in it. */
type Anchored = { open: true } | { open: false; value: unknown; size: number };

const MAP_TAG = "tag:yaml.org,2002:map";
const SEQ_TAG = "tag:yaml.org,2002:seq";

/** The plain value of a parsed document, built in one walk in document order, as the yaml package's toJS would build
 * it with mapAsMap (each mapping a Map; a repeated key keeps its first place and takes its last value; a merge key
 * (<<) adds the keys the mapping does not have; an alias is the very value its anchor names, the last definition of
 * that anchor before it). While building, it counts the values the aliases add (an alias adds every value of the
 * node it names, its own aliases included; a mapping's keys are not values, as the Rust SDK counts) and refuses the
 * document past ALIAS_EXPANSION_LIMIT; it also refuses, by line, what has no JSON form: a mapping key that is not a
 * string, an alias naming a collection it is inside, a collection tag other than map and seq. The yaml package's own
 * toJS is not used: resolving its aliases takes time growing with the square of their number. */
function plainValue(root: YamlNode | null, text: string, path: string): unknown {
  const anchors = new Map<string, Anchored>();
  let expanded = 0;
  const lineOf = (node: unknown) => {
    const range = (node as { range?: [number, number, number] } | null)?.range;
    return range === undefined ? 1 : text.slice(0, range[0]).split("\n").length;
  };
  const refused = (node: unknown, problem: string) => new ManifestError(`${path} is not valid YAML: line ${lineOf(node)}: `
    + problem);
  const isMergeKey = (key: unknown) => isScalar(key) && typeof key.value === "symbol" && key.value.description === "<<";

  /** A node's value and the number of values in it. */
  const build = (node: unknown, near: unknown): [unknown, number] => {
    if (node === null || node === undefined) return [null, 1]; // an empty value is a null
    if (isAlias(node)) {
      const anchored = anchors.get(node.source);
      if (anchored === undefined) throw refused(node, `the alias *${node.source} names no anchor before it`);
      if (anchored.open) {
        throw refused(node, `the alias *${node.source} names a collection it is inside, which has no JSON form; write the `
          + "value out");
      }
      expanded += anchored.size;
      if (expanded > ALIAS_EXPANSION_LIMIT) {
        throw refused(node, "the aliases expand to more than 1,000,000 values; write the repeated parts out or use fewer "
          + "aliases");
      }
      return [anchored.value, anchored.size];
    }
    const anchor = (node as YamlNode).anchor;
    let built: [unknown, number];
    const tag = (node as YamlNode).tag;
    if (tag !== undefined && !JSON_TAGS.has(tag)) {
      throw refused(node, `the tag ${tag.startsWith("tag:yaml.org,2002:") ? `!!${tag.slice(18)}` : tag} is not supported; `
        + "write the value without a tag");
    }
    if (isMap(node) || isSeq(node)) {
      if (isMap(node) ? tag === SEQ_TAG : tag === MAP_TAG) throw refused(node, `the tag ${tag} does not fit the collection`);
      if (anchor !== undefined) anchors.set(anchor, { open: true });
      if (isMap(node)) {
        built = mapping(node.items, node);
      } else {
        const items: unknown[] = [];
        let size = 1;
        for (const item of node.items) {
          // a "key: value" in a flow sequence is a mapping of one pair
          const [value, count] = isPair(item) ? mapping([item], item) : build(item, node);
          items.push(value);
          size += count;
        }
        built = [items, size];
      }
    } else if (isScalar(node)) {
      // PyYAML resolves a plain = to its value tag, which the safe loader cannot construct (except as a key: below)
      if (node.value === "=" && node.type === "PLAIN" && tag === undefined) {
        throw refused(node, "the value key (=) is not supported; quote it");
      }
      built = [node.value, 1];
    } else {
      throw refused(near, `a YAML node of an unknown kind (${String(node)})`);
    }
    if (anchor !== undefined) anchors.set(anchor, { open: false, value: built[0], size: built[1] });
    return built;
  };

  /** A mapping's pairs as a Map, with the number of values in it. */
  /** As PyYAML's flatten_mapping: the pairs every merge key (<<) names come first (of a list of mappings, the last
   * mapping's first, so an earlier mapping's value wins), then the mapping's own pairs, whose values win. */
  const mapping = (pairs: Array<{ key: unknown; value: unknown }>, near: unknown): [Map<string, unknown>, number] => {
    const map = new Map<string, unknown>();
    const sizes = new Map<string, number>();
    const merged: Array<[string, unknown]> = [];
    const own: Array<{ key: unknown; value: unknown }> = [];
    for (const pair of pairs) {
      if (!isMergeKey(pair.key)) {
        own.push(pair);
        continue;
      }
      const [source] = build(pair.value, pair.key);
      const sources = Array.isArray(source) ? [...source].reverse() : [source];
      for (const item of sources) {
        if (!(item instanceof Map)) throw refused(pair.key, "a merge key (<<) must name a mapping or a list of mappings");
        merged.push(...(item as Map<string, unknown>));
      }
    }
    for (const [key, value] of merged) {
      map.set(key, value);
      sizes.set(key, valuesIn(value));
    }
    for (const pair of own) {
      // a plain = is a key like any other (PyYAML reads it as the string "=")
      const plainEquals = isScalar(pair.key) && pair.key.value === "=" && pair.key.type === "PLAIN";
      const [key] = plainEquals ? ["="] : build(pair.key, near);
      if (typeof key !== "string") {
        throw refused(pair.key ?? near, `a mapping key that is not a string (${pyRepr(key)}); quote it`);
      }
      const [value, size] = build(pair.value, pair.key);
      map.set(key, value);
      sizes.set(key, size);
    }
    let size = 1;
    for (const count of sizes.values()) size += count;
    return [map, size];
  };

  return build(root, root)[0];
}

/** The number of values in a value already built, itself included (counted without recursion). */
function valuesIn(value: unknown): number {
  let count = 0;
  const pending = [value];
  while (pending.length > 0) {
    const next = pending.pop();
    count += 1;
    if (next instanceof Map) pending.push(...next.values());
    else if (Array.isArray(next)) pending.push(...next);
  }
  return count;
}

/** Where the first value `test` picks is, in document order, as a JSON pointer (as the Python SDK names a place),
 * with the value; null when there is none. The walk goes through Maps, arrays and plain objects only (a class
 * instance, such as a policy or a function step, is the caller's code, not data read from YAML) and keeps its own
 * stack. */
export function firstValue<T>(value: unknown, test: (item: unknown) => item is T): { at: string; value: T } | null {
  const pending: Array<[unknown, string]> = [[value, ""]];
  while (pending.length > 0) {
    const [item, at] = pending.pop() as [unknown, string];
    if (test(item)) return { at: at === "" ? "/" : at, value: item };
    let children: Array<[unknown, string]> = [];
    if (item instanceof Map) children = [...item].map(([key, child]) => [child, `${at}/${String(key)}`]);
    else if (Array.isArray(item)) children = item.map((child, index) => [child, `${at}/${index}`]);
    else if (item !== null && typeof item === "object" && [Object.prototype, null].includes(Object.getPrototypeOf(item))) {
      children = Object.entries(item).map(([key, child]) => [child, `${at}/${key}`]);
    }
    for (let index = children.length - 1; index >= 0; index--) pending.push(children[index] as [unknown, string]);
  }
  return null;
}

/** Where the first integer too large to hold exactly as a number is (readYaml reads an integer past 2**53 as a
 * bigint, exact). Manifests and graphs refuse one by name (their schema checks would only say "must be integer" or
 * give an unhelpful oneOf message), since nothing real uses such a number. */
export function firstBigint(value: unknown): { at: string; value: bigint } | null {
  return firstValue(value, (item): item is bigint => typeof item === "bigint");
}

/** Where the first number that is not finite is (NaN or an infinity, which YAML's .nan and .inf give). A manifest
 * refuses one by its place and value, as every SDK does (contract/conformance/manifests.json): JSON has no such
 * number, and a calibration threshold of NaN would turn calibration off without a word (no confidence is below it). */
export function firstNonFinite(value: unknown): { at: string; value: number } | null {
  return firstValue(value, (item): item is number => typeof item === "number" && !Number.isFinite(item));
}

/** The message refusing a bigint found by firstBigint. */
export function bigintProblem(found: { at: string; value: bigint }): string {
  return `${found.at} is ${found.value}, an integer too large to keep exactly as a JavaScript number (more than `
    + "2**53 from zero); this SDK refuses it: write a smaller number";
}

/** A value read by readYaml (Maps) as plain JSON (objects), for schema checks and for `raw`. */
export function toPlain(value: unknown): unknown {
  if (value instanceof Map) {
    const out: Record<string, unknown> = {};
    for (const [key, item] of value) out[String(key)] = toPlain(item);
    return out;
  }
  if (Array.isArray(value)) return value.map(toPlain);
  if (value !== null && typeof value === "object" && !(value instanceof Date)) {
    return Object.fromEntries(Object.entries(value).map(([key, item]) => [key, toPlain(item)]));
  }
  return value;
}

/** A required string field of a YAML mapping (interfaces.yaml's description, errors.yaml's meaning): throws naming
 * the missing or wrongly-typed field instead of coercing a missing value into the text "undefined". */
export function requireYamlString(spec: Map<string, unknown>, key: string, context: string): string {
  const value = spec.get(key);
  if (typeof value !== "string") throw new ContractError(`${context} is missing its '${key}' string (got ${pyRepr(value)})`);
  return value;
}

export interface InterfaceSpec {
  readonly name: string;
  readonly questionType: QuestionType;
  readonly values: readonly string[] | null;
  readonly description: string;
}

let interfaceSpecs: ReadonlyMap<string, InterfaceSpec> | null = null;

export function interfaces(): ReadonlyMap<string, InterfaceSpec> {
  if (interfaceSpecs !== null) return interfaceSpecs;
  const path = join(shippedDir(), "contract", "interfaces.yaml");
  const found = new Map<string, InterfaceSpec>();
  for (const [name, spec] of readYaml(path) as Map<string, Map<string, unknown>>) {
    const questionType = spec.get("question_type");
    if (questionType !== "choice" && questionType !== "noul" && questionType !== "score") {
      throw new ContractError(`${path}: interface ${name} has an unknown question_type ${pyRepr(questionType)}`);
    }
    const values = spec.has("values") ? [...(spec.get("values") as string[])] : null;
    found.set(name, { name, questionType, values, description: requireYamlString(spec, "description", `${path}: interface ${name}`) });
  }
  interfaceSpecs = found;
  return found;
}

export interface ErrorSpec {
  readonly name: string;
  readonly status: number | null;
  readonly meaning: string;
}

export function errorCatalogue(): ReadonlyMap<string, ErrorSpec> {
  const path = join(shippedDir(), "contract", "errors.yaml");
  const found = new Map<string, ErrorSpec>();
  for (const [name, spec] of readYaml(path) as Map<string, Map<string, unknown>>) {
    const status = spec.get("status");
    if (status !== null && !(Number.isInteger(status) && (status as number) >= 400 && (status as number) <= 599)) {
      throw new ContractError(`${path}: ${name} has status ${pyRepr(status)}; use an HTTP error status or null`);
    }
    found.set(name, { name, status: status as number | null, meaning: requireYamlString(spec, "meaning", `${path}: ${name}`) });
  }
  return found;
}
