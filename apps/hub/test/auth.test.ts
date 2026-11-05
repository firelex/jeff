// @vitest-environment node
// Better Auth over the real migrated schema (PGlite): the email link (bot check first, one use, 15 minutes), the
// GitHub and Google redirects, the session cookie's attributes, sign-out and sign-out everywhere, the CSRF and
// same-origin redirect checks, and account linking (only for an email the provider says is verified). No provider is
// called: GitHub and Google are checked up to the redirect they build, and the test OpenID provider's three addresses
// (discovery, token, user info) are answered by a stubbed fetch.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { AccountsSettings } from '../app/lib/accounts-settings.server';
import { CALLBACK_PATHS } from '../app/lib/auth-shared';
import { createAuth, MAGIC_LINK_SECONDS, SESSION_SECONDS } from '../app/lib/auth.server';
import type { Mail } from '../app/lib/mail.server';
import { revokeEverySession } from '../app/lib/session.server';
import { TurnstileRefused } from '../app/lib/turnstile.server';
import { testDb } from './helpers/db';
import { testSettings } from './helpers/settings';

const BASE = 'https://jeffhub.ai';

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

// Better Auth's rate limit (in memory) is shared by every instance in the process and counts per address and path,
// so each test is a visitor with its own address.
let visitors = 0;
let ip = '';

// A new visitor address (10.0.0.1, 10.0.0.2, ...): a valid IPv4 address for any count, so Better Auth never reads it
// as no address at all.
function nextVisitor(): void {
  visitors += 1;
  ip = `10.${(visitors >> 16) & 255}.${(visitors >> 8) & 255}.${visitors & 255}`;
}

async function setUp(changes: Partial<AccountsSettings> = {}) {
  nextVisitor();
  const mails: Mail[] = [];
  const auth = createAuth({
    db: await testDb(),
    settings: testSettings(changes),
    sendMail: changes.mail === null ? null : async (mail) => { mails.push(mail); },
    verifyTurnstile: async (token) => { if (token !== 'good-token') throw new TurnstileRefused(['invalid-input-response']); },
  });
  return { auth, mails };
}

type Auth = Awaited<ReturnType<typeof setUp>>['auth'];

function post(path: string, body: unknown, headers: Record<string, string> = {}): Request {
  return new Request(`${BASE}/api/auth${path}`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', origin: BASE, 'x-forwarded-for': ip, ...headers },
    body: JSON.stringify(body),
  });
}

function get(path: string, headers: Record<string, string> = {}): Request {
  return new Request(path.startsWith('https://') ? path : `${BASE}/api/auth${path}`, { headers: { 'x-forwarded-for': ip, ...headers } });
}

function linkIn(mail: Mail): string {
  const match = /https:\/\/jeffhub\.ai\/api\/auth\/magic-link\/verify\?\S+/.exec(mail.text);
  if (match === null) throw new Error(`no sign-in link in: ${mail.text}`);
  return match[0];
}

function sessionCookie(response: Response): string | null {
  const cookie = response.headers.getSetCookie().find((c) => /session_token=[^;]+/.test(c));
  return cookie === undefined ? null : cookie;
}

const LINK_FORM = { email: 'ada@example.com', callbackURL: '/account', errorCallbackURL: '/signin' };

async function signIn(auth: Auth, mails: Mail[], email: string): Promise<string> {
  const sent = await auth.handler(post('/sign-in/magic-link', { ...LINK_FORM, email }, { 'x-turnstile-token': 'good-token' }));
  if (sent.status !== 200) throw new Error(`the link was not sent: ${sent.status} ${await sent.text()}`);
  const cookie = sessionCookie(await auth.handler(get(linkIn(mails[mails.length - 1]))));
  if (cookie === null) throw new Error('the link did not sign in');
  return cookie.split(';')[0];
}

async function sessionOf(auth: Auth, cookie: string): Promise<{ user: { id: string; email: string; emailVerified: boolean } } | null> {
  return (await auth.handler(get('/get-session', { cookie }))).json();
}

it('has the spec times: the email link 15 minutes, the session 30 days', () => {
  expect(MAGIC_LINK_SECONDS).toBe(900);
  expect(SESSION_SECONDS).toBe(2_592_000);
});

