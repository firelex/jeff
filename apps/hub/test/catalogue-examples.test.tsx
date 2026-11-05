import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { CODE_LANGUAGE_STORAGE_KEY } from '../app/lib/code-language';
import { SDK_LABELS } from '../app/lib/sdk-languages';
import { createRoutesStub } from 'react-router';
import { parse } from 'yaml';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { CatalogueEntry } from '../app/lib/catalogue.server';
import { loadCatalogueExamples, MAX_EXAMPLES } from '../app/lib/catalogue-examples.server';
import type { Example } from '../app/lib/examples.server';
import { playgroundSamples, SAMPLES_PER_ADAPTER } from '../app/lib/playground-samples.server';
import { forgetOwnerView } from '../app/lib/owner-view';
import { HUB_ROOT } from '../app/lib/paths.server';
import { catalogue, catalogueExamples, shownLanguages } from '../app/lib/site.server';
import CatalogueAdapter, { loader } from '../app/routes/catalogue-adapter';

// The adapters that answered a clear case wrongly even after their input was made clearer, so their pages show no
// example. None today: every adapter page shows a real-world example. A new adapter with no example fails here (and
// its page fails to build); one listed as a finding must be added here, so the gap is a deliberate decision.
const FINDINGS: string[] = [];

// The adapters whose page lists fewer than five examples: the ones the live API does not serve (no example of theirs
// can be recorded, so each keeps its one). None since aml and nav left the catalogue (5 November 2026). Every other
// adapter lists five, with different outcomes where it has several.
const FEWER_THAN_FIVE: string[] = [];

describe('every adapter page has an example', () => {
  it('names an example for every catalogue adapter except the listed findings', () => {
    const shown = catalogueExamples();
    const entries = catalogue();
    expect(entries.filter((e) => !shown.has(e.id)).map((e) => e.id)).toEqual([]);
    const findings = entries.filter((e) => 'finding' in (shown.get(e.id) as object)).map((e) => e.id).sort();
    expect(findings).toEqual(FINDINGS);
  });

  it(`lists ${MAX_EXAMPLES} examples for every adapter the API serves, except the listed ones`, () => {
    const shown = catalogueExamples();
    const short = catalogue()
      .filter((e) => { const s = shown.get(e.id); return s !== undefined && 'examples' in s && s.examples.length < MAX_EXAMPLES; })
      .map((e) => e.id).sort();
    expect(short).toEqual(FEWER_THAN_FIVE);
  });

  it("offers in the playground exactly the examples each adapter's page lists, as many as it takes", () => {
    expect(SAMPLES_PER_ADAPTER).toBe(MAX_EXAMPLES);
    const shown = catalogueExamples();
    const differ = catalogue().filter((e) => {
      const s = shown.get(e.id);
      if (s === undefined || !('examples' in s) || FEWER_THAN_FIVE.includes(e.id)) return false;
      // The playground offers open adapters only (owner, 4 November 2026), and the catalogue lists no other kind.
      expect(e.kind).toBe('open');
      const listed = s.examples.map((x) => x.example.id).sort();
      const offered = playgroundSamples().filter((p) => p.adapter === e.name).map((p) => p.id).sort();
      return listed.join(' ') !== offered.join(' ');
    }).map((e) => e.id);
    expect(differ).toEqual([]);
  });
});

const entry = (id: string, name: string = id) => ({ id, name }) as CatalogueEntry;
const example = (id: string, fn: string, name: string | null = `Case ${id}`) => ({ id, function: fn, name }) as Example;
const EXAMPLES = new Map([
  ['guard-1', example('guard-1', 'jeff.safety.guard.attempt')],
  ['guard-2', example('guard-2', 'jeff.safety.guard.kind')],
  ['spam-1', example('spam-1', 'jeff.safety.spam.message_type')],
  ['unnamed', example('unnamed', 'jeff.safety.guard.attempt', null)],
]);

function fileWith(text: string): string {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'catalogue-examples-')), 'catalogue-examples.yaml');
  fs.writeFileSync(file, text);
  return file;
}

