// A worked example's call in Rust, C# and Java (examples.server.ts: exampleBlock shows it when the language is on,
// content/sdk-languages.yaml). Each writes the call the Python and Node tabs make, in its SDK's own API:
//
//   remote (an open function the SDK binds): the client made with the SDK's remote constructor (Jeff::remote,
//     JeffClient.RemoteAsync, Jeff.remote), the function bound with its context and labels, then called with the
//     input; the key from JEFF_API_KEY.
//   http (a proprietary function, which no SDK binds: its manifest is not in the SDK's catalogue): the SDK's HTTP
//     transport (RestTransport) sends the one call as POST /v1/decide, and the answer is read as the SDK's Decision.
//
// No snippet sets a proxy or any other connection option: each SDK reads HTTPS_PROXY, HTTP_PROXY and NO_PROXY from
// the environment itself.
//
//   local (the same open function on the reader's machine): the SDK's local client named with the adapter it calls
//     (Jeff::local_adapters, JeffClient.LocalAsync, Jeff.local with a list of adapters), which finds the engine
//     running for this user or starts it (contract/local-engine.md), as content/home-example-local.<ext> writes it;
//     no key.
//
// An offline example has none: its adapter runs locally, with the Python SDK. Every snippet prints the value and the
// confidence on one line, as the Python and Node ones do (scripts/sdk-samples.ts reads that line back); it compiles
// against its SDK (scripts/sdk-samples.ts compile).
import { indentLines, statements } from './code-statements.js';
import type { Example } from './examples.server.js';

type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

// The call's fields as POST /v1/decide takes them (examples.server.ts: requestBody's one call).
export type DecideCall = Record<string, Json>;

const hasContext = (e: Example) => e.context !== null && Object.keys(e.context).length > 0;

// A string written as a literal: the escapes every one of these languages reads, and \u escapes for the other
// control characters. Rust writes them \u{..}; C# also escapes the characters it counts as line breaks
// (U+0085, U+2028, U+2029), which may not appear raw in a string; Java's \u is read before the rest of the source, so
// a line break is never written as \u000a there (it is \n, handled first).
function quoted(text: string, language: 'rust' | 'csharp' | 'java'): string {
  let out = '"';
  for (const ch of text) {
    const code = ch.codePointAt(0)!;
    if (ch === '"') out += '\\"';
    else if (ch === '\\') out += '\\\\';
    else if (ch === '\n') out += '\\n';
    else if (ch === '\r') out += '\\r';
    else if (ch === '\t') out += '\\t';
    else if (code < 0x20 || code === 0x7f || (language === 'csharp' && (code === 0x85 || code === 0x2028 || code === 0x2029))) {
      out += language === 'rust' ? `\\u{${code.toString(16)}}` : `\\u${code.toString(16).padStart(4, '0')}`;
    } else out += ch;
  }
  return `${out}"`;
}

function number(value: number): string {
  if (!Number.isFinite(value)) throw new Error(`${value} is not a JSON number`);
  return JSON.stringify(value);
}

// ---------------------------------------------------------------------------------------------------------- Rust
// serde_json's json! macro: the JSON itself, in order (jeff-sdk builds serde_json with preserve_order).

function rustJson(value: Json, indent: string): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return String(value);
  if (typeof value === 'number') return number(value);
  if (typeof value === 'string') return quoted(value, 'rust');
  const inner = `${indent}    `;
  if (Array.isArray(value)) {
    if (value.length === 0) return '[]';
    return `[\n${value.map((v) => `${inner}${rustJson(v, inner)},`).join('\n')}\n${indent}]`;
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return '{}';
  return `{\n${entries.map(([k, v]) => `${inner}${quoted(k, 'rust')}: ${rustJson(v, inner)},`).join('\n')}\n${indent}}`;
}

// The attribute, the signature and reading the key: ceremony that wraps every example, not one of its own steps, so
// no blank line separates it from what follows -- the first statement the example itself makes is "client creation"
// (`let jeff = ...`), which does get a blank line before and after it, like every statement from there on.
const RUST_MAIN = [
  '#[tokio::main]',
  'async fn main() -> Result<(), Box<dyn std::error::Error>> {',
  '    let token = std::env::var("JEFF_API_KEY").map_err(|_| "set JEFF_API_KEY")?;',
];

// A local call has no key, so its main is only the attribute and the signature, and its first statement (making the
// client) follows the signature with no blank line, as content/home-example-local.rs writes it.
const RUST_LOCAL_MAIN = RUST_MAIN.slice(0, 2);

