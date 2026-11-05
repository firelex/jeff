import { describe, expect, it } from 'vitest';
import { buildIndex, search } from '../app/lib/search';

const pages = [
  { tab: 'guides' as const, id: 'escalation', title: 'Escalation', group: 'The API', body: '',
    headings: [{ id: 'section-low-confidence', text: 'Low confidence', depth: 2 as const }], benchmarks: [] },
  { tab: 'guides' as const, id: 'confidence', title: 'Confidence', group: 'Older', body: '', headings: [], benchmarks: [] },
];

describe('search', () => {
  const index = buildIndex(pages);
  it('indexes pages and headings', () => {
    expect(index).toContainEqual({ title: 'Escalation', section: 'Low confidence',
      path: '/docs/guides/escalation#section-low-confidence', group: 'The API' });
  });
  it('ranks page titles above headings', () => {
    expect(search(index, 'confidence', 5)[0].path).toBe('/docs/guides/confidence');
  });
  it('needs every word', () => {
    expect(search(index, 'low escalation', 5).map((e) => e.section)).toEqual(['Low confidence']);
  });
  it('returns nothing for an empty query', () => {
    expect(search(index, '  ', 5)).toEqual([]);
  });
});