describe('the email link', () => {
  it('is sent after the bot check, and signs in once', async () => {
    const { auth, mails } = await setUp();
    const sent = await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }));
    expect(sent.status).toBe(200);
    expect(mails.map((m) => [m.to, m.subject])).toEqual([['ada@example.com', 'Your Jeff sign-in link']]);
    const first = await auth.handler(get(linkIn(mails[0])));
    expect(first.status).toBe(302);
    expect(first.headers.get('location')).toBe(`${BASE}/account`);
    const cookie = sessionCookie(first);
    expect(cookie).toMatch(/^__Secure-/);
    expect(cookie).toMatch(/HttpOnly/i);
    expect(cookie).toMatch(/Secure/i);
    expect(cookie).toMatch(/SameSite=Lax/i);
    expect(cookie).toMatch(/Path=\//);
    expect(cookie).toMatch(/Max-Age=2592000/);
    const again = await auth.handler(get(linkIn(mails[0])));
    expect(sessionCookie(again)).toBeNull();
    expect(again.headers.get('location')).toBe(`${BASE}/signin?error=INVALID_TOKEN`);
  });

  it('older than 15 minutes does not sign in', async () => {
    vi.useFakeTimers({ now: Date.parse('2026-11-03T10:00:00Z'), toFake: ['Date'] });
    const { auth, mails } = await setUp();
    await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }));
    vi.setSystemTime(Date.parse('2026-11-03T10:15:01Z'));
    const late = await auth.handler(get(linkIn(mails[0])));
    expect(sessionCookie(late)).toBeNull();
    expect(late.headers.get('location')).toBe(`${BASE}/signin?error=INVALID_TOKEN`);
  });

  it('within 15 minutes signs in', async () => {
    vi.useFakeTimers({ now: Date.parse('2026-11-03T10:00:00Z'), toFake: ['Date'] });
    const { auth, mails } = await setUp();
    await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }));
    vi.setSystemTime(Date.parse('2026-11-03T10:14:59Z'));
    expect(sessionCookie(await auth.handler(get(linkIn(mails[0]))))).not.toBeNull();
  });

  it('is stored only as a hash: the token in the email is not in the database', async () => {
    const db = await testDb();
    const mails: Mail[] = [];
    const auth = createAuth({ db, settings: testSettings(), sendMail: async (mail) => { mails.push(mail); },
      verifyTurnstile: async () => {} });
    await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }));
    const token = new URL(linkIn(mails[0])).searchParams.get('token') as string;
    const rows = await db.query.verification.findMany();
    expect(rows).toHaveLength(1);
    expect(JSON.stringify(rows)).not.toContain(token);
  });

  it('without a passing bot check no email is sent', async () => {
    const { auth, mails } = await setUp();
    for (const headers of [{}, { 'x-turnstile-token': 'bad-token' }] as Record<string, string>[]) {
      const response = await auth.handler(post('/sign-in/magic-link', LINK_FORM, headers));
      expect(response.status).toBe(403);
      expect(await response.text()).toContain('bot check');
    }
    expect(mails).toEqual([]);
  });

  it('is limited to 5 requests a minute from one address', async () => {
    const { auth, mails } = await setUp();
    const statuses: number[] = [];
    for (let i = 0; i < 6; i += 1) {
      statuses.push((await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }))).status);
    }
    expect(statuses).toEqual([200, 200, 200, 200, 200, 429]);
    expect(mails).toHaveLength(5);
  });

  it('a new email address becomes a verified user', async () => {
    const { auth, mails } = await setUp();
    const cookie = await signIn(auth, mails, 'new@example.com');
    expect((await sessionOf(auth, cookie))?.user).toMatchObject({ email: 'new@example.com', emailVerified: true });
  });
});