// The open function bound and called: `clientNames` are the SDK's names for the client's options (RemoteOptions with
// TokenSource, or StartOptions) for the use line, and `opening` the main function's opening statements up to and
// including making the client.
function rustSdk(e: Example, opening: string[][], clientNames: string[]): string {
  const options: string[] = [];
  if (hasContext(e)) options.push(`        context: Some(serde_json::from_value(json!(${rustJson(e.context, '        ')}))?),`);
  if (e.labels !== null) options.push(`        labels: Some(Labels::from_json(&json!(${rustJson(e.labels, '        ')}))?),`);
  const names = ['CallOptions', 'Jeff', ...(e.labels === null ? [] : ['Labels']), ...clientNames].sort().join(', ');
  const optionsBlock = options.length === 0 ? null : [
    '    let options = CallOptions {',
    ...options,
    '        ..CallOptions::default()',
    '    };',
  ];
  const decide = options.length === 0
    ? `    let decide = jeff.function(${quoted(e.function, 'rust')}, CallOptions::default()).await?;`
    : `    let decide = jeff.function(${quoted(e.function, 'rust')}, options).await?;`;
  return statements(
    [`use jeff_sdk::{${names}};`, 'use serde_json::json;'],
    ...opening,
    optionsBlock,
    decide,
    `    let decision = decide.call(json!(${rustJson(e.input, '    ')})).await?;`,
    '    println!("{:?} {:?}", decision.value, decision.confidence);',
    '    jeff.close().await;',
    ['    Ok(())', '}'],
  ).join('\n');
}

export function rustRemote(e: Example, apiOrigin: string): string {
  return rustSdk(e, [RUST_MAIN,
    [`    let jeff = Jeff::remote(${quoted(apiOrigin, 'rust')}, TokenSource::fixed(token), RemoteOptions::new(30_000)).await?;`]],
  ['RemoteOptions', 'TokenSource']);
}

// `adapter`: the name local mode is given for the adapter the call uses (its short name, CatalogueEntry.localName).
export function rustLocal(e: Example, adapter: string): string {
  return rustSdk(e, [[...RUST_LOCAL_MAIN, `    let jeff = Jeff::local_adapters([${quoted(adapter, 'rust')}], StartOptions::default()).await?;`]],
    ['StartOptions']);
}

export function rustHttp(call: DecideCall, apiOrigin: string): string {
  return statements(
    ['use jeff_sdk::{Decision, RestTransport, TokenSource, Transport};', 'use serde_json::json;'],
    RUST_MAIN,
    `    let transport = RestTransport::new(${quoted(apiOrigin, 'rust')}, TokenSource::fixed(token), 30_000)?;`,
    `    let call = json!(${rustJson(call, '    ')});`,
    '    let answers = transport.decide_calls(vec![call], json!("return")).await?;',
    '    let decision = Decision::from_json(&answers[0])?;',
    '    println!("{:?} {:?}", decision.value, decision.confidence);',
    '    transport.close().await;',
    ['    Ok(())', '}'],
  ).join('\n');
}

// ------------------------------------------------------------------------------------------------------------ C#
// System.Text.Json.Nodes: a JsonObject collection initializer keeps its order; strings, numbers and booleans become
// JsonNodes implicitly. A top-level program (.NET 6 or later; ImplicitUsings on, as `dotnet new console` makes it).
// A whole number is written as a long (2025L), the SDK's canonical type for one, as the Java code below does; a bare
// 2025 would be an Int32.

function csharpJson(value: Json, indent: string): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return String(value);
  if (typeof value === 'number') return Number.isInteger(value) ? `${number(value)}L` : number(value);
  if (typeof value === 'string') return quoted(value, 'csharp');
  const inner = `${indent}    `;
  if (Array.isArray(value)) {
    if (value.length === 0) return 'new JsonArray()';
    return `new JsonArray(\n${value.map((v) => `${inner}${csharpJson(v, inner)}`).join(',\n')})`;
  }
  const entries = Object.entries(value);
  if (entries.length === 0) return 'new JsonObject()';
  return `new JsonObject\n${indent}{\n${entries.map(([k, v]) => `${inner}[${quoted(k, 'csharp')}] = ${csharpJson(v, inner)},`).join('\n')}\n${indent}}`;
}

