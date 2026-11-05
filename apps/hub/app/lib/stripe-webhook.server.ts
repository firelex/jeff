import { eq, sql } from 'drizzle-orm';
import type Stripe from 'stripe';
import { ApiError, errorResponse, json } from '../../server/http.js';
import type { Db } from './db.server';
import { stripeEvent, subscription, user } from './schema.js';
import { hasEnded, refundAndCancel, verifyEvent, WebhookRefused, type StripeApi } from './stripe.server.js';
import { isUsable } from './subscription.server.js';

// Every subscription event is answered the same way: fetch the subscription and store it as Stripe has it now.
const SUBSCRIPTION_EVENTS = new Set(['customer.subscription.created', 'customer.subscription.updated',
  'customer.subscription.deleted', 'customer.subscription.paused', 'customer.subscription.resumed']);

// Which subscription an event is about, read from the event alone (no network). checkoutUserId: for a completed
// Checkout, the user it was made for (client_reference_id). null: an event the hub does not use.
type Reference = { subscriptionId: string; checkoutUserId: string | null };

function referenceOf(event: Stripe.Event): Reference | null {
  if (event.type === 'checkout.session.completed') {
    const session = event.data.object as Stripe.Checkout.Session;
    if (session.mode !== 'subscription') return null;
    if (session.client_reference_id === null) {
      throw new Error(`Checkout session ${session.id} has no client_reference_id; the hub always sets it to the user's id`);
    }
    if (session.subscription === null) throw new Error(`Checkout session ${session.id} completed without a subscription`);
    const subscriptionId = typeof session.subscription === 'string' ? session.subscription : session.subscription.id;
    return { subscriptionId, checkoutUserId: session.client_reference_id };
  }
  if (SUBSCRIPTION_EVENTS.has(event.type)) {
    return { subscriptionId: (event.data.object as Stripe.Subscription).id, checkoutUserId: null };
  }
  return null;
}

// Which hub made a subscription: its metadata.hub, the hub's address (BETTER_AUTH_URL), which the hub's Checkout
// sets. Several hubs (previews, a local one, a test run) can share one Stripe test-mode account and so receive each
// other's events; a hub acts only on its own subscriptions.
function hubOf(current: Stripe.Subscription): string {
  const hub = current.metadata.hub;
  if (hub === undefined || hub === '') {
    throw new Error(`Subscription ${current.id} has no metadata.hub: it was not made by the hub's Checkout`);
  }
  return hub;
}

// The user a subscription belongs to: its metadata.user_id, which the hub's Checkout sets (and which must match the
// Checkout session's client_reference_id when the event is a completed Checkout).
function ownerOf(current: Stripe.Subscription, checkoutUserId: string | null): string {
  const userId = current.metadata.user_id;
  if (userId === undefined || userId === '') {
    throw new Error(`Subscription ${current.id} has no metadata.user_id: it was not made by the hub's Checkout`);
  }
  if (checkoutUserId !== null && checkoutUserId !== userId) {
    throw new Error(`Subscription ${current.id} has metadata.user_id ${userId} but its Checkout session's client_reference_id is ${checkoutUserId}`);
  }
  return userId;
}

// The subscription row as Stripe has it. The billing period is on the subscription's item (Stripe API versions from
// 2025-03-31 on; the installed SDK's Stripe.SubscriptionItem carries current_period_start and _end).
function rowOf(userId: string, current: Stripe.Subscription, now: Date) {
  if (current.items.data.length !== 1) {
    throw new Error(`Subscription ${current.id} has ${current.items.data.length} items; the hub sells exactly one Price`);
  }
  const item = current.items.data[0];
  return {
    userId,
    stripeCustomerId: typeof current.customer === 'string' ? current.customer : current.customer.id,
    stripeSubscriptionId: current.id,
    status: current.status,
    currentPeriodStart: new Date(item.current_period_start * 1000),
    currentPeriodEnd: new Date(item.current_period_end * 1000),
    cancelAtPeriodEnd: current.cancel_at_period_end,
    updatedAt: now,
  };
}

// Which of two subscriptions was made later, by Stripe's `created` (the id breaks a tie, so both sides of a race agree).
function later(a: Stripe.Subscription, b: Stripe.Subscription): Stripe.Subscription {
  if (a.created !== b.created) return a.created > b.created ? a : b;
  return a.id > b.id ? a : b;
}

export type EventResult = 'processed' | 'duplicate' | 'ignored' | 'refunded';

