// Section 6 of /request-adapter, "Generate the data for me": the labelled examples. They are added by typing one at a time, by pasting CSV or
// JSON Lines, or by uploading a .csv or .jsonl file; the templates for both are built from the form's own fields
// (app/lib/adapter-request-import.ts). The counts per option are shown as they change, against the minimums.
import { useRef, useState } from 'react';
import {
  countsPerOption, inputLength, LIMITS, NONE_KEY, type Example, type OptionDef,
} from '../../lib/adapter-request';
import { csvTemplate, examplesFromCsv, examplesFromJsonl, examplesFromText, ImportRefused, jsonlTemplate, type ImportShape } from '../../lib/adapter-request-import';
import { BUTTON_SECONDARY } from '../styles';
import { download } from './download';
import { HINT, LABEL, MONO_INPUT, SMALL_BUTTON, TextField, Tick } from './fields';

// The shape the examples follow, or null while sections 2 to 5 do not yet say it (no kind of answer, no input form).
export type Shape = ImportShape | null;

interface Draft { input: string; fields: Record<string, string>; context: Record<string, string>; options: string; answer: string[]; reason: string; tricky: boolean }

const emptyDraft = (): Draft => ({ input: '', fields: {}, context: {}, options: '', answer: [], reason: '', tricky: false });

// The options of an example of "given by the caller", one per line as "key: definition".
function optionsOf(text: string): OptionDef[] {
  return text.split(/\r?\n/).map((line) => line.trim()).filter((line) => line !== '').map((line) => {
    const at = line.indexOf(':');
    return at === -1 ? { key: line, definition: '' } : { key: line.slice(0, at).trim(), definition: line.slice(at + 1).trim() };
  });
}

function exampleOf(draft: Draft, shape: ImportShape): Example {
  const example: Example = {
    input: shape.input.form === 'text' ? draft.input : Object.fromEntries(shape.input.fields.map((f) => [f.name, draft.fields[f.name] ?? ''])),
    context: Object.fromEntries(shape.context.map((f) => [f.name, draft.context[f.name] ?? ''])),
    answer: shape.answer.kind === 'several' ? draft.answer : (draft.answer[0] ?? ''),
    reason: draft.reason,
    tricky: draft.tricky,
  };
  if (shape.answer.kind === 'caller') example.options = optionsOf(draft.options);
  return example;
}

function draftOf(example: Example): Draft {
  return {
    input: typeof example.input === 'string' ? example.input : '',
    fields: typeof example.input === 'string' ? {} : { ...example.input },
    context: { ...example.context },
    options: (example.options ?? []).map((o) => `${o.key}: ${o.definition}`).join('\n'),
    answer: Array.isArray(example.answer) ? [...example.answer] : example.answer === '' ? [] : [example.answer],
    reason: example.reason,
    tricky: example.tricky,
  };
}

function preview(example: Example): string {
  const text = typeof example.input === 'string' ? example.input : Object.entries(example.input).map(([k, v]) => `${k}: ${v}`).join('; ');
  return text.length > 90 ? `${text.slice(0, 90)}...` : text;
}

