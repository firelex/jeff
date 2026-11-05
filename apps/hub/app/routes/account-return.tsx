// /account/return: where Stripe Checkout sends the customer back (Checkout's success_url, app/lib/billing.server.ts).
// The redirect proves nothing (anyone can open this address, and its ?session_id= is never read): the page is a
// prerendered shell that, in the browser, asks the hub's function (GET /api/account/status) every two seconds until
// Stripe's webhook has made the subscription usable, then goes to the account. A visitor who is not signed in goes to
// /signin. While Stripe reports the subscription incomplete (a payment not finished, such as a card check with the
// bank), it says so and how to finish, and goes on asking. After 90 seconds it stops and says so. Any other answer, a
// request that fails or takes longer than 10 seconds, or an answer that is not the status, stops it with an alert
// that says what happened.
import { useEffect, useState } from 'react';
import { useNavigate } from 'react-router';
import { PAGE_TITLE } from '../components/styles';

export const POLL_MS = 2000;
export const GIVE_UP_MS = 90_000;
export const REQUEST_TIMEOUT_MS = 10_000;

export const WAITING = 'Waiting for Stripe to confirm your payment…';
export const GAVE_UP = 'Stripe has not confirmed your payment yet. Reload this page in a minute; your account page '
  + 'shows the subscription as soon as Stripe confirms it.';
export const INCOMPLETE = 'Your payment is not complete yet: Stripe is waiting for you to finish it, for example to '
  + 'confirm the card with your bank on the payment page. Finish it there, or go back to your account and choose '
  + 'Subscribe again (the unfinished payment is then cancelled).';
export const CHECK_FAILED = 'Could not check your subscription';

type Answer = { kind: 'answer'; status: number; body: string } | { kind: 'failed'; reason: string };

// One request for the status, with its own time limit. A failure of the request itself (no network, the time limit)
// is returned as what it is, for the page to show; it is never retried silently.
async function askStatus(): Promise<Answer> {
  try {
    const response = await fetch('/api/account/status', { credentials: 'same-origin', headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) });
    // Every answer's body is read to its end, even one the page only needs the status of: an unread body keeps the
    // request open in the browser.
    return { kind: 'answer', status: response.status, body: await response.text() };
  } catch (error) {
    // AbortSignal.timeout's error is a DOMException named TimeoutError (compared by name: a test DOM's DOMException
    // class is not the browser's).
    if (typeof error === 'object' && error !== null && (error as { name?: unknown }).name === 'TimeoutError') {
      return { kind: 'failed', reason: `the hub did not answer within ${REQUEST_TIMEOUT_MS / 1000} seconds` };
    }
    if (error instanceof TypeError) return { kind: 'failed', reason: `the request failed: ${error.message}` };
    throw error;
  }
}

function statusOf(body: string): { usable: boolean; status: string | null } | null {
  let parsed: unknown;
  try {
    parsed = JSON.parse(body);
  } catch (error) {
    if (error instanceof SyntaxError) return null;
    throw error;
  }
  if (typeof parsed !== 'object' || parsed === null) return null;
  const { usable, status } = parsed as { usable?: unknown; status?: unknown };
  if (typeof usable !== 'boolean' || (status !== null && typeof status !== 'string')) return null;
  return { usable, status };
}

export function meta() {
  return [{ title: 'Confirming your payment | Jeff' }];
}

export default function AccountReturn() {
  const navigate = useNavigate();
  const [message, setMessage] = useState(WAITING);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    const started = Date.now();
    let live = true;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const stop = (reason: string) => { setFailed(true); setMessage(`${CHECK_FAILED} (${reason}). Reload this page.`); };
    const poll = async () => {
      const answer = await askStatus();
      if (!live) return;
      if (answer.kind === 'failed') { stop(answer.reason); return; }
      if (answer.status === 401) { navigate('/signin', { replace: true }); return; }
      if (answer.status !== 200) { stop(`the hub answered ${answer.status}`); return; }
      const known = statusOf(answer.body);
      if (known === null) { stop('the hub\'s answer was not the subscription status'); return; }
      if (known.usable) { navigate('/account', { replace: true }); return; }
      setMessage(known.status === 'incomplete' ? INCOMPLETE : WAITING);
      if (Date.now() - started > GIVE_UP_MS) { if (known.status !== 'incomplete') setMessage(GAVE_UP); return; }
      timer = setTimeout(() => void poll(), POLL_MS);
    };
    void poll();
    return () => { live = false; if (timer !== undefined) clearTimeout(timer); };
  }, [navigate]);
  return (
    <main id="main" className="mx-auto max-w-md px-4 py-16">
      <h1 className={PAGE_TITLE}>Thank you</h1>
      <p role={failed ? 'alert' : 'status'} className={`mt-4 text-sm ${failed ? 'text-danger' : 'text-soft'}`}>{message}</p>
    </main>
  );
}
