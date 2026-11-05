// @vitest-environment node
// The internal API jeff-server polls (app/lib/internal-api.server.ts, server/endpoints/internal.ts): the shared
// secret over https, the key list, and usage totals that are safe to repeat.
import fs from 'node:fs';
import path from 'node:path';
import { Ajv2020 } from 'ajv/dist/2020.js';
import { eq } from 'drizzle-orm';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../api/index';
import { internalSettings } from '../app/lib/accounts-settings.server';
import { deleteAccount } from '../app/lib/billing.server';
import type { Db } from '../app/lib/db.server';
import { checkInternal, keyList, recordUsage } from '../app/lib/internal-api.server';
import { ACCOUNTS_KEYS_SCHEMA, ACCOUNTS_USAGE_SCHEMA } from '../app/lib/internal-schemas';
import { createKey, hashKey, liveKeys, revokeKey } from '../app/lib/keys.server';
import { apiKey, playgroundRun, subscription, usage } from '../app/lib/schema';
import { serveEndpoint } from '../server/endpoint';
import { internalEndpoints } from '../server/endpoints/internal';
import { ApiError } from '../server/http';
import { aSubscriber, aUser, NOW } from './helpers/accounts';
import { testDb } from './helpers/db';
import { TEST_ENV, testSettings } from './helpers/settings';
import { fakeStripe } from './helpers/stripe';

const CONTRACT = path.join(import.meta.dirname, '../../../contract/schemas');
const contractSchema = (name: string) => JSON.parse(fs.readFileSync(path.join(CONTRACT, `${name}.schema.json`), 'utf8'));
const validKeys = new Ajv2020({ strict: false }).compile(contractSchema('accounts_keys'));
const settings = testSettings();
const TOKEN = settings.internalToken;

function thrownBy(run: () => void): ApiError {
  try { run(); } catch (error) { return error as ApiError; }
  throw new Error('nothing was thrown');
}

async function refusalOf(work: Promise<unknown>): Promise<ApiError> {
  return work.then(() => { throw new Error('nothing was thrown'); }, (error: unknown) => error as ApiError);
}

it('the copies of the contract schemas the function carries are the contract files', () => {
  expect(ACCOUNTS_KEYS_SCHEMA).toEqual(contractSchema('accounts_keys'));
  expect(ACCOUNTS_USAGE_SCHEMA).toEqual(contractSchema('accounts_usage'));
});

describe('checkInternal', () => {
  const HTTPS = { 'x-forwarded-proto': 'https' };
  const ok = { ...HTTPS, authorization: `Bearer ${TOKEN}` };
  const check = (url: string, headers: Record<string, string>, production = false) =>
    () => checkInternal(new Request(url, { headers }), TOKEN, production);

  // On Vercel request.url is built from the Host header, http:// for a host without a port (review C1): only
  // X-Forwarded-Proto, which Vercel sets itself, says the client came over https.
  it('lets a request Vercel says came over https through with the shared secret, whatever request.url says', () => {
    for (const production of [false, true]) {
      expect(check('http://jeffhub.ai/api/internal/v1/keys', ok, production)).not.toThrow();
      expect(check('https://jeffhub.ai/api/internal/v1/keys', ok, production)).not.toThrow();
    }
  });

  it('refuses with 403 a request not over https, whatever request.url says', () => {
    const auth = { authorization: `Bearer ${TOKEN}` };
    for (const production of [false, true]) {
      for (const headers of [auth, { ...auth, 'x-forwarded-proto': 'http' }, { ...auth, 'x-forwarded-proto': 'HTTPS ' }]) {
        for (const url of ['http://jeffhub.ai/api/internal/v1/keys', 'https://jeffhub.ai/api/internal/v1/keys']) {
          const refusal = thrownBy(check(url, headers, production));
          expect([refusal.status, refusal.type]).toEqual([403, 'HttpsRequired']);
        }
      }
    }
  });

  it('lets plain http to a loopback host through outside production only, as the local server sends it', () => {
    const local = { authorization: `Bearer ${TOKEN}`, 'x-forwarded-proto': 'http' };
    for (const host of ['127.0.0.1:5195', '[::1]:5195', 'localhost:5195']) {
      expect(check(`http://${host}/api/internal/v1/keys`, local)).not.toThrow();
      expect(thrownBy(check(`http://${host}/api/internal/v1/keys`, local, true)).status).toBe(403);
    }
  });

  it('refuses anything but the shared secret with one and the same 401', () => {
    const refusals = ([{}, { authorization: 'Bearer wrong' }, { authorization: TOKEN }, { authorization: `bearer ${TOKEN}` },
      { authorization: `Bearer ${TOKEN}x` }, { authorization: `Basic ${TOKEN}` }] as Record<string, string>[])
      .map((headers) => thrownBy(check('https://jeffhub.ai/api/internal/v1/keys', { ...HTTPS, ...headers })));
    for (const refusal of refusals) {
      expect([refusal.status, refusal.type, refusal.message, refusal.details])
        .toEqual([401, 'Unauthorised', 'the internal API needs its shared secret as a bearer token', {}]);
      expect(refusal.message).not.toContain(TOKEN);
    }
  });
});

