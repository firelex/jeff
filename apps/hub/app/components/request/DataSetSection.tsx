// Section 6 of /request-adapter, "I have a data set": the requester chooses one .zip of CSV and JSON Lines files, and
// the page checks every file in the browser before anything is sent (app/lib/data-set-zip.ts): the columns or keys,
// the answers, empty rows, the input's length, and the minimums. It shows the rows per option, or every problem with
// its file and row. The zip is uploaded only when the request is sent (request-adapter.tsx). The templates are the
// ones typed examples use (app/lib/adapter-request-import.ts), built from the form's own fields.
import { useRef } from 'react';
import { LIMITS, megabytes } from '../../lib/adapter-request';
import { csvTemplate, jsonlTemplate, type ImportShape } from '../../lib/adapter-request-import';
import { checkDataSetZip, DataSetRefused, type DataSetCheck } from '../../lib/data-set-zip';
import { download } from './download';
import { HINT, SMALL_BUTTON } from './fields';

// What the page knows of the chosen zip. `key` is the form's sections 2 to 5 as they were when it was checked, so a
// change there asks for the check again.
export type DataSetState =
  | { kind: 'none' }
  | { kind: 'checking'; file: File; percent: number }
  | { kind: 'refused'; file: File; key: string; problems: string[]; more: boolean }
  | { kind: 'checked'; file: File; key: string; result: DataSetCheck };

export const keyOf = (shape: ImportShape, maxLength: number) => JSON.stringify({ shape, maxLength });

export function DataSetSection({ shape, maxLength, state, onChange }: {
  // Null while sections 2 to 5 are not yet right: the data set is checked against them.
  shape: ImportShape | null;
  maxLength: number | null;
  state: DataSetState;
  onChange: (state: DataSetState) => void;
}) {
  const fileRef = useRef<HTMLInputElement>(null);
  const ready = shape !== null && maxLength !== null;
  const key = ready ? keyOf(shape, maxLength) : null;

  const check = async (file: File) => {
    if (!ready) return;
    let percent = 0;
    onChange({ kind: 'checking', file, percent });
    try {
      const result = await checkDataSetZip(file, file.name, shape, maxLength, (read, total) => {
        const now = Math.floor((read / total) * 100);
        if (now !== percent) { percent = now; onChange({ kind: 'checking', file, percent }); }
      });
      onChange({ kind: 'checked', file, key: key!, result });
    } catch (error) {
      if (!(error instanceof DataSetRefused)) throw error;
      onChange({ kind: 'refused', file, key: key!, problems: error.problems, more: error.more });
    }
  };

  const stale = (state.kind === 'checked' || state.kind === 'refused') && state.key !== key;
  return (
    <div className="mt-4">
      <p className="text-sm text-soft">
        {`One .zip of CSV or JSON Lines files (or both), one labelled row per case: the input in the form section 4 gives, the context values if section 5 has fields, and the correct answer, which must be one of the options${shape?.answer.kind === 'caller' ? ' (each row also carries the options it was asked with)' : ''}. At least ${LIMITS.dataSetRowsMin} rows and ${LIMITS.dataSetPerOptionMin} for every option; at most ${megabytes(LIMITS.dataSetBytesMax)}.`}
      </p>
      <p className={HINT}>Only .csv and .jsonl files and one optional README (.md or .txt) may be in the zip. It is checked here, in your browser, before anything is sent.</p>
      {!ready
        ? <p className="mt-3 text-sm text-soft">Fill in sections 2 to 5 first: the data set is checked against them.</p>
        : (
          <>
            <div className="mt-3 flex flex-wrap gap-3">
              <button type="button" className={SMALL_BUTTON} onClick={() => download('data-set-template.csv', 'text/csv', csvTemplate(shape))}>CSV template</button>
              <button type="button" className={SMALL_BUTTON} onClick={() => download('data-set-template.jsonl', 'application/x-ndjson', jsonlTemplate(shape))}>JSON Lines template</button>
              <button type="button" className={SMALL_BUTTON} disabled={state.kind === 'checking'} onClick={() => fileRef.current?.click()}>
                {state.kind === 'none' ? 'Choose the zip' : 'Choose another zip'}
              </button>
              <input ref={fileRef} type="file" accept=".zip,application/zip" className="sr-only" aria-label="Choose the zip of your data set" tabIndex={-1}
                onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ''; if (file !== undefined) void check(file); }} />
            </div>
          </>
        )}

      {state.kind === 'checking' && (
        <p role="status" className="mt-3 font-mono text-sm text-soft">{`Checking ${state.file.name}: ${state.percent}%`}</p>
      )}
      {stale && (
        <div role="alert" className="mt-3 text-sm text-danger">
          <p>{`Sections 2 to 5 changed since ${state.file.name} was checked; check it again.`}</p>
          {ready && <button type="button" className={`mt-2 ${SMALL_BUTTON}`} onClick={() => void check(state.file)}>Check it again</button>}
        </div>
      )}
      {state.kind === 'refused' && !stale && (
        <div role="alert" className="mt-3 border border-danger p-3 text-sm text-danger">
          <p>{`${state.file.name} cannot be sent: ${state.problems.length === 1 ? 'one thing needs' : `${state.problems.length} things need`} fixing${state.more ? ' (the check stopped here; there may be more)' : ''}.`}</p>
          <ul className="mt-2 list-disc break-words pl-5">{state.problems.map((p, i) => <li key={i}>{p}</li>)}</ul>
        </div>
      )}
      {state.kind === 'checked' && !stale && (
        <div className="mt-3 border border-rule bg-background p-3" aria-live="polite">
          <p className="break-words font-mono text-sm text-text">
            {`${state.file.name}: ${state.result.rows.toLocaleString('en-US')} rows in ${state.result.files.length} ${state.result.files.length === 1 ? 'file' : 'files'}`}
            <span className="text-mute">{` · ${megabytes(state.file.size)}`}</span>
          </p>
          {state.result.counts.length > 0 && (
            <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs text-soft" aria-label="Rows per option">
              {state.result.counts.map(({ key: option, count }) => <li key={option}>{`${option}: ${count.toLocaleString('en-US')}`}</li>)}
            </ul>
          )}
          <p className="mt-2 text-xs text-mute">Checked. It is uploaded when you send the request.</p>
        </div>
      )}
    </div>
  );
}
