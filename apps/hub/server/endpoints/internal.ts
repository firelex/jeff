// The internal API jeff-server polls once a minute in accounts mode (app/lib/internal-api.server.ts; jeff-server's
// JEFF_ACCOUNTS_URL is the hub's own address, to which it adds these paths, apps/server/src/jeff_server/accounts.py):
// - GET  /api/internal/v1/keys: the keys that may be used now (contract schema accounts_keys).
// - POST /api/internal/v1/usage: jeff-server's usage totals (contract schema accounts_usage), as JSON; answers
//   200 {"recorded": <totals stored>, "left_out": <totals of accounts that do not exist, not stored>}. jeff-server reads
//   only the status (200 or 204 is success). Each report also sweeps the playground's counted runs older than its
//   counting window (playground.server.ts: sweepPlaygroundRuns), so the rows of visitors who never come back go.
// Both need https, as Vercel's X-Forwarded-Proto says (403 otherwise; plain http only on a loopback host outside
// production, for tests) and the shared
// secret JEFF_HUB_INTERNAL_TOKEN as a bearer token (401 otherwise), checked before anything else is read. Every
// answer is no-store (json() and server/router.ts).
import { INTERNAL_SETTING_NAMES, internalSettings, type InternalSettings } from '../../app/lib/accounts-settings.server.js';
import { getDb, type Db } from '../../app/lib/db.server.js';
import { inProduction } from '../../app/lib/env.server.js';
import { checkInternal, keyList, recordUsage } from '../../app/lib/internal-api.server.js';
import { sweepPlaygroundRuns } from '../../app/lib/playground.server.js';
import { defineEndpoint } from '../endpoint.js';
import { ApiError, json } from '../http.js';
import { isJson } from './account.js';

// What the endpoints run against; tests pass a database of their own.
export interface InternalContext {
  // Only the internal API's own settings (final review I7): a wrong sign-in or mail setting cannot stop the key list.
  settings(): InternalSettings;
  db(): Promise<Db>;
  production(): boolean;
  now(): Date;
}

const live: InternalContext = { settings: internalSettings, db: getDb, production: inProduction, now: () => new Date() };

export function internalEndpoints(context: InternalContext) {
  const checked = (request: Request) => {
    const settings = context.settings();
    checkInternal(request, settings.internalToken, context.production());
    return settings;
  };
  return {
    keys: defineEndpoint({
      settings: INTERNAL_SETTING_NAMES,
      hidesMissingSettings: true,
      methods: {
        GET: async (request) => {
          const settings = checked(request);
          return json(200, await keyList(await context.db(), settings, context.now()));
        },
      },
    }),
    usage: defineEndpoint({
      settings: INTERNAL_SETTING_NAMES,
      hidesMissingSettings: true,
      methods: {
        POST: async (request) => {
          checked(request);
          if (!isJson(request.headers.get('content-type'))) {
            throw new ApiError(415, 'UnsupportedMediaType', 'send the usage report as JSON, with content-type application/json');
          }
          let body: unknown;
          try {
            body = JSON.parse(await request.text());
          } catch (error) {
            if (!(error instanceof SyntaxError)) throw error;
            // A fixed message: the parser's own quotes part of the body, which can hold a key's hash.
            throw new ApiError(422, 'InvalidRequest', 'the usage report is not valid JSON');
          }
          const db = await context.db();
          const now = context.now();
          const recorded = await recordUsage(db, body, now);
          await sweepPlaygroundRuns(db, now);
          return json(200, recorded);
        },
      },
    }),
  };
}

export const internalEndpoint = internalEndpoints(live);
