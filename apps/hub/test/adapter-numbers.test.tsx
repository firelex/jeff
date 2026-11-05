import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { cleanup, render, screen } from '@testing-library/react';
import { createRoutesStub } from 'react-router';
import { parse } from 'yaml';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { forgetOwnerView } from '../app/lib/owner-view';
import { loadAdapterNumbers } from '../app/lib/adapter-numbers.server';
import { loadCatalogues } from '../app/lib/catalogue.server';
import type { CatalogueEntry } from '../app/lib/catalogue.server';
import { HUB_ROOT, REPO_ROOT } from '../app/lib/paths.server';
import { accuracyText, shareText, thresholdText } from '../app/components/catalogue/Measured';
import { adapterNumbers, catalogue } from '../app/lib/site.server';
import CatalogueAdapter, { loader } from '../app/routes/catalogue-adapter';

const FILE = path.join(HUB_ROOT, 'content/adapter-numbers.yaml');
const HIDDEN_TEXT = path.join(HUB_ROOT, 'content/owner-only/catalogue-text.yaml');
const HIDDEN_FILE = path.join(HUB_ROOT, 'content/owner-only/adapter-numbers.yaml');
// The hidden adapters with no numbers (content/owner-only/without-numbers.yaml).
const WITHOUT_NUMBERS = parse(fs.readFileSync(path.join(HUB_ROOT, 'content/owner-only/without-numbers.yaml'), 'utf8')) as string[];

// The hidden adapters' entries (every adapter the API serves that is not open), as the build loads them.
function hiddenEntries(): CatalogueEntry[] {
  return loadCatalogues(
    path.join(REPO_ROOT, 'catalogue'), path.join(REPO_ROOT, 'results/v14-manifests'), path.join(HUB_ROOT, 'content/catalogue-text.yaml'),
    path.join(REPO_ROOT, 'contract/schemas/manifest.schema.json'), HIDDEN_TEXT, path.join(HUB_ROOT, 'content/owner-only/retired-adapters.yaml'),
  ).hidden;
}

// Every hidden adapter's numbers (content/owner-only/adapter-numbers.yaml), as scripts/function-content.mjs loads them:
// all but those of content/owner-only/without-numbers.yaml must have an entry, and an entry for any other adapter is an error.
function hiddenNumbers() {
  return loadAdapterNumbers(HIDDEN_FILE, hiddenEntries().filter((e) => !WITHOUT_NUMBERS.includes(e.id)));
}

describe('content/adapter-numbers.yaml', () => {
  const listed = Object.keys(parse(fs.readFileSync(FILE, 'utf8')) as Record<string, unknown>).sort();

  it('has an entry for every adapter the catalogue lists, and none for an adapter it does not', () => {
    const ids = catalogue().map((e) => e.id).sort();
    expect(ids.filter((id) => !listed.includes(id))).toEqual([]);
    expect(listed.filter((id) => !ids.includes(id))).toEqual([]);
  });

  it('has no entry for a hidden adapter', () => {
    const hidden = Object.keys(parse(fs.readFileSync(HIDDEN_TEXT, 'utf8')) as Record<string, unknown>);
    expect(listed.filter((id) => hidden.includes(id))).toEqual([]);
  });

  it("gives every function, of a listed or a hidden adapter, the calibration its manifest records", () => {
    const differ: string[] = [];
    for (const [id, numbers] of [...adapterNumbers(), ...hiddenNumbers()]) {
      const file = [path.join(REPO_ROOT, 'catalogue', id, 'manifest.yaml'), path.join(REPO_ROOT, 'results/v14-manifests', id, 'manifest.yaml')]
        .find((f) => fs.existsSync(f));
      if (file === undefined) throw new Error(`${id}: no manifest.yaml in catalogue/ or results/v14-manifests/`);
      const manifest = parse(fs.readFileSync(file, 'utf8')) as { functions: Record<string, { calibration?: Record<string, number> | null }> };
      for (const [key, fn] of Object.entries(manifest.functions)) {
        const shown = numbers.confident === null ? undefined : numbers.confident[key];
        const cal = fn.calibration ?? null;
        if (cal === null) {
          if (shown !== undefined) differ.push(`${id}.${key}: shown, but the manifest has no calibration`);
          continue;
        }
        if (shown === undefined) { differ.push(`${id}.${key}: the manifest has a calibration the page does not show`); continue; }
        const same = shown.precision === cal.precision_at_threshold && shown.coverage === cal.coverage_at_threshold
          && shown.threshold === cal.threshold && shown.rows === cal.rows;
        if (!same) differ.push(`${id}.${key}: ${JSON.stringify(shown)} against ${JSON.stringify(cal)}`);
      }
    }
    expect(differ).toEqual([]);
  });
});

describe('content/owner-only/adapter-numbers.yaml', () => {
  it('has an entry for every hidden adapter but those without-numbers.yaml names, and none for any other', () => {
    const hidden = hiddenEntries().map((e) => e.id);
    expect(WITHOUT_NUMBERS.filter((id) => !hidden.includes(id))).toEqual([]);
    expect([...hiddenNumbers().keys()].sort()).toEqual(hidden.filter((id) => !WITHOUT_NUMBERS.includes(id)).sort());
  });
});

