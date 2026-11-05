// The examples of an adapter request as files (the /request-adapter page): the CSV and JSON Lines templates, built
// from the form's own fields, and reading pasted or uploaded CSV or JSON Lines into examples. Reading checks only
// the file's shape (its columns, its quoting, one JSON object per line) and names the line of every problem; what
// the values must be is checkAdapterRequest's (adapter-request.ts), run on the whole form afterwards.
//
// The columns, in order: the input (one column "input" for one text, else one column per named input field), one
// column per context field, "options" (for options given by the caller only: "key: definition | key: definition"),
// "answer" (for several options: keys separated by "|"), then the optional "reason" and "tricky" (yes, no or empty).
import type { AdapterRequestForm, Example, OptionDef } from './adapter-request';

export type ImportShape = Pick<AdapterRequestForm, 'answer' | 'input' | 'context'>;

export class ImportRefused extends Error {}

const OPTION_SEPARATOR = ' | ';
const ANSWER_SEPARATOR = '|';

export function columnsOf(shape: ImportShape): string[] {
  const input = shape.input.form === 'text' ? ['input'] : shape.input.fields.map((f) => f.name);
  return [...input, ...shape.context.map((f) => f.name), ...(shape.answer.kind === 'caller' ? ['options'] : []), 'answer', 'reason', 'tricky'];
}

const OPTIONAL_COLUMNS = ['reason', 'tricky'];

