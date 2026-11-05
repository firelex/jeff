// What GET /api/account answers and the /account page shows (server/endpoints/account.ts, app/routes/account.tsx):
// shared by the hub's function and the page's browser code, so it holds no server code.
export interface AccountKey { id: string; name: string; shown: string; createdAt: string; lastUsedAt: string | null }

// The playground's input tokens this calendar month: shown to the owner only, whatever their own subscription.
export type PlaygroundUsage = { used: number; since: string } | null;

export type AccountPage =
  | { view: 'subscribe'; email: string; price: string; allowance: string; status: string | null; playground: PlaygroundUsage }
  | {
      view: 'account'; email: string; status: string; periodEnd: string; cancelAtPeriodEnd: boolean;
      usage: { used: number; allowance: number };
      keys: AccountKey[];
      playground: PlaygroundUsage;
    };

// The answers of the account page's POSTs.
export interface CreatedKeyAnswer { created: { key: string; name: string } }
export interface DoneAnswer { done: string }

// The text of a revoked key's answer.
export const KEY_REVOKED = 'The key is revoked. It stops working within about a minute.';
