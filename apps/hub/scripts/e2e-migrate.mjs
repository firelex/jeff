#!/usr/bin/env node
// Applies drizzle/ to a PGlite folder: node scripts/e2e-migrate.mjs <folder>. Tests only.
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PGlite } from '@electric-sql/pglite';
import { drizzle } from 'drizzle-orm/pglite';
import { migrate } from 'drizzle-orm/pglite/migrator';

const folder = process.argv[2];
if (folder === undefined) throw new Error('usage: node scripts/e2e-migrate.mjs <folder>');
const client = new PGlite(path.resolve(folder));
await migrate(drizzle(client), { migrationsFolder: path.join(path.dirname(fileURLToPath(import.meta.url)), '../drizzle') });
await client.close();
console.log(`e2e-migrate: ${folder} is migrated`);
