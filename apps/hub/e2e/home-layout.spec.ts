import { fileURLToPath } from 'node:url';
import type { Page } from '@playwright/test';
import { SDK_LABELS } from '../app/lib/sdk-languages';
import { loadSdkLanguages } from '../app/lib/sdk-languages.server';
import { expect, test } from './fixtures';

// The home page's example screen, measured in a real browser (jsdom has no layout): nothing inside the screen panel
// may stick out of it, the two code windows sit side by side without overlapping and start and end level, and at
// phone width nothing scrolls the page sideways -- in the tab of every language the site shows
// (content/sdk-languages.yaml). The code wraps its long lines, so it never scrolls sideways inside its window either.

const TABS = loadSdkLanguages(fileURLToPath(new URL('../content/sdk-languages.yaml', import.meta.url))).map(({ language }) => SDK_LABELS[language]);

async function openTab(page: Page, width: number, tab: string) {
  await page.setViewportSize({ width, height: 900 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await page.goto('/');
  await page.getByRole('tab', { name: tab }).click();
}

for (const tab of TABS) {
  test(`at 1280 px in the ${tab} tab, the code windows stay inside the screen, side by side and level`, async ({ page }) => {
    await openTab(page, 1280, tab);
    const m = await page.evaluate(() => {
      const panels = [...document.querySelectorAll<HTMLElement>('[role=tabpanel]')];
      const screenPanel = panels[0]!.closest<HTMLElement>('.on-screen')!;
      const outer = screenPanel.getBoundingClientRect();
      // Every rendered element (an element in a hidden tab has no boxes), except the code's own lines inside a <pre>,
      // which are measured by the pre's scroll width below.
      const sticking = [...screenPanel.querySelectorAll<Element>('*')]
        .filter((el) => el.getClientRects().length > 0 && el.closest('pre') === null)
        .filter((el) => { const r = el.getBoundingClientRect(); return r.left < outer.left - 0.5 || r.right > outer.right + 0.5; })
        .map((el) => `${el.tagName}.${el.getAttribute('class') ?? ''}`);
      const windows = panels.map((p) => {
        const pre = [...p.querySelectorAll<HTMLElement>('pre')].find((el) => el.offsetParent !== null)!;
        const r = pre.closest<HTMLElement>('.on-screen')!.getBoundingClientRect();
        return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, sideways: pre.scrollWidth - pre.clientWidth };
      });
      return { sticking, windows, pageOverflow: document.documentElement.scrollWidth - window.innerWidth };
    });
    expect(m.sticking).toEqual([]);
    expect(m.pageOverflow).toBe(0);
    const [left, right] = m.windows;
    expect(left!.right).toBeLessThanOrEqual(right!.left);
    expect(Math.round(left!.top)).toBe(Math.round(right!.top));
    expect(Math.round(left!.bottom)).toBe(Math.round(right!.bottom));
    expect(m.windows.map((w) => w.sideways)).toEqual([0, 0]);
  });

  test(`at 360 px in the ${tab} tab, nothing scrolls the page or the code sideways`, async ({ page }) => {
    await openTab(page, 360, tab);
    expect(await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth)).toBe(0);
    expect(await page.evaluate(() => [...document.querySelectorAll<HTMLElement>('pre')]
      .filter((pre) => pre.offsetParent !== null).map((pre) => pre.scrollWidth - pre.clientWidth))).toEqual([0, 0]);
  });
}