// Every value the security review tried (task-10-review.md, I1), and more: other sites, protocol-relative and
// encoded forms, the three Better Auth itself let through (/%09/evil.example/, /.//evil.example, / /evil.example),
// scripts, and near-misses of the hub's own pages. Only the exact paths in CALLBACK_PATHS are accepted.
const HOSTILE = [
  'https://evil.example/account', 'http://evil.example', '//evil.example/account', '///evil.example', '/%09/evil.example/',
  '/%09/evil.example', '/.//evil.example', '/ /evil.example', '/\\evil.example', '\\\\evil.example', '/%5Cevil.example',
  '/%5C/evil.example', '/%2F%2Fevil.example', '%2F%2Fevil.example', '/%2F/evil.example', 'https:evil.example',
  'https://jeffhub.ai@evil.example/', 'javascript:alert(1)', 'JavaScript:alert(1)', ' javascript:alert(1)',
  'java\tscript:alert(1)', 'data:text/html,<script>alert(1)</script>', 'https://jeffhub.ai/account', '/account/',
  '/account?next=//evil.example', '/account#x', '/Account', '/docs', '', ' /account',
];
const FIELDS = ['callbackURL', 'errorCallbackURL', 'newUserCallbackURL'] as const;
const CASES = FIELDS.flatMap((field) => HOSTILE.map((url) => [field, url] as const));

// Better Auth's own check refuses many of these first ("Invalid callbackURL", which it also logs); the hub's check
// refuses the rest ("callbackURL must be one of ..."). Either way the answer is 403 and names the field.
function refusesField(message: string, field: string, url: string): boolean {
  return message === `Invalid ${field}` || message === `${field} must be one of /account, /signin, /signup, /request-adapter; got ${JSON.stringify(url)}`;
}

describe('redirects stay on the hub', () => {
  let logged: ReturnType<typeof vi.spyOn>;
  beforeEach(() => { logged = vi.spyOn(console, 'error').mockImplementation(() => {}); });
  afterEach(() => {
    for (const [line] of logged.mock.calls) expect(String(line)).toMatch(/Invalid (callbackURL|errorCallbackURL|newUserCallbackURL)/);
    logged.mockRestore();
  });

  it("accepts exactly the hub's own pages", () => {
    expect(CALLBACK_PATHS).toEqual(['/account', '/signin', '/signup', '/request-adapter']);
  });

  it.each(CASES)('the email form refuses %s %j, and sends nothing', async (field, url) => {
    const { auth, mails } = await setUp();
    const response = await auth.handler(post('/sign-in/magic-link', { ...LINK_FORM, [field]: url }, { 'x-turnstile-token': 'good-token' }));
    expect(response.status).toBe(403);
    expect(refusesField((await response.json()).message, field, url)).toBe(true);
    expect(mails).toEqual([]);
  });

  it.each(CASES)('a social sign-in refuses %s %j', async (field, url) => {
    const { auth } = await setUp();
    const response = await auth.handler(post('/sign-in/social', { provider: 'github', callbackURL: '/account', [field]: url }));
    expect(response.status).toBe(403);
    expect(refusesField((await response.json()).message, field, url)).toBe(true);
  });

  it.each(CASES)('a real email link with %s set to %j does not sign in or redirect', async (field, url) => {
    const { auth, mails } = await setUp();
    await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }));
    const link = new URL(linkIn(mails[0]));
    link.searchParams.set(field, url);
    const response = await auth.handler(get(link.toString()));
    expect(response.status).toBe(403);
    expect(response.headers.get('location')).toBeNull();
    expect(sessionCookie(response)).toBeNull();
    // The link was not used up: as sent, it still signs in.
    expect(sessionCookie(await auth.handler(get(linkIn(mails[0]))))).not.toBeNull();
  });

  it.each(HOSTILE)('a made-up link (no valid token) with errorCallbackURL %j does not redirect', async (url) => {
    const { auth } = await setUp();
    const response = await auth.handler(get(`/magic-link/verify?token=x&callbackURL=%2Faccount&errorCallbackURL=${encodeURIComponent(url)}`));
    expect(response.status).toBe(403);
    expect(response.headers.get('location')).toBeNull();
  });

  it('a link without its callbackURL does not answer with the session token', async () => {
    const { auth, mails } = await setUp();
    await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }));
    const link = new URL(linkIn(mails[0]));
    link.searchParams.delete('callbackURL');
    const response = await auth.handler(get(link.toString()));
    expect(response.status).toBe(400);
    expect(sessionCookie(response)).toBeNull();
    expect(await response.text()).not.toContain('token');
  });

  it.each(['/account', '/signin', '/signup'])('a link may come back to %s', async (page) => {
    const { auth, mails } = await setUp();
    await auth.handler(post('/sign-in/magic-link', { ...LINK_FORM, callbackURL: page }, { 'x-turnstile-token': 'good-token' }));
    expect((await auth.handler(get(linkIn(mails[0])))).headers.get('location')).toBe(`${BASE}${page}`);
  });
});

