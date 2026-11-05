import path from 'node:path';
import fs from 'node:fs';
import os from 'node:os';
import { describe, expect, it } from 'vitest';
import { parse } from 'yaml';
import { codeNameOf, loadCatalogue, loadCatalogues } from '../app/lib/catalogue.server';

const f = path.join(__dirname, 'fixtures/catalogue');
const schema = path.join(__dirname, '../../../contract/schemas/manifest.schema.json');
// The fixtures: the open guard (public, its text in text.yaml) and an API-only sample (hidden, its text in
// hidden-text.yaml), with no retired adapter (retired.yaml). `load` gives the public catalogue, `loadBoth` both.
const RETIRED = path.join(f, 'retired.yaml');
const load = (text = path.join(f, 'text.yaml'), open = path.join(f, 'open'), served = path.join(f, 'served'), hiddenText = path.join(f, 'hidden-text.yaml'), retired = RETIRED) =>
  loadCatalogue(open, served, text, schema, hiddenText, retired);
const loadBoth = (text = path.join(f, 'text.yaml'), open = path.join(f, 'open'), served = path.join(f, 'served'), hiddenText = path.join(f, 'hidden-text.yaml'), retired = RETIRED) =>
  loadCatalogues(open, served, text, schema, hiddenText, retired);
const GUARD_TEXT = 'jeff.safety.guard: {title: G, summary: S, use_when: [], avoid_when: []}';
const SAMPLE_TEXT = 'jeff.operations.sample: {title: R, summary: S, use_when: [], avoid_when: []}';
// A text file in a temporary place with these lines.
function textFile(name: string, lines: string[]): string {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'catalogue-text-')), name);
  fs.writeFileSync(file, lines.length === 0 ? '{}\n' : lines.join('\n'));
  return file;
}

// A copy of the fixture open folder in a temporary place, with the guard manifest changed by `edit`.
function editedOpen(edit: (manifest: string) => string): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'catalogue-open-'));
  fs.mkdirSync(path.join(dir, 'jeff.safety.guard'));
  const source = fs.readFileSync(path.join(f, 'open/jeff.safety.guard/manifest.yaml'), 'utf8');
  fs.writeFileSync(path.join(dir, 'jeff.safety.guard/manifest.yaml'), edit(source));
  return dir;
}

// A copy of the fixture served folder in a temporary place, plus `extra` folders (name -> manifest text, or null for
// an empty folder).
function servedWith(extra: Record<string, string | null>): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'catalogue-served-'));
  fs.cpSync(path.join(f, 'served'), dir, { recursive: true });
  for (const [name, manifest] of Object.entries(extra)) {
    fs.mkdirSync(path.join(dir, name), { recursive: true });
    if (manifest !== null) fs.writeFileSync(path.join(dir, name, 'manifest.yaml'), manifest);
  }
  return dir;
}

const repo = path.join(__dirname, '../../..');

