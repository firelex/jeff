import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// Two projects (owner, 2026-11-04: the everyday run must not spike the laptop's CPU). `light` is every test file but
// the heavy ones below and is what `pnpm test` runs; `heavy` is those files, run by `pnpm test:heavy`, and
// `pnpm test:all` runs both projects and then the browser checks (e2e) -- what must pass before a merge. Each heavy
// file was among the slowest of a timed run with one worker (2026-11-04, test time per file): a Postgres database in
// memory (PGlite) per test, child processes, a TypeScript program over the function's imports, or every page of the
// site rendered.
export const HEAVY = [
  'test/auth.test.ts', //             138.4 s  PGlite: Better Auth end to end
  'test/stripe-webhook.test.ts', //    19.6 s  PGlite
  'test/account-endpoints.test.ts', // 13.2 s  PGlite
  'test/adapter-request-endpoints.test.ts', // 7.1 s  PGlite
  'test/examples-retention.test.ts', //  PGlite: a database per test, and the migrations run twice
  'test/internal-api.test.ts', //       7.9 s  PGlite
  'test/billing.test.ts', //            7.6 s  PGlite
  'test/playground.test.ts', //         7.1 s  PGlite
  'test/keys.test.ts', //               6.1 s  PGlite
  'test/sdk-coverage.test.ts', //       3.3 s  child processes: uv (Python), the Node SDK's build, tsc
  'test/playground-page.test.tsx', //   3.2 s  every adapter page and the playground rendered
  'test/account-page.test.ts', //       2.6 s  PGlite
  'test/email-limit.test.ts', //        1.8 s  PGlite
  'test/schema.test.ts', //             1.3 s  PGlite: the migrations
  'test/function-imports.test.ts', //   1.0 s  a TypeScript program over the function's import graph
  'test/sdk-languages.test.tsx', //     0.9 s  every generated page rendered with each language setting
];

// A heavy file renamed or deleted must not silently leave the heavy project matching nothing.
const here = path.dirname(fileURLToPath(import.meta.url));
const missing = HEAVY.filter((file) => !fs.existsSync(path.join(here, file)));
if (missing.length > 0) throw new Error(`vitest.config.ts lists heavy test files that do not exist: ${missing.join(', ')}`);

const ALL = ['test/**/*.test.ts', 'test/**/*.test.tsx'];

export default defineConfig({
  test: {
    environment: 'jsdom',
    setupFiles: ['./test/setup.ts'],
    projects: [
      { extends: true, test: { name: 'light', include: ALL, exclude: HEAVY } },
      { extends: true, test: { name: 'heavy', include: HEAVY } },
    ],
  },
});