const CSHARP_IMPORTS = ['using System.Text.Json.Nodes;', 'using Jeff.Sdk;'];
// One statement (the `??` continuation keeps it with the line it falls back from), separated by a blank line from
// what comes before and after it like every other statement.
const CSHARP_TOKEN = ['var token = Environment.GetEnvironmentVariable("JEFF_API_KEY")', '    ?? throw new InvalidOperationException("Set JEFF_API_KEY");'];
const CSHARP_PRINT = 'Console.WriteLine($"{decision.Value} {decision.Confidence}");';

function csharpSdk(e: Example, before: (string[] | null)[]): string {
  const options: string[] = [];
  if (hasContext(e)) options.push(`    Context = ${csharpJson(e.context, '    ')},`);
  if (e.labels !== null) options.push(`    Labels = ${csharpJson(e.labels, '    ')},`);
  const bind = options.length === 0
    ? `await using var decide = await jeff.FunctionAsync(${quoted(e.function, 'csharp')});`
    : [`await using var decide = await jeff.FunctionAsync(${quoted(e.function, 'csharp')}, new CallOptions`, '{', ...options, '});'];
  return statements(
    CSHARP_IMPORTS,
    ...before,
    bind,
    `var decision = await decide.CallAsync(${csharpJson(e.input, '')});`,
    CSHARP_PRINT,
  ).join('\n');
}

export function csharpRemote(e: Example, apiOrigin: string): string {
  return csharpSdk(e, [
    CSHARP_TOKEN,
    ['await using var jeff = await JeffClient.RemoteAsync(', `    ${quoted(apiOrigin, 'csharp')}, token, new RemoteOptions { Timeout = TimeSpan.FromSeconds(30) });`],
  ]);
}

export function csharpLocal(e: Example, adapter: string): string {
  return csharpSdk(e, [
    [`await using var jeff = await JeffClient.LocalAsync(new[] { ${quoted(adapter, 'csharp')} });`],
  ]);
}

export function csharpHttp(call: DecideCall, apiOrigin: string): string {
  return statements(
    CSHARP_IMPORTS,
    CSHARP_TOKEN,
    `await using var transport = new RestTransport(${quoted(apiOrigin, 'csharp')}, token, TimeSpan.FromSeconds(30));`,
    `var call = ${csharpJson(call, '')};`,
    'var answers = await transport.DecideCallsAsync(new JsonArray(call), "return", CancellationToken.None);',
    'var decision = Decision.FromJson(answers[0]!.AsObject());',
    CSHARP_PRINT,
  ).join('\n');
}

// ---------------------------------------------------------------------------------------------------------- Java
// Java has no ordered map literal (Map.of keeps no order and refuses null), and the SDK takes labels, and passes
// nested values to the model, in the order they are given: so every map is a LinkedHashMap built with put, declared
// before the line that uses it. A list is List.of (Arrays.asList when it holds a null); a whole number is a long (the
// SDK reads JSON whole numbers as Long).

// Java's keywords and literals, which cannot name a variable (a map named after one gets "Map" added).
const JAVA_RESERVED = new Set(('abstract assert boolean break byte case catch char class const continue default do double else enum '
  + 'extends final finally float for goto if implements import instanceof int interface long native new package private '
  + 'protected public return short static strictfp super switch synchronized this throw throws transient try void '
  + 'volatile while true false null var yield record sealed permits _').split(' '));

class JavaValues {
  readonly lines: string[] = [];
  private readonly used = new Set<string>(['jeff', 'decide', 'decision', 'token', 'transport', 'answers', 'args']);

  private name(hint: string): string {
    const words = hint.replace(/[^A-Za-z0-9]+/g, ' ').trim().split(' ').filter((w) => w !== '');
    let base = words.length === 0 ? 'value' : words.map((w, i) => (i === 0 ? w.toLowerCase() : w[0].toUpperCase() + w.slice(1).toLowerCase())).join('');
    if (/^[0-9]/.test(base)) base = `value${base}`;
    if (JAVA_RESERVED.has(base)) base = `${base}Map`;
    let candidate = base;
    for (let n = 2; this.used.has(candidate); n += 1) candidate = `${base}${n}`;
    this.used.add(candidate);
    return candidate;
  }

