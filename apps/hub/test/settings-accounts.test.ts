// @vitest-environment node
// Every hub setting of the accounts work is required and named (plan Task 9): a missing or empty one, or one with a
// value the hub cannot use, is an error naming it, and the test-only values are refused in production.
import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  accountsSettings, ACCOUNTS_SETTING_NAMES, REQUEST_MAIL_SETTING_NAMES, requestMailSettings, requestNotifyEmail, SIGN_IN_PROVIDER_SETTINGS,
} from '../app/lib/accounts-settings.server';
import { priceText, publicSettings, tokensText } from '../app/lib/public-settings.server';
import { requireEnv } from '../app/lib/env.server';

const VALID: Record<string, string> = {
  PLAN_PRICE_USD: '1', PLAN_ALLOWANCE_INPUT_TOKENS: '50000000', TURNSTILE_SITE_KEY: 'site-key',
  PLAYGROUND_RUNS_PER_MINUTE: '5', SIGN_IN_PROVIDERS: 'github,google,email', DATABASE_URL: 'pglite:memory', BETTER_AUTH_SECRET: 'x'.repeat(32),
  BETTER_AUTH_URL: 'https://jeffhub.ai', GITHUB_CLIENT_ID: 'gh-id', GITHUB_CLIENT_SECRET: 'gh-secret',
  GOOGLE_CLIENT_ID: 'g-id', GOOGLE_CLIENT_SECRET: 'g-secret', MAIL_TRANSPORT: 'sendgrid', MAIL_FROM: 'Jeff <no-reply@jeffhub.ai>',
  SENDGRID_API_KEY: 'SG.test', TURNSTILE_SECRET_KEY: 'ts-secret',
  TURNSTILE_VERIFY_URL: 'https://challenges.cloudflare.com/turnstile/v0/siteverify', STRIPE_SECRET_KEY: 'sk_test_x',
  STRIPE_WEBHOOK_SECRET: 'whsec_x', STRIPE_PRICE_ID: 'price_x', STRIPE_AUTOMATIC_TAX: 'off',
  JEFF_HUB_INTERNAL_TOKEN: 'internal', PLAYGROUND_API_KEY: `jeff_live_${'c'.repeat(52)}`,
  PLAYGROUND_API_URL: 'https://api.jeffhub.ai', PLAYGROUND_RUNS_PER_SECOND: '5', PLAYGROUND_PASS_SECRET: 'a-pass-secret-that-is-at-least-32-characters',
  OWNER_EMAIL: 'owner@example.com', AUTH_TEST_PROVIDER: 'off', JEFF_HUB_API_ORIGIN: 'https://api.jeffhub.ai',
};

function stub(env: Record<string, string>) {
  for (const [name, value] of Object.entries(env)) vi.stubEnv(name, value);
}

afterEach(() => vi.unstubAllEnvs());

