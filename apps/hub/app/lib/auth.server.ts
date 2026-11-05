// Sign-in (spec Section 3) with Better Auth 1.7.16: GitHub, Google, and a one-time email link, no passwords. Served
// at /api/auth/* by the hub's function (server/endpoints/auth.ts). The option names and behaviour below were checked
// in the installed library's source (task-10-report.md lists what and where).
//
// - Only the methods SIGN_IN_PROVIDERS lists are configured, and a request for another one is refused by name
//   (signInRefusal, socialRefusal): social sign-in with an unlisted provider, its callback, and the email link's two endpoints when
//   email is not listed. The test provider is on only when AUTH_TEST_PROVIDER names one.
// - The email link works once and for 15 minutes: Better Auth's magic-link plugin consumes the token atomically on
//   the first use and treats an expired one as invalid; its token is stored hashed, so the database never holds a
//   usable link. Both an expired and a used link come back to /signin?error=INVALID_TOKEN.
// - The email form needs a passing Cloudflare Turnstile check, sent in the x-turnstile-token header (the hook below);
//   without one no email is sent.
// - No account is made for an address that is not proven (final review C1): the databaseHooks below refuse to create
//   a user whose emailVerified is false. The email link proves its address (Better Auth's magic-link plugin creates
//   the user verified); GitHub's flag is the one its emails API (/user/emails) gives for that address, Google's and
//   the test provider's the one their user info gives. Without this, a GitHub account with someone else's
//   unverified address would create that person's account, and keep its link once they signed in by email.
// - The same verified email from two methods is one account. Better Auth joins a GitHub, Google or test-provider
//   sign-in to the user with that email only when the provider reports the email as verified and the user's own email
//   is verified (every user here is, by the rule above). No provider is listed as trusted, which would skip
//   the provider's check.
// - Sessions are database sessions, 30 days, in an HttpOnly, Secure, SameSite=Lax cookie (__Secure- prefix). The
//   session is read from the database on every request (no cookie cache), so signing out everywhere ends every
//   session at once.
// - CSRF and redirects: every state change is a POST; a POST that carries the session cookie must come from the
//   hub's own origin, the sign-in forms refuse a cross-site post, and every callbackURL, errorCallbackURL and
//   newUserCallbackURL must be exactly one of the hub's pages (CALLBACK_PATHS; the before hook). Better Auth turns
//   its own checks off by default when NODE_ENV is "test", so they are turned on here explicitly: the tests check
//   the same behaviour production has.
// - Only the endpoints the hub uses answer (AUTH_PATHS); rate limits are counted in the database (rate_limit), shared
//   by every function instance.
import { betterAuth } from 'better-auth';
import { drizzleAdapter } from 'better-auth/adapters/drizzle';
import { APIError, createAuthMiddleware } from 'better-auth/api';
import { genericOAuth, magicLink } from 'better-auth/plugins';
import { accountsSettings, type AccountsSettings } from './accounts-settings.server.js';
import { CALLBACK_PATHS, MAGIC_LINK_SECONDS, SESSION_SECONDS, SOCIAL_PROVIDERS, TEST_PROVIDER_ID, type SocialProvider } from './auth-shared.js';
import { clientIp } from './client-ip.server.js';
import { claimSignInEmail, SIGN_IN_EMAILS_PER_HOUR } from './email-limit.server.js';
import { getDb, type Db } from './db.server.js';
import { mailerFor, signInEmail, type SendMail } from './mail.server.js';
import * as schema from './schema.js';
import { TURNSTILE_HEADER, TurnstileRefused, turnstileVerifier, type VerifyTurnstile } from './turnstile.server.js';

export { MAGIC_LINK_SECONDS, SESSION_SECONDS };

// sendMail is null exactly when SIGN_IN_PROVIDERS does not list email (settings.mail is null).
export interface AuthDeps { db: Db; settings: AccountsSettings; sendMail: SendMail | null; verifyTurnstile: VerifyTurnstile }

// The Better Auth endpoints the hub uses (as Better Auth names them, below /api/auth). Every other one it has
// (list-sessions, which hands session tokens to page scripts; update-user; get-access-token; unlink-account; ...)
// answers 404: the before hook refuses it (review M2).
export const AUTH_PATHS = new Set(['/get-session', '/sign-out', '/revoke-sessions', '/sign-in/social', '/sign-in/magic-link',
  '/magic-link/verify', '/callback/:id']);

// Whether a path below /api/auth is one of AUTH_PATHS, with /callback/:id matched as a pattern (one segment). The
// function's endpoint (server/endpoints/auth.ts) asks before Better Auth runs, so an unused path costs nothing, not
// even a rate-limit row (review N1).
export function isAuthPath(path: string): boolean {
  if (AUTH_PATHS.has(path)) return true;
  return /^\/callback\/[A-Za-z0-9_-]+$/.test(path);
}

// The email link's two endpoints, answered only when SIGN_IN_PROVIDERS lists email.
const EMAIL_PATHS = ['/sign-in/magic-link', '/magic-link/verify'];

