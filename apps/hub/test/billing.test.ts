// Paying (spec Section 4): Stripe Checkout for the one plan, the Customer Portal, and deleting an account (a fake
// Stripe here; no network).
import { expect, it } from 'vitest';
import { apiKey, stripeCustomer, subscription, usage, user } from '../app/lib/schema';
import { ALREADY_SUBSCRIBED, BillingRefused, checkoutUrl, deleteAccount, PAY_IN_PORTAL, portalUrl, RESUME_IN_PORTAL } from '../app/lib/billing.server';
import { createKey } from '../app/lib/keys.server';
import { testDb } from './helpers/db';
import { aSubscriber, aUser, NOW, PERIOD } from './helpers/accounts';
import { aStripeSubscription, fakeStripe } from './helpers/stripe';
import { testSettings } from './helpers/settings';

// The user has no data sets: deleting the account deletes no zips (test/adapter-request-endpoints.test.ts has those).
const noDataSets = { list: async () => [], remove: async (pathnames: string[]) => { if (pathnames.length > 0) throw new Error(`unexpected data sets ${pathnames.join(', ')}`); } };

const ADA = { id: 'u1', email: 'u1@example.com', emailVerified: true, name: 'Ada', image: null };

const deps = (db: Awaited<ReturnType<typeof testDb>>, stripe: ReturnType<typeof fakeStripe>, settings = testSettings()) =>
  ({ db, stripe: stripe.api, settings });
const cancelKey = (id: string) => ({ idempotencyKey: `hub-cancel-${id}` });

it('opens Checkout for one Price, for the user\'s own new Stripe customer, coming back to the return page', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  const stripe = fakeStripe({});
  const url = await checkoutUrl(deps(db, stripe), ADA);
  expect(url).toBe('https://checkout.stripe.test/u1');
  expect(stripe.calls.customer).toHaveBeenCalledWith({ email: 'u1@example.com', metadata: { user_id: 'u1' } },
    { idempotencyKey: 'hub-customer-u1' });
  expect(await db.select().from(stripeCustomer)).toEqual([{ userId: 'u1', customerId: 'cus_u1', createdAt: expect.any(Date) }]);
  expect(stripe.calls.checkout.mock.calls[0][0]).toEqual({
    mode: 'subscription',
    line_items: [{ price: 'price_test', quantity: 1 }],
    client_reference_id: 'u1',
    customer: 'cus_u1',
    subscription_data: { metadata: { user_id: 'u1', hub: 'https://jeffhub.ai' } },
    automatic_tax: { enabled: false },
    billing_address_collection: 'auto',
    success_url: 'https://jeffhub.ai/account/return?session_id={CHECKOUT_SESSION_ID}',
    cancel_url: 'https://jeffhub.ai/account',
  });
});

it('with Stripe Tax on, asks for the address; a returning customer keeps their Stripe customer', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'canceled');
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'canceled', 'u1') });
  await checkoutUrl(deps(db, stripe, testSettings({ stripe: { ...testSettings().stripe, automaticTax: true } })), ADA);
  expect(stripe.calls.customer).not.toHaveBeenCalled();
  expect(stripe.calls.checkout.mock.calls[0][0]).toMatchObject({ customer: 'cus_u1', customer_update: { address: 'auto' },
    automatic_tax: { enabled: true }, billing_address_collection: 'required' });
  expect(stripe.calls.checkout.mock.calls[0][0]).not.toHaveProperty('customer_email');
  expect(stripe.calls.cancel).not.toHaveBeenCalled();
});

it('two Checkouts started at once make one Stripe customer, used by both', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  const stripe = fakeStripe({});
  await Promise.all([checkoutUrl(deps(db, stripe), ADA), checkoutUrl(deps(db, stripe), ADA)]);
  expect(stripe.calls.customer).toHaveBeenCalledTimes(1);
  expect(await db.select().from(stripeCustomer)).toHaveLength(1);
  expect(stripe.calls.checkout.mock.calls.map(([params]) => params.customer)).toEqual(['cus_u1', 'cus_u1']);
});

