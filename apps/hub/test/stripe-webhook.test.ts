// @vitest-environment node
// The Stripe webhook: signed events only, each event id processed once, and every subscription event answered by
// what Stripe says now (a fake Stripe here; no network), so any order of arrival leaves the right state.
import { createHmac } from 'node:crypto';
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { PGlite } from '@electric-sql/pglite';
import { eq } from 'drizzle-orm';
import { drizzle } from 'drizzle-orm/pglite';
import { migrate } from 'drizzle-orm/pglite/migrator';
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest';
import api from '../api/index';
import { stripeEvent as stripeEventTable, subscription } from '../app/lib/schema';
import { handleStripeEvent as handleAt, stripeWebhook } from '../app/lib/stripe-webhook.server';
import { canUseKeys } from '../app/lib/subscription.server';
import { testDb } from './helpers/db';
import { aSubscriber, aUser, NOW } from './helpers/accounts';
import { TEST_ENV } from './helpers/settings';
import type Stripe from 'stripe';
import { aPaidInvoice, aStripeSubscription, fakeStripe, HUB, PERIOD_END, PERIOD_START, signatureFor, signedWebhook, stripeEvent } from './helpers/stripe';

const SECRET = 'whsec_test';

// This hub's events, as the endpoint hands them over (its address is metadata.hub of its own subscriptions).
const handleStripeEvent = (db: Parameters<typeof handleAt>[0], stripe: Parameters<typeof handleAt>[1],
  event: Parameters<typeof handleAt>[2], now: Date) => handleAt(db, stripe, event, now, HUB);

async function setUp(status: Parameters<typeof aStripeSubscription>[1] = 'active') {
  const db = await testDb();
  await aUser(db, 'u1');
  const stripe = fakeStripe({ sub_1: aStripeSubscription('sub_1', status, 'u1') });
  const deliver = (event: ReturnType<typeof stripeEvent>, tamper = false) =>
    stripeWebhook(signedWebhook(event, SECRET, tamper), { db, stripe: stripe.api, webhookSecret: SECRET, now: NOW, hub: HUB });
  return { db, stripe, deliver };
}

const completed = (id = 'evt_1') => stripeEvent(id, 'checkout.session.completed',
  { id: 'cs_1', object: 'checkout.session', mode: 'subscription', client_reference_id: 'u1', subscription: 'sub_1' });

it('a signed checkout.session.completed makes the subscription usable, with its billing period', async () => {
  const { db, deliver } = await setUp();
  const response = await deliver(completed());
  expect(response.status).toBe(200);
  expect(await canUseKeys(db, 'u1')).toBe(true);
  const [row] = await db.select().from(subscription);
  expect(row).toMatchObject({ stripeCustomerId: 'cus_u1', stripeSubscriptionId: 'sub_1', status: 'active',
    cancelAtPeriodEnd: false });
  expect(row.currentPeriodStart.getTime()).toBe(PERIOD_START * 1000);
  expect(row.currentPeriodEnd.getTime()).toBe(PERIOD_END * 1000);
});

it('refuses a webhook whose signature does not match, or that has none, and changes nothing', async () => {
  const { db, deliver } = await setUp();
  expect((await deliver(completed(), true)).status).toBe(400);
  const unsigned = await stripeWebhook(new Request('https://jeffhub.ai/api/stripe/webhook', { method: 'POST', body: '{}' }),
    { db, stripe: fakeStripe({}).api, webhookSecret: SECRET, now: NOW, hub: HUB });
  expect(unsigned.status).toBe(400);
  expect(await unsigned.text()).toContain('Stripe-Signature');
  expect(await db.select().from(subscription)).toEqual([]);
  expect(await db.select().from(stripeEventTable)).toEqual([]);
});

it('processes an event id once', async () => {
  const { db, deliver } = await setUp();
  await deliver(completed());
  const again = await deliver(completed());
  expect(await again.json()).toMatchObject({ result: 'duplicate' });
  expect(await db.select().from(stripeEventTable)).toHaveLength(1);
});

it.each([['active', true], ['trialing', true], ['past_due', true], ['canceled', false], ['unpaid', false],
  ['incomplete', false], ['incomplete_expired', false], ['paused', false]] as const)(
  'a subscription %s may use keys: %s', async (status, usable) => {
    const { db, deliver } = await setUp(status);
    await deliver(stripeEvent('evt_s', 'customer.subscription.updated', aStripeSubscription('sub_1', status, 'u1')));
    expect(await canUseKeys(db, 'u1')).toBe(usable);
  });

