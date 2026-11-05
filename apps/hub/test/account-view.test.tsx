import { act, fireEvent, render, screen } from '@testing-library/react';
import { createRoutesStub } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import Account, { LOADING, REQUEST_TIMEOUT_MS } from '../app/routes/account';

// The prerendered /account shell: it loads the account from GET /api/account and acts through the account
// endpoints' POSTs (a stubbed fetch here).
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });

const PAGE = {
  view: 'account', email: 'ada@example.com', status: 'active', periodEnd: '2026-12-03T10:00:00.000Z',
  cancelAtPeriodEnd: false, usage: { used: 12_500_000, allowance: 50_000_000 },
  keys: [{ id: 'k1', name: 'laptop', shown: 'jeff_live_abcdef…', createdAt: '2026-11-03T10:00:00.000Z', lastUsedAt: null }],
  playground: null,
};

type Reply = (url: string, init?: RequestInit) => Response;

// The page's own list of adapter requests (components/account/AdapterRequests.tsx), which every view loads too.
const NO_REQUESTS = '/api/adapter-requests';
const accountCalls = (fetchImpl: ReturnType<typeof vi.fn>) => fetchImpl.mock.calls.map(([url]) => url).filter((url) => url !== NO_REQUESTS);

// Answers each request by its path; every request is recorded. The list of adapter requests is empty unless a test
// answers it itself.
function hub(given: Record<string, Reply | Reply[]>) {
  const routes: Record<string, Reply | Reply[]> = { [NO_REQUESTS]: answer({ requests: [] }), ...given };
  const fetchImpl = vi.fn(async (url: string, init?: RequestInit) => {
    const route = routes[url];
    if (route === undefined) throw new Error(`no stubbed answer for ${url}`);
    const reply = Array.isArray(route) ? (route.length > 1 ? route.shift()! : route[0]) : route;
    return reply(url, init);
  });
  vi.stubGlobal('fetch', fetchImpl);
  return fetchImpl;
}

const answer = (body: unknown, status = 200): Reply => () => Response.json(body, { status });
const refusal = (status: number, type: string, message: string): Reply => answer({ error: { type, message, details: {} } }, status);

function show() {
  const Stub = createRoutesStub([
    { path: '/account', Component: Account },
    { path: '/signin', Component: () => <p>The sign-in page</p> },
    { path: '/', Component: () => <p>The home page</p> },
  ]);
  return render(<Stub initialEntries={['/account']} />);
}

it('says it is loading until the account arrives', async () => {
  hub({ '/api/account': answer(PAGE) });
  show();
  expect(screen.getByRole('status').textContent).toBe(LOADING);
  expect(await screen.findByText('laptop')).toBeTruthy();
});

it('shows the usage bar, the reset date and the keys', async () => {
  const fetchImpl = hub({ '/api/account': answer(PAGE) });
  show();
  const meter = await screen.findByRole('meter');
  expect(meter.getAttribute('aria-valuenow')).toBe('12500000');
  expect(meter.getAttribute('aria-valuemax')).toBe('50000000');
  expect(screen.getByText('12,500,000 of 50,000,000 input tokens used. Resets on 3 December 2026.')).toBeTruthy();
  expect(screen.getByText('Active. Next billing date: 3 December 2026.')).toBeTruthy();
  expect(screen.getByText('laptop')).toBeTruthy();
  expect(screen.getByText('jeff_live_abcdef…')).toBeTruthy();
  expect(screen.getByText('never')).toBeTruthy();
  for (const name of ['Create key', 'Revoke laptop', 'Manage billing', 'Sign out', 'Sign out everywhere', 'Delete account']) {
    expect(screen.getByRole('button', { name })).toBeTruthy();
  }
  expect(fetchImpl).toHaveBeenCalledWith('/api/account', expect.objectContaining({ credentials: 'same-origin' }));
});

it('says when the subscription ends instead of renewing, and when a payment failed', async () => {
  hub({ '/api/account': answer({ ...PAGE, cancelAtPeriodEnd: true }) });
  const { unmount } = show();
  expect(await screen.findByText('Active until 3 December 2026; it will not renew.')).toBeTruthy();
  unmount();
  hub({ '/api/account': answer({ ...PAGE, status: 'past_due' }) });
  show();
  expect(await screen.findByText('Payment failed; Stripe is retrying. Your keys keep working meanwhile. Next billing date: 3 December 2026.')).toBeTruthy();
});

