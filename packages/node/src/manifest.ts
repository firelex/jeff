/** Adapter manifests: what an adapter is, its context and input, and the functions callers call (port of
 * jeff/manifest.py; every check and message is the Python SDK's). A manifest may be given as parsed YAML (Maps, from
 * readYaml, whose keys keep their order even when they look like numbers) or as plain JSON. */

import { join } from "node:path";
import { type QuestionType, bigintProblem, firstBigint, firstNonFinite, interfaces, readYaml, toPlain, validate } from "./contract.js";
import { ManifestError } from "./errors.js";
import { pyRepr, pySorted } from "./pyformat.js";

export const CODE_NAME = /^[a-z][a-z0-9_]*$/;
const NOT_CODE = /[^a-z0-9]+/g;

export function codeNameOf(key: string): string {
  if (CODE_NAME.test(key)) return key;
  return key.toLowerCase().replace(NOT_CODE, "_").replace(/^_+|_+$/g, "");
}

/** A mapping's entries in their document order (a Map from readYaml, or a plain object); [] for anything else. */
export function entriesOf(value: unknown): Array<[string, unknown]> {
  if (value instanceof Map) return [...value].map(([key, item]) => [String(key), item]);
  if (value !== null && typeof value === "object" && !Array.isArray(value)) return Object.entries(value);
  return [];
}

function get(value: unknown, key: string): unknown {
  if (value instanceof Map) return value.get(key);
  if (value !== null && typeof value === "object") return (value as Record<string, unknown>)[key];
  return undefined;
}

export interface ContextField { readonly key: string; readonly description: string; readonly required: boolean; readonly codeName: string }
export interface InputField { readonly key: string; readonly description: string; readonly codeName: string; readonly form: "fields" | "text" }
export interface LabelSpec {
  readonly source: "fixed" | "caller" | "subset" | "describe";
  readonly fixed: ReadonlyMap<string, string | null>;
  readonly levels: readonly string[];
  readonly fixedFirst: ReadonlyMap<string, string | null>;
  readonly callerKeys: string | null;
  readonly max: number | null;
  readonly required: readonly string[];
  readonly keys: readonly string[];
  readonly subset: boolean | null;
}
export interface Calibration {
  readonly data: string;
  readonly rows: number;
  readonly targetPrecision: number;
  readonly threshold: number;
  readonly precisionAtThreshold: number;
  readonly coverageAtThreshold: number;
  readonly dataMirror: { readonly path: string; readonly sha256: string } | null;
  /** Only on an INT4 base's entry (calibration_by_format): which calibration rows were left out, and why. */
  readonly excluded?: string;
}
export interface FunctionSpec {
  readonly adapterId: string;
  readonly key: string;
  readonly id: string;
  readonly interface: string;
  readonly type: QuestionType;
  readonly instructions: string;
  readonly labels: LabelSpec;
  readonly abstainLabels: readonly string[];
  readonly calibration: Calibration | null;
  readonly context: readonly ContextField[];
  readonly input: InputField;
  readonly calibrationByFormat: ReadonlyMap<string, Calibration>;
}
export interface Manifest {
  readonly id: string;
  readonly version: string;
  readonly aliases: readonly string[];
  readonly licenceKind: "open" | "proprietary";
  readonly weights: Record<string, unknown>;
  readonly baseId: string;
  readonly baseVersion: string;
  readonly maxOptions: number;
  readonly context: readonly ContextField[];
  readonly input: InputField;
  readonly functions: ReadonlyMap<string, FunctionSpec>;
  readonly routesTo: readonly string[];
  readonly tests: Record<string, unknown>;
  readonly raw: Record<string, unknown>;
  readonly path: string;
  readonly isRouter: boolean;
}

/** A calibration block as manifests and GET /v1/functions write it (snake_case). */
export function calibrationFromWire(block: Record<string, unknown>): Calibration {
  const mirror = block.data_mirror as { path: string; sha256: string } | undefined;
  return {
    data: String(block.data), rows: Number(block.rows), targetPrecision: Number(block.target_precision),
    threshold: Number(block.threshold), precisionAtThreshold: Number(block.precision_at_threshold),
    coverageAtThreshold: Number(block.coverage_at_threshold), dataMirror: mirror ? { ...mirror } : null,
    ...(block.excluded !== undefined ? { excluded: String(block.excluded) } : {}),
  };
}