it('events in any order leave the subscription as Stripe has it now', async () => {
  const { db, deliver } = await setUp('active');
  await deliver(stripeEvent('evt_2', 'customer.subscription.updated', aStripeSubscription('sub_1', 'active', 'u1'), PERIOD_START + 2));
  await deliver(stripeEvent('evt_1', 'customer.subscription.created', aStripeSubscription('sub_1', 'incomplete', 'u1'), PERIOD_START + 1));
  await deliver(completed('evt_0'));
  const [row] = await db.select().from(subscription);
  expect(row.status).toBe('active');
});

it('a subscription event before checkout.session.completed already makes the row', async () => {
  const { db, deliver } = await setUp('active');
  await deliver(stripeEvent('evt_c', 'customer.subscription.created', aStripeSubscription('sub_1', 'active', 'u1')));
  expect(await canUseKeys(db, 'u1')).toBe(true);
});

it('an old subscription ending never overrides the one that replaced it', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'active');  // sub_u1 is current
  const stripe = fakeStripe({ sub_old: aStripeSubscription('sub_old', 'canceled', 'u1') });
  await handleStripeEvent(db, stripe.api, stripeEvent('evt_o', 'customer.subscription.deleted',
    aStripeSubscription('sub_old', 'canceled', 'u1')), NOW);
  const [row] = await db.select().from(subscription).where(eq(subscription.userId, 'u1'));
  expect(row).toMatchObject({ stripeSubscriptionId: 'sub_u1', status: 'active' });
});

it('an event for a deleted account is recorded and ignored', async () => {
  const db = await testDb();
  const stripe = fakeStripe({ sub_9: aStripeSubscription('sub_9', 'canceled', 'gone') });
  const result = await handleStripeEvent(db, stripe.api, stripeEvent('evt_g', 'customer.subscription.deleted',
    aStripeSubscription('sub_9', 'canceled', 'gone')), NOW);
  expect(result).toBe('ignored');
  expect(await db.select().from(stripeEventTable)).toHaveLength(1);
});

it('a subscription the hub did not make is an error, and is not recorded, so Stripe sends it again', async () => {
  const db = await testDb();
  const stranger = aStripeSubscription('sub_x', 'active', 'u1', { metadata: { hub: HUB } });
  const stripe = fakeStripe({ sub_x: stranger });
  await expect(handleStripeEvent(db, stripe.api, stripeEvent('evt_x', 'customer.subscription.created', stranger), NOW))
    .rejects.toThrow(/sub_x has no metadata.user_id/);
  expect(await db.select().from(stripeEventTable)).toEqual([]);
});

it('records and ignores the event types it does not use', async () => {
  const { db, deliver } = await setUp();
  const response = await deliver(stripeEvent('evt_i', 'invoice.paid', { id: 'in_1', object: 'invoice' }));
  expect(await response.json()).toMatchObject({ result: 'ignored' });
  expect(await db.select().from(stripeEventTable)).toHaveLength(1);
});

it('an old subscription\'s event that is skipped answers ignored', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'active');
  const stripe = fakeStripe({ sub_old: aStripeSubscription('sub_old', 'canceled', 'u1') });
  expect(await handleStripeEvent(db, stripe.api, stripeEvent('evt_o', 'customer.subscription.updated',
    aStripeSubscription('sub_old', 'canceled', 'u1')), NOW)).toBe('ignored');
});

it.each(['customer.subscription.paused', 'customer.subscription.resumed'])('%s is stored as Stripe has it', async (type) => {
  const { db, stripe, deliver } = await setUp('paused');
  const response = await deliver(stripeEvent('evt_p', type, aStripeSubscription('sub_1', 'active', 'u1')));
  expect(await response.json()).toMatchObject({ result: 'processed' });
  expect(stripe.calls.retrieve).toHaveBeenCalledWith('sub_1');
  expect((await db.select().from(subscription))[0].status).toBe('paused');
});

it('a repeated event is answered before any call to Stripe', async () => {
  const { stripe, deliver } = await setUp();
  await deliver(completed());
  stripe.calls.retrieve.mockClear();
  expect(await (await deliver(completed())).json()).toMatchObject({ result: 'duplicate' });
  expect(stripe.calls.retrieve).not.toHaveBeenCalled();
});