describe('loadCatalogueExamples', () => {
  const entries = [entry('jeff.safety.guard'), entry('jeff.safety.spam')];
  const spamFinding = 'jeff.safety.spam: {finding: "F."}';

  it('reads the examples in order, each with its sentence, and a finding', () => {
    const file = fileWith([
      'jeff.safety.guard: {examples: [{example: guard-1, shows: "An attack, so the answer is `true`."}, {example: guard-2, shows: "S."}]}',
      'jeff.safety.spam: {finding: "Answered legitimate for a phishing message."}',
    ].join('\n'));
    const out = loadCatalogueExamples(file, EXAMPLES, entries);
    expect(out.get('jeff.safety.guard')).toEqual({ examples: [
      { example: EXAMPLES.get('guard-1'), shows: 'An attack, so the answer is `true`.' },
      { example: EXAMPLES.get('guard-2'), shows: 'S.' },
    ] });
    expect(out.get('jeff.safety.spam')).toEqual({ finding: 'Answered legitimate for a phishing message.' });
  });

  it('fails naming an adapter with no entry, and an entry for an adapter not in the catalogue', () => {
    const file = fileWith('jeff.safety.guard: {examples: [{example: guard-1, shows: "S."}]}\njeff.safety.nothing: {finding: "F."}');
    expect(() => loadCatalogueExamples(file, EXAMPLES, entries)).toThrow(/jeff\.safety\.spam: no entry[\s\S]*jeff\.safety\.nothing: not an adapter/);
  });

  it("fails for an unknown example, another adapter's example, an unnamed one, one listed twice, or an entry with both examples and a finding", () => {
    const load = (guard: string) => () => loadCatalogueExamples(fileWith(`jeff.safety.guard: ${guard}\n${spamFinding}`), EXAMPLES, entries);
    expect(load('{examples: [{example: nope, shows: "S."}]}')).toThrow(/jeff\.safety\.guard, example 1: no example nope/);
    expect(load('{examples: [{example: guard-1, shows: "S."}, {example: spam-1, shows: "S."}]}'))
      .toThrow(/jeff\.safety\.guard, example 2: example spam-1 calls jeff\.safety\.spam\.message_type/);
    expect(load('{examples: [{example: unnamed, shows: "S."}]}')).toThrow(/example unnamed has no name/);
    expect(load('{examples: [{example: guard-1, shows: "S."}, {example: guard-1, shows: "T."}]}')).toThrow(/lists guard-1 more than once/);
    expect(load('{examples: [{example: guard-1, shows: "S."}], finding: "F."}')).toThrow(/must have either examples, or finding only/);
  });

  it(`takes one to ${MAX_EXAMPLES} examples`, () => {
    const many = Array.from({ length: MAX_EXAMPLES + 1 }, (_, i) => `{example: g${i}, shows: "S."}`).join(', ');
    const examples = new Map(Array.from({ length: MAX_EXAMPLES + 1 }, (_, i) => [`g${i}`, example(`g${i}`, 'jeff.safety.guard.attempt')]));
    expect(() => loadCatalogueExamples(fileWith(`jeff.safety.guard: {examples: [${many}]}\n${spamFinding}`), examples, entries))
      .toThrow(new RegExp(`examples must be a list of 1 to ${MAX_EXAMPLES}`));
    expect(() => loadCatalogueExamples(fileWith(`jeff.safety.guard: {examples: []}\n${spamFinding}`), examples, entries))
      .toThrow(new RegExp(`examples must be a list of 1 to ${MAX_EXAMPLES}`));
  });

  it('takes an example of an adapter with a version part only by the name the pages use, its unversioned ID', () => {
    const routers = [entry('jeff.routing.router-open-v14', 'jeff.routing.router-open')];
    const examples = new Map([
      ['by-name', example('by-name', 'jeff.routing.router-open.specialist')],
      ['by-id', example('by-id', 'jeff.routing.router-open-v14.specialist')],
    ]);
    const out = loadCatalogueExamples(fileWith('jeff.routing.router-open-v14: {examples: [{example: by-name, shows: "S."}]}'), examples, routers);
    expect(out.get('jeff.routing.router-open-v14')).toEqual({ examples: [{ example: examples.get('by-name'), shows: 'S.' }] });
    expect(() => loadCatalogueExamples(fileWith('jeff.routing.router-open-v14: {examples: [{example: by-id, shows: "S."}]}'), examples, routers))
      .toThrow(/jeff\.routing\.router-open-v14, example 1: example by-id calls jeff\.routing\.router-open-v14\.specialist/);
  });
});