describe('hub settings', () => {
  it('reads every setting when all are given', () => {
    stub(VALID);
    const settings = accountsSettings();
    expect(settings.plan).toEqual({ priceUsd: 1, allowanceInputTokens: 50_000_000 });
    expect(settings.mail).toEqual({ transport: 'sendgrid', apiKey: 'SG.test', from: 'Jeff <no-reply@jeffhub.ai>' });
    expect(settings.stripe.automaticTax).toBe(false);
    expect(settings.testOAuthIssuer).toBeNull();
    expect(settings.playground.runsPerSecond).toBe(5);
    expect(settings.signInProviders).toEqual(['github', 'google', 'email']);
    expect(settings.github).toEqual({ clientId: 'gh-id', clientSecret: 'gh-secret' });
    expect(settings.google).toEqual({ clientId: 'g-id', clientSecret: 'g-secret' });
  });

  it('names every missing setting at once', () => {
    stub({ ...VALID, STRIPE_PRICE_ID: '', OWNER_EMAIL: '' });
    expect(() => accountsSettings()).toThrow(/STRIPE_PRICE_ID.*OWNER_EMAIL|OWNER_EMAIL.*STRIPE_PRICE_ID/);
  });

  it('lists every name the table in the plan lists', () => {
    const perMethod = [...Object.values(SIGN_IN_PROVIDER_SETTINGS).flat(), 'SENDGRID_API_KEY'];
    expect([...ACCOUNTS_SETTING_NAMES].sort()).toEqual(Object.keys(VALID).filter((n) => !perMethod.includes(n)).sort());
  });

  it.each([
    ['PLAN_PRICE_USD', '0'], ['PLAN_PRICE_USD', 'one'], ['PLAN_ALLOWANCE_INPUT_TOKENS', '1.5'],
    ['PLAYGROUND_RUNS_PER_MINUTE', '0'], ['PLAYGROUND_RUNS_PER_SECOND', '-1'], ['STRIPE_AUTOMATIC_TAX', 'yes'],
    ['MAIL_TRANSPORT', 'smtp'], ['BETTER_AUTH_URL', 'http://jeffhub.ai'], ['PLAYGROUND_API_URL', 'https://api.jeffhub.ai/'],
    ['PLAYGROUND_API_KEY', 'not-a-key'], ['DATABASE_URL', 'mysql://x'], ['AUTH_TEST_PROVIDER', 'on'],
    ['MAIL_TRANSPORT', 'resend'], ['MAIL_FROM', 'Jeff no-reply'],
  ])('refuses %s=%s naming it', (name, value) => {
    stub({ ...VALID, [name]: value });
    expect(() => accountsSettings()).toThrow(name);
  });

  it('needs SENDGRID_API_KEY with sendgrid and MAIL_TEST_DIR with the test mailbox', () => {
    stub({ ...VALID, SENDGRID_API_KEY: '' });
    expect(() => accountsSettings()).toThrow('SENDGRID_API_KEY');
    vi.unstubAllEnvs();
    stub({ ...VALID, MAIL_TRANSPORT: 'test-mailbox', SENDGRID_API_KEY: '' });
    expect(() => accountsSettings()).toThrow('MAIL_TEST_DIR');
  });

  describe('SIGN_IN_PROVIDERS', () => {
    // VALID without any sign-in method's own settings.
    const BARE = Object.fromEntries(Object.entries(VALID).filter(([name]) =>
      ![...Object.values(SIGN_IN_PROVIDER_SETTINGS).flat(), 'SENDGRID_API_KEY'].includes(name)));

    it('is required', () => {
      stub({ ...VALID, SIGN_IN_PROVIDERS: '' });
      expect(() => accountsSettings()).toThrow('SIGN_IN_PROVIDERS');
    });

    it.each([
      ['github,gogle', 'SIGN_IN_PROVIDERS="github,gogle" names an unknown sign-in provider "gogle"; each must be one of github, google, email'],
      ['GitHub', 'SIGN_IN_PROVIDERS="GitHub" names an unknown sign-in provider "GitHub"'],
      ['test', 'SIGN_IN_PROVIDERS="test" names an unknown sign-in provider "test"'],
      ['github,email,github', 'SIGN_IN_PROVIDERS="github,email,github" names github twice'],
      ['github,', 'SIGN_IN_PROVIDERS="github," has an empty entry'],
      [',', 'SIGN_IN_PROVIDERS="," has an empty entry'],
    ])('refuses %j, naming what is wrong', (value, message) => {
      stub({ ...VALID, SIGN_IN_PROVIDERS: value });
      expect(() => accountsSettings()).toThrow(message);
      expect(() => publicSettings()).toThrow(message);
    });

    it('keeps the listed order and allows spaces around a name', () => {
      stub({ ...VALID, SIGN_IN_PROVIDERS: ' email , github' });
      expect(accountsSettings().signInProviders).toEqual(['email', 'github']);
    });

    it('needs only the listed methods\' settings', () => {
      stub({ ...BARE, SIGN_IN_PROVIDERS: 'github', GITHUB_CLIENT_ID: 'gh-id', GITHUB_CLIENT_SECRET: 'gh-secret' });
      expect(accountsSettings()).toMatchObject({ signInProviders: ['github'], github: { clientId: 'gh-id', clientSecret: 'gh-secret' },
        google: null, mail: null });
      vi.unstubAllEnvs();
      stub({ ...BARE, SIGN_IN_PROVIDERS: 'google', GOOGLE_CLIENT_ID: 'g-id', GOOGLE_CLIENT_SECRET: 'g-secret' });
      expect(accountsSettings()).toMatchObject({ github: null, google: { clientId: 'g-id', clientSecret: 'g-secret' }, mail: null });
      vi.unstubAllEnvs();
      stub({ ...BARE, SIGN_IN_PROVIDERS: 'email', MAIL_TRANSPORT: 'sendgrid', MAIL_FROM: 'Jeff <no-reply@jeffhub.ai>', SENDGRID_API_KEY: 'SG.test' });
      expect(accountsSettings()).toMatchObject({ github: null, google: null,
        mail: { transport: 'sendgrid', apiKey: 'SG.test', from: 'Jeff <no-reply@jeffhub.ai>' } });
    });

    it('github,email (the owner\'s choice for now) needs no Google settings', () => {
      stub({ ...VALID, SIGN_IN_PROVIDERS: 'github,email', GOOGLE_CLIENT_ID: '', GOOGLE_CLIENT_SECRET: '' });
      expect(accountsSettings()).toMatchObject({ signInProviders: ['github', 'email'], google: null });
    });

    it.each([
      ['github', 'GITHUB_CLIENT_ID', 'the setting GITHUB_CLIENT_ID is not set (or empty): SIGN_IN_PROVIDERS lists github, which needs it'],
      ['github', 'GITHUB_CLIENT_SECRET', 'the setting GITHUB_CLIENT_SECRET is not set (or empty): SIGN_IN_PROVIDERS lists github'],
      ['google', 'GOOGLE_CLIENT_ID', 'the setting GOOGLE_CLIENT_ID is not set (or empty): SIGN_IN_PROVIDERS lists google'],
      ['google', 'GOOGLE_CLIENT_SECRET', 'the setting GOOGLE_CLIENT_SECRET is not set (or empty): SIGN_IN_PROVIDERS lists google'],
      ['email', 'MAIL_TRANSPORT', 'the setting MAIL_TRANSPORT is not set (or empty): SIGN_IN_PROVIDERS lists email'],
      ['email', 'MAIL_FROM', 'the setting MAIL_FROM is not set (or empty): SIGN_IN_PROVIDERS lists email'],
      ['email', 'SENDGRID_API_KEY', 'the setting SENDGRID_API_KEY is not set (or empty): SIGN_IN_PROVIDERS lists email and MAIL_TRANSPORT is sendgrid'],
    ])('a listed %s with %s missing fails, naming the setting', (provider, name, message) => {
      stub({ ...VALID, SIGN_IN_PROVIDERS: provider, [name]: '' });
      expect(() => accountsSettings()).toThrow(message);
    });

    it('with both GitHub settings missing, names both at once', () => {
      stub({ ...VALID, SIGN_IN_PROVIDERS: 'github,email', GITHUB_CLIENT_ID: '', GITHUB_CLIENT_SECRET: '' });
      expect(() => accountsSettings()).toThrow('the settings GITHUB_CLIENT_ID, GITHUB_CLIENT_SECRET are not set (or empty): SIGN_IN_PROVIDERS lists github, which needs them');
    });

    it('an unlisted method\'s settings are not read, even when they are wrong', () => {
      stub({ ...BARE, SIGN_IN_PROVIDERS: 'github', GITHUB_CLIENT_ID: 'gh-id', GITHUB_CLIENT_SECRET: 'gh-secret', MAIL_TRANSPORT: 'smtp' });
      expect(accountsSettings().mail).toBeNull();
    });
  });

  it('refuses test-only values in production', () => {
    const changes: Record<string, string>[] = [{ MAIL_TRANSPORT: 'test-mailbox', MAIL_TEST_DIR: '/tmp/mail' },
      { AUTH_TEST_PROVIDER: 'http://localhost:9400' }, { DATABASE_URL: 'pglite:memory' }];
    for (const change of changes) {
      vi.unstubAllEnvs();
      stub({ ...VALID, ...change, VERCEL_ENV: 'production' });
      expect(() => accountsSettings()).toThrow(/production/);
    }
  });

  it.each(['BETTER_AUTH_URL', 'PLAYGROUND_API_URL'])('accepts http:// for %s only on a loopback host, outside production', (name) => {
    for (const address of ['http://127.0.0.1:9400', 'http://localhost:3000', 'http://[::1]:3000', 'http://localhost']) {
      vi.unstubAllEnvs();
      stub({ ...VALID, [name]: address });
      const settings = accountsSettings();
      expect(name === 'BETTER_AUTH_URL' ? settings.baseUrl : settings.playground.apiUrl).toBe(address);
    }
    for (const address of ['http://jeffhub.ai', 'http://localhost.example.com', 'http://localhost@example.com',
      'http://127.0.0.2:9400', 'http://127.0.0.1:9400/', 'ftp://localhost', 'localhost:3000']) {
      vi.unstubAllEnvs();
      stub({ ...VALID, [name]: address });
      expect(() => accountsSettings(), address).toThrow(`${name}=${JSON.stringify(address)} must be an https:// address`);
    }
  });

  it.each(['BETTER_AUTH_URL', 'PLAYGROUND_API_URL'])('refuses http:// for %s in production, even on a loopback host', (name) => {
    const PRODUCTION = { ...VALID, DATABASE_URL: 'postgres://user@db.example.com/hub', VERCEL_ENV: 'production' };
    for (const address of ['http://127.0.0.1:9400', 'http://localhost:3000', 'http://[::1]:3000']) {
      vi.unstubAllEnvs();
      stub({ ...PRODUCTION, [name]: address });
      expect(() => accountsSettings(), address).toThrow(`${name}=${JSON.stringify(address)}: http:// is refused in production`);
    }
    vi.unstubAllEnvs();
    stub(PRODUCTION);
    expect(accountsSettings().baseUrl).toBe('https://jeffhub.ai');
  });

  it.each(['BETTER_AUTH_SECRETS', 'AUTH_SECRET'])('refuses to start when %s is set, naming it', (name) => {
    stub({ ...VALID, [name]: 'y'.repeat(40) });
    expect(() => accountsSettings()).toThrow(`${name} is set; remove it: the hub's only signing secret is BETTER_AUTH_SECRET`);
  });

  it('refuses a BETTER_AUTH_SECRET shorter than 32 characters, naming it', () => {
    stub({ ...VALID, BETTER_AUTH_SECRET: 'x'.repeat(31) });
    expect(() => accountsSettings()).toThrow('BETTER_AUTH_SECRET must be at least 32 characters');
  });

  it('refuses a PLAYGROUND_PASS_SECRET shorter than 32 characters, naming it', () => {
    stub({ ...VALID, PLAYGROUND_PASS_SECRET: 'x'.repeat(31) });
    expect(() => accountsSettings()).toThrow('PLAYGROUND_PASS_SECRET must be at least 32 characters');
  });

  it("in production, refuses any TURNSTILE_VERIFY_URL but Cloudflare's own", () => {
    const PRODUCTION = { ...VALID, DATABASE_URL: 'postgres://user@db.example.com/hub', VERCEL_ENV: 'production' };
    stub({ ...PRODUCTION, TURNSTILE_VERIFY_URL: 'https://turnstile.example/siteverify' });
    expect(() => accountsSettings()).toThrow('TURNSTILE_VERIFY_URL must be https://challenges.cloudflare.com/turnstile/v0/siteverify in production');
    vi.unstubAllEnvs();
    stub(PRODUCTION);
    expect(accountsSettings().turnstile.verifyUrl).toBe('https://challenges.cloudflare.com/turnstile/v0/siteverify');
    vi.unstubAllEnvs();
    stub({ ...VALID, TURNSTILE_VERIFY_URL: 'http://127.0.0.1:5194/siteverify' });
    expect(accountsSettings().turnstile.verifyUrl).toBe('http://127.0.0.1:5194/siteverify');
  });

  it('formats the plan for pages', () => {
    stub(VALID);
    expect(publicSettings()).toEqual({ priceUsd: 1, allowanceInputTokens: 50_000_000, turnstileSiteKey: 'site-key',
      playgroundRunsPerMinute: 5, automaticTax: false, signInProviders: ['github', 'google', 'email'] });
    expect([priceText(1), priceText(1.5), tokensText(50_000_000), tokensText(1_234_567)])
      .toEqual(['$1', '$1.50', '50 million', '1,234,567']);
  });

  it('requireEnv treats empty as missing', () => {
    vi.stubEnv('A_SET', 'x');
    vi.stubEnv('A_EMPTY', '');
    expect(() => requireEnv(['A_SET', 'A_EMPTY', 'A_UNSET_ONE'])).toThrow('A_EMPTY, A_UNSET_ONE');
  });
});

