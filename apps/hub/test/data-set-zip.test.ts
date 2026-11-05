// @vitest-environment node
// The data set check of /request-adapter (app/lib/data-set-zip.ts), one rule at a time, on small zips built here:
// what may be in the zip, the columns and keys, the answers, empty rows, the input's length, the minimums, and the
// counts per option.
import { strToU8, zipSync, type Zippable } from 'fflate';
import { describe, expect, it } from 'vitest';
import { csvTemplate, jsonlTemplate, type ImportShape } from '../app/lib/adapter-request-import';
import { checkDataSetZip, DataSetRefused, MAX_PROBLEMS } from '../app/lib/data-set-zip';
import { aForm, TEAMS } from './helpers/adapter-request';

const SHAPE: ImportShape = { answer: aForm().answer, input: aForm().input, context: aForm().context };
const MAX = 4000;

// `n` CSV rows (header first), a third per team, unless `answer` says otherwise.
function csv(n: number, answer: (i: number) => string = (i) => TEAMS[i % 3], start = 0): string {
  const rows = Array.from({ length: n }, (_, i) => `"Support email ${start + i}, about ${answer(start + i)}",Widget,${answer(start + i)}`);
  return ['input,product,answer', ...rows].join('\r\n') + '\r\n';
}
function jsonl(n: number, start = 0): string {
  return Array.from({ length: n }, (_, i) => JSON.stringify({ input: `Email ${start + i}`, context: { product: 'Widget' }, answer: TEAMS[(start + i) % 3] })).join('\n') + '\n';
}

const zipOf = (files: Record<string, string | Uint8Array>) => {
  const entries: Zippable = {};
  for (const [name, content] of Object.entries(files)) entries[name] = typeof content === 'string' ? strToU8(content) : content;
  return new Blob([zipSync(entries)]);
};

async function refusal(promise: Promise<unknown>): Promise<DataSetRefused> {
  const error = await promise.then(() => null, (e: unknown) => e);
  if (!(error instanceof DataSetRefused)) throw new Error(`expected a DataSetRefused, got ${String(error)}`);
  return error;
}
const check = (blob: Blob, name = 'data.zip', shape = SHAPE) => checkDataSetZip(blob, name, shape, MAX);