function labelsOf(raw: unknown): LabelSpec {
  const plain = toPlain(raw) as Record<string, any>;
  return {
    source: plain.source,
    fixed: new Map(entriesOf(get(raw, "fixed")) as Array<[string, string | null]>),
    levels: [...(plain.levels ?? [])],
    fixedFirst: new Map(entriesOf(get(raw, "fixed_first")) as Array<[string, string | null]>),
    callerKeys: plain.caller_keys ?? null,
    max: plain.max ?? null,
    required: [...(plain.required ?? [])],
    keys: [...(plain.keys ?? [])],
    subset: plain.subset ?? null,
  };
}

function codeNameFor(where: string, field: Record<string, any>): string {
  if ("code_name" in field) return String(field.code_name);
  const code = codeNameOf(field.key);
  if (!CODE_NAME.test(code)) {
    throw new ManifestError(`${where}: field ${pyRepr(field.key)} gives no code name by the rule (lower-case, other `
      + `characters to '_': ${pyRepr(code)}, which is not of the form ${CODE_NAME.source}); declare its code_name`);
  }
  return code;
}

function checkCodeNames(where: string, fields: Array<[string, string]>): void {
  const byCode = new Map<string, string[]>();
  for (const [key, code] of fields) byCode.set(code, [...(byCode.get(code) ?? []), key]);
  const shared = [...byCode].filter(([, keys]) => keys.length > 1);
  if (shared.length > 0) {
    throw new ManifestError(`${where}: fields share a code name, which callers could not tell apart: `
      + shared.map(([code, keys]) => `${keys.map(pyRepr).join(", ")} -> ${code}`).join("; ")
      + "; declare a distinct code_name for each");
  }
}

function inputField(where: string, raw: Record<string, any>, context: readonly ContextField[]): InputField {
  if (raw.form === "text" && context.length > 0) {
    throw new ManifestError(`${where}: input ${pyRepr(raw.key)} has form text (the state is the plain input string), so `
      + `there can be no context fields, but it has ${pyRepr(context.map((field) => field.key))}`);
  }
  return { key: raw.key, description: raw.description, codeName: codeNameFor(where, raw), form: raw.form };
}

function requestFields(where: string, rawContext: Array<Record<string, any>>, rawInput: Record<string, any>
): [readonly ContextField[], InputField] {
  const context = rawContext.map((item) => ({
    key: String(item.key), description: String(item.description), required: Boolean(item.required),
    codeName: codeNameFor(where, item),
  }));
  const keys = context.map((field) => field.key);
  if (new Set(keys).size !== keys.length || keys.includes(rawInput.key)) {
    throw new ManifestError(`${where}: context keys must be unique and differ from the input key (${pyRepr(keys)}, `
      + `input ${rawInput.key})`);
  }
  const input = inputField(where, rawInput, context);
  checkCodeNames(where, [...context.map((field): [string, string] => [field.key, field.codeName]), [input.key, input.codeName]]);
  return [context, input];
}

