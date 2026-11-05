import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { checkLinks, loadDocs } from '../app/lib/docs.server';
import { SNIPPET_FENCE, type SnippetBlock } from '../app/lib/snippet-block';
import { PLAN } from './helpers/plan';
import { headingId } from '../app/lib/slug';

const root = path.join(__dirname, 'fixtures/docs');

describe('loadDocs', () => {
  it('reads pages in sidebar order with titles, groups and headings', () => {
    const { pages, sidebar } = loadDocs(root, 'https://api.example.com', ['python', 'node'], PLAN, new Map());
    expect(pages.map((p) => p.id)).toEqual(['a', 'b']);
    expect(pages[0]).toMatchObject({ tab: 'guides', title: 'Page A', group: 'Start' });
    expect(pages[0].headings).toEqual([{ id: 'section-one', text: 'One', depth: 2 }]);
    expect(sidebar.guides[0].pages).toEqual([{ id: 'a', title: 'Page A' }, { id: 'b', title: 'Page B' }]);
  });
  it('replaces {{origin}}', () => {
    const { pages } = loadDocs(root, 'https://api.example.com', ['python', 'node'], PLAN, new Map());
    expect(pages[0].body).toContain('https://api.example.com/v1/decide');
    expect(pages[0].body).not.toContain('{{origin}}');
  });
  it('replaces {{price}} and {{allowance}} with the plan the settings give', () => {
    const { pages } = loadDocs(root, 'https://api.example.com', ['python', 'node'], { price: '$2.50', allowance: '7 million' }, new Map());
    expect(pages[0].body).toContain('The API is $2.50 a month for 7 million input tokens.');
  });
  it('replaces a snippet marker with the file as a fenced block, without its "# offline" line', () => {
    const { pages } = loadDocs(root, 'https://api.example.com', ['python', 'node'], PLAN, new Map());
    expect(pages[1].body).toContain('```python\nprint("https://api.example.com")\n```');
    expect(pages[1].body).not.toContain('# offline');
    expect(pages[1].body).not.toContain('<!-- snippet');
  });
  // A tree with one snippet group, `files` (name -> code) in its snippets folder, and a page b showing it.
  function groupTree(files: Record<string, string>): string {
    const tree = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-docs-'));
    fs.cpSync(path.join(__dirname, 'fixtures'), tree, { recursive: true });
    for (const [name, code] of Object.entries(files)) fs.writeFileSync(path.join(tree, 'snippets/sdk', name), code);
    fs.writeFileSync(path.join(tree, 'docs/guides/b.md'), '---\ntitle: Page B\n---\n<!-- snippet: sdk/group-1 -->\n');
    return tree;
  }
  const EXTENSIONS = ['py', 'mjs', 'rs', 'cs', 'java'];
  const all = (suffix: string) => Object.fromEntries(EXTENSIONS.map((ext) => [`group-1${suffix}.${ext}`, `// offline\n${ext}${suffix} {{origin}}\n`]));

  it('replaces a group marker with a snippet block in the languages shown, both forms, without the markers', () => {
    const tree = groupTree({ ...all(''), ...all('.local') });
    try {
      const { pages } = loadDocs(path.join(tree, 'docs'), 'https://api.example.com', ['python', 'node', 'java'], PLAN, new Map());
      const match = new RegExp(`\`\`\`${SNIPPET_FENCE}\\n(.*)\\n\`\`\``).exec(pages[1].body);
      expect(match).not.toBeNull();
      const block = JSON.parse(match![1]) as SnippetBlock;
      expect(block.id).toBe('sdk/group-1');
      expect(block.code).toEqual([{ language: 'python', code: 'py https://api.example.com' }, { language: 'node', code: 'mjs https://api.example.com' },
        { language: 'java', code: 'java https://api.example.com' }]);
      expect(block.local?.map((c) => c.code)).toEqual(['py.local https://api.example.com', 'mjs.local https://api.example.com', 'java.local https://api.example.com']);
    } finally {
      fs.rmSync(tree, { recursive: true, force: true });
    }
  });
  it('shows a group with only its local form with no API code', () => {
    const tree = groupTree(all('.local'));
    try {
      const { pages } = loadDocs(path.join(tree, 'docs'), 'https://api.example.com', ['python', 'node'], PLAN, new Map());
      const block = JSON.parse(new RegExp(`\`\`\`${SNIPPET_FENCE}\\n(.*)\\n\`\`\``).exec(pages[1].body)![1]) as SnippetBlock;
      expect(block.code).toBeNull();
      expect(block.local?.map((c) => c.language)).toEqual(['python', 'node']);
    } finally {
      fs.rmSync(tree, { recursive: true, force: true });
    }
  });
  it('fails naming the files a group lacks, even in a language that is not shown', () => {
    const files = all('');
    delete files['group-1.rs'];
    const tree = groupTree(files);
    try {
      expect(() => loadDocs(path.join(tree, 'docs'), 'https://api.example.com', ['python', 'node'], PLAN, new Map())).toThrow('snippet sdk/group-1 has no group-1.rs');
    } finally {
      fs.rmSync(tree, { recursive: true, force: true });
    }
  });
  it('fails naming a group that has no files at all', () => {
    const tree = groupTree({});
    try {
      expect(() => loadDocs(path.join(tree, 'docs'), 'https://api.example.com', ['python', 'node'], PLAN, new Map())).toThrow('no snippet files for sdk/group-1');
    } finally {
      fs.rmSync(tree, { recursive: true, force: true });
    }
  });
  it('fails naming a snippet file that does not exist', () => {
    const tree = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-docs-'));
    fs.cpSync(path.join(__dirname, 'fixtures'), tree, { recursive: true });
    try {
      fs.writeFileSync(path.join(tree, 'docs/guides/b.md'), '---\ntitle: Page B\n---\n<!-- snippet: sdk/missing-1.py -->\n');
      expect(() => loadDocs(path.join(tree, 'docs'), 'https://api.example.com', ['python', 'node'], PLAN, new Map())).toThrow('sdk/missing-1.py');
    } finally {
      fs.rmSync(tree, { recursive: true, force: true });
    }
  });
});

describe('checkLinks', () => {
  const page = (id: string, body: string, headings: string[] = []) => ({
    tab: 'guides' as const, id, title: id, group: 'g', body,
    headings: headings.map((text) => ({ id: headingId(text), text, depth: 2 as const })), benchmarks: [],
  });
  it('fails on a link to a missing page', () => {
    expect(() => checkLinks([page('a', '[x](/docs/guides/zzz)')], new Set())).toThrow('/docs/guides/zzz');
  });
  it('fails on a link to a missing heading', () => {
    expect(() => checkLinks([page('a', '[x](/docs/guides/b#section-gone)'), page('b', '', ['Here'])], new Set()))
      .toThrow('#section-gone');
  });
  it('accepts paths given by other parts of the site', () => {
    expect(() => checkLinks([page('a', '[x](/catalogue/jeff.safety.guard)')], new Set(['/catalogue/jeff.safety.guard'])))
      .not.toThrow();
  });
  it('fails on a console-style link', () => {
    expect(() => checkLinks([page('a', '[x](#docs/state)')], new Set())).toThrow('#docs/state');
  });
});

it('headingId matches the console', () => {
  expect(headingId('Functions and /v1/decide')).toBe('section-functions-and-v1-decide');
});
