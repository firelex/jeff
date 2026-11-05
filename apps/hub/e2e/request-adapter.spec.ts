// Requesting an adapter in the browser, against the built site and the hub's function: a visitor who is not signed in
// is sent to sign in and comes back to the form; the form takes 30 pasted CSV examples and counts them per option;
// the request is sent, the notification address is emailed (the test mailbox), the request shows on the account page, and the owner
// sees it on /admin/requests, which anyone else sees as Not found. The form, the account page's section and the
// admin list are photographed into .e2e/site/screenshots.
//
// Better Auth's rate limits count every check's sign-ins together (e2e/accounts.ts): this check asks for one email
// link and one social sign-in (the owner, with the test provider), then waits out the social window (10 quiet
// seconds), so signin-provider.spec.ts can make its 3.
import path from 'node:path';
import type { Page } from '@playwright/test';
import { STATE_DIR } from '../playwright.config';
import { linkIn, newestMailTo, signInWithProvider, stubTurnstile } from './accounts';
import { expect, test } from './fixtures';

const SCREENSHOTS = path.join(STATE_DIR, 'screenshots');
const REQUESTER = 'requester@example.com';
const OWNER = 'owner@example.com';
// ADAPTER_REQUEST_NOTIFY_EMAIL in playwright.config.ts: new requests are announced there, not to the owner's sign-in.
const NOTIFY = 'notify@example.com';
const QUESTION = 'Which team should handle this support email?';
const TEAMS = ['billing', 'tech', 'none'];
const CSV = ['input,answer,tricky', ...Array.from({ length: 30 }, (_, i) =>
  `"Support email ${i + 1}: a question about ${TEAMS[i % 3]}",${TEAMS[i % 3]},${i === 0 ? 'yes' : 'no'}`)].join('\n');

async function noSideScroll(page: Page) {
  const { scroll, client } = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, client: document.documentElement.clientWidth }));
  expect(scroll, 'the page scrolls sideways').toBeLessThanOrEqual(client);
}

async function fillForm(page: Page) {
  await page.getByLabel('The question, in one sentence').fill(QUESTION);
  await page.getByLabel('Why it matters, in one or two sentences').fill('Emails reach the right team on the first try.');
  await page.getByLabel('One option from a fixed list').check();
  await page.getByLabel('A "none" option').check();
  await page.getByLabel('Options: key 1').fill('billing');
  await page.getByLabel('Options: when "billing" is right').fill('Payments, invoices and refunds.');
  await page.getByLabel('Options: key 2').fill('tech');
  await page.getByLabel('Options: when "tech" is right').fill('Bugs and errors in the product.');
  await page.getByLabel('Options: when "none" is right').fill('Neither team: sales, spam or anything else.');
  await page.getByLabel('One text').check();
  await page.getByLabel('Typical length (characters)').fill('600');
  await page.getByLabel('Maximum length (characters)').fill('5000');
  await page.getByLabel('Language or languages').fill('English');
  await page.getByLabel('Generate the data for me').check();
  await page.getByLabel('Paste CSV or JSON Lines').fill(CSV);
  await page.getByRole('button', { name: 'Add pasted examples' }).click();
  await page.getByLabel('How many more real examples could you supply later?').fill('500');
  await page.getByLabel('Calls a day, roughly').fill('2000');
  await page.getByRole('group', { name: 'Does speed matter (an answer in under 50 ms)?' }).getByLabel('No').check();
  await page.getByLabel('What accuracy would make it useful?').fill('Right 95 times in 100');
  await page.getByRole('group', { name: 'Is no answer better than a wrong answer?' }).getByLabel('Yes').check();
  await page.getByLabel('I have the right to share these examples.').check();
  await page.getByLabel('They contain no personal data, or it has been removed.').check();
}

