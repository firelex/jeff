// @vitest-environment node
// The billing endpoints (server/endpoints/account.ts): the status /account/return polls, Checkout, the Customer
// Portal and deleting an account. First with a fake Stripe and a signed-in user given directly; then through the
// hub's function (api/index.ts), for what every visitor meets before any of that: the sign-in and cross-site checks.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { drizzle } from 'drizzle-orm/pglite';
import { migrate } from 'drizzle-orm/pglite/migrator';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../api/index';
import type { Db } from '../app/lib/db.server';
import { KEY_REVOKED } from '../app/lib/account-page';
import { liveKeys } from '../app/lib/keys.server';
import { apiKey, usage, user } from '../app/lib/schema';
import type { SessionUser } from '../app/lib/session.server';
import { serveEndpoint } from '../server/endpoint';
import { accountEndpoints, isJson } from '../server/endpoints/account';
import { ApiError } from '../server/http';
import { aSubscriber, aUser, NOW, PERIOD } from './helpers/accounts';
import { testDb } from './helpers/db';
import { TEST_ENV, testSettings } from './helpers/settings';
import { aStripeSubscription, fakeStripe } from './helpers/stripe';

const BASE = 'https://jeffhub.ai';
const ADA: SessionUser = { id: 'u1', email: 'u1@example.com', emailVerified: true, name: 'Ada', image: null };

beforeEach(() => { for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value); });
afterEach(() => vi.unstubAllEnvs());

function endpointsFor(db: Db, stripe = fakeStripe({}), signedIn: SessionUser | null = ADA) {
  const revokeSessions = vi.fn(async (_headers: Headers) => {});
  const billing = vi.fn(async () => ({ db, stripe: stripe.api, settings: testSettings() }));
  return {
    stripe,
    revokeSessions,
    billing,
    endpoints: accountEndpoints({
      revokeSessions,
      dataSets: { list: async () => [], remove: async (pathnames) => { if (pathnames.length > 0) throw new Error(`unexpected data sets ${pathnames.join(', ')}`); } },
      now: () => NOW,
      billing,
      store: async () => ({ db, settings: testSettings() }),
      signedIn: async () => {
        if (signedIn === null) throw new ApiError(401, 'SignInRequired', 'sign in first');
        return signedIn;
      },
      expiredSessionCookies: async () => ['session=; Max-Age=0', 'cache=; Max-Age=0'],
    }),
  };
}

const post = (pathname: string, headers: Record<string, string> = { origin: BASE }, body?: string) =>
  new Request(`${BASE}${pathname}`, { method: 'POST', headers, body });
const confirm = (value: unknown) => post('/api/account/delete', { origin: BASE, 'content-type': 'application/json' },
  JSON.stringify({ confirm: value }));

describe('GET /api/account/status', () => {
  it('before any payment: not usable, no status', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const response = await serveEndpoint(endpointsFor(db).endpoints.status, new Request(`${BASE}/api/account/status`));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ account: 'u1', usable: false, status: null });
  });

  it.each([['active', true], ['past_due', true], ['incomplete', false], ['canceled', false]])(
    'a %s subscription is usable: %s', async (status, usable) => {
      const db = await testDb();
      await aSubscriber(db, 'u1', status);
      const response = await serveEndpoint(endpointsFor(db).endpoints.status, new Request(`${BASE}/api/account/status`));
      expect(await response.json()).toEqual({ account: 'u1', usable, status });
    });
});