describe('the data set zip', () => {
  it('counts the rows of every CSV and JSON Lines file, per option', async () => {
    const result = await check(zipOf({ 'data/a.csv': csv(240), 'data/b.jsonl': jsonl(90, 240), 'README.md': '# Our emails' }));
    expect(result.rows).toBe(330);
    expect(result.counts).toEqual([{ key: 'billing', count: 110 }, { key: 'tech', count: 110 }, { key: 'none', count: 110 }]);
    expect(result.files).toEqual([{ name: 'data/a.csv', rows: 240 }, { name: 'data/b.jsonl', rows: 90 }]);
  });

  it('accepts files written from the templates', async () => {
    const header = csvTemplate(SHAPE).split('\r\n')[0];
    const sample = JSON.parse(jsonlTemplate(SHAPE)) as Record<string, unknown>;
    const rows = Array.from({ length: 300 }, (_, i) => JSON.stringify({ ...sample, input: `Email ${i}`, answer: TEAMS[i % 3] })).join('\n');
    const csvRows = Array.from({ length: 3 * 20 }, (_, i) => `Email ${i},Widget,${TEAMS[i % 3]},,no`).join('\n');
    expect((await check(zipOf({ 'a.jsonl': rows, 'b.csv': `${header}\n${csvRows}\n` }))).rows).toBe(360);
  });

  it('skips the files a Mac adds when it compresses a folder (__MACOSX, .DS_Store)', async () => {
    const zip = zipOf({ 'data/a.csv': csv(300), '__MACOSX/data/._a.csv': 'x', 'data/.DS_Store': 'x', '.DS_Store': 'x' });
    expect((await check(zip)).rows).toBe(300);
  });

  it('refuses any file that is not .csv, .jsonl or one README, a zip inside the zip, and paths with ".."', async () => {
    expect((await refusal(check(zipOf({ 'a.csv': csv(300), 'notes.pdf': 'x' })))).problems)
      .toEqual(['The zip holds "notes.pdf", which is not a .csv or .jsonl file or a README; remove it.']);
    expect((await refusal(check(zipOf({ 'a.csv': csv(300), '.hidden.csv': 'x' })))).problems)
      .toEqual(['The zip holds ".hidden.csv", which is not a .csv or .jsonl file or a README; remove it.']);
    expect((await refusal(check(zipOf({ 'a.csv': csv(300), 'inner.zip': zipSync({ 'b.csv': strToU8(csv(1)) }) })))).problems)
      .toEqual(['The zip holds another zip, "inner.zip"; put its CSV or JSON Lines files in the one zip instead.']);
    expect((await refusal(check(zipOf({ '../a.csv': csv(300) })))).problems)
      .toEqual(['The zip holds the path "../a.csv"; paths with ".." (or starting with "/") are not allowed.']);
    expect((await refusal(check(zipOf({ 'a.csv': csv(300), 'README.md': 'a', 'docs/readme.txt': 'b' })))).message)
      .toBe('The zip holds more than one README ("docs/readme.txt" is the second); keep one.');
    expect((await refusal(check(zipOf({ 'README.md': 'only a readme' })))).problems).toEqual(['The zip holds no .csv or .jsonl file.']);
  });

  it('refuses what is not a zip, an empty file, or a zip over 500 MB, before reading it', async () => {
    expect((await refusal(check(new Blob(['input,answer']), 'data.csv'))).message).toBe('Choose a .zip file; "data.csv" is not one.');
    expect((await refusal(check(new Blob(['not a zip at all'])))).message).toBe('"data.zip" is not a zip file.');
    expect((await refusal(check(new Blob([])))).message).toBe('"data.zip" is empty.');
    const huge = { size: 500_000_001, slice: () => new Blob([]) } as unknown as Blob;
    expect((await refusal(check(huge))).message).toBe('"data.zip" is 500.0 MB; a data set may be at most 500.0 MB.');
  });

  it("checks a CSV file's columns against the form", async () => {
    expect((await refusal(check(zipOf({ 'a.csv': 'text,answer\nx,billing\n' })))).problems)
      .toEqual(['a.csv header has no column "input", "product"; the columns are input, product, answer, reason, tricky.']);
    expect((await refusal(check(zipOf({ 'a.csv': 'input,product,answer,label\nx,W,billing,y\n' })))).problems)
      .toEqual(['a.csv header has the column "label", which the form does not have; the columns are input, product, answer, reason, tricky.']);
    expect((await refusal(check(zipOf({ 'a.csv': `${csv(300)}one,two\n` })))).problems).toEqual(['a.csv: CSV line 302 has 2 cells; the header has 3.']);
  });

  it("checks a JSON Lines row's keys against the form", async () => {
    const bad = `${jsonl(299)}{"input": "x", "context": {"product": "W"}, "answer": "tech", "label": "y"}\n`;
    expect((await refusal(check(zipOf({ 'b.jsonl': bad })))).problems).toEqual(['b.jsonl, row 300: it has a field the form does not have: "label".']);
    const noContext = `${jsonl(299)}{"input": "x", "answer": "tech"}\n`;
    expect((await refusal(check(zipOf({ 'b.jsonl': noContext })))).problems).toEqual(['b.jsonl, row 300: the context value "product" is required.']);
    expect((await refusal(check(zipOf({ 'b.jsonl': `${jsonl(10)}not json\n` })))).message).toMatch(/^b\.jsonl: JSON Lines line 11 is not JSON/);
  });

  it('refuses an answer that is not one of the options, naming the file and row', async () => {
    const result = await refusal(check(zipOf({ 'a.csv': csv(300, (i) => (i === 7 ? 'sales' : TEAMS[i % 3])) })));
    expect(result.problems).toEqual(['a.csv, row 8 (line 9): the answer "sales" is not one of the options.']);
  });

  it('refuses an empty row, in CSV and in JSON Lines', async () => {
    const withBlank = csv(300).replace('\r\n"Support email 4', '\r\n\r\n"Support email 4');
    expect((await refusal(check(zipOf({ 'a.csv': withBlank })))).problems).toEqual(['a.csv, row 5 (line 6) is empty.']);
    expect((await refusal(check(zipOf({ 'a.csv': csv(300).replace('\r\n"Support email 4', '\r\n,,\r\n"Support email 4') })))).problems)
      .toEqual(['a.csv, row 5 (line 6) is empty.']);
    expect((await refusal(check(zipOf({ 'b.jsonl': `${jsonl(5)}\n${jsonl(300, 5)}` })))).problems).toEqual(['b.jsonl, row 6 is empty.']);
  });

  it('refuses an input longer than the maximum length', async () => {
    const long = `${jsonl(299)}${JSON.stringify({ input: 'x'.repeat(MAX + 1), context: { product: 'W' }, answer: 'tech' })}\n`;
    expect((await refusal(check(zipOf({ 'b.jsonl': long })))).problems)
      .toEqual(['b.jsonl, row 300: the input has 4,001 characters, more than the maximum length of 4,000.']);
  });

  it('needs at least 300 rows, and 20 for every option', async () => {
    expect((await refusal(check(zipOf({ 'a.csv': csv(299) })))).problems).toEqual(['The data set has 299 rows; it needs at least 300.']);
    const fewTech = await refusal(check(zipOf({ 'a.csv': csv(320, (i) => (i < 19 ? 'tech' : i % 2 === 0 ? 'billing' : 'none')) })));
    expect(fewTech.problems).toEqual(['The option "tech" has 19 rows; every option needs at least 20.']);
  });

  it('gives the first 20 problems and stops reading', async () => {
    const result = await refusal(check(zipOf({ 'a.csv': csv(400, () => 'sales') })));
    expect(result.problems).toHaveLength(MAX_PROBLEMS);
    expect(result.more).toBe(true);
    expect(result.problems[0]).toBe('a.csv, row 1 (line 2): the answer "sales" is not one of the options.');
  });

  it('reads files of options given by the caller, each row with its option list', async () => {
    const shape: ImportShape = { ...SHAPE, answer: { kind: 'caller', options: [], noneFits: 'abstain',
      exampleSets: [[{ key: 'a', definition: 'A' }, { key: 'b', definition: 'B' }], [], []] } };
    const rows = Array.from({ length: 300 }, (_, i) => `Email ${i},W,a: Option A | b: Option B,${i % 2 === 0 ? 'a' : 'b'}`).join('\n');
    const result = await check(zipOf({ 'a.csv': `input,product,options,answer\n${rows}\n` }), 'data.zip', shape);
    expect(result).toMatchObject({ rows: 300, counts: [] });
    const wrong = `input,product,options,answer\n${rows}\nx,W,a: A | b: B,c\n`;
    expect((await refusal(check(zipOf({ 'a.csv': wrong }), 'data.zip', shape))).problems).toEqual(['a.csv, row 301 (line 302): the answer "c" is not one of the options.']);
  });

  it('refuses a file that is not UTF-8 text', async () => {
    const latin1 = new Uint8Array([...strToU8('input,product,answer\n'), 0xe9, ...strToU8(',W,tech\n')]);
    expect((await refusal(check(zipOf({ 'a.csv': latin1 })))).problems).toEqual(['a.csv is not UTF-8 text; save it as UTF-8 and zip it again.']);
  });
});