test('a request goes from the form to the account page and the owner\'s list', async ({ page, allowFailedLoad }) => {
  // Signed out: the form sends the visitor to sign in, and the email link brings them back to it.
  allowFailedLoad('/api/adapter-requests');
  allowFailedLoad('/api/admin/adapter-requests');
  await stubTurnstile(page);
  await page.goto('/request-adapter');
  await expect(page).toHaveURL(/\/signin\?next=%2Frequest-adapter$/);
  await page.getByLabel('Or sign in with a link sent to your email').fill(REQUESTER);
  await page.getByRole('button', { name: 'Email me a link' }).click();
  await page.getByText(`Check your email: we sent a link to ${REQUESTER}.`, { exact: false }).waitFor();
  await page.goto(linkIn(newestMailTo(REQUESTER)));
  await page.waitForURL('**/request-adapter');

  await fillForm(page);
  await expect(page.getByText('Pasted: 30 examples added.')).toBeVisible();
  await expect(page.getByLabel('Examples per option')).toHaveText('billing: 10tech: 10none: 10');
  await expect(page.getByRole('button', { name: 'Send request' })).toBeEnabled();
  await page.setViewportSize({ width: 375, height: 812 });
  await noSideScroll(page);
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(SCREENSHOTS, 'request-form-375.png'), fullPage: true });
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(SCREENSHOTS, 'request-form-1280.png'), fullPage: true });

  const sent = page.waitForResponse((r) => r.url().endsWith('/api/adapter-requests') && r.request().method() === 'POST');
  await page.getByRole('button', { name: 'Send request' }).click();
  expect((await sent).status()).toBe(201);
  await expect(page.getByRole('heading', { level: 1, name: 'Request sent' })).toBeVisible();
  await expect(page.getByText("Thank you. We'll reply within a few days by email.")).toBeVisible();
  const number = (await page.getByText(/^AR-[A-Z2-7]{8}$/).textContent())!;

  // The notification address is told, with the request number and a link, and never the examples.
  const mail = newestMailTo(NOTIFY);
  expect(mail.text).toContain(`Request number: ${number}`);
  expect(mail.text).toContain(`From: ${REQUESTER}`);
  expect(mail.text).not.toContain('Support email 1');

  // The account page lists it.
  await page.goto('/account');
  const section = page.getByRole('region', { name: 'Your adapter requests' });
  await expect(section.getByText(QUESTION)).toBeVisible();
  await expect(section).toContainText(`${number} · Received · sent`);
  await section.screenshot({ path: path.join(SCREENSHOTS, 'account-requests-1280.png') });

  // The admin list is not the requester's to see.
  await page.goto('/admin/requests');
  await expect(page.getByRole('heading', { level: 1, name: 'Not found' })).toBeVisible();

  // The owner sees it, opens it and moves it on.
  await page.context().clearCookies();
  await signInWithProvider(page, { sub: 'oidc-owner', email: OWNER, email_verified: true, name: 'Owner' });
  await page.waitForURL('**/account');
  const signedInAt = Date.now();
  await page.goto('/admin/requests');
  const row = page.getByRole('row').filter({ hasText: number });
  await expect(row).toContainText(REQUESTER);
  await expect(row).toContainText(QUESTION);
  await expect(row).toContainText('Received');
  await expect(row).toContainText('30');
  await page.screenshot({ path: path.join(SCREENSHOTS, 'admin-requests-1280.png'), fullPage: true });
  await row.getByRole('link', { name: number }).click();
  await expect(page.getByRole('heading', { level: 2, name: number })).toBeVisible();
  await page.getByRole('combobox', { name: 'Status' }).selectOption('in_review');
  await page.getByRole('button', { name: 'Set status' }).click();
  await expect(page.getByRole('status')).toHaveText('Status set to In review.');
  const exported = await page.request.get(`/api/admin/adapter-requests/${number}/export?file=examples`);
  expect((await exported.text()).trimEnd().split('\n')).toHaveLength(30);

  // Better Auth's social sign-in window (above): 10 quiet seconds before the next check's.
  await page.waitForTimeout(Math.max(0, 11_000 - (Date.now() - signedInAt)));
});