describe('cross-site requests (CSRF)', () => {
  it('a signed-in POST from another site is refused, and the session lives on', async () => {
    const { auth, mails } = await setUp();
    const cookie = await signIn(auth, mails, 'ada@example.com');
    for (const origin of ['https://evil.example', 'null']) {
      const response = await auth.handler(post('/revoke-sessions', {}, { cookie, origin }));
      expect(response.status).toBe(403);
    }
    const noOrigin = new Request(`${BASE}/api/auth/sign-out`, { method: 'POST', headers: { cookie, 'content-type': 'application/json' }, body: '{}' });
    expect((await auth.handler(noOrigin)).status).toBe(403);
    expect(await sessionOf(auth, cookie)).not.toBeNull();
  });

  it('the email form refuses a cross-site form post and a foreign Origin', async () => {
    const { auth, mails } = await setUp();
    const crossSite = await auth.handler(post('/sign-in/magic-link', LINK_FORM,
      { 'x-turnstile-token': 'good-token', origin: 'https://evil.example', 'sec-fetch-site': 'cross-site', 'sec-fetch-mode': 'navigate' }));
    expect(crossSite.status).toBe(403);
    const foreign = await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token', origin: 'https://evil.example' }));
    expect(foreign.status).toBe(403);
    expect(mails).toEqual([]);
  });

  it('a GET to a state-changing endpoint is not served', async () => {
    const { auth, mails } = await setUp();
    const cookie = await signIn(auth, mails, 'ada@example.com');
    for (const path of ['/sign-out', '/revoke-sessions', '/sign-in/magic-link', '/sign-in/social']) {
      expect((await auth.handler(get(path, { cookie }))).status).toBe(404);
    }
    expect(await sessionOf(auth, cookie)).not.toBeNull();
  });
});

describe('sign-in starts name where to come back to', () => {
  it.each([
    ['/sign-in/magic-link', { email: 'ada@example.com', errorCallbackURL: '/signin' }],
    ['/sign-in/social', { provider: 'github', errorCallbackURL: '/signin' }],
  ])('%s without a callbackURL is a 400 naming it', async (path, body) => {
    const { auth, mails } = await setUp();
    const response = await auth.handler(post(path, body, { 'x-turnstile-token': 'good-token' }));
    expect(response.status).toBe(400);
    expect(await response.json()).toMatchObject({ code: 'CALLBACK_REQUIRED', message: `${path} needs a callbackURL: one of /account, /signin, /signup, /request-adapter` });
    expect(mails).toEqual([]);
  });
});

// At most 5 sign-in emails an hour to one address, counted in the database whichever instance and visitor address
// the request comes from (email-limit.server.ts).
describe('sign-in emails per address', () => {
  it('the 6th for one address in the hour is refused through another instance; other addresses go on', async () => {
    const db = await testDb();
    const mails: Mail[] = [];
    const make = () => createAuth({ db, settings: testSettings(), sendMail: async (mail) => { mails.push(mail); },
      verifyTurnstile: async () => {} });
    const [a, b] = [make(), make()];
    const send = async (instance: Auth, email: string) => {
      nextVisitor(); // a new visitor address each time, so only the per-address limit can refuse
      return instance.handler(post('/sign-in/magic-link', { ...LINK_FORM, email }, { 'x-turnstile-token': 'good-token' }));
    };
    const statuses: number[] = [];
    for (let i = 0; i < 5; i += 1) statuses.push((await send(a, 'ada@example.com')).status);
    const sixth = await send(b, 'Ada@Example.com');
    statuses.push(sixth.status);
    expect(statuses).toEqual([200, 200, 200, 200, 200, 429]);
    expect(await sixth.json()).toMatchObject({ code: 'TOO_MANY_SIGN_IN_EMAILS',
      message: 'We already sent 5 sign-in links to this address in the last hour. Use one of those, or try again later.' });
    expect((await send(b, 'grace@example.com')).status).toBe(200);
    expect(mails.map((m) => m.to)).toEqual([...Array(5).fill('ada@example.com'), 'grace@example.com']);
  });
});

