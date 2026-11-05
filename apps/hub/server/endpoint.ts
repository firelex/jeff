// One endpoint of the hub: the settings it needs and a handler per HTTP method. serveEndpoint answers a method the
// endpoint has no handler for with 405 (naming the methods it has), and never runs a handler while one of the
// endpoint's settings is missing: it answers 500 naming them instead (and logs it), so a deployment without a
// setting fails on the first request with the setting's name, never with a guessed value.
import { ApiError, errorResponse } from './http.js';
import { MissingSettingsError, readSettings } from './settings.js';

export const METHODS = ['GET', 'POST', 'PUT', 'PATCH', 'DELETE'] as const;
export type Method = (typeof METHODS)[number];

export type Handler<Name extends string> = (request: Request, settings: Record<Name, string>) => Response | Promise<Response>;

export interface Endpoint<Name extends string = string> {
  settings: readonly Name[];
  methods: Partial<Record<Method, Handler<Name>>>;
  // An endpoint whose caller proves itself with a secret (the cron job, the internal API) answers a missing setting
  // with the same 404 an unknown path gets, since the answer comes before the secret is checked: it must not tell
  // anyone the path exists. The log names the setting (final review M5, re-review R8).
  hidesMissingSettings?: true;
}

// Keeps each handler's `settings` argument typed by the names the endpoint lists.
export function defineEndpoint<const Name extends string>(endpoint: Endpoint<Name>): Endpoint<Name> {
  return endpoint;
}

export async function serveEndpoint<Name extends string>(endpoint: Endpoint<Name>, request: Request): Promise<Response> {
  const pathname = new URL(request.url).pathname;
  const allowed = METHODS.filter((method) => endpoint.methods[method] !== undefined);
  const handler = endpoint.methods[request.method as Method];
  if (handler === undefined) {
    return errorResponse(new ApiError(405, 'MethodNotAllowed', `${pathname} does not answer ${request.method}; it answers ${allowed.join(', ')}`, { method: request.method, allowed }, { allow: allowed.join(', ') }));
  }
  let settings: Record<Name, string>;
  try {
    settings = readSettings(endpoint.settings);
  } catch (error) {
    if (!(error instanceof MissingSettingsError)) throw error;
    console.error(`hub endpoint ${pathname} cannot run: ${error.message}`);
    if (endpoint.hidesMissingSettings === true) {
      return errorResponse(new ApiError(404, 'NotFound', `the hub has no endpoint at ${pathname}`, { path: pathname }));
    }
    return errorResponse(new ApiError(500, 'MissingSetting', `the hub cannot serve ${pathname}: ${error.message}`, { missing: error.missing }));
  }
  try {
    return await handler(request, settings);
  } catch (error) {
    if (error instanceof ApiError) return errorResponse(error);
    throw error;
  }
}
