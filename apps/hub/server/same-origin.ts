// The cross-site check for the hub's own state-changing POSTs (the account endpoints), the same check Better Auth's
// social sign-in gets in app/lib/auth.server.ts: the request must carry an Origin header that is exactly the hub's
// own origin (BETTER_AUTH_URL's), and must not be marked cross-site by the browser (Sec-Fetch-Site). A browser sends
// Origin on every POST, and a page on another site cannot set it, so a form or script elsewhere cannot use the
// visitor's session cookie here. A request without an Origin is refused too.
import { ApiError } from './http.js';

export function requireSameOrigin(request: Request, baseUrl: string): void {
  const own = new URL(baseUrl).origin;
  const origin = request.headers.get('origin');
  if (origin !== own || request.headers.get('sec-fetch-site') === 'cross-site') {
    throw new ApiError(403, 'CrossSiteRefused', `this must be sent from a page on ${own}; this request came from ${JSON.stringify(origin)}`,
      { origin, expected: own });
  }
}
