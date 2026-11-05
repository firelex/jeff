// @vitest-environment node
// POST /api/playground/decide (server/endpoints/playground.ts, app/lib/playground.server.ts): a visitor's bot check
// and per-minute limit, a subscriber's run on their own account, the call sent to the Jeff API and its code, and the
// API's errors refused in plain words. Most tests stand a fake fetch in for the API; the last one runs a local fake
// Jeff API over HTTP with the real fetch.
import http from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterEach, expect, it, vi } from 'vitest';
import { localAdapters } from '../app/lib/catalogue-call.server';
import { requestBody, localCallOf } from '../app/lib/examples.server';
import { createDb } from '../app/lib/db.server';
import { playgroundRun as runs } from '../app/lib/schema';
import { admitRun, isDatabaseUnreachable, PASS_COOKIE, PASS_SECONDS, visitorOf, type PlaygroundDeps } from '../app/lib/playground.server';
import { playgroundLanguages, playgroundSamples } from '../app/lib/playground-samples.server';
import { catalogue } from '../app/lib/site.server';
import { TurnstileRefused } from '../app/lib/turnstile.server';
import { serveEndpoint } from '../server/endpoint';
import { playgroundEndpointFor } from '../server/endpoints/playground';
import { testDb } from './helpers/db';
import { TEST_ENV, testSettings } from './helpers/settings';

const SAMPLE = playgroundSamples()[0];
// The playground offers open adapters' samples only (owner, 4 November 2026), each called through the SDK and so with
// a local call: SAMPLE, the first, and LOCAL_SAMPLE are both such samples.
const LOCAL_SAMPLE = playgroundSamples().find((sample) => sample.example.via !== 'http');
if (LOCAL_SAMPLE === undefined) throw new Error('playground.test.ts: no playground sample of an open adapter to test the local call with');
const DECISION = { value: 'forward', confidence: 0.99, probabilities: { forward: 0.99, fyi_only: 0.01 } };
const START = new Date('2026-11-03T10:00:00Z');
let now = START;

afterEach(() => { now = START; vi.unstubAllEnvs(); vi.restoreAllMocks(); });

type TestDeps = PlaygroundDeps & { fetchApi: ReturnType<typeof vi.fn> };

async function deps(changes: Partial<PlaygroundDeps> = {}): Promise<TestDeps> {
  const fetchApi = vi.fn(async () => Response.json({ decisions: [DECISION] }));
  return {
    db: await testDb(), settings: testSettings(), apiOrigin: 'https://api.jeffhub.ai', fetchApi: fetchApi as never,
    verifyTurnstile: async (token) => { if (token !== 'good') throw new TurnstileRefused(['invalid-input-response']); },
    now: () => now, clock: () => 0, subscriberOf: async () => null, ...changes,
  } as TestDeps;
}

const subscriber = (id: string) => ({ subscriberOf: async () => ({ id }) });

// The endpoint as the router serves it, against `d`.
function playground(d: PlaygroundDeps, request: Request): Promise<Response> {
  for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value);
  return serveEndpoint(playgroundEndpointFor({ settings: () => d.settings, deps: async () => d }), request);
}

function run(body: unknown, headers: Record<string, string> = {}, ip = '203.0.113.7'): Request {
  return new Request('https://jeffhub.ai/api/playground/decide', { method: 'POST',
    headers: { origin: 'https://jeffhub.ai', 'content-type': 'application/json', 'x-forwarded-for': ip, ...headers },
    body: JSON.stringify(body) });
}

const passOf = (response: Response) => {
  const cookie = response.headers.getSetCookie().find((c) => c.startsWith(`${PASS_COOKIE}=`));
  if (cookie === undefined) throw new Error('no pass cookie');
  return cookie.split(';')[0];
};

const good = { 'x-turnstile-token': 'good' };