it('a checkout for one user whose subscription names another is an error, and is not recorded', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  await aUser(db, 'u2');
  const stripe = fakeStripe({ sub_1: aStripeSubscription('sub_1', 'active', 'u2') });
  await expect(handleStripeEvent(db, stripe.api, completed(), NOW))
    .rejects.toThrow(/sub_1 has metadata.user_id u2 but its Checkout session's client_reference_id is u1/);
  expect(await db.select().from(subscription)).toEqual([]);
  expect(await db.select().from(stripeEventTable)).toEqual([]);
});

it('a failed fetch from Stripe records nothing, so Stripe\'s retry is processed', async () => {
  const { db, stripe, deliver } = await setUp();
  stripe.calls.retrieve.mockRejectedValueOnce(new Error('Stripe is unreachable'));
  // The endpoint lets the error through, so the platform answers 500 and Stripe sends the event again.
  await expect(deliver(completed())).rejects.toThrow('Stripe is unreachable');
  expect(await db.select().from(stripeEventTable)).toEqual([]);
  expect(await db.select().from(subscription)).toEqual([]);
  expect(await (await deliver(completed())).json()).toMatchObject({ result: 'processed' });
  expect(await canUseKeys(db, 'u1')).toBe(true);
});

// Review I1: Checkout sends created, completed and updated within milliseconds. Each handler fetches from Stripe
// only once it holds the subscription's lock, so the fetch that sees the newer state also writes last.
it('two events handled at once: the newer state Stripe answers wins', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  let calls = 0;
  const answers = [
    // The first fetch is slow and sees the older state; the second is fast and sees the newer one.
    () => new Promise<Stripe.Subscription>((resolve) => setTimeout(() => resolve(aStripeSubscription('sub_1', 'incomplete', 'u1')), 50)),
    async () => aStripeSubscription('sub_1', 'active', 'u1'),
  ];
  const stripe = fakeStripe({});
  stripe.calls.retrieve.mockImplementation(() => answers[calls++]());
  const results = await Promise.all([
    handleStripeEvent(db, stripe.api, stripeEvent('evt_a', 'customer.subscription.created', aStripeSubscription('sub_1', 'incomplete', 'u1')), NOW),
    handleStripeEvent(db, stripe.api, completed('evt_b'), NOW),
  ]);
  expect(results).toEqual(['processed', 'processed']);
  expect((await db.select().from(subscription))[0].status).toBe('active');
});

// PGlite runs one transaction at a time, so the test above cannot show the lock itself working; this one pins the
// order of the steps inside the transaction, which is what makes the lock work on Postgres.
it('records the event, then locks the subscription, fetches from Stripe, locks the user, then writes', async () => {
  const steps: string[] = [];
  const db = await testDb((query) => {
    if (/^insert into "stripe_event"/.test(query)) steps.push('record the event');
    else if (/pg_advisory_xact_lock\(hashtext\(\$1\)\)/.test(query)) steps.push(steps.includes('lock the subscription') ? 'lock the user' : 'lock the subscription');
    else if (/^insert into "subscription"/.test(query)) steps.push('write the subscription');
  });
  await aUser(db, 'u1');
  steps.length = 0;
  const stripe = fakeStripe({ sub_1: aStripeSubscription('sub_1', 'active', 'u1') });
  stripe.calls.retrieve.mockImplementation(async (id: string) => {
    steps.push(`fetch ${id} from Stripe`);
    return stripe.subscriptions[id];
  });
  await handleStripeEvent(db, stripe.api, completed(), NOW);
  expect(steps).toEqual(['record the event', 'lock the subscription', 'fetch sub_1 from Stripe', 'lock the user', 'write the subscription']);
});

// A subscription that must not bill: refunded (every paid payment of its latest invoice), then cancelled, each call
// with an idempotency key from the subscription's id; the event is recorded only once both have succeeded.
const refundOf = (id: string, reason: string) => [{ payment_intent: `pi_${id}`, amount: 100, reason, metadata: { invoice_payment: `inpay_${id}` } },
  { idempotencyKey: `hub-refund-${id}-inpay_${id}` }];
