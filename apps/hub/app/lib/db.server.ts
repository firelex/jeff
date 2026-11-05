import { drizzle as drizzlePostgres } from 'drizzle-orm/node-postgres';
import type { PgDatabase, PgQueryResultHKT } from 'drizzle-orm/pg-core';
import pg from 'pg';
import { databaseUrlSetting } from './accounts-settings.server.js';
import * as schema from './schema.js';

export type Db = PgDatabase<PgQueryResultHKT, typeof schema>;

export const POOL_CONNECTION_TIMEOUT_MS = 5000;

// postgres:// (Neon in production; use its pooled connection string) or, for tests and local runs only, PGlite:
// pglite:memory (in memory) or pglite:/absolute/folder. databaseUrlSetting() refuses pglite in production.
export async function createDb(url: string): Promise<Db> {
  if (/^postgres(ql)?:\/\//.test(url)) {
    // A connection that cannot be made in 5 seconds fails (pg would otherwise wait without end), so a request meets
    // an error it can answer in time (the playground's 503) instead of the platform's timeout.
    return drizzlePostgres(new pg.Pool({ connectionString: url, max: 5, connectionTimeoutMillis: POOL_CONNECTION_TIMEOUT_MS }), { schema }) as unknown as Db;
  }
  if (url === 'pglite:memory' || url.startsWith('pglite:/')) {
    const [{ PGlite }, { drizzle: drizzlePglite }] = await Promise.all([
      loadTestOnly('@electric-sql/pglite') as Promise<typeof import('@electric-sql/pglite')>,
      loadTestOnly('drizzle-orm/pglite') as Promise<typeof import('drizzle-orm/pglite')>,
    ]);
    const client = url === 'pglite:memory' ? new PGlite() : new PGlite(url.slice('pglite:'.length));
    return drizzlePglite(client, { schema }) as unknown as Db;
  }
  throw new Error(`DATABASE_URL must be postgres://..., pglite:memory or pglite:/absolute/folder; got ${url.split(':')[0]}:...`);
}

// PGlite is for tests and local runs only, so the production function must neither load it nor carry its files
// (WebAssembly and data, about 18 files). Vercel finds the files a function needs by tracing its imports (@vercel/nft),
// and it follows every import whose package name it can work out, a dynamic import('...') of a string included. A
// name passed in as an argument is one it cannot follow, so PGlite is loaded here and only here. Measured with
// @vercel/nft 1.11.0 (task-9-report.md, fix round 1): import('@electric-sql/pglite') in this file traced PGlite's 18
// files and its two .wasm files; this loader traced none. On a deployment without PGlite's files, a pglite:
// DATABASE_URL fails here, naming the missing package. scripts/function-imports.ts (run by check-build.mjs over the
// function's sources, and by test/function-imports.test.ts over this file) refuses any other import of PGlite.
function loadTestOnly(name: string): Promise<unknown> {
  return import(/* @vite-ignore */ name);
}

// One database per running function, made on first use. The promise is kept, so requests at the same moment share
// one pool.
let cached: Promise<Db> | null = null;

export function getDb(): Promise<Db> {
  if (cached === null) cached = createDb(databaseUrlSetting());
  return cached;
}