describe('POST /api/account/checkout and /api/account/portal', () => {
  it('answer the Stripe page to go to', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const { endpoints, stripe } = endpointsFor(db);
    const checkout = await serveEndpoint(endpoints.checkout, post('/api/account/checkout'));
    expect(checkout.status).toBe(200);
    expect(await checkout.json()).toEqual({ url: 'https://checkout.stripe.test/u1' });
    expect(stripe.calls.checkout.mock.calls[0][0]).toMatchObject({ client_reference_id: 'u1', subscription_data: { metadata: { user_id: 'u1' } } });
  });

  it('a second Checkout while subscribed, and the portal before any payment, are 409s with words for the customer', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1', 'active');
    await aUser(db, 'u2');
    const subscribed = await serveEndpoint(endpointsFor(db, fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'active', 'u1') })).endpoints.checkout,
      post('/api/account/checkout'));
    expect(subscribed.status).toBe(409);
    expect((await subscribed.json()).error).toEqual({ type: 'BillingRefused',
      message: 'Your subscription is already active; manage it with "Manage billing"', details: {} });
    const { endpoints } = endpointsFor(db, fakeStripe({}), { ...ADA, id: 'u2', email: 'u2@example.com' });
    const portal = await serveEndpoint(endpoints.portal, post('/api/account/portal'));
    expect(portal.status).toBe(409);
    expect((await portal.json()).error.message).toBe('There is no billing account yet: subscribe first');
  });

  it('the portal opens for a subscriber', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1');
    const response = await serveEndpoint(endpointsFor(db).endpoints.portal, post('/api/account/portal'));
    expect(await response.json()).toEqual({ url: 'https://billing.stripe.test/cus_u1' });
  });

  it.each([
    [{ origin: 'https://evil.example' }],
    [{ origin: 'null' }],
    [{ origin: BASE, 'sec-fetch-site': 'cross-site' }],
    [{}],
  ])('are refused from another site, before Stripe is asked: %j', async (headers) => {
    const db = await testDb();
    await aUser(db, 'u1');
    const { endpoints, stripe } = endpointsFor(db);
    for (const [endpoint, pathname] of [[endpoints.checkout, '/api/account/checkout'], [endpoints.portal, '/api/account/portal']] as const) {
      const response = await serveEndpoint(endpoint, post(pathname, headers));
      expect(response.status).toBe(403);
      expect((await response.json()).error.type).toBe('CrossSiteRefused');
    }
    expect(stripe.calls.checkout).not.toHaveBeenCalled();
    expect(stripe.calls.portal).not.toHaveBeenCalled();
  });

  it('answer only POST', async () => {
    const db = await testDb();
    const response = await serveEndpoint(endpointsFor(db).endpoints.checkout, new Request(`${BASE}/api/account/checkout`));
    expect(response.status).toBe(405);
    expect(response.headers.get('allow')).toBe('POST');
  });
});

