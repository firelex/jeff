import { defineConfig } from '@playwright/test';

// The browser smoke test (e2e/smoke.spec.ts), separate from the unit tests (`pnpm test`, vitest, src/**/*.test.ts*):
// run with `pnpm --filter jeff-console e2e`. e2e/global-setup.ts starts a real Jeff server (apps/server/tests/
// console_server.py) on its own free port before any test runs, and stops it afterwards; the server's URL and the
// Playwright accounts' credentials reach the tests as environment variables that global setup sets.
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: 'list',
  timeout: 30_000,
  globalSetup: './e2e/global-setup.ts',
  use: {
    viewport: { width: 1440, height: 900 },
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
