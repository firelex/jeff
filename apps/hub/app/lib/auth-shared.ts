// What the hub's function (auth.server.ts) and the sign-in page share about sign-in, so this module imports nothing:
// the times (spec Section 3), the pages a sign-in may come back to, and the test OpenID provider's id.
export const MAGIC_LINK_MINUTES = 15;
export const MAGIC_LINK_SECONDS = MAGIC_LINK_MINUTES * 60;
export const SESSION_SECONDS = 30 * 24 * 60 * 60;

// The only values a callbackURL, errorCallbackURL or newUserCallbackURL may have: the hub's own pages, as exact
// root-relative paths. Anything else is refused before Better Auth reads it (auth.server.ts), so no form, link or
// provider callback can send a visitor anywhere else (review I1: Better Auth's own check lets /%09/evil.example/
// through, which a trailing-slash redirect turns into //evil.example).
export const CALLBACK_PATHS = ['/account', '/signin', '/signup', '/request-adapter'] as const;
export type CallbackPath = (typeof CALLBACK_PATHS)[number];

// The pages that send a visitor to sign in and want them back afterwards (/signin?next=<page>): the sign-in page
// comes back to one of these, and to /account for any other ?next.
export const NEXT_PAGES = ['/request-adapter'] as const satisfies readonly CallbackPath[];
export type NextPage = (typeof NEXT_PAGES)[number];

export function signInFor(page: NextPage): string {
  return `/signin?next=${encodeURIComponent(page)}`;
}

// The test provider (AUTH_TEST_PROVIDER: tests and local runs only, refused in production).
export const TEST_PROVIDER_ID = 'test';

// The sign-in methods the hub can offer, as SIGN_IN_PROVIDERS names them (a required setting, read at build time by
// the sign-in page and at run time by the hub's function: public-settings.server.ts). Only the listed ones are
// configured, shown and answered; the test provider is separate (AUTH_TEST_PROVIDER).
export const SIGN_IN_PROVIDER_NAMES = ['github', 'google', 'email'] as const;
export type SignInProvider = (typeof SIGN_IN_PROVIDER_NAMES)[number];
// The two of them that sign in through a provider's own page (Better Auth's social sign-in).
export const SOCIAL_PROVIDERS = ['github', 'google'] as const satisfies readonly SignInProvider[];
export type SocialProvider = (typeof SOCIAL_PROVIDERS)[number];
