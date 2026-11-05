import type { SignInProvider, SocialProvider } from './auth-shared.js';
import { KEY_PATTERN, KEY_PREFIX } from './keys-format.js';
import { MissingSettingsError } from '../../server/settings.js';
import { addressSetting, inProduction, oneOf, positiveInteger, positiveNumber, requireEnv } from './env.server.js';
import { parseAddress } from './mail.server.js';
import { PUBLIC_SETTING_NAMES, publicSettings } from './public-settings.server.js';

// Every runtime setting of the accounts work (the table in the plan's Task 9; the go-live runbook lists them too),
// read at run time only, by the hub's function: never import this module from a page. The settings of the sign-in
// methods are read according to SIGN_IN_PROVIDERS (a public setting): GITHUB_CLIENT_ID and GITHUB_CLIENT_SECRET when
// it lists github, GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET when it lists google, and MAIL_TRANSPORT and MAIL_FROM
// (then SENDGRID_API_KEY or MAIL_TEST_DIR, according to MAIL_TRANSPORT) when it lists email. A listed method with a
// missing setting is an error naming the setting; an unlisted method's settings are not read.
export const ACCOUNTS_SETTING_NAMES = [...PUBLIC_SETTING_NAMES, 'DATABASE_URL', 'BETTER_AUTH_SECRET', 'BETTER_AUTH_URL',
  'TURNSTILE_SECRET_KEY', 'TURNSTILE_VERIFY_URL', 'STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET', 'STRIPE_PRICE_ID',
  'JEFF_HUB_INTERNAL_TOKEN', 'PLAYGROUND_API_KEY', 'PLAYGROUND_API_URL',
  'PLAYGROUND_RUNS_PER_SECOND', 'PLAYGROUND_PASS_SECRET', 'OWNER_EMAIL', 'AUTH_TEST_PROVIDER',
  'JEFF_HUB_API_ORIGIN'] as const;

// The settings each sign-in method needs, read only when SIGN_IN_PROVIDERS lists it.
export const SIGN_IN_PROVIDER_SETTINGS = {
  github: ['GITHUB_CLIENT_ID', 'GITHUB_CLIENT_SECRET'],
  google: ['GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET'],
  email: ['MAIL_TRANSPORT', 'MAIL_FROM'],
} as const satisfies Record<SignInProvider, readonly string[]>;

// Cloudflare Turnstile's server-side check, the only TURNSTILE_VERIFY_URL production accepts.
export const TURNSTILE_SITEVERIFY = 'https://challenges.cloudflare.com/turnstile/v0/siteverify';

export type MailSettings =
  | { transport: 'sendgrid'; apiKey: string; from: string }
  | { transport: 'test-mailbox'; dir: string; from: string };

export interface OAuthApp { clientId: string; clientSecret: string }

export interface AccountsSettings {
  databaseUrl: string;
  authSecret: string;
  baseUrl: string;
  // SIGN_IN_PROVIDERS, in its order. Each method's settings are null exactly when it is not listed.
  signInProviders: SignInProvider[];
  github: OAuthApp | null;
  google: OAuthApp | null;
  mail: MailSettings | null;
  turnstile: { siteKey: string; secretKey: string; verifyUrl: string };
  stripe: { secretKey: string; webhookSecret: string; priceId: string; automaticTax: boolean };
  internalToken: string;
  playground: { apiKey: string; apiUrl: string; runsPerMinute: number; runsPerSecond: number; passSecret: string };
  ownerEmail: string;
  testOAuthIssuer: string | null;
  plan: { priceUsd: number; allowanceInputTokens: number };
}

