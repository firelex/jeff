import path from 'node:path';
import { afterEach, expect, it, vi } from 'vitest';
import { loadCatalogue } from '../app/lib/catalogue.server';
import { loadDocs } from '../app/lib/docs.server';
import { PLAN, PLAN_ENV } from './helpers/plan';
import { catalogue, checkSite } from '../app/lib/site.server';
import { localAdapters } from '../app/lib/catalogue-call.server';

afterEach(() => vi.unstubAllEnvs());

const root = path.join(__dirname, '../content/docs');
const docs = () => loadDocs(root, 'https://api.example.com', ['python', 'node'], PLAN, localAdapters(catalogue()));

it('loads every guide', () => {
  expect(docs().pages.filter((p) => p.tab === 'guides').length).toBeGreaterThanOrEqual(27);
});

it('has no console-only syntax left', () => {
  for (const page of docs().pages) {
    expect(page.body, page.id).not.toMatch(/\]\(#docs\/|\]\(#api\)/);
  }
});

// The guides about the older question API (/v1/systemone: Questions, Choice, Score, Noul and the rest of their group)
// were removed (owner, 4 November 2026): the server still answers it, but the docs describe only /v1/decide. The
// comparison with hosted decision APIs names another service's own question API, never this path.
const REMOVED_OLDER_API_PAGES = ['questions', 'choice', 'score', 'noul', 'structure', 'images', 'confidence', 'option-order',
  'patterns', 'fan-out', 'confidence-routing', 'composite-scoring', 'intent-routing', 'models', 'limits', 'clients'];

it('has no page, group or mention of the older question API at /v1/systemone', () => {
  const pages = docs().pages;
  expect(pages.filter((page) => page.group === 'The older question API').map((page) => page.id)).toEqual([]);
  expect(pages.filter((page) => page.tab === 'guides' && REMOVED_OLDER_API_PAGES.includes(page.id)).map((page) => page.id)).toEqual([]);
  expect(pages.filter((page) => page.body.includes('/v1/systemone')).map((page) => page.id)).toEqual([]);
});

it('has no broken links anywhere on the site', () => {
  vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com');
  for (const [name, value] of Object.entries(PLAN_ENV)) vi.stubEnv(name, value);
  expect(() => checkSite()).not.toThrow();
});

it('loads the real catalogue', () => {
  const repo = path.join(__dirname, '../../..');
  const entries = loadCatalogue(path.join(repo, 'catalogue'), path.join(repo, 'results/v14-manifests'),
    path.join(__dirname, '../content/catalogue-text.yaml'), path.join(repo, 'contract/schemas/manifest.schema.json'),
    path.join(__dirname, '../content/owner-only/catalogue-text.yaml'), path.join(__dirname, '../content/owner-only/retired-adapters.yaml'));
  expect(entries.filter((e) => e.kind === 'open')).toHaveLength(34);
  expect(entries.some((e) => e.id.includes('code'))).toBe(false);
});

it('the quick start says where to get an API key and links the pricing page', () => {
  const quickstart = docs().pages.find((page) => page.id === 'quickstart');
  expect(quickstart?.body).toContain('[See pricing and sign up](/pricing)');
  expect(quickstart?.body).toContain('https://jeffhub.ai/account');
  // The price and the allowance are the plan's settings, put in when the docs are loaded.
  expect(quickstart?.body).toContain('$1 a month includes\n50 million input tokens a month');
  expect(quickstart?.body).not.toMatch(/\{\{(price|allowance)\}\}/);
});

// The guides sidebar, as the owner set it (4 November 2026): these groups first, then the groups after them; the
// worked examples between Why Jeff and Concepts.
it('orders the guides sidebar: Get started, Why Jeff, Worked examples, Concepts, then the rest', () => {
  const { sidebar } = docs();
  const titles = (group: string) => {
    const found = sidebar.guides.find((g) => g.group === group);
    if (found === undefined) throw new Error(`no sidebar group ${group}`);
    return found.pages.map((p) => p.title);
  };
  expect(sidebar.guides.map((g) => g.group)).toEqual(['Get started', 'Why Jeff', 'Worked examples', 'Concepts', 'Open-source adapters', 'The API', 'Roadmap']);
  expect(titles('Get started')).toEqual(['Introduction', 'Quick start', 'Jeff with coding agents']);
  expect(titles('Why Jeff')).toEqual(['What are System 1 models?', 'Jeff vs. hosted decision APIs', 'AI primer']);
  expect(titles('Worked examples')).toEqual(['A support agent, end to end', 'A shared inbox that sorts itself',
    'A retrieval pipeline that knows when not to answer', 'CI failures routed to the right person']);
  expect(titles('Concepts')).toEqual(['How Jeff works', 'Adapters', 'The swarm', 'State', 'How to build with Jeff']);
});

it('opens "Why Jeff" with "What are System 1 models?", which has a recorded example', () => {
  const { sidebar, pages } = docs();
  expect(sidebar.guides.find((g) => g.group === 'Why Jeff')?.pages[0].id).toBe('system-1');
  const page = pages.find((p) => p.id === 'system-1');
  expect(page?.title).toBe('What are System 1 models?');
  expect(page?.body).toContain('"id":"oss-spam"');
  expect(page?.body).not.toMatch(/\{\{(price|allowance)\}\}/);
});

it('the quick start sets up both ways to run, and its example offers both', () => {
  const body = docs().pages.find((page) => page.id === 'quickstart')?.body ?? '';
  for (const text of ['### On your machine (free)', '### Through the API', '| | On your machine (free) | Through the API |', 'pip install jeff-sdk "jeff-runtime[mac]"', 'jeff.Jeff.local(["guard"])',
    'Hugging Face', '/docs/sdk/connect#section-only-on-an-nvidia-gpu-the-prefix-pool', 'Every adapter in the catalogue']) {
    expect(body, text).toContain(text);
  }
  const block = /```\S+\n(\{"id":"guard-attempt".*\})\n```/.exec(body);
  expect(block, 'the guard-attempt example').not.toBeNull();
  expect(JSON.parse(block![1]).local).not.toBeNull();
});

it('has no page about the older API as a whole, and no link to one', () => {
  const { pages } = docs();
  expect(pages.some((page) => page.id === 'older-api')).toBe(false);
  for (const page of pages) expect(page.body, page.id).not.toContain('/docs/guides/older-api');
});

it('dates the comparison with hosted decision APIs, sits it right after the System 1 page, and keeps hardware plans out', () => {
  const { sidebar, pages } = docs();
  const why = sidebar.guides.find((g) => g.group === 'Why Jeff');
  if (why === undefined) throw new Error('no Why Jeff group');
  const ids = why.pages.map((p) => p.id);
  expect(ids.indexOf('hosted-decision-apis')).toBe(ids.indexOf('system-1') + 1);
  const page = pages.find((p) => p.id === 'hosted-decision-apis')!;
  expect(page.title).toBe('Jeff vs. hosted decision APIs');
  expect(page.body).toContain('# Jeff vs. hosted decision APIs');
  expect(page.body).toContain('17 October 2026');
  expect(page.body).toContain('not results of the current v1.4 adapters');
  expect(page.body).toContain('accessed through their public APIs for evaluation only');
  expect(page.body).not.toMatch(/\bchip\b|ASIC|4-bit|\bGLM/i);
  expect(pages.find((p) => p.id === 'system-1')!.body).toContain('(/docs/guides/hosted-decision-apis)');
});
