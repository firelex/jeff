// Cloudflare Turnstile's server-side check (https://developers.cloudflare.com/turnstile/get-started/server-side-validation/).
import { TURNSTILE_HEADER_NAME } from './turnstile-header.js';

export const TURNSTILE_HEADER = TURNSTILE_HEADER_NAME;

export class TurnstileRefused extends Error {
  constructor(reason: string[] | string) {
    super(`The bot check failed (${Array.isArray(reason) ? reason.join(', ') : reason}); reload the page and try again`);
    this.name = 'TurnstileRefused';
  }
}

export type VerifyTurnstile = (token: string | null, ip: string) => Promise<void>;

// `hostname` is the hub's own host (BETTER_AUTH_URL's): Cloudflare reports the host the widget was solved on, and a
// token solved on another site is refused (review M5).
export function turnstileVerifier(settings: { secretKey: string; verifyUrl: string; hostname: string }, fetchImpl: typeof fetch): VerifyTurnstile {
  return async (token, ip) => {
    if (token === null || token === '') throw new TurnstileRefused('No bot check was sent with the form');
    const response = await fetchImpl(settings.verifyUrl, {
      method: 'POST',
      headers: { 'content-type': 'application/x-www-form-urlencoded' },
      body: new URLSearchParams({ secret: settings.secretKey, response: token, remoteip: ip }).toString(),
    });
    if (!response.ok) throw new TurnstileRefused(`Cloudflare's check answered ${response.status}`);
    const result = (await response.json()) as { success: boolean; hostname?: string; 'error-codes'?: string[] };
    // `?? [...]` names the absence of error codes in the message; it is not a substitute value.
    if (!result.success) throw new TurnstileRefused(result['error-codes'] ?? ['no error code given']);
    if (result.hostname !== settings.hostname) {
      throw new TurnstileRefused(`it was solved on ${JSON.stringify(result.hostname)}, not on ${settings.hostname}`);
    }
  };
}
