// The docs' worked examples: content/examples/<family>.yaml (format and recording: scripts/hub_examples.py). A page
// shows one with a `<!-- example: <id> -->` line, which the docs loader replaces with the block made here (an adapter's
// catalogue page shows one too): the case (context, input, labels), the call in Python, Node and curl, and the answer
// recorded from the live API with its options of highest probability.
import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'yaml';
import type { ExampleBlock, ExampleField } from './example-block.js';
import { statements } from './code-statements.js';
import { csharpHttp, csharpLocal, csharpRemote, javaHttp, javaLocal, javaRemote, rustHttp, rustLocal, rustRemote } from './sdk-code.server.js';
import { ENGINE_SOCKET } from './engine-socket.js';
import type { SdkLanguage } from './sdk-languages.js';
import { singleQuoted } from './shell.js';

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

export interface Example {
  id: string;
  // A short phrase saying what the case is ("Supplier invoice to the office manager"), unique among its adapter's
  // examples: the playground's menu of samples shows it. Every example file gives one (test/examples.test.ts); null
  // only in a test's own example.
  name: string | null;
  function: string;
  context: Record<string, Json> | null;
  input: Json;
  labels: Json;
  // http: a proprietary function, which the SDK does not bind, so the call is a plain POST /v1/decide.
  via: 'sdk' | 'http';
  // An adapter the live server does not serve: it runs locally with Jeff.local, and has no recorded answer.
  offline: boolean;
  recorded: {
    value: boolean | number | string;
    confidence: number | null;
    topOptions: { option: string; probability: number }[];
  } | null;
}

// routes_to_example: on a router example, the id of the specialist example it routes to (checked by
// scripts/hub_examples.py --validate).
const FIELDS = new Set(['id', 'name', 'function', 'context', 'input', 'labels', 'via', 'offline', 'routes_to_example', 'recorded']);
const RECORDED_FIELDS = ['at', 'confidence', 'server', 'top_options', 'value'];
const TOP_OPTIONS = 3;
// Context keys are code names, the names a caller passes (catalogue.server.ts: codeNameOf).
const CODE_NAME = /^[a-z][a-z0-9_]*$/;
const SERVER_VERSION = /^v\d+\.\d+$/;
const ISO_DATE = /^\d{4}-\d{2}-\d{2}$/;

