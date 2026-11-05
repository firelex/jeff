// What the browser checks of sign-in and the account pages share: Turnstile answered locally, the test mailbox,
// signing in with an email link, the test OpenID provider's next identity, and the signed-in account's id.
import fs from 'node:fs';
import path from 'node:path';
import type { Page, Response } from '@playwright/test';
import { OIDC_IDENTITY_FILE, STATE_DIR } from '../playwright.config';

// Turnstile's widget script, answered locally: it renders nothing and hands the page the token `token`.
export async function stubTurnstile(page: Page, token = 'e2e-pass'): Promise<void> {
  await page.route('https://challenges.cloudflare.com/**', (route) => route.fulfill({ contentType: 'text/javascript',
    body: `window.turnstile = { render: (el, o) => { setTimeout(() => o.callback(${JSON.stringify(token)}), 0); return 'w'; }, remove: () => {} };` }));
}

export function newestMailTo(email: string): { text: string } {
  const dir = path.join(STATE_DIR, 'mail');
  const mails = fs.existsSync(dir)
    ? fs.readdirSync(dir).sort().map((file) => JSON.parse(fs.readFileSync(path.join(dir, file), 'utf8')) as { to: string; text: string })
    : [];
  const mine = mails.filter((mail) => mail.to === email);
  if (mine.length === 0) throw new Error(`no email to ${email} in ${dir}`);
  return mine[mine.length - 1];
}

export function linkIn(mail: { text: string }): string {
  const match = /http:\/\/127\.0\.0\.1:\d+\/api\/auth\/magic-link\/verify\?\S+/.exec(mail.text);
  if (match === null) throw new Error(`no sign-in link in: ${mail.text}`);
  return match[0];
}

// Better Auth counts sign-in requests per visitor address in the database, and every check comes from 127.0.0.1:
// at most 5 email-link requests (and 5 link uses) before 60 seconds pass without one, and at most 3 social sign-ins
// before 10 seconds pass without one (auth.server.ts; Better Auth's own rules). The checks are written to stay within
// them (signin-provider.spec.ts says how); a refusal fails the check with this explanation instead of a timeout.
async function expectNotRateLimited(response: Response, what: string): Promise<void> {
  if (response.status() === 429) {
    throw new Error(`Better Auth's rate limit refused ${what} (429, retry after ${response.headers()['x-retry-after']} s): the browser `
      + 'checks ask for more sign-ins from 127.0.0.1 in a row than it allows (e2e/accounts.ts)');
  }
  if (!response.ok()) throw new Error(`${what} answered ${response.status()}: ${await response.text()}`);
}

// Signs `email` in with an email link. The link lands on /account.
export async function signInByEmail(page: Page, email: string): Promise<void> {
  await stubTurnstile(page);
  await page.goto('/signin');
  await page.getByLabel('Or sign in with a link sent to your email').fill(email);
  const sent = page.waitForResponse('**/api/auth/sign-in/magic-link');
  await page.getByRole('button', { name: 'Email me a link' }).click();
  await expectNotRateLimited(await sent, `the email link for ${email}`);
  await page.getByText(`Check your email: we sent a link to ${email}.`, { exact: false }).waitFor();
  await page.goto(linkIn(newestMailTo(email)));
  await page.waitForURL('**/account');
}

// Whom the test OpenID provider signs in next: its id_token and user info carry these claims.
export function providerIdentity(identity: { sub: string; email: string; email_verified: boolean; name: string }): void {
  fs.writeFileSync(OIDC_IDENTITY_FILE, JSON.stringify(identity));
}

// Signs in with the test OpenID provider as `identity`, from the sign-in page's button. The sign-in lands on /account.
export async function signInWithProvider(page: Page, identity: Parameters<typeof providerIdentity>[0]): Promise<void> {
  providerIdentity(identity);
  await stubTurnstile(page);
  await page.goto('/signin');
  const started = page.waitForResponse('**/api/auth/sign-in/social');
  await page.getByRole('button', { name: 'Continue with the test provider' }).click();
  await expectNotRateLimited(await started, `the test provider's sign-in for ${identity.email}`);
}

// The signed-in account's id (GET /api/account/status).
export async function accountId(page: Page): Promise<string> {
  const response = await page.request.get('/api/account/status');
  if (!response.ok()) throw new Error(`/api/account/status answered ${response.status()}: ${await response.text()}`);
  return ((await response.json()) as { account: string }).account;
}
