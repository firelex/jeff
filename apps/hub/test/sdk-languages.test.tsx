// content/sdk-languages.yaml is the one setting that turns a language's tab on: these tests read the site through
// its real loaders (docs pages, catalogue adapter pages, the home page), with a language turned off on a synthetic
// setting and again with every language turned on, and render every worked example. scripts/check-build.mjs makes
// the same check of the built files (HTML and .data): an off language's code is in none of them.
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Example } from '../app/components/docs/Example';
import { EXAMPLE_LANGUAGE, type ExampleBlock } from '../app/lib/example-block';
import { SDK_CODE_MARKERS, SDK_LABELS, SDK_LANGUAGES, type SdkLanguage } from '../app/lib/sdk-languages';
import { loadSdkLanguages } from '../app/lib/sdk-languages.server';
import { PLAN_ENV } from './helpers/plan';

const REAL = path.join(__dirname, '../content/sdk-languages.yaml');
const BLOCK = new RegExp(`\`\`\`${EXAMPLE_LANGUAGE}\\n(.*)\\n\`\`\``, 'g');

function fileWith(text: string): string {
  const file = path.join(fs.mkdtempSync(path.join(os.tmpdir(), 'sdk-languages-')), 'sdk-languages.yaml');
  fs.writeFileSync(file, text);
  return file;
}

const entry = (language: SdkLanguage, shown: boolean, name: string | null = 'pkg') => `${language}:\n  shown: ${shown}\n  package: ${name === null ? 'null' : name}\n`;
const ALL_ON = SDK_LANGUAGES.map((l) => entry(l, true)).join('');

describe('loadSdkLanguages', () => {
  it('reads the real setting: Python, Node, Rust, C# and Java all shown', () => {
    expect(loadSdkLanguages(REAL)).toEqual([{ language: 'python', package: 'jeff-sdk' }, { language: 'node', package: 'jeff-sdk' },
      { language: 'rust', package: 'jeff-sdk' }, { language: 'csharp', package: 'Jeff.Sdk' }, { language: 'java', package: 'ai.jeffhub:jeff-sdk:1.4.0' }]);
  });
  it('returns the shown languages in tab order, whatever order the file lists them in', () => {
    const text = [...SDK_LANGUAGES].reverse().map((l) => entry(l, true)).join('');
    expect(loadSdkLanguages(fileWith(text)).map((l) => l.language)).toEqual([...SDK_LANGUAGES]);
  });
  it('names what is wrong with a setting', () => {
    const without = (language: SdkLanguage) => SDK_LANGUAGES.filter((l) => l !== language).map((l) => entry(l, true)).join('');
    expect(() => loadSdkLanguages(fileWith(without('java')))).toThrow('no entry for java');
    expect(() => loadSdkLanguages(fileWith(`${ALL_ON}go:\n  shown: true\n  package: x\n`))).toThrow('unknown languages go');
    expect(() => loadSdkLanguages(fileWith(without('java') + entry('java', true, null)))).toThrow('java is shown but has no package');
    expect(() => loadSdkLanguages(fileWith(without('python') + entry('python', false)))).toThrow('python must be shown');
    expect(() => loadSdkLanguages(fileWith(without('rust') + 'rust:\n  shown: yes\n  package: x\n'))).toThrow('rust.shown must be true or false');
    expect(() => loadSdkLanguages(fileWith(without('rust') + 'rust:\n  shown: true\n'))).toThrow('rust must have exactly shown and package');
  });
  it('accepts an off language with no package yet', () => {
    const text = SDK_LANGUAGES.map((l) => entry(l, l === 'python' || l === 'node', l === 'java' ? null : 'pkg')).join('');
    expect(loadSdkLanguages(fileWith(text)).map((l) => l.language)).toEqual(['python', 'node']);
  });
});