describe('endpoints the hub does not use', () => {
  it.each(['/list-sessions', '/update-user', '/get-access-token', '/refresh-token', '/unlink-account', '/link-social',
    '/revoke-other-sessions', '/revoke-session', '/update-session', '/list-accounts', '/delete-user', '/account-info'])(
    '%s answers 404, signed in or not', async (path) => {
      const { auth, mails } = await setUp();
      const cookie = await signIn(auth, mails, 'ada@example.com');
      const asPost = await auth.handler(post(path, {}, { cookie }));
      const asGet = await auth.handler(get(path, { cookie }));
      expect([asPost.status, asGet.status]).toEqual([404, 404]);
      expect(await asPost.text()).not.toMatch(/"token"/);
    });
});

describe('social sign-in from another site', () => {
  it.each([
    [{ origin: 'https://evil.example' }],
    [{ origin: 'null' }],
    [{ origin: BASE, 'sec-fetch-site': 'cross-site' }],
  ])('is refused: %j', async (headers) => {
    const { auth } = await setUp();
    const response = await auth.handler(post('/sign-in/social', { provider: 'github', callbackURL: '/account' }, headers));
    expect(response.status).toBe(403);
    expect((await response.json()).code).toBe('CROSS_SITE_SIGN_IN');
  });

  it('is refused without an Origin', async () => {
    const { auth } = await setUp();
    const request = new Request(`${BASE}/api/auth/sign-in/social`, { method: 'POST',
      headers: { 'content-type': 'application/json', 'x-forwarded-for': ip }, body: JSON.stringify({ provider: 'github', callbackURL: '/account' }) });
    expect((await auth.handler(request)).status).toBe(403);
  });
});

// The counts are kept in the database (rate_limit), so two function instances share them: two createAuth on one
// database stand for two Vercel instances.
describe('rate limits in the database', () => {
  async function twoInstances() {
    const db = await testDb();
    const mails: Mail[] = [];
    const make = () => createAuth({ db, settings: testSettings(), sendMail: async (mail) => { mails.push(mail); },
      verifyTurnstile: async () => {} });
    nextVisitor();
    return { a: make(), b: make(), db, mails };
  }

  it('5 email requests through one instance, and the 6th through the other is refused', async () => {
    const { a, b, mails } = await twoInstances();
    const statuses: number[] = [];
    for (let i = 0; i < 5; i += 1) {
      statuses.push((await a.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }))).status);
    }
    statuses.push((await b.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }))).status);
    expect(statuses).toEqual([200, 200, 200, 200, 200, 429]);
    expect(mails).toHaveLength(5);
  });

  it('get-session is not counted', async () => {
    const { a, b, db } = await twoInstances();
    for (let i = 0; i < 120; i += 1) expect((await (i % 2 === 0 ? a : b).handler(get('/get-session'))).status).toBe(200);
    expect(await db.query.rateLimit.findMany()).toEqual([]);
  });

  it('rows older than the longest window are deleted', async () => {
    vi.useFakeTimers({ now: Date.parse('2026-11-03T10:00:00Z'), toFake: ['Date'] });
    const { a, db } = await twoInstances();
    const first = ip;
    await a.handler(post('/sign-out', {}));
    ip = '198.51.100.1';
    await a.handler(post('/sign-out', {}));
    expect((await db.query.rateLimit.findMany()).map((row) => row.key).sort()).toEqual(['198.51.100.1|/sign-out', `${first}|/sign-out`].sort());
    vi.setSystemTime(Date.parse('2026-11-03T10:05:00Z'));
    ip = first;
    await a.handler(post('/sign-out', {}));
    expect((await db.query.rateLimit.findMany()).map((row) => row.key)).toEqual([`${first}|/sign-out`]);
  });
});

