// Every open adapter is shown working in the docs: content/examples/*.yaml holds at least one example for each open
// adapter in catalogue/, and every example names a function of an adapter the catalogue lists (open, or served by
// the API). An example is offline exactly when its adapter is open but the live API does not serve it (no folder in
// results/v14-manifests): none since aml and nav, on base v1.3, left the catalogue on 5 November 2026.
import fs from 'node:fs';
import path from 'node:path';
import { expect, it } from 'vitest';
import { parse } from 'yaml';
import { loadCatalogue } from '../app/lib/catalogue.server';
import { loadExamples } from '../app/lib/examples.server';

const repo = path.join(__dirname, '../../..');
const served = path.join(repo, 'results/v14-manifests');
const entries = loadCatalogue(path.join(repo, 'catalogue'), served, path.join(__dirname, '../content/catalogue-text.yaml'),
  path.join(repo, 'contract/schemas/manifest.schema.json'), path.join(__dirname, '../content/owner-only/catalogue-text.yaml'),
  path.join(__dirname, '../content/owner-only/retired-adapters.yaml'));
const examples = [...loadExamples(path.join(__dirname, '../content/examples')).values()];
const adapterOf = (functionId: string) => functionId.slice(0, functionId.lastIndexOf('.'));

it('has an example for every open adapter', () => {
  const shown = new Set(examples.map((e) => adapterOf(e.function)));
  // An example names its adapter by the name the pages use (CatalogueEntry.name: jeff.routing.router-open for the
  // open router jeff.routing.router-open-v14).
  const missing = entries.filter((e) => e.kind === 'open' && !shown.has(e.name)).map((e) => e.id);
  expect(missing).toEqual([]);
});

it('names only functions of catalogue adapters', () => {
  const functions = new Set(entries.flatMap((e) => e.functions.map((f) => f.id)));
  expect(examples.filter((e) => !functions.has(e.function)).map((e) => e.id)).toEqual([]);
});

it('marks an example offline exactly when the live API does not serve its adapter', () => {
  const isServed = (id: string) => fs.existsSync(path.join(served, id, 'manifest.yaml'));
  const openNotServed = new Set(entries.filter((e) => e.kind === 'open' && !isServed(e.id)).map((e) => e.id));
  expect([...openNotServed].sort()).toEqual([]);
  const wrong = examples.filter((e) => e.offline !== openNotServed.has(adapterOf(e.function))).map((e) => e.id);
  expect(wrong).toEqual([]);
});

// Every open adapter appears on one of the "Open-source adapters" family pages (guides/oss-*.md): one of its examples
// is shown there with a `<!-- example: <id> -->` marker, and the page links to its catalogue page.
const guides = path.join(__dirname, '../content/docs/guides');
const FAMILY_PAGES = ['oss-reasoning', 'oss-agents', 'oss-support', 'oss-engineering', 'oss-retrieval', 'oss-safety-language'];

it('lists the family pages in the sidebar, as the group after Concepts', () => {
  const groups = parse(fs.readFileSync(path.join(guides, '_sidebar.yaml'), 'utf8')) as { group: string; pages: string[] }[];
  const at = groups.findIndex((g) => g.group === 'Open-source adapters');
  expect(at).toBeGreaterThan(0);
  expect(groups[at - 1].group).toBe('Concepts');
  expect(groups[at].pages).toEqual(FAMILY_PAGES);
});

