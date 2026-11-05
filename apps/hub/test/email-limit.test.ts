// @vitest-environment node
// The per-address limit on sign-in emails, on the real migration (PGlite).
import { expect, it } from 'vitest';
import { addressHash, claimSignInEmail } from '../app/lib/email-limit.server';
import { testDb } from './helpers/db';

const T0 = Date.parse('2026-11-03T10:00:00Z');
const MIN = 60_000;

it('allows 5 emails to one address in an hour, then refuses, and other addresses are unaffected', async () => {
  const db = await testDb();
  const results: boolean[] = [];
  for (let i = 0; i < 6; i += 1) results.push(await claimSignInEmail(db, 'ada@example.com', T0 + i * MIN));
  expect(results).toEqual([true, true, true, true, true, false]);
  expect(await claimSignInEmail(db, 'grace@example.com', T0 + 6 * MIN)).toBe(true);
  // The same address however it is written.
  expect(await claimSignInEmail(db, '  ADA@Example.com ', T0 + 7 * MIN)).toBe(false);
});

it('is a rolling hour: a slot frees an hour after the email that took it', async () => {
  const db = await testDb();
  for (let i = 0; i < 5; i += 1) await claimSignInEmail(db, 'ada@example.com', T0 + i * MIN);
  expect(await claimSignInEmail(db, 'ada@example.com', T0 + 60 * MIN - 1)).toBe(false);
  expect(await claimSignInEmail(db, 'ada@example.com', T0 + 60 * MIN + 1)).toBe(true);
  expect(await claimSignInEmail(db, 'ada@example.com', T0 + 60 * MIN + 2)).toBe(false);
});

it('keeps only a hash of the address, and deletes rows older than an hour', async () => {
  const db = await testDb();
  await claimSignInEmail(db, 'ada@example.com', T0);
  await claimSignInEmail(db, 'grace@example.com', T0 + 30 * MIN);
  const rows = await db.query.signInEmail.findMany();
  expect(JSON.stringify(rows)).not.toContain('example.com');
  expect(rows.map((r) => r.addressHash).sort()).toEqual([addressHash('ada@example.com'), addressHash('grace@example.com')].sort());
  await claimSignInEmail(db, 'linus@example.com', T0 + 61 * MIN);
  expect((await db.query.signInEmail.findMany()).map((r) => r.addressHash).sort())
    .toEqual([addressHash('grace@example.com'), addressHash('linus@example.com')].sort());
});

it('counts right when requests arrive at the same moment', async () => {
  const db = await testDb();
  const results = await Promise.all(Array.from({ length: 10 }, () => claimSignInEmail(db, 'ada@example.com', T0)));
  expect(results.filter(Boolean)).toHaveLength(5);
});
