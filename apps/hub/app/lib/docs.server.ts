import fs from 'node:fs';
import path from 'node:path';
import { parse } from 'yaml';
import { BENCHMARK_LINK_PREFIX, type Benchmark } from './benchmarks.js';
import { loadBenchmarks } from './benchmarks.server.js';
import { EXAMPLE_LANGUAGE } from './example-block.js';
import { exampleBlock, loadExamples, type Example, type LocalAdapters } from './examples.server.js';
import { playgroundLink } from './playground-samples.server.js';
import { SDK_LANGUAGES, type SdkLanguage } from './sdk-languages.js';
import { SNIPPET_EXTENSIONS, SNIPPET_FENCE, type SnippetBlock } from './snippet-block.js';
import { headingId } from './slug.js';

export type Tab = 'guides' | 'sdk';
export const TABS: Tab[] = ['guides', 'sdk'];
export interface Heading { id: string; text: string; depth: 2 | 3 }
// `benchmarks`: the benchmark sets the page links by name (content/benchmarks.yaml), in the order it first links them.
export interface DocPage { tab: Tab; id: string; title: string; group: string; body: string; headings: Heading[]; benchmarks: Benchmark[] }
export type Sidebar = Record<Tab, { group: string; pages: { id: string; title: string }[] }[]>;

function readPage(file: string): { title: string; body: string } {
  const text = fs.readFileSync(file, 'utf8');
  const match = /^---\n([\s\S]*?)\n---\n/.exec(text);
  if (match === null) throw new Error(`${file}: starts without front matter (---, title: ..., ---)`);
  const front = parse(match[1]) as { title?: unknown };
  if (typeof front.title !== 'string' || front.title === '') throw new Error(`${file}: front matter has no title`);
  return { title: front.title, body: text.slice(match[0].length) };
}

