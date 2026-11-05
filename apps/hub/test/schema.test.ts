// @vitest-environment node
// The migrations in drizzle/ build the schema the code expects: they apply to an empty database, and the
// constraints the code relies on hold (unique key hashes, a user's rows going with the user).
import { eq } from 'drizzle-orm';
import { expect, it } from 'vitest';
import { apiKey, subscription, user } from '../app/lib/schema';
import { testDb } from './helpers/db';

const now = new Date('2026-11-03T10:00:00Z');

async function aUser(db: Awaited<ReturnType<typeof testDb>>, id: string) {
  await db.insert(user).values({ id, name: id, email: `${id}@example.com`, emailVerified: true, createdAt: now, updatedAt: now });
}

it('refuses two keys with one hash', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  const row = { userId: 'u1', name: 'k', hash: 'a'.repeat(64), prefix: 'abcdef', createdAt: now };
  await db.insert(apiKey).values({ id: 'k1', ...row });
  await expect(db.insert(apiKey).values({ id: 'k2', ...row })).rejects.toThrow();
});

it("deletes a user's keys and subscription with the user", async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  await db.insert(apiKey).values({ id: 'k1', userId: 'u1', name: 'k', hash: 'b'.repeat(64), prefix: 'abcdef', createdAt: now });
  await db.insert(subscription).values({ userId: 'u1', stripeCustomerId: 'cus_1', stripeSubscriptionId: 'sub_1',
    status: 'active', currentPeriodStart: now, currentPeriodEnd: now, cancelAtPeriodEnd: false, updatedAt: now });
  await db.delete(user).where(eq(user.id, 'u1'));
  expect(await db.select().from(apiKey)).toEqual([]);
  expect(await db.select().from(subscription)).toEqual([]);
});

it('refuses a key whose hash or prefix is not in the key form', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  const row = { userId: 'u1', name: 'k', hash: 'c'.repeat(64), prefix: 'abcdef', createdAt: now };
  const cases = [['k1', { prefix: 'abcde' }], ['k2', { prefix: 'abcdefg' }], ['k3', { prefix: 'ABCDEF' }],
    ['k4', { prefix: 'abcde1' }], ['k5', { hash: 'c'.repeat(63) }], ['k6', { hash: 'C'.repeat(64) }]] as const;
  for (const [id, change] of cases) {
    const refusal = await db.insert(apiKey).values({ id, ...row, ...change }).then(() => null, (error: Error) => error);
    const constraint = 'prefix' in change ? 'api_key_prefix_form' : 'api_key_hash_form';
    expect((refusal?.cause as Error | undefined)?.message, id).toContain(`violates check constraint "${constraint}"`);
  }
  await db.insert(apiKey).values({ id: 'k7', ...row, prefix: 'abc234' });
  expect((await db.select().from(apiKey)).map((key) => key.id)).toEqual(['k7']);
});
