// /api/auth/*: the Better Auth endpoints the hub uses (sign-in, the provider callbacks, the email link, sessions,
// sign-out), matched by prefix in server/router.ts. Three checks come before Better Auth runs at all: the path must be
// one the hub uses (isAuthPath; any other is a 404 that costs nothing, not even a rate-limit row), it must not belong
// to a sign-in method SIGN_IN_PROVIDERS leaves out (signInRefusal, and socialRefusal for social sign-in's JSON body; a
// 404 naming the method, which uses up none of Better Auth's sign-in rate limit), and the request must carry exactly
// one visitor address (clientIp; 400 otherwise), which Better Auth counts its limits by.
import { ACCOUNTS_SETTING_NAMES, accountsSettings } from '../../app/lib/accounts-settings.server.js';
import { auth, isAuthPath, signInRefusal, socialRefusal } from '../../app/lib/auth.server.js';
import { clientIp } from '../../app/lib/client-ip.server.js';
import { defineEndpoint } from '../endpoint.js';
import { ApiError } from '../http.js';

const BASE_PATH = '/api/auth';

async function toBetterAuth(request: Request): Promise<Response> {
  const pathname = new URL(request.url).pathname;
  const path = pathname.slice(BASE_PATH.length);
  if (!isAuthPath(path)) throw new ApiError(404, 'NotFound', `the hub has no endpoint at ${pathname}`, { path: pathname });
  const settings = accountsSettings();
  let forward = request;
  let refused: string | null;
  if (request.method === 'POST' && path === '/sign-in/social') {
    // The body is read here, so Better Auth gets a new request with the same method, headers and body text.
    const text = await request.text();
    refused = socialRefusal(settings, socialBody(request.headers.get('content-type'), text).provider);
    forward = new Request(request.url, { method: request.method, headers: request.headers, body: text });
  } else {
    refused = signInRefusal(settings, path);
  }
  if (refused !== null) throw new ApiError(404, 'SignInMethodNotOffered', refused, { path: pathname });
  clientIp(request.headers);
  return (await auth()).handler(forward);
}

// Social sign-in's body. The sign-in page sends JSON; anything else is refused here, naming what it was.
function socialBody(type: string | null, text: string): { provider?: unknown } {
  if (type === null || !/^application\/json\s*(;|$)/i.test(type)) {
    throw new ApiError(415, 'UnsupportedMediaType', `${BASE_PATH}/sign-in/social takes a JSON body; got content-type ${JSON.stringify(type)}`);
  }
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch (error) {
    throw new ApiError(400, 'BadRequest', `${BASE_PATH}/sign-in/social takes a JSON body; this one does not parse: ${(error as Error).message}`);
  }
  if (body === null || typeof body !== 'object' || Array.isArray(body)) {
    throw new ApiError(400, 'BadRequest', `${BASE_PATH}/sign-in/social takes a JSON object`);
  }
  return body as { provider?: unknown };
}

export const betterAuthEndpoint = defineEndpoint({
  settings: ACCOUNTS_SETTING_NAMES,
  methods: { GET: toBetterAuth, POST: toBetterAuth },
});
