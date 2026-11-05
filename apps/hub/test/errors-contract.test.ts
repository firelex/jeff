// Checks the Errors guide's table against the real contract/errors.yaml (parsed from disk, not a fixture), so a
// later change to that file -- a new error, a renamed one, a changed status or a reworded meaning -- makes this
// test fail instead of leaving the page quietly out of date. Moved here from the console (which no longer carries
// its own copy of the docs) when the console's "Documentation" rail item became a link to this site.
import fs from 'node:fs';
import path from 'node:path';
import { describe, expect, it } from 'vitest';
import { parse } from 'yaml';

const REPO = path.join(__dirname, '../../..');
const ERRORS_PAGE = fs.readFileSync(path.join(__dirname, '../content/docs/guides/errors.md'), 'utf8');
const ERRORS_YAML = parse(fs.readFileSync(path.join(REPO, 'contract', 'errors.yaml'), 'utf8')) as
  Record<string, { status: number | null; meaning: string }>;

/** One row of the page's own `| Error | HTTP status | Meaning |` table: {name: {statusCell, meaning}}. */
function tableRows(markdown: string): Record<string, { statusCell: string; meaning: string }> {
  const rows: Record<string, { statusCell: string; meaning: string }> = {};
  for (const line of markdown.split('\n')) {
    const match = /^\| `([A-Za-z]+)` \| ([^|]+) \| ([^|]+) \|$/.exec(line.trim());
    if (!match) continue;
    const [, name, statusCell, meaning] = match;
    rows[name] = { statusCell: statusCell.trim(), meaning: meaning.trim() };
  }
  return rows;
}

describe('the Errors guide against the real contract/errors.yaml', () => {
  it('has at least one row (the table pattern actually matched something)', () => {
    expect(Object.keys(tableRows(ERRORS_PAGE)).length).toBeGreaterThan(0);
  });

  it('lists exactly the errors contract/errors.yaml lists, no more and no fewer', () => {
    expect(new Set(Object.keys(tableRows(ERRORS_PAGE)))).toEqual(new Set(Object.keys(ERRORS_YAML)));
  });

  it('gives each error the same status and the same meaning as contract/errors.yaml', () => {
    const rows = tableRows(ERRORS_PAGE);
    for (const [name, spec] of Object.entries(ERRORS_YAML)) {
      const expectedStatus = spec.status === null ? 'Not sent over HTTP' : String(spec.status);
      expect(rows[name]?.statusCell, `${name}'s status`).toBe(expectedStatus);
      expect(rows[name]?.meaning, `${name}'s meaning`).toBe(spec.meaning);
    }
  });
});
