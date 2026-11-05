// The build's one setting (spec Section 6): the public API's address, which replaces `{{origin}}` in the pages. It is
// required: the build stops naming it when it is missing. A production build (VERCEL_ENV=production) also needs
// BLOB_READ_WRITE_TOKEN, the private store the adapter requests' data sets are uploaded to (data-set-store.server.ts):
// the build only checks that it is set, and never puts it in a page. It also needs CRON_SECRET, which Vercel's cron
// sends to the daily job that deletes adapter requests' examples 90 days after they were closed
// (server/endpoints/cron.ts): checked the same way, at least 16 characters. And it reads every setting the function
// reads at run time, with the function's own checks (accountsSettings, the adapter requests' mail and notification
// address), so a production deployment with a missing or wrong one is refused at build, naming it, instead of at its
// first request (final review M7).
import { accountsSettings, requestMailSettings, requestNotifyEmail } from './accounts-settings.server.js';

export interface Settings { apiOrigin: string }

function required(name: string, why = 'the hub cannot be built without it'): string {
  const value = process.env[name];
  if (value === undefined || value.trim() === '') throw new Error(`Set ${name}: ${why}`);
  return value;
}

// JEFF_HUB_API_ORIGIN alone, for the function at run time (the playground's endpoint): the build's own checks below
// are the build's, and run once there, not on every call (re-review R7).
export function apiOriginSetting(): string {
  const apiOrigin = required('JEFF_HUB_API_ORIGIN');
  if (apiOrigin.endsWith('/')) throw new Error(`JEFF_HUB_API_ORIGIN must not end with a trailing slash: ${apiOrigin}`);
  return apiOrigin;
}

// The build's settings (the pages, prerendered): read by the build only.
export function settings(): Settings {
  const apiOrigin = apiOriginSetting();
  if (process.env.VERCEL_ENV === 'production') {
    required('BLOB_READ_WRITE_TOKEN', 'a production build needs the data set store\'s token (connect the private Blob store to the project)');
    const cronSecret = required('CRON_SECRET', 'a production build needs the secret Vercel\'s cron sends to the daily job that deletes adapter requests\' examples (make one with: openssl rand -hex 32)');
    if (cronSecret.length < 16) throw new Error('CRON_SECRET must be at least 16 characters (make one with: openssl rand -hex 32)');
    accountsSettings();
    requestMailSettings();
    requestNotifyEmail();
  }
  return { apiOrigin };
}
