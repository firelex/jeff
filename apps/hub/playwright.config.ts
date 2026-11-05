import path from 'node:path';
import { defineConfig } from '@playwright/test';

// Browser checks (e2e/*.spec.ts), separate from the unit tests (`pnpm test`, vitest, test/**): run with
// `pnpm --filter jeff-hub e2e`, or after every unit test by `pnpm --filter jeff-hub test:all` (before a merge). They
// run against the static build that ships, not the dev server: Playwright builds the site (with a placeholder API
// origin, since these checks do not call the API) and serves build/client and the hub's
// function (api/index.ts) the way vercel.json tells Vercel to (scripts/serve-local.ts), then stops the server
// afterwards. The function's version is this checkout's commit (scripts/hub-version.mjs, run by the build). Every page a check opens must load with no console error and no uncaught error
// (e2e/fixtures.ts).
//
// Sign-in runs with every outside service replaced: a fresh PGlite folder for Postgres, a folder of JSON files for
// SendGrid (the test mailbox), a local answer for Cloudflare Turnstile's check (e2e/global-setup.ts; the widget's
// script is answered in the browser, e2e/accounts.ts), and a local test OpenID provider (oauth2-mock-server, on
// AUTH_TEST_PROVIDER) that stands in for GitHub and Google, whose linking rule is the same (e2e/signin-provider.spec.ts).
// GitHub is never reached, and Google is not offered (SIGN_IN_PROVIDERS=github,email, the owner's choice for now). The playground calls a
// stand-in Jeff API on 127.0.0.1 (e2e/global-setup.ts).
const PORT = 5195;
export const TURNSTILE_PORT = 5194;
// The stand-in Jeff API the playground calls (e2e/global-setup.ts).
export const JEFF_API_PORT = 5193;
// The test OpenID provider (e2e/global-setup.ts), and the file that says whom it signs in next (e2e/accounts.ts:
// providerIdentity). The file is outside STATE_DIR, which the web server's command empties.
export const OIDC_PORT = 5192;
export const OIDC_IDENTITY_FILE = path.join(import.meta.dirname, '.e2e', 'oidc', 'identity.json');
export const PLAYGROUND_API_KEY = `jeff_live_${'c'.repeat(52)}`;
export const STATE_DIR = path.join(import.meta.dirname, '.e2e', 'site');

export default defineConfig({
  testDir: './e2e',
  globalSetup: './e2e/global-setup.ts',
  workers: 1,
  retries: 0,
  reporter: 'list',
  timeout: 60_000,
  use: { baseURL: `http://127.0.0.1:${PORT}` },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
  webServer: {
    command: `rm -rf ${STATE_DIR} && mkdir -p ${STATE_DIR} && node scripts/e2e-migrate.mjs ${STATE_DIR}/db && node scripts/e2e-seed.mjs ${STATE_DIR}/db && pnpm build && pnpm exec tsx scripts/serve-local.ts ${PORT}`,
    url: `http://127.0.0.1:${PORT}/`,
    reuseExistingServer: false,
    timeout: 240_000,
    env: {
      JEFF_HUB_API_ORIGIN: 'https://api.example.com',
      PLAN_PRICE_USD: '1', PLAN_ALLOWANCE_INPUT_TOKENS: '50000000', TURNSTILE_SITE_KEY: 'e2e-site-key',
      PLAYGROUND_RUNS_PER_MINUTE: '5', SIGN_IN_PROVIDERS: 'github,email', DATABASE_URL: `pglite:${STATE_DIR}/db`,
      BETTER_AUTH_SECRET: 'e2e-secret-that-is-only-for-these-tests', BETTER_AUTH_URL: `http://127.0.0.1:${PORT}`,
      GITHUB_CLIENT_ID: 'e2e', GITHUB_CLIENT_SECRET: 'e2e',
      MAIL_TRANSPORT: 'test-mailbox', MAIL_TEST_DIR: `${STATE_DIR}/mail`, MAIL_FROM: 'Jeff <no-reply@jeffhub.ai>',
      TURNSTILE_SECRET_KEY: 'e2e', TURNSTILE_VERIFY_URL: `http://127.0.0.1:${TURNSTILE_PORT}/siteverify`,
      STRIPE_SECRET_KEY: 'sk_test_e2e_unused', STRIPE_WEBHOOK_SECRET: 'whsec_e2e', STRIPE_PRICE_ID: 'price_e2e',
      STRIPE_AUTOMATIC_TAX: 'off', JEFF_HUB_INTERNAL_TOKEN: 'e2e', PLAYGROUND_API_KEY,
      PLAYGROUND_API_URL: `http://127.0.0.1:${JEFF_API_PORT}`, PLAYGROUND_RUNS_PER_SECOND: '5', PLAYGROUND_PASS_SECRET: 'e2e-pass-secret-that-is-only-for-these-tests',
      OWNER_EMAIL: 'owner@example.com', AUTH_TEST_PROVIDER: `http://127.0.0.1:${OIDC_PORT}`,
      ADAPTER_REQUEST_NOTIFY_EMAIL: 'notify@example.com', CRON_SECRET: 'e2e-cron-secret-only-for-these-tests',
    },
  },
});