it('a visitor passes the bot check once, then runs with the pass cookie, at that address only', async () => {
  const d = await deps();
  const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
  const refused = await playground(d, run({ sample: SAMPLE.id, input: SAMPLE.example.input }, { 'x-turnstile-token': 'bad' }));
  expect(refused.status).toBe(403);
  expect((await refused.json()).refusal).toEqual({ kind: 'bot-check', retryAfterS: null,
    message: 'Complete the bot check above the Run button, then run again.' });
  expect(logged).toHaveBeenCalledWith(expect.stringContaining('invalid-input-response'));
  const first = await playground(d, run({ sample: SAMPLE.id, input: SAMPLE.example.input }, good));
  expect(first.status).toBe(200);
  const cookie = passOf(first);
  expect(first.headers.getSetCookie()[0]).toMatch(/HttpOnly; Secure; SameSite=Strict; Path=\/api\/playground; Max-Age=3600$/);
  const second = await playground(d, run({ sample: SAMPLE.id, input: SAMPLE.example.input }, { cookie }));
  expect(second.status).toBe(200);
  expect(second.headers.getSetCookie()).toEqual([]);
  expect(await second.json()).toMatchObject({ ran_as: 'visitor', decision: DECISION });
  const elsewhere = await playground(d, run({ sample: SAMPLE.id, input: SAMPLE.example.input }, { cookie }, '198.51.100.1'));
  expect(elsewhere.status).toBe(403);
});

it('refuses a pass that has expired or was not signed by the hub', async () => {
  const d = await deps();
  const cookie = passOf(await playground(d, run({ sample: SAMPLE.id, input: 'x' }, good)));
  const [expires, signature] = cookie.slice(PASS_COOKIE.length + 1).split('.');
  const forged = `${PASS_COOKIE}=${Number(expires) + 3600}.${signature}`;
  expect((await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { cookie: forged }))).status).toBe(403);
  now = new Date(START.getTime() + PASS_SECONDS * 1000);
  expect((await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { cookie }))).status).toBe(403);
});

it('forwards one call with the playground key, the sample context and the edited input, and shows its code in every language', async () => {
  const d = await deps();
  const response = await playground(d, run({ sample: SAMPLE.id, input: 'An edited input' }, good));
  expect(d.fetchApi).toHaveBeenCalledTimes(1);
  const [url, init] = d.fetchApi.mock.calls[0] as [string, RequestInit];
  expect(url).toBe('https://api.test/v1/decide');
  const headers = init.headers as Record<string, string>;
  expect(headers.authorization).toBe(`Bearer ${testSettings().playground.apiKey}`);
  expect(headers['x-jeff-account']).toBeUndefined();
  const edited = { ...SAMPLE.example, input: 'An edited input' };
  expect(JSON.parse(init.body as string)).toEqual(requestBody(edited));
  expect(JSON.parse(init.body as string).calls[0]).toMatchObject({ function: SAMPLE.example.function, input: 'An edited input' });
  expect(response.headers.get('cache-control')).toBe('no-store');
  const body = await response.json();
  expect(body.code.map((c: { language: string }) => c.language)).toEqual(playgroundLanguages());
  for (const { code } of body.code) expect(code).toContain('An edited input');
  expect(body.code[0].code).toContain('https://api.jeffhub.ai');
  expect(body.curl).toContain('An edited input');
  expect(typeof body.latency_ms).toBe('number');
  expect(JSON.stringify(body)).not.toContain(testSettings().playground.apiKey);
});

it("also shows the same call \"on your machine\" for an open adapter's sample, for the edited input, exactly as every other widget's", async () => {
  const d = await deps();
  const response = await playground(d, run({ sample: LOCAL_SAMPLE.id, input: 'A local run' }, good));
  const body = await response.json();
  expect(body.local).not.toBeNull();
  expect(body.local.adapter).toBe(LOCAL_SAMPLE.adapter);
  expect(body.local.code.map((c: { language: string }) => c.language)).toEqual(playgroundLanguages());
  for (const { code } of body.local.code) expect(code).toContain('A local run');
  // The same generator every other widget's local call is built from (examples.server.ts: localCallOf).
  const expected = localCallOf({ ...LOCAL_SAMPLE.example, input: 'A local run' }, playgroundLanguages(), localAdapters(catalogue()));
  expect(body.local).toEqual(expected);
  // A local call needs no key and makes no HTTP request.
  for (const { code } of body.local.code) expect(code).not.toMatch(/JEFF_API_KEY|api\.jeffhub\.ai/);
});

