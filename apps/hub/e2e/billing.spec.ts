// Paying, in the browser, against the built site and the hub's function: the page Stripe Checkout comes back to, and
// the billing endpoints' sign-in and cross-site checks. Stripe itself is never reached here (no Checkout is started);
// Task 19's accounts run pays through a Stripe stand-in.
import { signInByEmail } from './accounts';
import { expect, test } from './fixtures';

test('/account/return sends a visitor who is not signed in to /signin', async ({ page, allowFailedLoad }) => {
  allowFailedLoad('/api/account/status');
  const status = page.waitForResponse('**/api/account/status');
  expect((await page.goto('/account/return?session_id=cs_test_1'))?.status()).toBe(200);
  expect((await status).status()).toBe(401);
  await expect(page).toHaveURL(/\/signin$/);
});

test('/account/return waits for Stripe while the hub has no subscription', async ({ page }) => {
  await signInByEmail(page, 'lin@example.com');
  const status = page.waitForResponse('**/api/account/status');
  await page.goto('/account/return?session_id=cs_test_1');
  const body = (await (await status).json()) as { account: string; usable: boolean; status: string | null };
  expect(body).toMatchObject({ usable: false, status: null });
  expect(body.account).not.toBe('');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Thank you');
  await expect(page.getByRole('status')).toHaveText('Waiting for Stripe to confirm your payment…');
  await expect(page).toHaveURL(/\/account\/return\?session_id=cs_test_1$/);
});

test('the billing endpoints refuse a POST from another site, signed in or not', async ({ page }) => {
  await signInByEmail(page, 'mo@example.com');
  for (const path of ['/api/account/checkout', '/api/account/portal', '/api/account/delete']) {
    const foreign = await page.request.post(path, { headers: { origin: 'https://evil.example' }, data: { confirm: 'delete' } });
    expect(foreign.status(), path).toBe(403);
    expect(((await foreign.json()) as { error: { type: string } }).error.type).toBe('CrossSiteRefused');
  }
  // The account is still there.
  expect((await page.request.get('/api/account/status')).status()).toBe(200);
  // The portal from the hub itself, before any payment: refused with words for the customer, Stripe not asked.
  const portal = await page.request.post('/api/account/portal', { headers: { origin: new URL(page.url()).origin } });
  expect(portal.status()).toBe(409);
  expect(((await portal.json()) as { error: { message: string } }).error.message).toBe('There is no billing account yet: subscribe first');
});
