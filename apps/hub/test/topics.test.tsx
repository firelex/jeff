import { render, screen, within } from '@testing-library/react';
import { createRoutesStub } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { CatalogueEntry } from '../app/lib/catalogue.server';
import { catalogue } from '../app/lib/site.server';
import { TOPICS, groupByTopic } from '../app/lib/topics';
import { forgetOwnerView } from '../app/lib/owner-view';
import { PRIVATE_MARKERS } from '../app/lib/private-markers';
import Catalogue from '../app/routes/catalogue';

const entry = (id: string, kind: 'open' | 'proprietary', title: string) => ({
  id, name: id, version: '1.4.0', kind, domain: id.split('.')[1], title, summary: 's', useWhen: [], avoidWhen: [],
  base: 'jeff-base v1.4', functions: [{ key: 'f', id: `${id}.f`, interface: 'classify', instructions: null, labels: null, context: null, input: null }],
  context: [], input: { key: 'text', codeName: 'text', description: 'd' },
  weights: kind === 'open' ? { repo: 'mstrasser/jeff-adapter-x', revision: 'v1.4' } : null,
  localName: kind === 'open' ? id.slice(id.lastIndexOf('.') + 1) : null,
  licence: kind === 'open' ? 'Apache-2.0' : null,
}) as CatalogueEntry;

const ENTRIES = [
  entry('jeff.compliance.vendor-file', 'proprietary', 'Vendor file review'),
  entry('jeff.compliance.aml', 'open', 'Anti-money-laundering review'),
  entry('jeff.compliance.aml-broker', 'proprietary', 'Anti-money-laundering review for brokers'),
  entry('jeff.compliance.sanctions', 'open', 'Sanctions screening'),
  entry('jeff.safety.spam', 'open', 'Spam and phishing'),
  entry('jeff.safety.guard', 'open', 'Prompt injection guard'),
  entry('jeff.knowledge.knowledge-qa', 'proprietary', 'General knowledge questions'),
  entry('jeff.reasoning.entails', 'open', 'Evidence supports a claim'),
];

describe('groupByTopic', () => {
  it('orders the groups as TOPICS does, and inside a group puts open adapters first, then API-only, each by title', () => {
    const groups = groupByTopic(ENTRIES);
    expect(groups.map((g) => g.topic.id)).toEqual(['reasoning', 'safety', 'compliance']);
    expect(groups[0].entries.map((e) => e.id)).toEqual(['jeff.reasoning.entails', 'jeff.knowledge.knowledge-qa']);
    expect(groups[1].entries.map((e) => e.title)).toEqual(['Prompt injection guard', 'Spam and phishing']);
    expect(groups[2].entries.map((e) => e.id)).toEqual([
      'jeff.compliance.aml', 'jeff.compliance.sanctions', 'jeff.compliance.aml-broker', 'jeff.compliance.vendor-file',
    ]);
  });

  it('leaves out a topic with no entries', () => {
    const groups = groupByTopic(ENTRIES.filter((e) => e.domain !== 'safety'));
    expect(groups.map((g) => g.topic.id)).toEqual(['reasoning', 'compliance']);
    expect(groupByTopic([])).toEqual([]);
  });

  it('fails, naming the adapter and its domain, for a domain no topic covers', () => {
    expect(() => groupByTopic([entry('jeff.gardening.prune', 'open', 'Pruning')])).toThrow(/jeff\.gardening\.prune.*gardening/);
  });

  it('gives each domain to exactly one topic, with a heading and a one-line description', () => {
    const domains = TOPICS.flatMap((t) => t.domains);
    expect(new Set(domains).size).toBe(domains.length);
    for (const topic of TOPICS) {
      expect(topic.heading).not.toBe('');
      expect(topic.description).toMatch(/^[A-Z].*\.$/);
      for (const marker of PRIVATE_MARKERS) expect(`${topic.heading} ${topic.description}`).not.toContain(marker);
    }
  });

  it('covers every domain of the real catalogue, and every topic has at least one adapter in it', () => {
    const entries = catalogue();
    const groups = groupByTopic(entries);
    expect(groups.map((g) => g.topic.id)).toEqual(TOPICS.map((t) => t.id));
    expect(groups.flatMap((g) => g.entries).length).toBe(entries.length);
  });
});

