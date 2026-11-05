// @vitest-environment node
// The local server (scripts/local-server.ts) serves a static build and the hub's function in Vercel's order, as
// vercel.json sets it up: redirects, trailing slashes, static files, the function for /api paths, else 404.html.
import fs from 'node:fs';
import type http from 'node:http';
import { request as httpRequest } from 'node:http';
import type { AddressInfo } from 'node:net';
import os from 'node:os';
import path from 'node:path';
import { afterAll, beforeAll, describe, expect, it, vi } from 'vitest';
import api from '../api/index';
import { HUB_VERSION } from '../app/lib/hub-version.generated';
import { createLocalServer, MAX_REQUEST_BYTES, type VercelConfig } from '../scripts/local-server';

const config = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'vercel.json'), 'utf8')) as VercelConfig;
let server: http.Server;
let base: string;
let clientDir: string;

// A server with the real vercel.json (or `rewrites` in its place) whose function answers with the URL and body size
// it was given.
async function echoServer(rewrites: VercelConfig['rewrites'] = config.rewrites): Promise<{ base: string; close: () => Promise<unknown> }> {
  const echo = createLocalServer({
    clientDir,
    config: { ...config, rewrites },
    api: async (request) => Response.json({ url: request.url, bytes: (await request.arrayBuffer()).byteLength }),
  });
  await new Promise<void>((resolve) => echo.listen(0, '127.0.0.1', resolve));
  const url = `http://127.0.0.1:${(echo.address() as AddressInfo).port}`;
  return { base: url, close: () => new Promise((resolve) => echo.close(resolve)) };
}

beforeAll(async () => {
  clientDir = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-client-'));
  fs.writeFileSync(path.join(clientDir, '404.html'), '<h1>Not found</h1>');
  fs.mkdirSync(path.join(clientDir, 'docs/guides/introduction'), { recursive: true });
  fs.writeFileSync(path.join(clientDir, 'docs/guides/introduction/index.html'), '<h1>Introduction</h1>');
  fs.writeFileSync(path.join(clientDir, 'robots.txt'), 'User-agent: *');
  server = createLocalServer({ clientDir, config, api: api.fetch });
  await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  base = `http://127.0.0.1:${(server.address() as AddressInfo).port}`;
});

afterAll(async () => {
  vi.unstubAllEnvs();
  await new Promise((resolve) => server.close(resolve));
});

const get = (url: string, init?: RequestInit) => fetch(`${base}${url}`, { redirect: 'manual', ...init });

// A GET with the request target sent exactly as written (fetch would tidy //x or /./x first).
function rawGet(target: string): Promise<{ status: number; location: string | undefined }> {
  return new Promise((resolve, reject) => {
    const request = httpRequest({ host: '127.0.0.1', port: new URL(base).port, path: target }, (response) => {
      response.resume();
      resolve({ status: response.statusCode as number, location: response.headers.location });
    });
    request.on('error', reject);
    request.end();
  });
}

describe('local server', () => {
  it('serves /api/health from the function', async () => {
    const response = await get('/api/health');
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ status: 'ok', version: HUB_VERSION });
  });

  it('answers an unknown /api path with a JSON 404, not the HTML 404 page', async () => {
    const response = await get('/api/no/such/endpoint');
    expect(response.status).toBe(404);
    expect(response.headers.get('content-type')).toMatch(/^application\/json/);
    expect(await response.json()).toEqual({
      error: { type: 'NotFound', message: 'the hub has no endpoint at /api/no/such/endpoint', details: { path: '/api/no/such/endpoint' } },
    });
  });

  it('passes the method and body through to the function', async () => {
    const response = await get('/api/health', { method: 'POST', body: '{"a":1}', headers: { 'content-type': 'application/json' } });
    expect(response.status).toBe(405);
    expect(response.headers.get('allow')).toBe('GET');
  });

  it('serves static pages and files, and 404.html for a page the build does not have', async () => {
    expect(await (await get('/docs/guides/introduction')).text()).toBe('<h1>Introduction</h1>');
    expect(await (await get('/robots.txt')).text()).toBe('User-agent: *');
    const missing = await get('/no/such/page');
    expect(missing.status).toBe(404);
    expect(await missing.text()).toBe('<h1>Not found</h1>');
  });

  it('the trailing-slash redirect keeps the path percent-encoded, so it never names another site', async () => {
    for (const [sent, location] of [['/%09/evil.example/', '/%09/evil.example'], ['/%2F%2Fevil.example/', '/%2F%2Fevil.example'],
      ['/%5Cevil.example/', '/%5Cevil.example'], ['/a%20b/?x=1', '/a%20b?x=1'],
      // Repeated slashes are merged first, as Vercel does (measured on the preview).
      ['//evil.example/', '/evil.example/'], ['//evil.example', '/evil.example'], ['///evil.example/x/', '/evil.example/x/']]) {
      const response = await rawGet(sent);
      expect(response.status, sent).toBe(308);
      expect(response.location, sent).toBe(location);
    }
  });

  it("gives every answer vercel.json's security headers: pages, files, 404 and the function, but not redirects", async () => {
    for (const url of ['/docs/guides/introduction', '/robots.txt', '/no/such/page', '/api/health', '/api/nope', '/api']) {
      const response = await get(url);
      expect(response.headers.get('x-frame-options'), url).toBe('DENY');
      expect(response.headers.get('x-content-type-options'), url).toBe('nosniff');
      expect(response.headers.get('referrer-policy'), url).toBe('strict-origin-when-cross-origin');
      expect(response.headers.get('content-security-policy'), url).toContain("frame-ancestors 'none'");
    }
    expect((await get('/docs/guides/introduction')).headers.get('content-security-policy')).toContain("script-src 'self'");
    expect((await get('/api/health')).headers.get('content-security-policy')).toMatch(/^default-src 'none'/);
    expect((await get('/apiary')).headers.get('content-security-policy')).toContain("script-src 'self'");
    // As measured on the Vercel preview: a redirect carries none of them.
    for (const url of ['/docs', '/api/health/']) expect((await get(url)).headers.get('x-frame-options'), url).toBeNull();
  });

  it("refuses to guess when the function sets a header vercel.json sets too", async () => {
    const clash = createLocalServer({ clientDir, config, api: async () => new Response('{}', { headers: { 'x-frame-options': 'SAMEORIGIN' } }) });
    await new Promise<void>((resolve) => clash.listen(0, '127.0.0.1', resolve));
    const logged = vi.spyOn(console, 'error').mockImplementation(() => {});
    const response = await fetch(`http://127.0.0.1:${(clash.address() as AddressInfo).port}/api/health`);
    expect(response.status).toBe(500);
    expect(String(logged.mock.calls[0][1])).toContain('the function set x-frame-options on /api/health, which vercel.json also sets');
    logged.mockRestore();
    await new Promise((resolve) => clash.close(resolve));
  });

  it("follows vercel.json's redirects and trailing-slash rule, for /api paths too", async () => {
    const docs = await get('/docs');
    expect(docs.status).toBe(308);
    expect(docs.headers.get('location')).toBe('/docs/guides/introduction');
    const slash = await get('/api/health/');
    expect(slash.status).toBe(308);
    expect(slash.headers.get('location')).toBe('/api/health');
  });
});