it('offers only open adapters\' samples, and refuses a sample of an API-only adapter as an unknown sample, without calling the API', async () => {
  const open = new Set(catalogue().filter((e) => e.kind === 'open').map((e) => e.name));
  expect(playgroundSamples().filter((sample) => !open.has(sample.adapter)).map((sample) => sample.id)).toEqual([]);
  expect(playgroundSamples().some((sample) => sample.example.via === 'http')).toBe(false);
  const d = await deps();
  // An example id no open adapter's sample has, as an API-only adapter's example would be.
  const refused = await playground(d, run({ sample: 'api-hidden-sample', input: 'x' }, good));
  expect(refused.status).toBe(404);
  expect((await refused.json()).refusal.kind).toBe('sample');
  expect(d.fetchApi).not.toHaveBeenCalled();
});

it('holds a visitor to the runs a minute, saying when to try again', async () => {
  const d = await deps();
  const cookie = passOf(await playground(d, run({ sample: SAMPLE.id, input: 'x' }, good)));
  for (let i = 0; i < 4; i++) expect((await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { cookie }))).status).toBe(200);
  now = new Date(now.getTime() + 20_000);
  const refused = await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { cookie }));
  expect(refused.status).toBe(429);
  expect(refused.headers.get('retry-after')).toBe('40');
  expect((await refused.json()).refusal).toEqual({ kind: 'per-visitor', retryAfterS: 40,
    message: 'You can run 5 a minute, and this minute\'s runs are used up.' });
  expect(d.fetchApi).toHaveBeenCalledTimes(5);
  now = new Date(now.getTime() + 41_000);
  expect((await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { cookie }))).status).toBe(200);
});

it('admits no more than the limit when many runs from one address arrive at once', async () => {
  const db = await testDb();
  const results = await Promise.all(Array.from({ length: 12 }, () => admitRun(db, 'one-address', 5, now)));
  expect(results.filter((r) => r.admitted)).toHaveLength(5);
  expect((await admitRun(db, 'another-address', 5, now)).admitted).toBe(true);
});

it('refuses an input over 2,000 characters, an empty one, extra fields and an unknown sample, without calling the API', async () => {
  const d = await deps();
  const long = await playground(d, run({ sample: SAMPLE.id, input: 'x'.repeat(2001) }, good));
  expect(long.status).toBe(422);
  expect((await long.json()).refusal).toEqual({ kind: 'input', retryAfterS: null,
    message: 'The input is 2,001 characters; the playground takes at most 2,000.' });
  expect((await playground(d, run({ sample: SAMPLE.id, input: { text: 'x'.repeat(1990) } }, good))).status).toBe(422);
  expect((await playground(d, run({ sample: SAMPLE.id, input: '' }, good))).status).toBe(422);
  const context = await playground(d, run({ sample: SAMPLE.id, input: 'x', context: { big: 'y' } }, good));
  expect(context.status).toBe(422);
  expect((await context.json()).refusal.message).toContain('remove context');
  const unknown = await playground(d, run({ sample: 'no-such-sample', input: 'x' }, good));
  expect(unknown.status).toBe(404);
  expect((await unknown.json()).refusal.kind).toBe('sample');
  expect(d.fetchApi).not.toHaveBeenCalled();
});

it('checks the origin before anything else, then the media type and the address', async () => {
  const d = await deps();
  const crossSite = await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { ...good, origin: 'https://evil.example' }));
  expect(crossSite.status).toBe(403);
  expect((await crossSite.json()).error.type).toBe('CrossSiteRefused');
  const form = await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { ...good, 'content-type': 'text/plain' }));
  expect(form.status).toBe(415);
  const noAddress = await playground(d, run({ sample: SAMPLE.id, input: 'x' }, good, '203.0.113.7, 10.0.0.1'));
  expect(noAddress.status).toBe(400);
  expect((await noAddress.json()).error.type).toBe('ClientAddressRefused');
  expect(d.fetchApi).not.toHaveBeenCalled();
});

it('when the API holds the playground back, a visitor is told it is busy and shown no answer', async () => {
  const d = await deps({ fetchApi: vi.fn(async () => Response.json({ error: { type: 'RateLimited', message: 'over', details: { retry_after_s: 0.2 } } },
    { status: 429, headers: { 'retry-after': '1' } })) as never });
  const response = await playground(d, run({ sample: SAMPLE.id, input: 'x' }, good));
  expect(response.status).toBe(503);
  expect(response.headers.get('retry-after')).toBe('1');
  expect(await response.json()).toEqual({ refusal: { kind: 'busy', retryAfterS: 1, message: 'The playground is busy; try again in a few seconds.' } });
});