// Records the event by id and applies it, in one transaction: a repeat changes nothing ('duplicate'), and a failure
// records nothing, so Stripe sends the event again. Events the hub does not use are recorded and 'ignored'.
// The order inside the transaction is what keeps the stored state current (review I1): the event row first (a repeat
// stops there, before any call to Stripe); then a lock on the subscription id, held to the end of the transaction;
// only then the fetch from Stripe, then a lock on the user, and the write. Two events about one subscription handled
// at the same moment therefore fetch one after the other, and the later fetch, which sees Stripe's newer state, also
// writes last; two events about two subscriptions of one user (two Checkouts finished at once) decide one after the
// other.
// A subscription another hub made (its metadata.hub names another address) is recorded and ignored, before anything
// else is decided about it; one with no metadata.hub is an error, as one with no metadata.user_id is.
// A subscription of this hub that must not bill is refunded and cancelled ('refunded'; stripe.server.ts refundAndCancel), and the
// event is recorded only once both calls have succeeded (the transaction commits after them):
// - one whose user no longer exists (a Checkout finished after the account was deleted), unless it has ended;
// - a user's second usable subscription: the later of the two by Stripe's `created`, whichever event comes first.
export async function handleStripeEvent(db: Db, stripe: StripeApi, event: Stripe.Event, now: Date, hub: string): Promise<EventResult> {
  const reference = referenceOf(event);
  return db.transaction(async (tx) => {
    const fresh = await tx.insert(stripeEvent).values({ id: event.id, type: event.type, processedAt: now })
      .onConflictDoNothing().returning({ id: stripeEvent.id });
    if (fresh.length === 0) return 'duplicate';
    if (reference === null) return 'ignored';
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${reference.subscriptionId}))`);
    let current = await stripe.subscriptions.retrieve(reference.subscriptionId);
    if (current.id !== reference.subscriptionId) {
      throw new Error(`Stripe answered subscription ${current.id} when asked for ${reference.subscriptionId}`);
    }
    // Another hub's subscription (one Stripe account, several hubs): recorded and left alone, with a log line naming
    // both addresses, so a changed BETTER_AUTH_URL that leaves this hub's own subscriptions behind shows (final review M9).
    if (hubOf(current) !== hub) {
      console.warn(`Stripe event ${event.id} (${event.type}) ignored: subscription ${current.id} belongs to the hub at ${hubOf(current)}, and this hub is ${hub}`);
      return 'ignored';
    }
    const userId = ownerOf(current, reference.checkoutUserId);
    const [owner] = await tx.select({ id: user.id }).from(user).where(eq(user.id, userId));
    if (owner === undefined) {
      // The account was deleted. Its own subscriptions were cancelled then; one that still runs was paid for after.
      if (hasEnded(current.status)) return 'ignored';
      await refundAndCancel(stripe, current, 'requested_by_customer');
      return 'refunded';
    }
    await tx.execute(sql`select pg_advisory_xact_lock(hashtext(${`hub-user:${userId}`}))`);
    const [existing] = await tx.select().from(subscription).where(eq(subscription.userId, userId)).for('update');
    if (existing !== undefined && existing.stripeSubscriptionId !== current.id) {
      // A user's earlier subscription ending (canceled, expired) never overrides the one that replaced it.
      if (!isUsable(current.status)) return 'ignored';
      // Both fetched again under the user's lock: another event may have just cancelled either of them.
      current = await stripe.subscriptions.retrieve(current.id);
      const other = await stripe.subscriptions.retrieve(existing.stripeSubscriptionId);
      if (!isUsable(current.status)) return 'ignored';
      if (isUsable(other.status)) {
        const second = later(current, other);
        await refundAndCancel(stripe, second, 'duplicate');
        if (second.id === current.id) return 'refunded';
      }
    }
    const row = rowOf(userId, current, now);
    const { userId: _key, ...changes } = row;
    await tx.insert(subscription).values(row).onConflictDoUpdate({ target: subscription.userId, set: changes });
    return 'processed';
  });
}

// POST /api/stripe/webhook (server/endpoints/stripe-webhook.ts). The signature is checked over the body exactly as it
// arrived (request.text(), never parsed and serialised again). 400 for a missing or wrong signature (Stripe shows it
// in its dashboard); any other failure throws (500), so Stripe sends the event again.
export async function stripeWebhook(request: Request, deps: { db: Db; stripe: StripeApi; webhookSecret: string; now: Date; hub: string }):
  Promise<Response> {
  const raw = await request.text();
  let event: Stripe.Event;
  try {
    event = verifyEvent(raw, request.headers.get('stripe-signature'), deps.webhookSecret);
  } catch (error) {
    if (error instanceof WebhookRefused) return errorResponse(new ApiError(400, 'WebhookRefused', error.message));
    throw error;
  }
  const result = await handleStripeEvent(deps.db, deps.stripe, event, deps.now, deps.hub);
  return json(200, { received: event.id, result });
}
