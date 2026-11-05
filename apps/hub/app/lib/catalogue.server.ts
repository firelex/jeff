import fs from 'node:fs';
import path from 'node:path';
import { Ajv2020 } from 'ajv/dist/2020.js';
import addFormats from 'ajv-formats';
import { parse } from 'yaml';
import { privateMarkersIn } from './private-markers.js';

// The field as the manifest declares it: `code_name` is the name callers must use (SDK context, HTTP/channel
// context keys, MCP tool arguments); absent, it is derived from `key` (codeNameOf).
type RawField = { key: string; description: string; code_name?: string };
type RawContextField = RawField & { required: boolean };

// The field as the catalogue publishes it: `codeName` is what a caller passes; `key` (the field exactly as trained,
// and as the prompt shows it) is kept only as secondary, human-readable text -- never as a request key.
type ContextField = { key: string; codeName: string; description: string; required: boolean };
type InputField = { key: string; codeName: string; description: string };

const CODE_NAME = /^[a-z][a-z0-9_]*$/;
const NOT_CODE = /[^a-z0-9]+/g;

// Ported from packages/python/src/jeff/manifest.py's code_name_of: the name callers use for a field whose key is
// `key`. A key that already is a code name (^[a-z][a-z0-9_]*$) is its own, unchanged; any other key is lower-cased,
// every run of characters other than a-z and 0-9 replaced by one underscore, underscores trimmed from both ends
// ('counterparty name' -> counterparty_name). A manifest's own `code_name` (when that derivation would not itself
// be a code name) always wins -- callers below never call this for a field that declares one.
export function codeNameOf(key: string): string {
  if (CODE_NAME.test(key)) return key;
  return key.toLowerCase().replace(NOT_CODE, '_').replace(/^_+|_+$/g, '');
}

// context and input are the function's own when it declares them (an adapter trained on several request shapes),
// and null when it takes the adapter's.
export interface CatalogueFunction {
  key: string;
  id: string;
  interface: string;
  instructions: string | null;
  labels: string[] | null;
  context: ContextField[] | null;
  input: InputField | null;
}
// id is the adapter's own ID (the page's address); name is what the pages tell callers to use: its unversioned ID
// when the manifest lists one among its aliases (jeff.routing.router-open for jeff.routing.router-open-v14, so code keeps working
// across base releases; contract/schemas/manifest.schema.json, aliases), its ID when it has none. Each function's id
// is name, a dot and its key. localName (an open adapter only) is the short name Jeff.local takes in Python, as the
// home page writes it (guard for jeff.safety.guard): the alias that is the last part of its ID.
export interface CatalogueEntry {
  id: string;
  name: string;
  version: string;
  kind: 'open' | 'proprietary';
  domain: string;
  title: string;
  summary: string;
  useWhen: string[];
  avoidWhen: string[];
  base: string;
  functions: CatalogueFunction[];
  context: ContextField[];
  input: InputField;
  weights: { repo: string; revision: string } | null;
  licence: string | null;
  localName: string | null;
}

// The parts of a manifest the catalogue reads (the schema guarantees their shape).
interface Labels { source: 'fixed' | 'caller' | 'subset' | 'describe'; fixed?: Record<string, string | null>; levels?: string[]; keys?: string[] }
interface Manifest {
  id: string;
  version: string;
  aliases: string[];
  licence: { kind: 'open' | 'proprietary'; adapter: string };
  // Who may use it (absent: everyone the API admits). Any value makes the adapter hidden on the hub.
  access?: unknown;
  weights: { open?: { repo: string; revision: string } };
  base: { id: string; version: string };
  context: RawContextField[];
  input: RawField;
  functions: Record<string, {
    interface: string; type: 'choice' | 'noul' | 'score'; instructions: string; labels: Labels;
    context?: RawContextField[]; input?: RawField;
  }>;
}
interface Text { title: string; summary: string; use_when: string[]; avoid_when: string[] }

// staged-not-served (in the served folder) holds manifests staged on the server that the API does not serve, so they
// are not in the catalogue. Every other folder must hold an adapter's manifest.yaml.
const NOT_ADAPTER_DIRS = new Set(['staged-not-served']);

// Retired adapters (retiredFile, content/owner-only/retired-adapters.yaml: a list of adapter IDs): the served folder
// still holds their manifest.yaml, but the live API no longer serves them, so the hub must not list them either. A
// listed ID with no manifest in the served folder is an error naming it.
function readRetired(file: string): Set<string> {
  const data: unknown = parse(fs.readFileSync(file, 'utf8'));
  if (!Array.isArray(data) || !data.every((id) => typeof id === 'string' && id !== '')) {
    throw new Error(`${file}: not a list of adapter IDs`);
  }
  return new Set(data as string[]);
}

