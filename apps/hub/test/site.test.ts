import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import { matchPath } from 'react-router';
import routes from '../app/routes';
import { allPaths, catalogue, checkSite, publishedWeights } from '../app/lib/site.server';
import { PLAN_ENV } from './helpers/plan';

// site.server.ts memoises docs()/operations()/catalogue(), reading settings() (so the stubbed env) only the first
// time each is called. Stub before any test calls allPaths() or checkSite(), so every call in this file sees the
// same origin.
describe('allPaths and checkSite (real content)', () => {
  beforeAll(() => {
    vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com');
    for (const [name, value] of Object.entries(PLAN_ENV)) vi.stubEnv(name, value);
  });
  afterAll(() => vi.unstubAllEnvs());

  it('has no duplicate paths', () => {
    const paths = allPaths();
    expect(new Set(paths).size).toBe(paths.length);
  });

  it('includes the decide operation and the guard adapter', () => {
    const paths = allPaths();
    expect(paths).toContain('/docs/api/post-v1-decide');
    expect(paths).toContain('/catalogue/jeff.safety.guard');
  });

  it('does not throw on the real content', () => {
    expect(() => checkSite()).not.toThrow();
  });

  // A route whose paths allPaths() leaves out would build without complaint and fall to the SPA fallback.
  it('prerenders at least one path of every route', () => {
    const patterns = routes.map((r) => `/${r.path ?? ''}`);
    const unmatched = patterns.filter((pattern) => !allPaths().some((p) => matchPath(pattern, p) !== null));
    expect(unmatched).toEqual([]);
  });

  // Since the v1.4 release (2026-11-05) every open adapter's weights are public, so no page says "published at launch".
  it('lists the published weights of every open adapter and of nothing else', () => {
    const published = publishedWeights();
    const open = catalogue().flatMap((e) => (e.weights === null ? [] : [`${e.weights.repo}@${e.weights.revision}`]));
    expect(open.length).toBe(34);
    expect([...published].sort()).toEqual([...open].sort());
    expect(published.has('mstrasser/jeff-adapter-guard@v1.4')).toBe(true);
  });
});