describe('POST /api/account/delete', () => {
  it('with the confirmation, cancels in Stripe and deletes the account', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1', 'active');
    const { endpoints, stripe } = endpointsFor(db, fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'active', 'u1') }));
    const response = await serveEndpoint(endpoints.delete, confirm('delete'));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ deleted: 'u1' });
    expect(response.headers.getSetCookie()).toEqual(['session=; Max-Age=0', 'cache=; Max-Age=0']);
    expect(stripe.calls.cancel).toHaveBeenCalledWith('sub_u1', {}, { idempotencyKey: 'hub-cancel-sub_u1' });
    expect(await db.select().from(user)).toEqual([]);
  });

  it('without BLOB_READ_WRITE_TOKEN answers 500 naming it, and deletes nothing (final review M6)', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.stubEnv('BLOB_READ_WRITE_TOKEN', '');
    const db = await testDb();
    await aSubscriber(db, 'u1', 'active');
    const { endpoints, stripe } = endpointsFor(db, fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'active', 'u1') }));
    const response = await serveEndpoint(endpoints.delete, confirm('delete'));
    expect(response.status).toBe(500);
    expect((await response.json()).error).toMatchObject({ type: 'MissingSetting', details: { missing: ['BLOB_READ_WRITE_TOKEN'] } });
    expect(stripe.calls.cancel).not.toHaveBeenCalled();
    expect(await db.select().from(user)).toHaveLength(1);
  });

  it.each([['Delete'], [''], [null], [true]])('without the confirmation (%j) deletes nothing', async (value) => {
    const db = await testDb();
    await aSubscriber(db, 'u1', 'active');
    const { endpoints, stripe } = endpointsFor(db);
    const response = await serveEndpoint(endpoints.delete, confirm(value));
    expect(response.status).toBe(400);
    expect((await response.json()).error).toMatchObject({ type: 'ConfirmationRequired', message: 'Type delete in the box to confirm deleting the account.' });
    expect(stripe.calls.cancel).not.toHaveBeenCalled();
    expect(await db.select().from(user)).toHaveLength(1);
  });

  it('a body that is not JSON is a named 400 or 415', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const { endpoints } = endpointsFor(db);
    const notJson = await serveEndpoint(endpoints.delete, post('/api/account/delete', { origin: BASE, 'content-type': 'application/json' }, '{'));
    expect(notJson.status).toBe(400);
    expect((await notJson.json()).error.type).toBe('InvalidRequest');
    for (const type of ['application/x-www-form-urlencoded', 'application/jsonx', 'application/json-patch+json', 'text/plain']) {
      const other = await serveEndpoint(endpoints.delete, post('/api/account/delete', { origin: BASE, 'content-type': type }, '{"confirm":"delete"}'));
      expect(other.status, type).toBe(415);
    }
    expect(await db.select().from(user)).toHaveLength(1);
  });

  it('takes application/json with parameters, in any case', async () => {
    expect(isJson('application/json')).toBe(true);
    expect(isJson('Application/JSON; charset=utf-8')).toBe(true);
    expect(isJson('application/jsonx')).toBe(false);
    expect(isJson(null)).toBe(false);
  });

  it('from another site deletes nothing', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const { endpoints } = endpointsFor(db);
    const response = await serveEndpoint(endpoints.delete, post('/api/account/delete',
      { origin: 'https://evil.example', 'content-type': 'application/json' }, JSON.stringify({ confirm: 'delete' })));
    expect(response.status).toBe(403);
    expect(await db.select().from(user)).toHaveLength(1);
  });
});

const postJson = (pathname: string, body: unknown, headers: Record<string, string> = { origin: BASE }) =>
  post(pathname, { 'content-type': 'application/json', ...headers }, JSON.stringify(body));

describe('GET /api/account', () => {
  it('answers what the account page shows, no-store', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1');
    await db.insert(usage).values({ accountId: 'u1', periodStart: PERIOD.start, inputTokens: 1234, updatedAt: NOW });
    const response = await serveEndpoint(endpointsFor(db).endpoints.page, new Request(`${BASE}/api/account`));
    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(await response.json()).toEqual({ view: 'account', email: 'u1@example.com', status: 'active',
      periodEnd: '2026-12-03T10:00:00.000Z', cancelAtPeriodEnd: false, usage: { used: 1234, allowance: 50_000_000 },
      keys: [], playground: null });
  });

  it('before any payment: the subscribe screen', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const response = await serveEndpoint(endpointsFor(db).endpoints.page, new Request(`${BASE}/api/account`));
    expect(await response.json()).toEqual({ view: 'subscribe', email: 'u1@example.com', price: '$1', allowance: '50 million', status: null, playground: null });
  });
});

