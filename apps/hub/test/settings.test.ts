import { afterEach, describe, expect, it, vi } from 'vitest';
import { apiOriginSetting, settings } from '../app/lib/settings.server';
import { TEST_ENV } from './helpers/settings';

afterEach(() => vi.unstubAllEnvs());

// Every run-time setting with values a production build accepts (made-up ones), on top of the test values.
function stubProductionSettings(): void {
  for (const [name, value] of Object.entries(TEST_ENV)) vi.stubEnv(name, value);
  const production: Record<string, string> = { DATABASE_URL: 'postgres://user:pass@db.example.com/hub', MAIL_TRANSPORT: 'sendgrid',
    SENDGRID_API_KEY: 'SG.test', TURNSTILE_VERIFY_URL: 'https://challenges.cloudflare.com/turnstile/v0/siteverify',
    PLAYGROUND_API_URL: 'https://api.example.com', JEFF_HUB_API_ORIGIN: 'https://api.example.com' };
  for (const [name, value] of Object.entries(production)) vi.stubEnv(name, value);
}

describe('settings', () => {
  it('reads the required setting', () => {
    vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com');
    expect(settings()).toEqual({ apiOrigin: 'https://api.example.com' });
  });
  it('names a missing setting', () => {
    vi.stubEnv('JEFF_HUB_API_ORIGIN', '');
    expect(() => settings()).toThrow('JEFF_HUB_API_ORIGIN');
  });
  it('refuses an origin with a trailing slash', () => {
    vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com/');
    expect(() => settings()).toThrow('trailing slash');
  });
});

describe('the data set store token', () => {
  it('is required when the site is built for production, naming it', () => {
    stubProductionSettings();
    vi.stubEnv('VERCEL_ENV', 'production');
    vi.stubEnv('CRON_SECRET', 'a-cron-secret-of-enough-length');
    vi.stubEnv('BLOB_READ_WRITE_TOKEN', '');
    expect(() => settings()).toThrow('Set BLOB_READ_WRITE_TOKEN');
    vi.stubEnv('BLOB_READ_WRITE_TOKEN', 'vercel_blob_rw_store_secret');
    expect(settings()).toEqual({ apiOrigin: 'https://api.example.com' });
  });
  it('is not read by a build that is not for production', () => {
    vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com');
    vi.stubEnv('VERCEL_ENV', 'preview');
    vi.stubEnv('BLOB_READ_WRITE_TOKEN', '');
    expect(settings()).toEqual({ apiOrigin: 'https://api.example.com' });
  });
});

describe("the cron job's secret", () => {
  it('is required when the site is built for production, naming it, and must be at least 16 characters', () => {
    stubProductionSettings();
    vi.stubEnv('VERCEL_ENV', 'production');
    vi.stubEnv('BLOB_READ_WRITE_TOKEN', 'vercel_blob_rw_store_secret');
    vi.stubEnv('CRON_SECRET', '');
    expect(() => settings()).toThrow('Set CRON_SECRET');
    vi.stubEnv('CRON_SECRET', 'too-short');
    expect(() => settings()).toThrow('CRON_SECRET must be at least 16 characters');
    vi.stubEnv('CRON_SECRET', 'a-cron-secret-of-enough-length');
    expect(settings()).toEqual({ apiOrigin: 'https://api.example.com' });
  });
  it('is not read by a build that is not for production', () => {
    vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com');
    vi.stubEnv('VERCEL_ENV', 'preview');
    vi.stubEnv('CRON_SECRET', '');
    expect(settings()).toEqual({ apiOrigin: 'https://api.example.com' });
  });
});

describe('a production build', () => {
  it('reads every run-time setting the function reads, naming a missing or wrong one (final review M7)', () => {
    stubProductionSettings();
    vi.stubEnv('VERCEL_ENV', 'production');
    expect(settings()).toEqual({ apiOrigin: 'https://api.example.com' });
    vi.stubEnv('STRIPE_WEBHOOK_SECRET', '');
    expect(() => settings()).toThrow('STRIPE_WEBHOOK_SECRET');
    stubProductionSettings();
    vi.stubEnv('MAIL_TRANSPORT', 'test-mailbox');
    expect(() => settings()).toThrow('MAIL_TRANSPORT=test-mailbox is refused in production');
    stubProductionSettings();
    vi.stubEnv('ADAPTER_REQUEST_NOTIFY_EMAIL', '');
    expect(() => settings()).toThrow('ADAPTER_REQUEST_NOTIFY_EMAIL');
  });
});

describe('the run-time API origin (re-review R7)', () => {
  it('reads only JEFF_HUB_API_ORIGIN, not the build\'s production checks', () => {
    stubProductionSettings();
    vi.stubEnv('VERCEL_ENV', 'production');
    vi.stubEnv('STRIPE_WEBHOOK_SECRET', '');
    vi.stubEnv('CRON_SECRET', '');
    expect(apiOriginSetting()).toBe('https://api.example.com');
    vi.stubEnv('JEFF_HUB_API_ORIGIN', '');
    expect(() => apiOriginSetting()).toThrow('Set JEFF_HUB_API_ORIGIN');
  });
});
