// @vitest-environment node
// The production function never carries PGlite (Task 9's ruling b): app/lib/db.server.ts imports it only through a
// loader Vercel's file tracer cannot follow, the check check-build.mjs runs over the function's sources catches every
// other way of importing it, and the loader still works where PGlite is installed (tests and local runs).
import fs from 'node:fs';
import path from 'node:path';
import { sql } from 'drizzle-orm';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createDb, getDb } from '../app/lib/db.server';
import { testOnlyImports, extensionlessImports } from '../scripts/function-imports';
import { TEST_ENV } from './helpers/settings';

afterEach(() => vi.unstubAllEnvs());

const DB_MODULE = path.join(import.meta.dirname, '../app/lib/db.server.ts');

describe('the function imports PGlite in no way Vercel would bundle', () => {
  it('app/lib/db.server.ts has no such import', () => {
    expect(testOnlyImports('app/lib/db.server.ts', fs.readFileSync(DB_MODULE, 'utf8'))).toEqual([]);
  });

  it('the check finds every form that would be bundled, by line', () => {
    const text = [
      "import { PGlite } from '@electric-sql/pglite';",
      "import { drizzle } from 'drizzle-orm/pglite';",
      "export { migrate } from 'drizzle-orm/pglite/migrator';",
      "const later = await import('@electric-sql/pglite/vector');",
      "const old = require('@electric-sql/pglite');",
      "import type { PGlite as T } from '@electric-sql/pglite';",
      "type U = typeof import('@electric-sql/pglite');",
      "import pg from 'pg';",
      "import { drizzle as d } from 'drizzle-orm/node-postgres';",
    ].join('\n');
    expect(testOnlyImports('x.ts', text)).toEqual([
      'x.ts:1 imports @electric-sql/pglite',
      'x.ts:2 imports drizzle-orm/pglite',
      'x.ts:3 imports drizzle-orm/pglite/migrator',
      'x.ts:4 imports @electric-sql/pglite/vector',
      'x.ts:5 imports @electric-sql/pglite',
    ]);
  });
});

describe('createDb and getDb', () => {
  it('open a PGlite database through the loader, asynchronously', async () => {
    const made = createDb('pglite:memory');
    expect(made).toBeInstanceOf(Promise);
    const db = await made;
    expect(await db.execute(sql`select 1 as one`)).toMatchObject({ rows: [{ one: 1 }] });
  });

  it('refuse an address that is neither postgres:// nor pglite:, naming the setting', async () => {
    await expect(createDb('mysql://db.example.com/hub')).rejects.toThrow('DATABASE_URL must be postgres://');
    await expect(createDb('pglite:relative/folder')).rejects.toThrow('DATABASE_URL must be postgres://');
  });

  it('getDb makes one database from DATABASE_URL and shares it', async () => {
    for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value);
    const first = getDb();
    expect(getDb()).toBe(first);
    expect(await (await first).execute(sql`select 2 as two`)).toMatchObject({ rows: [{ two: 2 }] });
  });
});

describe('extensionlessImports', () => {
  it('names a relative import or export with no file ending, and passes .js, packages and type-only imports', () => {
    const text = [
      "import { a } from './schema';",
      "import type { B } from './types';",
      "import { c } from './c.js';",
      "import { eq } from 'drizzle-orm';",
      "export { d } from '../d';",
      "const e = await import('./e');",
    ].join('\n');
    expect(extensionlessImports('x.ts', text)).toEqual(['x.ts:1 imports ./schema', 'x.ts:5 imports ../d', 'x.ts:6 imports ./e']);
  });
});