function exampleOf(raw: unknown, where: string): Example {
  if (raw === null || typeof raw !== 'object' || Array.isArray(raw)) throw new Error(`${where}: an example must be a map`);
  const e = raw as Record<string, unknown>;
  const unknown = Object.keys(e).filter((k) => !FIELDS.has(k));
  if (unknown.length > 0) throw new Error(`${where}: unknown fields ${unknown.join(', ')}`);
  for (const field of ['id', 'function'] as const) {
    if (typeof e[field] !== 'string' || e[field] === '') throw new Error(`${where}: ${field} must be a non-empty string`);
  }
  if (e.name !== undefined && (typeof e.name !== 'string' || e.name.trim() === '')) throw new Error(`${where}: name must be a non-empty string`);
  if ((e.function as string).split('.').length < 4) {
    throw new Error(`${where}: function ${e.function as string} is not a full function id (adapter id, a dot, the function's key)`);
  }
  if (!('input' in e)) throw new Error(`${where}: no input`);
  if (e.input === null) throw new Error(`${where}: input must not be null`);
  const context = e.context;
  if (context !== undefined && (context === null || typeof context !== 'object' || Array.isArray(context))) {
    throw new Error(`${where}: context must be a map of field code name to value`);
  }
  const badKeys = Object.keys(context ?? {}).filter((k) => !CODE_NAME.test(k));
  if (badKeys.length > 0) throw new Error(`${where}: context keys must be code names: ${badKeys.join(', ')}`);
  if (e.routes_to_example !== undefined && (typeof e.routes_to_example !== 'string' || e.routes_to_example === '')) {
    throw new Error(`${where}: routes_to_example must be the id of the specialist example the router picks`);
  }
  if (e.via !== undefined && e.via !== 'http') throw new Error(`${where}: via must be http (or absent)`);
  if (e.offline !== undefined && e.offline !== true) throw new Error(`${where}: offline must be true (or absent)`);
  const offline = e.offline === true;
  if (offline && e.via !== undefined) throw new Error(`${where}: an offline example cannot be via http`);
  if (offline && e.recorded !== undefined) throw new Error(`${where}: an offline example has no recorded answer`);
  let recorded: Example['recorded'] = null;
  if (e.recorded !== undefined) {
    const r = e.recorded as Record<string, unknown> | null;
    if (r === null || typeof r !== 'object' || Object.keys(r).sort().join(',') !== RECORDED_FIELDS.join(',')) {
      throw new Error(`${where}: recorded must have exactly ${RECORDED_FIELDS.join(', ')}`);
    }
    // The same rules as scripts/hub_examples.py's check_recorded.
    if (!(typeof r.value === 'boolean' || typeof r.value === 'string' || Number.isInteger(r.value))) {
      throw new Error(`${where}: recorded.value must be a string, true/false or a whole number`);
    }
    if (r.confidence !== null && (typeof r.confidence !== 'number' || r.confidence < 0 || r.confidence > 1)) {
      throw new Error(`${where}: recorded.confidence must be a number from 0 to 1, or null`);
    }
    if (typeof r.server !== 'string' || !SERVER_VERSION.test(r.server)) {
      throw new Error(`${where}: recorded.server must be a base version such as v1.4`);
    }
    if (typeof r.at !== 'string' || !ISO_DATE.test(r.at)) throw new Error(`${where}: recorded.at must be a date written YYYY-MM-DD`);
    const top = r.top_options;
    if (!Array.isArray(top) || top.length < 1 || top.length > TOP_OPTIONS) {
      throw new Error(`${where}: recorded.top_options must be a list of 1 to ${TOP_OPTIONS} options`);
    }
    const topOptions = top.map((o: unknown) => {
      const option = o as Record<string, unknown> | null;
      if (option === null || typeof option !== 'object' || Object.keys(option).sort().join(',') !== 'option,probability'
        || typeof option.option !== 'string' || typeof option.probability !== 'number' || option.probability < 0 || option.probability > 1) {
        throw new Error(`${where}: recorded.top_options must hold maps of option (a string) and probability (0 to 1)`);
      }
      return { option: option.option, probability: option.probability };
    });
    recorded = { value: r.value as boolean | number | string, confidence: r.confidence as number | null, topOptions };
  }
  return {
    id: e.id as string,
    name: e.name === undefined ? null : e.name as string,
    function: e.function as string,
    context: (context ?? null) as Record<string, Json> | null,
    input: e.input as Json,
    labels: (e.labels ?? null) as Json,
    via: e.via === 'http' ? 'http' : 'sdk',
    offline,
    recorded,
  };
}

// Every example in `dir`, by id; an id used twice (in any files) is an error.
export function loadExamples(dir: string): Map<string, Example> {
  if (!fs.existsSync(dir)) throw new Error(`No examples folder ${dir}`);
  const files = fs.readdirSync(dir).filter((f) => f.endsWith('.yaml')).sort().map((name) => {
    const file = path.join(dir, name);
    try {
      return { file, data: parse(fs.readFileSync(file, 'utf8')) as unknown };
    } catch (error) {
      throw new Error(`${file}: not valid YAML: ${(error as Error).message}`, { cause: error });
    }
  });
  return examplesFromFiles(files);
}

