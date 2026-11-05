// The console's sign-in, kept in this browser's localStorage under one key. The token is sent as
// Authorization: Bearer on every call (api.ts), never as a cookie. (Calls do carry this site's own cookies, such as
// the gateway's sign-in cookie when a gateway serves the console; the Jeff server reads none of them.)
import type { Session } from './api';

export const SESSION_KEY = 'jeff-console.session';

export type StoredSession =
  | { session: Session; problem: null }
  | { session: null; problem: null }
  | { session: null; problem: string };

/**
 * The saved session, if there is one. A saved value that is not a {user, token} pair is removed, and the reason is
 * returned so the sign-in screen can say why the visitor has to sign in again.
 */
export function readSession(): StoredSession {
  const stored = localStorage.getItem(SESSION_KEY);
  if (stored === null) return { session: null, problem: null };
  let value: unknown;
  try {
    value = JSON.parse(stored);
  } catch (error) {
    localStorage.removeItem(SESSION_KEY);
    return { session: null, problem: `The saved sign-in was not valid JSON (${(error as Error).message}) and was removed.` };
  }
  if (
    typeof value === 'object' && value !== null &&
    typeof (value as Session).user === 'string' && typeof (value as Session).token === 'string'
  ) {
    return { session: { user: (value as Session).user, token: (value as Session).token }, problem: null };
  }
  localStorage.removeItem(SESSION_KEY);
  return { session: null, problem: 'The saved sign-in had no user and token and was removed.' };
}

export function writeSession(session: Session): void {
  localStorage.setItem(SESSION_KEY, JSON.stringify({ user: session.user, token: session.token }));
}

export function clearSession(): void {
  localStorage.removeItem(SESSION_KEY);
}
