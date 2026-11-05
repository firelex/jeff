// The data set of an adapter request ("I have a data set", section 6 of /request-adapter), checked in the browser
// before anything is sent: one .zip of CSV and JSON Lines files, one labelled row per case, read a piece at a time
// (a zip may be 500 MB) and never kept whole in memory. Every file is checked against the form:
// - only .csv and .jsonl files and at most one README (.md or .txt) are in the zip: no other file, no zip inside it,
//   no path with "..";
// - a CSV file's header has the form's columns (adapter-request-import.ts: the same columns as the templates); a
//   JSON Lines row has the form's keys;
// - no row is empty; every row's input, context values and answer meet the form's rules (adapter-request.ts:
//   checkExample, the rule a typed example meets: the answer is one of the options, the input within the maximum
//   length);
// - in all, at least 300 rows, and at least 20 for every option of a fixed kind.
// A refusal names the file and the row in plain words; the first 20 problems are given, and reading stops there.
import { Unzip, UnzipInflate, type UnzipFile } from 'fflate';
import { checkExample, LIMITS, megabytes, type Example, type ExampleRules } from './adapter-request';
import { CsvReader, csvHeader, exampleFromCsvRow, exampleFromJsonLine, ImportRefused, isEmptyRow, type ImportShape } from './adapter-request-import';

export const MAX_PROBLEMS = 20;

export class DataSetRefused extends Error {
  // `more` is true when reading stopped at MAX_PROBLEMS, so there may be more problems than these.
  constructor(readonly problems: string[], readonly more: boolean) {
    super(problems[0]);
  }
}

export interface DataSetCheck {
  rows: number;
  // Rows per option of a fixed kind, in the options' order (a row of "several options" counts for each of its
  // answers); empty for options given by the caller.
  counts: { key: string; count: number }[];
  files: { name: string; rows: number }[];
}

// The rules a row meets, from sections 2 to 5 once they are right: the shape the files follow, and the maximum length.
export function rowRulesOf(shape: ImportShape, maxLength: number): ExampleRules {
  return {
    kind: shape.answer.kind, noneFits: shape.answer.noneFits, form: shape.input.form, inputFields: shape.input.fields,
    context: shape.context, maxLength, fixed: shape.answer.kind === 'caller' ? null : shape.answer.options,
  };
}

const ZIP_SIGNATURE = [0x50, 0x4b, 0x03, 0x04];
const README = /^readme\.(md|txt)$/i;

// Stops reading: enough problems were found, or one that ends the check.
class Stop extends Error {}

