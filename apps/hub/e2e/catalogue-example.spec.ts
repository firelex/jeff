import path from 'node:path';
import { STATE_DIR } from '../playwright.config';
import { expect, test } from './fixtures';
import { CODE_MODE_LABELS } from '../app/lib/code-mode';

// The worked examples on an adapter's catalogue page (one or more), measured in a real browser: at phone and desktop
// width each example's case and answer come before its code, and neither the page nor the code (in any of its tabs)
// scrolls sideways. Every adapter on the public site is open (the others are the owner's only).

const PAGES = ['/catalogue/jeff.safety.spam', '/catalogue/jeff.compliance.sanctions', '/catalogue/jeff.legal.legal-clauses'];

for (const width of [375, 1280]) {
  for (const url of PAGES) {
    test(`at ${width} px, ${url} shows each example's case and answer above code that does not scroll sideways`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.goto(url);
      const figures = await page.locator('figure[data-example]').all();
      expect(figures.length).toBeGreaterThan(0);
      for (const figure of figures) {
        const name = await figure.getAttribute('data-example');
        // The featured example is open; each one in the "More examples" list opens from its line.
        const line = page.locator(`details[data-example-line="${name}"]`);
        if (await line.count() === 1) {
          await line.locator('summary').click();
          await expect(line).toHaveAttribute('open', '');
          const sideways = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
          expect(sideways, `${name}: the page scrolls sideways with its line open`).toBe(0);
        }
        // Every tab the example has: each language the site shows, and curl; only Python for an offline example
        // (app/lib/example-block.ts), which has no request and no recorded answer.
        const tabs = await figure.getByRole('tab').allTextContents();
        const offline = tabs.length === 1;
        if (offline) expect(tabs, `${name}`).toEqual(['Python']);
        else expect(tabs.at(-1), `${name}`).toBe('curl');
        for (const tab of tabs) {
          await figure.getByRole('tab', { name: tab }).click();
          const m = await figure.evaluate((fig) => {
            const pre = fig.querySelector<HTMLElement>('pre')!;
            const answer = [...fig.querySelectorAll('dt')].find((dt) => dt.textContent === 'Answer (JSON)');
            return {
              page: document.documentElement.scrollWidth - window.innerWidth,
              code: pre.scrollWidth - pre.clientWidth,
              answerAboveCode: answer === undefined ? null : answer.getBoundingClientRect().bottom <= pre.getBoundingClientRect().top,
            };
          });
          if (offline) {
            expect(m, `${name} ${tab}`).toEqual({ page: 0, code: 0, answerAboveCode: null });
            continue;
          }
          expect(m, `${name} ${tab}`).toEqual({ page: 0, code: 0, answerAboveCode: true });
        }
      }
    });
  }
}

// "Through the API" or "On your machine" on an open adapter's page: every worked example and the "Call it" block
// offer it, choosing it on one switches them all and is remembered after a reload (the first render, the prerendered page,
// is the API form, so hydration matches it before the stored choice is read -- the fixture fails the check on the
// hydration error React would log otherwise), and no local tab scrolls sideways. Photographed in both modes at a phone's width and a desktop's, into .e2e/site/screenshots.
const SCREENSHOTS = path.join(STATE_DIR, 'screenshots');

for (const width of [375, 1280]) {
  test(`at ${width} px, an open adapter's code runs through the API or on your machine, the choice remembered`, async ({ page }) => {
    await page.setViewportSize({ width, height: 900 });
    await page.emulateMedia({ reducedMotion: 'reduce' });
    await page.goto('/catalogue/jeff.safety.spam');
    const blocks = page.locator('figure[data-example], figure[data-call]');
    await expect(page.locator('figure[data-call]')).toHaveCount(1);
    expect(await page.locator('figure[data-example]').count()).toBeGreaterThan(0);
    // The examples in the "More examples" list are folded; open each so its choice is seen.
    for (const line of await page.locator('details[data-example-line]').all()) await line.locator('summary').click();
    for (const block of await blocks.all()) {
      await expect(block.getByRole('button', { name: CODE_MODE_LABELS.api })).toHaveAttribute('aria-pressed', 'true');
      await expect(block.getByRole('tab', { name: 'curl' })).toBeVisible();
    }
    const call = page.locator('figure[data-call]');
    await call.scrollIntoViewIfNeeded();
    await call.screenshot({ path: path.join(SCREENSHOTS, `call-api-${width}.png`) });
    await blocks.first().getByRole('button', { name: CODE_MODE_LABELS.local }).click();
    for (const block of await blocks.all()) {
      await expect(block.getByRole('button', { name: CODE_MODE_LABELS.local })).toHaveAttribute('aria-pressed', 'true');
      await expect(block.getByRole('tab', { name: 'curl' })).toHaveCount(0);
      await expect(block.locator('pre')).toContainText('jeff.Jeff.local(["spam"])');
    }
    await call.screenshot({ path: path.join(SCREENSHOTS, `call-local-${width}.png`) });
    await page.evaluate(() => window.scrollTo(0, 0));
    await page.screenshot({ path: path.join(SCREENSHOTS, `adapter-local-${width}.png`), fullPage: true });
    for (const tab of await call.getByRole('tab').allTextContents()) {
      await call.getByRole('tab', { name: tab }).click();
      const m = await call.evaluate((fig) => {
        const pre = fig.querySelector<HTMLElement>('pre')!;
        return { page: document.documentElement.scrollWidth - window.innerWidth, code: pre.scrollWidth - pre.clientWidth };
      });
      expect(m, tab).toEqual({ page: 0, code: 0 });
    }
    await page.reload();
    await expect(call.getByRole('button', { name: CODE_MODE_LABELS.local })).toHaveAttribute('aria-pressed', 'true');
    await expect(call.locator('pre')).toContainText('Jeff');
    await page.evaluate(() => window.scrollTo(0, 0));
    await call.getByRole('button', { name: CODE_MODE_LABELS.api }).click();
    // The last tab chosen above (Java) is the site's language now; Python again for the photograph.
    await call.getByRole('tab', { name: 'Python' }).click();
    await expect(blocks.first().getByRole('button', { name: CODE_MODE_LABELS.api })).toHaveAttribute('aria-pressed', 'true');
    await expect(blocks.first().locator('pre')).toContainText('jeff.Jeff.remote(');
    await page.screenshot({ path: path.join(SCREENSHOTS, `adapter-api-${width}.png`), fullPage: true });
  });
}
