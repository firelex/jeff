// The signed-in visitor of a request to the hub's function, from the Better Auth session cookie the request carries.
// The pages are prerendered shells, so no page reads a session itself: the endpoints do, and an endpoint that needs
// a signed-in visitor answers 401 (requireUser), which the page turns into a visit to /signin.
import type { BetterAuthOptions } from 'better-auth';
import { getCookies } from 'better-auth/cookies';
import { ApiError } from '../../server/http.js';
import { auth, type Auth } from './auth.server.js';

// emailVerified: whether the address is proven (the email link, or a provider that reports it verified). The hub
// creates no user whose address is not proven (auth.server.ts), so it is false only for a row made some other way.
export interface SessionUser { id: string; email: string; emailVerified: boolean; name: string; image: string | null }

export async function sessionUser(request: Request): Promise<SessionUser | null> {
  const result = await (await auth()).api.getSession({ headers: request.headers });
  if (result === null) return null;
  const { id, email, emailVerified, name, image } = result.user;
  if (typeof emailVerified !== 'boolean') throw new Error(`the session's user ${id} has no emailVerified flag`);
  return { id, email, emailVerified, name, image: image === undefined || image === null ? null : image };
}

export async function requireUser(request: Request): Promise<SessionUser> {
  const user = await sessionUser(request);
  if (user === null) throw new ApiError(401, 'SignInRequired', 'sign in first: this needs a signed-in Jeff account', { signIn: '/signin' });
  return user;
}

// Set-Cookie values that expire the session cookies in the browser, by Better Auth's own names and attributes for
// these options (the __Secure- prefix, path /, HttpOnly, Secure, SameSite), as its own sign-out expires them: the
// session token, the session cache and the "don't remember me" cookie. Sent when an account is deleted, whose
// sessions are already gone from the database.
export function expiredSessionCookies(options: BetterAuthOptions): string[] {
  const cookies = getCookies(options);
  return [cookies.sessionToken, cookies.sessionData, cookies.dontRememberToken].map(({ name, attributes }) => {
    if (attributes.domain !== undefined) throw new Error(`the session cookie ${name} has a domain; the hub's cookies have none`);
    const sameSite = attributes.sameSite.charAt(0).toUpperCase() + attributes.sameSite.slice(1).toLowerCase();
    return [`${name}=`, 'Max-Age=0', `Path=${attributes.path}`, ...(attributes.httpOnly ? ['HttpOnly'] : []),
      ...(attributes.secure ? ['Secure'] : []), `SameSite=${sameSite}`].join('; ');
  });
}

// Sign out everywhere: Better Auth's own revoke-sessions deletes every session of the request's signed-in user (the
// hub reads sessions from the database on every request, so each one ends at once). It needs the request's headers,
// whose cookie names the user.
export async function revokeEverySession(auth: Pick<Auth, 'api'>, headers: Headers): Promise<void> {
  const { status } = await auth.api.revokeSessions({ headers });
  if (status !== true) throw new Error(`Better Auth's revoke-sessions answered status ${JSON.stringify(status)}`);
}

export async function revokeEverySessionOfTheHub(headers: Headers): Promise<void> {
  await revokeEverySession(await auth(), headers);
}

export async function expiredSessionCookiesOfTheHub(): Promise<string[]> {
  return expiredSessionCookies((await auth()).options);
}
