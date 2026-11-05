// @vitest-environment node
// The hub's endpoints (api/index.ts, server/): JSON answers, errors that name what is wrong, method checks, and
// settings read by name with no defaults.
import { afterEach, describe, expect, it, vi } from 'vitest';
import api from '../api/index';
import { HUB_VERSION } from '../app/lib/hub-version.generated';
import { readSettings, MissingSettingsError } from '../server/settings';

afterEach(() => vi.unstubAllEnvs());

const call = (path: string, init?: RequestInit) => api.fetch(new Request(`https://jeffhub.ai${path}`, init));

describe('GET /api/health', () => {
  it('answers ok and the build version, the commit scripts/hub-version.mjs wrote, with no setting', async () => {
    const response = await call('/api/health');
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toMatch(/^application\/json/);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(await response.json()).toEqual({ status: 'ok', version: HUB_VERSION });
    expect(HUB_VERSION).toMatch(/^[0-9a-f]{7,40}$/);
  });

  it('answers another method with 405 and the methods it has', async () => {
    const response = await call('/api/health', { method: 'POST', body: '{}' });
    expect(response.status).toBe(405);
    expect(response.headers.get('allow')).toBe('GET');
    expect(await response.json()).toEqual({
      error: { type: 'MethodNotAllowed', message: '/api/health does not answer POST; it answers GET', details: { method: 'POST', allowed: ['GET'] } },
    });
  });
});

describe('an unknown /api path', () => {
  for (const path of ['/api/no/such/endpoint', '/api', '/api/healthz']) {
    it(`${path} is a 404 with a JSON error naming the path`, async () => {
      const response = await call(path);
      expect(response.status).toBe(404);
      expect(response.headers.get('content-type')).toMatch(/^application\/json/);
      expect(await response.json()).toEqual({
        error: { type: 'NotFound', message: `the hub has no endpoint at ${path}`, details: { path } },
      });
    });
  }
});

describe('readSettings', () => {
  it('returns every named setting', () => {
    expect(readSettings(['A', 'B'], { A: 'one', B: 'two' })).toEqual({ A: 'one', B: 'two' });
  });

  it('names every missing or empty setting at once, with no default', () => {
    let thrown: unknown;
    try {
      readSettings(['A', 'B', 'C'], { A: 'one', B: '  ' });
    } catch (error) {
      thrown = error;
    }
    expect(thrown).toBeInstanceOf(MissingSettingsError);
    expect((thrown as MissingSettingsError).missing).toEqual(['B', 'C']);
    expect((thrown as Error).message).toBe('the settings B, C are not set (or empty)');
  });
});