describe('the hidden catalogue', () => {
  const write = (name: string, lines: string[]) => {
    const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'catalogue-text-')), name);
    fs.writeFileSync(file, lines.length === 0 ? '{}\n' : lines.join('\n'));
    return file;
  };
  const guardText = 'jeff.safety.guard: {title: G, summary: S, use_when: [], avoid_when: []}';
  const sampleText = 'jeff.operations.sample: {title: R, summary: S, use_when: [], avoid_when: []}';
  const catalogues = (text: string, hidden: string) => loadCatalogues(path.join(f, 'open'), path.join(f, 'served'), text, schema, hidden, RETIRED);

  it('lists exactly the open adapters of catalogue/ with no access field, and hides every other one', () => {
    const { listed, hidden } = catalogues(write('text.yaml', [guardText]), write('hidden.yaml', [sampleText]));
    expect(listed.map((e) => e.id)).toEqual(['jeff.safety.guard']);
    expect(hidden.map((e) => [e.id, e.kind, e.title])).toEqual([['jeff.operations.sample', 'proprietary', 'R']]);
  });
  it('refuses a hidden adapter with text in the public file, even with its text in the hidden file too (final review I2)', () => {
    expect(() => catalogues(write('text.yaml', [guardText, sampleText]), write('hidden.yaml', [])))
      .toThrow(/not public .* but have text in .*text\.yaml:\njeff\.operations\.sample/);
    expect(() => catalogues(write('text.yaml', [guardText, sampleText]), write('hidden.yaml', [sampleText])))
      .toThrow(/not public .* but have text in .*text\.yaml:\njeff\.operations\.sample/);
  });
  it('refuses an open adapter with text in the owner-only file', () => {
    expect(() => catalogues(write('text.yaml', [guardText]), write('hidden.yaml', [sampleText, guardText])))
      .toThrow(/Open adapters with text in .*hidden\.yaml.*:\njeff\.safety\.guard/);
  });
  it('refuses text for an adapter with no manifest, in either file', () => {
    const ghost = 'jeff.safety.ghost: {title: X, summary: S, use_when: [], avoid_when: []}';
    expect(() => catalogues(write('text.yaml', [guardText, ghost]), write('hidden.yaml', [sampleText]))).toThrow(/no manifest:\njeff\.safety\.ghost/);
    expect(() => catalogues(write('text.yaml', [guardText]), write('hidden.yaml', [sampleText, ghost]))).toThrow(/no manifest:\njeff\.safety\.ghost/);
  });
  it('refuses a hidden adapter with no text in the owner-only file', () => {
    expect(() => catalogues(write('text.yaml', [guardText]), write('hidden.yaml', []))).toThrow(/Hidden adapters with no entry .*:\njeff\.operations\.sample/);
  });
  it('refuses an adapter whose catalogue/ copy and served copy disagree on access (re-review R3)', () => {
    const servedGuard = fs.readFileSync(path.join(f, 'served/jeff.safety.guard/manifest.yaml'), 'utf8');
    const served = servedWith({});
    fs.writeFileSync(path.join(served, 'jeff.safety.guard/manifest.yaml'), `${servedGuard}\naccess: owner\n`);
    expect(() => loadCatalogues(path.join(f, 'open'), served, write('text.yaml', [guardText]), schema, write('hidden.yaml', [sampleText]), RETIRED))
      .toThrow(/copies .* disagree on access or licence\.kind:\njeff\.safety\.guard: .* has no access field; .* has access "owner"/);
  });
  it('refuses an adapter whose catalogue/ copy and served copy disagree on licence.kind (re-review R3)', () => {
    const served = servedWith({});
    const servedGuard = fs.readFileSync(path.join(served, 'jeff.safety.guard/manifest.yaml'), 'utf8');
    fs.writeFileSync(path.join(served, 'jeff.safety.guard/manifest.yaml'), servedGuard.replace('kind: open', 'kind: proprietary'));
    expect(() => loadCatalogues(path.join(f, 'open'), served, write('text.yaml', [guardText]), schema, write('hidden.yaml', [sampleText]), RETIRED))
      .toThrow(/copies .* disagree on access or licence\.kind:\njeff\.safety\.guard: .* has licence\.kind "open"; .* has licence\.kind "proprietary"/);
  });
  it('hides an open adapter of catalogue/ whose manifest has an access field', () => {
    const open = editedOpen((m) => `${m}\naccess: owner\n`);
    const served = servedWith({});
    const servedGuard = fs.readFileSync(path.join(f, 'served/jeff.safety.guard/manifest.yaml'), 'utf8');
    fs.writeFileSync(path.join(served, 'jeff.safety.guard/manifest.yaml'), `${servedGuard}\naccess: owner\n`);
    const { listed, hidden } = loadCatalogues(open, served, write('text.yaml', []), schema, write('hidden.yaml', [sampleText, guardText]), RETIRED);
    expect(listed).toEqual([]);
    expect(hidden.map((e) => e.id)).toEqual(['jeff.operations.sample', 'jeff.safety.guard']);
  });
});