function checkFunction(spec: FunctionSpec, maxOptions: number, where: string): void {
  const known = interfaces();
  const name = `${where}: function ${spec.key}`;
  const iface = known.get(spec.interface);
  if (iface === undefined) {
    throw new ManifestError(`${name} names interface ${pyRepr(spec.interface)}, which is not in the interface catalogue `
      + `(${[...known.keys()].join(", ")})`);
  }
  if (spec.type !== iface.questionType) {
    throw new ManifestError(`${name} has type ${spec.type} but interface ${spec.interface} compiles to ${iface.questionType}`);
  }
  const labels = spec.labels;
  if (spec.type !== "choice" && (labels.required.length > 0 || labels.keys.length > 0)) {
    throw new ManifestError(`${name}: only choice functions have 'required' or 'keys'`);
  }
  if (spec.type === "choice") {
    if (labels.levels.length > 0) throw new ManifestError(`${name}: choice functions have no levels`);
    if (labels.source === "fixed" && labels.fixed.size === 0) throw new ManifestError(`${name}: fixed labels need a non-empty 'fixed'`);
    if (labels.source === "caller" && labels.max === null) {
      throw new ManifestError(`${name}: caller labels need 'max', the most labels the adapter was trained on`);
    }
    if (labels.required.length > 0 && labels.source !== "subset") throw new ManifestError(`${name}: only subset labels have 'required'`);
    if (labels.keys.length > 0 && labels.source !== "describe") throw new ManifestError(`${name}: only describe labels have 'keys'`);
    if (labels.source === "subset") {
      if (labels.fixed.size === 0 || labels.fixedFirst.size > 0 || labels.callerKeys || labels.max !== null) {
        throw new ManifestError(`${name}: subset labels need a non-empty 'fixed' (every allowed label) and no `
          + "fixed_first, caller_keys or max");
      }
      const stray = labels.required.filter((key) => !labels.fixed.has(key));
      if (stray.length > 0) {
        throw new ManifestError(`${name}: required labels ${pyRepr(stray)} are not in 'fixed' (${pyRepr([...labels.fixed.keys()])})`);
      }
    }
    if (labels.source === "describe") {
      if (labels.keys.length === 0 || labels.fixed.size > 0 || labels.fixedFirst.size > 0 || labels.callerKeys
          || labels.max !== null) {
        throw new ManifestError(`${name}: describe labels need non-empty 'keys' and no fixed, fixed_first, caller_keys or max`);
      }
      if (new Set(labels.keys).size !== labels.keys.length) {
        throw new ManifestError(`${name}: describe keys ${pyRepr(labels.keys)} repeat a key`);
      }
    }
    const count = labels.fixed.size + labels.fixedFirst.size + (labels.max ?? 0) + labels.keys.length;
    if (count > maxOptions) throw new ManifestError(`${name} allows ${count} options, more than the base's max_options ${maxOptions}`);
    const fixedKeys = [...labels.fixed.keys()];
    if (iface.values !== null && (fixedKeys.length !== iface.values.length
        || fixedKeys.some((key, index) => key !== iface.values?.[index]))) {
      throw new ManifestError(`${name}: interface ${spec.interface} requires labels ${pyRepr(iface.values)}, got ${pyRepr(fixedKeys)}`);
    }
  } else if (spec.type === "score") {
    if (labels.levels.length < 2 || labels.fixed.size > 0 || labels.source !== "fixed") {
      throw new ManifestError(`${name}: score functions need fixed 'levels' (2 to 10) and nothing else`);
    }
  } else if (labels.levels.length > 0 || [...labels.fixed.keys()].some((key) => key !== "true" && key !== "false")
      || labels.source !== "fixed") {
    throw new ManifestError(`${name}: noul functions may only describe 'true' and 'false' in 'fixed'`);
  }
  const knownLabels = new Set([...labels.fixed.keys(), ...labels.fixedFirst.keys(), ...labels.keys]);
  const unknown = spec.abstainLabels.filter((label) => !knownLabels.has(label));
  if (unknown.length > 0) {
    throw new ManifestError(`${name}: abstain_labels ${pyRepr(unknown)} are not among its labels ${pyRepr(pySorted(knownLabels))}`);
  }
}

function criteria(functionBy: unknown): string[] {
  return typeof functionBy === "string" ? [functionBy] : [...(functionBy as string[])];
}

function checkTests(where: string, tests: Record<string, any>, functions: ReadonlyMap<string, FunctionSpec>): void {
  if (tests.function_by === "question_type") {
    const types = [...functions.values()].map((spec) => spec.type);
    if (new Set(types).size !== types.length) {
      throw new ManifestError(`${where}: tests.function_by question_type needs one function per question type; the `
        + `functions have types ${pyRepr(types)}`);
    }
  }
  if ("kinds" in tests) {
    if (!criteria(tests.function_by).includes("source_kind")) {
      throw new ManifestError(`${where}: tests.kinds maps source.kind values to functions, so tests.function_by must `
        + `use source_kind (it is ${pyRepr(tests.function_by)})`);
    }
    const kinds = Object.entries(tests.kinds as Record<string, string | string[]>);
    const named = kinds.flatMap(([, value]) => (typeof value === "string" ? [value] : value));
    const stray = pySorted(new Set(named.filter((key) => !functions.has(key))));
    if (stray.length > 0) {
      throw new ManifestError(`${where}: tests.kinds names ${pyRepr(stray)}, which are not its functions (${pyRepr([...functions.keys()])})`);
    }
    const several = kinds.filter(([, value]) => typeof value !== "string").map(([kind]) => kind);
    const only = criteria(tests.function_by);
    if (several.length > 0 && only.length === 1 && only[0] === "source_kind") {
      throw new ManifestError(`${where}: tests.kinds maps ${pyRepr(several)} to several functions each, but `
        + "tests.function_by is source_kind alone, so no other criterion can leave one of them: every row of those "
        + "kinds would match several functions");
    }
  }
  const uses = criteria(tests.function_by).includes("id_pattern");
  if (!("id_patterns" in tests)) {
    if (uses) {
      throw new ManifestError(`${where}: tests.function_by uses id_pattern, so tests.id_patterns must give every `
        + `function's row id pattern (${[...functions.keys()].join(", ")})`);
    }
    return;
  }
  if (!uses) {
    throw new ManifestError(`${where}: tests.id_patterns is given, but tests.function_by does not use id_pattern (it `
      + `is ${pyRepr(tests.function_by)})`);
  }
  const idPatterns = tests.id_patterns as Record<string, string>;
  const named = Object.keys(idPatterns);
  const keys = [...functions.keys()];
  if (named.length !== keys.length || named.some((key, index) => key !== keys[index])) {
    throw new ManifestError(`${where}: tests.id_patterns must name every function, in the functions' order `
      + `(${pyRepr(keys)}); it names ${pyRepr(named)}`);
  }
  // The patterns themselves only map test rows, which this SDK never does, so a pattern is never run here, only
  // checked for being a syntactically valid regular expression (Python's own check uses re.compile; this SDK reads
  // the pattern with JS's RegExp instead, so the syntax accepted differs slightly from Python's, but a typo still
  // fails the manifest the way Python's check does).
  for (const [key, pattern] of Object.entries(idPatterns)) {
    try {
      new RegExp(pattern);
    } catch (error) {
      throw new ManifestError(`${where}: tests.id_patterns ${key}: ${pyRepr(pattern)} is not a regular expression `
        + `(${error instanceof Error ? error.message : String(error)})`);
    }
  }
}

