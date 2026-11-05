// @vitest-environment node
// The hub's function with SIGN_IN_PROVIDERS=github (no Google, no email link), set before the function makes its one
// Better Auth (it is made once per process, so this is a file of its own): the unlisted methods' endpoints are refused,
// naming the method, and a listed method with a missing setting fails, naming the setting.
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
  vi.stubEnv('SIGN_IN_PROVIDERS', 'github');
  for (const name of ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'MAIL_TRANSPORT', 'MAIL_FROM', 'MAIL_TEST_DIR']) vi.stubEnv(name, '');
});
afterEach(() => vi.unstubAllEnvs());

const call = (path: string, init: RequestInit = {}) => api.fetch(new Request(`https://jeffhub.ai${path}`,
  { ...init, headers: { 'x-forwarded-for': '203.0.113.7', ...(init.headers as Record<string, string> | undefined) } }));

describe('a sign-in method SIGN_IN_PROVIDERS does not list', () => {
  const json = { 'content-type': 'application/json', origin: 'https://jeffhub.ai' };

  it.each([
    ['POST', '/api/auth/sign-in/magic-link', { email: 'ada@example.com', callbackURL: '/account' }, 'the hub does not offer sign-in with email: SIGN_IN_PROVIDERS is github'],
    ['GET', '/api/auth/magic-link/verify?token=x&callbackURL=%2Faccount', null, 'the hub does not offer sign-in with email: SIGN_IN_PROVIDERS is github'],
    ['GET', '/api/auth/callback/google?code=c&state=s', null, 'the hub does not offer sign-in with google: SIGN_IN_PROVIDERS is github'],
    ['GET', '/api/auth/callback/test?code=c&state=s', null, 'the hub does not offer sign-in with the test provider: AUTH_TEST_PROVIDER is off'],
  ])('%s %s is a 404 naming it', async (method, path, body, message) => {
    const response = await call(path, body === null ? {} : { method, headers: json, body: JSON.stringify(body) });
    expect(response.status).toBe(404);
    expect((await response.json()).error).toMatchObject({ type: 'SignInMethodNotOffered', message });
  });

  it('social sign-in with google is refused by name, before Better Auth counts it; with github it starts', async () => {
    for (let i = 0; i < 5; i += 1) {
      const google = await call('/api/auth/sign-in/social', { method: 'POST', headers: json,
        body: JSON.stringify({ provider: 'google', callbackURL: '/account' }) });
      expect(google.status).toBe(404);
      expect((await google.json()).error).toMatchObject({ type: 'SignInMethodNotOffered',
        message: 'the hub does not offer sign-in with google: SIGN_IN_PROVIDERS is github' });
    }
    const github = await call('/api/auth/sign-in/social', { method: 'POST', headers: json,
      body: JSON.stringify({ provider: 'github', callbackURL: '/account' }) });
    expect(github.status).toBe(200);
    expect(((await github.json()) as { url: string }).url).toMatch(/^https:\/\/github\.com\/login\/oauth\/authorize/);
  });

  it('social sign-in takes a JSON object, and says so', async () => {
    const form = await call('/api/auth/sign-in/social', { method: 'POST', headers: { ...json, 'content-type': 'application/x-www-form-urlencoded' },
      body: 'provider=github' });
    expect(form.status).toBe(415);
    const broken = await call('/api/auth/sign-in/social', { method: 'POST', headers: json, body: '{"provider":' });
    expect(broken.status).toBe(400);
    expect((await broken.json()).error.message).toMatch(/^\/api\/auth\/sign-in\/social takes a JSON body; this one does not parse: /);
  });

  it('a listed method with a missing setting fails, naming the setting', async () => {
    vi.stubEnv('SIGN_IN_PROVIDERS', 'github,email');
    await expect(call('/api/auth/get-session')).rejects.toThrow('the settings MAIL_TRANSPORT, MAIL_FROM are not set (or empty): SIGN_IN_PROVIDERS lists email, which needs them');
  });
});