export function accountsSettings(): AccountsSettings {
  const env = requireEnv(ACCOUNTS_SETTING_NAMES);
  const shown = publicSettings();
  const production = inProduction();
  const listed = (provider: SignInProvider) => shown.signInProviders.includes(provider);
  const databaseUrl = checkedDatabaseUrl(env.DATABASE_URL, production);
  const testProvider = env.AUTH_TEST_PROVIDER;
  if (testProvider !== 'off' && !/^https?:\/\/[^/]+(\/.*)?$/.test(testProvider)) {
    throw new Error(`AUTH_TEST_PROVIDER=${JSON.stringify(testProvider)} must be off or a test OpenID issuer URL`);
  }
  if (production && testProvider !== 'off') throw new Error('AUTH_TEST_PROVIDER must be off in production');
  // Better Auth reads these itself and would let them replace BETTER_AUTH_SECRET (review N4): one secret, named here.
  for (const name of ['BETTER_AUTH_SECRETS', 'AUTH_SECRET']) {
    if (process.env[name] !== undefined) throw new Error(`${name} is set; remove it: the hub's only signing secret is BETTER_AUTH_SECRET`);
  }
  // Better Auth signs sessions with it and only warns when it is short (review M7).
  if (env.BETTER_AUTH_SECRET.length < 32) throw new Error('BETTER_AUTH_SECRET must be at least 32 characters (make one with scripts/make-secret.mjs)');
  // It signs the playground's pass cookies and keys the hash of visitors' addresses (playground.server.ts).
  if (env.PLAYGROUND_PASS_SECRET.length < 32) throw new Error('PLAYGROUND_PASS_SECRET must be at least 32 characters (make one with scripts/make-secret.mjs)');
  // Anything else answering there would decide the bot check (review M6); tests point it at a local stand-in.
  if (production && env.TURNSTILE_VERIFY_URL !== TURNSTILE_SITEVERIFY) {
    throw new Error(`TURNSTILE_VERIFY_URL must be ${TURNSTILE_SITEVERIFY} in production`);
  }
  checkedPlaygroundKey(env.PLAYGROUND_API_KEY);
  return {
    databaseUrl,
    authSecret: env.BETTER_AUTH_SECRET,
    baseUrl: addressSetting('BETTER_AUTH_URL', env.BETTER_AUTH_URL, production),
    signInProviders: shown.signInProviders,
    github: listed('github') ? oauthApp('github') : null,
    google: listed('google') ? oauthApp('google') : null,
    mail: listed('email') ? mailSettings(listedSettings('email'), production) : null,
    turnstile: { siteKey: shown.turnstileSiteKey, secretKey: env.TURNSTILE_SECRET_KEY, verifyUrl: env.TURNSTILE_VERIFY_URL },
    stripe: {
      secretKey: env.STRIPE_SECRET_KEY, webhookSecret: env.STRIPE_WEBHOOK_SECRET, priceId: env.STRIPE_PRICE_ID,
      automaticTax: shown.automaticTax,
    },
    internalToken: env.JEFF_HUB_INTERNAL_TOKEN,
    playground: {
      apiKey: env.PLAYGROUND_API_KEY,
      apiUrl: addressSetting('PLAYGROUND_API_URL', env.PLAYGROUND_API_URL, production),
      runsPerMinute: shown.playgroundRunsPerMinute,
      runsPerSecond: positiveNumber('PLAYGROUND_RUNS_PER_SECOND', env.PLAYGROUND_RUNS_PER_SECOND),
      passSecret: env.PLAYGROUND_PASS_SECRET,
    },
    ownerEmail: env.OWNER_EMAIL.toLowerCase(),
    testOAuthIssuer: testProvider === 'off' ? null : testProvider,
    plan: { priceUsd: shown.priceUsd, allowanceInputTokens: shown.allowanceInputTokens },
  };
}

function checkedDatabaseUrl(databaseUrl: string, production: boolean): string {
  if (!/^postgres(ql)?:\/\//.test(databaseUrl) && !databaseUrl.startsWith('pglite:')) {
    throw new Error(`DATABASE_URL must be a postgres:// URL (or pglite: for tests); got ${databaseUrl.split(':')[0]}:...`);
  }
  if (production && databaseUrl.startsWith('pglite:')) throw new Error('DATABASE_URL=pglite: is refused in production');
  return databaseUrl;
}

function checkedPlaygroundKey(key: string): string {
  if (!KEY_PATTERN.test(key)) throw new Error(`PLAYGROUND_API_KEY must be a whole ${KEY_PREFIX} key (make one with scripts/make-secret.mjs --key)`);
  return key;
}

// --- The settings of the endpoints that must keep working whatever the sign-in and mail settings are (final review
// I7): the database alone (getDb), the Stripe webhook, and the internal API jeff-server polls for the key list. Each
// reads only its own settings, with the same checks accountsSettings() makes, so a missing or wrong sign-in or mail
// setting cannot stop the webhook or every paid key; a missing one of their own is still an error naming it. ---

export const DATABASE_SETTING_NAMES = ['DATABASE_URL'] as const;

export function databaseUrlSetting(): string {
  return checkedDatabaseUrl(requireEnv(DATABASE_SETTING_NAMES).DATABASE_URL, inProduction());
}

export const WEBHOOK_SETTING_NAMES = [...DATABASE_SETTING_NAMES, 'BETTER_AUTH_URL', 'STRIPE_SECRET_KEY', 'STRIPE_WEBHOOK_SECRET'] as const;

export interface WebhookSettings { baseUrl: string; stripe: { secretKey: string; webhookSecret: string } }

export function webhookSettings(): WebhookSettings {
  const env = requireEnv(WEBHOOK_SETTING_NAMES);
  return { baseUrl: addressSetting('BETTER_AUTH_URL', env.BETTER_AUTH_URL, inProduction()),
    stripe: { secretKey: env.STRIPE_SECRET_KEY, webhookSecret: env.STRIPE_WEBHOOK_SECRET } };
}

export const INTERNAL_SETTING_NAMES = [...DATABASE_SETTING_NAMES, 'JEFF_HUB_INTERNAL_TOKEN', 'PLAN_ALLOWANCE_INPUT_TOKENS',
  'PLAYGROUND_API_KEY', 'PLAYGROUND_RUNS_PER_SECOND'] as const;

export interface InternalSettings {
  internalToken: string;
  plan: { allowanceInputTokens: number };
  playground: { apiKey: string; runsPerSecond: number };
}

