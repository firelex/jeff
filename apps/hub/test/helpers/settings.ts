import type { AccountsSettings } from '../../app/lib/accounts-settings.server';

// Settings for unit tests: test-only values, nothing real.
export function testSettings(changes: Partial<AccountsSettings> = {}): AccountsSettings {
  return {
    databaseUrl: 'pglite:memory',
    authSecret: 'a-test-secret-that-is-at-least-32-characters',
    baseUrl: 'https://jeffhub.ai',
    signInProviders: ['github', 'google', 'email'],
    github: { clientId: 'gh-test', clientSecret: 'gh-test-secret' },
    google: { clientId: 'g-test', clientSecret: 'g-test-secret' },
    mail: { transport: 'test-mailbox', dir: '/unused', from: 'Jeff <no-reply@jeffhub.ai>' },
    turnstile: { siteKey: 'site-test', secretKey: 'turnstile-test', verifyUrl: 'https://turnstile.test/siteverify' },
    stripe: { secretKey: 'sk_test_unused', webhookSecret: 'whsec_test', priceId: 'price_test', automaticTax: false },
    internalToken: 'internal-test-token',
    playground: { apiKey: `jeff_live_${'c'.repeat(52)}`, apiUrl: 'https://api.test', runsPerMinute: 5, runsPerSecond: 5,
      passSecret: 'a-test-pass-secret-of-at-least-32-characters' },
    ownerEmail: 'owner@example.com',
    testOAuthIssuer: null,
    plan: { priceUsd: 1, allowanceInputTokens: 50_000_000 },
    ...changes,
  };
}

// The same test-only values as environment variables, for code that reads the settings itself (accountsSettings(),
// getDb()).
export const TEST_ENV: Record<string, string> = {
  PLAN_PRICE_USD: '1', PLAN_ALLOWANCE_INPUT_TOKENS: '50000000', TURNSTILE_SITE_KEY: 'site-test',
  PLAYGROUND_RUNS_PER_MINUTE: '5', SIGN_IN_PROVIDERS: 'github,google,email', DATABASE_URL: 'pglite:memory', BETTER_AUTH_SECRET: 'a-test-secret-that-is-at-least-32-characters',
  BETTER_AUTH_URL: 'https://jeffhub.ai', GITHUB_CLIENT_ID: 'gh-test', GITHUB_CLIENT_SECRET: 'gh-test-secret',
  GOOGLE_CLIENT_ID: 'g-test', GOOGLE_CLIENT_SECRET: 'g-test-secret', MAIL_TRANSPORT: 'test-mailbox', MAIL_TEST_DIR: '/unused',
  MAIL_FROM: 'Jeff <no-reply@jeffhub.ai>', TURNSTILE_SECRET_KEY: 'turnstile-test',
  TURNSTILE_VERIFY_URL: 'https://turnstile.test/siteverify', STRIPE_SECRET_KEY: 'sk_test_unused',
  STRIPE_WEBHOOK_SECRET: 'whsec_test', STRIPE_PRICE_ID: 'price_test', STRIPE_AUTOMATIC_TAX: 'off',
  JEFF_HUB_INTERNAL_TOKEN: 'internal-test-token', PLAYGROUND_API_KEY: `jeff_live_${'c'.repeat(52)}`,
  PLAYGROUND_API_URL: 'https://api.test', PLAYGROUND_RUNS_PER_SECOND: '5', PLAYGROUND_PASS_SECRET: 'a-test-pass-secret-of-at-least-32-characters',
  OWNER_EMAIL: 'owner@example.com', AUTH_TEST_PROVIDER: 'off', JEFF_HUB_API_ORIGIN: 'https://api.test',
  ADAPTER_REQUEST_NOTIFY_EMAIL: 'notify@example.com', CRON_SECRET: 'a-test-cron-secret-of-32-characters',
  BLOB_READ_WRITE_TOKEN: 'vercel_blob_rw_teststore_notarealsecret',
};
