// At most SIGN_IN_EMAILS_PER_HOUR sign-in emails to one email address in any rolling hour, counted in the database
// (sign_in_email), so every function instance shares the count. Better Auth's own limits count per visitor address;
// this one stops someone who solves Turnstile from many addresses from flooding one inbox (review M8).
//
// The address is stored only as the SHA-256 of its trimmed, lower-cased form. The count and the new row are one
// transaction holding a Postgres advisory lock on that hash, so two instances at the same moment cannot both see 4
// and both send. Rows older than an hour, for any address, are deleted in the same transaction.
import { createHash, randomUUID } from 'node:crypto';
import { and, count, eq, gt, lt, sql } from 'drizzle-orm';
import type { Db } from './db.server.js';
import { signInEmail } from './schema.js';

export const SIGN_IN_EMAILS_PER_HOUR = 5;
const HOUR_MS = 60 * 60 * 1000;

export function addressHash(email: string): string {
  return createHash('sha256').update(email.trim().toLowerCase(), 'utf8').digest('hex');
}

// Records one sign-in email to `email` and returns true, or returns false (recording nothing) when that address has
// had SIGN_IN_EMAILS_PER_HOUR in the last hour.
export async function claimSignInEmail(db: Db, email: string, now: number = Date.now()): Promise<boolean> {
  const hash = addressHash(email);
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtextextended(${hash}, 0))`);
    await tx.delete(signInEmail).where(lt(signInEmail.sentAt, now - HOUR_MS));
    const [{ sent }] = await tx.select({ sent: count() }).from(signInEmail)
      .where(and(eq(signInEmail.addressHash, hash), gt(signInEmail.sentAt, now - HOUR_MS)));
    if (sent >= SIGN_IN_EMAILS_PER_HOUR) return false;
    await tx.insert(signInEmail).values({ id: randomUUID(), addressHash: hash, sentAt: now });
    return true;
  });
}