// PGlite runs one transaction at a time, so the test above cannot show the lock working; this pins that the lock is
// taken before the customer row is read, which is what makes it work on Postgres.
it('locks the user before it looks for their customer', async () => {
  const steps: string[] = [];
  const db = await testDb((query) => {
    if (/pg_advisory_xact_lock/.test(query)) steps.push('lock');
    else if (/from "stripe_customer"/.test(query)) steps.push('read');
    else if (/^insert into "stripe_customer"/.test(query)) steps.push('write');
  });
  await aUser(db, 'u1');
  steps.length = 0;
  await checkoutUrl(deps(db, fakeStripe({})), ADA);
  expect(steps).toEqual(['lock', 'read', 'write']);
});

it.each(['active', 'trialing', 'past_due'] as const)('refuses a second subscription while one is %s in Stripe', async (status) => {
  const db = await testDb();
  await aUser(db, 'u1');
  const stripe = fakeStripe({ sub_x: aStripeSubscription('sub_x', status, 'u1') });
  await expect(checkoutUrl(deps(db, stripe), ADA)).rejects.toThrow(new BillingRefused(ALREADY_SUBSCRIBED));
  expect(stripe.calls.checkout).not.toHaveBeenCalled();
});

it('an unpaid subscription is paid in the portal, not replaced', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'unpaid');
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'unpaid', 'u1') });
  await expect(checkoutUrl(deps(db, stripe), ADA)).rejects.toThrow(new BillingRefused(PAY_IN_PORTAL));
  expect(PAY_IN_PORTAL).toContain('Manage billing');
  expect(stripe.calls.checkout).not.toHaveBeenCalled();
});

it('a paused subscription is resumed in the portal, not replaced', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'paused');
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'paused', 'u1') });
  await expect(checkoutUrl(deps(db, stripe), ADA)).rejects.toThrow(new BillingRefused(RESUME_IN_PORTAL));
  expect(RESUME_IN_PORTAL).toContain('Manage billing');
  expect(stripe.calls.checkout).not.toHaveBeenCalled();
  expect(stripe.calls.cancel).not.toHaveBeenCalled();
});

it('deleting an account cancels a paused subscription', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'paused');
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'paused', 'u1') });
  await deleteAccount(deps(db, stripe), 'u1', noDataSets);
  expect(stripe.calls.cancel.mock.calls).toEqual([['sub_u1', {}, cancelKey('sub_u1')]]);
  expect(await db.select().from(user)).toEqual([]);
});

it('an unfinished (incomplete) subscription is cancelled in Stripe before the new Checkout', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'incomplete');
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'incomplete', 'u1'),
    sub_old: aStripeSubscription('sub_old', 'incomplete_expired', 'u1') });
  await checkoutUrl(deps(db, stripe), ADA);
  expect(stripe.calls.cancel.mock.calls).toEqual([['sub_u1', {}, cancelKey('sub_u1')]]);
  expect(stripe.calls.cancel.mock.invocationCallOrder[0]).toBeLessThan(stripe.calls.checkout.mock.invocationCallOrder[0]);
});

it('a failure to cancel the unfinished one opens no Checkout', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'incomplete');
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'incomplete', 'u1') });
  stripe.calls.cancel.mockRejectedValueOnce(new Error('Stripe is down'));
  await expect(checkoutUrl(deps(db, stripe), ADA)).rejects.toThrow('Stripe is down');
  expect(stripe.calls.checkout).not.toHaveBeenCalled();
});

it("opens the Customer Portal for the user's Stripe customer, and refuses before any payment", async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1');
  await aUser(db, 'u2');
  const stripe = fakeStripe({});
  expect(await portalUrl(deps(db, stripe), 'u1')).toBe('https://billing.stripe.test/cus_u1');
  expect(stripe.calls.portal.mock.calls[0][0]).toEqual({ customer: 'cus_u1', return_url: 'https://jeffhub.ai/account' });
  await expect(portalUrl(deps(db, stripe), 'u2')).rejects.toThrow('subscribe first');
});