// The examples of example files already parsed (each file's name, for the errors, and its YAML as data), by id, in
// file order: what loadExamples reads from disk, and what the hub's function gets from function-content.generated.ts.
export function examplesFromFiles(files: readonly { file: string; data: unknown }[]): Map<string, Example> {
  const out = new Map<string, Example>();
  for (const { file, data } of files) {
    if (!Array.isArray(data)) throw new Error(`${file}: must be a list of examples`);
    data.forEach((raw, i) => {
      const example = exampleOf(raw, `${file}[${i}]`);
      if (out.has(example.id)) throw new Error(`${file}: example id ${example.id} is used twice`);
      const adapter = example.function.slice(0, example.function.lastIndexOf('.'));
      const twin = example.name === null ? undefined : [...out.values()]
        .find((other) => other.name === example.name && other.function.slice(0, other.function.lastIndexOf('.')) === adapter);
      if (twin !== undefined) throw new Error(`${file}: example ${example.id} has the name ${JSON.stringify(example.name)}, as ${twin.id} of the same adapter ${adapter} has`);
      out.set(example.id, example);
    });
  }
  return out;
}

// The POST /v1/decide body for the example's one call (the same body scripts/hub_examples.py's request_body sends).
// The playground (playground.server.ts) sends exactly this body, so the call it makes is the call its code shows.
export function requestBody(e: Example): { calls: Record<string, Json>[] } {
  const call: Record<string, Json> = { function: e.function, input: e.input };
  if (e.context !== null && Object.keys(e.context).length > 0) call.context = e.context;
  if (e.labels !== null) call.labels = e.labels;
  return { calls: [call] };
}

// A JSON value written as a Python literal; `indent` is the indentation of the line it starts on.
function py(value: Json, indent: string): string {
  if (value === null) return 'None';
  if (value === true) return 'True';
  if (value === false) return 'False';
  if (typeof value === 'number' || typeof value === 'string') return JSON.stringify(value);
  const inner = `${indent}    `;
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    return `[\n${value.map((v) => `${inner}${py(v, inner)},`).join('\n')}\n${indent}]`;
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return '{}';
  return `{\n${entries.map(([k, v]) => `${inner}${JSON.stringify(k)}: ${py(v, inner)},`).join('\n')}\n${indent}}`;
}

// Python through the SDK, as content/home-example.py writes it: Jeff.remote, the function bound with its context and
// labels, then called with the input. One blank line between each statement -- client creation, binding the function,
// the call, the print -- so they do not run together. An offline example runs locally (pythonLocal, with its
// adapter's id: the catalogue does not list it).
function pythonSdk(e: Example, apiOrigin: string): string {
  if (e.offline) return pythonLocal(e, e.function.slice(0, e.function.lastIndexOf('.')));
  const indent = '    ';
  return statements(
    ['import os', '', 'import jeff'],
    `with jeff.Jeff.remote(${JSON.stringify(apiOrigin)}, token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:`,
    ...pythonCall(e, indent),
  ).join('\n');
}

// Binding the function, the call and the print, each a statement, indented by `indent`.
function pythonCall(e: Example, indent: string): string[] {
  const args = [JSON.stringify(e.function)];
  if (e.context !== null && Object.keys(e.context).length > 0) args.push(`context=${py(e.context, indent)}`);
  if (e.labels !== null) args.push(`labels=${py(e.labels, indent)}`);
  return [
    `${indent}decide = client.function(${args.join(', ')})`,
    `${indent}decision = decide(${py(e.input, indent)})`,
    `${indent}print(decision.value, decision.confidence)`,
  ];
}

// Python on the reader's machine, as content/home-example-local.py writes it: Jeff.local with the adapter named as
// Jeff.local takes it (`adapter`: the catalogue's short name, CatalogueEntry.localName, or an offline adapter's id).
function pythonLocal(e: Example, adapter: string): string {
  return statements('import jeff', `client = jeff.Jeff.local([${JSON.stringify(adapter)}])`, ...pythonCall(e, '')).join('\n');
}