function headingsOf(body: string): Heading[] {
  const out: Heading[] = [];
  let fenced = false;
  for (const line of body.split('\n')) {
    if (line.startsWith('```')) fenced = !fenced;
    if (fenced) continue;
    const match = /^(##|###) (.+)$/.exec(line);
    if (match) out.push({ id: headingId(match[2].trim()), text: match[2].trim(), depth: match[1].length as 2 | 3 });
  }
  return out;
}

const SNIPPET = /^<!-- snippet: (\S+) -->$/gm;
const SNIPPET_LANGUAGE: Record<string, string> = {
  '.py': 'python', '.mjs': 'javascript', '.rs': 'rust', '.cs': 'csharp', '.java': 'java', '.yaml': 'yaml',
};
// A snippet's first line may say how the checks run it; it is a marker for the check, not code, and is left out.
// `# offline` (Python) and `// offline` (the other languages): it needs no server and no model, so it runs in the
// ordinary test run too (Python and Node; Rust, C# and Java are built and run by scripts/sdk-samples.ts). `// local`
// (Node): it attaches to an engine on this machine (scripts/hub_examples.py --check-local).
const SNIPPET_MARKER = /^(# offline|\/\/ offline|\/\/ local)\n/;

function snippetCode(file: string): string {
  return fs.readFileSync(file, 'utf8').replace(SNIPPET_MARKER, '').replace(/\n+$/, '');
}

// The code of one snippet group (snippet-block.ts) in the languages shown: `<name>.<ext>` is the code that runs
// through the API (or needs neither), `<name>.local.<ext>` the same on the reader's machine. Each form a group has
// must exist in all five languages, shown or not, so turning a language on never finds a gap; a group with neither
// form, or with a form in only some languages, is an error naming the files missing.
function snippetGroup(name: string, snippetsRoot: string, page: string, languages: readonly SdkLanguage[]): SnippetBlock {
  const form = (suffix: string) => {
    const files = SDK_LANGUAGES.map((language) => ({ language, file: path.join(snippetsRoot, `${name}${suffix}.${SNIPPET_EXTENSIONS[language]}`) }));
    const missing = files.filter((f) => !fs.existsSync(f.file));
    if (missing.length === files.length) return null;
    if (missing.length > 0) throw new Error(`${page}: snippet ${name}${suffix} has no ${missing.map((f) => path.basename(f.file)).join(', ')}`);
    return files.filter((f) => languages.includes(f.language)).map(({ language, file }) => ({ language, code: snippetCode(file) }));
  };
  const code = form('');
  const local = form('.local');
  if (code === null && local === null) throw new Error(`${page}: no snippet files for ${name} (looked for ${name}.py, ${name}.local.py, ... in ${snippetsRoot})`);
  return { id: name, code, local };
}

// Replaces each `<!-- snippet: <path> -->` line with the snippet at content/snippets/<path> (beside the docs folder),
// so a page shows exactly the code the snippet checks run (Python: scripts/run-snippets.sh and
// scripts/hub_examples.py; Node: scripts/run-node-samples.ts; Rust, C# and Java: scripts/sdk-samples.ts). A path with
// an extension is one file, shown as a fenced block; a path without one is a group in every SDK language, shown as a
// fenced block of JSON in SNIPPET_FENCE (components/docs/Snippet.tsx renders it with the language tabs).
function expandSnippets(body: string, snippetsRoot: string, page: string, languages: readonly SdkLanguage[]): string {
  return body.replace(SNIPPET, (_, name: string) => {
    const extension = path.extname(name);
    if (extension === '') return `\`\`\`${SNIPPET_FENCE}\n${JSON.stringify(snippetGroup(name, snippetsRoot, page, languages))}\n\`\`\``;
    const file = path.join(snippetsRoot, name);
    if (!fs.existsSync(file)) throw new Error(`${page}: no snippet file ${name} (looked for ${file})`);
    const language = SNIPPET_LANGUAGE[extension];
    if (language === undefined) throw new Error(`${page}: snippet ${name} is not .py, .mjs, .rs, .cs, .java or .yaml`);
    return `\`\`\`${language}\n${snippetCode(file)}\n\`\`\``;
  });
}

const EXAMPLE = /^<!-- example: (\S+) -->$/;
// Replaces each `<!-- example: <id> -->` line with the example's block (examples.server.ts: exampleBlock) as a
// fenced block of JSON in EXAMPLE_LANGUAGE. The examples folder is read only when a page has a marker (`examples`
// holds it once read); an unknown id, or a referenced example with no recorded answer, is an error naming it.
function expandExamples(body: string, examples: () => Map<string, Example>, page: string, apiOrigin: string,
  languages: readonly SdkLanguage[], adapters: LocalAdapters): string {
  // A marker inside a fenced code block is code being shown (a page explaining markers, say), not a marker.
  let fenced = false;
  return body.split('\n').map((line) => {
    if (line.startsWith('```')) fenced = !fenced;
    const match = EXAMPLE.exec(line);
    if (fenced || match === null) return line;
    const id = match[1];
    const example = examples().get(id);
    if (example === undefined) throw new Error(`${page}: no example ${id} in content/examples`);
    let block;
    try {
      block = exampleBlock(example, apiOrigin, languages, playgroundLink(example), adapters);
    } catch (e) {
      throw new Error(`${page}: ${(e as Error).message}`);
    }
    return `\`\`\`${EXAMPLE_LANGUAGE}\n${JSON.stringify(block)}\n\`\`\``;
  }).join('\n');
}

// The benchmark sets a page links by name, `[name](#benchmark-<id>)`, in the order it first links them. The
// benchmarks file is read only when a page has such a link (`benchmarks` holds it once read); an id it does not list
// is an error naming the page and the id.
const BENCHMARK_LINK = new RegExp(`\\]\\(${BENCHMARK_LINK_PREFIX}([^)\\s]+)\\)`, 'g');
function benchmarksOf(body: string, benchmarks: () => Map<string, Benchmark>, page: string): Benchmark[] {
  const ids = [...new Set([...body.matchAll(BENCHMARK_LINK)].map((m) => m[1]))];
  return ids.map((id) => {
    const benchmark = benchmarks().get(id);
    if (benchmark === undefined) throw new Error(`${page}: links benchmark ${id}, which content/benchmarks.yaml does not describe`);
    return benchmark;
  });
}

// The plan's price and monthly allowance as the pages say them (public-settings.server.ts: priceText, tokensText),
// which replace `{{price}}` and `{{allowance}}`: they are settings, never written into a page.
export interface PlanText { price: string; allowance: string }

// `languages`: the SDK languages a worked example shows code in (content/sdk-languages.yaml), Python first.
// `adapters`: the catalogue's adapters, for each worked example's local call (catalogue-call.server.ts: localAdapters).
export function loadDocs(root: string, apiOrigin: string, languages: readonly SdkLanguage[], plan: PlanText,
  adapters: LocalAdapters): { pages: DocPage[]; sidebar: Sidebar } {
  const pages: DocPage[] = [];
  const sidebar = { guides: [], sdk: [] } as Sidebar;
  let examples: Map<string, Example> | null = null;
  const examplesOnce = () => {
    if (examples === null) examples = loadExamples(path.join(root, '..', 'examples'));
    return examples;
  };
  let benchmarks: Map<string, Benchmark> | null = null;
  const benchmarksOnce = () => {
    if (benchmarks === null) benchmarks = loadBenchmarks(path.join(root, '..', 'benchmarks.yaml'));
    return benchmarks;
  };
  for (const tab of TABS) {
    const sidebarFile = path.join(root, tab, '_sidebar.yaml');
    const groups = parse(fs.readFileSync(sidebarFile, 'utf8')) as { group: string; pages: string[] }[];
    const listed = new Set<string>();
    for (const { group, pages: ids } of groups) {
      const entries: { id: string; title: string }[] = [];
      for (const id of ids) {
        if (listed.has(id)) throw new Error(`${sidebarFile}: ${id} is listed twice`);
        listed.add(id);
        const file = path.join(root, tab, `${id}.md`);
        const { title, body: raw } = readPage(file);
        const body = expandExamples(expandSnippets(raw, path.join(root, '..', 'snippets'), file, languages), examplesOnce, file, apiOrigin, languages, adapters)
          .replaceAll('{{origin}}', apiOrigin).replaceAll('{{price}}', plan.price).replaceAll('{{allowance}}', plan.allowance);
        pages.push({ tab, id, title, group, body, headings: headingsOf(body), benchmarks: benchmarksOf(body, benchmarksOnce, file) });
        entries.push({ id, title });
      }
      sidebar[tab].push({ group, pages: entries });
    }
    const files = fs.readdirSync(path.join(root, tab)).filter((f) => f.endsWith('.md')).map((f) => f.slice(0, -3));
    const unlisted = files.filter((id) => !listed.has(id));
    if (unlisted.length > 0) throw new Error(`${sidebarFile}: pages not in the sidebar: ${unlisted.join(', ')}`);
  }
  return { pages, sidebar };
}

const LINK = /\]\(([^)\s]+)\)/g;

export function checkLinks(pages: DocPage[], extraPaths: Set<string>): void {
  const byPath = new Map(pages.map((p) => [`/docs/${p.tab}/${p.id}`, p]));
  const bad: string[] = [];
  for (const page of pages) {
    for (const [, target] of page.body.matchAll(LINK)) {
      const where = `/docs/${page.tab}/${page.id}`;
      if (target.startsWith('#docs/') || target === '#api') { bad.push(`${where}: console link ${target}`); continue; }
      if (!target.startsWith('/')) continue;
      const [pathPart, anchor] = target.split('#');
      const linked = byPath.get(pathPart);
      if (linked === undefined && !extraPaths.has(pathPart)) { bad.push(`${where}: no page ${pathPart}`); continue; }
      if (anchor !== undefined && linked !== undefined && !linked.headings.some((h) => h.id === anchor)) {
        bad.push(`${where}: ${pathPart} has no heading #${anchor}`);
      }
    }
  }
  if (bad.length > 0) throw new Error(`Broken docs links:\n${bad.join('\n')}`);
}