describe('GitHub and Google', () => {
  it.each([
    ['github', 'https://github.com/login/oauth/authorize'],
    ['google', 'https://accounts.google.com/'],
  ])('%s sign-in redirects to the provider with the hub callback', async (provider, start) => {
    const { auth } = await setUp();
    const response = await auth.handler(post('/sign-in/social', { provider, callbackURL: '/account', errorCallbackURL: '/signin' }));
    expect(response.status).toBe(200);
    const { url } = (await response.json()) as { url: string };
    expect(url.startsWith(start)).toBe(true);
    expect(new URL(url).searchParams.get('redirect_uri')).toBe(`${BASE}/api/auth/callback/${provider}`);
  });

  it('the test provider is not there unless AUTH_TEST_PROVIDER names one', async () => {
    const { auth } = await setUp();
    const response = await auth.handler(post('/sign-in/social', { provider: 'test', callbackURL: '/account' }));
    expect(response.status).toBe(404);
  });
});

// SIGN_IN_PROVIDERS: Better Auth is configured with the listed methods only, and refuses the others by name.
describe('only the methods SIGN_IN_PROVIDERS lists', () => {
  const GITHUB_AND_EMAIL = { signInProviders: ['github', 'email'], google: null } as const;

  it('github,email: Google is refused by name, GitHub and the email link work', async () => {
    const { auth, mails } = await setUp({ ...GITHUB_AND_EMAIL, signInProviders: [...GITHUB_AND_EMAIL.signInProviders] });
    const google = await auth.handler(post('/sign-in/social', { provider: 'google', callbackURL: '/account' }));
    expect(google.status).toBe(404);
    expect(await google.json()).toMatchObject({ code: 'SIGN_IN_METHOD_NOT_OFFERED',
      message: 'the hub does not offer sign-in with google: SIGN_IN_PROVIDERS is github,email' });
    const callback = await auth.handler(get('/callback/google?code=c&state=s'));
    expect(callback.status).toBe(404);
    expect((await callback.json()).message).toBe('the hub does not offer sign-in with google: SIGN_IN_PROVIDERS is github,email');
    expect((await auth.handler(post('/sign-in/social', { provider: 'github', callbackURL: '/account' }))).status).toBe(200);
    expect(await sessionOf(auth, await signIn(auth, mails, 'ada@example.com'))).not.toBeNull();
  });

  it('an unknown provider is refused by name', async () => {
    const { auth } = await setUp();
    const response = await auth.handler(post('/sign-in/social', { provider: 'facebook', callbackURL: '/account' }));
    expect(response.status).toBe(404);
    expect((await response.json()).message).toBe('the hub has no sign-in provider "facebook": SIGN_IN_PROVIDERS is github,google,email');
  });

  it('without email, the email link\'s endpoints are not there and no email is sent', async () => {
    const { auth, mails } = await setUp({ signInProviders: ['github'], google: null, mail: null });
    const form = await auth.handler(post('/sign-in/magic-link', LINK_FORM, { 'x-turnstile-token': 'good-token' }));
    expect(form.status).toBe(404);
    expect(mails).toEqual([]);
  });

  it('refuses a sendMail that does not match the listed methods', async () => {
    const db = await testDb();
    const verifyTurnstile = async () => {};
    expect(() => createAuth({ db, settings: testSettings({ mail: null, signInProviders: ['github'] }), sendMail: async () => {}, verifyTurnstile }))
      .toThrow('createAuth was given a sendMail although SIGN_IN_PROVIDERS does not list email');
    expect(() => createAuth({ db, settings: testSettings(), sendMail: null, verifyTurnstile }))
      .toThrow('createAuth needs a sendMail: SIGN_IN_PROVIDERS lists email');
  });
});

