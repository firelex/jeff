// The browser's side of Better Auth: it calls /api/auth on the page's own origin, so the session cookie (HttpOnly,
// set by the hub's function) goes with every call. Made on first use in the browser with that origin named, never
// guessed from settings: the pages are prerendered, so this module also loads at build time, where there is no page.
import { magicLinkClient } from 'better-auth/client/plugins';
import { createAuthClient } from 'better-auth/react';

function create() {
  return createAuthClient({ baseURL: window.location.origin, basePath: '/api/auth', plugins: [magicLinkClient()] });
}

let client: ReturnType<typeof create> | null = null;

export function authClient(): ReturnType<typeof create> {
  if (typeof window === 'undefined') throw new Error('authClient() runs only in the browser, on a page of the hub');
  if (client === null) client = create();
  return client;
}
