import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it } from 'vitest';
import { loadDocs } from '../app/lib/docs.server';
import { PLAN } from './helpers/plan';
import { localAdapters } from '../app/lib/catalogue-call.server';
import { catalogue } from '../app/lib/site.server';

const REPO = path.join(__dirname, '../../..');
const DOCS = path.join(__dirname, '../content/docs');
const SNIPPETS = path.join(__dirname, '../content/snippets/sdk');

it('mentions every public name of the jeff package on an SDK page', () => {
  const names = JSON.parse(execFileSync('uv', ['run', 'python', '-c', 'import jeff, json; print(json.dumps(jeff.__all__))'],
    { cwd: REPO, encoding: 'utf8' })) as string[];
  const text = loadDocs(DOCS, 'https://api.example.com', ['python', 'node'], PLAN, localAdapters(catalogue())).pages
    .filter((p) => p.tab === 'sdk').map((p) => p.body).join('\n');
  const missing = names.filter((n) => !new RegExp(`\\b${n}\\b`).test(text));
  expect(missing).toEqual([]);
});

it('every snippet file is used on an SDK page or a guide (the worked examples), and every code block on an SDK page is a snippet or marked illustrative', () => {
  const read = (tab: string) => fs.readdirSync(path.join(DOCS, tab)).filter((f) => f.endsWith('.md'))
    .map((f) => fs.readFileSync(path.join(DOCS, tab, f), 'utf8'));
  const sources = read('sdk');
  const included = new Set([...sources, ...read('guides')].flatMap((s) => [...s.matchAll(/<!-- snippet: sdk\/(\S+) -->/g)].map((m) => m[1])));
  // A marker without an extension is a group (docs.server.ts: snippetGroup): <name>.<ext> and <name>.local.<ext>.
  const used = (f: string) => included.has(f) || included.has(f.replace(/(\.local)?\.(py|mjs|rs|cs|java)$/, ''));
  expect(fs.readdirSync(SNIPPETS).filter((f) => /\.(py|mjs|rs|cs|java|yaml)$/.test(f) && !used(f))).toEqual([]);
  // A fence written straight into a page is not run by the snippet check, so it must say so: ```<lang> illustrative.
  // Shell commands and data (bash, json, yaml) are not programs the check could run.
  const unmarked = sources.flatMap((s) => [...s.matchAll(/^```(\w+)(.*)$/gm)])
    .filter(([, lang, meta]) => !['bash', 'json', 'yaml', 'text'].includes(lang) && meta.trim() !== 'illustrative')
    .map(([line]) => line);
  expect(unmarked).toEqual([]);
});

// Every snippet group on an SDK page has its code in all five languages, through the API and on your machine alike
// (the loader refuses a group with a gap, naming the files; this names every group that has one).
it('has every snippet group in all five languages', () => {
  const groups = new Set(fs.readdirSync(SNIPPETS).filter((f) => /\.(py|mjs|rs|cs|java)$/.test(f))
    .map((f) => f.replace(/\.(py|mjs|rs|cs|java)$/, '')));
  const single = new Set(['decision-log-1.local', 'rust-1', 'csharp-1', 'java-1']);
  const gaps = [...groups].filter((g) => !single.has(g)).flatMap((g) => ['py', 'mjs', 'rs', 'cs', 'java']
    .filter((ext) => !fs.existsSync(path.join(SNIPPETS, `${g}.${ext}`))).map((ext) => `${g}.${ext}`));
  expect(gaps).toEqual([]);
});

// Snippets whose first line is `# offline` need no server and no model weights, so they run here; the others run in
// the pre-publish check (scripts/run-snippets.sh, and scripts/hub_examples.py --check and --check-local).
const offline = fs.readdirSync(SNIPPETS).filter((f) => f.endsWith('.py'))
  .filter((f) => fs.readFileSync(path.join(SNIPPETS, f), 'utf8').startsWith('# offline\n'));

// They run in a temporary copy of the snippets folder, as scripts/run-snippets.sh runs them: a snippet can read a file
// beside it, and nothing it writes lands in the source folder.
describe('offline SDK snippets', () => {
  let work = '';
  beforeAll(() => {
    work = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-snippets-'));
    fs.cpSync(SNIPPETS, work, { recursive: true });
  });
  afterAll(() => fs.rmSync(work, { recursive: true, force: true }));

  it('there are some', () => expect(offline.length).toBeGreaterThan(0));
  it.each(offline)('%s runs', (file) => {
    execFileSync('uv', ['run', '--project', REPO, 'python', file], { cwd: work, encoding: 'utf8', stdio: 'pipe' });
  }, 60_000);
});

// No Node block on the SDK pages is illustrative: each is a snippet (content/snippets/sdk/*.mjs) that
// scripts/run-node-samples.ts typechecks here and runs (offline ones here, the others in the pre-publish check).
it('has no illustrative Node block on an SDK page', () => {
  const illustrative = fs.readdirSync(path.join(DOCS, 'sdk')).filter((f) => f.endsWith('.md'))
    .filter((f) => /^```javascript illustrative$/m.test(fs.readFileSync(path.join(DOCS, 'sdk', f), 'utf8')));
  expect(illustrative).toEqual([]);
});

it('typechecks every Node sample against jeff-sdk and runs the offline ones', () => {
  execFileSync(path.join(__dirname, '../node_modules/.bin/tsx'), ['scripts/run-node-samples.ts', 'offline'],
    { cwd: path.join(__dirname, '..'), encoding: 'utf8', stdio: 'pipe' });
}, 120_000);