describe('POST /api/account/create-key and /api/account/revoke-key', () => {
  it('create-key answers the key once and stores only its hash; revoke-key revokes it', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1');
    const { endpoints } = endpointsFor(db);
    const made = await serveEndpoint(endpoints.createKey, postJson('/api/account/create-key', { name: ' ci ' }));
    expect(made.status).toBe(200);
    expect(made.headers.get('cache-control')).toBe('no-store');
    const { created } = await made.json() as { created: { key: string; name: string } };
    expect(created).toEqual({ name: 'ci', key: expect.stringMatching(/^jeff_live_[a-z2-7]{52}$/) });
    const rows = await db.select().from(apiKey);
    expect(rows).toHaveLength(1);
    expect(JSON.stringify(rows)).not.toContain(created.key);
    expect(rows[0].createdAt).toEqual(NOW);
    const revoked = await serveEndpoint(endpoints.revokeKey, postJson('/api/account/revoke-key', { key: rows[0].id }));
    expect(revoked.status).toBe(200);
    expect(await revoked.json()).toEqual({ done: KEY_REVOKED });
    expect(KEY_REVOKED).toBe('The key is revoked. It stops working within about a minute.');
    expect(await liveKeys(db, 'u1')).toEqual([]);
  });

  it('a refusal the customer can act on is a 409 KeyRefused in their words', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const { endpoints } = endpointsFor(db);
    const unpaid = await serveEndpoint(endpoints.createKey, postJson('/api/account/create-key', { name: 'x' }));
    expect(unpaid.status).toBe(409);
    expect((await unpaid.json()).error).toEqual({ type: 'KeyRefused',
      message: 'Keys can be created only while your subscription is active; subscribe first', details: {} });
    const unknown = await serveEndpoint(endpoints.revokeKey, postJson('/api/account/revoke-key', { key: 'no-such-key' }));
    expect(unknown.status).toBe(409);
    expect((await unknown.json()).error.message).toBe('No live key with that id on your account');
  });

  it('the eleventh key is refused', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1');
    const { endpoints } = endpointsFor(db);
    for (let n = 0; n < 10; n += 1) {
      expect((await serveEndpoint(endpoints.createKey, postJson('/api/account/create-key', { name: `k${n}` }))).status).toBe(200);
    }
    const eleventh = await serveEndpoint(endpoints.createKey, postJson('/api/account/create-key', { name: 'k10' }));
    expect(eleventh.status).toBe(409);
    expect((await eleventh.json()).error.message).toBe('You have 10 live keys, the most an account may hold; revoke one first');
  });

  it('a missing field is a 400 that says what to send; a body that is not JSON a 415', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1');
    const { endpoints } = endpointsFor(db);
    const noName = await serveEndpoint(endpoints.createKey, postJson('/api/account/create-key', { name: 5 }));
    expect(noName.status).toBe(400);
    expect((await noName.json()).error).toEqual({ type: 'InvalidRequest', message: 'Give the key a name.', details: { field: 'name' } });
    const noKey = await serveEndpoint(endpoints.revokeKey, postJson('/api/account/revoke-key', {}));
    expect((await noKey.json()).error).toEqual({ type: 'InvalidRequest', message: 'Choose the key to revoke.', details: { field: 'key' } });
    const form = await serveEndpoint(endpoints.createKey, post('/api/account/create-key',
      { origin: BASE, 'content-type': 'application/x-www-form-urlencoded' }, 'name=ci'));
    expect(form.status).toBe(415);
    expect(await db.select().from(apiKey)).toEqual([]);
  });

  it.each([[{ origin: 'https://evil.example' }], [{ origin: BASE, 'sec-fetch-site': 'cross-site' }], [{}]])(
    'from another site make and revoke nothing: %j', async (headers) => {
      const db = await testDb();
      await aSubscriber(db, 'u1');
      const { endpoints } = endpointsFor(db);
      const made = await serveEndpoint(endpoints.createKey, postJson('/api/account/create-key', { name: 'ci' }, headers));
      expect(made.status).toBe(403);
      expect(await db.select().from(apiKey)).toEqual([]);
      const revoked = await serveEndpoint(endpoints.revokeKey, postJson('/api/account/revoke-key', { key: 'k' }, headers));
      expect(revoked.status).toBe(403);
    });
});

it('the account page, status, key and sign-out endpoints never build the Stripe client', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  const { endpoints, billing } = endpointsFor(db);
  await serveEndpoint(endpoints.page, new Request(`${BASE}/api/account`));
  await serveEndpoint(endpoints.status, new Request(`${BASE}/api/account/status`));
  const made = await serveEndpoint(endpoints.createKey, postJson('/api/account/create-key', { name: 'ci' }));
  const { created } = await made.json() as { created: { key: string } };
  expect(created.key).toMatch(/^jeff_live_/);
  const [row] = await liveKeys(db, 'u1');
  await serveEndpoint(endpoints.revokeKey, postJson('/api/account/revoke-key', { key: row.id }));
  await serveEndpoint(endpoints.signOutEverywhere, post('/api/account/sign-out-everywhere'));
  expect(billing).not.toHaveBeenCalled();
  await serveEndpoint(endpoints.portal, post('/api/account/portal'));
  expect(billing).toHaveBeenCalledTimes(1);
});

