// How each catalogue adapter was measured (content/adapter-numbers.yaml), as its page's "How it was measured"
// section shows it: its test figures, led by a public-benchmark or real-document set where it has one; per
// function, its precision when confident, coverage and threshold from the manifest's calibration; and the caveats of
// its data notes. Every figure is copied from the owner-approved table of evaluation numbers; nothing here computes
// one. A listed adapter with no entry, an entry for an adapter the catalogue does not list, a calibration whose
// functions differ from the adapter's, or a malformed entry is an error naming it.
import fs from 'node:fs';
import { parse } from 'yaml';
import type { CatalogueEntry } from './catalogue.server';
import { privateMarkersIn } from './private-markers.js';

// What a test set is. public: a public benchmark; real: held-out real documents; generator: an in-house test from
// the same generator as the training data; mixed: several kinds of source; unknown: not recorded.
export const TEST_KINDS = ['public', 'real', 'generator', 'mixed', 'unknown'] as const;
export type TestKind = (typeof TEST_KINDS)[number];

export interface TestFigure { accuracy: number; rows: number | null; kind: TestKind; set: string }
export interface Confident { precision: number; coverage: number; threshold: number; rows: number }

export interface AdapterNumbers {
  base: string;
  date: string | null;
  // Empty only when noAccuracy says why.
  tests: TestFigure[];
  noAccuracy: string | null;
  // By function key; null only when noCalibration says why.
  confident: Record<string, Confident> | null;
  noCalibration: string | null;
  caveats: string[];
}

const ENTRY_KEYS = new Set(['base', 'date', 'tests', 'no_accuracy', 'confident', 'no_calibration', 'caveats']);

function isMap(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value);
}

function isText(value: unknown): value is string {
  return typeof value === 'string' && value.trim() !== '';
}

const isShare = (v: unknown): v is number => typeof v === 'number' && v >= 0 && v <= 1;
const isCount = (v: unknown): v is number => typeof v === 'number' && Number.isInteger(v) && v > 0;

function testOf(where: string, raw: unknown, problems: string[]): TestFigure | null {
  if (!isMap(raw) || Object.keys(raw).sort().join(',') !== 'accuracy,kind,rows,set') {
    problems.push(`${where}: must have exactly accuracy, rows, kind and set`);
    return null;
  }
  const { accuracy, rows, kind, set } = raw;
  if (typeof accuracy !== 'number' || accuracy < 0 || accuracy > 100) problems.push(`${where}: accuracy must be a percentage`);
  if (rows !== null && !isCount(rows)) problems.push(`${where}: rows must be a positive whole number or null`);
  if (!TEST_KINDS.includes(kind as TestKind)) problems.push(`${where}: kind must be one of ${TEST_KINDS.join(', ')}`);
  if (!isText(set)) problems.push(`${where}: set must be a non-empty string`);
  return { accuracy: accuracy as number, rows: rows as number | null, kind: kind as TestKind, set: set as string };
}

function entryOf(id: string, raw: unknown, entry: CatalogueEntry, problems: string[]): AdapterNumbers | null {
  if (!isMap(raw)) { problems.push(`${id}: not a map`); return null; }
  for (const key of Object.keys(raw)) if (!ENTRY_KEYS.has(key)) problems.push(`${id}: unknown field ${key}`);
  if (!isText(raw.base)) problems.push(`${id}: no base`);
  if (raw.date !== null && !(typeof raw.date === 'string' && /^\d{4}-\d{2}-\d{2}$/.test(raw.date))) {
    problems.push(`${id}: date must be YYYY-MM-DD or null (not recorded)`);
  }

  const hasTests = raw.tests !== undefined;
  if (hasTests === (raw.no_accuracy !== undefined)) problems.push(`${id}: needs exactly one of tests and no_accuracy`);
  if (raw.no_accuracy !== undefined && !isText(raw.no_accuracy)) problems.push(`${id}: no_accuracy must say why`);
  const tests: TestFigure[] = [];
  if (hasTests) {
    if (!Array.isArray(raw.tests) || raw.tests.length === 0) problems.push(`${id}: tests must be a non-empty list`);
    else raw.tests.forEach((t, i) => { const f = testOf(`${id}: tests[${i}]`, t, problems); if (f !== null) tests.push(f); });
    // The owner's layout: a public-benchmark or real-document figure, where there is one, leads.
    const lead = tests.findIndex((t) => t.kind === 'public' || t.kind === 'real');
    if (lead > 0) problems.push(`${id}: a public or real test set must come first (tests[${lead}] is one)`);
  }

  const hasConfident = raw.confident !== undefined;
  if (hasConfident === (raw.no_calibration !== undefined)) problems.push(`${id}: needs exactly one of confident and no_calibration`);
  if (raw.no_calibration !== undefined && !isText(raw.no_calibration)) problems.push(`${id}: no_calibration must say why`);
  let confident: Record<string, Confident> | null = null;
  if (hasConfident) {
    if (!isMap(raw.confident)) problems.push(`${id}: confident must be a map of function key to figures`);
    else {
      const keys = entry.functions.map((f) => f.key);
      const named = Object.keys(raw.confident);
      if ([...named].sort().join(',') !== [...keys].sort().join(',')) {
        problems.push(`${id}: confident names functions ${named.join(', ')}, the adapter has ${keys.join(', ')}`);
      }
      confident = {};
      for (const [key, c] of Object.entries(raw.confident)) {
        if (!isMap(c) || Object.keys(c).sort().join(',') !== 'coverage,precision,rows,threshold'
          || !isShare(c.precision) || !isShare(c.coverage) || !isShare(c.threshold) || !isCount(c.rows)) {
          problems.push(`${id}: confident.${key} must have precision, coverage and threshold (0 to 1) and rows`);
          continue;
        }
        confident[key] = { precision: c.precision, coverage: c.coverage, threshold: c.threshold, rows: c.rows };
      }
    }
  }

  const caveats = raw.caveats ?? [];
  if (!Array.isArray(caveats) || !caveats.every(isText)) problems.push(`${id}: caveats must be a list of sentences`);

  const text = JSON.stringify(raw);
  for (const marker of privateMarkersIn(text)) problems.push(`${id}: contains the private name "${marker}"`);
  if (/jevbench-hard/i.test(text)) problems.push(`${id}: names jevbench-hard, which the site never shows`);

  return {
    base: raw.base as string,
    date: raw.date as string | null,
    tests,
    noAccuracy: (raw.no_accuracy as string | undefined) ?? null,
    confident,
    noCalibration: (raw.no_calibration as string | undefined) ?? null,
    caveats: caveats as string[],
  };
}

export function loadAdapterNumbers(file: string, entries: CatalogueEntry[]): Map<string, AdapterNumbers> {
  const data: unknown = parse(fs.readFileSync(file, 'utf8'));
  if (!isMap(data)) throw new Error(`${file}: not a map of adapter id to numbers`);
  const byId = new Map(entries.map((e) => [e.id, e]));
  const problems: string[] = [];
  const out = new Map<string, AdapterNumbers>();
  for (const [id, raw] of Object.entries(data)) {
    const entry = byId.get(id);
    if (entry === undefined) { problems.push(`${id}: not an adapter the catalogue lists`); continue; }
    const numbers = entryOf(id, raw, entry, problems);
    if (numbers !== null) out.set(id, numbers);
  }
  for (const e of entries) if (!(e.id in data)) problems.push(`${e.id}: a listed adapter with no numbers`);
  if (problems.length > 0) throw new Error(`${file}: invalid adapter numbers:\n${problems.join('\n')}`);
  return out;
}