  // An expression for `value`; a map is declared (and its own maps before it) as a variable named after `hint`.
  expr(value: Json, hint: string): string {
    if (value === null) return 'null';
    if (typeof value === 'boolean') return String(value);
    if (typeof value === 'number') return Number.isInteger(value) ? `${number(value)}L` : number(value);
    if (typeof value === 'string') return quoted(value, 'java');
    if (Array.isArray(value)) {
      const items = value.map((v, i) => this.expr(v, `${hint} ${i + 1}`));
      return `${value.includes(null) ? 'Arrays.asList' : 'List.of'}(${items.join(', ')})`;
    }
    const entries = Object.entries(value).map(([k, v]) => [k, this.expr(v, k)] as const);
    const variable = this.name(hint);
    this.lines.push(`var ${variable} = new LinkedHashMap<String, Object>();`);
    for (const [k, v] of entries) this.lines.push(`${variable}.put(${quoted(k, 'java')}, ${v});`);
    return variable;
  }
}

// The class around `body` (the statements of main after reading the key, already blank-line separated by
// `statements`), importing what the body uses of java.util. `token`: whether main reads the key first (a local call
// has none). Reading and checking the key is two statements of its
// own ceremony: a blank line between them, and another before `body`'s first statement, but none between `main`'s
// signature and the first of the two (ceremony that wraps the example, not one of its own steps).
function javaClass(body: string[], token: boolean): string {
  const text = body.join('\n');
  const used = [
    ['java.util.Arrays', /\bArrays\.asList\(/],
    ['java.util.LinkedHashMap', /\bLinkedHashMap</],
    ['java.util.List', /\bList(\.of\(|<)/],
    ['java.util.Map', /(?<![A-Za-z])Map</],
  ] as const;
  const withToken = token
    ? statements('String token = System.getenv("JEFF_API_KEY");', 'if (token == null) throw new IllegalStateException("Set JEFF_API_KEY");', body)
    : body;
  return [
    'import ai.jeff.sdk.*;',
    ...used.filter(([, pattern]) => pattern.test(text)).map(([name]) => `import ${name};`),
    '',
    'public class Decide {',
    '    public static void main(String[] args) {',
    ...indentLines(withToken, '        '),
    '    }',
    '}',
  ].join('\n');
}

const JAVA_PRINT = 'System.out.println(decision.value() + " " + decision.confidence());';

// Runs `build` (one call to `values.expr`) and returns its result together with the lines it added to `values`, so
// a map built for one option (context, then labels, then the input) is its own statement group, blank-line
// separated from the next one -- rather than one undifferentiated run of `put` calls.
function grouped<T>(values: JavaValues, build: () => T): { value: T; lines: string[] } {
  const before = values.lines.length;
  const value = build();
  return { value, lines: values.lines.slice(before) };
}

// The open function bound and called inside `open` (the try-with-resources line that makes the client).
function javaSdk(e: Example, open: string, token: boolean): string {
  const values = new JavaValues();
  let options = 'CallOptions.none()';
  const groups: string[][] = [];
  if (hasContext(e)) {
    const context = grouped(values, () => values.expr(e.context, 'context'));
    options += `.context(${context.value})`;
    groups.push(context.lines);
  }
  if (e.labels !== null) {
    const labels = grouped(values, () => values.expr(e.labels, 'labels'));
    options += `.labels(${labels.value})`;
    groups.push(labels.lines);
  }
  const input = grouped(values, () => values.expr(e.input, 'input'));
  groups.push(input.lines);
  return javaClass(statements(
    ...groups,
    open,
    `    BoundFunction decide = jeff.function(${quoted(e.function, 'java')}, ${options});`,
    `    Decision decision = decide.call(${input.value});`,
    [`    ${JAVA_PRINT}`, '}'],
  ), token);
}

export function javaRemote(e: Example, apiOrigin: string): string {
  return javaSdk(e, `try (RemoteJeff jeff = Jeff.remote(${quoted(apiOrigin, 'java')}, token, RemoteOptions.timeoutMs(30000))) {`, true);
}

export function javaLocal(e: Example, adapter: string): string {
  return javaSdk(e, `try (Jeff jeff = Jeff.local(List.of(${quoted(adapter, 'java')}))) {`, false);
}

export function javaHttp(call: DecideCall, apiOrigin: string): string {
  const values = new JavaValues();
  const callVariable = values.expr(call, 'call');
  return javaClass(statements(
    values.lines,
    `try (RestTransport transport = new RestTransport(${quoted(apiOrigin, 'java')}, TokenSource.of(token), 30000)) {`,
    `    List<Map<String, Object>> answers = transport.decideCalls(List.of(${callVariable}), "return");`,
    '    Decision decision = Decision.fromJson(answers.get(0));',
    [`    ${JAVA_PRINT}`, '}'],
  ), true);
}
