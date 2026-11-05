// The playground in the browser, against the built site, the hub's function and a stand-in Jeff API (e2e/global-setup.ts):
// a visitor passes the bot check once, runs a sample live and sees the answer and the code; the next runs carry the
// pass cookie instead of a token; past PLAYGROUND_RUNS_PER_MINUTE (5) the run is refused with a countdown. An adapter
// page's "Open in playground" opens the playground with that sample chosen. Nothing scrolls sideways at a phone's
// width or a desktop's. Photographed into .e2e/site/screenshots.
import path from 'node:path';
import type { Page } from '@playwright/test';
import { STATE_DIR } from '../playwright.config';
import { stubTurnstile } from './accounts';
import { expect, test } from './fixtures';

const SCREENSHOTS = path.join(STATE_DIR, 'screenshots');
const WIDTHS = [375, 1280];

async function photograph(page: Page, name: string) {
  const overflow = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  expect(overflow, `${name} scrolls sideways`).toBe(0);
  for (const width of WIDTHS) {
    await page.setViewportSize({ width, height: 900 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth), `${name} at ${width} px`).toBe(0);
    await page.screenshot({ path: path.join(SCREENSHOTS, `${name}-${width}.png`), fullPage: true });
  }
}

test('a visitor runs a sample live once past the bot check, runs on with the pass, and is refused past 5 a minute', async ({ page, context, allowFailedLoad }) => {
  allowFailedLoad('/api/playground/decide');
  await stubTurnstile(page);
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/playground');
  await expect(page.getByRole('heading', { level: 1 })).toHaveText('Playground');
  await expect(page.getByTestId('turnstile')).toBeAttached();
  await expect(page.getByText(/^Visitors can run 5 a minute after a bot check/)).toBeVisible();

  const sent: (string | null)[] = [];
  page.on('request', (request) => {
    if (request.url().endsWith('/api/playground/decide')) sent.push(request.headers()['x-turnstile-token'] ?? null);
  });
  await expect(page.getByRole('button', { name: 'Run' })).toBeEnabled();
  const answered = page.waitForResponse('**/api/playground/decide');
  await page.getByRole('button', { name: 'Run' }).click();
  expect((await answered).status()).toBe(200);
  const answer = page.getByRole('region', { name: 'Answer', exact: true });
  await expect(answer).toContainText('Confidence 0.93');
  await expect(answer).toContainText('Ran as a visitor');
  await expect(answer).toContainText(/\d+(\.\d)? ms/);
  await expect(page.getByText('The call for the input you ran.')).toBeVisible();
  await expect(page.getByTestId('turnstile')).toHaveCount(0);
  const pass = (await context.cookies()).find((cookie) => cookie.name === 'jeff_playground_pass');
  expect(pass).toMatchObject({ httpOnly: true, secure: true, sameSite: 'Strict', path: '/api/playground' });
  await photograph(page, 'playground-run');

  // Four more runs on the pass alone, then the sixth in the minute is refused.
  for (let n = 2; n <= 6; n += 1) {
    const next = page.waitForResponse('**/api/playground/decide');
    await page.getByRole('button', { name: 'Run' }).click();
    const response = await next;
    expect(response.status(), `run ${n}`).toBe(n <= 5 ? 200 : 429);
    if (n <= 5) await expect(answer).toBeVisible();
  }
  expect(sent[0]).toBe('e2e-pass');
  expect(sent.slice(1)).toEqual([null, null, null, null, null]);
  await expect(page.getByRole('alert')).toHaveText('You can run 5 a minute, and this minute\'s runs are used up.');
  await expect(page.getByRole('timer')).toHaveText(/^You can run again in \d+ s$/);
  await expect(page.getByRole('button', { name: 'Run' })).toBeDisabled();
  await expect(answer).toHaveCount(0);
  await photograph(page, 'playground-refused');
});

test('an adapter page opens the playground with its sample chosen', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/catalogue/jeff.support.message-triage');
  const link = page.getByRole('main').getByRole('link', { name: 'Open in playground' }).first();
  await expect(link).toBeVisible();
  const href = await link.getAttribute('href');
  expect(href).toMatch(/^\/playground\?adapter=jeff\.support\.message-triage&sample=/);
  await photograph(page, 'adapter-link');
  await page.setViewportSize({ width: 1280, height: 900 });
  await link.click();
  await expect(page).toHaveURL(/\/playground\?adapter=jeff\.support\.message-triage&sample=/);
  const sample = new URL(href as string, 'http://x').searchParams.get('sample');
  await expect(page.getByLabel('Sample')).toHaveValue(sample as string);
  await expect(page.getByLabel('Adapter')).toHaveValue('jeff.support.message-triage');
  // The menu shows each sample by its name; the value and the query keep its id.
  const shown = await page.getByLabel('Sample').evaluate((select: HTMLSelectElement) => select.selectedOptions[0].textContent);
  expect(shown).not.toBe(sample);
  expect(shown?.length).toBeGreaterThan(0);
  // Choosing another adapter writes the query.
  await page.getByLabel('Adapter').selectOption('jeff.safety.guard');
  await expect(page).toHaveURL(/\/playground\?adapter=jeff\.safety\.guard&sample=/);
});

// The "Call it" code of an adapter page (every adapter on the public site is open): every shown language and curl,
// following the visitor's choice, and no code that scrolls sideways in any tab.
for (const url of ['/catalogue/jeff.safety.spam']) {
  test(`the adapter page ${url} shows its call in every language`, async ({ page }) => {
    await page.setViewportSize({ width: 375, height: 900 });
    await page.goto(url);
    const call = page.locator('figure[data-call]');
    await expect(call).toHaveCount(1);
    const tabs = await call.getByRole('tab').allTextContents();
    expect(tabs).toEqual(['Python', 'Node', 'Rust', 'C#', 'Java', 'curl']);
    for (const tab of tabs) {
      await call.getByRole('tab', { name: tab }).click();
      const sideways = await call.evaluate((figure) => { const pre = figure.querySelector('pre')!; return pre.scrollWidth - pre.clientWidth; });
      expect(sideways, tab).toBe(0);
    }
    await call.getByRole('tab', { name: 'Python' }).click();
    await expect(call.locator('pre')).toContainText('jeff.Jeff.remote(');
    await call.scrollIntoViewIfNeeded();
    for (const width of WIDTHS) {
      await page.setViewportSize({ width, height: 900 });
      expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth), `${url} at ${width} px`).toBe(0);
      await call.screenshot({ path: path.join(SCREENSHOTS, `call-open-${width}.png`) });
    }
  });
}