it('deleting an account cancels in Stripe every subscription that has not ended, then deletes the user, keys and usage', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'active');
  await createKey(db, 'u1', 'k', NOW);
  await db.insert(usage).values({ accountId: 'u1', periodStart: PERIOD.start, inputTokens: 5, updatedAt: NOW });
  // Stripe knows one more than the hub's row: a Checkout finished whose webhook has not arrived yet.
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'active', 'u1'),
    sub_late: aStripeSubscription('sub_late', 'incomplete', 'u1'), sub_done: aStripeSubscription('sub_done', 'canceled', 'u1'),
    sub_other: aStripeSubscription('sub_other', 'active', 'u2') });
  await deleteAccount(deps(db, stripe), 'u1', noDataSets);
  expect(stripe.calls.list).toHaveBeenCalledWith({ customer: 'cus_u1', status: 'all', limit: 100 });
  expect(stripe.calls.cancel.mock.calls).toEqual([['sub_u1', {}, cancelKey('sub_u1')], ['sub_late', {}, cancelKey('sub_late')]]);
  for (const table of [user, subscription, apiKey, usage, stripeCustomer]) expect(await db.select().from(table)).toEqual([]);
});

it('deleting an account first expires its open Checkouts, so none can be paid afterwards', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  const stripe = fakeStripe({});
  await checkoutUrl(deps(db, stripe), ADA);
  await checkoutUrl(deps(db, stripe), ADA);
  stripe.sessions.push({ id: 'cs_paid', customer: 'cus_u1', status: 'complete' }, { id: 'cs_theirs', customer: 'cus_u2', status: 'open' });
  stripe.subscriptions.sub_u1 = aStripeSubscription('sub_u1', 'incomplete', 'u1');
  await deleteAccount(deps(db, stripe), 'u1', noDataSets);
  expect(stripe.calls.checkouts).toHaveBeenCalledWith({ customer: 'cus_u1', status: 'open', limit: 100 });
  expect(stripe.calls.expire.mock.calls).toEqual([['cs_test_1'], ['cs_test_2']]);
  expect(stripe.sessions.map((session) => session.status)).toEqual(['expired', 'expired', 'complete', 'open']);
  expect(stripe.calls.expire.mock.invocationCallOrder[1]).toBeLessThan(stripe.calls.cancel.mock.invocationCallOrder[0]);
  expect(await db.select().from(user)).toEqual([]);
});

it('a failure to expire a Checkout deletes nothing and cancels nothing', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  const stripe = fakeStripe({});
  await checkoutUrl(deps(db, stripe), ADA);
  stripe.subscriptions.sub_u1 = aStripeSubscription('sub_u1', 'active', 'u1');
  stripe.calls.expire.mockRejectedValueOnce(new Error('Stripe is down'));
  await expect(deleteAccount(deps(db, stripe), 'u1', noDataSets)).rejects.toThrow('Stripe is down');
  expect(stripe.calls.cancel).not.toHaveBeenCalled();
  expect(await db.select().from(user)).toHaveLength(1);
});

it('a failure to cancel in Stripe deletes nothing, and deleting again finishes it', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'active');
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'active', 'u1') });
  stripe.calls.cancel.mockRejectedValueOnce(new Error('Stripe is down'));
  await expect(deleteAccount(deps(db, stripe), 'u1', noDataSets)).rejects.toThrow('Stripe is down');
  expect(await db.select().from(user)).toHaveLength(1);
  await deleteAccount(deps(db, stripe), 'u1', noDataSets);
  expect(stripe.subscriptions.sub_u1.status).toBe('canceled');
  expect(await db.select().from(user)).toEqual([]);
});

it('an already ended subscription is not cancelled again', async () => {
  const db = await testDb();
  await aSubscriber(db, 'u1', 'canceled');
  const stripe = fakeStripe({ sub_u1: aStripeSubscription('sub_u1', 'canceled', 'u1') });
  await deleteAccount(deps(db, stripe), 'u1', noDataSets);
  expect(stripe.calls.cancel).not.toHaveBeenCalled();
  expect(await db.select().from(user)).toEqual([]);
});

it('a user who never started a Checkout is deleted without asking Stripe', async () => {
  const db = await testDb();
  await aUser(db, 'u1');
  const stripe = fakeStripe({});
  await deleteAccount(deps(db, stripe), 'u1', noDataSets);
  expect(stripe.calls.list).not.toHaveBeenCalled();
  expect(await db.select().from(user)).toEqual([]);
});
