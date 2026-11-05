import { Alert } from '@jeff/ui';
import type { ShownError } from '../api';

/** A failure as the server reported it: "type: message", and the request id when there is one. */
export function ErrorAlert({ error, className = '' }: { error: ShownError; className?: string }) {
  return (
    <Alert tone="error" className={className}>
      <span className="block whitespace-pre-wrap break-words">
        <b className="font-semibold text-danger">{error.type}</b>: {error.message}
      </span>
      {error.requestId !== null && (
        <span className="mt-1 block font-mono text-xs text-mute">Request id {error.requestId}</span>
      )}
    </Alert>
  );
}
