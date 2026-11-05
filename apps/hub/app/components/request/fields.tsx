// The small pieces the /request-adapter form is built from: a labelled text box, a choice of radio buttons, a tick,
// a list of problems, and the editor of a list of options (key and definition).
import { useId, type ReactNode } from 'react';
import type { OptionDef, Problem } from '../../lib/adapter-request';
import { BUTTON_SECONDARY } from '../styles';

export const INPUT = 'w-full min-w-0 border border-ink bg-background px-3 py-2 text-sm placeholder:text-faint';
export const MONO_INPUT = `${INPUT} font-mono`;
export const LABEL = 'block font-mono text-sm text-soft';
export const HINT = 'mt-1 text-xs text-mute';
export const SMALL_BUTTON = `${BUTTON_SECONDARY} px-3 py-1`;

export function TextField({ label, hint, value, onChange, multiline = false, maxLength, placeholder, mono = false, inputMode, readOnly = false }: {
  label: string; hint?: string; value: string; onChange: (value: string) => void; multiline?: boolean; maxLength?: number;
  placeholder?: string; mono?: boolean; inputMode?: 'numeric'; readOnly?: boolean;
}) {
  const id = useId();
  const className = mono ? MONO_INPUT : INPUT;
  return (
    <div className="mt-4">
      <label htmlFor={id} className={LABEL}>{label}</label>
      {multiline
        ? <textarea id={id} value={value} onChange={(e) => onChange(e.target.value)} maxLength={maxLength} placeholder={placeholder} rows={3} className={`mt-1 ${className}`} />
        : <input id={id} value={value} onChange={(e) => onChange(e.target.value)} maxLength={maxLength} placeholder={placeholder} inputMode={inputMode}
            readOnly={readOnly} autoComplete="off" className={`mt-1 ${className} ${readOnly ? 'text-mute' : ''}`} />}
      {hint !== undefined && <p className={HINT}>{hint}</p>}
    </div>
  );
}

export function Choice<T extends string>({ legend, value, options, onChange }: {
  legend: string; value: T | ''; options: readonly { value: T; label: string }[]; onChange: (value: T) => void;
}) {
  const name = useId();
  return (
    <fieldset className="mt-4">
      <legend className={LABEL}>{legend}</legend>
      <div className="mt-2 flex flex-col gap-2">
        {options.map((option) => (
          <label key={option.value} className="flex items-start gap-2 text-sm text-text">
            <input type="radio" name={name} value={option.value} checked={value === option.value} onChange={() => onChange(option.value)} className="mt-1 accent-ink" />
            <span>{option.label}</span>
          </label>
        ))}
      </div>
    </fieldset>
  );
}

export function Tick({ checked, onChange, children }: { checked: boolean; onChange: (checked: boolean) => void; children: ReactNode }) {
  return (
    <label className="mt-3 flex items-start gap-2 text-sm text-text">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="mt-1 accent-ink" />
      <span>{children}</span>
    </label>
  );
}

// The problems of one section, after the visitor first tried to send: the first ten, and how many more.
export function Problems({ problems }: { problems: Problem[] }) {
  if (problems.length === 0) return null;
  const shown = problems.slice(0, 10);
  return (
    <div role="alert" className="mt-4 border border-danger p-3 text-sm text-danger">
      <ul className="list-disc pl-5">
        {shown.map((problem, i) => <li key={`${problem.field}-${i}`}>{problem.message}</li>)}
      </ul>
      {problems.length > shown.length && <p className="mt-2">{`And ${problems.length - shown.length} more.`}</p>}
    </div>
  );
}

// A list of options, each a key and a one-line definition. `fixedKeys` keeps the keys as they are (yes or no).
export function OptionsEditor({ title, options, onChange, fixedKeys = false, max }: {
  title: string; options: OptionDef[]; onChange: (options: OptionDef[]) => void; fixedKeys?: boolean; max: number;
}) {
  const set = (i: number, change: Partial<OptionDef>) => onChange(options.map((o, j) => (j === i ? { ...o, ...change } : o)));
  return (
    <fieldset className="mt-4">
      <legend className={LABEL}>{title}</legend>
      <div className="mt-2 flex flex-col gap-3">
        {options.map((option, i) => (
          <div key={i} className="flex flex-col gap-2 border border-rule p-3 sm:flex-row sm:items-start">
            <input aria-label={`${title}: key ${i + 1}`} value={option.key} readOnly={fixedKeys} onChange={(e) => set(i, { key: e.target.value })}
              placeholder="key, e.g. billing" autoComplete="off" className={`${MONO_INPUT} sm:w-44 ${fixedKeys ? 'text-mute' : ''}`} />
            <input aria-label={`${title}: when ${option.key === '' ? `option ${i + 1}` : `"${option.key}"`} is right`} value={option.definition}
              onChange={(e) => set(i, { definition: e.target.value })} placeholder="When this option is right, in one line" autoComplete="off" className={INPUT} />
            {!fixedKeys && (
              <button type="button" className={SMALL_BUTTON} onClick={() => onChange(options.filter((_, j) => j !== i))}
                aria-label={`Remove ${title.toLowerCase()} option ${i + 1}`}>Remove</button>
            )}
          </div>
        ))}
      </div>
      {!fixedKeys && options.length < max && (
        <button type="button" className={`mt-3 ${SMALL_BUTTON}`} onClick={() => onChange([...options, { key: '', definition: '' }])}>Add an option</button>
      )}
    </fieldset>
  );
}
