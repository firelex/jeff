import { eq, sql } from 'drizzle-orm';
import type { AccountsSettings } from './accounts-settings.server.js';
import { dataSetPathnamesOfUserFolder } from './adapter-requests.server.js';
import type { DataSetStore } from './data-set-store.server.js';
import type { Db } from './db.server.js';
import { stripeCustomer, usage, user } from './schema.js';
import type { SessionUser } from './session.server.js';
import { cancelSubscription, hasEnded, openCheckoutsOf, subscriptionsOf, type StripeApi } from './stripe.server.js';
import { isUsable } from './subscription.server.js';

// A request the customer can do something about (subscribe first, or manage the subscription they have); the
// account endpoints (server/endpoints/account.ts) answer it as a 409 with this message.
export class BillingRefused extends Error {}

export interface BillingDeps { db: Db; stripe: StripeApi; settings: AccountsSettings }

export const ALREADY_SUBSCRIBED = 'Your subscription is already active; manage it with "Manage billing"';
export const PAY_IN_PORTAL = 'Your last payment did not go through, so your subscription is on hold. Pay it in '
  + '"Manage billing" (the Stripe customer portal) to start using it again.';

export const RESUME_IN_PORTAL = 'Your subscription is paused. Resume it in "Manage billing" (the Stripe customer '
  + 'portal) instead of starting a new one.';

async function storedCustomer(db: Db, userId: string): Promise<string | null> {
  const rows = await db.select({ id: stripeCustomer.customerId }).from(stripeCustomer).where(eq(stripeCustomer.userId, userId));
  return rows.length === 0 ? null : rows[0].id;
}

// The user's one Stripe customer, made on their first Checkout. A transaction-long lock on the user means two tabs
// starting Checkout at once make one customer: the second waits, then finds the first one's row. The idempotency key
// (the user's id) also makes a repeated create, after a failure to store the row, answer the same customer.
async function customerFor(db: Db, stripe: StripeApi, buyer: SessionUser): Promise<string> {
  return db.transaction(async (tx) => {
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`hub-customer:${buyer.id}`}))`);
    const [existing] = await tx.select({ id: stripeCustomer.customerId }).from(stripeCustomer).where(eq(stripeCustomer.userId, buyer.id));
    if (existing !== undefined) return existing.id;
    const made = await stripe.customers.create({ email: buyer.email, metadata: { user_id: buyer.id } },
      { idempotencyKey: `hub-customer-${buyer.id}` });
    await tx.insert(stripeCustomer).values({ userId: buyer.id, customerId: made.id, createdAt: new Date() });
    return made.id;
  });
}

// Stripe Checkout for the one plan (spec Section 4), always for the user's own Stripe customer. What the customer
// has in Stripe decides (the hub's row can lag behind the webhooks): a usable subscription, an unpaid one (to be
// paid in the portal) or a paused one (to be resumed there) refuses; an incomplete one (a payment never finished) is cancelled first, so it can never
// bill beside the new one. The subscription carries the user's id and this hub's address in its metadata and the session in
// client_reference_id, so every webhook can find the user whatever order the events come in (the webhook refuses a
// Checkout where the two differ). The redirect back to /account/return is never trusted: the page waits for the
// webhook.
export async function checkoutUrl({ db, stripe, settings }: BillingDeps, buyer: SessionUser): Promise<string> {
  const customer = await customerFor(db, stripe, buyer);
  const subscriptions = await subscriptionsOf(stripe, customer);
  if (subscriptions.some((s) => isUsable(s.status))) throw new BillingRefused(ALREADY_SUBSCRIBED);
  if (subscriptions.some((s) => s.status === 'unpaid')) throw new BillingRefused(PAY_IN_PORTAL);
  if (subscriptions.some((s) => s.status === 'paused')) throw new BillingRefused(RESUME_IN_PORTAL);
  for (const unfinished of subscriptions.filter((s) => s.status === 'incomplete')) await cancelSubscription(stripe, unfinished.id);
  const tax = settings.stripe.automaticTax;
  const session = await stripe.checkout.sessions.create({
    mode: 'subscription',
    line_items: [{ price: settings.stripe.priceId, quantity: 1 }],
    client_reference_id: buyer.id,
    customer,
    ...(tax ? { customer_update: { address: 'auto' as const } } : {}),
    subscription_data: { metadata: { user_id: buyer.id, hub: settings.baseUrl } },
    automatic_tax: { enabled: tax },
    billing_address_collection: tax ? 'required' : 'auto',
    success_url: `${settings.baseUrl}/account/return?session_id={CHECKOUT_SESSION_ID}`,
    cancel_url: `${settings.baseUrl}/account`,
  });
  if (session.url === null) throw new Error(`Stripe made Checkout session ${session.id} without a URL`);
  return session.url;
}

export async function portalUrl({ db, stripe, settings }: BillingDeps, userId: string): Promise<string> {
  const customer = await storedCustomer(db, userId);
  if (customer === null) throw new BillingRefused('There is no billing account yet: subscribe first');
  const portal = await stripe.billingPortal.sessions.create({ customer, return_url: `${settings.baseUrl}/account` });
  return portal.url;
}

// Expires, in Stripe, every open Checkout of the user's customer, and cancels every subscription that has not ended (Stripe's list, not the hub's
// row, which can lag behind the webhooks), then deletes the user: sessions, sign-in methods, keys, the customer and
// subscription rows go with it (their foreign keys cascade); usage rows are deleted here. A failure in Stripe stops
// before anything is deleted. Invoices stay in Stripe. A webhook that arrives afterwards finds no user: a
// subscription that has ended is ignored, one that has not (a Checkout finished after the deletion) is refunded and
// cancelled (stripe-webhook.server.ts). The user's adapter requests go with it (cascade), and every zip of theirs,
// the requests' and everything else in their folder of the file store (uploads that never became a request), is
// deleted from `dataSets` inside the same transaction, so a failure there leaves the account as it was (the error
// goes to the caller).
export async function deleteAccount({ db, stripe }: BillingDeps, userId: string, dataSets: Pick<DataSetStore, 'list' | 'remove'>): Promise<void> {
  const customer = await storedCustomer(db, userId);
  if (customer !== null) {
    // Open Checkouts first, so none can be paid once the account is gone.
    for (const open of await openCheckoutsOf(stripe, customer)) await stripe.checkout.sessions.expire(open.id);
    for (const live of (await subscriptionsOf(stripe, customer)).filter((s) => !hasEnded(s.status))) {
      await cancelSubscription(stripe, live.id);
    }
  }
  // The user's row first, then its usage: recordUsage (internal-api.server.ts) takes the same rows in the same order
  // (it locks the user's row before storing a total), so a usage report arriving during a deletion either finishes
  // first and its rows are deleted here, or waits and then finds no user and stores nothing.
  // The lock on the user's row is the one sendRequest (adapter-requests.server.ts) takes, so no request with a data
  // set is stored between reading the zips and deleting the user.
  await db.transaction(async (tx) => {
    await tx.select({ id: user.id }).from(user).where(eq(user.id, userId)).for('update');
    const zips = await dataSetPathnamesOfUserFolder(tx, dataSets, userId);
    await tx.delete(user).where(eq(user.id, userId));
    await tx.delete(usage).where(eq(usage.accountId, userId));
    await dataSets.remove(zips);
  });
}
