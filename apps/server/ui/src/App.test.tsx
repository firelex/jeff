import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { App } from './App';
import { SESSION_KEY } from './session';
import { TRIAGE_ROUTE } from './test/fixtures';

type Handler = (init: RequestInit) => Response | Promise<Response>;

function jsonResponse(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', 'x-request-id': `req-${status}` } });
}

const unauthorised = () => jsonResponse(401, {
  error: { type: 'Unauthorised', message: 'The credentials are not valid on this server: not a current console session', details: {} },
});

function serve(routes: Record<string, Handler>) {
  const fetch = vi.fn(async (path: string, init: RequestInit) => {
    const handler = routes[`${init.method ?? 'GET'} ${path}`];
    if (!handler) throw new Error(`No mocked answer for ${init.method ?? 'GET'} ${path}`);
    return handler(init);
  });
  vi.stubGlobal('fetch', fetch);
  return fetch;
}

const SIGNED_IN: Record<string, Handler> = {
  'GET /ui/me': () => jsonResponse(200, { user: 'menlo@scissero.com', kind: 'console', admin: false }),
  'GET /health': () => jsonResponse(200, { status: 'ready' }),
  'POST /ui/events': () => jsonResponse(200, { logged: true }),
  'GET /v1/functions': () => jsonResponse(200, { base_format: { format: 'bf16' }, functions: [TRIAGE_ROUTE] }),
  'GET /ui/examples': () => jsonResponse(200, { examples: [] }),
};

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('the console session', () => {
  it('a 401 from any call clears the session and shows sign-in with the server\'s message', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 'removed-user-token' }));
    serve({ ...SIGNED_IN, 'GET /v1/functions': unauthorised });
    render(<App />);
    expect(await screen.findByText('The credentials are not valid on this server: not a current console session')).toBeTruthy();
    expect(screen.getByText('Unauthorised')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy();
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it('two concurrent 401s keep the first message and do not remount the sign-in screen', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 'removed-user-token' }));
    let releaseSecond: (response: Response) => void = () => {
      throw new Error('GET /v1/functions was never called');
    };
    serve({
      ...SIGNED_IN,
      'GET /ui/me': () => jsonResponse(401, { error: { type: 'Unauthorised', message: 'First refusal', details: {} } }),
      'GET /v1/functions': () => new Promise<Response>((resolve) => {
        releaseSecond = resolve;
      }),
    });
    const user = userEvent.setup();
    render(<App />);
    expect(await screen.findByText('First refusal')).toBeTruthy();
    await user.type(screen.getByLabelText('Login'), 'menlo@scissero.com');
    releaseSecond(jsonResponse(401, { error: { type: 'Unauthorised', message: 'Second refusal', details: {} } }));
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByText('First refusal')).toBeTruthy();
    expect(screen.queryByText('Second refusal')).toBeNull();
    expect((screen.getByLabelText('Login') as HTMLInputElement).value).toBe('menlo@scissero.com');
  });

  it('a 401 for an old token after signing in again leaves the new session in place', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 'old-token' }));
    let releaseOld: (response: Response) => void = () => {
      throw new Error('GET /v1/functions was never called with the old token');
    };
    const bearer = (init: RequestInit) => (init.headers as Record<string, string>).Authorization;
    serve({
      ...SIGNED_IN,
      'GET /ui/me': (init) => bearer(init) === 'Bearer old-token'
        ? unauthorised()
        : jsonResponse(200, { user: 'menlo@scissero.com', kind: 'console', admin: false }),
      'GET /v1/functions': (init) => bearer(init) === 'Bearer old-token'
        ? new Promise<Response>((resolve) => {
          releaseOld = resolve;
        })
        : jsonResponse(200, { base_format: { format: 'bf16' }, functions: [TRIAGE_ROUTE] }),
      'POST /ui/login': () => jsonResponse(200, { user: 'menlo@scissero.com', token: 'new-token' }),
    });
    const user = userEvent.setup();
    render(<App />);
    await screen.findByRole('button', { name: 'Sign in' });
    await user.type(screen.getByLabelText('Login'), 'menlo@scissero.com');
    await user.type(screen.getByLabelText('Password'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('jeff.support.triage')).toBeTruthy();
    releaseOld(unauthorised());
    await new Promise((resolve) => setTimeout(resolve, 20));
    expect(screen.getByText('jeff.support.triage')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Sign in' })).toBeNull();
    expect(JSON.parse(localStorage.getItem(SESSION_KEY) as string)).toEqual({ user: 'menlo@scissero.com', token: 'new-token' });
  });

  it('signs in, stores {user, token}, shows the shell, and sends the token on later calls', async () => {
    const fetch = serve({ ...SIGNED_IN, 'POST /ui/login': () => jsonResponse(200, { user: 'menlo@scissero.com', token: 'tok-1' }) });
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText('Login'), 'menlo@scissero.com');
    await user.type(screen.getByLabelText('Password'), 'secret');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('jeff.support.triage')).toBeTruthy();
    expect(JSON.parse(localStorage.getItem(SESSION_KEY) as string)).toEqual({ user: 'menlo@scissero.com', token: 'tok-1' });
    const functionsCall = fetch.mock.calls.find(([path]) => path === '/v1/functions');
    expect((functionsCall?.[1].headers as Record<string, string>).Authorization).toBe('Bearer tok-1');
    expect(screen.getByText('Jeff Console', { selector: 'b' })).toBeTruthy();
    for (const name of ['Home', 'Playground', 'Adapters', 'API reference']) {
      expect(screen.getByRole('button', { name })).toBeTruthy();
    }
    expect(screen.queryByRole('button', { name: 'Access log' })).toBeNull();
    const nav = screen.getByRole('navigation', { name: 'Console' });
    expect(nav.querySelector('[aria-current="page"]')?.textContent).toBe('Home');
  });

  it('a wrong password shows the server\'s message and stores nothing', async () => {
    serve({ 'POST /ui/login': () => jsonResponse(401, { error: { type: 'Unauthorised', message: 'Wrong email or password.', details: {} } }) });
    const user = userEvent.setup();
    render(<App />);
    await user.type(screen.getByLabelText('Login'), 'menlo@scissero.com');
    await user.type(screen.getByLabelText('Password'), 'wrong');
    await user.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('Wrong email or password.')).toBeTruthy();
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it('reports each view to POST /ui/events, shows the access log to admins, and signs out', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 't' }));
    const fetch = serve({ ...SIGNED_IN, 'GET /ui/me': () => jsonResponse(200, { user: 'menlo@scissero.com', kind: 'console', admin: true }) });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Access log' }));
    await waitFor(() => {
      const views = fetch.mock.calls.filter(([path]) => path === '/ui/events').map(([, init]) => JSON.parse(init.body as string).view);
      expect(views).toEqual(['home', 'access']);
    });
    await user.click(screen.getByRole('button', { name: 'Sign out' }));
    expect(screen.getByRole('button', { name: 'Sign in' })).toBeTruthy();
    expect(localStorage.getItem(SESSION_KEY)).toBeNull();
  });

  it('a failed page-view report is shown, not swallowed', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 't' }));
    serve({ ...SIGNED_IN, 'POST /ui/events': () => jsonResponse(429, { error: { type: 'RateLimited', message: 'Too many requests', details: {} } }) });
    render(<App />);
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('RateLimited: Too many requests');
    expect(alert.textContent).toContain('Request id req-429');
  });

  it('"try it" opens the playground with the example once; it is not applied again on a later visit', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 't' }));
    const example = { name: 'triage route', call: { function: TRIAGE_ROUTE.id, input: 'The box was crushed', context: { company: 'A shop', channel: 'email' }, labels: { k: 'Deliveries' } } };
    serve({ ...SIGNED_IN, 'GET /ui/examples': () => jsonResponse(200, { examples: [example] }) });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: `Try ${TRIAGE_ROUTE.id}` }));
    const input = await screen.findByLabelText('Input: message');
    expect((input as HTMLTextAreaElement).value).toBe('The box was crushed');
    await user.clear(input);
    await user.type(input, 'Edited');
    await user.click(screen.getByRole('button', { name: 'Home' }));
    await user.click(screen.getByRole('button', { name: 'Playground' }));
    expect(await screen.findByText('Choose a function, or load an example.')).toBeTruthy();
    expect(screen.queryByLabelText('Input: message')).toBeNull();
  });

  it('the owner (a console session) sees the Factory in the rail, and it opens the factory page', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 't' }));
    serve({
      ...SIGNED_IN,
      'GET /v1/factory/mode': () => jsonResponse(200, { mode: 'off', replays: true, description_check: false, preview: false }),
      'GET /v1/factory/replays': () => jsonResponse(200, { replays: [] }),
    });
    const user = userEvent.setup();
    render(<App />);
    await user.click(await screen.findByRole('button', { name: 'Factory' }));
    expect(await screen.findByRole('heading', { level: 1, name: 'Make an adapter' })).toBeTruthy();
    expect(await screen.findByText('No recorded runs yet.')).toBeTruthy();
    expect(window.location.hash).toBe('#factory');
  });

  it('a hub token (a customer, not the owner) has no Factory in the rail', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 't' }));
    const fetch = serve({ ...SIGNED_IN, 'GET /ui/me': () => jsonResponse(200, { user: 'customer-7', kind: 'hub', admin: false }) });
    render(<App />);
    expect(await screen.findByText('jeff.support.triage')).toBeTruthy();
    await waitFor(() => expect(fetch.mock.calls.some(([path]) => path === '/ui/me')).toBe(true));
    await new Promise((resolve) => setTimeout(resolve, 20)); // GET /ui/me has answered and been applied
    expect(screen.getByRole('button', { name: 'Adapters' })).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Factory' })).toBeNull();
  });

  it('"Documentation" is a link to the public hub\'s docs, opened in a new tab', async () => {
    localStorage.setItem(SESSION_KEY, JSON.stringify({ user: 'menlo@scissero.com', token: 't' }));
    serve(SIGNED_IN);
    render(<App />);
    const link = await screen.findByRole('link', { name: 'Documentation' });
    expect(link.getAttribute('href')).toBe(import.meta.env.VITE_JEFF_DOCS_URL);
    expect(link.getAttribute('target')).toBe('_blank');
    expect(link.getAttribute('rel')).toBe('noopener noreferrer');
  });
});
