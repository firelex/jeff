// Reads required settings from the environment: every name must be set and not empty. One error lists every name
// that is missing, so a deployment is fixed in one go. requireEnv is the hub's readSettings (server/settings.ts), the
// function the endpoints' settings check uses too, so a missing setting is the same MissingSettingsError wherever it
// is read, and a value of only spaces counts as missing.
//
// The relative imports in this file and the other modules the hub's function loads end in .js: Vercel compiles the
// function as ES modules, and Vite, vitest and tsx resolve .js to the .ts file.
import { readSettings } from '../../server/settings.js';

export function requireEnv<const Name extends string>(names: readonly Name[]): Record<Name, string> {
  return readSettings(names);
}

export function positiveNumber(name: string, text: string): number {
  const value = Number(text);
  if (!Number.isFinite(value) || value <= 0) throw new Error(`${name}=${JSON.stringify(text)} must be a number above 0`);
  return value;
}

export function positiveInteger(name: string, text: string): number {
  const value = positiveNumber(name, text);
  if (!Number.isInteger(value)) throw new Error(`${name}=${JSON.stringify(text)} must be a whole number above 0`);
  return value;
}

export function oneOf<T extends string>(name: string, text: string, allowed: readonly T[]): T {
  if (!(allowed as readonly string[]).includes(text)) throw new Error(`${name}=${JSON.stringify(text)} must be one of ${allowed.join(', ')}`);
  return text as T;
}

// The hosts a test run serves on (Playwright's hub, a fake API): the only ones allowed http://, and only outside
// production. URL writes ::1 as [::1].
const LOOPBACK_HOSTS = ['127.0.0.1', '[::1]', 'localhost'];

// An address the hub builds links and calls from (BETTER_AUTH_URL, PLAYGROUND_API_URL): https:// without a trailing
// slash. http:// is a test rule: allowed only for a loopback host (127.0.0.1, ::1, localhost) and only when not in
// production.
export function addressSetting(name: string, text: string, production: boolean): string {
  const shape = `must be an https:// address without a trailing slash (http:// only for 127.0.0.1, ::1 or localhost, outside production)`;
  if (text.endsWith('/') || !URL.canParse(text)) throw new Error(`${name}=${JSON.stringify(text)} ${shape}`);
  const url = new URL(text);
  if (url.protocol === 'https:') return text;
  if (url.protocol !== 'http:' || !LOOPBACK_HOSTS.includes(url.hostname)) throw new Error(`${name}=${JSON.stringify(text)} ${shape}`);
  if (production) throw new Error(`${name}=${JSON.stringify(text)}: http:// is refused in production; use an https:// address`);
  return text;
}

// Off Vercel VERCEL_ENV is unset, and the test-only values (the test mailbox, a test OpenID provider, PGlite) are
// allowed; on Vercel's production deployment they are refused.
export function inProduction(): boolean {
  return process.env.VERCEL_ENV === 'production';
}
