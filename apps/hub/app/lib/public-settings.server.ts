import { SIGN_IN_PROVIDER_NAMES, type SignInProvider } from './auth-shared.js';
import { oneOf, positiveInteger, positiveNumber, requireEnv } from './env.server.js';

// The settings the prerendered pages show (pricing, playground), so they are read at build time as well as at run
// time. Price and allowance are settings, never constants (spec Section 1); they must match the Stripe Price.
// STRIPE_AUTOMATIC_TAX is one of them because the pricing page says whether tax is added on top of the price, and
// SIGN_IN_PROVIDERS because the sign-in page shows only the listed methods' buttons. On Vercel one setting serves
// both reads: a deployment's build and its function get the same environment, and changing a setting takes a new
// deployment. The sign-in page still checks at run time that the function's list is the one it was built with
// (GET /api/signin-options answers it), and says so when they differ.
export const PUBLIC_SETTING_NAMES = ['PLAN_PRICE_USD', 'PLAN_ALLOWANCE_INPUT_TOKENS', 'TURNSTILE_SITE_KEY',
  'PLAYGROUND_RUNS_PER_MINUTE', 'STRIPE_AUTOMATIC_TAX', 'SIGN_IN_PROVIDERS'] as const;

export interface PublicSettings {
  priceUsd: number;
  allowanceInputTokens: number;
  turnstileSiteKey: string;
  playgroundRunsPerMinute: number;
  // Stripe adds VAT or sales tax to the price at Checkout (on), or charges the price alone (off).
  automaticTax: boolean;
  // The sign-in methods offered, in the order SIGN_IN_PROVIDERS lists them.
  signInProviders: SignInProvider[];
}

export function publicSettings(): PublicSettings {
  const env = requireEnv(PUBLIC_SETTING_NAMES);
  return {
    priceUsd: positiveNumber('PLAN_PRICE_USD', env.PLAN_PRICE_USD),
    allowanceInputTokens: positiveInteger('PLAN_ALLOWANCE_INPUT_TOKENS', env.PLAN_ALLOWANCE_INPUT_TOKENS),
    turnstileSiteKey: env.TURNSTILE_SITE_KEY,
    playgroundRunsPerMinute: positiveInteger('PLAYGROUND_RUNS_PER_MINUTE', env.PLAYGROUND_RUNS_PER_MINUTE),
    automaticTax: oneOf('STRIPE_AUTOMATIC_TAX', env.STRIPE_AUTOMATIC_TAX, ['on', 'off'] as const) === 'on',
    signInProviders: signInProviders(env.SIGN_IN_PROVIDERS),
  };
}

// SIGN_IN_PROVIDERS: a comma list of github, google and email, at least one, each at most once. Spaces around a name
// are allowed; an empty entry, an unknown name or a repeated one is an error naming it.
export function signInProviders(text: string): SignInProvider[] {
  const said = `SIGN_IN_PROVIDERS=${JSON.stringify(text)}`;
  const names = text.split(',').map((name) => name.trim());
  const providers: SignInProvider[] = [];
  for (const name of names) {
    if (name === '') throw new Error(`${said} has an empty entry; list one or more of ${SIGN_IN_PROVIDER_NAMES.join(', ')}, separated by commas`);
    if (!(SIGN_IN_PROVIDER_NAMES as readonly string[]).includes(name)) {
      throw new Error(`${said} names an unknown sign-in provider ${JSON.stringify(name)}; each must be one of ${SIGN_IN_PROVIDER_NAMES.join(', ')}`);
    }
    if ((providers as string[]).includes(name)) throw new Error(`${said} names ${name} twice; list each sign-in provider once`);
    providers.push(name as SignInProvider);
  }
  return providers;
}

export function priceText(usd: number): string {
  return Number.isInteger(usd) ? `$${usd}` : `$${usd.toFixed(2)}`;
}

export function tokensText(tokens: number): string {
  return tokens % 1_000_000 === 0 ? `${tokens / 1_000_000} million` : tokens.toLocaleString('en-US');
}