describe('POST /api/account/sign-out-everywhere', () => {
  it('revokes every session with the request\'s headers and expires this browser\'s cookies', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const { endpoints, revokeSessions } = endpointsFor(db);
    const request = post('/api/account/sign-out-everywhere');
    const response = await serveEndpoint(endpoints.signOutEverywhere, request);
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ signedOut: 'u1' });
    expect(revokeSessions).toHaveBeenCalledWith(request.headers);
    expect(response.headers.getSetCookie()).toEqual(['session=; Max-Age=0', 'cache=; Max-Age=0']);
  });

  it('from another site signs nothing out', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const { endpoints, revokeSessions } = endpointsFor(db);
    const response = await serveEndpoint(endpoints.signOutEverywhere, post('/api/account/sign-out-everywhere', { origin: 'https://evil.example' }));
    expect(response.status).toBe(403);
    expect(revokeSessions).not.toHaveBeenCalled();
  });
});

describe('through the hub\'s function', () => {
  // The function reads DATABASE_URL itself, so it gets a PGlite folder with the real migrations applied.
  let database = '';
  beforeAll(async () => {
    database = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-account-db-'));
    const client = new PGlite(database);
    await migrate(drizzle(client), { migrationsFolder: path.join(import.meta.dirname, '../drizzle') });
    await client.close();
  });
  beforeEach(() => vi.stubEnv('DATABASE_URL', `pglite:${database}`));

  const call = (pathname: string, init: RequestInit = {}) => api.fetch(new Request(`${BASE}${pathname}`, init));

  it('every account endpoint needs a signed-in visitor, and says so (no-store)', async () => {
    const json = { method: 'POST', headers: { origin: BASE, 'content-type': 'application/json' } };
    for (const [pathname, init] of [['/api/account', {}], ['/api/account/status', {}], ['/api/account/checkout', { method: 'POST', headers: { origin: BASE } }],
      ['/api/account/create-key', { ...json, body: '{"name":"ci"}' }], ['/api/account/revoke-key', { ...json, body: '{"key":"k"}' }],
      ['/api/account/sign-out-everywhere', { method: 'POST', headers: { origin: BASE } }],
      ['/api/account/portal', { method: 'POST', headers: { origin: BASE } }],
      ['/api/account/delete', { method: 'POST', headers: { origin: BASE, 'content-type': 'application/json' }, body: '{"confirm":"delete"}' }]] as const) {
      const response = await call(pathname, init);
      expect(response.status, pathname).toBe(401);
      expect(response.headers.get('cache-control')).toBe('no-store');
      expect((await response.json()).error).toMatchObject({ type: 'SignInRequired', details: { signIn: '/signin' } });
    }
  });

  it('a POST from another site is refused before the session is read', async () => {
    const response = await call('/api/account/checkout', { method: 'POST', headers: { origin: 'https://evil.example' } });
    expect(response.status).toBe(403);
    expect((await response.json()).error).toMatchObject({ type: 'CrossSiteRefused', details: { origin: 'https://evil.example', expected: BASE } });
  });

  it('a deletion expires Better Auth\'s own session cookies, by their names and attributes', async () => {
    const { expiredSessionCookiesOfTheHub } = await import('../app/lib/session.server');
    expect(await expiredSessionCookiesOfTheHub()).toEqual([
      '__Secure-better-auth.session_token=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax',
      '__Secure-better-auth.session_data=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax',
      '__Secure-better-auth.dont_remember=; Max-Age=0; Path=/; HttpOnly; Secure; SameSite=Lax',
    ]);
  });

  it('refuses to run without its settings, and names them', async () => {
    vi.stubEnv('STRIPE_PRICE_ID', '');
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    const response = await call('/api/account/status');
    expect(response.status).toBe(500);
    expect((await response.json()).error.details).toEqual({ missing: ['STRIPE_PRICE_ID'] });
    logged.mockRestore();
  });
});
