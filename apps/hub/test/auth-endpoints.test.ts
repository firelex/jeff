// @vitest-environment node
// The sign-in endpoints as the hub's function serves them (api/index.ts): every /api/auth/* path goes to Better Auth
// (the router's prefix match), /api/signin-options tells the sign-in page what it needs, and both refuse to run
// without their settings, naming them.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { drizzle } from 'drizzle-orm/pglite';
import { migrate } from 'drizzle-orm/pglite/migrator';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../api/index';
import { TEST_ENV } from './helpers/settings';

// The function reads DATABASE_URL itself, so it gets a PGlite folder with the real migrations applied (Better Auth
// counts requests in the rate_limit table).
let database = '';
beforeAll(async () => {
  database = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-auth-db-'));
  const client = new PGlite(database);
  await migrate(drizzle(client), { migrationsFolder: path.join(import.meta.dirname, '../drizzle') });
  await client.close();
});

beforeEach(() => {
  for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value);
  vi.stubEnv('DATABASE_URL', `pglite:${database}`);
});
afterEach(() => vi.unstubAllEnvs());

const call = (path: string, init: RequestInit = {}) => api.fetch(new Request(`https://jeffhub.ai${path}`,
  { ...init, headers: { 'x-forwarded-for': '203.0.113.7', ...(init.headers as Record<string, string> | undefined) } }));

describe('/api/auth/*', () => {
  it('reaches Better Auth for any path under it', async () => {
    const session = await call('/api/auth/get-session');
    expect(session.status).toBe(200);
    expect(await session.json()).toBeNull();
    const form = await call('/api/auth/sign-in/magic-link', { method: 'POST', headers: { 'content-type': 'application/json',
      origin: 'https://jeffhub.ai' }, body: JSON.stringify({ email: 'ada@example.com', callbackURL: '/account' }) });
    expect(form.status).toBe(403);
    expect(await form.text()).toContain('No bot check was sent with the form');
  });

  it('every answer under it is no-store, Better Auth\'s own included', async () => {
    for (const response of [await call('/api/auth/get-session'), await call('/api/auth/list-sessions'),
      await call('/api/auth/magic-link/verify?token=x&callbackURL=%2Faccount&errorCallbackURL=%2Fsignin')]) {
      expect(response.headers.get('cache-control')).toBe('no-store');
    }
  });

  it('a path that only starts like it is not Better Auth\'s', async () => {
    const response = await call('/api/authx');
    expect(response.status).toBe(404);
    expect((await response.json()).error.message).toBe('the hub has no endpoint at /api/authx');
  });

  it('answers only GET and POST', async () => {
    const response = await call('/api/auth/sign-out', { method: 'DELETE' });
    expect(response.status).toBe(405);
    expect(response.headers.get('allow')).toBe('GET, POST');
  });

  it('refuses to run without its settings, and names them', async () => {
    vi.stubEnv('BETTER_AUTH_SECRET', '');
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    const response = await call('/api/auth/get-session');
    expect(response.status).toBe(500);
    expect((await response.json()).error.details).toEqual({ missing: ['BETTER_AUTH_SECRET'] });
    logged.mockRestore();
  });
});

describe('before Better Auth runs', () => {
  async function rateLimitRows(): Promise<number> {
    const { getDb } = await import('../app/lib/db.server');
    return (await (await getDb()).query.rateLimit.findMany()).length;
  }

  it('a path the hub does not use is a 404 that writes no rate-limit row', async () => {
    await call('/api/auth/get-session');
    const before = await rateLimitRows();
    for (const path of ['/api/auth/list-sessions', '/api/auth/x', '/api/auth/callback', '/api/auth/callback/a/b', '/api/auth/error',
      `/api/auth/${'z'.repeat(40)}`, '/api/auth/sign-in/email', '/api/auth/']) {
      for (const method of ['GET', 'POST']) {
        const response = await call(path, method === 'POST'
          ? { method, headers: { 'content-type': 'application/json', origin: 'https://jeffhub.ai' }, body: '{}' } : {});
        expect(response.status, `${method} ${path}`).toBe(404);
        expect((await response.json()).error.type).toBe('NotFound');
      }
    }
    expect(await rateLimitRows()).toBe(before);
  });

  it.each([[undefined], [''], ['203.0.113.7, 10.0.0.1'], ['not-an-address'], ['203.0.113.300']])(
    'X-Forwarded-For %j is a named 400', async (value) => {
      const headers: Record<string, string> = value === undefined ? {} : { 'x-forwarded-for': value };
      const request = new Request('https://jeffhub.ai/api/auth/get-session', { headers });
      if (value === undefined) request.headers.delete('x-forwarded-for');
      const response = await api.fetch(request);
      expect(response.status).toBe(400);
      expect((await response.json()).error).toMatchObject({ type: 'ClientAddressRefused', details: { header: 'x-forwarded-for' } });
    });
});

describe('GET /api/signin-options', () => {
  it('a visitor without a session is not signed in, and the test provider is off', async () => {
    const response = await call('/api/signin-options');
    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(await response.json()).toEqual({ signedIn: false, providers: ['github', 'google', 'email'], testProvider: false });
  });

  it('says when the test provider is on', async () => {
    vi.stubEnv('AUTH_TEST_PROVIDER', 'http://localhost:9400');
    expect(await (await call('/api/signin-options')).json()).toEqual({ signedIn: false, providers: ['github', 'google', 'email'], testProvider: true });
  });

  it('answers the methods SIGN_IN_PROVIDERS lists, in its order', async () => {
    vi.stubEnv('SIGN_IN_PROVIDERS', 'email,github');
    expect((await (await call('/api/signin-options')).json()).providers).toEqual(['email', 'github']);
  });
});