describe('loadCatalogue', () => {
  it('dedupes an adapter present in both folders, open wins', () => {
    const entries = load();
    expect(entries.filter((e) => e.id === 'jeff.safety.guard')).toHaveLength(1);
    expect(entries.find((e) => e.id === 'jeff.safety.guard')?.kind).toBe('open');
    expect(loadBoth().hidden.find((e) => e.id === 'jeff.operations.sample')?.kind).toBe('proprietary');
  });
  it('names an adapter whose ID has a version part by its unversioned ID, and every other by its ID', () => {
    // The open router's manifest, served here without a catalogue/ copy, so hidden.
    const router = fs.readFileSync(path.join(repo, 'catalogue/jeff.routing.router-open-v14/manifest.yaml'), 'utf8');
    const hiddenText = textFile('hidden.yaml', [SAMPLE_TEXT, 'jeff.routing.router-open-v14: {title: Router, summary: S, use_when: [], avoid_when: []}']);
    const { listed, hidden } = loadBoth(undefined, undefined, servedWith({ 'jeff.routing.router-open-v14': router }), hiddenText);
    const entry = hidden.find((e) => e.id === 'jeff.routing.router-open-v14');
    expect(entry?.name).toBe('jeff.routing.router-open');
    expect(entry?.functions.map((fn) => fn.id)).toEqual(['jeff.routing.router-open.specialist']);
    expect(listed.find((e) => e.id === 'jeff.safety.guard')?.name).toBe('jeff.safety.guard');
  });
  it('gives proprietary entries no weights or licence', () => {
    const sample = loadBoth().hidden.find((e) => e.id === 'jeff.operations.sample');
    expect(sample?.weights).toBeNull();
    expect(sample?.licence).toBeNull();
    expect(JSON.stringify(sample)).not.toMatch(/teamsuzie|\/mnt\//);
  });
  it('builds an open entry from its manifest and its public text', () => {
    const guard = load().find((e) => e.id === 'jeff.safety.guard');
    expect(guard).toMatchObject({
      version: '1.4.0',
      domain: 'safety',
      title: 'Prompt injection guard',
      base: 'jeff-base v1.4',
      weights: { repo: 'mstrasser/jeff-adapter-guard', revision: 'v1.4' },
      licence: 'Apache-2.0',
      input: { key: 'text' },
    });
    expect(guard?.functions.map((fn) => fn.id)).toEqual(['jeff.safety.guard.attempt', 'jeff.safety.guard.kind']);
    expect(guard?.functions[0].labels).toEqual(['false', 'true']);
    expect(guard?.functions[1].labels).toContain('jailbreak');
  });
  it('sorts entries by id', () => {
    const { listed, hidden } = loadBoth(textFile('text.yaml', [GUARD_TEXT]), undefined, undefined,
      textFile('hidden.yaml', [SAMPLE_TEXT]));
    expect(listed.map((e) => e.id)).toEqual(['jeff.safety.guard']);
    expect(hidden.map((e) => e.id)).toEqual(['jeff.operations.sample']);
  });
  it('fails when an adapter has no public text', () => {
    expect(() => load(textFile('text.yaml', []))).toThrow('jeff.safety.guard');
  });
  it('fails when public text names an adapter that is not in the catalogue', () => {
    const ghost = 'jeff.safety.ghost: {title: X, summary: S, use_when: [], avoid_when: []}';
    expect(() => load(textFile('text.yaml', [GUARD_TEXT, ghost]))).toThrow('jeff.safety.ghost');
  });
  it('fails when public or hidden text contains a private marker', () => {
    expect(() => load(textFile('text.yaml', ['jeff.safety.guard: {title: G, summary: Built for Delta, use_when: [], avoid_when: []}'])))
      .toThrow(/jeff.safety.guard.*Delta/);
    expect(() => load(undefined, undefined, undefined, textFile('hidden.yaml', ['jeff.operations.sample: {title: R, summary: Built for Delta, use_when: [], avoid_when: []}'])))
      .toThrow(/jeff.operations.sample.*Delta/);
  });
  it('fails when a manifest does not match the schema, naming the file and the path', () => {
    const open = editedOpen((m) => m.replace('version: 1.4.0', 'version: one'));
    expect(() => load(undefined, open)).toThrow(/jeff\.safety\.guard\/manifest\.yaml.*\/version/s);
  });
  it('fails when an adapter in the open folder is not open-licensed', () => {
    const open = editedOpen((m) => m.replace('kind: open', 'kind: proprietary'));
    expect(() => load(undefined, open)).toThrow(/jeff\.safety\.guard/);
  });
  it('fails when an open adapter has no public weights repo', () => {
    const open = editedOpen((m) => m.replace('repo: mstrasser/jeff-adapter-guard', 'repo: someone/jeff-adapter-guard'));
    expect(() => load(undefined, open)).toThrow(/jeff\.safety\.guard/);
  });
  it('fails when a function instruction contains a private marker', () => {
    const open = editedOpen((m) => m.replace('What kind of text is this', 'What kind of text on datigator is this'));
    expect(() => load(undefined, open)).toThrow(/jeff\.safety\.guard.*functions.*datigator/s);
  });
  it('shows the own context and input of a function that declares them, and null for the others', () => {
    const entry = loadBoth().hidden.find((e) => e.id === 'jeff.operations.sample');
    const reply = entry?.functions.find((fn) => fn.key === 'reply');
    expect(reply?.context).toEqual([expect.objectContaining({ key: 'page', required: true })]);
    expect(reply?.input?.key).toBe('message');
    const route = entry?.functions.find((fn) => fn.key === 'route');
    expect(route?.context).toBeNull();
    expect(route?.input).toBeNull();
    expect(entry?.input.key).toBe('thread');
  });
  it('skips the staged-not-served folder', () => {
    const served = servedWith({ 'staged-not-served/jeff.operations.sample-v2': 'not: a manifest\n' });
    const { listed, hidden } = loadBoth(undefined, undefined, served);
    expect([...listed, ...hidden].map((e) => e.id)).toEqual(['jeff.safety.guard', 'jeff.operations.sample']);
  });
  it('fails on any other folder without a manifest, naming it', () => {
    const served = servedWith({ 'old-stuff': null });
    expect(() => load(undefined, undefined, served)).toThrow(/old-stuff: a folder with no manifest\.yaml/);
  });
  it('excludes a retired adapter even though the served folder still holds its manifest.yaml', () => {
    const manifest = fs.readFileSync(path.join(f, 'served/jeff.operations.sample/manifest.yaml'), 'utf8')
      .replace('id: jeff.operations.sample', 'id: jeff.operations.sample-old');
    const served = servedWith({ 'jeff.operations.sample-old': manifest });
    const retired = textFile('retired.yaml', ['- jeff.operations.sample-old']);
    const { listed, hidden } = loadBoth(undefined, undefined, served, undefined, retired);
    expect([...listed, ...hidden].map((e) => e.id)).toEqual(['jeff.safety.guard', 'jeff.operations.sample']);
  });
  it('refuses a retired adapter with no manifest in the served folder, naming it', () => {
    const retired = textFile('retired.yaml', ['- jeff.operations.gone']);
    expect(() => loadBoth(undefined, undefined, undefined, undefined, retired)).toThrow(/Retired adapters .* with no manifest .*:\njeff\.operations\.gone/);
  });
});

describe('the real catalogue', () => {
  // Owner, 4 November 2026: only the open adapters are public ("They are private"). The public catalogue is exactly
  // catalogue/ (the open adapters), and every other adapter the API serves is hidden.
  it('lists exactly the open adapters of catalogue/, and hides every other adapter the API serves', () => {
    const repo = path.join(__dirname, '../../..');
    const hub = path.join(__dirname, '..');
    const { listed, hidden } = loadCatalogues(
      path.join(repo, 'catalogue'), path.join(repo, 'results/v14-manifests'), path.join(hub, 'content/catalogue-text.yaml'),
      schema, path.join(hub, 'content/owner-only/catalogue-text.yaml'), path.join(hub, 'content/owner-only/retired-adapters.yaml'),
    );
    const openIds = fs.readdirSync(path.join(repo, 'catalogue'), { withFileTypes: true }).filter((d) => d.isDirectory()).map((d) => d.name).sort();
    expect(listed.map((e) => e.id)).toEqual(openIds);
    expect(listed.every((e) => e.kind === 'open')).toBe(true);
    expect(hidden.every((e) => e.kind === 'proprietary')).toBe(true);
    // The hidden set is exactly the adapters with text in content/owner-only (none in a copy without that content).
    const hiddenText = parse(fs.readFileSync(path.join(hub, 'content/owner-only/catalogue-text.yaml'), 'utf8')) as Record<string, unknown>;
    expect(hidden.map((e) => e.id)).toEqual(Object.keys(hiddenText).sort());
  });
});

describe('codeNameOf', () => {
  it('leaves a key that is already a code name unchanged', () => {
    expect(codeNameOf('application')).toBe('application');
  });
  it('lower-cases and underscores a key with spaces, matching the schema’s own example', () => {
    expect(codeNameOf('counterparty name')).toBe('counterparty_name');
  });
  it('turns an apostrophe (and the space around it) into one underscore, trimmed at the ends', () => {
    expect(codeNameOf("reviewer's note on the draft")).toBe('reviewer_s_note_on_the_draft');
  });
});

describe('a field’s codeName', () => {
  it('derives it from the key when the manifest declares none', () => {
    const guard = load().find((e) => e.id === 'jeff.safety.guard');
    expect(guard?.context.find((c) => c.key === 'application')).toMatchObject({ key: 'application', codeName: 'application' });
  });
  it('uses the manifest’s own code_name instead of deriving one, when it declares one', () => {
    const open = editedOpen((m) => m.replace(
      '- key: application\n  description: One sentence on what the AI model that will read the text is used for.\n  required: true',
      '- key: application\n  code_name: app_ctx\n  description: One sentence on what the AI model that will read the text is used for.\n  required: true',
    ));
    const guard = load(undefined, open).find((e) => e.id === 'jeff.safety.guard');
    expect(guard?.context.find((c) => c.key === 'application')).toMatchObject({ key: 'application', codeName: 'app_ctx' });
  });
  it('derives the code name of a key with an apostrophe and spaces (the manifest declares none)', () => {
    const entry = loadBoth().hidden.find((e) => e.id === 'jeff.operations.sample');
    const field = entry?.context.find((c) => c.key === "reviewer's note on the draft");
    expect(field?.codeName).toBe('reviewer_s_note_on_the_draft');
  });
});