const cancelOf = (id: string) => [id, {}, { idempotencyKey: `hub-cancel-${id}` }];
const events = async (db: Awaited<ReturnType<typeof testDb>>) => (await db.select().from(stripeEventTable)).map((e) => e.id);

describe('a subscription whose account no longer exists', () => {
  const orphan = () => fakeStripe({ sub_9: aStripeSubscription('sub_9', 'active', 'gone') }, aPaidInvoice('sub_9'));
  const sessionFor = (id: string) => stripeEvent(id, 'checkout.session.completed',
    { id: 'cs_9', object: 'checkout.session', mode: 'subscription', client_reference_id: 'gone', subscription: 'sub_9' });

  it.each([
    ['checkout.session.completed', sessionFor('evt_x')],
    ['customer.subscription.created', stripeEvent('evt_x', 'customer.subscription.created', aStripeSubscription('sub_9', 'active', 'gone'))],
    ['customer.subscription.updated', stripeEvent('evt_x', 'customer.subscription.updated', aStripeSubscription('sub_9', 'active', 'gone'))],
    ['customer.subscription.resumed', stripeEvent('evt_x', 'customer.subscription.resumed', aStripeSubscription('sub_9', 'active', 'gone'))],
  ])('%s: its payment is refunded, then it is cancelled', async (_type, event) => {
    const db = await testDb();
    const stripe = orphan();
    expect(await handleStripeEvent(db, stripe.api, event, NOW)).toBe('refunded');
    expect(stripe.calls.invoicePayments).toHaveBeenCalledWith({ invoice: 'in_sub_9', status: 'paid', limit: 100 });
    expect(stripe.calls.refund.mock.calls).toEqual([refundOf('sub_9', 'requested_by_customer')]);
    expect(stripe.calls.cancel.mock.calls).toEqual([cancelOf('sub_9')]);
    expect(stripe.calls.refund.mock.invocationCallOrder[0]).toBeLessThan(stripe.calls.cancel.mock.invocationCallOrder[0]);
    expect(await events(db)).toEqual(['evt_x']);
    expect(await db.select().from(subscription)).toEqual([]);
  });

  it('a failed refund cancels nothing and records nothing; the retried event does both, once', async () => {
    const db = await testDb();
    const stripe = orphan();
    stripe.calls.refund.mockRejectedValueOnce(new Error('Stripe is down'));
    await expect(handleStripeEvent(db, stripe.api, sessionFor('evt_x'), NOW)).rejects.toThrow('Stripe is down');
    expect(stripe.calls.cancel).not.toHaveBeenCalled();
    expect(await events(db)).toEqual([]);
    expect(await handleStripeEvent(db, stripe.api, sessionFor('evt_x'), NOW)).toBe('refunded');
    expect(stripe.refunded).toHaveLength(1);
    expect(stripe.subscriptions.sub_9.status).toBe('canceled');
    expect(await events(db)).toEqual(['evt_x']);
  });

  it('a refund that went through and a failed cancel: the retry finds the refund, makes no second one, then cancels', async () => {
    const db = await testDb();
    const stripe = orphan();
    stripe.calls.cancel.mockRejectedValueOnce(new Error('Stripe is down'));
    await expect(handleStripeEvent(db, stripe.api, sessionFor('evt_x'), NOW)).rejects.toThrow('Stripe is down');
    expect(await events(db)).toEqual([]);
    expect(await handleStripeEvent(db, stripe.api, sessionFor('evt_x'), NOW)).toBe('refunded');
    // Within the 24 hours the refund made is seen in the payment's refunds, so it is not asked for again.
    expect(stripe.calls.refund).toHaveBeenCalledTimes(1);
    expect(stripe.refunded).toHaveLength(1);
    expect(stripe.subscriptions.sub_9.status).toBe('canceled');
  });

  it('a retry after Stripe has forgotten the keys (25 hours later) still cancels, and never refunds twice', async () => {
    const db = await testDb();
    const stripe = orphan();
    stripe.calls.cancel.mockRejectedValueOnce(new Error('Stripe is down'));
    await expect(handleStripeEvent(db, stripe.api, sessionFor('evt_x'), NOW)).rejects.toThrow('Stripe is down');
    stripe.forgetKeys();
    expect(await handleStripeEvent(db, stripe.api, sessionFor('evt_x'), new Date(NOW.getTime() + 25 * 3600_000))).toBe('refunded');
    expect(stripe.calls.refunds).toHaveBeenLastCalledWith({ payment_intent: 'pi_sub_9', limit: 100 });
    expect(stripe.calls.refund).toHaveBeenCalledTimes(1);
    expect(stripe.refunded).toHaveLength(1);
    expect(stripe.subscriptions.sub_9.status).toBe('canceled');
    expect(await events(db)).toEqual(['evt_x']);
  });

  it('a cancel that went through in Stripe but whose answer was lost: the retry finds it ended, refunds nothing more', async () => {
    const db = await testDb();
    const stripe = orphan();
    const cancel = stripe.calls.cancel.getMockImplementation()!;
    stripe.calls.cancel.mockImplementationOnce(async (...args) => { await cancel(...args); throw new Error('connection reset'); });
    await expect(handleStripeEvent(db, stripe.api, sessionFor('evt_x'), NOW)).rejects.toThrow('connection reset');
    expect(await events(db)).toEqual([]);
    expect(await handleStripeEvent(db, stripe.api, sessionFor('evt_x'), NOW)).toBe('ignored');
    expect(stripe.refunded).toHaveLength(1);
    expect(stripe.calls.cancel).toHaveBeenCalledTimes(1);
    expect(await events(db)).toEqual(['evt_x']);
  });

  it('an unpaid latest invoice is not refunded; the subscription is still cancelled', async () => {
    const db = await testDb();
    const stripe = fakeStripe({ sub_9: aStripeSubscription('sub_9', 'past_due', 'gone') });
    expect(await handleStripeEvent(db, stripe.api, sessionFor('evt_x'), NOW)).toBe('refunded');
    expect(stripe.calls.refund).not.toHaveBeenCalled();
    expect(stripe.calls.cancel.mock.calls).toEqual([cancelOf('sub_9')]);
  });
});

