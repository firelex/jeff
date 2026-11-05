// The plan's price and allowance as the pages say them, for loadDocs's {{price}} and {{allowance}} (the values the
// e2e build and test/helpers/settings.ts set: PLAN_PRICE_USD 1, PLAN_ALLOWANCE_INPUT_TOKENS 50000000).
export const PLAN = { price: '$1', allowance: '50 million' };

// The public settings site.server.ts's docs() reads the plan from (public-settings.server.ts), stubbed with the
// values PLAN stands for.
export const PLAN_ENV = { PLAN_PRICE_USD: '1', PLAN_ALLOWANCE_INPUT_TOKENS: '50000000', TURNSTILE_SITE_KEY: 'site-test',
  PLAYGROUND_RUNS_PER_MINUTE: '5', STRIPE_AUTOMATIC_TAX: 'off', SIGN_IN_PROVIDERS: 'github,google,email' } as const;
