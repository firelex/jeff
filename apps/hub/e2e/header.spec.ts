// The header's account buttons and the pricing page in the browser, against the built site and the hub's function:
// a visitor sees "Sign in" and "Get an API key" once the page knows there is no session, and the header fits at every
// width. (A signed-in customer's avatar is checked in account.spec.ts, by an account that signs in there anyway: the
// email-link sign-in allows 5 a minute from one address.) Below 1024 px there is room for one key only:
// "Sign in" is left out and "Get an API key" is a key icon. Photographed at a phone's width
// and a desktop's, into .e2e/site/screenshots.
import path from 'node:path';
import type { Page } from '@playwright/test';
import { STATE_DIR } from '../playwright.config';
import { expect, test } from './fixtures';

const SCREENSHOTS = path.join(STATE_DIR, 'screenshots');

// The home page's screen types its code in line by line; with reduced motion it shows it all at once, so the
// photograph never catches the code windows empty. On the home page it also waits until the screen's closing "Ready."
// line is shown (app.css: .type-line, opacity 1 once typed).
async function photograph(page: Page, name: string) {
  if (new URL(page.url()).pathname === '/') {
    await page.waitForFunction(() => [...document.querySelectorAll<HTMLElement>('.type-line')]
      .some((line) => line.textContent?.trim() === 'Ready.' && getComputedStyle(line).opacity === '1'));
  }
  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: path.join(SCREENSHOTS, name), fullPage: true });
}

// Nothing scrolls the page sideways, and every visible part of the header is inside the window.
async function headerFits(page: Page) {
  const m = await page.evaluate(() => {
    const header = document.querySelector('header')!;
    const outside = [...header.querySelectorAll<HTMLElement>('a, button, input')]
      .filter((el) => el.getClientRects().length > 0 && getComputedStyle(el).visibility !== 'hidden')
      .filter((el) => { const r = el.getBoundingClientRect(); return r.left < -0.5 || r.right > window.innerWidth + 0.5; })
      .map((el) => el.textContent);
    return { outside, overflow: document.documentElement.scrollWidth - window.innerWidth };
  });
  expect(m.outside).toEqual([]);
  expect(m.overflow).toBe(0);
}

for (const width of [375, 640, 768, 1024, 1280]) {
  test(`at ${width} px a visitor sees Sign in and Get an API key, and the header fits`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/');
    const banner = page.getByRole('banner');
    const key = banner.getByRole('link', { name: 'Get an API key' });
    await expect(key).toHaveAttribute('href', '/signup');
    await expect(key).toBeVisible();
    const signIn = banner.getByRole('link', { name: 'Sign in', includeHidden: true });
    await expect(signIn).toHaveAttribute('href', '/signin');
    if (width >= 1024) await expect(signIn).toBeVisible();
    else await expect(signIn).toBeHidden();
    await headerFits(page);
    // The search box keeps its full width (it does not shrink to make room); a phone gets the search button instead.
    const search = banner.getByRole('searchbox', { name: 'Search' });
    if (width >= 640) expect((await search.boundingBox())?.width).toBe(176);
    else await expect(search).toBeHidden();
    // The three links, Playground among them.
    await expect(banner.getByRole('navigation', { name: 'Site' }).getByRole('link')).toHaveText(['Docs', 'Catalogue', 'Playground']);
    await photograph(page, `header-visitor-${width}.png`);
  });
}

for (const width of [375, 1280]) {
  test(`at ${width} px /pricing shows both plans and the comparison line`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.goto('/pricing');
    await expect(page.getByRole('heading', { level: 1, name: 'Pricing' })).toBeVisible();
    await expect(page.getByText('Free forever, every adapter, the SDKs and the engine on your machine.')).toBeVisible();
    await expect(page.getByText('$1 a month, 50 million input tokens a month, every adapter, cancel any time.')).toBeVisible();
    await expect(page.getByText("Half TypeSafe's price per token at the full allowance.")).toBeVisible();
    await expect(page.getByRole('main').getByRole('link', { name: 'Get an API key' })).toHaveAttribute('href', '/signup');
    await headerFits(page);
    await photograph(page, `pricing-${width}.png`);
  });
}