describe('sessions', () => {
  it('sign out ends this session', async () => {
    const { auth, mails } = await setUp();
    const cookie = await signIn(auth, mails, 'ada@example.com');
    expect((await auth.handler(post('/sign-out', {}, { cookie }))).status).toBe(200);
    expect(await sessionOf(auth, cookie)).toBeNull();
  });

  it('sign out everywhere ends every session of the user', async () => {
    const { auth, mails } = await setUp();
    const first = await signIn(auth, mails, 'ada@example.com');
    const second = await signIn(auth, mails, 'ada@example.com');
    const other = await signIn(auth, mails, 'grace@example.com');
    const revoked = await auth.handler(post('/revoke-sessions', {}, { cookie: first }));
    expect(revoked.status).toBe(200);
    expect(await sessionOf(auth, first)).toBeNull();
    expect(await sessionOf(auth, second)).toBeNull();
    expect(await sessionOf(auth, other)).not.toBeNull();
  });

  it('the account page\'s sign out everywhere (revokeEverySession, through Better Auth\'s own API) does the same', async () => {
    const { auth, mails } = await setUp();
    const first = await signIn(auth, mails, 'ada@example.com');
    const second = await signIn(auth, mails, 'ada@example.com');
    const other = await signIn(auth, mails, 'grace@example.com');
    await revokeEverySession(auth, new Headers({ cookie: first, origin: BASE, 'x-forwarded-for': ip }));
    expect(await sessionOf(auth, first)).toBeNull();
    expect(await sessionOf(auth, second)).toBeNull();
    expect(await sessionOf(auth, other)).not.toBeNull();
  });
});

// The test OpenID provider (AUTH_TEST_PROVIDER), answered by a stubbed fetch: discovery, the token exchange and the
// user info. It stands in for GitHub and Google, whose linking rule is the same: Better Auth joins a provider's
// sign-in to an existing user with the same email only when the provider says the email is verified.
describe('account linking', () => {
  const ISSUER = 'https://idp.test';

  function stubProvider(person: { id: string; email: string; email_verified: boolean; name: string }) {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      if (url === `${ISSUER}/.well-known/openid-configuration`) {
        return Response.json({ issuer: ISSUER, authorization_endpoint: `${ISSUER}/authorize`, token_endpoint: `${ISSUER}/token`,
          userinfo_endpoint: `${ISSUER}/userinfo` });
      }
      if (url === `${ISSUER}/token`) return Response.json({ access_token: 'access', token_type: 'Bearer', expires_in: 3600 });
      if (url === `${ISSUER}/userinfo`) return Response.json({ sub: person.id, ...person });
      throw new Error(`the test reached an address it does not answer: ${url}`);
    }));
  }

  async function signInWithProvider(auth: Auth): Promise<Response> {
    const start = await auth.handler(post('/sign-in/social', { provider: 'test', callbackURL: '/account', errorCallbackURL: '/signin' }));
    expect(start.status).toBe(200);
    const { url } = (await start.json()) as { url: string };
    const state = new URL(url).searchParams.get('state') as string;
    const cookies = start.headers.getSetCookie().map((c) => c.split(';')[0]).join('; ');
    return auth.handler(get(`/callback/test?code=the-code&state=${encodeURIComponent(state)}`, { cookie: cookies }));
  }

  it('a provider that says the email is verified signs in to the same account', async () => {
    stubProvider({ id: 'p-1', email: 'linked@example.com', email_verified: true, name: 'Linked' });
    const { auth, mails } = await setUp({ testOAuthIssuer: ISSUER });
    const byEmail = (await sessionOf(auth, await signIn(auth, mails, 'linked@example.com')))?.user.id;
    const response = await signInWithProvider(auth);
    expect(response.headers.get('location')).toBe('/account');
    const cookie = sessionCookie(response);
    if (cookie === null) throw new Error('the provider did not sign in');
    expect((await sessionOf(auth, cookie.split(';')[0]))?.user.id).toBe(byEmail);
  });

  it('a provider that does not say the email is verified is not joined to the account with that email', async () => {
    stubProvider({ id: 'p-2', email: 'careful@example.com', email_verified: false, name: 'Careful' });
    const { auth, mails } = await setUp({ testOAuthIssuer: ISSUER });
    await signIn(auth, mails, 'careful@example.com');
    const response = await signInWithProvider(auth);
    expect(sessionCookie(response)).toBeNull();
    expect(response.headers.get('location')).toMatch(/^\/signin\?error=account_not_linked/);
  });
});

