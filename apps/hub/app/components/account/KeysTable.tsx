import type { AccountKey } from '../../lib/account-page';
import { longDate } from '../../lib/dates';
import { TABLE_FRAME, TD, TH } from '../styles';

// The account's live keys: two columns, so the table fits a phone's width without scrolling (the name over its
// visible prefix and the revoke button; created over last used).
// Revoking asks first (account.tsx's ConfirmDialog), since a revoked key cannot be brought back.
export function KeysTable({ keys, busy, onRevoke }: { keys: AccountKey[]; busy: boolean; onRevoke: (key: AccountKey) => void }) {
  if (keys.length === 0) return <p className="mt-4 text-sm text-soft">No keys yet. Create one below.</p>;
  return (
    <div className={`mt-4 ${TABLE_FRAME}`}>
      <table className="w-full text-left text-sm">
        <thead><tr><th className={TH}>Key</th><th className={TH}>Dates</th></tr></thead>
        <tbody>
          {keys.map((key) => (
            <tr key={key.id}>
              <td className={TD}>
                <div className="break-words font-medium text-text">{key.name}</div>
                <div className="mt-0.5 whitespace-nowrap font-mono text-xs text-mute">{key.shown}</div>
                <button type="button" disabled={busy} onClick={() => onRevoke(key)} aria-label={`Revoke ${key.name}`}
                  className="mt-2 font-mono text-sm text-danger underline underline-offset-2 hover:no-underline disabled:opacity-50">Revoke</button>
              </td>
              <td className={`${TD} text-xs text-soft`}>
                <div>Created <span className="whitespace-nowrap">{longDate(key.createdAt)}</span></div>
                <div className="mt-0.5">Last used <span className="whitespace-nowrap">{key.lastUsedAt === null ? 'never' : longDate(key.lastUsedAt)}</span></div>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