describe('the new adapter request email', () => {
  it('goes to ADAPTER_REQUEST_NOTIFY_EMAIL, a setting of its own beside OWNER_EMAIL', () => {
    stub({ ...VALID, ADAPTER_REQUEST_NOTIFY_EMAIL: 'mstrasser@scissero.com' });
    expect(requestNotifyEmail()).toBe('mstrasser@scissero.com');
    expect(accountsSettings().ownerEmail).toBe('owner@example.com');
    expect(REQUEST_MAIL_SETTING_NAMES).toContain('ADAPTER_REQUEST_NOTIFY_EMAIL');
  });

  it('is required: a missing one is an error naming it', () => {
    stub({ ...VALID, ADAPTER_REQUEST_NOTIFY_EMAIL: '' });
    expect(() => requestNotifyEmail()).toThrow('ADAPTER_REQUEST_NOTIFY_EMAIL');
    expect(() => requireEnv(REQUEST_MAIL_SETTING_NAMES)).toThrow('ADAPTER_REQUEST_NOTIFY_EMAIL');
  });

  it('must be one address', () => {
    stub({ ...VALID, ADAPTER_REQUEST_NOTIFY_EMAIL: 'Owner <owner@example.com>' });
    expect(() => requestNotifyEmail()).toThrow('ADAPTER_REQUEST_NOTIFY_EMAIL must be one email address');
  });

  it("names the address its mail settings are needed for", () => {
    stub({ ...VALID, SENDGRID_API_KEY: '' });
    expect(() => requestMailSettings()).toThrow('adapter requests are emailed to ADAPTER_REQUEST_NOTIFY_EMAIL, and MAIL_TRANSPORT is sendgrid');
  });
});