describe('one Stripe account, several hubs', () => {
  it('an event for another hub\'s subscription is recorded and ignored, with no call to Stripe after the fetch', async () => {
    const warned = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const db = await testDb();
    const theirs = aStripeSubscription('sub_9', 'active', 'gone', { metadata: { user_id: 'gone', hub: 'https://preview.jeffhub.test' } });
    const stripe = fakeStripe({ sub_9: theirs }, aPaidInvoice('sub_9'));
    for (const type of ['checkout.session.completed', 'customer.subscription.created', 'customer.subscription.updated']) {
      const event = type === 'checkout.session.completed'
        ? stripeEvent(`evt_${type}`, type, { id: 'cs_9', object: 'checkout.session', mode: 'subscription', client_reference_id: 'gone', subscription: 'sub_9' })
        : stripeEvent(`evt_${type}`, type, theirs);
      expect(await handleStripeEvent(db, stripe.api, event, NOW), type).toBe('ignored');
    }
    expect(stripe.calls.retrieve).toHaveBeenCalledTimes(3);
    for (const call of [stripe.calls.invoicePayments, stripe.calls.refunds, stripe.calls.refund, stripe.calls.cancel, stripe.calls.list]) {
      expect(call).not.toHaveBeenCalled();
    }
    expect(await events(db)).toHaveLength(3);
    // A line naming both hubs, so a changed BETTER_AUTH_URL shows (final review M9).
    expect(String(warned.mock.calls[0][0])).toMatch(/belongs to the hub at .*, and this hub is /);
    warned.mockRestore();
  });

  it('another hub\'s subscription of a user who exists here changes nothing either', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1', 'active');
    const stripe = fakeStripe({ sub_b: aStripeSubscription('sub_b', 'active', 'u1', { metadata: { user_id: 'u1', hub: 'http://localhost:5173' } }) });
    expect(await handleStripeEvent(db, stripe.api, stripeEvent('evt_b', 'customer.subscription.created', stripe.subscriptions.sub_b), NOW)).toBe('ignored');
    expect(stripe.calls.retrieve).toHaveBeenCalledTimes(1);
    expect(stripe.calls.cancel).not.toHaveBeenCalled();
    expect((await db.select().from(subscription))[0]).toMatchObject({ stripeSubscriptionId: 'sub_u1' });
  });

  it.each([[{ user_id: 'u1' }], [{ user_id: 'u1', hub: '' }]])('a subscription with no hub (%j) is an error, and is not recorded', async (metadata) => {
    const db = await testDb();
    await aUser(db, 'u1');
    const stripe = fakeStripe({ sub_1: aStripeSubscription('sub_1', 'active', 'u1', { metadata }) });
    await expect(handleStripeEvent(db, stripe.api, stripeEvent('evt_n', 'customer.subscription.created', stripe.subscriptions.sub_1), NOW))
      .rejects.toThrow('Subscription sub_1 has no metadata.hub');
    expect(await events(db)).toEqual([]);
    expect(stripe.calls.cancel).not.toHaveBeenCalled();
  });
});