it('a subscriber runs on their own account, with no bot check or playground limit', async () => {
  const d = await deps(subscriber('u1'));
  for (let i = 0; i < 7; i++) {
    const response = await playground(d, run({ sample: SAMPLE.id, input: 'x' }));
    expect(response.status).toBe(200);
    expect((await response.json()).ran_as).toBe('account');
  }
  const [, init] = d.fetchApi.mock.calls[0] as [string, RequestInit];
  expect((init.headers as Record<string, string>)['x-jeff-account']).toBe('u1');
});

it("a subscriber past their allowance is told so in the API's words for the customer", async () => {
  const message = 'This account has used its 50,000,000 input tokens for the period ending 2026-12-03 10:00 UTC; calls resume then';
  const d = await deps({ ...subscriber('u1'), fetchApi: vi.fn(async () => Response.json(
    { error: { type: 'UsageLimitReached', message, details: {} } }, { status: 402 })) as never });
  const response = await playground(d, run({ sample: SAMPLE.id, input: 'x' }));
  expect(response.status).toBe(402);
  expect((await response.json()).refusal).toEqual({ kind: 'api', retryAfterS: null, message });
});

it("never passes on the API's own words when it refuses the playground's key", async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  const d = await deps({ fetchApi: vi.fn(async () => Response.json(
    { error: { type: 'Unauthorised', message: 'key jeff_live_cccc refused at https://internal.example/v1', details: {} } }, { status: 401 })) as never });
  const response = await playground(d, run({ sample: SAMPLE.id, input: 'x' }, good));
  expect(response.status).toBe(502);
  const text = await response.text();
  expect(JSON.parse(text).refusal).toMatchObject({ kind: 'api', retryAfterS: null });
  expect(text).not.toContain('jeff_live_');
  expect(text).not.toContain('internal.example');
});

it('says so when the API cannot be reached, naming no address', async () => {
  const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
  const d = await deps({ ...subscriber('u1'), fetchApi: vi.fn(async () => { throw new TypeError('fetch failed'); }) as never });
  const response = await playground(d, run({ sample: SAMPLE.id, input: 'x' }));
  expect(response.status).toBe(502);
  expect((await response.json()).refusal).toEqual({ kind: 'api', retryAfterS: null, message: 'The API could not be reached; try again in a minute.' });
  expect(logged).toHaveBeenCalledWith(expect.stringContaining('fetch failed'));
});

it('runs against a Jeff API over HTTP (a local fake) with the real fetch', async () => {
  const received: { headers: http.IncomingHttpHeaders; body: string }[] = [];
  const fake = http.createServer((req, res) => {
    let body = '';
    req.on('data', (chunk) => { body += chunk; });
    req.on('end', () => {
      received.push({ headers: req.headers, body });
      res.writeHead(req.url === '/v1/decide' && req.method === 'POST' ? 200 : 404, { 'content-type': 'application/json' });
      res.end(JSON.stringify({ decisions: [DECISION] }));
    });
  });
  await new Promise<void>((resolve) => fake.listen(0, '127.0.0.1', resolve));
  try {
    const port = (fake.address() as AddressInfo).port;
    const settings = testSettings();
    const d = await deps({ ...subscriber('u1'), fetchApi: fetch, clock: () => performance.now(),
      settings: { ...settings, playground: { ...settings.playground, apiUrl: `http://127.0.0.1:${port}` } } });
    const response = await playground(d, run({ sample: SAMPLE.id, input: 'Over the wire' }));
    expect(response.status).toBe(200);
    expect(await response.json()).toMatchObject({ ran_as: 'account', decision: DECISION });
    expect(received).toHaveLength(1);
    expect(received[0].headers.authorization).toBe(`Bearer ${settings.playground.apiKey}`);
    expect(received[0].headers['x-jeff-account']).toBe('u1');
    expect(JSON.parse(received[0].body).calls[0].input).toBe('Over the wire');
  } finally {
    await new Promise<void>((resolve) => fake.close(() => resolve()));
  }
});