it('lists the live keys of paying accounts only, with each account allowance and period, and the playground', async () => {
  const db = await testDb();
  await aSubscriber(db, 'paying');
  await aSubscriber(db, 'late', 'past_due');
  await aSubscriber(db, 'gone', 'active');
  const kept = await createKey(db, 'paying', 'a', NOW);
  const revoked = await createKey(db, 'paying', 'b', NOW);
  await revokeKey(db, 'paying', revoked.view.id, NOW);
  await createKey(db, 'late', 'c', NOW);
  await createKey(db, 'gone', 'd', NOW);
  await db.update(subscription).set({ status: 'canceled' }).where(eq(subscription.userId, 'gone'));
  await aUser(db, 'never-paid');
  const body = await keyList(db, settings, NOW);
  expect(validKeys(body), JSON.stringify(validKeys.errors)).toBe(true);
  expect(body.accounts.map((a) => a.account).sort()).toEqual(['late', 'paying']);
  expect(body.accounts).toContainEqual({ account: 'paying', allowance: 50_000_000, period_start: '2026-11-03T10:00:00Z',
    period_end: '2026-12-03T10:00:00Z' });
  expect(body.keys).toContainEqual({ hash: hashKey(kept.key), account: 'paying' });
  expect(body.keys).not.toContainEqual({ hash: hashKey(revoked.key), account: 'paying' });
  expect(body.keys.map((k) => k.account).sort()).toEqual(['late', 'paying']);
  expect(body.playground).toEqual({ hash: hashKey(settings.playground.apiKey), runs_per_second: 5,
    period_start: '2026-11-01T00:00:00Z', period_end: '2026-12-01T00:00:00Z' });
});

it('with no paying account the list has only the playground', async () => {
  const db = await testDb();
  await aUser(db, 'never-paid');
  const body = await keyList(db, settings, NOW);
  expect(validKeys(body)).toBe(true);
  expect([body.accounts, body.keys]).toEqual([[], []]);
});

it('refuses to answer a key list that breaks its schema, naming the place', async () => {
  const db = await testDb();
  await aSubscriber(db, 'has a space');
  await expect(keyList(db, settings, NOW)).rejects.toThrow(/does not match accounts_keys at \/accounts\/0\/account: must match pattern/);
});

const report = (key: string, tokens: number, lastUsed: string) => ({
  accounts: [{ account: 'u1', period_start: '2026-11-03T10:00:00Z', input_tokens: tokens },
    { account: 'playground', period_start: '2026-11-01T00:00:00Z', input_tokens: 9 }],
  keys: [{ hash: hashKey(key), last_used: lastUsed }, { hash: 'f'.repeat(64), last_used: lastUsed }],
});