describe('refunds of a payment intent that paid several invoices', () => {
  it('refund only this invoice\'s own payment, by its amount, and a refund for another invoice does not count', async () => {
    const db = await testDb();
    // pi_shared paid 300 cents across invoices; this subscription's invoice got 100 of them, and another invoice's
    // payment was refunded already.
    const stripe = fakeStripe({ sub_9: aStripeSubscription('sub_9', 'active', 'gone') }, aPaidInvoice('sub_9', 100, 'pi_shared'));
    stripe.refunded.push({ payment_intent: 'pi_shared', amount: 200, metadata: { invoice_payment: 'inpay_other' } });
    expect(await handleStripeEvent(db, stripe.api, stripeEvent('evt_x', 'customer.subscription.updated', stripe.subscriptions.sub_9), NOW)).toBe('refunded');
    expect(stripe.calls.refund.mock.calls).toEqual([[{ payment_intent: 'pi_shared', amount: 100, reason: 'requested_by_customer',
      metadata: { invoice_payment: 'inpay_sub_9' } }, { idempotencyKey: 'hub-refund-sub_9-inpay_sub_9' }]]);
  });
});

describe('a user\'s second usable subscription', () => {
  const T = 1_793_700_000;
  const two = () => fakeStripe({
    sub_a: aStripeSubscription('sub_a', 'active', 'u1', { created: T }),
    sub_b: aStripeSubscription('sub_b', 'active', 'u1', { created: T + 10 }),
  }, { ...aPaidInvoice('sub_a'), ...aPaidInvoice('sub_b') });
  const completedFor = (event: string, sub: string) => stripeEvent(event, 'checkout.session.completed',
    { id: `cs_${sub}`, object: 'checkout.session', mode: 'subscription', client_reference_id: 'u1', subscription: sub });
  const rowOf = async (db: Awaited<ReturnType<typeof testDb>>) => (await db.select().from(subscription))[0];

  it('the later one (by Stripe\'s created) is refunded and cancelled when its event comes second', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const stripe = two();
    expect(await handleStripeEvent(db, stripe.api, completedFor('evt_a', 'sub_a'), NOW)).toBe('processed');
    expect(await handleStripeEvent(db, stripe.api, completedFor('evt_b', 'sub_b'), NOW)).toBe('refunded');
    expect(stripe.calls.refund.mock.calls).toEqual([refundOf('sub_b', 'duplicate')]);
    expect(stripe.calls.cancel.mock.calls).toEqual([cancelOf('sub_b')]);
    expect(await rowOf(db)).toMatchObject({ stripeSubscriptionId: 'sub_a', status: 'active' });
    expect(await events(db)).toEqual(['evt_a', 'evt_b']);
  });

  it('the later one is refunded and cancelled when its event comes first, and the earlier one is kept', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const stripe = two();
    expect(await handleStripeEvent(db, stripe.api, completedFor('evt_b', 'sub_b'), NOW)).toBe('processed');
    expect(await handleStripeEvent(db, stripe.api, completedFor('evt_a', 'sub_a'), NOW)).toBe('processed');
    expect(stripe.calls.cancel.mock.calls).toEqual([cancelOf('sub_b')]);
    expect(stripe.refunded).toEqual([{ payment_intent: 'pi_sub_b', amount: 100, reason: 'duplicate', metadata: { invoice_payment: 'inpay_sub_b' } }]);
    expect(await rowOf(db)).toMatchObject({ stripeSubscriptionId: 'sub_a', status: 'active' });
    // sub_b's own later events (its cancellation) change nothing more.
    expect(await handleStripeEvent(db, stripe.api, stripeEvent('evt_b2', 'customer.subscription.deleted',
      stripe.subscriptions.sub_b), NOW)).toBe('ignored');
    expect(stripe.refunded).toHaveLength(1);
    expect(await rowOf(db)).toMatchObject({ stripeSubscriptionId: 'sub_a' });
  });

  it('two Checkouts completed at the same moment: one subscription kept, the later refunded and cancelled once', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const stripe = two();
    await Promise.all([handleStripeEvent(db, stripe.api, completedFor('evt_a', 'sub_a'), NOW),
      handleStripeEvent(db, stripe.api, completedFor('evt_b', 'sub_b'), NOW)]);
    // Stripe also sends each one's created event, the same moment again.
    await Promise.all([
      handleStripeEvent(db, stripe.api, stripeEvent('evt_a2', 'customer.subscription.created', stripe.subscriptions.sub_a), NOW),
      handleStripeEvent(db, stripe.api, stripeEvent('evt_b2', 'customer.subscription.created', stripe.subscriptions.sub_b), NOW)]);
    expect(stripe.subscriptions.sub_a.status).toBe('active');
    expect(stripe.subscriptions.sub_b.status).toBe('canceled');
    expect(stripe.refunded).toEqual([{ payment_intent: 'pi_sub_b', amount: 100, reason: 'duplicate', metadata: { invoice_payment: 'inpay_sub_b' } }]);
    expect(await rowOf(db)).toMatchObject({ stripeSubscriptionId: 'sub_a', status: 'active' });
  });

  it('a failed cancel records nothing; the retried event repeats nothing it already did', async () => {
    const db = await testDb();
    await aUser(db, 'u1');
    const stripe = two();
    await handleStripeEvent(db, stripe.api, completedFor('evt_a', 'sub_a'), NOW);
    stripe.calls.cancel.mockRejectedValueOnce(new Error('Stripe is down'));
    await expect(handleStripeEvent(db, stripe.api, completedFor('evt_b', 'sub_b'), NOW)).rejects.toThrow('Stripe is down');
    expect(await events(db)).toEqual(['evt_a']);
    expect(await handleStripeEvent(db, stripe.api, completedFor('evt_b', 'sub_b'), NOW)).toBe('refunded');
    expect(stripe.refunded).toHaveLength(1);
    expect(stripe.subscriptions.sub_b.status).toBe('canceled');
  });

  it('a row whose subscription has ended in Stripe is replaced, nothing refunded', async () => {
    const db = await testDb();
    await aSubscriber(db, 'u1', 'active');  // the row says sub_u1 is active; Stripe has it cancelled
    const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'canceled', 'u1'), sub_b: aStripeSubscription('sub_b', 'active', 'u1') });
    expect(await handleStripeEvent(db, stripe.api, completedFor('evt_b', 'sub_b'), NOW)).toBe('processed');
    expect(stripe.calls.refund).not.toHaveBeenCalled();
    expect(stripe.calls.cancel).not.toHaveBeenCalled();
    expect(await rowOf(db)).toMatchObject({ stripeSubscriptionId: 'sub_b', status: 'active' });
  });
});