// The provider ids social sign-in and its callback answer: the listed GitHub and Google, and the test provider when
// AUTH_TEST_PROVIDER names one.
function socialIds(settings: AccountsSettings): string[] {
  const ids: string[] = SOCIAL_PROVIDERS.filter((provider) => settings[provider] !== null);
  if (settings.testOAuthIssuer !== null) ids.push(TEST_PROVIDER_ID);
  return ids;
}

function refusalFor(method: string, settings: AccountsSettings): string {
  const list = `SIGN_IN_PROVIDERS is ${settings.signInProviders.join(',')}`;
  if (method === TEST_PROVIDER_ID) return 'the hub does not offer sign-in with the test provider: AUTH_TEST_PROVIDER is off';
  if ((SOCIAL_PROVIDERS as readonly string[]).includes(method) || method === 'email') return `the hub does not offer sign-in with ${method}: ${list}`;
  return `the hub has no sign-in provider ${JSON.stringify(method)}: ${list}`;
}

// Why the hub refuses a request below /api/auth (`path`) for a sign-in method it does not offer, or null when it
// offers it: the email link's endpoints, and a provider's callback. The function's endpoint asks before Better Auth
// runs (an unlisted method's endpoints are not even configured there); the before hook asks too.
export function signInRefusal(settings: AccountsSettings, path: string): string | null {
  if (EMAIL_PATHS.includes(path)) return settings.mail === null ? refusalFor('email', settings) : null;
  const callback = /^\/callback\/([A-Za-z0-9_-]+)$/.exec(path);
  if (callback !== null) return socialIds(settings).includes(callback[1]) ? null : refusalFor(callback[1], settings);
  return null;
}

// The same for social sign-in, whose provider is in the body (the before hook asks).
export function socialRefusal(settings: AccountsSettings, provider: unknown): string | null {
  if (typeof provider !== 'string') return 'social sign-in needs a provider: one of ' + socialIds(settings).join(', ');
  return socialIds(settings).includes(provider) ? null : refusalFor(provider, settings);
}

const CALLBACK_FIELDS = ['callbackURL', 'errorCallbackURL', 'newUserCallbackURL'] as const;

// Every callback field, in a body or a query, must be exactly one of CALLBACK_PATHS.
function checkCallbacks(source: unknown): void {
  if (source === undefined || source === null || typeof source !== 'object') return;
  for (const field of CALLBACK_FIELDS) {
    const value = (source as Record<string, unknown>)[field];
    if (value === undefined) continue;
    if (typeof value !== 'string' || !(CALLBACK_PATHS as readonly string[]).includes(value)) {
      throw new APIError('FORBIDDEN', { code: 'CALLBACK_NOT_ALLOWED',
        message: `${field} must be one of ${CALLBACK_PATHS.join(', ')}; got ${JSON.stringify(value)}` });
    }
  }
}

