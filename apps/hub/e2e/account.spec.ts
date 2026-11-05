// The account page in the browser, against the built site and the hub's function: signed out it sends the visitor to
// /signin; a visitor who never paid sees the subscribe screen and can delete the account; the paying account the e2e
// run seeds (scripts/e2e-seed.mjs: an active subscription and this period's usage) sees its usage, creates a key
// (shown once), revokes it and signs out everywhere. Stripe is never reached. The page is photographed at a phone's
// width and a desktop's, into .e2e/site/screenshots.
import path from 'node:path';
import { STATE_DIR } from '../playwright.config';
import { signInByEmail } from './accounts';
import { expect, test } from './fixtures';

const SUBSCRIBER = 'subscriber@example.com';
const SCREENSHOTS = path.join(STATE_DIR, 'screenshots');

const signedIn = async (page: import('@playwright/test').Page) =>
  ((await (await page.request.get('/api/signin-options')).json()) as { signedIn: boolean }).signedIn;

// A full-page photograph from the top, so the sticky header is where a reader sees it.
async function photograph(page: import('@playwright/test').Page, name: string) {
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(SCREENSHOTS, name), fullPage: true });
}

async function noSideScroll(page: import('@playwright/test').Page) {
  const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(scroll, 'the page scrolls sideways').toBeLessThanOrEqual(client);
}

test('/account sends a visitor who is not signed in to /signin', async ({ page, allowFailedLoad }) => {
  allowFailedLoad('/api/account');
  const answer = page.waitForResponse('**/api/account');
  expect((await page.goto('/account'))?.status()).toBe(200);
  expect((await answer).status()).toBe(401);
  await expect(page).toHaveURL(/\/signin$/);
});

test('a visitor who never paid sees the subscribe screen, and can delete the account', async ({ page }) => {
  await signInByEmail(page, 'nell@example.com');
  await expect(page.getByText('$1 a month, 50 million input tokens, cancel any time.')).toBeVisible();
  await expect(page.getByRole('button', { name: 'Subscribe with Stripe' })).toBeVisible();
  await page.setViewportSize({ width: 375, height: 812 });
  await noSideScroll(page);
  await photograph(page, 'account-subscribe-375.png');
  await page.setViewportSize({ width: 1280, height: 900 });
  await photograph(page, 'account-subscribe-1280.png');
  await page.getByLabel('Type delete to confirm').fill('delete');
  await page.getByRole('button', { name: 'Delete account' }).click();
  await expect(page).toHaveURL(/\/$/);
  expect(await signedIn(page)).toBe(false);
});