// Stripe-Signature as Stripe writes it: t=<seconds>, then one v1=<HMAC-SHA256 of "t.body"> per signing secret.
const v1 = (timestamp: number, payload: string, secret: string) =>
  createHmac('sha256', secret).update(`${timestamp}.${payload}`).digest('hex');

describe('the signature header', () => {
  const event = stripeEvent('evt_sig', 'invoice.paid', { id: 'in_1', object: 'invoice' });
  const payload = JSON.stringify(event);
  const send = async (signature: string) => {
    const db = await testDb();
    const request = new Request('https://jeffhub.ai/api/stripe/webhook', { method: 'POST',
      headers: { 'stripe-signature': signature }, body: payload });
    return stripeWebhook(request, { db, stripe: fakeStripe({}).api, webhookSecret: SECRET, now: NOW, hub: HUB });
  };
  const nowSeconds = () => Math.floor(Date.now() / 1000);

  it('refuses a signature made more than 5 minutes ago', async () => {
    const old = nowSeconds() - 301;
    const response = await send(`t=${old},v1=${v1(old, payload, SECRET)}`);
    expect(response.status).toBe(400);
    expect((await response.json()).error.message).toMatch(/tolerance/i);
  });

  it('accepts a header with several v1 signatures when one of them matches', async () => {
    const t = nowSeconds();
    const response = await send(`t=${t},v1=${v1(t, payload, 'whsec_rolled_away')},v1=${v1(t, payload, SECRET)}`);
    expect(response.status).toBe(200);
  });

  it('refuses a header with several v1 signatures when none matches', async () => {
    const t = nowSeconds();
    const response = await send(`t=${t},v1=${v1(t, payload, 'whsec_a')},v1=${v1(t, payload, 'whsec_b')}`);
    expect(response.status).toBe(400);
  });
});