function pythonHttp(e: Example, apiOrigin: string): string {
  return statements(
    ['import os', '', 'import httpx'],
    [
      'response = httpx.post(',
      `    ${JSON.stringify(`${apiOrigin}/v1/decide`)},`,
      '    headers={"Authorization": f"Bearer {os.environ[\'JEFF_API_KEY\']}"},',
      `    json=${py(requestBody(e), '    ')},`,
      '    timeout=30,',
      ')',
    ],
    'response.raise_for_status()',
    'decision = response.json()["decisions"][0]',
    'print(decision["value"], decision["confidence"])',
  ).join('\n');
}

// A JSON value written as a JavaScript literal, keys unquoted where they are plain identifiers (as
// content/home-example.mjs writes them); `indent` is the indentation of the line it starts on.
const IDENTIFIER = /^[A-Za-z_$][A-Za-z0-9_$]*$/;
function js(value: Json, indent: string): string {
  if (value === null || typeof value === 'boolean' || typeof value === 'number' || typeof value === 'string') return JSON.stringify(value);
  const inner = `${indent}  `;
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    return `[\n${value.map((v) => `${inner}${js(v, inner)},`).join('\n')}\n${indent}]`;
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return '{}';
  return `{\n${entries.map(([k, v]) => `${inner}${IDENTIFIER.test(k) ? k : JSON.stringify(k)}: ${js(v, inner)},`).join('\n')}\n${indent}}`;
}

// Node through the jeff-sdk package, as content/home-example.mjs (and, made with Jeff.local on the engine socket,
// home-example-local.mjs) writes it: the client, the function bound with its context and labels, then called with
// the input. One blank line between each statement, as in Python.
function nodeSdk(e: Example, client: string): string {
  const options: string[] = [];
  if (e.context !== null && Object.keys(e.context).length > 0) options.push(`  context: ${js(e.context, '  ')},`);
  if (e.labels !== null) options.push(`  labels: ${js(e.labels, '  ')},`);
  const bound = options.length === 0
    ? `const decide = await jeff.function(${JSON.stringify(e.function)});`
    : `const decide = await jeff.function(${JSON.stringify(e.function)}, {\n${options.join('\n')}\n});`;
  return statements(
    'import { Jeff } from "jeff-sdk";',
    client,
    bound,
    `const decision = await decide.call(${js(e.input, '')});`,
    'console.log(decision.value, decision.confidence);',
    'await jeff.close();',
  ).join('\n');
}

const nodeRemoteClient = (apiOrigin: string) =>
  `const jeff = await Jeff.remote(${JSON.stringify(apiOrigin)}, process.env.JEFF_API_KEY, { timeoutMs: 30000 });`;
const NODE_LOCAL_CLIENT = `const jeff = await Jeff.local(${JSON.stringify(ENGINE_SOCKET)}, { timeoutMs: 30000 });`;

// Node for a proprietary function, which the SDK does not bind: a plain POST /v1/decide with fetch.
function nodeHttp(e: Example, apiOrigin: string): string {
  return statements(
    [
      `const response = await fetch(${JSON.stringify(`${apiOrigin}/v1/decide`)}, {`,
      '  method: "POST",',
      '  headers: {',
      '    Authorization: `Bearer ${process.env.JEFF_API_KEY}`,',
      '    "Content-Type": "application/json",',
      '  },',
      `  body: JSON.stringify(${js(requestBody(e) as unknown as Json, '  ')}),`,
      '});',
    ],
    'if (!response.ok) throw new Error(`POST /v1/decide answered ${response.status}: ${await response.text()}`);',
    'const [decision] = (await response.json()).decisions;',
    'console.log(decision.value, decision.confidence);',
  ).join('\n');
}

// A value as the case shows it: text as it is; a list one item a line; a map one "key: value" line each, a nested
// list or map written as compact JSON.
function shown(value: Json): string {
  if (typeof value === 'string') return value;
  if (Array.isArray(value)) return value.map((v) => `- ${typeof v === 'string' ? v : JSON.stringify(v)}`).join('\n');
  if (value !== null && typeof value === 'object') {
    return Object.entries(value).map(([k, v]) => `${k}: ${typeof v === 'string' ? v : JSON.stringify(v)}`).join('\n');
  }
  return JSON.stringify(value);
}

