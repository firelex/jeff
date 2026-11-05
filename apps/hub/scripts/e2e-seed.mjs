#!/usr/bin/env node
// Adds the e2e run's paying account to a migrated PGlite folder: node scripts/e2e-seed.mjs <folder>. Tests only.
// The browser checks cannot pay (Stripe is never reached), so the one account that sees the paying view of /account
// is written here before the hub's function opens the folder: a user (subscriber@example.com, who signs in with an
// email link as any visitor does), its Stripe customer, an active subscription whose period runs around today, and
// that period's usage.
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { PGlite } from '@electric-sql/pglite';

const SUBSCRIBER = { id: 'e2e-subscriber', email: 'subscriber@example.com', usedInputTokens: 12_345_678 };
const DAY = 24 * 60 * 60 * 1000;

// The only folder it writes to: the e2e run's own database (playwright.config.ts: STATE_DIR/db), never another one.
const E2E_DB = path.join(path.dirname(fileURLToPath(import.meta.url)), '..', '.e2e', 'site', 'db');

const folder = process.argv[2];
if (folder === undefined) throw new Error('usage: node scripts/e2e-seed.mjs <folder>');
if (path.resolve(folder) !== path.resolve(E2E_DB)) {
  throw new Error(`e2e-seed writes only to the e2e run's database ${path.resolve(E2E_DB)}; refusing ${path.resolve(folder)}`);
}
const now = new Date();
const start = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate()) - 9 * DAY);
const end = new Date(start.getTime() + 30 * DAY);
const client = new PGlite(path.resolve(folder));
await client.transaction(async (tx) => {
  await tx.query('insert into "user" (id, name, email, email_verified, created_at, updated_at) values ($1, $2, $3, true, $4, $4)',
    [SUBSCRIBER.id, 'Subscriber', SUBSCRIBER.email, now]);
  await tx.query('insert into stripe_customer (user_id, customer_id, created_at) values ($1, $2, $3)', [SUBSCRIBER.id, 'cus_e2e', now]);
  await tx.query(`insert into subscription (user_id, stripe_customer_id, stripe_subscription_id, status, current_period_start,
    current_period_end, cancel_at_period_end, updated_at) values ($1, 'cus_e2e', 'sub_e2e', 'active', $2, $3, false, $4)`,
  [SUBSCRIBER.id, start, end, now]);
  await tx.query('insert into usage (account_id, period_start, input_tokens, updated_at) values ($1, $2, $3, $4)',
    [SUBSCRIBER.id, start, SUBSCRIBER.usedInputTokens, now]);
});
await client.close();
console.log(`e2e-seed: ${folder} has the paying account ${SUBSCRIBER.email}`);
