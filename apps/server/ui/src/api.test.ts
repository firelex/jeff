import { afterEach, describe, expect, it, vi } from 'vitest';
import { ApiError, NetworkError, createApi, errorFromResponse, serverRoot, shownError, signIn } from './api';

function jsonResponse(status: number, body: unknown, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json', ...headers } });
}

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('errorFromResponse', () => {
  it('reads type, message and details from the standard error body, and the request id from x-request-id', async () => {
    const error = await errorFromResponse(jsonResponse(422,
      { error: { type: 'MissingContext', message: 'jeff.support.triage.route needs context company', details: { missing: ['company'] } } },
      { 'x-request-id': 'req-123' }));
    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({ status: 422, type: 'MissingContext', message: 'jeff.support.triage.route needs context company',
      details: { missing: ['company'] }, requestId: 'req-123' });
  });

  it('reads the error body of /v1/systemone too, which also carries a legacy detail', async () => {
    const error = await errorFromResponse(jsonResponse(404,
      { error: { type: 'NotInstalled', message: 'No adapter x', details: {} }, detail: 'No adapter x' }, { 'x-request-id': 'r9' }));
    expect(error).toMatchObject({ status: 404, type: 'NotInstalled', message: 'No adapter x', requestId: 'r9' });
  });

  it('reports a body that is not a Jeff error under its HTTP status, with what came back', async () => {
    const error = await errorFromResponse(new Response('<html>Bad gateway</html>', { status: 502 }));
    expect(error.type).toBe('HTTP 502');
    expect(error.message).toContain('<html>Bad gateway</html>');
    expect(error.requestId).toBeNull();
  });
});

describe('createApi', () => {
  it('sends the session token as a bearer header, and same-origin cookies only (a gateway in front needs its own)', async () => {
    const fetch = vi.fn(async () => jsonResponse(200, { user: 'a@b.c', kind: 'console', admin: false }));
    vi.stubGlobal('fetch', fetch);
    const api = createApi({ token: 'tok', onUnauthorised: () => {} });
    await api.me();
    const [path, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe('/ui/me');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer tok');
    expect(init.credentials).toBe('same-origin');
  });

  it('throws ApiError for every non-2xx answer, and calls onUnauthorised for a 401 only', async () => {
    const onUnauthorised = vi.fn();
    const api = createApi({ token: 'tok', onUnauthorised });
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(403, { error: { type: 'NotLicensed', message: 'owner only', details: {} } })));
    await expect(api.setServed(['jeff.safety.spam'])).rejects.toMatchObject({ status: 403, type: 'NotLicensed' });
    expect(onUnauthorised).not.toHaveBeenCalled();

    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(401,
      { error: { type: 'Unauthorised', message: 'The credentials are not valid on this server', details: {} } }, { 'x-request-id': 'r1' })));
    await expect(api.functions()).rejects.toMatchObject({ status: 401, type: 'Unauthorised' });
    expect(onUnauthorised).toHaveBeenCalledTimes(1);
    expect(onUnauthorised.mock.calls[0][0]).toMatchObject({ message: 'The credentials are not valid on this server', requestId: 'r1' });
    expect(onUnauthorised.mock.calls[0][1]).toBe('tok');
  });

  it('PUT /v1/adapters/served sends {adapters}', async () => {
    const fetch = vi.fn(async () => jsonResponse(200, { adapters: [], adapters_version: 2 }));
    vi.stubGlobal('fetch', fetch);
    await createApi({ token: 't', onUnauthorised: () => {} }).setServed(['jeff.safety.spam']);
    const [path, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe('/v1/adapters/served');
    expect(init.method).toBe('PUT');
    expect(JSON.parse(init.body as string)).toEqual({ adapters: ['jeff.safety.spam'] });
  });

  it('decide returns the body with its request id and round trip', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(200, { decisions: [] }, { 'x-request-id': 'abc' })));
    const result = await createApi({ token: 't', onUnauthorised: () => {} }).decide({ calls: [] });
    expect(result.body).toEqual({ decisions: [] });
    expect(result.requestId).toBe('abc');
    expect(result.roundTripMs).toBeGreaterThanOrEqual(0);
  });
});