it("shows the owner the playground's month", async () => {
  hub({ '/api/account': answer({ ...PAGE, playground: { used: 77, since: '2026-11-01T00:00:00.000Z' } }) });
  show();
  expect(await screen.findByText('Playground since 1 November 2026: 77 input tokens.')).toBeTruthy();
});

it('the subscribe screen has one button to Checkout, which goes to the address the hub answers', async () => {
  const assign = vi.fn();
  vi.stubGlobal('location', { ...window.location, assign });
  const fetchImpl = hub({
    '/api/account': answer({ view: 'subscribe', email: 'ada@example.com', price: '$1', allowance: '50 million', status: null, playground: null }),
    '/api/account/checkout': answer({ url: 'https://checkout.stripe.test/u1' }),
  });
  show();
  expect(await screen.findByText('$1 a month, 50 million input tokens, cancel any time.')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Manage billing' })).toBeNull();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Subscribe with Stripe' })); });
  expect(fetchImpl).toHaveBeenCalledWith('/api/account/checkout', expect.objectContaining({ method: 'POST', credentials: 'same-origin' }));
  expect(assign).toHaveBeenCalledWith('https://checkout.stripe.test/u1');
});

it('a refused Checkout says why, in the hub\'s words', async () => {
  hub({
    '/api/account': answer({ view: 'subscribe', email: 'ada@example.com', price: '$1', allowance: '50 million', status: 'unpaid', playground: null }),
    '/api/account/checkout': refusal(409, 'BillingRefused', 'Your last invoice is unpaid: pay it with "Manage billing"'),
  });
  show();
  expect(await screen.findByText('Your last subscription is unpaid.')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Manage billing' })).toBeTruthy();
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Subscribe with Stripe' })); });
  expect(screen.getByRole('alert').textContent).toBe('Your last invoice is unpaid: pay it with "Manage billing".');
});

it('creating a key shows it once, then the list again; the key is sent nowhere else', async () => {
  const key = `jeff_live_${'a'.repeat(52)}`;
  const withTwo = { ...PAGE, keys: [...PAGE.keys, { id: 'k2', name: 'ci', shown: 'jeff_live_aaaaaa…', createdAt: '2026-11-04T10:00:00.000Z', lastUsedAt: null }] };
  const fetchImpl = hub({
    '/api/account': [answer(PAGE), answer(withTwo)],
    '/api/account/create-key': answer({ created: { key, name: 'ci' } }),
  });
  show();
  fireEvent.change(await screen.findByLabelText('Key name'), { target: { value: 'ci' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Create key' })); });
  const [, init] = fetchImpl.mock.calls.find(([url]) => url === '/api/account/create-key')!;
  expect(init).toMatchObject({ method: 'POST', body: '{"name":"ci"}', headers: { 'content-type': 'application/json' } });
  expect(screen.getByText(key)).toBeTruthy();
  // The focused heading names the key and is described by the sentence, so a screen reader reads both on the focus;
  // the words are said once, with no live region filled as it appears.
  const heading = screen.getByRole('heading', { name: 'Your new key "ci"' });
  expect(document.activeElement).toBe(heading);
  const description = document.getElementById(heading.getAttribute('aria-describedby')!)!;
  expect(description.textContent).toBe('This is the only time it is shown: copy it now.');
  expect(description.getAttribute('role')).toBeNull();
  expect(screen.queryAllByText(/Your new key/)).toEqual([heading]);
  expect(screen.getByText('jeff_live_aaaaaa…')).toBeTruthy();
  expect(fetchImpl.mock.calls.filter(([url]) => url === '/api/account')).toHaveLength(2);
  for (const [url] of fetchImpl.mock.calls) expect(url).not.toContain(key);
});

it('a refused key says why', async () => {
  hub({
    '/api/account': answer(PAGE),
    '/api/account/create-key': refusal(409, 'KeyRefused', 'You have 10 live keys, the most an account may hold; revoke one first'),
  });
  show();
  fireEvent.change(await screen.findByLabelText('Key name'), { target: { value: 'ci' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Create key' })); });
  expect(screen.getByRole('alert').textContent).toBe('You have 10 live keys, the most an account may hold; revoke one first.');
});

it('revoking a key asks first, in the site\'s own dialog, then does it', async () => {
  const fetchImpl = hub({
    '/api/account': [answer(PAGE), answer({ ...PAGE, keys: [] })],
    '/api/account/revoke-key': answer({ done: 'The key is revoked. It stops working within about a minute.' }),
  });
  show();
  const revoke = await screen.findByRole('button', { name: 'Revoke laptop' });
  await act(async () => { fireEvent.click(revoke); });
  // Not the browser's own confirm(): the site's dialog, asking the same question.
  const dialog = screen.getByRole('dialog');
  expect(dialog.textContent).toContain('Revoke the key "laptop"? Programs that use it stop working within about a minute, '
    + 'and it cannot be brought back.');
  expect(accountCalls(fetchImpl)).toEqual(['/api/account']);
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Revoke key' })); });
  expect(screen.queryByRole('dialog')).toBeNull();
  const [, init] = fetchImpl.mock.calls.find(([url]) => url === '/api/account/revoke-key')!;
  expect(init).toMatchObject({ method: 'POST', body: '{"key":"k1"}' });
  expect(screen.getByText('The key is revoked. It stops working within about a minute.').getAttribute('role')).toBe('status');
  expect(screen.getByText('No keys yet. Create one below.')).toBeTruthy();
});

it('sign out everywhere goes to the sign-in page', async () => {
  const fetchImpl = hub({ '/api/account': answer(PAGE), '/api/account/sign-out-everywhere': answer({ signedOut: 'u1' }) });
  show();
  const everywhere = await screen.findByRole('button', { name: 'Sign out everywhere' });
  await act(async () => { fireEvent.click(everywhere); });
  expect(fetchImpl).toHaveBeenCalledWith('/api/account/sign-out-everywhere', expect.objectContaining({ method: 'POST' }));
  expect(await screen.findByText('The sign-in page')).toBeTruthy();
});

it('deleting needs the word; with it the account is gone and the page goes home', async () => {
  const fetchImpl = hub({
    '/api/account': answer(PAGE),
    '/api/account/delete': [refusal(400, 'ConfirmationRequired', 'Type delete in the box to confirm deleting the account.'), answer({ deleted: 'u1' })],
  });
  show();
  fireEvent.change(await screen.findByLabelText('Type delete to confirm'), { target: { value: 'yes' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete account' })); });
  expect(screen.getByRole('alert').textContent).toBe('Type delete in the box to confirm deleting the account.');
  fireEvent.change(screen.getByLabelText('Type delete to confirm'), { target: { value: 'delete' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete account' })); });
  const bodies = fetchImpl.mock.calls.filter(([url]) => url === '/api/account/delete').map(([, init]) => init?.body);
  expect(bodies).toEqual(['{"confirm":"yes"}', '{"confirm":"delete"}']);
  expect(await screen.findByText('The home page')).toBeTruthy();
});

it('a visitor who is not signed in goes to the sign-in page', async () => {
  hub({ '/api/account': refusal(401, 'SignInRequired', 'sign in first') });
  show();
  expect(await screen.findByText('The sign-in page')).toBeTruthy();
});

it('any other answer is shown as what it was', async () => {
  hub({ '/api/account': refusal(500, 'MissingSetting', 'the hub cannot serve /api/account: STRIPE_PRICE_ID is not set') });
  show();
  expect((await screen.findByRole('alert')).textContent)
    .toBe('Could not load your account. The hub cannot serve /api/account: STRIPE_PRICE_ID is not set.');
});

it('cancelling the confirmation revokes nothing', async () => {
  const fetchImpl = hub({ '/api/account': answer(PAGE) });
  show();
  const revoke = await screen.findByRole('button', { name: 'Revoke laptop' });
  revoke.focus();
  await act(async () => { fireEvent.click(revoke); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Cancel' })); });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(accountCalls(fetchImpl)).toEqual(['/api/account']);
  // Focus goes back to the button that opened the dialog.
  expect(document.activeElement).toBe(revoke);
});

it('Escape also declines the confirmation, giving focus back to the button that opened it', async () => {
  const fetchImpl = hub({ '/api/account': answer(PAGE) });
  show();
  const revoke = await screen.findByRole('button', { name: 'Revoke laptop' });
  revoke.focus();
  await act(async () => { fireEvent.click(revoke); });
  expect(screen.getByRole('dialog')).toBeTruthy();
  await act(async () => { fireEvent.keyDown(window, { key: 'Escape' }); });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(accountCalls(fetchImpl)).toEqual(['/api/account']);
  expect(document.activeElement).toBe(revoke);
});

it('both inputs have visible labels', async () => {
  hub({ '/api/account': answer(PAGE) });
  show();
  expect((await screen.findByText('Key name')).tagName).toBe('LABEL');
  expect(screen.getByText('Type delete to confirm').tagName).toBe('LABEL');
});

it('if the list cannot be refreshed after a create, the new key stays on screen and the failure is said', async () => {
  const key = `jeff_live_${'b'.repeat(52)}`;
  hub({
    '/api/account': [answer(PAGE), refusal(500, 'Internal', 'the database did not answer')],
    '/api/account/create-key': answer({ created: { key, name: 'ci' } }),
  });
  show();
  fireEvent.change(await screen.findByLabelText('Key name'), { target: { value: 'ci' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Create key' })); });
  expect(screen.getByText(key)).toBeTruthy();
  expect(screen.getByText('laptop')).toBeTruthy();
  expect(screen.getByRole('alert').textContent)
    .toBe('The list of keys could not be refreshed. The database did not answer. Reload the page to see it.');
});

it('the new key is dropped when the page is left, so coming back (the back-forward cache) does not show it', async () => {
  const key = `jeff_live_${'c'.repeat(52)}`;
  hub({ '/api/account': answer(PAGE), '/api/account/create-key': answer({ created: { key, name: 'ci' } }) });
  show();
  fireEvent.change(await screen.findByLabelText('Key name'), { target: { value: 'ci' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Create key' })); });
  expect(screen.getByText(key)).toBeTruthy();
  act(() => { window.dispatchEvent(new Event('pagehide')); });
  // pageshow from the back-forward cache: the page as it was left.
  act(() => { window.dispatchEvent(new Event('pageshow')); });
  expect(screen.queryByText(key)).toBeNull();
  expect(screen.getByText('laptop')).toBeTruthy();
});

it('the new key is dropped before going to Stripe', async () => {
  const key = `jeff_live_${'d'.repeat(52)}`;
  let shownAtAssign: boolean | null = null;
  const assign = vi.fn(() => { shownAtAssign = screen.queryByText(key) !== null; });
  vi.stubGlobal('location', { ...window.location, assign });
  hub({
    '/api/account': answer(PAGE),
    '/api/account/create-key': answer({ created: { key, name: 'ci' } }),
    '/api/account/portal': answer({ url: 'https://billing.stripe.test/cus_u1' }),
  });
  show();
  fireEvent.change(await screen.findByLabelText('Key name'), { target: { value: 'ci' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Create key' })); });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Manage billing' })); });
  expect(assign).toHaveBeenCalledWith('https://billing.stripe.test/cus_u1');
  expect(shownAtAssign).toBe(false);
});

it('a Stripe answer without an address is said, and goes nowhere', async () => {
  const assign = vi.fn();
  vi.stubGlobal('location', { ...window.location, assign });
  hub({ '/api/account': answer(PAGE), '/api/account/portal': answer({ url: 5 }) });
  show();
  const billing = await screen.findByRole('button', { name: 'Manage billing' });
  await act(async () => { fireEvent.click(billing); });
  expect(screen.getByRole('alert').textContent).toBe('Something went wrong: the hub\'s answer had no Stripe address to go to. Try again.');
  expect(assign).not.toHaveBeenCalled();
});

// A body whose reading fails part way: the connection dropped.
const cutOff: Reply = () => new Response(new ReadableStream({ pull(controller) { controller.error(new TypeError('network error')); } }),
  { headers: { 'content-type': 'application/json' } });

it('an answer whose body cannot be read is said, on loading and on an action, and nothing stays busy', async () => {
  hub({ '/api/account': cutOff });
  const { unmount } = show();
  expect((await screen.findByRole('alert')).textContent)
    .toBe('Could not load your account. Something went wrong: the request failed: network error. Try again.');
  unmount();
  hub({ '/api/account': answer(PAGE), '/api/account/sign-out-everywhere': cutOff });
  show();
  const everywhere = await screen.findByRole('button', { name: 'Sign out everywhere' });
  await act(async () => { fireEvent.click(everywhere); });
  expect(screen.getByRole('alert').textContent).toBe('Something went wrong: the request failed: network error. Try again.');
  expect((everywhere as HTMLButtonElement).disabled).toBe(false);
});

it('each request has its own time limit', async () => {
  vi.stubGlobal('fetch', vi.fn((_url: string, init?: RequestInit) => new Promise<Response>((_resolve, reject) => {
    init!.signal!.addEventListener('abort', () => reject(init!.signal!.reason));
  })));
  // The real limit is 10 seconds; the test runs it at 20 milliseconds.
  const short = AbortSignal.timeout.bind(AbortSignal);
  const timeout = vi.spyOn(AbortSignal, 'timeout').mockImplementation(() => short(20));
  show();
  expect((await screen.findByRole('alert')).textContent)
    .toBe('Could not load your account. Something went wrong: the hub did not answer within 10 seconds. Try again.');
  expect(timeout).toHaveBeenCalledWith(REQUEST_TIMEOUT_MS);
});

it.each([
  [0, '0', '0 of 50,000,000 input tokens used. Resets on 3 December 2026.'],
  [50_000_000, '50000000', '50,000,000 of 50,000,000 input tokens used: your allowance is used up, and calls are refused until it resets on 3 December 2026.'],
  [50_000_123, '50000000', '50,000,123 of 50,000,000 input tokens used: your allowance is used up, and calls are refused until it resets on 3 December 2026.'],
])('the usage bar at %i tokens', async (used, now, words) => {
  hub({ '/api/account': answer({ ...PAGE, usage: { used, allowance: 50_000_000 } }) });
  show();
  const meter = await screen.findByRole('meter');
  expect(meter.getAttribute('aria-valuenow')).toBe(now);
  expect(meter.getAttribute('aria-valuemax')).toBe('50000000');
  expect(meter.getAttribute('aria-valuetext')).toBe(words);
  expect(screen.getByText(words)).toBeTruthy();
  expect((meter.firstChild as HTMLElement).style.width).toBe(used === 0 ? '0%' : '100%');
});

it("the owner sees the playground's month on the subscribe screen too", async () => {
  hub({ '/api/account': answer({ view: 'subscribe', email: 'owner@example.com', price: '$1', allowance: '50 million', status: null,
    playground: { used: 9, since: '2026-11-01T00:00:00.000Z' } }) });
  show();
  expect(await screen.findByText('Playground since 1 November 2026: 9 input tokens.')).toBeTruthy();
});

it('"Copied." goes into a status region that is on the page before it is said', async () => {
  const key = `jeff_live_${'e'.repeat(52)}`;
  const writeText = vi.fn(async () => {});
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
  hub({ '/api/account': answer(PAGE), '/api/account/create-key': answer({ created: { key, name: 'ci' } }) });
  show();
  fireEvent.change(await screen.findByLabelText('Key name'), { target: { value: 'ci' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Create key' })); });
  const region = screen.getByRole('status');
  expect(region.textContent).toBe('');
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Copy' })); });
  expect(writeText).toHaveBeenCalledWith(key);
  expect(screen.getByRole('status')).toBe(region);
  expect(region.textContent).toBe('Copied.');
});


function timesOut(given: Record<string, Reply | Reply[] | 'never'>) {
  const routes: Record<string, Reply | Reply[] | 'never'> = { [NO_REQUESTS]: answer({ requests: [] }), ...given };
  const short = AbortSignal.timeout.bind(AbortSignal);
  vi.spyOn(AbortSignal, 'timeout').mockImplementation(() => short(20));
  const fetchImpl = vi.fn((url: string, init?: RequestInit) => {
    const route = routes[url];
    if (route === undefined) throw new Error(`no stubbed answer for ${url}`);
    if (route === 'never') {
      return new Promise<Response>((_resolve, reject) => { init!.signal!.addEventListener('abort', () => reject(init!.signal!.reason)); });
    }
    const reply = Array.isArray(route) ? (route.length > 1 ? route.shift()! : route[0]) : route;
    return Promise.resolve(reply(url, init));
  });
  vi.stubGlobal('fetch', fetchImpl);
  return fetchImpl;
}

it('a key create that times out refreshes the list and says the key may have been made', async () => {
  const withTwo = { ...PAGE, keys: [...PAGE.keys, { id: 'k2', name: 'ci', shown: 'jeff_live_ffffff…', createdAt: '2026-11-04T10:00:00.000Z', lastUsedAt: null }] };
  const fetchImpl = timesOut({ '/api/account': [answer(PAGE), answer(withTwo)], '/api/account/create-key': 'never' });
  show();
  fireEvent.change(await screen.findByLabelText('Key name'), { target: { value: 'ci' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Create key' })); });
  expect((await screen.findByRole('alert')).textContent).toBe('Something went wrong: the hub did not answer within 10 seconds, '
    + 'so the key may have been made. If it appears in the list, revoke it: its secret was never shown.');
  expect(screen.getByText('jeff_live_ffffff…')).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Revoke ci' })).toBeTruthy();
  expect(fetchImpl.mock.calls.filter(([url]) => url === '/api/account')).toHaveLength(2);
  expect((screen.getByRole('button', { name: 'Create key' }) as HTMLButtonElement).disabled).toBe(false);
});

it('a key create that times out, then a refresh that fails, says both', async () => {
  timesOut({ '/api/account': [answer(PAGE), refusal(500, 'Internal', 'the database did not answer')], '/api/account/create-key': 'never' });
  show();
  fireEvent.change(await screen.findByLabelText('Key name'), { target: { value: 'ci' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Create key' })); });
  expect((await screen.findByRole('alert')).textContent).toBe('Something went wrong: the hub did not answer within 10 seconds, '
    + 'so the key may have been made. If it appears in the list, revoke it: its secret was never shown. '
    + 'The list of keys could not be refreshed. The database did not answer. Reload the page to see it.');
});

it('a deletion that times out says the account may have been deleted, and goes to sign in when it was', async () => {
  timesOut({ '/api/account': [answer(PAGE), refusal(401, 'SignInRequired', 'sign in first')], '/api/account/delete': 'never' });
  show();
  fireEvent.change(await screen.findByLabelText('Type delete to confirm'), { target: { value: 'delete' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete account' })); });
  expect(await screen.findByText('The sign-in page')).toBeTruthy();
});

it('a deletion that times out while the account is still there says it may have been deleted', async () => {
  const fetchImpl = timesOut({ '/api/account': answer(PAGE), '/api/account/delete': 'never' });
  show();
  fireEvent.change(await screen.findByLabelText('Type delete to confirm'), { target: { value: 'delete' } });
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Delete account' })); });
  expect((await screen.findByRole('alert')).textContent).toBe('Something went wrong: the hub did not answer within 10 seconds, '
    + 'so the account may have been deleted. Reload the page in a moment: if you are signed out, it was.');
  expect(fetchImpl.mock.calls.filter(([url]) => url === '/api/account')).toHaveLength(2);
});

it('lists the adapter requests, newest first, with a declined one\'s reason, and links to the form', async () => {
  hub({
    '/api/account': answer(PAGE),
    '/api/adapter-requests': answer({ requests: [
      { id: 'AR-BBBBBBBB', question: 'Is this email urgent?', status: 'in_review', createdAt: '2026-11-04T10:00:00.000Z', updatedAt: '2026-11-05T10:00:00.000Z', declineReason: null },
      { id: 'AR-AAAAAAAA', question: 'Which team should handle this email?', status: 'declined', createdAt: '2026-11-03T10:00:00.000Z', updatedAt: '2026-11-03T10:00:00.000Z', declineReason: 'Too few real examples.' },
    ] }),
  });
  show();
  const section = await screen.findByRole('region', { name: 'Your adapter requests' });
  expect(await screen.findByText('Is this email urgent?')).toBeTruthy();
  expect(section.textContent).toContain('AR-BBBBBBBB · In review · sent 4 November 2026 · updated 5 November 2026');
  expect(section.textContent).toContain('AR-AAAAAAAA · Declined · sent 3 November 2026');
  expect(section.textContent).toContain('Why we declined it: Too few real examples.');
  expect(screen.getByRole('link', { name: 'Request an adapter' }).getAttribute('href')).toBe('/request-adapter');
});

it('says when the adapter requests could not be loaded, and the rest of the account still shows', async () => {
  hub({ '/api/account': answer(PAGE), '/api/adapter-requests': refusal(500, 'MissingSetting', 'the hub cannot serve /api/adapter-requests: MAIL_FROM is not set') });
  show();
  expect(await screen.findByText('Could not load your adapter requests. The hub cannot serve /api/adapter-requests: MAIL_FROM is not set.')).toBeTruthy();
  expect(screen.getByText('laptop')).toBeTruthy();
});