it('shows every open adapter on a family page, with a link to its catalogue page', () => {
  const byId = new Map(examples.map((e) => [e.id, e]));
  const shown = new Set<string>();
  const linked = new Set<string>();
  for (const page of FAMILY_PAGES) {
    const text = fs.readFileSync(path.join(guides, `${page}.md`), 'utf8');
    for (const [, id] of text.matchAll(/^<!-- example: (\S+) -->$/gm)) {
      const example = byId.get(id);
      if (example === undefined) throw new Error(`${page}.md shows example ${id}, which content/examples does not define`);
      shown.add(adapterOf(example.function));
    }
    for (const [, id] of text.matchAll(/\]\(\/catalogue\/([^)#\s]+)\)/g)) linked.add(id);
  }
  const open = entries.filter((e) => e.kind === 'open');
  expect(open.filter((e) => !shown.has(e.name)).map((e) => e.id)).toEqual([]);
  expect(open.filter((e) => !linked.has(e.id)).map((e) => e.id)).toEqual([]);
});

// Text that reads an example's answer quotes the answer the example recorded, so a re-recording that changes an answer
// fails here until the text is updated. In that text:
// - the recorded value is quoted in backticks;
// - every other answer of the function quoted in backticks sits in a sentence that says "would" (an answer the
//   function could have given, such as "`conflicting` would mean they disagree");
// - every confidence written with three decimals (such as 0.615) is the recorded confidence.
// Offline examples have no recorded answer, so there is nothing to compare.
const byId = new Map(examples.map((e) => [e.id, e]));
const functions = new Map(entries.flatMap((e) => e.functions.map((f) => [f.id, f] as const)));

// The labels a function with caller labels always lists first, whatever the caller passes (the manifest's
// labels.fixed_first: answer_directly and ask_user for the tool router, other and none for the clause shortlist, none
// for the trading desk's company): answers the function can give though no caller passed them.
function fixedFirst(functionId: string): string[] {
  const entry = entries.find((e) => e.functions.some((f) => f.id === functionId));
  if (entry === undefined) throw new Error(`no catalogue adapter has the function ${functionId}`);
  const folder = [path.join(repo, 'catalogue', entry.id), path.join(served, entry.id)].find((d) => fs.existsSync(path.join(d, 'manifest.yaml')));
  if (folder === undefined) throw new Error(`${entry.id}: no manifest in catalogue/ or results/v14-manifests`);
  const manifest = parse(fs.readFileSync(path.join(folder, 'manifest.yaml'), 'utf8')) as { functions: Record<string, { labels: { fixed_first?: Record<string, string> } }> };
  const fn = manifest.functions[functionId.slice(functionId.lastIndexOf('.') + 1)];
  if (fn === undefined) throw new Error(`${entry.id}: its manifest has no function ${functionId}`);
  // A function with fixed labels has no fixed_first: it lists only its own labels.
  return fn.labels.fixed_first === undefined ? [] : Object.keys(fn.labels.fixed_first);
}

function staleQuotes(where: string, reading: string, id: string): string[] {
  const example = byId.get(id);
  if (example === undefined) throw new Error(`${where}: shows example ${id}, which content/examples does not define`);
  if (example.offline) return [];
  if (example.recorded === null) throw new Error(`${where}: shows example ${id}, which has no recorded answer`);
  const value = String(example.recorded.value);
  const fn = functions.get(example.function);
  if (fn === undefined) throw new Error(`${where}: no catalogue function ${example.function}`);
  const callerLabels = example.labels !== null && typeof example.labels === 'object' && !Array.isArray(example.labels)
    ? Object.keys(example.labels) : [];
  const answers = new Set([...(fn.labels ?? []), ...callerLabels, ...fixedFirst(example.function)]);
  if (!answers.has(value)) throw new Error(`${where}: recorded value ${value} is not one of the function's answers`);
  const stale: string[] = [];
  if (!reading.includes(`\`${value}\``)) stale.push(`${where}: does not quote the recorded value \`${value}\``);
  for (const sentence of reading.split(/(?<=[.!?])\s+/)) {
    for (const [, quoted] of sentence.matchAll(/`([^`]+)`/g)) {
      if (quoted !== value && answers.has(quoted) && !/\bwould\b/.test(sentence)) {
        stale.push(`${where}: quotes \`${quoted}\` as the answer, but the recorded value is \`${value}\``);
      }
    }
  }
  for (const [number] of reading.matchAll(/\b[01]\.\d{3}\b/g)) {
    if (Number(number) !== example.recorded.confidence) {
      stale.push(`${where}: quotes confidence ${number}, but the recorded confidence is ${example.recorded.confidence}`);
    }
  }
  return stale;
}

// On a family page, the text that reads an answer runs from the example's marker to the next marker or heading.
it('quotes each example\'s recorded answer on the family pages, not a stale one', () => {
  const stale: string[] = [];
  for (const page of FAMILY_PAGES) {
    const text = fs.readFileSync(path.join(guides, `${page}.md`), 'utf8');
    const markers = [...text.matchAll(/^<!-- example: (\S+) -->$/gm)];
    markers.forEach((marker, i) => {
      const id = marker[1];
      const start = marker.index + marker[0].length;
      const rest = text.slice(start, i + 1 < markers.length ? markers[i + 1].index : text.length);
      const heading = rest.search(/^#{2,3} /m);
      const reading = heading === -1 ? rest : rest.slice(0, heading);
      stale.push(...staleQuotes(`${page}.md, example ${id}`, reading, id));
    });
  }
  expect(stale).toEqual([]);
});

// On the-swarm.md, the text that reads an answer runs from the example's marker to the next marker or heading, the
// same convention as the family pages above.
it("quotes each example's recorded answer on the swarm page, not a stale one", () => {
  const text = fs.readFileSync(path.join(guides, 'the-swarm.md'), 'utf8');
  const markers = [...text.matchAll(/^<!-- example: (\S+) -->$/gm)];
  const stale: string[] = [];
  markers.forEach((marker, i) => {
    const id = marker[1];
    const start = marker.index + marker[0].length;
    const rest = text.slice(start, i + 1 < markers.length ? markers[i + 1].index : text.length);
    const heading = rest.search(/^#{2,3} /m);
    const reading = heading === -1 ? rest : rest.slice(0, heading);
    stale.push(...staleQuotes(`the-swarm.md, example ${id}`, reading, id));
  });
  expect(stale).toEqual([]);
});

// On an adapter's catalogue page, the sentence under each example (content/catalogue-examples.yaml: shows) reads it:
// the featured example's and each of the ones listed under it.
it("quotes each example's recorded answer on the catalogue pages, not a stale one", () => {
  const file = path.join(__dirname, '../content/catalogue-examples.yaml');
  type Shown = { examples: { example: string; shows: string }[] } | { finding: string };
  const shown = parse(fs.readFileSync(file, 'utf8')) as Record<string, Shown>;
  // An adapter listed with a finding shows no example, so it has no sentence to read.
  const stale = Object.entries(shown)
    .flatMap(([adapter, item]) => ('finding' in item ? [] : item.examples)
      .flatMap(({ example, shows }) => staleQuotes(`catalogue-examples.yaml, ${adapter}, ${example}`, shows, example)));
  expect(stale).toEqual([]);
});