it('records usage totals; a repeated or an older report changes nothing', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  const { key } = await createKey(db, 'u1', 'k', NOW);
  expect(await recordUsage(db, report(key, 100, '2026-11-03T11:00:00Z'), NOW)).toEqual({ recorded: 2, left_out: 0 });
  await recordUsage(db, report(key, 100, '2026-11-03T11:00:00Z'), NOW);
  await recordUsage(db, report(key, 60, '2026-11-03T10:30:00Z'), NOW);
  const rows = await db.select().from(usage);
  expect(rows.map((r) => [r.accountId, r.inputTokens]).sort()).toEqual([['playground', 9], ['u1', 100]]);
  const [row] = await db.select().from(apiKey);
  expect(row.lastUsedAt?.toISOString()).toBe('2026-11-03T11:00:00.000Z');
  expect((await liveKeys(db, 'u1'))[0].lastUsedAt?.toISOString()).toBe('2026-11-03T11:00:00.000Z');
  await recordUsage(db, report(key, 150, '2026-11-03T12:00:00Z'), NOW);
  expect((await db.select().from(usage).where(eq(usage.accountId, 'u1')))[0].inputTokens).toBe(150);
  expect((await liveKeys(db, 'u1'))[0].lastUsedAt?.toISOString()).toBe('2026-11-03T12:00:00.000Z');
});

it('keeps the greater of two totals for the same account and period in one report, and the later of two uses', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  const { key } = await createKey(db, 'u1', 'k', NOW);
  const hash = hashKey(key);
  const answer = await recordUsage(db, {
    accounts: [{ account: 'u1', period_start: '2026-11-03T10:00:00Z', input_tokens: 70 },
      { account: 'u1', period_start: '2026-11-03T10:00:00Z', input_tokens: 30 },
      { account: 'u1', period_start: '2026-10-03T10:00:00Z', input_tokens: 5 }],
    keys: [{ hash, last_used: '2026-11-03T12:00:00Z' }, { hash, last_used: '2026-11-03T11:00:00Z' }],
  }, NOW);
  expect(answer).toEqual({ recorded: 2, left_out: 0 });
  const rows = await db.select().from(usage);
  expect(rows.map((r) => [r.periodStart.toISOString(), r.inputTokens]).sort())
    .toEqual([['2026-10-03T10:00:00.000Z', 5], ['2026-11-03T10:00:00.000Z', 70]]);
  expect((await liveKeys(db, 'u1'))[0].lastUsedAt?.toISOString()).toBe('2026-11-03T12:00:00.000Z');
});

it('stores no totals for a deleted account, and says how many it left out', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  await aSubscriber(db, 'u2');
  const { key } = await createKey(db, 'u1', 'k', NOW);
  await recordUsage(db, report(key, 100, '2026-11-03T11:00:00Z'), NOW);
  await deleteAccount({ db, stripe: fakeStripe({}).api, settings }, 'u1', { list: async () => [], remove: async (zips) => { expect(zips).toEqual([]); } });
  expect(await db.select().from(usage).where(eq(usage.accountId, 'u1'))).toEqual([]);
  const answer = await recordUsage(db, {
    accounts: [...report(key, 120, '2026-11-03T12:00:00Z').accounts,
      { account: 'u2', period_start: '2026-11-03T10:00:00Z', input_tokens: 4 },
      { account: 'never-was', period_start: '2026-11-03T10:00:00Z', input_tokens: 4 }],
    keys: report(key, 120, '2026-11-03T12:00:00Z').keys,
  }, NOW);
  expect(answer).toEqual({ recorded: 2, left_out: 2 });
  const rows = await db.select().from(usage);
  expect(rows.map((r) => [r.accountId, r.inputTokens]).sort()).toEqual([['playground', 9], ['u2', 4]]);
});