const VERSION_PART = /^v[0-9]+$/;

/** The ID's prefix (jeff.<domain>), its name's hyphen-separated segments, and the indexes of those that are a version
 * part (v and digits, the whole segment). */
function versionParts(adapterId: string): { prefix: string; segments: string[]; versions: number[] } {
  const dot = adapterId.lastIndexOf(".");
  const segments = adapterId.slice(dot + 1).split("-");
  const versions = segments.flatMap((segment, index) => (VERSION_PART.test(segment) ? [index] : []));
  return { prefix: adapterId.slice(0, dot), segments, versions };
}

/** The adapter ID without its version part (contract/schemas/manifest.schema.json, aliases): the one hyphen-separated
 * segment of its name (the part after the second dot) that is v and digits, removed with one hyphen, as
 * jeff.routing.router-open-v14 -> jeff.routing.router-open. Null when the name has no such segment, more than one,
 * or nothing besides it. */
export function unversionedId(adapterId: string): string | null {
  const { prefix, segments, versions } = versionParts(adapterId);
  if (versions.length !== 1 || segments.length === 1) return null;
  segments.splice(versions[0]!, 1);
  return `${prefix}.${segments.join("-")}`;
}

/** No alias is listed twice, and an alias of the ID form (the schema allows it as jeff.<domain>.<name>) must be the
 * adapter's unversioned ID. */
function checkAliases(where: string, adapterId: string, aliases: readonly string[]): void {
  const seen = new Set<string>();
  for (const alias of aliases) {
    if (seen.has(alias)) throw new ManifestError(`${where}: the alias ${alias} is listed twice`);
    seen.add(alias);
    if (!alias.startsWith("jeff.")) continue;
    const { segments, versions } = versionParts(adapterId);
    const form = `${where}: the alias ${alias} has the form of an adapter ID, which only the adapter's ID without its `
      + "version part may have, and ";
    if (versions.length === 0) {
      throw new ManifestError(`${form}${adapterId} has no version part (one segment v<digits> in its name besides `
        + "others, as in jeff.routing.router-open-v14)");
    }
    if (versions.length > 1) {
      throw new ManifestError(`${form}${adapterId} has more than one version part `
        + `(${versions.map((index) => segments[index]).join(", ")}), so it has no ID without one`);
    }
    if (segments.length === 1) {
      throw new ManifestError(`${form}the name of ${adapterId} is only a version part, so it has no ID without one`);
    }
    const expected = unversionedId(adapterId);
    if (alias !== expected) {
      throw new ManifestError(`${where}: the alias ${alias} has the form of an adapter ID, so it must be ${expected}, `
        + `the ID ${adapterId} without its version part`);
    }
  }
}

