import { execFileSync } from 'node:child_process';
import Stripe from 'stripe';
import { playgroundSamples } from '../app/lib/playground-samples.server';
import { expect, test } from './fixtures';

// The hub's function next to the static site, as vercel.json sets it up (playwright.config.ts serves both): its
// health endpoint answers with this checkout's commit, and an /api path it does not have is a JSON 404, never the
// site's HTML 404 page.

test('/api/health answers ok and the build version', async ({ request }) => {
  const response = await request.get('/api/health');
  expect(response.status()).toBe(200);
  const commit = execFileSync('git', ['rev-parse', '--short', 'HEAD'], { encoding: 'utf8' }).trim();
  expect(await response.json()).toEqual({ status: 'ok', version: commit });
});

test('an unknown /api path is a JSON 404', async ({ request }) => {
  const response = await request.get('/api/no/such/endpoint');
  expect(response.status()).toBe(404);
  expect(response.headers()['content-type']).toMatch(/^application\/json/);
  expect((await response.json()).error).toEqual({
    type: 'NotFound', message: 'the hub has no endpoint at /api/no/such/endpoint', details: { path: '/api/no/such/endpoint' },
  });
});

// The Stripe webhook over real HTTP, through the local server's body handling: the signature holds over the bytes as
// sent (laid out with newlines and indents, which parsing and serialising again would change), and a body changed in
// one byte is refused. invoice.paid is an event the hub records and ignores, so Stripe itself is never called.
test('the Stripe webhook checks its signature over the raw body', async ({ request }) => {
  const body = JSON.stringify({ id: 'evt_e2e_raw', object: 'event', type: 'invoice.paid', created: 1_793_700_000,
    data: { object: { id: 'in_1', object: 'invoice' } } }, null, 2);
  const signature = Stripe.webhooks.generateTestHeaderString({ payload: body, secret: 'whsec_e2e' });
  const post = (data: string) => request.post('/api/stripe/webhook', {
    headers: { 'stripe-signature': signature, 'content-type': 'application/json' }, data });
  const changed = body.replace('in_1', 'in_2');
  expect(changed.length).toBe(body.length);
  const refused = await post(changed);
  expect(refused.status()).toBe(400);
  expect((await refused.json()).error.type).toBe('WebhookRefused');
  const accepted = await post(body);
  expect(accepted.status()).toBe(200);
  expect(accepted.headers()['cache-control']).toBe('no-store');
  expect(await accepted.json()).toEqual({ received: 'evt_e2e_raw', result: 'ignored' });
});

// The internal API jeff-server polls, over the local server's plain http on 127.0.0.1 (allowed outside production
// only): the shared secret is required, the key list comes back, and a usage report is answered with what was stored.
test('the internal API answers jeff-server with the shared secret only', async ({ request }) => {
  const secret = { authorization: 'Bearer e2e' };
  expect((await request.get('/api/internal/v1/keys')).status()).toBe(401);
  expect((await request.get('/api/internal/v1/keys', { headers: { authorization: 'Bearer wrong' } })).status()).toBe(401);
  const keys = await request.get('/api/internal/v1/keys', { headers: secret });
  expect(keys.status()).toBe(200);
  expect(keys.headers()['cache-control']).toBe('no-store');
  const list = await keys.json();
  expect(Object.keys(list).sort()).toEqual(['accounts', 'keys', 'playground']);
  expect(list.playground.runs_per_second).toBe(5);
  const report = await request.post('/api/internal/v1/usage', { headers: { ...secret, 'content-type': 'application/json' },
    data: JSON.stringify({ accounts: [{ account: 'playground', period_start: '2026-11-01T00:00:00Z', input_tokens: 3 },
      { account: 'no-such-account', period_start: '2026-11-01T00:00:00Z', input_tokens: 3 }], keys: [] }) });
  expect(report.status()).toBe(200);
  expect(await report.json()).toEqual({ recorded: 1, left_out: 1 });
});

// The playground's endpoint as the served function runs it, reading the example files from disk: a sample it does
// not have is refused as such, and a visitor without the bot check is asked for it, before any call to the API.
test('the playground endpoint reads its samples and asks a visitor for the bot check', async ({ request, baseURL }) => {
  const post = (sample: string) => request.post('/api/playground/decide', {
    headers: { origin: new URL(baseURL as string).origin, 'content-type': 'application/json' }, data: JSON.stringify({ sample, input: 'x' }) });
  const unknown = await post('no-such-sample');
  expect(unknown.status()).toBe(404);
  expect((await unknown.json()).refusal.kind).toBe('sample');
  const check = await post(playgroundSamples()[0].id);
  expect(check.status()).toBe(403);
  expect(check.headers()['cache-control']).toBe('no-store');
  expect((await check.json()).refusal.kind).toBe('bot-check');
});