it('counts an IPv6 visitor by its /64, for the pass and for the runs a minute', async () => {
  const d = await deps();
  const cookie = passOf(await playground(d, run({ sample: SAMPLE.id, input: 'x' }, good, '2001:db8:1:2::1')));
  for (let i = 0; i < 4; i++) {
    expect((await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { cookie }, `2001:db8:1:2:${i + 1}::9`))).status).toBe(200);
  }
  expect((await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { cookie }, '2001:0db8:0001:0002:ffff::1'))).status).toBe(429);
  expect((await playground(d, run({ sample: SAMPLE.id, input: 'x' }, { cookie }, '2001:db8:1:3::1'))).status).toBe(403);
});

it('reads an address as the visitor it counts as', () => {
  expect(visitorOf('203.0.113.7')).toBe('203.0.113.7');
  expect(visitorOf('2001:db8:1:2:3:4:5:6')).toBe('2001:0db8:0001:0002::/64');
  expect(visitorOf('2001:DB8:1:2::')).toBe('2001:0db8:0001:0002::/64');
  expect(visitorOf('::1')).toBe('0000:0000:0000:0000::/64');
  expect(visitorOf('fe80::1%eth0')).toBe('fe80:0000:0000:0000::/64');
  expect(visitorOf('::ffff:192.0.2.1')).toBe('192.0.2.1');
  expect(visitorOf('::ffff:c000:201')).toBe('192.0.2.1');
  expect(visitorOf('64:ff9b::192.0.2.1')).toBe('0064:ff9b:0000:0000::/64');
});

it('counts an input in characters, refuses one nested over 32 levels, and shows at most 100 characters of a sample id', async () => {
  const d = await deps();
  expect((await playground(d, run({ sample: SAMPLE.id, input: '\u{1F600}'.repeat(2000) }, good))).status).toBe(200);
  const nested = (levels: number): unknown => (levels === 0 ? 'x' : [nested(levels - 1)]);
  expect((await playground(d, run({ sample: SAMPLE.id, input: nested(32) }, good))).status).toBe(200);
  const deep = await playground(d, run({ sample: SAMPLE.id, input: nested(33) }, good));
  expect(deep.status).toBe(422);
  expect((await deep.json()).refusal).toEqual({ kind: 'input', retryAfterS: null,
    message: 'The input nests lists and maps more than 32 levels deep; the playground takes at most 32.' });
  const long = await playground(d, run({ sample: 'y'.repeat(5000), input: 'x' }, good));
  expect(long.status).toBe(404);
  expect((await long.json()).refusal.message).toBe(`There is no sample "${'y'.repeat(100)}…".`);
});

it("deletes only the address's own old runs", async () => {
  const db = await testDb();
  await db.insert(runs).values([{ ipHash: 'one', at: new Date(START.getTime() - 120_000) }, { ipHash: 'two', at: new Date(START.getTime() - 120_000) }]);
  await admitRun(db, 'one', 5, START);
  const left = await db.select().from(runs);
  expect(left.map((row) => row.ipHash).sort()).toEqual(['one', 'two']);
});

it('when the database cannot be reached, answers a 503 in plain words', async () => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  const unreachable = await createDb('postgres://hub:hub@127.0.0.1:1/hub');
  const d = await deps({ db: unreachable });
  const response = await playground(d, run({ sample: SAMPLE.id, input: 'x' }, good));
  expect(response.status).toBe(503);
  expect(response.headers.get('content-type')).toMatch(/^application\/json/);
  expect(response.headers.get('retry-after')).toBe('60');
  expect((await response.json()).refusal).toEqual({ kind: 'busy', retryAfterS: 60,
    message: 'The playground\'s database is busy or unreachable right now; try again shortly.' });
  expect(console.error).toHaveBeenCalledWith(expect.stringMatching(/^playground: the database was busy or unreachable while /));
  const session = await deps({ subscriberOf: async () => { throw new Error('session failed', { cause: Object.assign(new Error('connect ECONNREFUSED'), { code: 'ECONNREFUSED' }) }); } });
  expect((await playground(session, run({ sample: SAMPLE.id, input: 'x' }))).status).toBe(503);
  expect(isDatabaseUnreachable(new Error('syntax error at or near "selec"'))).toBe(false);
  expect(d.fetchApi).not.toHaveBeenCalled();
});