describe('an adapter page', () => {
  // The hub's owner check (GET /api/admin/owner) answers `owner.current`.
  const owner = { current: false };
  beforeEach(() => {
    vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com');
    owner.current = false;
    vi.stubGlobal('fetch', vi.fn(async (asked: string) => {
      if (asked !== '/api/admin/owner') throw new Error(`no stubbed answer for ${asked}`);
      return Response.json({ owner: owner.current });
    }));
  });
  afterEach(() => { vi.unstubAllEnvs(); vi.unstubAllGlobals(); forgetOwnerView(); window.localStorage.clear(); });

  it('shows the public no Open tag in its header, and the owner the tag', async () => {
    await renderPage('jeff.safety.spam');
    await vi.waitFor(() => expect(fetch).toHaveBeenCalled());
    expect(screen.queryByText('Open')).toBeNull();
    cleanup();
    forgetOwnerView();
    owner.current = true;
    await renderPage('jeff.safety.spam');
    expect(await screen.findByText('Open')).toBeTruthy();
  });

  it('has no page for a hidden adapter (every adapter the API serves that is not open), or for an unknown one', () => {
    const hidden = Object.keys(parse(fs.readFileSync(path.join(HUB_ROOT, 'content/owner-only/catalogue-text.yaml'), 'utf8')) as Record<string, unknown>);
    for (const id of [...hidden, 'jeff.operations.unknown']) {
      let thrown: unknown = null;
      try { loader({ params: { id } } as unknown as Parameters<typeof loader>[0]); } catch (error) { thrown = error; }
      expect(thrown, id).toBeInstanceOf(Response);
      expect((thrown as Response).status, id).toBe(404);
    }
  });

  async function renderPage(id: string) {
    const data = loader({ params: { id } } as unknown as Parameters<typeof loader>[0]);
    const Stub = createRoutesStub([{ path: '/catalogue/:id', Component: CatalogueAdapter, loader: () => data }]);
    render(<Stub initialEntries={[`/catalogue/${id}`]} />);
    await screen.findByRole('heading', { level: 1 });
    return data;
  }

  it('shows an open adapter its recorded example, its answer and what it shows', async () => {
    const data = await renderPage('jeff.safety.spam');
    expect(screen.getByRole('heading', { level: 2, name: 'Example' })).toBeTruthy();
    const figure = document.querySelector('[data-example="oss-spam"]');
    if (figure === null) throw new Error('no example on the page');
    expect(figure.textContent).toContain('value phishing');
    // The sentence under the answer sets its backticked answer as code, with no backtick left in the text.
    const sentence = screen.getByText((_, el) => el?.tagName === 'P' && el.textContent === (data.example?.shows as string).replaceAll('`', ''));
    expect([...sentence.querySelectorAll('code')].map((c) => c.textContent)).toEqual(['phishing']);
    expect(sentence.textContent).not.toContain('`');
  });

  it('lists the other four examples under the featured one, each with its name, answer and confidence, opening to its full case and code', async () => {
    const data = await renderPage('jeff.safety.spam');
    expect(data.more.map((m) => m.block.id)).toEqual(['more-spam-legitimate', 'more-spam-advert', 'more-spam-is-spam', 'more-spam-not-spam']);
    const lines = [...document.querySelectorAll('[data-example-line]')];
    expect(lines).toHaveLength(4);
    const first = lines[0] as HTMLDetailsElement;
    const summary = first.querySelector('summary') as HTMLElement;
    const answer = data.more[0].block.answer;
    if (answer === null) throw new Error('a listed example with no answer');
    expect(summary.textContent).toContain(data.more[0].name);
    expect(summary.textContent).toContain(answer.value);
    expect(summary.textContent).toContain(answer.confidence);
    // Closed at first; opening it shows the same full example, with its sentence and code.
    expect(first.open).toBe(false);
    fireEvent.click(summary);
    const figure = first.querySelector('[data-example="more-spam-legitimate"]') as HTMLElement;
    expect(figure.textContent).toContain(`value ${answer.value}`);
    expect(within(figure).getAllByRole('tab').map((t) => t.textContent)).toEqual([...shownLanguages().map((l) => SDK_LABELS[l]), 'curl']);
    expect(within(figure).getByRole('group', { name: 'Where the call runs' })).toBeTruthy();
  });

  it('shows the "Call it" code in every shown language and curl, and it follows the visitor\'s language choice', async () => {
    for (const id of ['jeff.safety.spam']) {
      await renderPage(id);
      const call = document.querySelector(`[data-call="${id}"]`);
      if (call === null) throw new Error(`no call on ${id}`);
      const inCall = within(call as HTMLElement);
      expect(inCall.getAllByRole('tab').map((t) => t.textContent)).toEqual([...shownLanguages().map((l) => SDK_LABELS[l]), 'curl']);
      expect(call.querySelector('pre')?.textContent).toContain('jeff.Jeff.remote(');
      fireEvent.click(inCall.getByRole('tab', { name: 'Node' }));
      expect(window.localStorage.getItem(CODE_LANGUAGE_STORAGE_KEY)).toBe('node');
      // The page's example follows the choice made on the call.
      const example = within(document.querySelector('[data-example]') as HTMLElement);
      expect(example.getByRole('tab', { name: 'Node', selected: true })).toBeTruthy();
      window.localStorage.clear();
      cleanup();
    }
  });
});
