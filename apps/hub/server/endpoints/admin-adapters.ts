// The owner-only views of the hidden adapters (app/lib/hidden-adapters.ts), for the /admin/adapters page and the
// catalogue's owner view:
// - GET /api/admin/adapters: every hidden adapter's catalogue entry and worked examples, as {"adapters": [...]},
//   from the function's content module (HIDDEN_ADAPTERS; the function reads no file).
//   It answers only the owner (the session's email is OWNER_EMAIL); anyone else, signed in or not, gets the same 404
//   an unknown path gets (server/owner.ts).
// - GET /api/admin/owner: {"owner": true} for the owner, {"owner": false} for anyone else (signed in or not), so a
//   public page can learn after hydration whether to show the owner's view without any page carrying the owner's
//   email. It says nothing but whether the visitor asking is the owner, so it answers everyone: a 404 here would put
//   an error line in every visitor's console on every catalogue page.
import { ACCOUNTS_SETTING_NAMES, accountsSettings } from '../../app/lib/accounts-settings.server.js';
import { HIDDEN_ADAPTERS } from '../../app/lib/owner-only/hidden-adapters.generated.js';
import { requireUser, type SessionUser } from '../../app/lib/session.server.js';
import { defineEndpoint } from '../endpoint.js';
import { ApiError, json } from '../http.js';
import { requireOwner } from '../owner.js';

export interface OwnerContext {
  ownerEmail(): string;
  signedIn(request: Request): Promise<SessionUser>;
  // The hidden adapters (app/lib/hidden-adapters.ts: HiddenAdapter), passed on as they are.
  hidden(): readonly unknown[];
}

const live: OwnerContext = {
  ownerEmail: () => accountsSettings().ownerEmail,
  signedIn: requireUser,
  hidden: () => HIDDEN_ADAPTERS,
};

export function adminAdapterEndpoints(context: OwnerContext) {
  return {
    adapters: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        GET: async (request) => {
          await requireOwner(request, context.ownerEmail(), context.signedIn);
          return json(200, { adapters: context.hidden() });
        },
      },
    }),
    owner: defineEndpoint({
      settings: ACCOUNTS_SETTING_NAMES,
      methods: {
        GET: async (request) => {
          try {
            await requireOwner(request, context.ownerEmail(), context.signedIn);
          } catch (error) {
            if (error instanceof ApiError && error.status === 404) return json(200, { owner: false });
            throw error;
          }
          return json(200, { owner: true });
        },
      },
    }),
  };
}

export const adminAdapterEndpoint = adminAdapterEndpoints(live);
