import { act, render, screen } from '@testing-library/react';
import { createRoutesStub } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import AccountReturn, { GAVE_UP, GIVE_UP_MS, INCOMPLETE, POLL_MS, REQUEST_TIMEOUT_MS, WAITING } from '../app/routes/account-return';

// The prerendered /account/return shell: it waits for GET /api/account/status (the webhook's word, never the
// redirect from Stripe) to say the subscription is usable.
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

function answers(...responses: (() => Response)[]) {
  const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => {
    const next = responses.length > 1 ? responses.shift()! : responses[0];
    return next();
  });
  vi.stubGlobal('fetch', fetchImpl);
  return fetchImpl;
}

const status = (usable: boolean, value: string | null) => () => Response.json({ account: 'u1', usable, status: value });

function page() {
  const Stub = createRoutesStub([
    { path: '/account/return', Component: AccountReturn },
    { path: '/account', Component: () => <p>The account page</p> },
    { path: '/signin', Component: () => <p>The sign-in page</p> },
  ]);
  return render(<Stub initialEntries={['/account/return?session_id=cs_test_1']} />);
}

it('waits until the webhook has made the subscription usable, then goes to the account', async () => {
  vi.useFakeTimers();
  const fetchImpl = answers(status(false, null), status(false, 'incomplete'), status(true, 'active'));
  page();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByRole('status').textContent).toBe(WAITING);
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
  expect(screen.queryByText('The account page')).toBeNull();
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
  expect(screen.getByText('The account page')).toBeTruthy();
  expect(fetchImpl).toHaveBeenCalledTimes(3);
  expect(fetchImpl).toHaveBeenCalledWith('/api/account/status', expect.objectContaining({ credentials: 'same-origin' }));
});

it('gives up after 90 seconds and says so', async () => {
  vi.useFakeTimers();
  const fetchImpl = answers(status(false, null));
  page();
  await act(async () => { await vi.advanceTimersByTimeAsync(GIVE_UP_MS + POLL_MS); });
  expect(screen.getByRole('status').textContent).toBe(GAVE_UP);
  const calls = fetchImpl.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(10 * POLL_MS); });
  expect(fetchImpl.mock.calls.length).toBe(calls);
});

it('a visitor who is not signed in goes to the sign-in page', async () => {
  answers(() => Response.json({ error: { type: 'SignInRequired', message: 'sign in first', details: {} } }, { status: 401 }));
  page();
  expect(await screen.findByText('The sign-in page')).toBeTruthy();
});

it('any other answer stops it, naming the status', async () => {
  answers(() => new Response('{}', { status: 500 }));
  page();
  expect((await screen.findByRole('alert')).textContent).toBe('Could not check your subscription (the hub answered 500). Reload this page.');
});

it('an incomplete payment is named, with how to finish it, and the page goes on asking', async () => {
  vi.useFakeTimers();
  answers(status(false, 'incomplete'), status(true, 'active'));
  page();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(screen.getByRole('status').textContent).toBe(INCOMPLETE);
  expect(INCOMPLETE).toContain('Subscribe again');
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
  expect(screen.getByText('The account page')).toBeTruthy();
});

it('a request that fails stops with an alert naming the failure', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => { throw new TypeError('Failed to fetch'); }));
  page();
  expect((await screen.findByRole('alert')).textContent)
    .toBe('Could not check your subscription (the request failed: Failed to fetch). Reload this page.');
});

it('each request has its own time limit; one past it stops with an alert', async () => {
  const fetchImpl = vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init!.signal!.addEventListener('abort', () => reject(init!.signal!.reason));
  }));
  vi.stubGlobal('fetch', fetchImpl);
  // The real limit is 10 seconds; the test runs it at 20 milliseconds.
  const short = AbortSignal.timeout.bind(AbortSignal);
  const timeout = vi.spyOn(AbortSignal, 'timeout').mockImplementation(() => short(20));
  page();
  expect((await screen.findByRole('alert')).textContent)
    .toBe('Could not check your subscription (the hub did not answer within 10 seconds). Reload this page.');
  expect(timeout).toHaveBeenCalledWith(REQUEST_TIMEOUT_MS);
  timeout.mockRestore();
});

it.each([['not JSON', '<html>'], ['JSON of another shape', '{"ok":true}']])('an answer that is %s stops with an alert', async (_what, body) => {
  answers(() => new Response(body, { status: 200 }));
  page();
  expect((await screen.findByRole('alert')).textContent)
    .toBe("Could not check your subscription (the hub's answer was not the subscription status). Reload this page.");
});
