// Sign-in in the browser, against the built site and the hub's function as Vercel serves them (scripts/local-server.ts),
// over http://127.0.0.1: the pages, the email link end to end (the session cookie as the browser keeps it, one use,
// landing on the subscribe screen), and the checks that refuse a request without the bot check, from another site,
// or for a method SIGN_IN_PROVIDERS leaves out. The test OpenID provider, linking and sign-out everywhere across two
// browsers are in signin-provider.spec.ts.
import { linkIn, newestMailTo, stubTurnstile } from './accounts';
import { expect, test } from './fixtures';

for (const [url, heading, pricing] of [['/signin', 'Sign in', false], ['/signup', 'Get an API key', true]] as const) {
  test(`${url} offers GitHub and the email link (SIGN_IN_PROVIDERS=github,email), not Google, and this run's test provider`, async ({ page }) => {
    await stubTurnstile(page);
    const options = page.waitForResponse('**/api/signin-options');
    expect((await page.goto(url))?.status()).toBe(200);
    await expect(page.getByRole('heading', { level: 1 })).toHaveText(heading);
    expect(await (await options).json()).toEqual({ signedIn: false, providers: ['github', 'email'], testProvider: true });
    await expect(page.getByRole('button', { name: 'Continue with GitHub' })).toBeVisible();
    await expect(page.getByRole('button', { name: 'Continue with Google' })).toHaveCount(0);
    await expect(page.getByRole('button', { name: 'Email me a link' })).toBeVisible();
    // On only because this run sets AUTH_TEST_PROVIDER, which production refuses.
    await expect(page.getByRole('button', { name: 'Continue with the test provider' })).toBeVisible();
    await expect(page.getByText('$1 a month, 50 million input tokens, cancel any time.')).toHaveCount(pricing ? 1 : 0);
  });
}

test('sign-in with Google, which SIGN_IN_PROVIDERS leaves out, is refused by name', async ({ page }) => {
  await stubTurnstile(page);
  await page.goto('/signin');
  const origin = new URL(page.url()).origin;
  const social = await page.request.post('/api/auth/sign-in/social', { headers: { origin },
    data: { provider: 'google', callbackURL: '/account', errorCallbackURL: '/signin' } });
  expect(social.status()).toBe(404);
  expect(((await social.json()) as { error: { type: string; message: string } }).error)
    .toMatchObject({ type: 'SignInMethodNotOffered', message: 'the hub does not offer sign-in with google: SIGN_IN_PROVIDERS is github,email' });
  const callback = await page.request.get('/api/auth/callback/google?code=c&state=s', { maxRedirects: 0 });
  expect(callback.status()).toBe(404);
  expect(((await callback.json()) as { error: { type: string; message: string } }).error)
    .toMatchObject({ type: 'SignInMethodNotOffered', message: 'the hub does not offer sign-in with google: SIGN_IN_PROVIDERS is github,email' });
});

test('an email link signs in once, with an HttpOnly, Secure, SameSite=Lax session cookie, and lands on the subscribe screen', async ({ page, context }) => {
  const email = 'ada@example.com';
  await stubTurnstile(page);
  await page.goto('/signin');
  await page.getByLabel('Or sign in with a link sent to your email').fill(email);
  await page.getByRole('button', { name: 'Email me a link' }).click();
  await expect(page.getByRole('status')).toHaveText(`Check your email: we sent a link to ${email}. It works once, within 15 minutes.`);

  // The link lands on /account, where an account that never paid sees the one subscribe screen.
  const link = linkIn(newestMailTo(email));
  await page.goto(link);
  await expect(page).toHaveURL(/\/account$/);
  await expect(page.getByText('$1 a month, 50 million input tokens, cancel any time.')).toBeVisible();
  const session = (await context.cookies()).find((cookie) => cookie.name === '__Secure-better-auth.session_token');
  expect(session).toMatchObject({ httpOnly: true, secure: true, sameSite: 'Lax', path: '/' });
  expect(session?.expires).toBeGreaterThan(Date.now() / 1000 + 29 * 24 * 60 * 60);

  // Signed in: the sign-in page sends the visitor on to the account.
  await page.goto('/signin');
  await expect(page).toHaveURL(/\/account$/);

  // Signed out (the account page's button), the same link again does not sign in: it comes back to /signin and says
  // why.
  await page.getByRole('button', { name: 'Sign out', exact: true }).click();
  await expect(page).toHaveURL(/\/$/);
  expect((await context.cookies()).filter((cookie) => cookie.name === '__Secure-better-auth.session_token')).toEqual([]);
  await page.goto(link);
  await expect(page).toHaveURL(/\/signin\?error=INVALID_TOKEN$/);
  await expect(page.getByRole('alert')).toHaveText('That sign-in link has expired or was already used. Ask for a new one below.');
});

test('sign out everywhere, from the hub itself, ends the session; from another site it is refused', async ({ page }) => {
  const email = 'grace@example.com';
  await stubTurnstile(page);
  await page.goto('/signin');
  await page.getByLabel('Or sign in with a link sent to your email').fill(email);
  await page.getByRole('button', { name: 'Email me a link' }).click();
  await expect(page.getByRole('status')).toContainText('Check your email');
  await page.goto(linkIn(newestMailTo(email)));
  await expect(page).toHaveURL(/\/account$/);
  const signedIn = async () => ((await (await page.request.get('/api/signin-options')).json()) as { signedIn: boolean }).signedIn;
  expect(await signedIn()).toBe(true);

  const foreign = await page.request.post('/api/auth/revoke-sessions', { headers: { origin: 'https://evil.example' }, data: {} });
  expect(foreign.status()).toBe(403);
  expect(await signedIn()).toBe(true);

  const own = await page.request.post('/api/auth/revoke-sessions', { headers: { origin: new URL(page.url()).origin }, data: {} });
  expect(own.status()).toBe(200);
  expect(await signedIn()).toBe(false);
});

test('the email form sends nothing when the bot check fails', async ({ page, allowFailedLoad }) => {
  allowFailedLoad('/api/auth/sign-in/magic-link');
  await stubTurnstile(page, 'e2e-fail');
  await page.goto('/signin');
  await page.getByLabel('Or sign in with a link sent to your email').fill('bot@example.com');
  await page.getByRole('button', { name: 'Email me a link' }).click();
  await expect(page.getByRole('alert')).toContainText('The bot check failed (invalid-input-response)');
  expect(() => newestMailTo('bot@example.com')).toThrow(/no email to bot@example.com/);
});