function labelFields(labels: Json): ExampleField[] | null {
  if (labels === null) return null;
  if (Array.isArray(labels)) return labels.map((key) => ({ name: String(key), value: '' }));
  if (typeof labels === 'object') {
    return Object.entries(labels).map(([name, v]) => ({ name, value: v === null ? '' : typeof v === 'string' ? v : JSON.stringify(v) }));
  }
  throw new Error(`labels must be a list or a map, not ${JSON.stringify(labels)}`);
}

function curl(e: Example, apiOrigin: string): string {
  return [
    `curl -s ${apiOrigin}/v1/decide \\`,
    '  -H "Authorization: Bearer $JEFF_API_KEY" \\',
    '  -H "Content-Type: application/json" \\',
    `  -d ${singleQuoted(JSON.stringify(requestBody(e)))}`,
  ].join('\n');
}

// The example's call in `language`; null for an offline example in any language but Python (its adapter runs on
// the reader's machine with the Python SDK, and the API does not serve it).
export function exampleCode(e: Example, language: SdkLanguage, apiOrigin: string): string | null {
  if (language === 'python') return e.via === 'http' ? pythonHttp(e, apiOrigin) : pythonSdk(e, apiOrigin);
  if (e.offline) return null;
  const call = requestBody(e).calls[0];
  if (language === 'node') return e.via === 'http' ? nodeHttp(e, apiOrigin) : nodeSdk(e, nodeRemoteClient(apiOrigin));
  if (language === 'rust') return e.via === 'http' ? rustHttp(call, apiOrigin) : rustRemote(e, apiOrigin);
  if (language === 'csharp') return e.via === 'http' ? csharpHttp(call, apiOrigin) : csharpRemote(e, apiOrigin);
  return e.via === 'http' ? javaHttp(call, apiOrigin) : javaRemote(e, apiOrigin);
}

// The example's call in each of `languages` (the shown ones, in tab order) and as curl: the code and curl fields of
// its ExampleBlock. The playground (playground.server.ts) shows this for the input the visitor edited, so its code is
// the code the pages show. An offline example has only Python and no curl: its adapter is not served by the API.
export function callCode(e: Example, apiOrigin: string, languages: readonly SdkLanguage[]): Pick<ExampleBlock, 'code' | 'curl'> {
  return {
    code: languages.flatMap((language) => {
      const code = exampleCode(e, language, apiOrigin);
      return code === null ? [] : [{ language, code }];
    }),
    curl: e.offline ? null : curl(e, apiOrigin),
  };
}

// The same call made on the reader's machine, in `language`: Python with Jeff.local (`localName`, the adapter's short
// name, CatalogueEntry.localName), the others with their SDK's local client on the engine socket (ENGINE_SOCKET), as
// content/home-example-local.<ext> writes it. Only an open adapter's function called through the SDK has one: a
// via-http example (a proprietary function) or an offline one (already local) is an error naming it.
export function exampleLocalCode(e: Example, language: SdkLanguage, localName: string): string {
  if (e.via === 'http') throw new Error(`example ${e.id}: a proprietary function (via http) has no local call`);
  if (e.offline) throw new Error(`example ${e.id}: an offline example is already a local call`);
  if (language === 'python') return pythonLocal(e, localName);
  if (language === 'node') return nodeSdk(e, NODE_LOCAL_CLIENT);
  if (language === 'rust') return rustLocal(e);
  if (language === 'csharp') return csharpLocal(e);
  return javaLocal(e);
}

// The example's local call in each of `languages` (the shown ones, in tab order) and the adapter the engine must
// serve for it (its id, for `jeff runtime serve --adapter`): the local field of its ExampleBlock.
export function localCallCode(e: Example, languages: readonly SdkLanguage[], adapter: { id: string; localName: string }): NonNullable<ExampleBlock['local']> {
  return { adapter: adapter.id, code: languages.map((language) => ({ language, code: exampleLocalCode(e, language, adapter.localName) })) };
}

