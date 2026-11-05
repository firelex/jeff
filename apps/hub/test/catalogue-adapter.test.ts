import { describe, expect, it } from 'vitest';
import type { CatalogueEntry } from '../app/lib/catalogue.server';
import { assertOpenAdapterIsComplete } from '../app/routes/catalogue-adapter';

const entry = (over: Partial<CatalogueEntry>): CatalogueEntry => ({
  id: 'jeff.safety.guard', name: 'jeff.safety.guard', version: '1.4.0', kind: 'open', domain: 'safety', title: 'T', summary: 'S',
  useWhen: [], avoidWhen: [], base: 'jeff-base v1.4', functions: [],
  context: [], input: { key: 'text', codeName: 'text', description: 'd' },
  weights: { repo: 'mstrasser/jeff-adapter-guard', revision: 'v1.4' }, licence: 'Apache-2.0', localName: 'guard',
  ...over,
});

describe('assertOpenAdapterIsComplete', () => {
  it('passes a proprietary entry with no weights or licence', () => {
    expect(() => assertOpenAdapterIsComplete(entry({ kind: 'proprietary', weights: null, licence: null }))).not.toThrow();
  });
  it('passes an open entry that has both weights and a licence', () => {
    expect(() => assertOpenAdapterIsComplete(entry({}))).not.toThrow();
  });
  it('throws, naming the adapter, for an open entry with no weights', () => {
    expect(() => assertOpenAdapterIsComplete(entry({ weights: null }))).toThrow(/jeff\.safety\.guard/);
  });
  it('throws, naming the adapter, for an open entry with no licence', () => {
    expect(() => assertOpenAdapterIsComplete(entry({ licence: null }))).toThrow(/jeff\.safety\.guard/);
  });
});
