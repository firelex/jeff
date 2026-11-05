// The owner-only endpoints' gate: the signed-in visitor whose email is OWNER_EMAIL (compared without case) and
// verified (an unverified address proves nothing: final review C1), or the
// same 404 the router answers for a path it does not have, so an owner-only endpoint looks like no endpoint to
// anyone else, signed in or not.
import type { SessionUser } from '../app/lib/session.server.js';
import { ApiError } from './http.js';

export const notFound = (pathname: string) => new ApiError(404, 'NotFound', `the hub has no endpoint at ${pathname}`, { path: pathname });

export async function requireOwner(request: Request, ownerEmail: string, signedIn: (request: Request) => Promise<SessionUser>): Promise<SessionUser> {
  const pathname = new URL(request.url).pathname;
  let user: SessionUser;
  try {
    user = await signedIn(request);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) throw notFound(pathname);
    throw error;
  }
  if (user.email.toLowerCase() !== ownerEmail || user.emailVerified !== true) throw notFound(pathname);
  return user;
}
