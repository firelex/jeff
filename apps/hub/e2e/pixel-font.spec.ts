import { expect, test } from './fixtures';

// Jeff Pixel (the headings', navigation's and buttons' font) is crisp only at whole pixels per cell and on whole-pixel
// positions (packages/brand/fonts/jeff-pixel/README.md). Measured in a real browser on the home page, a docs page and
// the catalogue, at phone and desktop width: the font loads; everything set in it is at a multiple of 10 px, with no
// letter spacing, no synthesized bold or italic, no font smoothing and no transform; and every line of its text starts
// on a whole pixel. Centred lines are the one exception to the last rule: centring can leave half a pixel, which the
// switched-off font smoothing snaps to device pixels (macOS browsers).

const PAGES = { home: '/', docs: '/docs/guides/escalation', catalogue: '/catalogue', adapter: '/catalogue/jeff.compliance.sanctions' };

for (const width of [390, 1280]) {
  for (const [name, url] of Object.entries(PAGES)) {
    test(`at ${width} px, the ${name} page sets Jeff Pixel at whole-cell sizes on whole pixels`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await page.emulateMedia({ reducedMotion: 'reduce' });
      await page.goto(url);
      await page.evaluate(() => document.fonts.ready);
      const m = await page.evaluate(() => {
        const problems: string[] = [];
        let elements = 0;
        for (const el of document.querySelectorAll<HTMLElement>('body *')) {
          const cs = getComputedStyle(el);
          if (!cs.fontFamily.includes('Jeff Pixel') || el.getClientRects().length === 0) continue;
          elements += 1;
          const what = `${el.tagName} "${(el.textContent ?? '').trim().slice(0, 40)}"`;
          const size = parseFloat(cs.fontSize);
          if (size % 10 !== 0) problems.push(`${what}: font-size ${cs.fontSize}`);
          if (cs.letterSpacing !== 'normal' && cs.letterSpacing !== '0px') problems.push(`${what}: letter-spacing ${cs.letterSpacing}`);
          if (cs.fontSynthesis !== 'none') problems.push(`${what}: font-synthesis ${cs.fontSynthesis}`);
          const smoothing = cs.getPropertyValue('-webkit-font-smoothing');
          if (smoothing !== 'none') problems.push(`${what}: -webkit-font-smoothing ${smoothing}`);
          if (cs.transform !== 'none') problems.push(`${what}: transform ${cs.transform}`);
          if (cs.textAlign === 'center') continue;
          for (const node of el.childNodes) {
            if (node.nodeType !== Node.TEXT_NODE || (node.textContent ?? '').trim() === '') continue;
            const range = document.createRange();
            range.selectNodeContents(node);
            for (const r of range.getClientRects()) {
              const x = r.left + window.scrollX;
              const y = r.top + window.scrollY;
              if (r.width > 0 && (Math.abs(x - Math.round(x)) > 0.01 || Math.abs(y - Math.round(y)) > 0.01)) {
                problems.push(`${what}: a line starts at ${x.toFixed(2)}, ${y.toFixed(2)}`);
              }
            }
          }
        }
        const loaded = [...document.fonts].filter((f) => f.family.replace(/"/g, '') === 'Jeff Pixel').map((f) => f.status);
        return { problems, elements, loaded };
      });
      // The dev server can register the @font-face more than once (its CSS module and the page's stylesheet).
      expect(m.loaded.length).toBeGreaterThan(0);
      expect(m.loaded.every((status) => status === 'loaded')).toBe(true);
      expect(m.elements).toBeGreaterThan(0);
      expect(m.problems).toEqual([]);
    });
  }
}