export function ExamplesSection({ shape, examples, onChange, bytes }: {
  shape: Shape;
  examples: Example[];
  onChange: (examples: Example[]) => void;
  bytes: number;
}) {
  const [draft, setDraft] = useState<Draft>(emptyDraft);
  const [editing, setEditing] = useState<number | null>(null);
  const [pasted, setPasted] = useState('');
  const [notice, setNotice] = useState<{ kind: 'refused' | 'done'; text: string } | null>(null);
  const fileRef = useRef<HTMLInputElement>(null);

  if (shape === null) {
    return <p className="mt-3 text-sm text-soft">Choose the kind of answer (section 2) and the form of the input (section 4) first: the examples follow them.</p>;
  }
  const fixedKeys = shape.answer.kind === 'caller' ? null : shape.answer.options.map((o) => o.key).filter((k) => k !== '');
  const counts = shape.answer.kind === 'caller' ? [] : countsPerOption({ answer: shape.answer, examples });
  const add = (more: Example[], how: string) => {
    if (examples.length + more.length > LIMITS.examplesMax) {
      setNotice({ kind: 'refused', text: `That would make ${(examples.length + more.length).toLocaleString('en-US')} examples; the most is ${LIMITS.examplesMax.toLocaleString('en-US')}.` });
      return false;
    }
    onChange([...examples, ...more]);
    setNotice({ kind: 'done', text: `${how}: ${more.length.toLocaleString('en-US')} ${more.length === 1 ? 'example' : 'examples'} added.` });
    return true;
  };
  const importing = (work: () => Example[], how: string) => {
    try {
      return add(work(), how);
    } catch (error) {
      if (!(error instanceof ImportRefused)) throw error;
      setNotice({ kind: 'refused', text: error.message });
      return false;
    }
  };

  const save = () => {
    const example = exampleOf(draft, shape);
    if (editing === null) {
      if (!add([example], 'Typed')) return;
    } else {
      onChange(examples.map((e, i) => (i === editing ? example : e)));
      setNotice({ kind: 'done', text: `Example ${editing + 1} saved.` });
      setEditing(null);
    }
    setDraft(emptyDraft());
  };

  const upload = async (file: File) => {
    if (file.size > LIMITS.bodyBytesMax) {
      setNotice({ kind: 'refused', text: `The file is ${(file.size / 1_000_000).toFixed(1)} MB; the whole request may be at most ${LIMITS.bodyBytesMax / 1_000_000} MB.` });
      return;
    }
    const text = await file.text();
    const jsonl = /\.(jsonl|ndjson)$/i.test(file.name);
    if (!jsonl && !/\.csv$/i.test(file.name)) {
      setNotice({ kind: 'refused', text: `Upload a .csv or .jsonl file; "${file.name}" is neither.` });
      return;
    }
    importing(() => (jsonl ? examplesFromJsonl(text) : examplesFromCsv(text, shape)), `From ${file.name}`);
  };

  const setAnswer = (key: string, on: boolean) => setDraft((d) => {
    if (shape.answer.kind !== 'several') return { ...d, answer: on ? [key] : [] };
    return { ...d, answer: on ? [...d.answer.filter((k) => k !== key), key] : d.answer.filter((k) => k !== key) };
  });
  const callerKeys = shape.answer.kind === 'caller' ? optionsOf(draft.options).map((o) => o.key).filter((k) => k !== '') : [];
  const answerKeys = fixedKeys ?? callerKeys;

  return (
    <>
      <p className="mt-3 text-sm text-soft">
        {`At least ${LIMITS.examplesMin} labelled examples, and at least ${LIMITS.perOptionMin} for every option. At most ${LIMITS.examplesMax.toLocaleString('en-US')} examples and ${LIMITS.bodyBytesMax / 1_000_000} MB in all. Real examples work best; mark the tricky ones.`}
      </p>

      <div className="mt-4 border border-rule bg-background p-3" aria-live="polite">
        <p className="font-mono text-sm text-text">
          {`${examples.length.toLocaleString('en-US')} of at least ${LIMITS.examplesMin} examples`}
          <span className="text-mute">{` · ${(bytes / 1_000_000).toFixed(2)} MB of ${LIMITS.bodyBytesMax / 1_000_000} MB`}</span>
        </p>
        {counts.length > 0 && (
          <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 font-mono text-xs" aria-label="Examples per option">
            {counts.map(({ key, count }) => (
              <li key={key} className={count < LIMITS.perOptionMin ? 'text-danger' : 'text-soft'}>
                {`${key === '' ? '(no key yet)' : key}: ${count}${count < LIMITS.perOptionMin ? ` (needs ${LIMITS.perOptionMin})` : ''}`}
              </li>
            ))}
          </ul>
        )}
      </div>

      <h3 className="mt-6 font-mono text-sm font-medium text-text">Paste or upload</h3>
      <p className={HINT}>
        CSV with a header line, or JSON Lines (one JSON object per line). Download a template to see the columns this
        form needs.
      </p>
      <div className="mt-2 flex flex-wrap gap-3">
        <button type="button" className={SMALL_BUTTON} onClick={() => download('examples-template.csv', 'text/csv', csvTemplate(shape))}>CSV template</button>
        <button type="button" className={SMALL_BUTTON} onClick={() => download('examples-template.jsonl', 'application/x-ndjson', jsonlTemplate(shape))}>JSON Lines template</button>
      </div>
      <label htmlFor="paste-examples" className={`mt-4 ${LABEL}`}>Paste CSV or JSON Lines</label>
      <textarea id="paste-examples" value={pasted} onChange={(e) => setPasted(e.target.value)} rows={5} className={`mt-1 ${MONO_INPUT}`} spellCheck={false} />
      <div className="mt-2 flex flex-wrap gap-3">
        <button type="button" className={SMALL_BUTTON} disabled={pasted.trim() === ''}
          onClick={() => { if (importing(() => examplesFromText(pasted, shape), 'Pasted')) setPasted(''); }}>Add pasted examples</button>
        <button type="button" className={SMALL_BUTTON} onClick={() => fileRef.current?.click()}>Upload a file</button>
        <input ref={fileRef} type="file" accept=".csv,.jsonl,.ndjson" className="sr-only" aria-label="Upload a CSV or JSON Lines file" tabIndex={-1}
          onChange={(e) => { const file = e.target.files?.[0]; e.target.value = ''; if (file !== undefined) void upload(file); }} />
      </div>
      {notice !== null && (
        <p role={notice.kind === 'refused' ? 'alert' : 'status'} className={`mt-3 text-sm ${notice.kind === 'refused' ? 'text-danger' : 'text-soft'}`}>{notice.text}</p>
      )}

      <h3 className="mt-6 font-mono text-sm font-medium text-text">{editing === null ? 'Type an example' : `Edit example ${editing + 1}`}</h3>
      {shape.input.form === 'text'
        ? <TextField label="Input" value={draft.input} onChange={(input) => setDraft((d) => ({ ...d, input }))} multiline />
        : shape.input.fields.map((field) => (
          <TextField key={field.name} label={`Input: ${field.name}`} hint={field.description} value={draft.fields[field.name] ?? ''}
            onChange={(v) => setDraft((d) => ({ ...d, fields: { ...d.fields, [field.name]: v } }))} multiline />
        ))}
      {shape.context.map((field) => (
        <TextField key={field.name} label={`Context: ${field.name}`} hint={field.description} value={draft.context[field.name] ?? ''}
          onChange={(v) => setDraft((d) => ({ ...d, context: { ...d.context, [field.name]: v } }))} />
      ))}
      {shape.answer.kind === 'caller' && (
        <TextField label="The options this example was asked with" hint={`One per line, as key: when it is right${shape.answer.noneFits === 'none_option' ? `; include "${NONE_KEY}"` : ''}.`}
          value={draft.options} onChange={(options) => setDraft((d) => ({ ...d, options }))} multiline mono />
      )}
      <fieldset className="mt-4">
        <legend className={LABEL}>{shape.answer.kind === 'several' ? 'The correct answers' : 'The correct answer'}</legend>
        {answerKeys.length === 0
          ? <p className={HINT}>{shape.answer.kind === 'caller' ? 'Write the options above first.' : 'Give the options in section 3 first.'}</p>
          : (
            <div className="mt-2 flex flex-wrap gap-x-4 gap-y-2">
              {answerKeys.map((key) => (
                <label key={key} className="flex items-center gap-2 font-mono text-sm text-text">
                  <input type={shape.answer.kind === 'several' ? 'checkbox' : 'radio'} name="draft-answer" checked={draft.answer.includes(key)}
                    onChange={(e) => setAnswer(key, e.target.checked)} className="accent-ink" />
                  {key}
                </label>
              ))}
            </div>
          )}
      </fieldset>
      <TextField label="Why (optional, one line)" value={draft.reason} onChange={(reason) => setDraft((d) => ({ ...d, reason }))} maxLength={LIMITS.reasonMax} />
      <Tick checked={draft.tricky} onChange={(tricky) => setDraft((d) => ({ ...d, tricky }))}>This one is tricky</Tick>
      <div className="mt-3 flex flex-wrap gap-3">
        <button type="button" className={`${BUTTON_SECONDARY} px-4 py-2`} onClick={save}>{editing === null ? 'Add example' : 'Save example'}</button>
        {editing !== null && <button type="button" className={SMALL_BUTTON} onClick={() => { setEditing(null); setDraft(emptyDraft()); }}>Cancel</button>}
      </div>

      {examples.length > 0 && (
        <>
          <div className="mt-6 flex flex-wrap items-center justify-between gap-3">
            <h3 className="font-mono text-sm font-medium text-text">Your examples</h3>
            <button type="button" className={SMALL_BUTTON} onClick={() => { onChange([]); setEditing(null); setNotice({ kind: 'done', text: 'Every example removed.' }); }}>Remove all</button>
          </div>
          {/* A list, not a table: at phone width a table's fixed columns left the input one letter wide. */}
          <ol className="mt-2 max-h-96 overflow-y-auto border border-ink bg-panel" aria-label="Your examples">
            {examples.map((example, i) => (
              <li key={i} className="border-b border-rule px-3 py-2 text-sm last:border-b-0">
                <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
                  <span className="font-mono text-xs text-mute">{i + 1}</span>
                  <span className="break-all font-mono text-xs text-text">{Array.isArray(example.answer) ? example.answer.join(', ') : example.answer}</span>
                  <span className="font-mono text-xs text-mute">{`${inputLength(example.input).toLocaleString('en-US')} characters${example.tricky ? ' · tricky' : ''}`}</span>
                  <span className="ml-auto flex gap-3">
                    <button type="button" className="font-mono text-xs text-primary underline" onClick={() => { setEditing(i); setDraft(draftOf(example)); }}
                      aria-label={`Edit example ${i + 1}`}>Edit</button>
                    <button type="button" className="font-mono text-xs text-primary underline"
                      onClick={() => { onChange(examples.filter((_, j) => j !== i)); if (editing !== null) { setEditing(null); setDraft(emptyDraft()); } }}
                      aria-label={`Remove example ${i + 1}`}>Remove</button>
                  </span>
                </div>
                <p className="mt-1 break-words text-soft">{preview(example)}</p>
              </li>
            ))}
          </ol>
        </>
      )}
    </>
  );
}

