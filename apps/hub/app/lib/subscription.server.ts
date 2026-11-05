import { eq } from 'drizzle-orm';
import type { Db } from './db.server';
import { subscription } from './schema.js';

// Stripe subscription statuses that may use keys (spec Section 4): past_due keeps working while Stripe retries the
// card. Every other status (canceled, unpaid, incomplete, incomplete_expired, paused) may not.
export const USABLE_STATUSES = ['active', 'trialing', 'past_due'] as const;

export function isUsable(status: string): boolean {
  return (USABLE_STATUSES as readonly string[]).includes(status);
}

export type SubscriptionRow = typeof subscription.$inferSelect;

export async function subscriptionOf(db: Db, userId: string): Promise<SubscriptionRow | null> {
  const rows = await db.select().from(subscription).where(eq(subscription.userId, userId));
  return rows.length === 0 ? null : rows[0];
}

export async function canUseKeys(db: Db, userId: string): Promise<boolean> {
  const row = await subscriptionOf(db, userId);
  return row !== null && isUsable(row.status);
}
