import { createHash } from 'node:crypto';
import { expect, it } from 'vitest';
import { apiKey } from '../app/lib/schema';
import { base32, createKey, hashKey, KeyRefused, liveKeys, MAX_LIVE_KEYS, revokeKey } from '../app/lib/keys.server';
import { testDb } from './helpers/db';
import { aSubscriber, aUser, NOW } from './helpers/accounts';

it('encodes base32 as RFC 4648 does, in lower case and without padding', () => {
  const bytes = (text: string) => new TextEncoder().encode(text);
  expect(base32(bytes('f'))).toBe('my');
  expect(base32(bytes('foobar'))).toBe('mzxw6ytboi');
  expect(base32(new Uint8Array(32)).length).toBe(52);
});

it('shows a new key once and stores only its hash, name and prefix', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  const { key, view } = await createKey(db, 'u1', '  laptop  ', NOW);
  expect(key).toMatch(/^jeff_live_[a-z2-7]{52}$/);
  expect(view).toMatchObject({ name: 'laptop', prefix: key.slice(10, 16), lastUsedAt: null });
  const [row] = await db.select().from(apiKey);
  expect(row.hash).toBe(createHash('sha256').update(key).digest('hex'));
  expect(row.hash).toBe(hashKey(key));
  expect(JSON.stringify(row)).not.toContain(key.slice(10));
  expect(JSON.stringify(await liveKeys(db, 'u1'))).not.toContain(key.slice(16));
});

it.each([['', 'name of 1 to 60'], ['x'.repeat(61), 'name of 1 to 60']])('refuses the name %j', async (name, message) => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  await expect(createKey(db, 'u1', name, NOW)).rejects.toThrow(message);
});

it.each([['active', true], ['trialing', true], ['past_due', true], ['canceled', false], ['unpaid', false],
  ['incomplete', false], ['incomplete_expired', false], ['paused', false]])(
  'with a subscription %s a key can be made: %s', async (status, allowed) => {
    const db = await testDb();
    await aSubscriber(db, 'u1', status);
    const made = createKey(db, 'u1', 'k', NOW);
    if (allowed) await expect(made).resolves.toBeTruthy();
    else await expect(made).rejects.toThrow(/only while your subscription is active/);
  });

it('refuses a key without any subscription', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  await expect(createKey(db, 'u1', 'k', NOW)).rejects.toThrow(KeyRefused);
});

it('holds at most 10 live keys; revoking one frees its place', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  const made = [];
  for (let i = 0; i < MAX_LIVE_KEYS; i++) made.push(await createKey(db, 'u1', `k${i}`, NOW));
  await expect(createKey(db, 'u1', 'one too many', NOW)).rejects.toThrow('You have 10 live keys');
  await revokeKey(db, 'u1', made[0].view.id, NOW);
  await expect(createKey(db, 'u1', 'now it fits', NOW)).resolves.toBeTruthy();
  expect((await liveKeys(db, 'u1')).map((k) => k.name)).not.toContain('k0');
});

it('two keys made at the same moment never pass the limit', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  for (let i = 0; i < MAX_LIVE_KEYS - 1; i++) await createKey(db, 'u1', `k${i}`, NOW);
  const results = await Promise.allSettled([createKey(db, 'u1', 'a', NOW), createKey(db, 'u1', 'b', NOW)]);
  expect(results.filter((r) => r.status === 'fulfilled')).toHaveLength(1);
  expect(await liveKeys(db, 'u1')).toHaveLength(MAX_LIVE_KEYS);
});

it('a user can revoke only their own live keys', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  await aSubscriber(db, 'u2');
  const { view } = await createKey(db, 'u1', 'k', NOW);
  await expect(revokeKey(db, 'u2', view.id, NOW)).rejects.toThrow('No live key');
  await revokeKey(db, 'u1', view.id, NOW);
  await expect(revokeKey(db, 'u1', view.id, NOW)).rejects.toThrow('No live key');
});
