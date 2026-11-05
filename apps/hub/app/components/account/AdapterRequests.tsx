// "Your adapter requests" on /account: the signed-in visitor's own requests (GET /api/adapter-requests), newest
// first, each with its question, status, dates and, when declined, why. Loaded on its own after the account, so the
// rest of the page never waits for it; a failure is said here.
import { useEffect, useState } from 'react';
import { Link } from 'react-router';
import { STATUS_LABELS, type OwnRequest, type OwnRequests } from '../../lib/adapter-request';
import { askHub, refusalOf } from '../../lib/ask-hub';
import { longDate } from '../../lib/dates';
import { CARD, SECTION_HEADING, TEXT_LINK } from '../styles';

type State = { kind: 'loading' } | { kind: 'failed'; message: string } | { kind: 'ready'; requests: OwnRequest[] };

function isOwnRequests(body: unknown): body is OwnRequests {
  return Array.isArray((body as { requests?: unknown } | null)?.requests);
}

export function AdapterRequests() {
  const [state, setState] = useState<State>({ kind: 'loading' });

  useEffect(() => {
    let live = true;
    void askHub('/api/adapter-requests', null).then((answer) => {
      if (!live) return;
      if (answer.kind === 'answer' && answer.status === 200 && isOwnRequests(answer.body)) setState({ kind: 'ready', requests: answer.body.requests });
      else setState({ kind: 'failed', message: `Could not load your adapter requests. ${refusalOf(answer)}` });
    });
    return () => { live = false; };
  }, []);

  return (
    <section className={`mt-8 p-5 sm:p-6 ${CARD}`} aria-label="Your adapter requests">
      <h2 className={SECTION_HEADING}>Your adapter requests</h2>
      <p className="mt-3 text-sm text-soft">
        Need a decision that is not in the catalogue? <Link to="/request-adapter" className={TEXT_LINK}>Request an adapter</Link>.
      </p>
      {state.kind === 'loading' && <p role="status" className="mt-4 text-sm text-soft">Loading your adapter requests…</p>}
      {state.kind === 'failed' && <p role="alert" className="mt-4 text-sm text-danger">{state.message}</p>}
      {state.kind === 'ready' && state.requests.length === 0 && <p className="mt-4 text-sm text-soft">You have not requested an adapter yet.</p>}
      {state.kind === 'ready' && state.requests.length > 0 && (
        <ul className="mt-4 flex flex-col gap-3">
          {state.requests.map((request) => (
            <li key={request.id} className="border border-rule bg-background p-3">
              <p className="break-words text-text">{request.question}</p>
              <p className="mt-1 font-mono text-xs text-mute">
                {`${request.id} · ${STATUS_LABELS[request.status]} · sent ${longDate(request.createdAt)}`}
                {request.updatedAt !== request.createdAt ? ` · updated ${longDate(request.updatedAt)}` : ''}
              </p>
              {request.declineReason !== null && <p className="mt-2 text-sm text-soft">{`Why we declined it: ${request.declineReason}`}</p>}
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
