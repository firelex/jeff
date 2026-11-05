// The signed-in customer's account endpoints (spec Sections 4, 5 and 7), for the prerendered account pages:
// - GET  /api/account: what the /account page shows (app/lib/account-page.server.ts).
// - GET  /api/account/status: the subscription as the hub knows it from Stripe's webhooks. /account/return polls it.
// - POST /api/account/checkout: a Stripe Checkout for the one plan; answers {"url"} for the page to go to.
// - POST /api/account/portal: the Stripe Customer Portal; answers {"url"}.
// - POST /api/account/create-key with {"name"}: a new API key, answered once as {"created": {"key", "name"}}; the
//   hub keeps only its hash.
// - POST /api/account/revoke-key with {"key": <key id>}: revokes one of the account's live keys.
// - POST /api/account/sign-out-everywhere: ends every session of the account (Better Auth's own revoke-sessions) and
//   expires this browser's session cookies.
// - POST /api/account/delete with {"confirm": "delete"}: cancels the subscriptions in Stripe, then deletes the account
//   (with its adapter requests and their data sets' zips in the file store) and expires the browser's session cookies.
// Every one needs a signed-in visitor (401 otherwise, app/lib/session.server.ts); every POST must come from the hub's
// own pages (403 otherwise, server/same-origin.ts), checked before the session is read, and a POST with a body takes
// it as JSON only (415 otherwise). A request the customer can act on (subscribe first; already subscribed; too many
// keys) is a 409 BillingRefused or KeyRefused with words for them.
import { ACCOUNTS_SETTING_NAMES, accountsSettings } from '../../app/lib/accounts-settings.server.js';
import { BillingRefused, checkoutUrl, deleteAccount, portalUrl, type BillingDeps } from '../../app/lib/billing.server.js';
import { KEY_REVOKED, type AccountPage, type CreatedKeyAnswer, type DoneAnswer } from '../../app/lib/account-page.js';
import { accountPageData } from '../../app/lib/account-page.server.js';
import { BLOB_TOKEN_SETTING, liveDataSetStore, type DataSetStore } from '../../app/lib/data-set-store.server.js';
import { getDb } from '../../app/lib/db.server.js';
import { createKey, KeyRefused, revokeKey } from '../../app/lib/keys.server.js';
import { expiredSessionCookiesOfTheHub, requireUser, revokeEverySessionOfTheHub, type SessionUser } from '../../app/lib/session.server.js';
import { stripeApi } from '../../app/lib/stripe.server.js';
import { isUsable, subscriptionOf } from '../../app/lib/subscription.server.js';
import { defineEndpoint } from '../endpoint.js';
import { ApiError, json } from '../http.js';
import { requireSameOrigin } from '../same-origin.js';

export interface AccountStatus { account: string; usable: boolean; status: string | null }

// What the endpoints run against; tests pass a fake Stripe and a signed-in user of their own.
export interface AccountContext {
  // The database and settings, with the Stripe client: only checkout, portal and delete ask for it.
  billing(): Promise<BillingDeps>;
  // The database and settings alone, for every endpoint that never calls Stripe.
  store(): Promise<Pick<BillingDeps, 'db' | 'settings'>>;
  signedIn(request: Request): Promise<SessionUser>;
  // Set-Cookie values that sign the browser out (sent with a deletion and with signing out everywhere).
  expiredSessionCookies(): Promise<string[]>;
  // Ends every session of the user the request's headers sign in.
  revokeSessions(headers: Headers): Promise<void>;
  // The file store of the adapter requests' data sets: deletion lists the account's folder and deletes every zip in it.
  dataSets: Pick<DataSetStore, 'list' | 'remove'>;
  now(): Date;
}

const live: AccountContext = {
  billing: async () => {
    const settings = accountsSettings();
    return { db: await getDb(), stripe: stripeApi(settings.stripe.secretKey), settings };
  },
  store: async () => ({ db: await getDb(), settings: accountsSettings() }),
  signedIn: requireUser,
  expiredSessionCookies: expiredSessionCookiesOfTheHub,
  revokeSessions: revokeEverySessionOfTheHub,
  dataSets: liveDataSetStore,
  now: () => new Date(),
};

// The word the account page's deletion form asks the customer to type.
export const DELETE_CONFIRMATION = 'delete';

// The refusals of billing.server.ts and keys.server.ts are meant for the customer: a 409 with their words.
async function refusedAsConflict<T>(work: () => Promise<T>): Promise<T> {
  try {
    return await work();
  } catch (error) {
    if (error instanceof BillingRefused) throw new ApiError(409, 'BillingRefused', error.message);
    if (error instanceof KeyRefused) throw new ApiError(409, 'KeyRefused', error.message);
    throw error;
  }
}

// application/json exactly, with or without parameters (application/json; charset=utf-8); not application/jsonx.
export function isJson(contentType: string | null): boolean {
  if (contentType === null) return false;
  return contentType.split(';')[0].trim().toLowerCase() === 'application/json';
}

