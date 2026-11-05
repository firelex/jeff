// @vitest-environment node
// The /account page's data (app/lib/account-page.server.ts, answered by GET /api/account): the subscribe screen, or
// the subscription, this period's usage against the allowance and the live keys; the playground's month for the owner.
import { expect, it } from 'vitest';
import { accountPageData } from '../app/lib/account-page.server';
import { createKey, revokeKey } from '../app/lib/keys.server';
import { usage } from '../app/lib/schema';
import { usageOf } from '../app/lib/usage.server';
import { aSubscriber, aUser, NOW, PERIOD } from './helpers/accounts';
import { testDb } from './helpers/db';
import { testSettings } from './helpers/settings';

const userOf = (id: string, email = `${id}@example.com`) => ({ id, email, emailVerified: true, name: id, image: null });

it('an account that is not paying sees the one subscribe screen', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  expect(await accountPageData(db, testSettings(), userOf('u1'), NOW)).toEqual(
    { view: 'subscribe', email: 'u1@example.com', price: '$1', allowance: '50 million', status: null, playground: null });
});

it('an account whose subscription may not use keys sees the subscribe screen with its last status', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'canceled');
  expect(await accountPageData(db, testSettings(), userOf('u1'), NOW)).toMatchObject({ view: 'subscribe', status: 'canceled' });
});

it('a paying account sees its period, usage of the allowance and its keys', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  await createKey(db, 'u1', 'laptop', NOW);
  const { view: gone } = await createKey(db, 'u1', 'old', NOW);
  await revokeKey(db, 'u1', gone.id, NOW);
  await db.insert(usage).values({ accountId: 'u1', periodStart: PERIOD.start, inputTokens: 1234, updatedAt: NOW });
  // Another period's total is not this period's.
  await db.insert(usage).values({ accountId: 'u1', periodStart: new Date('2026-10-03T10:00:00Z'), inputTokens: 999, updatedAt: NOW });
  const page = await accountPageData(db, testSettings(), userOf('u1'), NOW);
  expect(page).toMatchObject({ view: 'account', status: 'active', periodEnd: '2026-12-03T10:00:00.000Z',
    usage: { used: 1234, allowance: 50_000_000 }, playground: null });
  if (page.view !== 'account') throw new Error('expected the account view');
  expect(page.keys).toHaveLength(1);
  expect(page.keys[0]).toMatchObject({ name: 'laptop', createdAt: '2026-11-03T10:00:00.000Z', lastUsedAt: null });
  expect(page.keys[0].shown).toMatch(/^jeff_live_[a-z2-7]{6}…$/);
});

it('usage of a period not yet reported is 0', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  const page = await accountPageData(db, testSettings(), userOf('u1'), NOW);
  expect(page.view === 'account' && page.usage.used).toBe(0);
  expect(await usageOf(db, 'u1', PERIOD.start)).toBe(0);
});

it("the owner also sees the playground's usage this month", async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  await db.insert(usage).values({ accountId: 'playground', periodStart: new Date('2026-11-01T00:00:00Z'), inputTokens: 77, updatedAt: NOW });
  const page = await accountPageData(db, testSettings({ ownerEmail: 'owner@example.com' }), userOf('u1', 'Owner@Example.com'), NOW);
  expect(page.view === 'account' && page.playground).toEqual({ used: 77, since: '2026-11-01T00:00:00.000Z' });
});

it("the owner sees the playground's month on the subscribe screen too", async () => {
  const db = await testDb();
  await aUser(db, 'u1', 'owner@example.com');
  await db.insert(usage).values({ accountId: 'playground', periodStart: new Date('2026-11-01T00:00:00Z'), inputTokens: 5, updatedAt: NOW });
  expect(await accountPageData(db, testSettings(), userOf('u1', 'owner@example.com'), NOW))
    .toMatchObject({ view: 'subscribe', playground: { used: 5, since: '2026-11-01T00:00:00.000Z' } });
});