function fail(what: string, problems: string[]): void {
  if (problems.length > 0) throw new Error(`${what}:\n${problems.join('\n')}`);
}

function readManifests(dir: string, validate: (data: unknown) => boolean, errorsOf: () => string): Manifest[] {
  const out: Manifest[] = [];
  const problems: string[] = [];
  const names = fs.readdirSync(dir, { withFileTypes: true }).filter((d) => d.isDirectory() && !NOT_ADAPTER_DIRS.has(d.name));
  for (const { name } of names) {
    const file = path.join(dir, name, 'manifest.yaml');
    if (!fs.existsSync(file)) { problems.push(`${path.join(dir, name)}: a folder with no manifest.yaml`); continue; }
    const data: unknown = parse(fs.readFileSync(file, 'utf8'));
    if (!validate(data)) { problems.push(`${file}: ${errorsOf()}`); continue; }
    const manifest = data as Manifest;
    if (manifest.id !== name) { problems.push(`${file}: id ${manifest.id} differs from its folder name`); continue; }
    out.push(manifest);
  }
  fail(`Invalid adapter manifests in ${dir}`, problems);
  return out;
}

// What a caller sees as the answer's labels: the fixed keys, the levels of a score, the keys a caller describes,
// false/true for a yes-no function (as the engine offers them), or null when the caller passes its own labels.
function labelsOf(type: 'choice' | 'noul' | 'score', labels: Labels): string[] | null {
  if (type === 'noul') return ['false', 'true'];
  if (labels.source === 'caller') return null;
  if (labels.source === 'describe') {
    if (labels.keys === undefined) throw new Error('describe labels without keys');
    return labels.keys;
  }
  if (type === 'score') {
    if (labels.levels === undefined) throw new Error('score labels without levels');
    return labels.levels;
  }
  if (labels.fixed === undefined) throw new Error(`${labels.source} labels without fixed`);
  return Object.keys(labels.fixed);
}

// The public parts of a context or input field (not the input's form), with its code name resolved: the
// manifest's own, or derived from its key.
function contextOf(fields: RawContextField[]): ContextField[] {
  return fields.map(({ key, description, required, code_name }) => ({ key, description, required, codeName: code_name ?? codeNameOf(key) }));
}

function inputOf(field: RawField): InputField {
  return { key: field.key, description: field.description, codeName: field.code_name ?? codeNameOf(field.key) };
}

// An open adapter's short name for Jeff.local: its alias that is the last part of its ID; an open adapter with none
// is an error naming it.
function shortAlias(m: Manifest): string {
  const last = m.id.slice(m.id.lastIndexOf('.') + 1);
  if (!m.aliases.includes(last)) {
    throw new Error(`${m.id}: an open adapter needs the alias ${last} (the last part of its ID), the short name its local calls use`);
  }
  return last;
}

// The manifest's alias of the ID form (the schema lets only the unversioned ID take that form), or null when it has none.
function unversionedAlias(m: Manifest): string | null {
  const found = m.aliases.filter((alias) => alias.startsWith('jeff.'));
  if (found.length > 1) throw new Error(`${m.id}: more than one alias of the ID form (${found.join(', ')})`);
  return found.length === 1 ? found[0] : null;
}

function entryOf(m: Manifest, kind: 'open' | 'proprietary', text: Text): CatalogueEntry {
  const name = unversionedAlias(m) ?? m.id;
  const functions = Object.entries(m.functions).map(([key, fn]) => {
    let labels: string[] | null;
    try {
      labels = labelsOf(fn.type, fn.labels);
    } catch (e) {
      throw new Error(`${m.id}: function ${key}: ${(e as Error).message}`);
    }
    return {
      key,
      id: `${name}.${key}`,
      interface: fn.interface,
      instructions: fn.instructions,
      labels,
      context: fn.context === undefined ? null : contextOf(fn.context),
      input: fn.input === undefined ? null : inputOf(fn.input),
    };
  });
  return {
    id: m.id,
    name,
    version: m.version,
    kind,
    domain: m.id.split('.')[1],
    title: text.title,
    summary: text.summary,
    useWhen: text.use_when,
    avoidWhen: text.avoid_when,
    base: `${m.base.id} ${m.base.version}`,
    functions,
    context: contextOf(m.context),
    input: inputOf(m.input),
    weights: kind === 'open' && m.weights.open !== undefined ? { repo: m.weights.open.repo, revision: m.weights.open.revision } : null,
    licence: kind === 'open' ? m.licence.adapter : null,
    localName: kind === 'open' ? shortAlias(m) : null,
  };
}

