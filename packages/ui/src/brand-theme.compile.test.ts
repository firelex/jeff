// @vitest-environment node
//
// This is the only test in the workspace that actually runs Tailwind: it compiles
// packages/brand/tokens.css + fonts.css (the `@theme` mapping) against the real class
// names Tailwind's own scanner finds in packages/ui/src/components/**/*.tsx (excluding
// test files), using Tailwind 4's Node API — the same pipeline @tailwindcss/vite drives
// internally, minus the dev-server/bundler integration. Nothing else here exercises
// Tailwind at all: `packages/ui`'s other tests run in jsdom with no CSS pipeline, so a
// typo in a token name or a missing `@theme` entry would otherwise go unnoticed until a
// console app is actually built and its page looks wrong.
import { existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { compile } from '@tailwindcss/node';
import { Scanner } from '@tailwindcss/oxide';

const uiDir = path.dirname(path.dirname(fileURLToPath(import.meta.url))); // packages/ui

const ENTRY_CSS = `
@import "tailwindcss";
@import "@jeff/brand/tokens.css";
@import "@jeff/brand/fonts.css";
`;

async function compileRealComponents() {
  const result = await compile(ENTRY_CSS, {
    base: uiDir,
    from: path.join(uiDir, 'virtual-entry.css'),
    onDependency: () => {},
  });

  // Mirrors @tailwindcss/vite's own fallback when `@import "tailwindcss"` doesn't name an
  // explicit `source(...)`: scan everything under the given base. Scoped here to the real
  // component sources only (not their *.test.tsx siblings, not config files), so a class
  // name only ever gets into `candidates` because a component actually uses it.
  const root =
    result.root === 'none'
      ? []
      : result.root === null
        ? [{ base: uiDir, pattern: 'src/components/**/*.tsx', negated: false }]
        : [{ ...result.root, negated: false }];
  const sources = root.concat([{ base: uiDir, pattern: 'src/components/**/*.test.tsx', negated: true }], result.sources);

  const scanner = new Scanner({ sources });
  const candidates = scanner.scan();
  const css = result.build(candidates);
  return { candidates, css };
}

// A rule's declarations, tolerant of the compiler's exact indentation/line breaks.
function rule(css: string, selector: string): string {
  const escaped = selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const match = css.match(new RegExp(`${escaped}\\s*\\{([^}]*)\\}`));
  if (!match) throw new Error(`No rule for ${selector} in the compiled CSS`);
  return match[1];
}

function customProperty(css: string, name: string): string {
  const match = css.match(new RegExp(`${name.replace('--', '--')}:\\s*([^;]+);`));
  if (!match) throw new Error(`No ${name} declaration in the compiled CSS`);
  return match[1].trim();
}

describe('the brand theme compiles with real Tailwind, from the actual component sources', () => {
  it('finds every utility this round is about, by scanning packages/ui/src/components (not by assuming it)', async () => {
    const { candidates } = await compileRealComponents();
    for (const utility of ['bg-grad', 'rounded-tag', 'rounded-status', 'shadow-violet', 'font-mono', 'bg-primary']) {
      expect(candidates, `expected a component to actually use "${utility}"`).toContain(utility);
    }
  });

  it('bg-grad resolves through --background-image-grad to the deck\'s exact gradient', async () => {
    const { css } = await compileRealComponents();
    expect(rule(css, '.bg-grad')).toContain('background-image: var(--background-image-grad)');
    expect(customProperty(css, '--background-image-grad')).toBe('var(--grad)');
    expect(customProperty(css, '--grad')).toBe('linear-gradient(78deg, #7438fe -47.47%, #d267ff 100%)');
  });

  it('rounded-tag and rounded-status use the deck\'s own 4px/5px radii, not each other\'s', async () => {
    const { css } = await compileRealComponents();
    expect(rule(css, '.rounded-tag')).toContain('border-radius: var(--radius-tag)');
    expect(customProperty(css, '--radius-tag')).toBe('4px');
    expect(rule(css, '.rounded-status')).toContain('border-radius: var(--radius-status)');
    expect(customProperty(css, '--radius-status')).toBe('5px');
  });

  it('shadow-violet carries the deck\'s exact box-shadow value', async () => {
    const { css } = await compileRealComponents();
    expect(rule(css, '.shadow-violet')).toContain('0 12px 32px -12px var(--tw-shadow-color, rgba(117, 71, 254, .45))');
  });

  it('font-mono resolves to the bundled JetBrains Mono stack', async () => {
    const { css } = await compileRealComponents();
    expect(rule(css, '.font-mono')).toContain('font-family: var(--font-mono)');
    expect(customProperty(css, '--font-mono')).toContain("'JetBrains Mono'");
  });

  it('bg-primary resolves through --color-primary to the deck\'s exact violet', async () => {
    const { css } = await compileRealComponents();
    expect(rule(css, '.bg-primary')).toContain('background-color: var(--color-primary)');
    expect(customProperty(css, '--color-primary')).toBe('var(--v)');
    expect(customProperty(css, '--v')).toBe('#7547fe');
  });
});

describe('the bundled fonts', () => {
  it('are only the latin and latin-ext subsets, as woff2, of Inter 400-800 and JetBrains Mono 400-500, and every file exists', async () => {
    const { css } = await compileRealComponents();
    const sources = [...css.matchAll(/src:\s*url\(['"]?([^'")]+)['"]?\)\s*format\(['"]?([a-z0-9]+)['"]?\)\s*;/g)];
    const names = sources.map((match) => match[1].split('/').pop()).sort();
    const expected = [
      ...[400, 500, 600, 700, 800].flatMap((weight) => [`inter-latin-${weight}-normal.woff2`, `inter-latin-ext-${weight}-normal.woff2`]),
      ...[400, 500].flatMap((weight) => [`jetbrains-mono-latin-${weight}-normal.woff2`, `jetbrains-mono-latin-ext-${weight}-normal.woff2`]),
    ].sort();
    expect(names).toEqual(expected);
    expect(sources.every((match) => match[2] === 'woff2')).toBe(true);
    expect((css.match(/unicode-range:/g) ?? []).length).toBe(expected.length);
    const brandDir = path.join(path.dirname(uiDir), 'brand');
    for (const match of sources) expect(existsSync(path.join(brandDir, match[1])), match[1]).toBe(true);
  });
});