export async function checkDataSetZip(file: Blob, fileName: string, shape: ImportShape, maxLength: number,
  onProgress: (read: number, total: number) => void = () => {}): Promise<DataSetCheck> {
  if (!/\.zip$/i.test(fileName)) throw new DataSetRefused([`Choose a .zip file; "${fileName}" is not one.`], false);
  if (file.size === 0) throw new DataSetRefused([`"${fileName}" is empty.`], false);
  if (file.size > LIMITS.dataSetBytesMax) {
    throw new DataSetRefused([`"${fileName}" is ${megabytes(file.size)}; a data set may be at most ${megabytes(LIMITS.dataSetBytesMax)}.`], false);
  }
  const head = new Uint8Array(await file.slice(0, 4).arrayBuffer());
  if (!ZIP_SIGNATURE.every((byte, i) => head[i] === byte)) throw new DataSetRefused([`"${fileName}" is not a zip file.`], false);

  const rules = rowRulesOf(shape, maxLength);
  const problems: string[] = [];
  let stopped = false;
  const problem = (message: string) => {
    problems.push(message);
    if (problems.length >= MAX_PROBLEMS) throw new Stop();
  };
  const counts = new Map(rules.fixed === null ? [] : rules.fixed.map((o) => [o.key, 0]));
  const files: DataSetCheck['files'] = [];
  let readmes = 0;

  // A row that meets every rule counts; one that does not adds its problems.
  const take = (example: Example, where: string, file: { rows: number }) => {
    let ok = true;
    checkExample(example, where, '', rules, (_field, message) => { ok = false; problem(message); });
    if (!ok) return;
    file.rows += 1;
    const keys = Array.isArray(example.answer) ? new Set(example.answer) : [example.answer];
    for (const key of keys) {
      const now = counts.get(key);
      if (now !== undefined) counts.set(key, now + 1);
    }
  };

  // The reader of one CSV or JSON Lines file: text in, rows checked as they complete.
  const readerFor = (name: string, csv: boolean) => {
    const entry = { name, rows: 0 };
    files.push(entry);
    let seen = 0;
    if (csv) {
      let header: string[] | null = null;
      let broken = false;
      const reader = new CsvReader((row) => {
        if (broken) return;
        if (header === null) {
          if (isEmptyRow(row.cells)) { broken = true; problem(`${name}: the first line must be the header with the column names.`); return; }
          try {
            header = csvHeader(row.cells, shape, name);
          } catch (error) {
            if (!(error instanceof ImportRefused)) throw error;
            broken = true;
            problem(error.message);
          }
          return;
        }
        seen += 1;
        const where = `${name}, row ${seen} (line ${row.line})`;
        if (isEmptyRow(row.cells)) { problem(`${where} is empty.`); return; }
        let example: Example;
        try {
          example = exampleFromCsvRow(header, row, shape);
        } catch (error) {
          if (!(error instanceof ImportRefused)) throw error;
          problem(`${name}: ${error.message}`);
          return;
        }
        take(example, where, entry);
      });
      return {
        push: (text: string) => { if (!broken) reader.push(text); },
        end: () => {
          if (broken) return;
          try {
            reader.end();
          } catch (error) {
            if (!(error instanceof ImportRefused)) throw error;
            problem(`${name}: ${error.message}`);
            return;
          }
          if (header === null) problem(`${name} is empty: it needs a header line and one line per row.`);
          else if (seen === 0) problem(`${name} has a header but no rows.`);
        },
      };
    }
    let rest = '';
    const line = (raw: string) => {
      seen += 1;
      const where = `${name}, row ${seen}`;
      if (raw.trim() === '') { problem(`${where} is empty.`); return; }
      let example: Example;
      try {
        example = exampleFromJsonLine(raw, seen);
      } catch (error) {
        if (!(error instanceof ImportRefused)) throw error;
        problem(`${name}: ${error.message}`);
        return;
      }
      take(example, where, entry);
    };
    return {
      push: (text: string) => {
        const lines = (rest + text).split('\n');
        rest = lines.pop()!;
        for (const raw of lines) line(raw.endsWith('\r') ? raw.slice(0, -1) : raw);
      },
      end: () => {
        if (rest !== '') line(rest.endsWith('\r') ? rest.slice(0, -1) : rest);
        if (seen === 0) problem(`${name} has no rows.`);
      },
    };
  };

  const unzip = new Unzip();
  unzip.register(UnzipInflate);
  // The first error inside fflate's callbacks, raised again after push() returns (a callback's error does not
  // reliably reach push()'s caller).
  let raised: unknown = null;
  unzip.onfile = (entry: UnzipFile) => {
    if (raised !== null || stopped) return;
    try {
      const name = entry.name;
      if (name.includes('..') || name.startsWith('/') || name.includes('\\')) {
        problem(`The zip holds the path "${name}"; paths with ".." (or starting with "/") are not allowed.`);
        throw new Stop();
      }
      if (name.endsWith('/')) return;
      const base = name.slice(name.lastIndexOf('/') + 1);
      if (/\.zip$/i.test(name)) { problem(`The zip holds another zip, "${name}"; put its CSV or JSON Lines files in the one zip instead.`); throw new Stop(); }
      if (README.test(base)) {
        readmes += 1;
        if (readmes > 1) { problem(`The zip holds more than one README ("${name}" is the second); keep one.`); throw new Stop(); }
        // Not read; fflate may still hand over a stored file's bytes.
        entry.ondata = () => {};
        return;
      }
      // The files a Mac's "Compress" adds (the __MACOSX folder, .DS_Store) are not data: skipped, never read (the
      // owner's ruling, 5 November 2026). Any other file starting with "." is refused below.
      if (name.startsWith('__MACOSX/') || base === '.DS_Store') {
        entry.ondata = () => {};
        return;
      }
      const hidden = base.startsWith('.');
      const csv = !hidden && /\.csv$/i.test(name);
      if (!csv && (hidden || !/\.jsonl$/i.test(name))) {
        problem(`The zip holds "${name}", which is not a .csv or .jsonl file or a README; remove it.`);
        throw new Stop();
      }
      const reader = readerFor(name, csv);
      const decoder = new TextDecoder('utf-8', { fatal: true });
      let unreadable = false;
      entry.ondata = (error, chunk, final) => {
        if (raised !== null || stopped || unreadable) return;
        try {
          if (error !== null) { unreadable = true; problem(`${name} could not be read from the zip: ${error.message}`); return; }
          let text: string;
          try {
            text = decoder.decode(chunk, { stream: !final });
          } catch (decodeError) {
            if (!(decodeError instanceof TypeError)) throw decodeError;
            unreadable = true;
            problem(`${name} is not UTF-8 text; save it as UTF-8 and zip it again.`);
            return;
          }
          reader.push(text);
          if (final) reader.end();
        } catch (thrown) {
          raised = thrown;
        }
      };
      try {
        entry.start();
      } catch (startError) {
        if (!(startError instanceof Error)) throw startError;
        problem(`${name} is compressed in a way the page cannot read (method ${entry.compression}); zip it with the usual method (deflate).`);
      }
    } catch (thrown) {
      raised = thrown;
    }
  };

  const reader = file.stream().getReader();
  let read = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (!done) read += value.byteLength;
      unzip.push(done ? new Uint8Array(0) : value, done);
      if (raised !== null) throw raised;
      onProgress(read, file.size);
      if (done) break;
    }
  } catch (error) {
    if (!(error instanceof Stop)) throw error;
    stopped = true;
    await reader.cancel();
  }

  if (!stopped && problems.length === 0) {
    if (files.length === 0) problems.push('The zip holds no .csv or .jsonl file.');
    const rows = files.reduce((sum, f) => sum + f.rows, 0);
    if (files.length > 0 && rows < LIMITS.dataSetRowsMin) {
      problems.push(`The data set has ${rows.toLocaleString('en-US')} ${rows === 1 ? 'row' : 'rows'}; it needs at least ${LIMITS.dataSetRowsMin}.`);
    }
    for (const [key, count] of counts) {
      if (files.length > 0 && count < LIMITS.dataSetPerOptionMin) {
        problems.push(`The option "${key}" has ${count.toLocaleString('en-US')} ${count === 1 ? 'row' : 'rows'}; every option needs at least ${LIMITS.dataSetPerOptionMin}.`);
      }
    }
  }
  if (problems.length > 0) throw new DataSetRefused(problems, stopped && problems.length >= MAX_PROBLEMS);
  return { rows: files.reduce((sum, f) => sum + f.rows, 0), counts: [...counts].map(([key, count]) => ({ key, count })), files };
}
