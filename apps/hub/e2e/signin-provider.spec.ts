// Sign-in with the test OpenID provider in the browser (oauth2-mock-server on AUTH_TEST_PROVIDER, e2e/global-setup.ts),
// against the built site and the hub's function: the provider stands in for GitHub and Google, whose linking rule is
// the same. A provider that says the email is verified signs in to the account that email already has; one that does
// not is refused, and the account stays as it was. Sign out everywhere, from one browser, ends the other's session.
//
// Better Auth's rate limits count every check's requests together, all from 127.0.0.1 (e2e/accounts.ts): at most 3
// social sign-ins before 10 quiet seconds, and 5 email links before 60. So these checks make 3 social sign-ins and
// one email link, and the two linking checks start from the paying account scripts/e2e-seed.mjs writes (email
// verified, as every account's is) instead of signing it in by email first. signin.spec.ts, which runs next, asks for
// 3 email links.
import { accountId, signInByEmail, signInWithProvider } from './accounts';
import { expect, test } from './fixtures';

// The seeded paying account (scripts/e2e-seed.mjs).
const SEEDED = { id: 'e2e-subscriber', email: 'subscriber@example.com' };

const signedIn = async (page: import('@playwright/test').Page) =>
  ((await (await page.request.get('/api/signin-options')).json()) as { signedIn: boolean }).signedIn;

test('the test provider signs in, and the same verified email is the same account', async ({ page }) => {
  await signInWithProvider(page, { sub: 'oidc-linked', email: SEEDED.email, email_verified: true, name: 'Linked' });
  await expect(page).toHaveURL(/\/account$/);
  expect(await accountId(page)).toBe(SEEDED.id);
});

test('an unverified email from a provider is not joined to the account with that email', async ({ page }) => {
  await signInWithProvider(page, { sub: 'oidc-careful', email: SEEDED.email, email_verified: false, name: 'Careful' });
  await expect(page).toHaveURL(/\/signin\?error=account_not_linked$/);
  await expect(page.getByRole('alert')).toHaveText('That email already has a Jeff account, and the provider did not confirm that the '
    + 'email is yours. Sign in with a link sent to your email instead.');
  expect(await signedIn(page)).toBe(false);
  expect((await page.request.get('/api/account/status')).status()).toBe(401);
});

test('sign out everywhere ends the other browser too', async ({ browser }) => {
  // One browser signs in with the provider (a new account), the other with an email link to the same address: both
  // are the one account.
  const email = 'everywhere@example.com';
  const first = await browser.newContext();
  const second = await browser.newContext();
  const a = await first.newPage();
  const b = await second.newPage();
  await signInWithProvider(a, { sub: 'oidc-everywhere', email, email_verified: true, name: 'Everywhere' });
  await expect(a).toHaveURL(/\/account$/);
  await signInByEmail(b, email);
  expect(await accountId(b)).toBe(await accountId(a));

  await a.getByRole('button', { name: 'Sign out everywhere' }).click();
  await expect(a).toHaveURL(/\/signin$/);
  expect(await signedIn(b)).toBe(false);
  await b.goto('/account');
  await expect(b).toHaveURL(/\/signin$/);
  await first.close();
  await second.close();
});