// Through the hub's one function (api/index.ts and the router), with the settings from the environment. Only an
// event type the hub ignores is sent, so the real Stripe client the endpoint makes is never called.
describe('POST /api/stripe/webhook through the function', () => {
  let database = '';
  beforeAll(async () => {
    database = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-webhook-db-'));
    const client = new PGlite(database);
    await migrate(drizzle(client), { migrationsFolder: path.join(import.meta.dirname, '../drizzle') });
    await client.close();
  });
  beforeEach(() => {
    for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value);
    vi.stubEnv('DATABASE_URL', `pglite:${database}`);
  });
  afterEach(() => vi.unstubAllEnvs());

  // Laid out the way Stripe sends it (indented, with newlines), which JSON.parse then JSON.stringify would change:
  // the signature holds only over these exact bytes.
  const payload = (id: string) => JSON.stringify(stripeEvent(id, 'invoice.paid', { id: 'in_1', object: 'invoice' }), null, 2);
  const post = (body: string, signature: string) => api.fetch(new Request('https://jeffhub.ai/api/stripe/webhook', {
    method: 'POST', headers: { 'stripe-signature': signature, 'content-type': 'application/json' }, body }));

  it('checks the signature over the raw body, as sent', async () => {
    const body = payload('evt_raw');
    const response = await post(body, signatureFor(body, TEST_ENV.STRIPE_WEBHOOK_SECRET));
    expect(response.status).toBe(200);
    expect(response.headers.get('cache-control')).toBe('no-store');
    expect(await response.json()).toEqual({ received: 'evt_raw', result: 'ignored' });
  });

  it('refuses a body changed in one byte, with the hub\'s error shape', async () => {
    const body = payload('evt_byte');
    const signature = signatureFor(body, TEST_ENV.STRIPE_WEBHOOK_SECRET);
    const at = body.indexOf('in_1') + 3;
    const changed = `${body.slice(0, at)}2${body.slice(at + 1)}`;
    expect(changed.length).toBe(body.length);
    const response = await post(changed, signature);
    expect(response.status).toBe(400);
    expect(response.headers.get('cache-control')).toBe('no-store');
    const { error } = await response.json();
    expect(error.type).toBe('WebhookRefused');
    expect(error.message).toContain('The Stripe signature does not match this body');
  });

  it('refuses a body signed with another secret', async () => {
    const body = payload('evt_other');
    expect((await post(body, signatureFor(body, 'whsec_someone_else'))).status).toBe(400);
  });

  it('works with the mail settings removed and a sign-in method listed without its settings (final review I7)', async () => {
    vi.stubEnv('MAIL_TRANSPORT', '');
    vi.stubEnv('MAIL_FROM', '');
    vi.stubEnv('SIGN_IN_PROVIDERS', 'github,google,email');
    vi.stubEnv('GOOGLE_CLIENT_ID', '');
    vi.stubEnv('TURNSTILE_SECRET_KEY', '');
    const body = payload('evt_no_mail');
    const response = await post(body, signatureFor(body, TEST_ENV.STRIPE_WEBHOOK_SECRET));
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ received: 'evt_no_mail', result: 'ignored' });
  });

  it('still names a missing setting of its own', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.stubEnv('STRIPE_WEBHOOK_SECRET', '');
    const body = payload('evt_no_secret');
    const response = await post(body, signatureFor(body, TEST_ENV.STRIPE_WEBHOOK_SECRET));
    expect(response.status).toBe(500);
    expect((await response.json()).error).toMatchObject({ type: 'MissingSetting', details: { missing: ['STRIPE_WEBHOOK_SECRET'] } });
  });

  it('answers only POST', async () => {
    const response = await api.fetch(new Request('https://jeffhub.ai/api/stripe/webhook'));
    expect(response.status).toBe(405);
  });
});