describe('loadAdapterNumbers', () => {
  const entries = [
    { id: 'jeff.safety.guard', functions: [{ key: 'attempt' }] },
    { id: 'jeff.safety.spam', functions: [{ key: 'is_spam' }] },
  ] as unknown as CatalogueEntry[];
  const good = (id: string, key: string) => `${id}:
  base: Jeff v1.4
  date: '2026-10-25'
  tests:
  - {accuracy: 90.5, rows: 100, kind: public, set: a public set}
  confident:
    ${key}: {precision: 0.95, coverage: 0.9, threshold: 0.5, rows: 50}
`;
  function fileWith(text: string): string {
    const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'adapter-numbers-')), 'adapter-numbers.yaml');
    fs.writeFileSync(file, text);
    return file;
  }

  it('reads a complete file', () => {
    const numbers = loadAdapterNumbers(fileWith(good('jeff.safety.guard', 'attempt') + good('jeff.safety.spam', 'is_spam')), entries);
    expect(numbers.get('jeff.safety.guard')?.tests[0].accuracy).toBe(90.5);
  });
  it('refuses a listed adapter with no entry, naming it', () => {
    expect(() => loadAdapterNumbers(fileWith(good('jeff.safety.guard', 'attempt')), entries))
      .toThrow(/jeff\.safety\.spam: a listed adapter with no numbers/);
  });
  it('refuses an entry for an adapter the catalogue does not list, naming it', () => {
    const text = good('jeff.safety.guard', 'attempt') + good('jeff.safety.spam', 'is_spam') + good('jeff.operations.unlisted', 'route');
    expect(() => loadAdapterNumbers(fileWith(text), entries)).toThrow(/jeff\.operations\.unlisted: not an adapter the catalogue lists/);
  });
  it('refuses a calibration that names other functions than the adapter has', () => {
    expect(() => loadAdapterNumbers(fileWith(good('jeff.safety.guard', 'kind') + good('jeff.safety.spam', 'is_spam')), entries))
      .toThrow(/jeff\.safety\.guard: confident names functions kind, the adapter has attempt/);
  });
  it('refuses an in-house figure ahead of a public or real one', () => {
    const text = good('jeff.safety.guard', 'attempt').replace('  - {accuracy: 90.5', '  - {accuracy: 99.0, rows: 10, kind: generator, set: in-house}\n  - {accuracy: 90.5')
      + good('jeff.safety.spam', 'is_spam');
    expect(() => loadAdapterNumbers(fileWith(text), entries)).toThrow(/a public or real test set must come first/);
  });
  it('refuses an entry with neither tests nor a reason there are none, and one naming jevbench-hard', () => {
    const none = good('jeff.safety.guard', 'attempt').replace(/  tests:\n.*\n/, '') + good('jeff.safety.spam', 'is_spam');
    expect(() => loadAdapterNumbers(fileWith(none), entries)).toThrow(/needs exactly one of tests and no_accuracy/);
    const jev = good('jeff.safety.guard', 'attempt').replace('a public set', 'jevbench-hard') + good('jeff.safety.spam', 'is_spam');
    expect(() => loadAdapterNumbers(fileWith(jev), entries)).toThrow(/names jevbench-hard/);
  });
});

describe('number text', () => {
  it('keeps recorded decimals and never rounds a figure below 1 up to 100% or 1.00', () => {
    expect(accuracyText(95.95)).toBe('95.95%');
    expect(accuracyText(100)).toBe('100.0%');
    expect(shareText(0.985)).toBe('98.5%');
    expect(shareText(0.9995)).toBe('99.95%');
    expect(shareText(1)).toBe('100.0%');
    expect(thresholdText(0.413434)).toBe('0.41');
    expect(thresholdText(0.999836)).toBe('0.9998');
    expect(thresholdText(1)).toBe('1.00');
  });
});

describe('the adapter page shows how it was measured', () => {
  beforeEach(() => {
    vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com');
    // The page asks the hub's owner check (GET /api/admin/owner); a visitor is not the owner.
    vi.stubGlobal('fetch', vi.fn(async (asked: string) => {
      if (asked !== '/api/admin/owner') throw new Error(`no stubbed answer for ${asked}`);
      return Response.json({ owner: false });
    }));
  });
  afterEach(() => { cleanup(); vi.unstubAllEnvs(); vi.unstubAllGlobals(); forgetOwnerView(); });
  async function section(id: string): Promise<HTMLElement> {
    const data = loader({ params: { id } } as unknown as Parameters<typeof loader>[0]);
    const Stub = createRoutesStub([{ path: '/catalogue/:id', Component: CatalogueAdapter, loader: () => data }]);
    render(<Stub initialEntries={[`/catalogue/${id}`]} />);
    await screen.findByRole('heading', { level: 2, name: 'How it was measured' });
    const found = document.querySelector<HTMLElement>('[data-measured]');
    if (found === null) throw new Error('no measured section');
    return found;
  }

  it('leads tool-route with the public function-calling benchmark, then the in-house figure and its kind', async () => {
    const s = await section('jeff.agents.tool-route');
    const figures = [...s.querySelectorAll('[data-test-figure]')].map((li) => li.textContent ?? '');
    expect(figures[0]).toContain('71.6%');
    expect(figures[0]).toContain('3,646 rows');
    expect(figures[0]).toContain('public benchmark');
    expect(figures[1]).toContain('98.2%');
    expect(figures[1]).toContain('in-house: same generator as the training data');
    expect(s.textContent).toContain('98.5%');
    expect(s.textContent).toContain('0.41');
  });

  it('says so where a figure is missing', async () => {
    expect((await section('jeff.compliance.sanctions')).textContent).toContain('test-set kind not recorded');
    cleanup();
    expect((await section('jeff.security.soc')).textContent).toContain('test-set kind not recorded');
  });

  it('states the caveats of the data notes', async () => {
    expect((await section('jeff.engineering.ci-failure')).textContent).toContain('blind LLM check agreed');
    cleanup();
    expect((await section('jeff.knowledge.knowledge-qa')).textContent).toContain("base model's own pretraining data");
  });
});
