// The worked examples on each adapter's catalogue page: content/catalogue-examples.yaml names, for every adapter in
// the catalogue, one to MAX_EXAMPLES examples from content/examples/, each with a sentence on what it shows (the first
// is the page's featured one) -- or, for an adapter that answered a clear case wrongly, the finding, and the page then
// shows no example. An adapter with neither, an entry for an adapter not in the catalogue, an example listed twice, or
// an example of another adapter's function (or of its own named otherwise than by the name the pages use,
// CatalogueEntry.name) is an error naming it.
import fs from 'node:fs';
import { parse } from 'yaml';
import type { CatalogueEntry } from './catalogue.server';
import type { Example } from './examples.server';
import { privateMarkersIn } from './private-markers.js';

// The most examples one page lists: the featured one and four more (the playground offers the same number,
// playground-samples.server.ts: SAMPLES_PER_ADAPTER).
export const MAX_EXAMPLES = 5;

export interface ShownExample { example: Example; shows: string }
export type CatalogueExample = { examples: ShownExample[] } | { finding: string };

const adapterOf = (functionId: string) => functionId.slice(0, functionId.lastIndexOf('.'));

function fail(file: string, problems: string[]): void {
  if (problems.length > 0) throw new Error(`${file}: invalid catalogue examples:\n${problems.join('\n')}`);
}

function isText(value: unknown): value is string {
  return typeof value === 'string' && value.trim() !== '';
}

function isMap(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

// One listed example, or the problem with it (`where` names the adapter and the place in its list).
function shownExample(where: string, item: unknown, examples: Map<string, Example>, adapterName: string | undefined): ShownExample | string {
  if (!isMap(item) || Object.keys(item).sort().join(',') !== 'example,shows') return `${where}: must have exactly example and shows`;
  if (!isText(item.example) || !isText(item.shows)) return `${where}: example and shows must be non-empty strings`;
  const example = examples.get(item.example);
  if (example === undefined) return `${where}: no example ${item.example} in content/examples`;
  // An example names its adapter by the name the pages use (catalogue.server.ts, CatalogueEntry.name).
  if (adapterOf(example.function) !== adapterName) return `${where}: example ${item.example} calls ${example.function}, a function of another adapter`;
  if (example.name === null) return `${where}: example ${item.example} has no name (the list under the featured example shows it)`;
  if (item.shows.split('`').length % 2 === 0) return `${where}: shows has a backtick with no closing one`;
  const marker = privateMarkersIn(item.shows as string)[0];
  if (marker !== undefined) return `${where}: shows names something private (${marker})`;
  return { example, shows: item.shows };
}

export function loadCatalogueExamples(file: string, examples: Map<string, Example>, entries: CatalogueEntry[]): Map<string, CatalogueExample> {
  const data: unknown = parse(fs.readFileSync(file, 'utf8'));
  if (!isMap(data)) throw new Error(`${file}: not a map of adapter id to examples`);
  const ids = new Set(entries.map((e) => e.id));
  const names = new Map(entries.map((e) => [e.id, e.name]));
  const problems: string[] = [
    ...entries.filter((e) => !(e.id in data)).map((e) => `${e.id}: no entry (give it examples and what each shows, or a finding)`),
    ...Object.keys(data).filter((id) => !ids.has(id)).map((id) => `${id}: not an adapter in the catalogue`),
  ];
  const out = new Map<string, CatalogueExample>();
  for (const [id, item] of Object.entries(data)) {
    if (!ids.has(id)) continue;
    if (!isMap(item)) { problems.push(`${id}: not a map`); continue; }
    const keys = Object.keys(item).sort().join(',');
    if (keys === 'finding') {
      if (!isText(item.finding)) { problems.push(`${id}: finding must be a sentence`); continue; }
      out.set(id, { finding: item.finding });
      continue;
    }
    if (keys !== 'examples') { problems.push(`${id}: must have either examples, or finding only (has ${keys})`); continue; }
    const list = item.examples;
    if (!Array.isArray(list) || list.length === 0 || list.length > MAX_EXAMPLES) {
      problems.push(`${id}: examples must be a list of 1 to ${MAX_EXAMPLES}`);
      continue;
    }
    const shown = list.map((entry, i) => shownExample(`${id}, example ${i + 1}`, entry, examples, names.get(id)));
    const bad = shown.filter((s): s is string => typeof s === 'string');
    if (bad.length > 0) { problems.push(...bad); continue; }
    const good = shown as ShownExample[];
    const twice = good.map((s) => s.example.id).filter((eid, i, all) => all.indexOf(eid) !== i);
    if (twice.length > 0) { problems.push(`${id}: lists ${twice.join(', ')} more than once`); continue; }
    out.set(id, { examples: good });
  }
  fail(file, problems);
  return out;
}