// A cell, quoted when it must be (a comma, a quote, a line break, or space at either end).
function cell(text: string): string {
  return /[",\r\n]|^\s|\s$/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

// What a template's sample row says should go in each column.
function sampleRow(shape: ImportShape): Record<string, string> {
  const keys = shape.answer.kind === 'caller'
    ? (shape.answer.exampleSets[0] ?? []).map((o) => o.key)
    : shape.answer.options.map((o) => o.key);
  const first = keys[0] ?? 'option_key';
  const row: Record<string, string> = {};
  if (shape.input.form === 'text') row.input = 'The text the adapter decides on';
  for (const field of shape.input.fields) row[field.name] = `The ${field.name.replaceAll('_', ' ')}`;
  for (const field of shape.context) row[field.name] = field.example;
  if (shape.answer.kind === 'caller') {
    const set: OptionDef[] = shape.answer.exampleSets[0] ?? [{ key: 'option_a', definition: 'When option a is right' }, { key: 'option_b', definition: 'When option b is right' }];
    row.options = set.map((o) => `${o.key}: ${o.definition}`).join(OPTION_SEPARATOR);
  }
  row.answer = shape.answer.kind === 'several' && keys.length > 1 ? `${first}${ANSWER_SEPARATOR}${keys[1]}` : first;
  row.reason = 'Why this answer is right (optional)';
  row.tricky = 'no';
  return row;
}

export function csvTemplate(shape: ImportShape): string {
  const columns = columnsOf(shape);
  const sample = sampleRow(shape);
  return `${columns.map(cell).join(',')}\r\n${columns.map((c) => cell(sample[c])).join(',')}\r\n`;
}

export function jsonlTemplate(shape: ImportShape): string {
  const s = sampleRow(shape);
  const row: Record<string, unknown> = {
    input: shape.input.form === 'text' ? s.input : Object.fromEntries(shape.input.fields.map((f) => [f.name, s[f.name]])),
    context: Object.fromEntries(shape.context.map((f) => [f.name, s[f.name]])),
  };
  if (shape.answer.kind === 'caller') row.options = parseOptions(s.options, 1);
  row.answer = shape.answer.kind === 'several' ? s.answer.split(ANSWER_SEPARATOR) : s.answer;
  row.reason = s.reason;
  row.tricky = false;
  return `${JSON.stringify(row)}\n`;
}

// CSV as RFC 4180 writes it: commas between cells, double quotes around a cell that holds a comma, a quote ("") or
// a line break. Text is given to push() in pieces (a large file is read a piece at a time), and every row is handed
// to `onRow` with the line it starts on as soon as it is complete; end() hands over the last one. A blank line is a
// row of one empty cell.
export class CsvReader {
  private cells: string[] = [];
  private current = '';
  private quoted = false;
  private line = 1;
  private rowLine = 1;
  private atCellStart = true;
  // A carriage return at the end of one piece, whose line feed may begin the next.
  private pendingReturn = false;
  // A quote that closed a quoted cell at the end of one piece: if the next piece begins with a quote, the two were
  // one quote ("") inside the cell instead.
  private closedAtEnd = false;

  constructor(private readonly onRow: (row: { line: number; cells: string[] }) => void) {}

  push(text: string): void {
    for (let i = 0; i < text.length; i += 1) {
      const ch = text[i];
      if (this.pendingReturn) {
        this.pendingReturn = false;
        if (ch === '\n') continue;
      }
      if (this.quoted) {
        if (ch === '"' && i + 1 === text.length) {
          // A quote at the end of a piece: whether it is "" (a quote) or the cell's end depends on the next piece.
          this.quoted = false;
          this.closedAtEnd = true;
          continue;
        }
        if (ch === '"' && text[i + 1] === '"') { this.current += '"'; i += 1; } else if (ch === '"') { this.quoted = false; } else { if (ch === '\n') this.line += 1; this.current += ch; }
        continue;
      }
      if (this.closedAtEnd) {
        this.closedAtEnd = false;
        if (ch === '"') { this.current += '"'; this.quoted = true; continue; }
      }
      if (ch === '"') {
        if (!this.atCellStart) throw new ImportRefused(`CSV line ${this.line}: a quote in the middle of a cell; put the whole cell in quotes and double any quote inside it.`);
        this.quoted = true;
        this.atCellStart = false;
      } else if (ch === ',') {
        this.cells.push(this.current); this.current = ''; this.atCellStart = true;
      } else if (ch === '\n' || ch === '\r') {
        if (ch === '\r') {
          if (i + 1 === text.length) this.pendingReturn = true;
          else if (text[i + 1] === '\n') i += 1;
        }
        this.cells.push(this.current);
        this.onRow({ line: this.rowLine, cells: this.cells });
        this.cells = []; this.current = ''; this.atCellStart = true;
        this.line += 1; this.rowLine = this.line;
      } else {
        this.current += ch; this.atCellStart = false;
      }
    }
  }

  end(): void {
    this.closedAtEnd = false;
    if (this.quoted) throw new ImportRefused(`CSV line ${this.rowLine}: a quoted cell is never closed.`);
    if (this.current !== '' || this.cells.length > 0) { this.cells.push(this.current); this.onRow({ line: this.rowLine, cells: this.cells }); }
    this.cells = []; this.current = '';
  }
}

// A row that is a blank line (one empty cell) or only commas.
export const isEmptyRow = (cells: string[]): boolean => cells.every((cell) => cell.trim() === '');

// The rows of a whole CSV text, blank lines left out.
export function parseCsv(text: string): { line: number; cells: string[] }[] {
  const rows: { line: number; cells: string[] }[] = [];
  const reader = new CsvReader((row) => rows.push(row));
  reader.push(text);
  reader.end();
  return rows.filter((row) => !(row.cells.length === 1 && row.cells[0].trim() === ''));
}

function parseOptions(text: string, line: number): OptionDef[] {
  return text.split(ANSWER_SEPARATOR).map((part) => part.trim()).filter((part) => part !== '').map((part) => {
    const at = part.indexOf(':');
    if (at < 1) throw new ImportRefused(`Line ${line}: each option in "options" must be written "key: definition"; "${part}" is not.`);
    return { key: part.slice(0, at).trim(), definition: part.slice(at + 1).trim() };
  });
}

function trickyOf(text: string, line: number): boolean {
  const t = text.trim().toLowerCase();
  if (t === 'yes') return true;
  if (t === 'no' || t === '') return false;
  throw new ImportRefused(`CSV line ${line}: "tricky" must be yes, no or empty; it is "${text}".`);
}

// The header line's column names, checked against the form's: every required column, none it does not have, none
// twice. `what` names the file in the refusal ("The CSV", "examples.csv").
export function csvHeader(cells: string[], shape: ImportShape, what = 'The CSV'): string[] {
  const names = cells.map((c) => c.trim());
  const expected = columnsOf(shape);
  const missing = expected.filter((c) => !OPTIONAL_COLUMNS.includes(c) && !names.includes(c));
  const unknown = names.filter((c) => !expected.includes(c));
  const twice = names.filter((c, i) => names.indexOf(c) !== i);
  if (missing.length > 0) throw new ImportRefused(`${what} header has no column ${missing.map((c) => `"${c}"`).join(', ')}; the columns are ${expected.join(', ')}.`);
  if (unknown.length > 0) throw new ImportRefused(`${what} header has the column ${unknown.map((c) => `"${c}"`).join(', ')}, which the form does not have; the columns are ${expected.join(', ')}.`);
  if (twice.length > 0) throw new ImportRefused(`${what} header has the column "${twice[0]}" twice.`);
  return names;
}

// One CSV row under its header, as an example.
export function exampleFromCsvRow(names: string[], { line, cells }: { line: number; cells: string[] }, shape: ImportShape): Example {
  if (cells.length !== names.length) throw new ImportRefused(`CSV line ${line} has ${cells.length} cells; the header has ${names.length}.`);
  const get = (name: string) => { const at = names.indexOf(name); return at === -1 ? '' : cells[at]; };
  const example: Example = {
    input: shape.input.form === 'text' ? get('input') : Object.fromEntries(shape.input.fields.map((f) => [f.name, get(f.name)])),
    context: Object.fromEntries(shape.context.map((f) => [f.name, get(f.name)])),
    answer: shape.answer.kind === 'several' ? get('answer').split(ANSWER_SEPARATOR).map((k) => k.trim()).filter((k) => k !== '') : get('answer').trim(),
    reason: get('reason').trim(),
    tricky: trickyOf(get('tricky'), line),
  };
  if (shape.answer.kind === 'caller') example.options = parseOptions(get('options'), line);
  return example;
}

export function examplesFromCsv(text: string, shape: ImportShape): Example[] {
  const rows = parseCsv(text);
  if (rows.length === 0) throw new ImportRefused('The CSV is empty: it needs a header line and one line per example.');
  const [header, ...body] = rows;
  const names = csvHeader(header.cells, shape);
  if (body.length === 0) throw new ImportRefused('The CSV has a header but no examples.');
  return body.map((row) => exampleFromCsvRow(names, row, shape));
}

// One line of JSON Lines as an example: a JSON object whose fields are taken as they are (checkAdapterRequest, or
// the data set's check, says what is wrong with them); an absent context, reason or tricky mark is empty.
export function exampleFromJsonLine(raw: string, line: number): Example {
  let value: unknown;
  try {
    value = JSON.parse(raw);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    throw new ImportRefused(`JSON Lines line ${line} is not JSON: ${error.message}`);
  }
  if (typeof value !== 'object' || value === null || Array.isArray(value)) throw new ImportRefused(`JSON Lines line ${line} must be one JSON object.`);
  return { context: {}, reason: '', tricky: false, ...(value as Record<string, unknown>) } as Example;
}

// One JSON object per line, each with the fields of an example (blank lines are skipped). An object's fields are
// taken as they are; checkAdapterRequest says what is wrong with them.
export function examplesFromJsonl(text: string): Example[] {
  const out: Example[] = [];
  text.split(/\r?\n/).forEach((raw, i) => {
    if (raw.trim() === '') return;
    out.push(exampleFromJsonLine(raw, i + 1));
  });
  if (out.length === 0) throw new ImportRefused('The JSON Lines text has no examples.');
  return out;
}

// Pasted text: JSON Lines when its first non-blank character opens an object, CSV otherwise.
export function examplesFromText(text: string, shape: ImportShape): Example[] {
  return text.trimStart().startsWith('{') ? examplesFromJsonl(text) : examplesFromCsv(text, shape);
}
