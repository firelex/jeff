import path from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import type { Logger } from 'drizzle-orm';
import { drizzle } from 'drizzle-orm/pglite';
import { migrate } from 'drizzle-orm/pglite/migrator';
import type { Db } from '../../app/lib/db.server';
import * as schema from '../../app/lib/schema';

// An empty database in memory with the real migrations of drizzle/ applied: what production runs, in a test.
// `onQuery`, when given, hears every statement run after the migrations (tests that check the order of operations).
export async function testDb(onQuery?: (query: string) => void): Promise<Db> {
  let listening = false;
  const logger: Logger = { logQuery: (query) => { if (listening && onQuery !== undefined) onQuery(query); } };
  const db = drizzle(new PGlite(), { schema, logger });
  await migrate(db, { migrationsFolder: path.join(import.meta.dirname, '../../drizzle') });
  listening = true;
  return db as unknown as Db;
}