export function manifestFromDict(raw: unknown, path: string): Manifest {
  const where = path;
  const huge = firstBigint(raw);
  if (huge !== null) throw new ManifestError(`${where}: ${bigintProblem(huge)}`);
  const infinite = firstNonFinite(raw);
  if (infinite !== null) {
    throw new ManifestError(`${where}: ${infinite.at} is ${pyRepr(infinite.value)}, not a finite number, which JSON cannot `
      + "hold; write a finite number");
  }
  const plain = toPlain(raw) as Record<string, any>;
  validate("manifest", plain, where);
  const adapterId = String(plain.id);
  checkAliases(where, adapterId, plain.aliases);
  const [context, input] = requestFields(where, plain.context, plain.input);
  const functions = new Map<string, FunctionSpec>();
  for (const [key, body] of entriesOf(get(raw, "functions"))) {
    const b = toPlain(body) as Record<string, any>;
    const calibration = b.calibration ? calibrationFromWire(b.calibration) : null;
    const byFormat = new Map(entriesOf(get(body, "calibration_by_format"))
      .map(([formatId, entry]): [string, Calibration] => [formatId, calibrationFromWire(toPlain(entry) as Record<string, unknown>)]));
    if (byFormat.size > 0 && calibration === null) {
      throw new ManifestError(`${where}: function ${key} has calibration_by_format entries (${[...byFormat.keys()].join(", ")}) `
        + "but calibration null; a function uncalibrated on the bfloat16 base stays uncalibrated in every format");
    }
    const own = "input" in b ? requestFields(`${where}: function ${key}`, b.context, b.input) : [context, input] as const;
    const spec: FunctionSpec = {
      adapterId, key, id: `${adapterId}.${key}`, interface: b.interface, type: b.type, instructions: b.instructions,
      labels: labelsOf(get(body, "labels")), abstainLabels: [...b.abstain_labels], calibration,
      context: own[0], input: own[1], calibrationByFormat: byFormat,
    };
    checkFunction(spec, plain.base.max_options, where);
    functions.set(key, spec);
  }
  checkTests(where, plain.tests, functions);
  const routesTo: string[] = [...plain.routes_to];
  if (routesTo.length > 0) {
    const only = [...functions.values()];
    const first = only[0];
    const fixed = first ? [...first.labels.fixed.keys()] : [];
    if (only.length !== 1 || !first || first.interface !== "route" || first.labels.source !== "subset"
        || fixed.length !== routesTo.length || fixed.some((key, index) => key !== routesTo[index])) {
      throw new ManifestError(`${where}: a router has exactly one route function with subset labels whose keys equal `
        + `routes_to ${pyRepr(routesTo)} (dispatch offers the targets that lead to the requested interface)`);
    }
    if (first.labels.required.length > 0) {
      throw new ManifestError(`${where}: a router's labels have no required keys; dispatch offers only the targets `
        + "that lead to the requested interface");
    }
  }
  return {
    id: adapterId, version: plain.version, aliases: [...plain.aliases], licenceKind: plain.licence.kind,
    weights: plain.weights, baseId: plain.base.id, baseVersion: plain.base.version, maxOptions: plain.base.max_options,
    context, input, functions, routesTo, tests: plain.tests, raw: plain, path, isRouter: routesTo.length > 0,
  };
}

export function loadManifest(path: string): Manifest {
  return manifestFromDict(readYaml(path), path);
}

export function resolveCalibration(functionId: string, calibration: Calibration | null,
  byFormat: ReadonlyMap<string, Calibration>, formatId: string | null): Calibration | null {
  if (formatId === null || calibration === null) return calibration;
  const entry = byFormat.get(formatId);
  if (entry === undefined) {
    const calibratedOn = [...byFormat.keys()].join(", ") || "no INT4 base";
    throw new ManifestError(`${functionId} has no calibration for the INT4 base this engine serves (${formatId}); `
      + `it is calibrated on the bfloat16 base and on ${calibratedOn}. Fit its threshold on this base with `
      + `\`jeff calibrate ${functionId} --base <the INT4 base folder>\` (for an adapter served from an adapter folder, `
      + "`jeff calibrate <that folder> --base <the INT4 base folder>`, which writes into the folder's manifest.yaml)");
  }
  return entry;
}

export function calibrationFor(fn: FunctionSpec, formatId: string | null): Calibration | null {
  return resolveCalibration(fn.id, fn.calibration, fn.calibrationByFormat, formatId);
}

/** The manifest file of an adapter folder. */
export function manifestPath(folder: string): string {
  return join(folder, "manifest.yaml");
}