// The adapters a page's examples can be called locally with, by the name an example's function starts with
// (CatalogueEntry.name): an open adapter's id and short name; null for a proprietary one, which has no local call.
export type LocalAdapters = ReadonlyMap<string, { id: string; localName: string } | null>;

// `LocalAdapters` from data already shaped this way (function-content.generated.ts: LOCAL_ADAPTERS_SETTING, written
// by scripts/function-content.mjs from the catalogue at generation time -- the hub's function reads this instead of
// the catalogue itself, so it never imports catalogue.server.ts, whose manifest reads and private-markers check
// belong only to the build). `where` names the data in an error (the generated module's export name).
export function localAdaptersFrom(data: unknown, where: string): LocalAdapters {
  if (data === null || typeof data !== 'object' || Array.isArray(data)) throw new Error(`${where}: must be a map of adapter name to its local call info or null`);
  const map = new Map<string, { id: string; localName: string } | null>();
  for (const [name, value] of Object.entries(data as Record<string, unknown>)) {
    if (value === null) { map.set(name, null); continue; }
    const entry = value as Partial<{ id: unknown; localName: unknown }> | null;
    if (entry === null || typeof entry !== 'object' || Array.isArray(entry) || typeof entry.id !== 'string' || typeof entry.localName !== 'string') {
      throw new Error(`${where}: ${name} must be null or a map of id and localName (both strings)`);
    }
    map.set(name, { id: entry.id, localName: entry.localName });
  }
  return map;
}

// `e`'s local call (localCallCode), or null when it has none: an offline or via-http example, or a proprietary
// adapter's. An adapter `adapters` does not list is an error naming it.
export function localCallOf(e: Example, languages: readonly SdkLanguage[], adapters: LocalAdapters): ExampleBlock['local'] {
  if (e.offline || e.via === 'http') return null;
  const name = e.function.slice(0, e.function.lastIndexOf('.'));
  const adapter = adapters.get(name);
  if (adapter === undefined) throw new Error(`example ${e.id}: its adapter ${name} is not in the catalogue`);
  return adapter === null ? null : localCallCode(e, languages, adapter);
}

// The block a page shows for `e`, with code in each of `languages` (the shown ones, in order; Python first) and
// `playground`, its "Open in playground" link (playground-samples.server.ts: playgroundLink), and its local call
// where its adapter is open (localCallOf, from `adapters`: the catalogue's open adapters). An example that is not offline must have a recorded answer: the page promises one, so an unrecorded example is an
// error naming it (run scripts/hub_examples.py --record).
export function exampleBlock(e: Example, apiOrigin: string, languages: readonly SdkLanguage[], playground: string | null,
  adapters: LocalAdapters): ExampleBlock {
  if (!e.offline && e.recorded === null) {
    throw new Error(`example ${e.id} has no recorded answer (run scripts/hub_examples.py --record)`);
  }
  if (languages[0] !== 'python') throw new Error(`example ${e.id}: the languages shown must start with python, not ${languages.join(', ')}`);
  let labels: ExampleField[] | null;
  try {
    labels = labelFields(e.labels);
  } catch (error) {
    throw new Error(`example ${e.id}: ${(error as Error).message}`, { cause: error });
  }
  return {
    id: e.id,
    context: Object.entries(e.context ?? {}).map(([name, value]) => ({ name, value: shown(value) })),
    input: shown(e.input),
    labels,
    ...callCode(e, apiOrigin, languages),
    local: localCallOf(e, languages, adapters),
    answer: e.recorded === null
      ? null
      : {
        value: String(e.recorded.value),
        confidence: e.recorded.confidence === null ? 'null' : String(e.recorded.confidence),
        top: e.recorded.topOptions.map(({ option, probability }) => ({ option, probability: String(probability) })),
      },
    playground,
  };
}