export function createAuth({ db, settings, sendMail, verifyTurnstile }: AuthDeps) {
  if ((settings.mail === null) !== (sendMail === null)) {
    throw new Error(settings.mail === null
      ? 'createAuth was given a sendMail although SIGN_IN_PROVIDERS does not list email'
      : 'createAuth needs a sendMail: SIGN_IN_PROVIDERS lists email');
  }
  const socialProviders: Partial<Record<SocialProvider, { clientId: string; clientSecret: string }>> = {};
  for (const provider of SOCIAL_PROVIDERS) {
    const app = settings[provider];
    if (app !== null) socialProviders[provider] = { clientId: app.clientId, clientSecret: app.clientSecret };
  }
  return betterAuth({
    baseURL: settings.baseUrl,
    basePath: '/api/auth',
    secret: settings.authSecret,
    trustedOrigins: [settings.baseUrl],
    database: drizzleAdapter(db, { provider: 'pg', schema }),
    emailAndPassword: { enabled: false },
    socialProviders,
    // The hub never uses the providers' access tokens; they are kept encrypted all the same (review M4).
    account: { accountLinking: { enabled: true, allowDifferentEmails: false }, encryptOAuthTokens: true },
    // An error Better Auth cannot send to a page's errorCallbackURL lands on the sign-in page, which says what it was.
    onAPIError: { errorURL: `${settings.baseUrl}/signin` },
    session: { expiresIn: SESSION_SECONDS, updateAge: 24 * 60 * 60, cookieCache: { enabled: false } },
    advanced: {
      useSecureCookies: true,
      defaultCookieAttributes: { httpOnly: true, secure: true, sameSite: 'lax' },
      disableCSRFCheck: false,
      disableOriginCheck: false,
      ipAddress: { ipAddressHeaders: ['x-forwarded-for'] },
    },
    // Counted in the database (the rate_limit table), so every function instance shares one count per visitor
    // address and path (review I2). get-session is not counted: every page that needs the session calls it.
    rateLimit: { enabled: true, window: 60, max: 100, storage: 'database', customRules: { '/get-session': false } },
    telemetry: { enabled: false },
    databaseHooks: {
      user: {
        create: {
          before: async (user) => {
            if (user.emailVerified !== true) {
              throw new APIError('FORBIDDEN', { code: 'EMAIL_NOT_VERIFIED',
                message: `The sign-in provider has not verified ${user.email}. Verify the address with the provider, or sign in with an email link.` });
            }
          },
        },
      },
    },
    hooks: {
      before: createAuthMiddleware(async (ctx) => {
        if (!AUTH_PATHS.has(ctx.path)) throw new APIError('NOT_FOUND', { message: `the hub does not use ${ctx.path}` });
        checkCallbacks(ctx.body);
        checkCallbacks(ctx.query);
        if (ctx.headers === undefined) throw new APIError('BAD_REQUEST', { message: `${ctx.path} needs its request headers` });
        const refused = ctx.path === '/sign-in/social'
          ? socialRefusal(settings, (ctx.body as { provider?: unknown } | undefined)?.provider)
          : signInRefusal(settings, ctx.path === '/callback/:id' ? `/callback/${String(ctx.params?.id)}` : ctx.path);
        if (refused !== null) throw new APIError('NOT_FOUND', { code: 'SIGN_IN_METHOD_NOT_OFFERED', message: refused });
        // The sign-in starts always name where to come back to (review N3): without one, Better Auth would choose.
        if ((ctx.path === '/sign-in/magic-link' || ctx.path === '/sign-in/social')
          && (ctx.body as { callbackURL?: unknown } | undefined)?.callbackURL === undefined) {
          throw new APIError('BAD_REQUEST', { code: 'CALLBACK_REQUIRED', message: `${ctx.path} needs a callbackURL: one of ${CALLBACK_PATHS.join(', ')}` });
        }
        // A link without its callbackURL would answer with the session token as JSON; the hub's links always carry one.
        if (ctx.path === '/magic-link/verify' && ctx.query?.callbackURL === undefined) {
          throw new APIError('BAD_REQUEST', { message: 'The sign-in link has no callbackURL; use the link from the email as it is' });
        }
        // Social sign-in has no form-CSRF check of its own in Better Auth (review M1): it must come from the hub's page.
        if (ctx.path === '/sign-in/social') {
          const origin = ctx.headers.get('origin');
          if (origin !== new URL(settings.baseUrl).origin || ctx.headers.get('sec-fetch-site') === 'cross-site') {
            throw new APIError('FORBIDDEN', { code: 'CROSS_SITE_SIGN_IN', message: `A sign-in must start on ${new URL(settings.baseUrl).origin}; this one came from ${JSON.stringify(origin)}` });
          }
        }
        if (ctx.path !== '/sign-in/magic-link') return;
        try {
          await verifyTurnstile(ctx.headers.get(TURNSTILE_HEADER), clientIp(ctx.headers));
        } catch (error) {
          // One known refusal becomes Better Auth's HTTP error; anything else is rethrown, never swallowed.
          if (error instanceof TurnstileRefused) throw new APIError('FORBIDDEN', { message: error.message });
          throw error;
        }
        // At most 5 links an hour to one email address, whoever asks (email-limit.server.ts; review M8).
        const email = (ctx.body as { email?: unknown } | undefined)?.email;
        if (typeof email !== 'string') throw new APIError('BAD_REQUEST', { message: 'The email-link form needs an email' });
        if (!(await claimSignInEmail(db, email))) {
          throw new APIError('TOO_MANY_REQUESTS', { code: 'TOO_MANY_SIGN_IN_EMAILS',
            message: `We already sent ${SIGN_IN_EMAILS_PER_HOUR} sign-in links to this address in the last hour. Use one of those, or try again later.` });
        }
      }),
    },
    plugins: [
      ...(sendMail === null ? [] : [magicLink({
        expiresIn: MAGIC_LINK_SECONDS,
        storeToken: 'hashed',
        sendMagicLink: async ({ email, url }) => { await sendMail(signInEmail(email, url)); },
      })]),
      // Tests and local runs only (AUTH_TEST_PROVIDER, refused in production): a local test OpenID provider.
      ...(settings.testOAuthIssuer === null ? [] : [genericOAuth({
        config: [{
          providerId: TEST_PROVIDER_ID,
          discoveryUrl: `${settings.testOAuthIssuer}/.well-known/openid-configuration`,
          clientId: 'hub-test-client',
          clientSecret: 'hub-test-client-secret',
          scopes: ['openid', 'email', 'profile'],
        }],
      })]),
    ],
  });
}

export type Auth = ReturnType<typeof createAuth>;

// One Better Auth per running function, made on first use from the settings. The promise is kept, so requests at
// the same moment share it.
let cached: Promise<Auth> | null = null;

export function auth(): Promise<Auth> {
  if (cached === null) {
    const settings = accountsSettings();
    cached = getDb().then((db) => createAuth({ db, settings, sendMail: settings.mail === null ? null : mailerFor(settings.mail, fetch),
      verifyTurnstile: turnstileVerifier({ ...settings.turnstile, hostname: new URL(settings.baseUrl).hostname }, fetch) }));
  }
  return cached;
}
