// The data of the /account page (GET /api/account, server/endpoints/account.ts): the subscribe screen for an account
// that may not use keys, or the subscription, this period's usage against the allowance and the live keys.
import type { AccountPage } from './account-page';
import type { AccountsSettings } from './accounts-settings.server';
import type { Db } from './db.server';
import { liveKeys } from './keys.server.js';
import { shownKey } from './keys-format.js';
import { priceText, tokensText } from './public-settings.server.js';
import type { SessionUser } from './session.server';
import { isUsable, subscriptionOf } from './subscription.server.js';
import { monthStart } from './time.js';
import { usageOf } from './usage.server.js';

export type { AccountPage };

// The account id jeff-server reports the playground's usage under (spec Section 7a).
export const PLAYGROUND_ACCOUNT = 'playground';

export async function accountPageData(db: Db, settings: AccountsSettings, user: SessionUser, now: Date): Promise<AccountPage> {
  const owner = user.email.toLowerCase() === settings.ownerEmail;
  const since = monthStart(now);
  const playground = owner ? { used: await usageOf(db, PLAYGROUND_ACCOUNT, since), since: since.toISOString() } : null;
  const row = await subscriptionOf(db, user.id);
  if (row === null || !isUsable(row.status)) {
    return { view: 'subscribe', email: user.email, price: priceText(settings.plan.priceUsd),
      allowance: tokensText(settings.plan.allowanceInputTokens), status: row === null ? null : row.status, playground };
  }
  return {
    view: 'account',
    email: user.email,
    status: row.status,
    periodEnd: row.currentPeriodEnd.toISOString(),
    cancelAtPeriodEnd: row.cancelAtPeriodEnd,
    usage: { used: await usageOf(db, user.id, row.currentPeriodStart), allowance: settings.plan.allowanceInputTokens },
    keys: (await liveKeys(db, user.id)).map((key) => ({ id: key.id, name: key.name, shown: shownKey(key.prefix),
      createdAt: key.createdAt.toISOString(), lastUsedAt: key.lastUsedAt === null ? null : key.lastUsedAt.toISOString() })),
    playground,
  };
}
