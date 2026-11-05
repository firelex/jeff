// POST /api/stripe/webhook: Stripe's events about the hub's subscriptions (app/lib/stripe-webhook.server.ts checks
// the signature, records the event id and stores each subscription as Stripe has it now). The handler reads the
// request's body itself, as raw text: the function's fetch handler (api/index.ts) passes the body on untouched.
import { WEBHOOK_SETTING_NAMES, webhookSettings } from '../../app/lib/accounts-settings.server.js';
import { getDb } from '../../app/lib/db.server.js';
import { stripeApi } from '../../app/lib/stripe.server.js';
import { stripeWebhook } from '../../app/lib/stripe-webhook.server.js';
import { defineEndpoint } from '../endpoint.js';

export const stripeWebhookEndpoint = defineEndpoint({
  // Only the webhook's own settings (final review I7): a wrong sign-in or mail setting cannot stop Stripe's events.
  settings: WEBHOOK_SETTING_NAMES,
  methods: {
    POST: async (request) => {
      const settings = webhookSettings();
      return stripeWebhook(request, { db: await getDb(), stripe: stripeApi(settings.stripe.secretKey),
        webhookSecret: settings.stripe.webhookSecret, now: new Date(), hub: settings.baseUrl });
    },
  },
});
