// POST /api/playground/decide with {"sample": <example id>, "input": <the edited input>}: one live call to the Jeff
// API, for the playground page (app/lib/playground.server.ts says how visitors and subscribers run). Like every hub
// POST, it must come from the hub's own pages (403 otherwise, server/same-origin.ts), checked before anything else,
// and takes its body as JSON only (415 otherwise).
import { ACCOUNTS_SETTING_NAMES, accountsSettings, type AccountsSettings } from '../../app/lib/accounts-settings.server.js';
import { getDb } from '../../app/lib/db.server.js';
import { playgroundRun, type PlaygroundDeps } from '../../app/lib/playground.server.js';
import { sessionUser } from '../../app/lib/session.server.js';
import { apiOriginSetting } from '../../app/lib/settings.server.js';
import { canUseKeys } from '../../app/lib/subscription.server.js';
import { turnstileVerifier } from '../../app/lib/turnstile.server.js';
import { defineEndpoint } from '../endpoint.js';
import { ApiError } from '../http.js';
import { requireSameOrigin } from '../same-origin.js';
import { isJson } from './account.js';

// What the endpoint runs against; tests pass their own database, settings, fake API and signed-in user.
export interface PlaygroundContext {
  settings(): AccountsSettings;
  deps(settings: AccountsSettings): Promise<PlaygroundDeps>;
}

const live: PlaygroundContext = {
  settings: accountsSettings,
  deps: async (settings) => {
    const db = await getDb();
    return {
      db,
      settings,
      apiOrigin: apiOriginSetting(),
      fetchApi: fetch,
      verifyTurnstile: turnstileVerifier({ ...settings.turnstile, hostname: new URL(settings.baseUrl).hostname }, fetch),
      now: () => new Date(),
      clock: () => performance.now(),
      subscriberOf: async (request) => {
        const user = await sessionUser(request);
        return user !== null && (await canUseKeys(db, user.id)) ? { id: user.id } : null;
      },
    };
  },
};

export function playgroundEndpointFor(context: PlaygroundContext) {
  return defineEndpoint({
    settings: ACCOUNTS_SETTING_NAMES,
    methods: {
      POST: async (request) => {
        const settings = context.settings();
        requireSameOrigin(request, settings.baseUrl);
        if (!isJson(request.headers.get('content-type'))) {
          throw new ApiError(415, 'UnsupportedMediaType', 'send the request as JSON: {"sample": "<sample id>", "input": "<the input>"} with content-type application/json');
        }
        return playgroundRun(request, await context.deps(settings));
      },
    },
  });
}

export const playgroundEndpoint = playgroundEndpointFor(live);
