import Stripe from 'stripe';
import { vi } from 'vitest';
import type { StripeApi } from '../../app/lib/stripe.server';

export const PERIOD_START = 1_793_700_000; // 2026-11-03, in Unix seconds
export const PERIOD_END = PERIOD_START + 30 * 24 * 3600;
// The hub's address in the tests (testSettings().baseUrl, TEST_ENV's BETTER_AUTH_URL): Checkout's metadata.hub.
export const HUB = 'https://jeffhub.ai';

export function aStripeSubscription(id: string, status: Stripe.Subscription.Status, userId: string,
  changes: Partial<Stripe.Subscription> = {}): Stripe.Subscription {
  return {
    id, object: 'subscription', status, customer: `cus_${userId}`, cancel_at_period_end: false, created: PERIOD_START,
    latest_invoice: `in_${id}`, metadata: { user_id: userId, hub: HUB },
    items: { object: 'list', data: [{ current_period_start: PERIOD_START, current_period_end: PERIOD_END }] },
    ...changes,
  } as unknown as Stripe.Subscription;
}

// The paid payment of a subscription's first invoice (in_<subscription id>), by payment intent: $1 (100 cents), or
// `amount`.
export function aPaidInvoice(subscriptionId: string, amount = 100, paymentIntent = `pi_${subscriptionId}`): Record<string, Stripe.InvoicePayment[]> {
  return { [`in_${subscriptionId}`]: [{ id: `inpay_${subscriptionId}`, object: 'invoice_payment', status: 'paid', amount_paid: amount,
    invoice: `in_${subscriptionId}`, payment: { type: 'payment_intent', payment_intent: paymentIntent } } as unknown as Stripe.InvoicePayment] };
}

// Stripe as the hub calls it, with no network: subscriptions answered from `subscriptions` (a cancel changes the
// status there), invoice payments from `payments`, every call recorded. Like Stripe, a call repeated with an
// idempotency key it has seen answers what the first did and changes nothing: `refunded` holds each refund made once.
export function fakeStripe(subscriptions: Record<string, Stripe.Subscription>,
  payments: Record<string, Stripe.InvoicePayment[]> = {}) {
  const seen = new Map<string, unknown>();
  const once = async <T>(key: string, work: () => T): Promise<T> => {
    if (!seen.has(key)) seen.set(key, work());
    return seen.get(key) as T;
  };
  const refunded: Stripe.RefundCreateParams[] = [];
  // Checkout sessions made, as Stripe keeps them: open until expired.
  const sessions: { id: string; customer: string | undefined; status: string }[] = [];
  // Stripe forgets an idempotency key after 24 hours: forgetKeys() is that time passing.
  const forgetKeys = () => seen.clear();
  const calls = {
    customer: vi.fn(async (params: Stripe.CustomerCreateParams, options: Stripe.RequestOptions) =>
      once(`customer ${options.idempotencyKey}`, () => ({ id: `cus_${(params.metadata as Record<string, string>).user_id}` }))),
    checkout: vi.fn(async (params: Stripe.Checkout.SessionCreateParams) => {
      const id = `cs_test_${sessions.length + 1}`;
      sessions.push({ id, customer: params.customer, status: 'open' });
      return { id, url: `https://checkout.stripe.test/${params.client_reference_id}` };
    }),
    checkouts: vi.fn(async (params: Stripe.Checkout.SessionListParams) => ({ has_more: false,
      data: sessions.filter((session) => session.customer === params.customer && session.status === params.status) })),
    expire: vi.fn(async (id: string) => {
      const found = sessions.find((session) => session.id === id);
      if (found === undefined || found.status !== 'open') throw new Error(`fake Stripe has no open Checkout session ${id}`);
      found.status = 'expired';
      return found;
    }),
    portal: vi.fn(async (params: Stripe.BillingPortal.SessionCreateParams) => ({ url: `https://billing.stripe.test/${params.customer}` })),
    retrieve: vi.fn(async (id: string) => {
      const found = subscriptions[id];
      if (found === undefined) throw new Error(`fake Stripe has no subscription ${id}`);
      return found;
    }),
    list: vi.fn(async (params: Stripe.SubscriptionListParams) => ({
      data: Object.values(subscriptions).filter((s) => s.customer === params.customer), has_more: false })),
    cancel: vi.fn(async (id: string, _params: Stripe.SubscriptionCancelParams, options: Stripe.RequestOptions) =>
      once(`cancel ${options.idempotencyKey}`, () => {
        const found = subscriptions[id];
        if (found === undefined) throw new Error(`fake Stripe has no subscription ${id}`);
        if (found.status === 'canceled') throw new Error(`fake Stripe: subscription ${id} is already canceled`);
        subscriptions[id] = { ...found, status: 'canceled' };
        return subscriptions[id];
      })),
    invoicePayments: vi.fn(async (params: Stripe.InvoicePaymentListParams) => ({
      data: (payments[params.invoice!] ?? []).filter((p) => p.status === params.status), has_more: false })),
    refunds: vi.fn(async (params: Stripe.RefundListParams) => ({ has_more: false,
      data: refunded.flatMap((made, i) => (params.payment_intent !== undefined && made.payment_intent === params.payment_intent)
        || (params.charge !== undefined && made.charge === params.charge) ? [{ id: `re_${i + 1}`, status: 'succeeded', metadata: made.metadata ?? null }] : []) })),
    refund: vi.fn(async (params: Stripe.RefundCreateParams, options: Stripe.RequestOptions) =>
      once(`refund ${options.idempotencyKey}`, () => { refunded.push(params); return { id: `re_${refunded.length}` }; })),
  };
  const api = {
    customers: { create: calls.customer },
    checkout: { sessions: { create: calls.checkout, list: calls.checkouts, expire: calls.expire } },
    billingPortal: { sessions: { create: calls.portal } },
    subscriptions: { retrieve: calls.retrieve, list: calls.list, cancel: calls.cancel },
    invoicePayments: { list: calls.invoicePayments },
    refunds: { create: calls.refund, list: calls.refunds },
  } as unknown as StripeApi;
  return { api, calls, subscriptions, refunded, forgetKeys, sessions };
}

export function stripeEvent(id: string, type: string, object: object, created = PERIOD_START): Stripe.Event {
  return { id, object: 'event', type, created, data: { object } } as unknown as Stripe.Event;
}

// A Stripe-Signature header for `payload`, made the way Stripe makes them (Stripe's own test helper; no network).
export function signatureFor(payload: string, secret: string): string {
  return Stripe.webhooks.generateTestHeaderString({ payload, secret });
}

export function signedWebhook(event: Stripe.Event, secret: string, tamper = false): Request {
  const payload = JSON.stringify(event);
  return new Request('https://jeffhub.ai/api/stripe/webhook', {
    method: 'POST', headers: { 'stripe-signature': signatureFor(payload, secret), 'content-type': 'application/json' },
    body: tamper ? payload.replace('"type"', '"type" ') : payload,
  });
}
