import fs from 'node:fs';
import path from 'node:path';
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router';
import { describe, expect, it } from 'vitest';
import { Header } from '../app/components/Header';
import { CELL, CURSOR, LETTER_ROWS } from '../app/components/Logo';

const ASSET = path.join(__dirname, '../app/assets/jeff_logo_v4.svg');

// The owner's logo file, read back as grid cells: every plain <rect> is one 12x12 letter cell, the one with
// class="cursor" the cursor block.
function assetCells(): { letters: Set<string>; cursor: { x: number; y: number; size: number }; origin: { x: number; y: number } } {
  const svg = fs.readFileSync(ASSET, 'utf8');
  const letters: { x: number; y: number }[] = [];
  let cursor: { x: number; y: number; size: number } | null = null;
  for (const m of svg.matchAll(/<rect( class="cursor")? x="(\d+)" y="(\d+)" width="(\d+)" height="(\d+)"\/>/g)) {
    const [x, y, w] = [Number(m[2]), Number(m[3]), Number(m[4])];
    if (m[1] !== undefined) cursor = { x, y, size: w };
    else letters.push({ x, y });
  }
  if (cursor === null) throw new Error(`${ASSET} has no cursor rect`);
  const origin = { x: Math.min(...letters.map((c) => c.x)), y: Math.min(...letters.map((c) => c.y)) };
  return { letters: new Set(letters.map((c) => `${(c.x - origin.x) / CELL},${(c.y - origin.y) / CELL}`)), cursor, origin };
}

describe('Logo', () => {
  it('draws exactly the letter cells and cursor of the owner\'s logo file, cropped to the letters', () => {
    const asset = assetCells();
    const drawn = new Set<string>();
    LETTER_ROWS.forEach((line, row) => [...line].forEach((ch, col) => { if (ch === '#') drawn.add(`${col},${row}`); }));
    expect([...drawn].sort()).toEqual([...asset.letters].sort());
    expect(CURSOR).toEqual({
      col: (asset.cursor.x - asset.origin.x) / CELL,
      row: (asset.cursor.y - asset.origin.y) / CELL,
      size: asset.cursor.size / CELL,
    });
  });
});

describe('Header', () => {
  it('renders the logo as an inline SVG named "Jeff", inked in currentColor, with the violet cursor block', () => {
    const { container } = render(<MemoryRouter><Header searchIndex={[]} /></MemoryRouter>);
    const home = screen.getByRole('link', { name: 'Jeff' });
    expect(home.getAttribute('href')).toBe('/');
    const svg = home.querySelector('svg');
    expect(svg).not.toBeNull();
    expect(svg!.getAttribute('aria-label')).toBe('Jeff');
    expect(svg!.querySelector('g')!.getAttribute('fill')).toBe('currentColor');
    expect(svg!.querySelectorAll('g[fill="currentColor"] rect').length).toBe(LETTER_ROWS.join('').split('').filter((c) => c === '#').length);
    // The cursor is drawn from the same cells as the letters (7 by 7), so it snaps to pixels exactly as they do.
    const cursor = svg!.querySelectorAll('g.jeff-logo-cursor');
    expect(cursor).toHaveLength(1);
    expect(cursor[0].querySelectorAll('rect')).toHaveLength(CURSOR.size * CURSOR.size);
    // The old bearded-face image logo is gone.
    expect(container.querySelector('header img')).toBeNull();
  });

  it('has no Console link: a customer of the public API never uses the console', () => {
    render(<MemoryRouter><Header searchIndex={[]} /></MemoryRouter>);
    expect(screen.queryByRole('link', { name: 'Console' })).toBeNull();
  });
});