function isStringList(value: unknown): value is string[] {
  return Array.isArray(value) && value.every((v) => typeof v === 'string');
}

function readText(file: string): Record<string, Text> {
  const data: unknown = parse(fs.readFileSync(file, 'utf8'));
  if (data === null || typeof data !== 'object' || Array.isArray(data)) throw new Error(`${file}: not a map of adapter id to text`);
  const problems: string[] = [];
  for (const [id, raw] of Object.entries(data as Record<string, unknown>)) {
    const t = raw as Partial<Record<keyof Text, unknown>> | null;
    if (t === null || typeof t !== 'object') { problems.push(`${id}: not a map`); continue; }
    if (typeof t.title !== 'string' || t.title === '') problems.push(`${id}: no title`);
    if (typeof t.summary !== 'string' || t.summary === '') problems.push(`${id}: no summary`);
    if (!isStringList(t.use_when)) problems.push(`${id}: use_when is not a list of strings`);
    if (!isStringList(t.avoid_when)) problems.push(`${id}: avoid_when is not a list of strings`);
  }
  fail(`${file}: invalid catalogue text`, problems);
  return data as Record<string, Text>;
}

// Every string in the entry, with the path to it (for example functions[0].instructions).
function* stringsOf(value: unknown, where: string): Generator<[string, string]> {
  if (typeof value === 'string') yield [where, value];
  else if (Array.isArray(value)) for (const [i, v] of value.entries()) yield* stringsOf(v, `${where}[${i}]`);
  else if (value !== null && typeof value === 'object') {
    for (const [k, v] of Object.entries(value)) yield* stringsOf(v, where === '' ? k : `${where}.${k}`);
  }
}

// The public catalogue and the hidden one (final review I2): an adapter is public exactly when its manifest is in
// openDir (catalogue/) with licence.kind open and no access field, and its copy in servedDir, if it has one, has the
// same access (none; the load refuses copies that disagree, re-review R3); every other adapter the hub knows of (the API's
// served adapters, an open one with an access field) is hidden (owner, 4 November 2026: "every adapter that is not
// open is owner-only"). `listed` is every public adapter, with its text from textFile (content/catalogue-text.yaml);
// `hidden` every hidden one, with its text from hiddenTextFile (content/owner-only/catalogue-text.yaml), which only
// the owner's page shows and the public build never reads. The load refuses, naming each adapter: a hidden adapter
// with text in textFile, a public one with text in hiddenTextFile, text for an adapter with no manifest, and an
// adapter with no text in its own file.
export interface Catalogues { listed: CatalogueEntry[]; hidden: CatalogueEntry[] }

// Whether an adapter is public on the hub: its manifest is in catalogue/, open, with no access field.
export function isPublicAdapter(manifest: { licence: { kind: string }; access?: unknown }, inOpenDir: boolean): boolean {
  return inOpenDir && manifest.licence.kind === 'open' && manifest.access === undefined;
}

