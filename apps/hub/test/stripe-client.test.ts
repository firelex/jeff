// @vitest-environment node
// The Stripe client the hub's function builds: a short timeout per call, never the SDK's default of 80 seconds, and
// no retries (the SDK's default is 2).
// Nothing here reaches Stripe: building the client makes no request.
import Stripe from 'stripe';
import { expect, it } from 'vitest';
import { STRIPE_RETRIES, STRIPE_TIMEOUT_MS, stripeApi } from '../app/lib/stripe.server';

it('gives every call to Stripe a 10 second timeout', () => {
  expect(STRIPE_TIMEOUT_MS).toBe(10_000);
  expect((stripeApi('sk_test_unused') as Stripe).getApiField('timeout')).toBe(10_000);
});

it('never retries a call itself', () => {
  expect(STRIPE_RETRIES).toBe(0);
  expect((stripeApi('sk_test_unused') as Stripe).getApiField('maxNetworkRetries')).toBe(0);
});