describe('refuses a report that is not a usage document, naming the problem', () => {
  const cases: [string, unknown, string][] = [
    ['a period that is not a time', { accounts: [{ account: 'u1', period_start: 'today', input_tokens: 1 }], keys: [] }, '/accounts/0/period_start'],
    ['a time that does not exist', { accounts: [{ account: 'u1', period_start: '2026-13-45T10:00:00Z', input_tokens: 1 }], keys: [] },
      '/accounts/0/period_start is not a real UTC time'],
    ['a last use that does not exist', { accounts: [], keys: [{ hash: 'a'.repeat(64), last_used: '2026-11-03T25:00:00Z' }] },
      '/keys/0/last_used is not a real UTC time'],
    ['a negative total', { accounts: [{ account: 'u1', period_start: '2026-11-03T10:00:00Z', input_tokens: -1 }], keys: [] },
      '/accounts/0/input_tokens'],
    ['a total past 2**53', { accounts: [{ account: 'u1', period_start: '2026-11-03T10:00:00Z', input_tokens: 2 ** 60 }], keys: [] },
      '/accounts/0/input_tokens is larger than the hub stores exactly'],
    ['a missing list', { accounts: [] }, 'the body must have required property \'keys\''],
    ['not an object', [1], 'the body must be object'],
  ];
  it.each(cases)('%s', async (_name, body, expected) => {
    const db = await testDb();
    const refusal = await refusalOf(recordUsage(db, body, NOW));
    expect([refusal.status, refusal.type]).toEqual([422, 'InvalidRequest']);
    expect(refusal.message).toContain(expected);
    expect(await db.select().from(usage)).toEqual([]);
  });
});