export function loadCatalogues(openDir: string, servedDir: string, textFile: string, schemaFile: string, hiddenTextFile: string,
  retiredFile: string): Catalogues {
  const ajv = new Ajv2020({ allErrors: true, strict: false });
  addFormats(ajv);
  const validate = ajv.compile(JSON.parse(fs.readFileSync(schemaFile, 'utf8')));
  const errorsOf = () => ajv.errorsText(validate.errors, { separator: '; ', dataVar: '' });

  const open = readManifests(openDir, validate, errorsOf);
  const allServed = readManifests(servedDir, validate, errorsOf);
  const retired = readRetired(retiredFile);
  fail(`Retired adapters in ${retiredFile} with no manifest in ${servedDir}`,
    [...retired].filter((id) => !allServed.some((m) => m.id === id)).sort());
  const served = allServed.filter((m) => !retired.has(m.id));

  const licenceProblems = open.filter((m) => m.licence.kind !== 'open').map((m) => `${m.id}: licence.kind is ${m.licence.kind}`);
  fail(`Adapters in ${openDir} that are not open`, licenceProblems);
  const repoProblems = open
    .filter((m) => m.weights.open === undefined || !m.weights.open.repo.startsWith('mstrasser/'))
    .map((m) => `${m.id}: weights.open.repo is ${m.weights.open?.repo ?? 'absent'}, not a mstrasser/ repo`);
  fail('Open adapters without public weights', repoProblems);

  const openIds = new Set(open.map((m) => m.id));
  // Every copy the repo has of an adapter must agree on who may use it, and on licence.kind (re-review R3): an
  // adapter in catalogue/ whose results/v14-manifests copy has an access field the catalogue/ copy lacks, or
  // another one (or the other way round), or whose results/v14-manifests copy's licence.kind differs from
  // catalogue/'s, is refused, so making an adapter private in one copy only, or getting its licence wrong in one
  // copy, can never leave it public, or wrongly licensed, on the hub.
  const servedById = new Map(served.map((m) => [m.id, m]));
  const accessOf = (m: Manifest) => (m.access === undefined ? 'no access field' : `access ${JSON.stringify(m.access)}`);
  fail(`Adapters whose copies in ${openDir} and ${servedDir} disagree on access or licence.kind`, open.flatMap((m) => {
    const copy = servedById.get(m.id);
    if (copy === undefined) return [];
    const problems: string[] = [];
    if (JSON.stringify(copy.access) !== JSON.stringify(m.access)) {
      problems.push(`${m.id}: ${openDir} has ${accessOf(m)}; ${servedDir} has ${accessOf(copy)}`);
    }
    if (copy.licence.kind !== m.licence.kind) {
      problems.push(`${m.id}: ${openDir} has licence.kind ${JSON.stringify(m.licence.kind)}; ${servedDir} has licence.kind ${JSON.stringify(copy.licence.kind)}`);
    }
    return problems;
  }));
  const all: [Manifest, 'open' | 'proprietary'][] = [
    ...open.map((m): [Manifest, 'open'] => [m, 'open']),
    ...served.filter((m) => !openIds.has(m.id)).map((m): [Manifest, 'proprietary'] => [m, 'proprietary']),
  ];
  const publicIds = new Set(all.filter(([m]) => isPublicAdapter(m, openIds.has(m.id))).map(([m]) => m.id));
  const hiddenIds = new Set(all.map(([m]) => m.id).filter((id) => !publicIds.has(id)));

  const text = readText(textFile);
  const hiddenText = readText(hiddenTextFile);
  fail(`Adapters with text in ${textFile} or ${hiddenTextFile} but no manifest`,
    [...Object.keys(text), ...Object.keys(hiddenText)].filter((id) => !publicIds.has(id) && !hiddenIds.has(id)).sort());
  fail(`Adapters that are not public (not open, or with an access field) but have text in ${textFile}`,
    Object.keys(text).filter((id) => hiddenIds.has(id)).sort());
  fail(`Open adapters with text in ${hiddenTextFile}; their text belongs in ${textFile}`,
    Object.keys(hiddenText).filter((id) => publicIds.has(id)).sort());
  fail(`Adapters with no entry in ${textFile}`, [...publicIds].filter((id) => !(id in text)).sort());
  fail(`Hidden adapters with no entry in ${hiddenTextFile}`, [...hiddenIds].filter((id) => !(id in hiddenText)).sort());

  const byId = (a: CatalogueEntry, b: CatalogueEntry) => a.id.localeCompare(b.id);
  const listedEntries = all.filter(([m]) => publicIds.has(m.id)).map(([m, kind]) => entryOf(m, kind, text[m.id])).sort(byId);
  const hiddenEntries = all.filter(([m]) => hiddenIds.has(m.id)).map(([m, kind]) => entryOf(m, kind, hiddenText[m.id])).sort(byId);

  const privateProblems: string[] = [];
  for (const entry of [...listedEntries, ...hiddenEntries]) {
    for (const [where, value] of stringsOf(entry, '')) {
      for (const marker of privateMarkersIn(value)) privateProblems.push(`${entry.id}: ${where} contains ${marker}`);
    }
  }
  fail('Catalogue text that names something private', privateProblems);
  return { listed: listedEntries, hidden: hiddenEntries };
}

// The public catalogue alone (loadCatalogues' listed): every adapter the public site shows.
export function loadCatalogue(openDir: string, servedDir: string, textFile: string, schemaFile: string, hiddenTextFile: string,
  retiredFile: string): CatalogueEntry[] {
  return loadCatalogues(openDir, servedDir, textFile, schemaFile, hiddenTextFile, retiredFile).listed;
}