test('the paying account sees its usage, creates a key shown once, revokes it and signs out everywhere', async ({ page }) => {
  await signInByEmail(page, SUBSCRIBER);
  // The header shows the signed-in account's avatar (the seeded account's name is Subscriber, scripts/e2e-seed.mjs),
  // not "Sign in", and it still fits.
  for (const width of [375, 1280]) {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/pricing');
    const avatar = page.getByRole('banner').getByRole('link', { name: 'Your account (Subscriber)' });
    await expect(avatar).toBeVisible();
    await expect(avatar).toHaveText('S');
    await expect(page.getByRole('banner').getByRole('link', { name: 'Sign in', includeHidden: true })).toHaveCount(0);
    await noSideScroll(page);
    await photograph(page, `header-signed-in-${width}.png`);
  }
  await page.getByRole('banner').getByRole('link', { name: /^Your account/ }).click();
  await expect(page).toHaveURL(/\/account$/);
  await expect(page.getByText(/^12,345,678 of 50,000,000 input tokens used\. Resets on \d{1,2} [A-Z][a-z]+ \d{4}\.$/)).toBeVisible();
  await expect(page.getByRole('meter')).toHaveAttribute('aria-valuenow', '12345678');
  await expect(page.getByText(/^Active\. Next billing date: /)).toBeVisible();

  await page.getByLabel('Key name').fill('laptop');
  const made = page.waitForResponse('**/api/account/create-key');
  await page.getByRole('button', { name: 'Create key' }).click();
  const { created } = (await (await made).json()) as { created: { key: string; name: string } };
  expect(created.key).toMatch(/^jeff_live_[a-z2-7]{52}$/);
  await expect(page.getByText(created.key, { exact: true })).toBeVisible();
  await expect(page.getByText(`${created.key.slice(0, 16)}…`)).toBeVisible();
  await expect(page.getByRole('heading', { name: 'Your new key "laptop"' })).toBeFocused();
  expect(page.url()).not.toContain(created.key);

  await page.getByLabel('Key name').fill('ci server');
  await page.getByRole('button', { name: 'Create key' }).click();
  const heading = page.getByRole('heading', { name: 'Your new key "ci server"' });
  await expect(heading).toBeFocused();
  await expect(heading).toHaveAccessibleDescription('This is the only time it is shown: copy it now.');

  await page.setViewportSize({ width: 375, height: 812 });
  await noSideScroll(page);
  // The keys table fits its frame: nothing, Revoke included, is scrolled out of sight.
  const frame = await page.getByRole('table').evaluate((table) => ({ scroll: table.parentElement!.scrollWidth, client: table.parentElement!.clientWidth }));
  expect(frame.scroll, 'the keys table scrolls sideways').toBeLessThanOrEqual(frame.client);
  await photograph(page, 'account-375.png');
  await page.setViewportSize({ width: 1280, height: 900 });
  await noSideScroll(page);
  await photograph(page, 'account-1280.png');

  // Shown once: after leaving the page and coming back (the back-forward cache, where the browser keeps one), or a
  // reload, only the visible prefix is left.
  await page.goto('/');
  await page.goBack();
  await expect(page).toHaveURL(/\/account$/);
  await expect(page.getByText('laptop', { exact: true })).toBeVisible();
  await expect(page.getByText(created.key, { exact: true })).toHaveCount(0);
  await page.reload();
  await expect(page.getByText('laptop', { exact: true })).toBeVisible();
  await expect(page.getByText(created.key, { exact: true })).toHaveCount(0);

  // Revoking asks first, in the site's own dialog (app/components/ConfirmDialog.tsx); Cancel keeps the key.
  await page.getByRole('button', { name: 'Revoke laptop' }).click();
  const asked = page.getByRole('dialog', { name: 'Revoke key' });
  await expect(asked).toHaveAccessibleDescription('Revoke the key "laptop"? Programs that use it stop working within about a minute, and it cannot be brought back.');
  await asked.getByRole('button', { name: 'Cancel' }).click();
  await expect(asked).toHaveCount(0);
  await expect(page.getByText('laptop', { exact: true })).toBeVisible();
  await page.getByRole('button', { name: 'Revoke laptop' }).click();
  await asked.getByRole('button', { name: 'Revoke key' }).click();
  await expect(page.getByRole('status')).toHaveText('The key is revoked. It stops working within about a minute.');
  await expect(page.getByText('laptop', { exact: true })).toHaveCount(0);

  await page.getByRole('button', { name: 'Sign out everywhere' }).click();
  await expect(page).toHaveURL(/\/signin$/);
  expect(await signedIn(page)).toBe(false);
  expect((await page.context().cookies()).filter((cookie) => cookie.name.includes('better-auth'))).toEqual([]);
});

test('the key endpoints refuse a POST from another site', async ({ page }) => {
  await signInByEmail(page, SUBSCRIBER);
  for (const [endpoint, data] of [['/api/account/create-key', { name: 'evil' }], ['/api/account/revoke-key', { key: 'k' }],
    ['/api/account/sign-out-everywhere', {}]] as const) {
    const foreign = await page.request.post(endpoint, { headers: { origin: 'https://evil.example' }, data });
    expect(foreign.status(), endpoint).toBe(403);
  }
  expect(await signedIn(page)).toBe(true);
});