describe('signIn', () => {
  it('a wrong password is an ApiError with the server message, and sends no Authorization header', async () => {
    const fetch = vi.fn(async () => jsonResponse(401, { error: { type: 'Unauthorised', message: 'Wrong email or password.', details: {} } }));
    vi.stubGlobal('fetch', fetch);
    await expect(signIn('a@b.c', 'nope')).rejects.toMatchObject({ type: 'Unauthorised', message: 'Wrong email or password.' });
    const [, init] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect((init.headers as Record<string, string>).Authorization).toBeUndefined();
  });
});

describe('signIn answers', () => {
  it('an answer without a string user and token is an error, not a session', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(200, { user: 'a@b.c' })));
    await expect(signIn('a@b.c', 'x')).rejects.toThrow('POST /ui/login answered without a string user and token');
    vi.stubGlobal('fetch', vi.fn(async () => jsonResponse(200, { user: 'a@b.c', token: 't', extra: 1 })));
    await expect(signIn('a@b.c', 'x')).resolves.toEqual({ user: 'a@b.c', token: 't' });
  });
});

describe('shownError', () => {
  it('keeps type, message and request id of an ApiError, and the name and message of any other error', () => {
    expect(shownError(new ApiError({ status: 500, type: 'EngineError', message: 'boom', details: {}, requestId: 'x' })))
      .toEqual({ type: 'EngineError', message: 'boom', requestId: 'x' });
    expect(shownError(new SyntaxError('Unexpected token'))).toEqual({ type: 'SyntaxError', message: 'Unexpected token', requestId: null });
  });
});

describe('a server that cannot be reached', () => {
  it('a fetch that fails before any answer is a NetworkError naming the server address', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }));
    const failure = await createApi({ token: 't', onUnauthorised: () => {} }).health().catch((error: unknown) => error);
    expect(failure).toBeInstanceOf(NetworkError);
    expect(shownError(failure)).toEqual({
      type: 'NetworkError',
      message: `Could not reach the Jeff server at ${window.location.origin} (GET /health): Failed to fetch`,
      requestId: null,
    });
  });

  it('signing in to a server that cannot be reached says so too', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => {
      throw new TypeError('Failed to fetch');
    }));
    await expect(signIn('a@b.c', 'pw')).rejects.toThrow(`Could not reach the Jeff server at ${window.location.origin} (POST /ui/login)`);
  });
});

describe('serverRoot', () => {
  it('is the top of the address when the console is at /ui/', () => {
    expect(serverRoot('http://127.0.0.1:8795/ui/')).toEqual({ origin: 'http://127.0.0.1:8795', path: '' });
    expect(serverRoot('http://127.0.0.1:8795/ui/#docs/decide')).toEqual({ origin: 'http://127.0.0.1:8795', path: '' });
  });

  it('keeps the path prefix a proxy adds, such as the gateway\'s /jeff/', () => {
    expect(serverRoot('https://datigator.example.net/jeff/ui/#playground'))
      .toEqual({ origin: 'https://datigator.example.net/jeff', path: '/jeff' });
  });

  it('refuses a page that is not at an address ending in /ui/', () => {
    expect(() => serverRoot('http://127.0.0.1:8795/ui')).toThrow('ending in /ui/');
    expect(() => serverRoot('http://127.0.0.1:8795/')).toThrow('ending in /ui/');
  });

  it('sends calls under the prefix when the console is served under one', async () => {
    const fetch = vi.fn(async () => jsonResponse(200, { user: 'a@b.c', kind: 'console', admin: false }));
    vi.stubGlobal('fetch', fetch);
    window.history.pushState({}, '', '/jeff/ui/');
    try {
      await createApi({ token: 'tok', onUnauthorised: () => {} }).me();
    } finally {
      window.history.pushState({}, '', '/ui/');
    }
    const [path] = fetch.mock.calls[0] as unknown as [string, RequestInit];
    expect(path).toBe('/jeff/ui/me');
  });
});