// Every page's loader data, through the site's own loaders with `languages` as the setting: each docs page's body,
// each catalogue adapter page's loader data, and the home page's.
async function site(setting: string | null) {
  vi.resetModules();
  vi.stubEnv('JEFF_HUB_API_ORIGIN', 'https://api.example.com');
  for (const [name, value] of Object.entries(PLAN_ENV)) vi.stubEnv(name, value);
  if (setting !== null) {
    const file = fileWith(setting);
    vi.doMock('../app/lib/sdk-languages.server', async (original) => {
      const real = await original<typeof import('../app/lib/sdk-languages.server')>();
      return { ...real, loadSdkLanguages: () => real.loadSdkLanguages(file) };
    });
  }
  const { catalogue, docs } = await import('../app/lib/site.server');
  const home = await import('../app/routes/home');
  const adapter = await import('../app/routes/catalogue-adapter');
  const pages = docs().pages.map((p) => ({ where: `/docs/${p.tab}/${p.id}`, text: p.body }));
  const adapters = catalogue().map((e) => {
    const data = adapter.loader({ params: { id: e.id } } as never);
    return { where: `/catalogue/${e.id}`, text: JSON.stringify(data), block: data.example?.block ?? null };
  });
  const blocks: { where: string; block: ExampleBlock }[] = [
    ...pages.flatMap(({ where, text }) => [...text.matchAll(BLOCK)].map((m) => ({ where, block: JSON.parse(m[1]) as ExampleBlock }))),
    ...adapters.flatMap(({ where, block }) => (block === null ? [] : [{ where, block }])),
  ];
  return { texts: [...pages, ...adapters, { where: '/', text: JSON.stringify(home.loader()) }], blocks };
}

afterEach(() => {
  vi.doUnmock('../app/lib/sdk-languages.server');
  vi.unstubAllEnvs();
  vi.resetModules();
});

describe('the setting', () => {
  it('keeps an off language out of every generated page: no worked example, catalogue adapter page or home page data has its code', async () => {
    // Every language is shown in the real setting now, so turn Java off on a synthetic one to exercise the
    // mechanism (check-build.mjs makes the same check of the built files, against whichever setting is real then).
    // Java's own dedicated SDK page (/docs/sdk/java) is hand-written prose, not generated from this setting, so it
    // keeps its real code regardless -- exactly as it kept the "planned, not built yet" placeholder while off.
    const javaOff = SDK_LANGUAGES.map((l) => entry(l, l !== 'java', l === 'java' ? null : 'pkg')).join('');
    const { texts, blocks } = await site(javaOff);
    expect(blocks.length).toBeGreaterThan(50);
    const off = (['java'] as const).flatMap((l) => SDK_CODE_MARKERS[l]);
    const leaks = texts.filter(({ where }) => where !== '/docs/sdk/java').flatMap(({ where, text }) => off
      .filter((marker) => text.includes(marker)).map((marker) => `${where}: ${marker}`));
    expect(leaks).toEqual([]);
    for (const { where, block } of blocks) {
      expect(block.code.map((c) => c.language), where).toEqual(block.curl === null ? ['python'] : ['python', 'node', 'rust', 'csharp']);
    }
    const home = texts.find((t) => t.where === '/')!.text;
    expect(home).toContain(SDK_CODE_MARKERS.rust[0]);
    expect(home).toContain(SDK_CODE_MARKERS.csharp[0]);
  });

  it('the real setting shows every language (Python, Node, Rust, C# and Java have all passed final review)', async () => {
    const { texts, blocks } = await site(null);
    for (const { where, block } of blocks) {
      const expected = block.curl === null ? ['python'] : [...SDK_LANGUAGES];
      expect(block.code.map((c) => c.language), where).toEqual(expected);
    }
    const home = texts.find((t) => t.where === '/')!.text;
    for (const markers of Object.values(SDK_CODE_MARKERS)) expect(home).toContain(markers[0]);
  });

  it('turned on for every language, shows every language\'s tab on every example (Python only on an offline one)', async () => {
    const { texts, blocks } = await site(ALL_ON);
    for (const { where, block } of blocks) {
      const expected = block.curl === null ? ['python'] : [...SDK_LANGUAGES];
      expect(block.code.map((c) => c.language), where).toEqual(expected);
      const { unmount } = render(<MemoryRouter><Example block={block} /></MemoryRouter>);
      expect(screen.getAllByRole('tab').map((t) => t.textContent), where)
        .toEqual([...expected.map((l) => SDK_LABELS[l as SdkLanguage]), ...(block.curl === null ? [] : ['curl'])]);
      unmount();
    }
    const home = texts.find((t) => t.where === '/')!.text;
    for (const markers of Object.values(SDK_CODE_MARKERS)) expect(home).toContain(markers[0]);
  });
});