// The JSON object a POST carries; `example` is the body the endpoint expects, for the refusal's words.
async function jsonBodyOf(request: Request, example: string): Promise<Record<string, unknown>> {
  if (!isJson(request.headers.get('content-type'))) {
    throw new ApiError(415, 'UnsupportedMediaType', `send the request as JSON: ${example} with content-type application/json`);
  }
  const text = await request.text();
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new ApiError(400, 'InvalidRequest', `the body is not JSON: ${error.message}`);
  }
  return typeof body === 'object' && body !== null && !Array.isArray(body) ? body as Record<string, unknown> : {};
}

// A field of the body that must be text; `missing` says, in the customer's words, what to do instead.
function textField(body: Record<string, unknown>, name: string, missing: string): string {
  const value = body[name];
  if (typeof value !== 'string') throw new ApiError(400, 'InvalidRequest', missing, { field: name });
  return value;
}

export function accountEndpoints(context: AccountContext) {
  // The POSTs: the cross-site check first, then the session. billingPost builds the Stripe client; storePost does not.
  const billingPost = async (request: Request) => {
    const deps = await context.billing();
    requireSameOrigin(request, deps.settings.baseUrl);
    return { deps, user: await context.signedIn(request) };
  };
  const storePost = async (request: Request) => {
    const deps = await context.store();
    requireSameOrigin(request, deps.settings.baseUrl);
    return { deps, user: await context.signedIn(request) };
  };
  // Answers that sign this browser out carry the session cookies, expired.
  const signingOut = async (response: Response) => {
    for (const cookie of await context.expiredSessionCookies()) response.headers.append('set-cookie', cookie);
    return response;
  };
  return {
    page: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        GET: async (request) => {
          const user = await context.signedIn(request);
          const { db, settings } = await context.store();
          const body: AccountPage = await accountPageData(db, settings, user, context.now());
          return json(200, body);
        },
      },
    }),
    status: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        GET: async (request) => {
          const user = await context.signedIn(request);
          const row = await subscriptionOf((await context.store()).db, user.id);
          const body: AccountStatus = { account: user.id, usable: row !== null && isUsable(row.status), status: row === null ? null : row.status };
          return json(200, body);
        },
      },
    }),
    checkout: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        POST: async (request) => {
          const { deps, user } = await billingPost(request);
          return json(200, { url: await refusedAsConflict(() => checkoutUrl(deps, user)) });
        },
      },
    }),
    portal: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        POST: async (request) => {
          const { deps, user } = await billingPost(request);
          return json(200, { url: await refusedAsConflict(() => portalUrl(deps, user.id)) });
        },
      },
    }),
    // The key is in this answer only: it is never stored (only its hash), logged or put in an address.
    createKey: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        POST: async (request) => {
          const { deps, user } = await storePost(request);
          const name = textField(await jsonBodyOf(request, '{"name": "laptop"}'), 'name', 'Give the key a name.');
          const { key, view } = await refusedAsConflict(() => createKey(deps.db, user.id, name, context.now()));
          const body: CreatedKeyAnswer = { created: { key, name: view.name } };
          return json(200, body);
        },
      },
    }),
    revokeKey: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        POST: async (request) => {
          const { deps, user } = await storePost(request);
          const key = textField(await jsonBodyOf(request, '{"key": "<the key\'s id>"}'), 'key', 'Choose the key to revoke.');
          await refusedAsConflict(() => revokeKey(deps.db, user.id, key, context.now()));
          const body: DoneAnswer = { done: KEY_REVOKED };
          return json(200, body);
        },
      },
    }),
    signOutEverywhere: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        POST: async (request) => {
          const { user } = await storePost(request);
          await context.revokeSessions(request.headers);
          return signingOut(json(200, { signedOut: user.id }));
        },
      },
    }),
    delete: defineEndpoint({
      // The data set store's token too (final review M6): deletion deletes the account's zips, so a missing token is
      // named before anything is done.
      settings: [...ACCOUNTS_SETTING_NAMES, BLOB_TOKEN_SETTING],
      methods: {
        POST: async (request) => {
          const { deps, user } = await billingPost(request);
          const confirm = (await jsonBodyOf(request, `{"confirm": "${DELETE_CONFIRMATION}"}`)).confirm;
          if (confirm !== DELETE_CONFIRMATION) {
            throw new ApiError(400, 'ConfirmationRequired', `Type ${DELETE_CONFIRMATION} in the box to confirm deleting the account.`,
              { field: 'confirm', expected: DELETE_CONFIRMATION });
          }
          await deleteAccount(deps, user.id, context.dataSets);
          return signingOut(json(200, { deleted: user.id }));
        },
      },
    }),
  };
}

export const accountEndpoint = accountEndpoints(live);
