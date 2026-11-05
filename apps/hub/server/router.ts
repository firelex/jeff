// Every hub endpoint by its path. api/index.ts is the one Vercel function, and vercel.json rewrites every /api path
// to it, so a path missing here is answered with a JSON 404 that names it. Later endpoints are added to ENDPOINTS.
// A path is matched exactly first; then by PREFIXES, for an endpoint that answers a whole subtree itself (Better
// Auth's /api/auth/..., and one adapter request's paths under /api/admin/adapter-requests/). A prefix ends with a slash, so /api/authx is not under /api/auth/.
import type { Endpoint } from './endpoint.js';
import { serveEndpoint } from './endpoint.js';
import { accountEndpoint } from './endpoints/account.js';
import { ADMIN_PREFIX, adapterRequestEndpoint } from './endpoints/adapter-requests.js';
import { adminAdapterEndpoint } from './endpoints/admin-adapters.js';
import { betterAuthEndpoint } from './endpoints/auth.js';
import { CLEAR_EXAMPLES_PATH, cronEndpoint } from './endpoints/cron.js';
import { health } from './endpoints/health.js';
import { internalEndpoint } from './endpoints/internal.js';
import { playgroundEndpoint } from './endpoints/playground.js';
import { signinOptions } from './endpoints/signin-options.js';
import { stripeWebhookEndpoint } from './endpoints/stripe-webhook.js';
import { ApiError, errorResponse } from './http.js';

export const ENDPOINTS: Record<string, Endpoint> = {
  '/api/account': accountEndpoint.page,
  '/api/account/checkout': accountEndpoint.checkout,
  '/api/account/create-key': accountEndpoint.createKey,
  '/api/account/delete': accountEndpoint.delete,
  '/api/account/portal': accountEndpoint.portal,
  '/api/account/revoke-key': accountEndpoint.revokeKey,
  '/api/account/sign-out-everywhere': accountEndpoint.signOutEverywhere,
  '/api/account/status': accountEndpoint.status,
  '/api/adapter-requests': adapterRequestEndpoint.mine,
  '/api/adapter-requests/upload': adapterRequestEndpoint.upload,
  '/api/admin/adapter-requests': adapterRequestEndpoint.adminList,
  '/api/admin/adapters': adminAdapterEndpoint.adapters,
  '/api/admin/owner': adminAdapterEndpoint.owner,
  [CLEAR_EXAMPLES_PATH]: cronEndpoint.clearExamples,
  '/api/health': health,
  '/api/internal/v1/keys': internalEndpoint.keys,
  '/api/internal/v1/usage': internalEndpoint.usage,
  '/api/playground/decide': playgroundEndpoint,
  '/api/signin-options': signinOptions,
  '/api/stripe/webhook': stripeWebhookEndpoint,
};

export const PREFIXES: [prefix: string, endpoint: Endpoint][] = [
  ['/api/auth/', betterAuthEndpoint],
  // One adapter request of the owner's: /<id>, /<id>/status, /<id>/delete, /<id>/export and /<id>/data-set.
  [ADMIN_PREFIX, adapterRequestEndpoint.adminOne],
];

export function endpointFor(pathname: string): Endpoint | undefined {
  const exact = ENDPOINTS[pathname];
  if (exact !== undefined) return exact;
  return PREFIXES.find(([prefix]) => pathname.startsWith(prefix))?.[1];
}

export async function handle(request: Request): Promise<Response> {
  const pathname = new URL(request.url).pathname;
  const endpoint = endpointFor(pathname);
  if (endpoint === undefined) {
    return errorResponse(new ApiError(404, 'NotFound', `the hub has no endpoint at ${pathname}`, { path: pathname }));
  }
  return noStore(await serveEndpoint(endpoint, request));
}

// No answer of the hub's function may be kept by a browser or a cache: they carry sessions, cookies and accounts.
// json() already says so; Better Auth's own answers (its redirects with Set-Cookie, for one) do not (review M3).
function noStore(response: Response): Response {
  if (response.headers.get('cache-control') === 'no-store') return response;
  if (response.headers.has('cache-control')) {
    throw new Error(`an endpoint answered with cache-control: ${response.headers.get('cache-control')}; the hub's answers are never cached`);
  }
  const copy = new Response(response.body, response);
  copy.headers.set('cache-control', 'no-store');
  return copy;
}
