// The benchmark sets' descriptions (content/benchmarks.yaml): the loader, the docs pages that link them, and the
// dialog a set's name opens (components/docs/BenchmarkTerm.tsx).
import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';
import { Markdown } from '../app/components/docs/Markdown';
import { loadBenchmarks } from '../app/lib/benchmarks.server';
import { localAdapters } from '../app/lib/catalogue-call.server';
import { loadDocs } from '../app/lib/docs.server';
import { catalogue } from '../app/lib/site.server';
import { PLAN } from './helpers/plan';

const FILE = path.join(__dirname, '../content/benchmarks.yaml');
const benchmarks = loadBenchmarks(FILE);

function tree(files: Record<string, string>): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hub-benchmarks-'));
  fs.cpSync(path.join(__dirname, 'fixtures'), dir, { recursive: true });
  for (const [name, text] of Object.entries(files)) fs.writeFileSync(path.join(dir, name), text);
  return dir;
}

describe('content/benchmarks.yaml', () => {
  // The swarm guide reported the v1.4 swarm's sets until 4 November 2026, when the router they measured left the
  // public site; it now gives the open router's own routing accuracy, and no page links a set. Any page that links one
  // again must give the rows the file gives.
  it('describes each set a docs page reports, with the rows its tables give', () => {
    const dir = path.join(__dirname, '../content/docs');
    const pages = fs.readdirSync(dir, { recursive: true, encoding: 'utf8' }).filter((name) => name.endsWith('.md'));
    for (const page of pages) {
      const text = fs.readFileSync(path.join(dir, page), 'utf8');
      for (const [, name, id, count] of text.matchAll(/^\| \[([^\]]+)\]\(#benchmark-([^)]+)\)[^|]*\| ([\d,]+) \|/gm)) {
        const benchmark = benchmarks.get(id);
        expect(benchmark, `${page}: ${id}`).toBeDefined();
        expect(benchmark!.name).toBe(name);
        expect(benchmark!.rows).toBe(Number(count.replaceAll(',', '')));
      }
    }
    const swarm = fs.readFileSync(path.join(dir, 'guides/the-swarm.md'), 'utf8');
    expect(swarm).not.toContain('#benchmark-');
  });
  it('no longer describes jevbench-hard, which is not reported', () => {
    expect([...benchmarks.keys()].some((id) => id.includes('jevbench'))).toBe(false);
  });
  it('refuses an entry with an unknown field or a missing description', () => {
    const dir = tree({ 'bad.yaml': '- id: x\n  name: x\n  rows: 1\n  what: a\n  measures: b\n  extra: c\n' });
    try {
      expect(() => loadBenchmarks(path.join(dir, 'bad.yaml'))).toThrow('unknown fields extra');
      fs.writeFileSync(path.join(dir, 'bad.yaml'), '- id: x\n  name: x\n  rows: 1\n  what: a\n');
      expect(() => loadBenchmarks(path.join(dir, 'bad.yaml'))).toThrow('measures must be a non-empty string');
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('the docs loader', () => {
  it('gives a page the sets it links, once each, in order', () => {
    const dir = tree({});
    try {
      fs.writeFileSync(path.join(dir, 'benchmarks.yaml'), fs.readFileSync(FILE, 'utf8'));
      fs.writeFileSync(path.join(dir, 'docs/guides/b.md'),
        '---\ntitle: Page B\n---\n| Set |\n| - |\n| [voice](#benchmark-voice) |\n| [panel](#benchmark-panel) |\n| [voice](#benchmark-voice) |\n');
      const { pages } = loadDocs(path.join(dir, 'docs'), 'https://api.example.com', ['python'], PLAN, new Map());
      expect(pages.find((p) => p.id === 'b')!.benchmarks.map((b) => b.id)).toEqual(['voice', 'panel']);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
    const real = loadDocs(path.join(__dirname, '../content/docs'), 'https://api.example.com', ['python'], PLAN, localAdapters(catalogue())).pages;
    expect(real.find((p) => p.id === 'quickstart')!.benchmarks).toEqual([]);
  });
  it('fails naming the page and a set the benchmarks file does not describe', () => {
    const dir = tree({});
    try {
      fs.writeFileSync(path.join(dir, 'benchmarks.yaml'), fs.readFileSync(FILE, 'utf8'));
      fs.writeFileSync(path.join(dir, 'docs/guides/b.md'), '---\ntitle: Page B\n---\n| Set |\n| - |\n| [nope](#benchmark-nope) |\n');
      expect(() => loadDocs(path.join(dir, 'docs'), 'https://api.example.com', ['python'], PLAN, new Map())).toThrow(/b\.md: links benchmark nope/);
    } finally {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });
});

describe('a set name in a page', () => {
  const body = 'Some text.\n\n| Set | Rows |\n| - | - |\n| [panel](#benchmark-panel) | 4,599 |\n';
  const renderPage = () => render(<MemoryRouter><Markdown body={body} benchmarks={[benchmarks.get('panel')!]} /></MemoryRouter>);

  it('is a button, not a link, that opens a dialog describing the set', () => {
    renderPage();
    expect(screen.queryByRole('link', { name: 'panel' })).toBeNull();
    const button = screen.getByRole('button', { name: 'panel' });
    expect(button.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(button);
    const dialog = screen.getByRole('dialog', { name: 'panel' });
    expect(dialog.getAttribute('aria-modal')).toBe('true');
    expect(dialog.textContent).toContain('4,599 rows');
    expect(dialog.textContent).toContain(benchmarks.get('panel')!.what);
    expect(dialog.textContent).toContain(benchmarks.get('panel')!.measures);
    expect(button.getAttribute('aria-expanded')).toBe('true');
  });
  it('moves focus into the dialog, keeps Tab there, and gives focus back on Escape', () => {
    renderPage();
    const button = screen.getByRole('button', { name: 'panel' });
    button.focus();
    fireEvent.click(button);
    const close = screen.getByRole('button', { name: 'Close' });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Tab' });
    expect(document.activeElement).toBe(close);
    fireEvent.keyDown(window, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
    expect(document.activeElement).toBe(button);
  });
  it('closes from its Close button and from a click outside it', () => {
    renderPage();
    const button = screen.getByRole('button', { name: 'panel' });
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('dialog').parentElement!);
    expect(screen.queryByRole('dialog')).toBeNull();
    fireEvent.click(button);
    fireEvent.click(screen.getByRole('dialog'));
    expect(screen.getByRole('dialog')).toBeTruthy();
  });
  it('is an error on a page given no benchmarks', () => {
    expect(() => render(<MemoryRouter><Markdown body={body} /></MemoryRouter>)).toThrow('benchmark panel');
  });
});
