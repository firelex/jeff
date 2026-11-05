import Stripe from 'stripe';

// The part of Stripe the hub calls; tests pass a fake with the same shape (test/helpers/stripe.ts).
export interface StripeApi {
  customers: { create(params: Stripe.CustomerCreateParams, options: Stripe.RequestOptions): Promise<{ id: string }> };
  checkout: {
    sessions: {
      create(params: Stripe.Checkout.SessionCreateParams): Promise<{ id: string; url: string | null }>;
      list(params: Stripe.Checkout.SessionListParams): Promise<{ data: { id: string; status: string | null }[]; has_more: boolean }>;
      expire(id: string): Promise<{ id: string }>;
    };
  };
  billingPortal: { sessions: { create(params: Stripe.BillingPortal.SessionCreateParams): Promise<{ url: string }> } };
  subscriptions: {
    retrieve(id: string): Promise<Stripe.Subscription>;
    list(params: Stripe.SubscriptionListParams): Promise<{ data: Stripe.Subscription[]; has_more: boolean }>;
    cancel(id: string, params: Stripe.SubscriptionCancelParams, options: Stripe.RequestOptions): Promise<Stripe.Subscription>;
  };
  invoicePayments: {
    list(params: Stripe.InvoicePaymentListParams): Promise<{ data: Stripe.InvoicePayment[]; has_more: boolean }>;
  };
  refunds: {
    create(params: Stripe.RefundCreateParams, options: Stripe.RequestOptions): Promise<{ id: string }>;
    list(params: Stripe.RefundListParams): Promise<{ data: { id: string; status: string | null; metadata: Stripe.Metadata | null }[]; has_more: boolean }>;
  };
}

// How long one call to Stripe may take, and no retries by the SDK. The webhook calls Stripe inside a database
// transaction (stripe-webhook.server.ts), so the SDK's default of 80 seconds and two retries would hold a pooled
// connection far too long; 10 seconds is ample for Stripe's API. A call that fails fails the request: the customer
// tries again, and Stripe sends a webhook again.
export const STRIPE_TIMEOUT_MS = 10_000;
export const STRIPE_RETRIES = 0;

export function stripeApi(secretKey: string): StripeApi {
  return new Stripe(secretKey, { timeout: STRIPE_TIMEOUT_MS, maxNetworkRetries: STRIPE_RETRIES });
}

// Subscription statuses that are over for good: Stripe cannot cancel them, and they never bill again.
export const ENDED_STATUSES: readonly string[] = ['canceled', 'incomplete_expired'];

export function hasEnded(status: string): boolean {
  return ENDED_STATUSES.includes(status);
}

// Every subscription of a Stripe customer, whatever its status. A hub customer has a handful at most; more than one
// page of 100 is an error, never a silently shortened list.
export async function subscriptionsOf(stripe: StripeApi, customerId: string): Promise<Stripe.Subscription[]> {
  const page = await stripe.subscriptions.list({ customer: customerId, status: 'all', limit: 100 });
  if (page.has_more) throw new Error(`Stripe customer ${customerId} has more than 100 subscriptions; the hub expects a handful`);
  return page.data;
}

// The customer's Checkout sessions still open (not completed, not expired); more than one page of 100 is an error.
export async function openCheckoutsOf(stripe: StripeApi, customerId: string): Promise<{ id: string }[]> {
  const page = await stripe.checkout.sessions.list({ customer: customerId, status: 'open', limit: 100 });
  if (page.has_more) throw new Error(`Stripe customer ${customerId} has more than 100 open Checkout sessions`);
  return page.data;
}

// Cancels a subscription now. The idempotency key is the subscription's, so a repeated call (a webhook Stripe sends
// again, a deletion tried again) repeats nothing within Stripe's 24 hours for keys.
export async function cancelSubscription(stripe: StripeApi, subscriptionId: string): Promise<void> {
  await stripe.subscriptions.cancel(subscriptionId, {}, { idempotencyKey: `hub-cancel-${subscriptionId}` });
}

// A subscription that must not bill (one whose account is gone, or a user's second one): every paid payment of its
// latest invoice is refunded (unless it has a refund already), then it is cancelled, in that order, so a failure part
// way is finished by the retry (a subscription already cancelled was refunded first). Every call has an idempotency
// key from the subscription's id.
export async function refundAndCancel(stripe: StripeApi, current: Stripe.Subscription,
  reason: Stripe.RefundCreateParams.Reason): Promise<void> {
  if (current.latest_invoice !== null) {
    const invoiceId = typeof current.latest_invoice === 'string' ? current.latest_invoice : current.latest_invoice.id;
    const payments = await stripe.invoicePayments.list({ invoice: invoiceId, status: 'paid', limit: 100 });
    if (payments.has_more) throw new Error(`Invoice ${invoiceId} has more than 100 paid payments`);
    for (const payment of payments.data) {
      const { payment_intent: intent, charge } = payment.payment;
      const target = intent !== undefined ? { payment_intent: typeof intent === 'string' ? intent : intent.id }
        : charge !== undefined ? { charge: typeof charge === 'string' ? charge : charge.id }
        : null;
      if (target === null) {
        throw new Error(`Payment ${payment.id} of invoice ${invoiceId} has neither a payment intent nor a charge to refund`);
      }
      if (payment.amount_paid === null) throw new Error(`Payment ${payment.id} of invoice ${invoiceId} is paid but has no amount_paid`);
      // Stripe keeps idempotency keys for 24 hours and sends a failed webhook again for up to 3 days, so the key alone
      // cannot stop a second refund: a payment with a refund of its own already (one this hub made for this invoice
      // payment, by its metadata, that has not failed or been cancelled) is skipped. One payment intent can pay
      // several invoices, so a refund for another of them does not count, and each refund is of this payment's own
      // amount, never the whole payment intent.
      const refunds = await stripe.refunds.list({ ...target, limit: 100 });
      if (refunds.has_more) throw new Error(`Payment ${payment.id} of invoice ${invoiceId} has more than 100 refunds`);
      if (refunds.data.some((refund) => refund.metadata?.invoice_payment === payment.id
        && refund.status !== 'failed' && refund.status !== 'canceled')) continue;
      await stripe.refunds.create({ ...target, amount: payment.amount_paid, reason, metadata: { invoice_payment: payment.id } },
        { idempotencyKey: `hub-refund-${current.id}-${payment.id}` });
    }
  }
  await cancelSubscription(stripe, current.id);
}

export class WebhookRefused extends Error {}

// Checks Stripe's signature over the raw body, exactly as it arrived (it needs no network or API key, only the
// webhook secret). Stripe's own check refuses a signature older than five minutes.
export function verifyEvent(rawBody: string, signature: string | null, webhookSecret: string): Stripe.Event {
  if (signature === null) throw new WebhookRefused('The request has no Stripe-Signature header');
  try {
    return Stripe.webhooks.constructEvent(rawBody, signature, webhookSecret);
  } catch (error) {
    if (!(error instanceof Stripe.errors.StripeSignatureVerificationError)) throw error;
    throw new WebhookRefused(`The Stripe signature does not match this body: ${error.message}`);
  }
}
