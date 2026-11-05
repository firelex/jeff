import { createHash, randomBytes, randomUUID } from 'node:crypto';
import { and, count, desc, eq, isNull } from 'drizzle-orm';
import type { Db } from './db.server';
import { base32, KEY_PREFIX, MAX_LIVE_KEYS, MAX_NAME_LENGTH, VISIBLE_PREFIX_LENGTH } from './keys-format.js';
import { apiKey, subscription } from './schema.js';
import { isUsable } from './subscription.server.js';

export { base32, MAX_LIVE_KEYS, MAX_NAME_LENGTH };

export function newKey(): string {
  return KEY_PREFIX + base32(randomBytes(32));
}

// What the hub stores and jeff-server looks keys up by: SHA-256 of the whole key, lowercase hex.
export function hashKey(key: string): string {
  return createHash('sha256').update(key, 'utf8').digest('hex');
}

// A refusal the account page shows the customer as it is.
export class KeyRefused extends Error {}

export interface KeyView { id: string; name: string; prefix: string; createdAt: Date; lastUsedAt: Date | null }

function viewOf(row: typeof apiKey.$inferSelect): KeyView {
  return { id: row.id, name: row.name, prefix: row.prefix, createdAt: row.createdAt, lastUsedAt: row.lastUsedAt };
}

// The key text is returned here once and never stored. The subscription row is locked while the live keys are
// counted, so two keys made at once cannot pass the limit.
export async function createKey(db: Db, userId: string, rawName: string, now: Date): Promise<{ key: string; view: KeyView }> {
  const name = rawName.trim();
  if (name.length === 0 || name.length > MAX_NAME_LENGTH) {
    throw new KeyRefused(`Give the key a name of 1 to ${MAX_NAME_LENGTH} characters`);
  }
  return db.transaction(async (tx) => {
    const [paying] = await tx.select().from(subscription).where(eq(subscription.userId, userId)).for('update');
    if (paying === undefined || !isUsable(paying.status)) {
      throw new KeyRefused('Keys can be created only while your subscription is active; subscribe first');
    }
    const [{ live }] = await tx.select({ live: count() }).from(apiKey)
      .where(and(eq(apiKey.userId, userId), isNull(apiKey.revokedAt)));
    if (live >= MAX_LIVE_KEYS) {
      throw new KeyRefused(`You have ${MAX_LIVE_KEYS} live keys, the most an account may hold; revoke one first`);
    }
    const key = newKey();
    const row = { id: randomUUID(), userId, name, hash: hashKey(key), createdAt: now, revokedAt: null, lastUsedAt: null,
      prefix: key.slice(KEY_PREFIX.length, KEY_PREFIX.length + VISIBLE_PREFIX_LENGTH) };
    await tx.insert(apiKey).values(row);
    return { key, view: viewOf(row) };
  });
}

export async function liveKeys(db: Db, userId: string): Promise<KeyView[]> {
  const rows = await db.select().from(apiKey).where(and(eq(apiKey.userId, userId), isNull(apiKey.revokedAt)))
    .orderBy(desc(apiKey.createdAt));
  return rows.map(viewOf);
}

// Revoked at once on the hub; jeff-server drops it at its next key-list fetch (within about a minute).
export async function revokeKey(db: Db, userId: string, keyId: string, now: Date): Promise<void> {
  const revoked = await db.update(apiKey).set({ revokedAt: now })
    .where(and(eq(apiKey.id, keyId), eq(apiKey.userId, userId), isNull(apiKey.revokedAt)))
    .returning({ id: apiKey.id });
  if (revoked.length === 0) throw new KeyRefused('No live key with that id on your account');
}