// The catalogue page, with the hub's owner check (GET /api/admin/owner) answering `owner`.
function renderCatalogue(url: string, owner = false) {
  vi.stubGlobal('fetch', vi.fn(async (asked: string) => {
    if (asked !== '/api/admin/owner') throw new Error(`no stubbed answer for ${asked}`);
    return Response.json({ owner });
  }));
  const Stub = createRoutesStub([{ path: '/catalogue', Component: Catalogue, loader: () => ({ entries: ENTRIES }) }]);
  return render(<Stub initialEntries={[url]} />);
}

afterEach(() => { vi.unstubAllGlobals(); forgetOwnerView(); });

describe('the catalogue page', () => {
  it('shows each topic as a heading with its description, the adapters under it', async () => {
    renderCatalogue('/catalogue');
    const headings = await screen.findAllByRole('heading', { level: 2 });
    expect(headings.map((h) => h.textContent)).toEqual(['Reasoning and knowledge', 'Safety', 'Compliance']);
    const safety = headings[1].closest('section');
    if (safety === null) throw new Error('the Safety heading is not inside a section');
    const topic = TOPICS.find((t) => t.id === 'safety');
    if (topic === undefined) throw new Error('no safety topic');
    expect(within(safety).getByText(topic.description)).toBeTruthy();
    expect(within(safety).getAllByRole('heading', { level: 3 }).map((h) => h.textContent)).toEqual(['Prompt injection guard', 'Spam and phishing']);
  });

  it('hides the topics a filter leaves empty, and keeps the counts line right', async () => {
    renderCatalogue('/catalogue?interface=classify&q=review');
    // The filter applies just after the first render (which matches the prerendered, unfiltered page).
    expect(await screen.findByText('3 adapters')).toBeTruthy();
    const headings = screen.getAllByRole('heading', { level: 2 });
    expect(headings.map((h) => h.textContent)).toEqual(['Compliance']);
  });

  it('has no filter by kind, and ignores a ?kind= in the address', async () => {
    renderCatalogue('/catalogue?kind=proprietary');
    expect(await screen.findByText('8 adapters')).toBeTruthy();
    expect(screen.queryByRole('combobox', { name: 'Kind' })).toBeNull();
    expect(screen.getByRole('combobox', { name: 'Interface' })).toBeTruthy();
  });

  it('shows the public every card alike: ink border on the panel, no Open or API only tag', async () => {
    renderCatalogue('/catalogue');
    const card = async (title: string) => (await screen.findByRole('heading', { level: 3, name: title })).closest('a')!;
    // The owner check has answered (not the owner) once the page has settled.
    await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith('/api/admin/owner', expect.anything()));
    for (const title of ['Spam and phishing', 'Vendor file review']) {
      const shown = await card(title);
      expect(shown.className).toContain('border-ink');
      expect(shown.className).toContain('bg-panel');
      expect(shown.className).not.toMatch(/border-open|bg-open-fill/);
    }
    expect(screen.queryByText('Open')).toBeNull();
    expect(screen.queryByText('API only')).toBeNull();
  });

  it("shows the owner an open adapter's card green and an API-only one in ink, each with its tag in words", async () => {
    renderCatalogue('/catalogue', true);
    const card = async (title: string) => (await screen.findByRole('heading', { level: 3, name: title })).closest('a')!;
    const open = await card('Spam and phishing');
    await vi.waitFor(() => expect(open.className).toContain('border-open'));
    expect(open.className).toContain('bg-open-fill');
    expect(open.className).not.toContain('border-ink');
    expect(within(open).getByText('Open')).toBeTruthy();
    const apiOnly = await card('Vendor file review');
    expect(apiOnly.className).toContain('border-ink');
    expect(apiOnly.className).not.toContain('border-open');
    expect(apiOnly.className).not.toMatch(/red|danger|error/);
    expect(within(apiOnly).getByText('API only').className).toContain('border-primary');
  });

  it('hides every topic when a search matches nothing', async () => {
    renderCatalogue('/catalogue?q=nothing-matches-this');
    expect(await screen.findByText('0 adapters')).toBeTruthy();
    expect(screen.queryAllByRole('heading', { level: 2 })).toEqual([]);
  });
});