describe('the endpoints', () => {
  let db: Db;
  let production = false;
  const endpoints = () => internalEndpoints({ settings: () => settings, db: async () => db, production: () => production, now: () => NOW });
  const ok = { authorization: `Bearer ${TOKEN}`, 'x-forwarded-proto': 'https' };

  beforeEach(async () => {
    for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value);
    db = await testDb();
    production = false;
  });
  afterEach(() => vi.unstubAllEnvs());

  it('GET keys answers the key list, no-store', async () => {
    await aSubscriber(db, 'u1');
    const response = await serveEndpoint(endpoints().keys, new Request('https://jeffhub.ai/api/internal/v1/keys', { headers: ok }));
    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    const body = await response.json();
    expect(validKeys(body)).toBe(true);
    expect(body.accounts.map((a: { account: string }) => a.account)).toEqual(['u1']);
  });

  it('GET keys works with the mail settings removed and a sign-in method listed without its settings, from the environment (final review I7)', async () => {
    await aSubscriber(db, 'u1');
    vi.stubEnv('MAIL_TRANSPORT', '');
    vi.stubEnv('MAIL_FROM', '');
    vi.stubEnv('GOOGLE_CLIENT_ID', '');
    vi.stubEnv('BETTER_AUTH_SECRET', '');
    vi.stubEnv('JEFF_HUB_INTERNAL_TOKEN', TOKEN);
    const fromEnv = internalEndpoints({ settings: internalSettings, db: async () => db, production: () => false, now: () => NOW });
    const response = await serveEndpoint(fromEnv.keys, new Request('https://jeffhub.ai/api/internal/v1/keys', { headers: ok }));
    expect(response.status).toBe(200);
    expect((await response.json()).accounts.map((a: { account: string }) => a.account)).toEqual(['u1']);
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.stubEnv('JEFF_HUB_INTERNAL_TOKEN', '');
    const missing = await serveEndpoint(fromEnv.keys, new Request('https://jeffhub.ai/api/internal/v1/keys', { headers: ok }));
    // The 404 of an unknown path, the setting named only in the log: the answer comes before the bearer token is
    // checked (re-review R8).
    expect(missing.status).toBe(404);
    expect((await missing.json()).error).toMatchObject({ type: 'NotFound', details: { path: '/api/internal/v1/keys' } });
    expect(String(vi.mocked(console.error).mock.calls[0][0])).toContain('JEFF_HUB_INTERNAL_TOKEN');
  });

  it('POST usage stores the totals and answers what it stored and left out', async () => {
    await aSubscriber(db, 'u1');
    const response = await serveEndpoint(endpoints().usage, new Request('https://jeffhub.ai/api/internal/v1/usage', {
      method: 'POST', headers: { ...ok, 'content-type': 'application/json' },
      body: JSON.stringify({ accounts: [{ account: 'u1', period_start: '2026-11-03T10:00:00Z', input_tokens: 7 },
        { account: 'gone', period_start: '2026-11-03T10:00:00Z', input_tokens: 3 }], keys: [] }) }));
    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(await response.json()).toEqual({ recorded: 1, left_out: 1 });
    expect((await db.select().from(usage)).map((r) => [r.accountId, r.inputTokens])).toEqual([['u1', 7]]);
  });

  it('POST usage sweeps every visitor\'s playground runs older than the counting window, and keeps the recent ones', async () => {
    const old = new Date(NOW.getTime() - 60_001);
    const recent = new Date(NOW.getTime() - 30_000);
    await db.insert(playgroundRun).values([{ ipHash: 'gone-a', at: old }, { ipHash: 'gone-b', at: new Date(NOW.getTime() - 86_400_000) },
      { ipHash: 'back', at: old }, { ipHash: 'back', at: recent }]);
    const response = await serveEndpoint(endpoints().usage, new Request('https://jeffhub.ai/api/internal/v1/usage', {
      method: 'POST', headers: { ...ok, 'content-type': 'application/json' }, body: JSON.stringify({ accounts: [], keys: [] }) }));
    expect(response.status).toBe(200);
    expect((await db.select().from(playgroundRun)).map((r) => [r.ipHash, r.at.getTime()])).toEqual([['back', recent.getTime()]]);
  });

  it('POST usage refuses a body that is not JSON, or not sent as JSON', async () => {
    const send = (contentType: string, body: string) => serveEndpoint(endpoints().usage, new Request('https://jeffhub.ai/api/internal/v1/usage', {
      method: 'POST', headers: { ...ok, 'content-type': contentType }, body }));
    const broken = await send('application/json', `{"keys": [{"hash": "${'a'.repeat(64)}"`);
    expect(broken.status).toBe(422);
    expect((await broken.json()).error.message).toBe('the usage report is not valid JSON');
    const plain = await send('text/plain', '{}');
    expect(plain.status).toBe(415);
  });

  it('checks the secret before reading the body or the database', async () => {
    const touched = vi.fn(async () => db);
    const guarded = internalEndpoints({ settings: () => settings, db: touched, production: () => false, now: () => NOW });
    const response = await serveEndpoint(guarded.usage, new Request('https://jeffhub.ai/api/internal/v1/usage', {
      method: 'POST', headers: { authorization: 'Bearer wrong', 'x-forwarded-proto': 'https', 'content-type': 'application/json' }, body: 'not json' }));
    expect(response.status).toBe(401);
    expect(response.headers.get('cache-control')).toBe('no-store');
    const keys = await serveEndpoint(guarded.keys, new Request('http://jeffhub.ai/api/internal/v1/keys', { headers: { authorization: ok.authorization } }));
    expect(keys.status).toBe(403);
    expect(touched).not.toHaveBeenCalled();
  });

  it('refuses loopback http in production', async () => {
    production = true;
    const response = await serveEndpoint(endpoints().keys, new Request('http://127.0.0.1:5195/api/internal/v1/keys', { headers: { authorization: ok.authorization, 'x-forwarded-proto': 'http' } }));
    expect(response.status).toBe(403);
    expect((await response.json()).error).toEqual({ type: 'HttpsRequired', message: 'the internal API is served over https only', details: {} });
  });

  it('are registered in the function at the paths jeff-server asks for', async () => {
    const server = fs.readFileSync(path.join(import.meta.dirname, '../../server/src/jeff_server/accounts.py'), 'utf8');
    expect(server).toContain('KEYS_PATH = "/api/internal/v1/keys"');
    expect(server).toContain('USAGE_PATH = "/api/internal/v1/usage"');
    for (const [pathname, method] of [['/api/internal/v1/keys', 'GET'], ['/api/internal/v1/usage', 'POST']]) {
      const response = await api.fetch(new Request(`https://jeffhub.ai${pathname}`, { method, headers: { authorization: 'Bearer wrong', 'x-forwarded-proto': 'https' } }));
      expect(response.status).toBe(401);
      expect(response.headers.get('cache-control')).toBe('no-store');
    }
  });
});