describe('what the function receives (as measured on a Vercel preview)', () => {
  it("with vercel.json's rewrite, the request's own path and query, unchanged", async () => {
    const echo = await echoServer();
    for (const url of ['/api/health', '/api/a/b', '/api/health?path=mine&x=1', '/api']) {
      const body = await (await fetch(`${echo.base}${url}`)).json();
      expect(body.url).toBe(`${echo.base}${url}`);
    }
    await echo.close();
  });

  it('with a named parameter (/api/:path*), Vercel adds it to the query, so vercel.json must not use one', async () => {
    const echo = await echoServer([{ source: '/api/:path*', destination: '/api' }]);
    expect((await (await fetch(`${echo.base}/api/a/b`)).json()).url).toBe(`${echo.base}/api/a/b?path=a%2Fb`);
    expect((await (await fetch(`${echo.base}/api/health?x=1`)).json()).url).toBe(`${echo.base}/api/health?x=1&path=health`);
    await echo.close();
  });

  it("X-Forwarded-For is the client's own address, whatever the client sent, as Vercel sets it", async () => {
    const echo = createLocalServer({ clientDir, config,
      api: async (request) => Response.json({ forwarded: request.headers.get('x-forwarded-for') }) });
    await new Promise<void>((resolve) => echo.listen(0, '127.0.0.1', resolve));
    const url = `http://127.0.0.1:${(echo.address() as AddressInfo).port}/api/health`;
    expect((await (await fetch(url)).json()).forwarded).toBe('127.0.0.1');
    expect((await (await fetch(url, { headers: { 'x-forwarded-for': '203.0.113.9' } })).json()).forwarded).toBe('127.0.0.1');
    await new Promise((resolve) => echo.close(resolve));
  });

  // Vercel's documented behaviour, not yet measured on a preview (task-15-report.md, fix round 1).
  it('X-Forwarded-Proto is the scheme the client used (http here), whatever the client sent, as Vercel sets it', async () => {
    const echo = createLocalServer({ clientDir, config,
      api: async (request) => Response.json({ proto: request.headers.get('x-forwarded-proto'), url: request.url }) });
    await new Promise<void>((resolve) => echo.listen(0, '127.0.0.1', resolve));
    const url = `http://127.0.0.1:${(echo.address() as AddressInfo).port}/api/health`;
    expect(await (await fetch(url)).json()).toEqual({ proto: 'http', url });
    expect((await (await fetch(url, { headers: { 'x-forwarded-proto': 'https' } })).json()).proto).toBe('http');
    await new Promise((resolve) => echo.close(resolve));
  });

  it('every Set-Cookie header of the function reaches the client', async () => {
    const echo = createLocalServer({ clientDir, config, api: async () => {
      const headers = new Headers();
      headers.append('set-cookie', 'a=1; Path=/; HttpOnly');
      headers.append('set-cookie', 'b=2; Path=/; Secure');
      return new Response('{}', { headers });
    } });
    await new Promise<void>((resolve) => echo.listen(0, '127.0.0.1', resolve));
    const response = await fetch(`http://127.0.0.1:${(echo.address() as AddressInfo).port}/api/health`);
    expect(response.headers.getSetCookie()).toEqual(['a=1; Path=/; HttpOnly', 'b=2; Path=/; Secure']);
    await new Promise((resolve) => echo.close(resolve));
  });

  it('a request over 4.5 MB is refused with 413 FUNCTION_PAYLOAD_TOO_LARGE before the function runs', async () => {
    const echo = await echoServer();
    const big = await fetch(`${echo.base}/api/health`, { method: 'POST', body: new Uint8Array(MAX_REQUEST_BYTES) });
    expect(big.status).toBe(413);
    expect(big.headers.get('x-vercel-error')).toBe('FUNCTION_PAYLOAD_TOO_LARGE');
    const fits = await fetch(`${echo.base}/api/health`, { method: 'POST', body: new Uint8Array(4_400_000) });
    expect(fits.status).toBe(200);
    expect((await fits.json()).bytes).toBe(4_400_000);
    await echo.close();
  });
});
