import type { Db } from '../../app/lib/db.server';
import { stripeCustomer, subscription, user } from '../../app/lib/schema';

export const NOW = new Date('2026-11-03T10:00:00Z');
export const PERIOD = { start: new Date('2026-11-03T10:00:00Z'), end: new Date('2026-12-03T10:00:00Z') };

export async function aUser(db: Db, id: string, email = `${id}@example.com`): Promise<void> {
  await db.insert(user).values({ id, name: id, email, emailVerified: true, createdAt: NOW, updatedAt: NOW });
}

export async function aSubscriber(db: Db, id: string, status = 'active', period = PERIOD): Promise<void> {
  await aUser(db, id);
  await db.insert(stripeCustomer).values({ userId: id, customerId: `cus_${id}`, createdAt: NOW });
  await db.insert(subscription).values({ userId: id, stripeCustomerId: `cus_${id}`, stripeSubscriptionId: `sub_${id}`,
    status, currentPeriodStart: period.start, currentPeriodEnd: period.end, cancelAtPeriodEnd: false, updatedAt: NOW });
}
