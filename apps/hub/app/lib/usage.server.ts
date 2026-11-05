import { and, eq } from 'drizzle-orm';
import type { Db } from './db.server';
import { usage } from './schema.js';

// Input tokens jeff-server has reported for this account (a user id, or 'playground') and period (the billing
// period's start for a user, spec Section 4; the month's start for the playground). No row yet means no call was
// counted this period (jeff-server reports every account it has counted, every minute), so it is 0.
export async function usageOf(db: Db, accountId: string, periodStart: Date): Promise<number> {
  const rows = await db.select({ tokens: usage.inputTokens }).from(usage)
    .where(and(eq(usage.accountId, accountId), eq(usage.periodStart, periodStart)));
  return rows.length === 0 ? 0 : rows[0].tokens;
}