// Final review C1: no account is made, and none is joined, for an address the provider has not verified. GitHub's
// three addresses (token exchange, /user, /user/emails) are answered by a stubbed fetch, so the flag is the one
// GitHub's emails API gives for the address, as Better Auth reads it.
describe('an unverified provider email', () => {
  function stubGitHub(profileEmail: string | null, emails: { email: string; primary: boolean; verified: boolean }[]) {
    vi.stubGlobal('fetch', vi.fn(async (input: string | URL | Request) => {
      const url = typeof input === 'string' ? input : input instanceof URL ? input.href : input.url;
      if (url === 'https://github.com/login/oauth/access_token') return Response.json({ access_token: 'gh-access', token_type: 'bearer', scope: 'read:user,user:email' });
      if (url === 'https://api.github.com/user') return Response.json({ id: 4242, login: 'mallory', name: 'Mallory', email: profileEmail, avatar_url: null });
      if (url === 'https://api.github.com/user/emails') return Response.json(emails);
      throw new Error(`the test reached an address it does not answer: ${url}`);
    }));
  }

  async function signInWithGitHub(auth: Auth): Promise<Response> {
    const start = await auth.handler(post('/sign-in/social', { provider: 'github', callbackURL: '/account', errorCallbackURL: '/signin' }));
    expect(start.status).toBe(200);
    const { url } = (await start.json()) as { url: string };
    const state = new URL(url).searchParams.get('state') as string;
    const cookies = start.headers.getSetCookie().map((c) => c.split(';')[0]).join('; ');
    return auth.handler(get(`/callback/github?code=the-code&state=${encodeURIComponent(state)}`, { cookie: cookies }));
  }

  async function setUpWithDb() {
    nextVisitor();
    const db = await testDb();
    const mails: Mail[] = [];
    const auth = createAuth({ db, settings: testSettings(), sendMail: async (mail) => { mails.push(mail); }, verifyTurnstile: async () => {} });
    return { db, auth, mails };
  }

  it('a verified GitHub email signs up (the stub works)', async () => {
    stubGitHub(null, [{ email: 'new@example.com', primary: true, verified: true }]);
    const { db, auth } = await setUpWithDb();
    const response = await signInWithGitHub(auth);
    expect(sessionCookie(response)).not.toBeNull();
    expect((await db.query.user.findMany()).map((u) => [u.email, u.emailVerified])).toEqual([['new@example.com', true]]);
  });

  it('cannot sign up with GitHub: no user, no session', async () => {
    stubGitHub(null, [{ email: 'owner@example.com', primary: true, verified: false }]);
    const { db, auth } = await setUpWithDb();
    const response = await signInWithGitHub(auth);
    expect(sessionCookie(response)).toBeNull();
    expect(response.headers.get('location')).toMatch(/\/signin\?error=/);
    expect(await db.query.user.findMany()).toEqual([]);
    expect(await db.query.account.findMany()).toEqual([]);
  });

  it('is unverified when GitHub\'s emails API says so, even if the profile shows the address publicly', async () => {
    stubGitHub('owner@example.com', [{ email: 'owner@example.com', primary: true, verified: false }]);
    const { db, auth } = await setUpWithDb();
    expect(sessionCookie(await signInWithGitHub(auth))).toBeNull();
    expect(await db.query.user.findMany()).toEqual([]);
  });

  it('cannot take over an existing account with that address', async () => {
    stubGitHub(null, [{ email: 'ada@example.com', primary: true, verified: false }]);
    const { db, auth, mails } = await setUpWithDb();
    await signIn(auth, mails, 'ada@example.com');
    const response = await signInWithGitHub(auth);
    expect(sessionCookie(response)).toBeNull();
    expect((await db.query.account.findMany()).filter((a) => a.providerId === 'github')).toEqual([]);
  });

  it('the email link still signs up, verified', async () => {
    const { db, auth, mails } = await setUpWithDb();
    await signIn(auth, mails, 'owner@example.com');
    expect((await db.query.user.findMany()).map((u) => [u.email, u.emailVerified])).toEqual([['owner@example.com', true]]);
  });
});
