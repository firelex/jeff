// @vitest-environment node
import { expect, it, vi } from 'vitest';
import { clientIp } from '../app/lib/client-ip.server';
import { ApiError } from '../server/http';
import { TurnstileRefused, turnstileVerifier } from '../app/lib/turnstile.server';

const settings = { secretKey: 'turnstile-test', verifyUrl: 'https://turnstile.test/siteverify', hostname: 'jeffhub.ai' };
const PASS = { success: true, hostname: 'jeffhub.ai' };

it('passes a token Cloudflare accepts, sending the secret, the token and the address', async () => {
  const fetchImpl = vi.fn(async () => Response.json(PASS));
  await turnstileVerifier(settings, fetchImpl as unknown as typeof fetch)('tok', '203.0.113.7');
  const [url, init] = fetchImpl.mock.calls[0] as unknown as [string, RequestInit];
  expect(url).toBe('https://turnstile.test/siteverify');
  expect(Object.fromEntries(new URLSearchParams(init.body as string))).toEqual(
    { secret: 'turnstile-test', response: 'tok', remoteip: '203.0.113.7' });
});

it.each([
  [null, async () => Response.json(PASS), /No bot check/],
  ['', async () => Response.json(PASS), /No bot check/],
  ['tok', async () => Response.json({ success: false, 'error-codes': ['timeout-or-duplicate'] }), /timeout-or-duplicate/],
  ['tok', async () => new Response('down', { status: 502 }), /answered 502/],
  ['tok', async () => Response.json({ success: true, hostname: 'evil.example' }), /solved on "evil.example", not on jeffhub.ai/],
  ['tok', async () => Response.json({ success: true }), /solved on undefined, not on jeffhub.ai/],
])('refuses token %s', async (token, answer, message) => {
  const verify = turnstileVerifier(settings, vi.fn(answer) as unknown as typeof fetch);
  await expect(verify(token, '203.0.113.7')).rejects.toThrow(TurnstileRefused);
  await expect(verify(token, '203.0.113.7')).rejects.toThrow(message);
});

it('a missing token is refused without asking Cloudflare', async () => {
  const fetchImpl = vi.fn(async () => Response.json(PASS));
  await expect(turnstileVerifier(settings, fetchImpl as unknown as typeof fetch)(null, '203.0.113.7')).rejects.toThrow(TurnstileRefused);
  expect(fetchImpl).not.toHaveBeenCalled();
});

it('the client address is exactly one IP address in X-Forwarded-For; anything else is a named 400', () => {
  expect(clientIp(new Headers({ 'x-forwarded-for': '203.0.113.7' }))).toBe('203.0.113.7');
  expect(clientIp(new Headers({ 'x-forwarded-for': '2001:db8::1' }))).toBe('2001:db8::1');
  for (const value of [undefined, '', ' ', '203.0.113.7, 10.0.0.1', '203.0.113.7,10.0.0.1', 'unknown',
    '203.0.113.300', 'localhost', '::ffff:zz']) {
    const headers = new Headers(value === undefined ? {} : { 'x-forwarded-for': value });
    let refused: unknown = null;
    try { clientIp(headers); } catch (error) { refused = error; }
    expect(refused, String(value)).toBeInstanceOf(ApiError);
    expect((refused as ApiError).status).toBe(400);
    expect((refused as ApiError).type).toBe('ClientAddressRefused');
    expect((refused as ApiError).message).toMatch(/^X-Forwarded-For must be exactly one IP address/);
  }
});