export function internalSettings(): InternalSettings {
  const env = requireEnv(INTERNAL_SETTING_NAMES);
  return {
    internalToken: env.JEFF_HUB_INTERNAL_TOKEN,
    plan: { allowanceInputTokens: positiveInteger('PLAN_ALLOWANCE_INPUT_TOKENS', env.PLAN_ALLOWANCE_INPUT_TOKENS) },
    playground: { apiKey: checkedPlaygroundKey(env.PLAYGROUND_API_KEY),
      runsPerSecond: positiveNumber('PLAYGROUND_RUNS_PER_SECOND', env.PLAYGROUND_RUNS_PER_SECOND) },
  };
}

// A listed sign-in method's settings, or a MissingSettingsError naming each missing one and the listing that needs it.
function listedSettings(provider: SignInProvider): Record<string, string> {
  try {
    return requireEnv(SIGN_IN_PROVIDER_SETTINGS[provider]);
  } catch (error) {
    if (!(error instanceof MissingSettingsError)) throw error;
    throw new MissingSettingsError(error.missing, `SIGN_IN_PROVIDERS lists ${provider}, which needs ${error.missing.length === 1 ? 'it' : 'them'}`);
  }
}

function oauthApp(provider: SocialProvider): OAuthApp {
  const [idName, secretName] = SIGN_IN_PROVIDER_SETTINGS[provider];
  const env = listedSettings(provider);
  return { clientId: env[idName], clientSecret: env[secretName] };
}

// The email link's mail: SendGrid, or the test mailbox (refused in production).
function mailSettings(env: Record<string, string>, production: boolean): MailSettings {
  const transport = oneOf('MAIL_TRANSPORT', env.MAIL_TRANSPORT, ['sendgrid', 'test-mailbox'] as const);
  parseAddress(env.MAIL_FROM);
  if (transport === 'sendgrid') {
    return { transport, apiKey: listedMailSetting('SENDGRID_API_KEY', transport), from: env.MAIL_FROM };
  }
  if (production) throw new Error('MAIL_TRANSPORT=test-mailbox is refused in production; use sendgrid');
  return { transport, dir: listedMailSetting('MAIL_TEST_DIR', transport), from: env.MAIL_FROM };
}

// The mail settings the email about a new adapter request is sent with (adapter-requests.server.ts), and the address
// it goes to: needed whatever SIGN_IN_PROVIDERS lists, so they are read here every time; a missing one is an error
// naming it. ADAPTER_REQUEST_NOTIFY_EMAIL is where new requests are announced (owner, 4 November 2026:
// mstrasser@scissero.com); OWNER_EMAIL still decides who the owner is.
export const REQUEST_MAIL_SETTING_NAMES = ['MAIL_TRANSPORT', 'MAIL_FROM', 'ADAPTER_REQUEST_NOTIFY_EMAIL'] as const;

// ADAPTER_REQUEST_NOTIFY_EMAIL: one bare address.
export function requestNotifyEmail(): string {
  const address = requireEnv(['ADAPTER_REQUEST_NOTIFY_EMAIL']).ADAPTER_REQUEST_NOTIFY_EMAIL.trim();
  if (!/^[^<>\s@]+@[^<>\s@]+$/.test(address)) {
    throw new Error(`ADAPTER_REQUEST_NOTIFY_EMAIL must be one email address, such as owner@example.com, not ${JSON.stringify(address)}`);
  }
  return address;
}

export function requestMailSettings(): MailSettings {
  const env = requireEnv(['MAIL_TRANSPORT', 'MAIL_FROM']);
  const production = inProduction();
  const transport = oneOf('MAIL_TRANSPORT', env.MAIL_TRANSPORT, ['sendgrid', 'test-mailbox'] as const);
  parseAddress(env.MAIL_FROM);
  const needed = (name: 'SENDGRID_API_KEY' | 'MAIL_TEST_DIR') => {
    try {
      return requireEnv([name])[name];
    } catch (error) {
      if (!(error instanceof MissingSettingsError)) throw error;
      throw new MissingSettingsError(error.missing, `adapter requests are emailed to ADAPTER_REQUEST_NOTIFY_EMAIL, and MAIL_TRANSPORT is ${transport}, which needs it`);
    }
  };
  if (transport === 'sendgrid') return { transport, apiKey: needed('SENDGRID_API_KEY'), from: env.MAIL_FROM };
  if (production) throw new Error('MAIL_TRANSPORT=test-mailbox is refused in production; use sendgrid');
  return { transport, dir: needed('MAIL_TEST_DIR'), from: env.MAIL_FROM };
}

function listedMailSetting(name: 'SENDGRID_API_KEY' | 'MAIL_TEST_DIR', transport: string): string {
  try {
    return requireEnv([name])[name];
  } catch (error) {
    if (!(error instanceof MissingSettingsError)) throw error;
    throw new MissingSettingsError(error.missing, `SIGN_IN_PROVIDERS lists email and MAIL_TRANSPORT is ${transport}, which needs it`);
  }
}
