// The "Call it" code on an adapter's catalogue page: its first function called with placeholders, in every SDK
// language the site shows and as curl, by the same generators as every worked example (examples.server.ts: callCode).
// The input is "<its key>" and each context field "<its code name>", keyed by the code name a caller passes. A
// proprietary adapter's function is not bound by the SDK, so it is called as a plain POST /v1/decide (via http); an
// open adapter's call also has its local form (examples.server.ts: localCallCode), with the adapter's short name.
import type { CatalogueEntry } from './catalogue.server';
import type { ExampleBlock } from './example-block';
import { callCode, localCallCode, type Example, type LocalAdapters } from './examples.server.js';
import type { SdkLanguage } from './sdk-languages';

export function placeholderExample(entry: CatalogueEntry): Example {
  const fn = entry.functions[0];
  if (fn === undefined) throw new Error(`${entry.id}: the catalogue lists no function, so its page has no call to show`);
  // A function's own context and input when it declares them, else the adapter's (catalogue.server.ts).
  const context = fn.context === null ? entry.context : fn.context;
  const input = fn.input === null ? entry.input : fn.input;
  return {
    id: `${entry.id}-call`,
    name: null,
    function: fn.id,
    context: context.length === 0 ? null : Object.fromEntries(context.map((field) => [field.codeName, `<${field.codeName}>`])),
    input: `<${input.key}>`,
    labels: null,
    via: entry.kind === 'open' ? 'sdk' : 'http',
    offline: false,
    recorded: null,
  };
}

export function adapterCall(entry: CatalogueEntry, apiOrigin: string, languages: readonly SdkLanguage[]): Pick<ExampleBlock, 'code' | 'local'> & { curl: string } {
  const example = placeholderExample(entry);
  const { code, curl } = callCode(example, apiOrigin, languages);
  if (curl === null) throw new Error(`${entry.id}: its call has no curl`);
  if (entry.kind === 'proprietary') return { code, curl, local: null };
  if (entry.localName === null) throw new Error(`${entry.id}: an open adapter with no short name`);
  return { code, curl, local: localCallCode(example, languages, { id: entry.id, localName: entry.localName }) };
}

// Every adapter of `entries` by the name its functions start with (CatalogueEntry.name): an open one's id and short
// name, for a worked example's local call (examples.server.ts: localCallOf); null for a proprietary one.
export function localAdapters(entries: readonly CatalogueEntry[]): LocalAdapters {
  return new Map(entries.map((entry) => {
    if (entry.kind === 'proprietary') return [entry.name, null];
    if (entry.localName === null) throw new Error(`${entry.id}: an open adapter with no short name`);
    return [entry.name, { id: entry.id, localName: entry.localName }];
  }));
}
