import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { createRoutesStub } from 'react-router';
import { afterEach, expect, it, vi } from 'vitest';
import SignIn from '../app/routes/signin';

// The prerendered /signin and /signup shell: what the build gives it (the Turnstile site key, the price, the
// allowance and the sign-in methods SIGN_IN_PROVIDERS lists), and what it asks the hub's function for once it runs in
// the browser (GET /api/signin-options: whether the visitor is signed in already, the methods the function offers,
// and whether the test provider is on).
afterEach(() => vi.unstubAllGlobals());

const ALL = ['github', 'google', 'email'];

function answer(options: { signedIn: boolean; testProvider: boolean; providers?: string[] }) {
  const fetchImpl = vi.fn(async (_url: string, _init?: RequestInit) => Response.json({ providers: ALL, ...options }));
  vi.stubGlobal('fetch', fetchImpl);
  return fetchImpl;
}

function page(data: object, entry = '/signin') {
  const Stub = createRoutesStub([
    { id: 'signin', path: '/signin', Component: SignIn as never, loader: () => data },
    { id: 'signup', path: '/signup', Component: SignIn as never, loader: () => data },
    { path: '/account', Component: () => <p>The account page</p> },
    { path: '/request-adapter', Component: () => <p>The request page</p> },
  ]);
  return render(<Stub initialEntries={[entry]} />);
}

const SIGNIN = { siteKey: 'site', price: '$1', allowance: '50 million', providers: ALL };

it('offers the three ways to sign in, and the test provider only when it is on', async () => {
  const fetchImpl = answer({ signedIn: false, testProvider: false });
  page(SIGNIN);
  expect(await screen.findByRole('heading', { level: 1, name: 'Sign in' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Continue with GitHub' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Continue with Google' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Email me a link' })).toBeTruthy();
  await waitFor(() => expect(fetchImpl).toHaveBeenCalledWith('/api/signin-options', expect.objectContaining({ credentials: 'same-origin' })));
  expect(screen.queryByRole('button', { name: 'Continue with the test provider' })).toBeNull();
  expect(screen.queryByText('$1 a month, 50 million input tokens, cancel any time.')).toBeNull();
});

it('the sign-up page carries the pricing line, and the test provider when it is on', async () => {
  answer({ signedIn: false, testProvider: true });
  page(SIGNIN, '/signup');
  expect(await screen.findByRole('heading', { level: 1, name: 'Get an API key' })).toBeTruthy();
  expect(await screen.findByText('$1 a month, 50 million input tokens, cancel any time.')).toBeTruthy();
  expect(await screen.findByRole('button', { name: 'Continue with the test provider' })).toBeTruthy();
});

it('a visitor who is signed in already goes to the account page', async () => {
  answer({ signedIn: true, testProvider: false });
  page(SIGNIN);
  expect(await screen.findByText('The account page')).toBeTruthy();
});

it('comes back to the page that sent the visitor (?next=/request-adapter), and to the account for any other ?next', async () => {
  answer({ signedIn: true, testProvider: false });
  const { unmount } = page(SIGNIN, '/signin?next=%2Frequest-adapter');
  expect(await screen.findByText('The request page')).toBeTruthy();
  unmount();
  answer({ signedIn: true, testProvider: false });
  page(SIGNIN, '/signin?next=%2F%2Fevil.example');
  expect(await screen.findByText('The account page')).toBeTruthy();
});

it('says why a used or expired link did not sign in', async () => {
  answer({ signedIn: false, testProvider: false });
  page(SIGNIN, '/signin?error=INVALID_TOKEN');
  expect((await screen.findByRole('alert')).textContent).toBe('That sign-in link has expired or was already used. Ask for a new one below.');
});

it('shows an error code it has no words for as it is, never hiding it', async () => {
  answer({ signedIn: false, testProvider: false });
  page(SIGNIN, '/signin?error=something_new');
  expect((await screen.findByRole('alert')).textContent).toBe('Sign-in failed: something_new');
});

it('does not send the email form before the bot check has passed', async () => {
  const fetchImpl = answer({ signedIn: false, testProvider: false });
  page(SIGNIN);
  fireEvent.change(await screen.findByLabelText('Or sign in with a link sent to your email'), { target: { value: 'ada@example.com' } });
  fireEvent.click(screen.getByRole('button', { name: 'Email me a link' }));
  expect((await screen.findByRole('alert')).textContent).toBe('Wait for the bot check above to finish, then try again.');
  expect(fetchImpl.mock.calls.map(([url]) => url)).toEqual(['/api/signin-options']);
});

it('says when the function cannot answer, instead of guessing', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => new Response('down', { status: 500 })));
  page(SIGNIN);
  expect((await screen.findByRole('alert')).textContent).toBe('The sign-in service answered 500. Reload the page to try again.');
});

it('shows only the methods SIGN_IN_PROVIDERS lists: github,email has no Google button', async () => {
  answer({ signedIn: false, testProvider: false, providers: ['github', 'email'] });
  page({ ...SIGNIN, providers: ['github', 'email'] });
  expect(await screen.findByRole('button', { name: 'Continue with GitHub' })).toBeTruthy();
  expect(screen.getByRole('button', { name: 'Email me a link' })).toBeTruthy();
  expect(screen.getByLabelText('Or sign in with a link sent to your email')).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Continue with Google' })).toBeNull();
});

it('without email listed, there is no email form; with only email, it is the one way in', async () => {
  answer({ signedIn: false, testProvider: false, providers: ['google'] });
  page({ ...SIGNIN, providers: ['google'] });
  expect(await screen.findByRole('button', { name: 'Continue with Google' })).toBeTruthy();
  expect(screen.queryByRole('button', { name: 'Email me a link' })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Continue with GitHub' })).toBeNull();
  cleanup();
  answer({ signedIn: false, testProvider: false, providers: ['email'] });
  page({ ...SIGNIN, providers: ['email'] });
  expect(await screen.findByLabelText('Sign in with a link sent to your email')).toBeTruthy();
  expect(screen.queryByRole('button', { name: /^Continue with/ })).toBeNull();
});

it('when the function offers other methods than the page was built with, it says so and offers none', async () => {
  answer({ signedIn: false, testProvider: true, providers: ['github', 'google', 'email'] });
  page({ ...SIGNIN, providers: ['github', 'email'] });
  expect((await screen.findByRole('alert')).textContent).toBe('This page was built to offer sign-in with github, email, but the '
    + "hub's function offers github, google, email. The site and its function must be deployed with the same SIGN_IN_PROVIDERS.");
  expect(screen.queryByRole('button', { name: /^Continue with/ })).toBeNull();
  expect(screen.queryByRole('button', { name: 'Email me a link' })).toBeNull();
});
