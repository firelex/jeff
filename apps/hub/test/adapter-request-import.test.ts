// @vitest-environment node
// The examples as files (app/lib/adapter-request-import.ts): the templates follow the form's own fields, and pasted
// or uploaded CSV and JSON Lines become examples the rules then check; a file of the wrong shape is refused, naming
// the line or the column.
import { describe, expect, it } from 'vitest';
import { checkAdapterRequest } from '../app/lib/adapter-request';
import {
  columnsOf, csvTemplate, examplesFromCsv, examplesFromJsonl, examplesFromText, ImportRefused, jsonlTemplate, parseCsv, type ImportShape,
} from '../app/lib/adapter-request-import';
import { aForm, aFormWith } from './helpers/adapter-request';

const shape: ImportShape = aForm();

describe('the columns and the templates', () => {
  it('one text: input, then the context fields, the answer, the reason and the tricky mark', () => {
    expect(columnsOf(shape)).toEqual(['input', 'product', 'answer', 'reason', 'tricky']);
    expect(csvTemplate(shape)).toBe('input,product,answer,reason,tricky\r\nThe text the adapter decides on,Widget,billing,Why this answer is right (optional),no\r\n');
  });

  it('named fields and options given by the caller have their own columns', () => {
    const form = aFormWith((f) => {
      f.input = { form: 'fields', fields: [{ name: 'subject', description: 's' }, { name: 'body', description: 'b' }], typicalLength: 1, maxLength: 9, languages: 'en' };
      f.answer = { kind: 'caller', options: [], noneFits: 'abstain', exampleSets: [[{ key: 'a', definition: 'When a' }, { key: 'b', definition: 'When b' }], [], []] };
    });
    expect(columnsOf(form)).toEqual(['subject', 'body', 'product', 'options', 'answer', 'reason', 'tricky']);
    expect(csvTemplate(form).split('\r\n')[1]).toBe('The subject,The body,Widget,a: When a | b: When b,a,Why this answer is right (optional),no');
    expect(JSON.parse(jsonlTemplate(form))).toEqual({ input: { subject: 'The subject', body: 'The body' }, context: { product: 'Widget' },
      options: [{ key: 'a', definition: 'When a' }, { key: 'b', definition: 'When b' }], answer: 'a', reason: 'Why this answer is right (optional)', tricky: false });
  });

  it('a template read back is the one example it shows', () => {
    expect(examplesFromCsv(csvTemplate(shape), shape)).toEqual(examplesFromJsonl(jsonlTemplate(shape)));
  });
});

describe('CSV', () => {
  it('reads quoted cells with commas, quotes and line breaks', () => {
    expect(parseCsv('a,b\r\n"one, two","say ""hi""\nthere"\n')).toEqual([
      { line: 1, cells: ['a', 'b'] }, { line: 2, cells: ['one, two', 'say "hi"\nthere'] }]);
  });

  it('becomes examples, the several kind\'s answers split on |', () => {
    const csv = 'input,product,answer,tricky\nMy card was charged twice,Widget,billing,yes\nIt crashes,Widget,tech,\n';
    expect(examplesFromCsv(csv, shape)).toEqual([
      { input: 'My card was charged twice', context: { product: 'Widget' }, answer: 'billing', reason: '', tricky: true },
      { input: 'It crashes', context: { product: 'Widget' }, answer: 'tech', reason: '', tricky: false },
    ]);
    const several = aFormWith((f) => { f.answer.kind = 'several'; });
    expect(examplesFromCsv('input,product,answer\nBoth,W,billing|tech\n', several)[0].answer).toEqual(['billing', 'tech']);
  });

  it.each([
    ['a missing column', 'input,answer\nx,billing\n', 'The CSV header has no column "product"; the columns are input, product, answer, reason, tricky.'],
    ['an unknown column', 'input,product,answer,label\nx,W,billing,y\n', 'The CSV header has the column "label", which the form does not have; the columns are input, product, answer, reason, tricky.'],
    ['a short line', 'input,product,answer\nx,W\n', 'CSV line 2 has 2 cells; the header has 3.'],
    ['an unclosed quote', 'input,product,answer\n"x,W,billing\n', 'CSV line 2: a quoted cell is never closed.'],
    ['a stray quote', 'input,product,answer\nx"y,W,billing\n', 'CSV line 2: a quote in the middle of a cell; put the whole cell in quotes and double any quote inside it.'],
    ['a bad tricky mark', 'input,product,answer,tricky\nx,W,billing,maybe\n', 'CSV line 2: "tricky" must be yes, no or empty; it is "maybe".'],
    ['no examples', 'input,product,answer\n', 'The CSV has a header but no examples.'],
  ])('refuses %s', (_name, csv, message) => {
    expect(() => examplesFromCsv(csv, shape)).toThrow(new ImportRefused(message));
  });
});

describe('JSON Lines', () => {
  it('becomes examples, skipping blank lines', () => {
    const text = '{"input": "It crashes", "context": {"product": "W"}, "answer": "tech"}\n\n{"input": "Refund", "context": {"product": "W"}, "answer": "billing", "tricky": true}\n';
    expect(examplesFromJsonl(text)).toEqual([
      { input: 'It crashes', context: { product: 'W' }, answer: 'tech', reason: '', tricky: false },
      { input: 'Refund', context: { product: 'W' }, answer: 'billing', reason: '', tricky: true },
    ]);
  });

  it('refuses a line that is not one JSON object, naming it', () => {
    expect(() => examplesFromJsonl('{"input": "x"}\n{oops\n')).toThrow(/^JSON Lines line 2 is not JSON/);
    expect(() => examplesFromJsonl('[1]\n')).toThrow(new ImportRefused('JSON Lines line 1 must be one JSON object.'));
  });

  it('leaves the values to the rules, which name the example', () => {
    const form = aFormWith((f) => { f.examples = examplesFromJsonl(f.examples.map((e, i) => JSON.stringify(i === 4 ? { ...e, answer: 'sales' } : e)).join('\n')); });
    const checked = checkAdapterRequest(form);
    expect(checked.ok ? [] : checked.problems.map((p) => p.message)).toEqual(['Example 5: the answer "sales" is not one of the options.']);
  });
});

it('pasted text is JSON Lines when it starts with {, CSV otherwise', () => {
  expect(examplesFromText('  {"input": "x", "context": {"product": "W"}, "answer": "tech"}', shape)[0].answer).toBe('tech');
  expect(examplesFromText('input,product,answer\nx,W,tech', shape)[0].answer).toBe('tech');
});
