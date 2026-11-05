// A form generated from one GET /v1/functions listing that builds one POST /v1/decide call: the input, each context
// field the function's adapter names (by its code name, as a call passes it), and the labels in whatever form the
// function takes them (see the manifest schema's labels.source: fixed, caller, subset or describe; fixed labels with
// subset true may be narrowed by the caller).
import { Braces, Plus, Trash2, Type } from 'lucide-react';
import { Badge, Button, Input, Textarea } from '@jeff/ui';
import type { DecideCall, FunctionListing, Json, LabelSpec, Labels } from '../api';

/** One field's text, sent as a string (text) or parsed first (json). */
export interface FieldValue {
  mode: 'text' | 'json';
  text: string;
}

export type LabelsState =
  | { source: 'none' }
  | { source: 'caller'; rows: { key: string; description: string }[] }
  | { source: 'subset'; chosen: string[] }
  // Fixed labels with subset true: every label is ticked to start with, and labels are sent only when some are not.
  | { source: 'fixed-subset'; chosen: string[] }
  | { source: 'describe'; descriptions: Record<string, string> };

export interface FormState {
  input: FieldValue;
  context: Record<string, FieldValue>;
  labels: LabelsState;
}

/** The form's own refusal of what is typed, before anything is sent. */
export class FormError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'FormError';
  }
}

/** Whether the caller passes labels at all: only choice functions whose labels are not fixed, or are fixed and allow
 * the caller a subset (labels.subset true). */
function labelSource(listing: FunctionListing): Exclude<LabelSpec['source'], 'fixed'> | 'fixed-subset' | 'none' {
  if (listing.type !== 'choice') return 'none';
  if (listing.labels.source === 'fixed') return listing.labels.subset === true ? 'fixed-subset' : 'none';
  return listing.labels.source;
}

function describeKeys(listing: FunctionListing): string[] {
  const keys = listing.labels.keys;
  if (!keys) throw new Error(`${listing.id} has describe labels but its listing names no keys`);
  return keys;
}

/** A subset function's whole label set (labels.fixed); its listing must name it. */
function subsetLabels(listing: FunctionListing): Record<string, string | null> {
  const fixed = listing.labels.fixed;
  if (!fixed) throw new Error(`${listing.id} has subset labels but its listing names no fixed labels`);
  return fixed;
}

/** The labels a subset function must always offer; labels.required is optional, and absent means none. */
function requiredLabels(listing: FunctionListing): string[] {
  return listing.labels.required === undefined ? [] : listing.labels.required;
}

/** A choice function's fixed labels; its listing must name them. */
function fixedChoiceLabels(listing: FunctionListing): Record<string, string | null> {
  const fixed = listing.labels.fixed;
  if (!fixed) throw new Error(`${listing.id} is a choice function with fixed labels but its listing names none`);
  return fixed;
}

/** A score function's levels, lowest first; its listing must name them. */
function scoreLevels(listing: FunctionListing): string[] {
  const levels = listing.labels.levels;
  if (!levels) throw new Error(`${listing.id} is a score function but its listing names no levels`);
  return levels;
}

function describedLabel(listing: FunctionListing, descriptions: Record<string, string>, key: string): string {
  const text = descriptions[key];
  if (text === undefined) throw new Error(`The form for ${listing.id} has no state for label ${key}`);
  return text;
}

function emptyLabels(listing: FunctionListing): LabelsState {
  const source = labelSource(listing);
  if (source === 'caller') return { source, rows: [{ key: '', description: '' }] };
  if (source === 'subset') return { source, chosen: [...requiredLabels(listing)] };
  if (source === 'fixed-subset') return { source, chosen: Object.keys(fixedChoiceLabels(listing)) };
  if (source === 'describe') return { source, descriptions: Object.fromEntries(describeKeys(listing).map((key) => [key, ''])) };
  return { source: 'none' };
}

export function emptyForm(listing: FunctionListing): FormState {
  return {
    input: { mode: 'text', text: '' },
    context: Object.fromEntries(listing.context.map((field) => [field.code_name, { mode: 'text', text: '' }])),
    labels: emptyLabels(listing),
  };
}

function fieldFrom(value: Json): FieldValue {
  return typeof value === 'string' ? { mode: 'text', text: value } : { mode: 'json', text: JSON.stringify(value, null, 2) };
}

/** The form showing an existing call (an example). Throws when the call does not fit the listing. */
export function formFromCall(listing: FunctionListing, call: DecideCall): FormState {
  if (call.function !== listing.id) throw new FormError(`The call is for ${call.function}, not ${listing.id}`);
  if (listing.input.form === 'text' && typeof call.input !== 'string') {
    throw new FormError(`${listing.id} takes plain text: its input (${listing.input.code_name}) must be a string`);
  }
  const form = emptyForm(listing);
  form.input = fieldFrom(call.input);
  const known = new Set(listing.context.map((field) => field.code_name));
  // The decide_request schema: a context or labels of null is the same as leaving it out.
  const given = call.context === undefined || call.context === null ? {} : call.context;
  for (const [key, value] of Object.entries(given)) {
    if (!known.has(key)) {
      // As the server does: a field given by its exact key (with spaces) is refused, naming its code name.
      const exact = listing.context.find((field) => field.key === key);
      const hint = exact === undefined ? '' : `; context is given by code name, so pass '${key}' as ${exact.code_name}`;
      throw new FormError(`${listing.id} has no context field ${key}${hint}`);
    }
    form.context[key] = fieldFrom(value);
  }
  const labels = call.labels;
  if (labels === undefined || labels === null) return form;
  // A label description of null means none, which the form shows (and sends back) as an empty description.
  if (form.labels.source === 'caller' && !Array.isArray(labels)) {
    form.labels = { source: 'caller', rows: Object.entries(labels).map(([key, description]) => ({ key, description: description ?? '' })) };
  } else if (form.labels.source === 'subset' && Array.isArray(labels)) {
    const allowed = subsetLabels(listing);
    const unknown = labels.filter((key) => !(key in allowed));
    if (unknown.length > 0) throw new FormError(`${listing.id} has no label(s) ${unknown.join(', ')}; its labels are ${Object.keys(allowed).join(', ')}`);
    form.labels = { source: 'subset', chosen: [...labels] };
  } else if (form.labels.source === 'fixed-subset' && Array.isArray(labels)) {
    const allowed = fixedChoiceLabels(listing);
    const unknown = labels.filter((key) => !(key in allowed));
    if (unknown.length > 0) throw new FormError(`${listing.id} has no label(s) ${unknown.join(', ')}; its labels are ${Object.keys(allowed).join(', ')}`);
    form.labels = { source: 'fixed-subset', chosen: [...labels] };
  } else if (form.labels.source === 'describe' && !Array.isArray(labels)) {
    const keys = describeKeys(listing);
    const given = Object.keys(labels);
    if (given.length !== keys.length || keys.some((key) => !given.includes(key))) {
      throw new FormError(`${listing.id} takes exactly the labels ${keys.join(', ')}; the call gives ${given.join(', ') || 'none'}`);
    }
    form.labels = { source: 'describe', descriptions: Object.fromEntries(keys.map((key) => [key, labels[key] ?? ''])) };
  } else {
    throw new FormError(`The example's labels do not fit ${listing.id}, whose labels are ${listing.labels.source}`);
  }
  return form;
}

function parseField(what: string, field: FieldValue): Json {
  if (field.mode === 'text') return field.text;
  try {
    return JSON.parse(field.text) as Json;
  } catch (error) {
    throw new FormError(`${what} is not valid JSON: ${(error as Error).message}`);
  }
}

function buildLabels(listing: FunctionListing, labels: LabelsState): Labels | undefined {
  switch (labels.source) {
    case 'none':
      return undefined;
    case 'caller': {
      const result: Record<string, string | null> = {};
      labels.rows.forEach((row, index) => {
        const key = row.key.trim();
        if (key === '' && row.description.trim() === '') return; // an untouched row
        if (key === '') throw new FormError(`Label ${index + 1} has a description but no key`);
        if (key in result) throw new FormError(`Label ${key} is given twice`);
        result[key] = row.description.trim() === '' ? null : row.description;
      });
      if (Object.keys(result).length === 0) throw new FormError('Add at least one label');
      return result;
    }
    case 'subset':
      return Object.keys(subsetLabels(listing)).filter((key) => labels.chosen.includes(key));
    case 'fixed-subset': {
      const all = Object.keys(fixedChoiceLabels(listing));
      const chosen = all.filter((key) => labels.chosen.includes(key));
      if (chosen.length === 0) throw new FormError(`Tick at least one label to offer; ${listing.id} needs one or more of ${all.join(', ')}`);
      // Every label ticked is the same as no labels: the function offers them all.
      return chosen.length === all.length ? undefined : chosen;
    }
    case 'describe':
      return Object.fromEntries(describeKeys(listing).map((key) => {
        const text = describedLabel(listing, labels.descriptions, key);
        return [key, text.trim() === '' ? null : text];
      }));
  }
}

/**
 * The /v1/decide call the form describes. Context fields left empty are left out, so a required one the server
 * still needs is named by the server's own MissingContext error. Throws FormError for what cannot be sent at all.
 */
export function buildCall(listing: FunctionListing, form: FormState): DecideCall {
  if (form.input.mode === 'text' && form.input.text.trim() === '') {
    throw new FormError(`The input (${listing.input.code_name}) is empty`);
  }
  const call: DecideCall = { function: listing.id, input: parseField(`The input (${listing.input.code_name})`, form.input) };
  const context: Record<string, Json> = {};
  for (const field of listing.context) {
    const value = contextField(form, listing, field.code_name);
    if (value.text.trim() === '') continue;
    context[field.code_name] = parseField(`Context ${field.code_name}`, value);
  }
  if (Object.keys(context).length > 0) call.context = context;
  const labels = buildLabels(listing, form.labels);
  if (labels !== undefined) call.labels = labels;
  return call;
}

// ---- The form ------------------------------------------------------------------------------------------------------

function ModeSwitch({ name, field, onChange }: { name: string; field: FieldValue; onChange: (next: FieldValue) => void }) {
  return (
    <div className="flex gap-1">
      <button type="button" aria-label={`${name} as text`} aria-pressed={field.mode === 'text'}
        onClick={() => onChange({ ...field, mode: 'text' })}
        className={['rounded-tag p-1', field.mode === 'text' ? 'bg-primary-light text-primary-dark' : 'text-faint hover:text-text'].join(' ')}>
        <Type className="size-3.5" aria-hidden="true" />
      </button>
      <button type="button" aria-label={`${name} as JSON`} aria-pressed={field.mode === 'json'}
        onClick={() => onChange({ ...field, mode: 'json' })}
        className={['rounded-tag p-1', field.mode === 'json' ? 'bg-primary-light text-primary-dark' : 'text-faint hover:text-text'].join(' ')}>
        <Braces className="size-3.5" aria-hidden="true" />
      </button>
    </div>
  );
}

function FieldBlock({ id, label, name, description, required, field, rows, textOnly, onChange }: {
  id: string; label: string; name: string; description: string; required: boolean; field: FieldValue; rows: number;
  textOnly: boolean; onChange: (next: FieldValue) => void;
}) {
  return (
    <div className="flex flex-col gap-1.5">
      <div className="flex items-center justify-between gap-2">
        <label htmlFor={id} className="flex items-center gap-2 text-sm font-medium text-text">
          <span className="font-mono text-[13px]">{name}</span>
          {required && <Badge tone="brand">required</Badge>}
        </label>
        {!textOnly && <ModeSwitch name={name} field={field} onChange={onChange} />}
      </div>
      <Textarea id={id} aria-label={label} rows={rows} value={field.text}
        className={field.mode === 'json' ? 'font-mono text-[13px]' : ''}
        onChange={(event) => onChange({ ...field, text: event.target.value })} />
      <p className="text-xs text-mute">{description}</p>
    </div>
  );
}

function OptionList({ options }: { options: [string, string | null][] }) {
  return (
    <ul className="flex flex-col gap-1 rounded-button border border-line bg-background px-3 py-2 text-sm">
      {options.map(([key, description]) => (
        <li key={key} className="flex gap-2">
          <span className="font-mono text-[13px] text-text">{key}</span>
          {description && <span className="text-soft">{description}</span>}
        </li>
      ))}
    </ul>
  );
}

function LabelsEditor({ listing, labels, onChange }: { listing: FunctionListing; labels: LabelsState; onChange: (next: LabelsState) => void }) {
  const spec = listing.labels;
  if (labels.source === 'none') {
    if (listing.type === 'score') {
      return (
        <div className="flex flex-col gap-1.5">
          <p className="text-sm font-medium text-text">Levels <span className="font-normal text-mute">(fixed, lowest first)</span></p>
          <OptionList options={scoreLevels(listing).map((level, index) => [String(index), level])} />
        </div>
      );
    }
    if (listing.type === 'noul') {
      // A yes/no function may describe its two answers (labels.fixed) or not.
      const fixed = spec.fixed === undefined ? [] : Object.entries(spec.fixed);
      return (
        <div className="flex flex-col gap-1.5">
          <p className="text-sm font-medium text-text">Answer <span className="font-normal text-mute">(yes or no)</span></p>
          {fixed.length > 0 && <OptionList options={fixed} />}
        </div>
      );
    }
    return (
      <div className="flex flex-col gap-1.5">
        <p className="text-sm font-medium text-text">Labels <span className="font-normal text-mute">(fixed)</span></p>
        <OptionList options={Object.entries(fixedChoiceLabels(listing))} />
      </div>
    );
  }
  if (labels.source === 'caller') {
    const setRow = (index: number, row: { key: string; description: string }) =>
      onChange({ source: 'caller', rows: labels.rows.map((old, other) => (other === index ? row : old)) });
    // Caller labels may have options always listed before them (labels.fixed_first), or none.
    const fixedFirst = spec.fixed_first === undefined ? [] : Object.entries(spec.fixed_first);
    return (
      <div className="flex flex-col gap-2">
        <p className="text-sm font-medium text-text">
          Labels <span className="font-normal text-mute">(yours{spec.max ? `, at most ${spec.max}` : ''}; an empty description sends none)</span>
        </p>
        {fixedFirst.length > 0 && (
          <div className="flex flex-col gap-1">
            <p className="text-xs text-mute">Always offered first:</p>
            <OptionList options={fixedFirst} />
          </div>
        )}
        {labels.rows.map((row, index) => (
          <div key={index} className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)_auto] items-center gap-2">
            <Input aria-label={`Label ${index + 1} key`} placeholder="key" className="font-mono text-[13px]" value={row.key}
              onChange={(event) => setRow(index, { ...row, key: event.target.value })} />
            <Input aria-label={`Label ${index + 1} description`} placeholder="What this label means" value={row.description}
              onChange={(event) => setRow(index, { ...row, description: event.target.value })} />
            <button type="button" aria-label={`Remove label ${index + 1}`} className="p-1 text-faint hover:text-danger"
              onClick={() => onChange({ source: 'caller', rows: labels.rows.filter((_, other) => other !== index) })}>
              <Trash2 className="size-4" aria-hidden="true" />
            </button>
          </div>
        ))}
        <div>
          <Button variant="ghost" size="sm" onClick={() => onChange({ source: 'caller', rows: [...labels.rows, { key: '', description: '' }] })}>
            <Plus className="size-3.5" aria-hidden="true" />
            Add a label
          </Button>
        </div>
      </div>
    );
  }
  if (labels.source === 'fixed-subset') {
    return (
      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1.5 text-sm font-medium text-text">Labels to offer <span className="font-normal text-mute">(fixed; untick any to offer only some)</span></legend>
        {Object.entries(fixedChoiceLabels(listing)).map(([key, description]) => (
          <label key={key} className="flex items-start gap-2 text-sm">
            <input type="checkbox" aria-label={key} className="mt-1 accent-primary" checked={labels.chosen.includes(key)}
              onChange={(event) => onChange({
                source: 'fixed-subset',
                chosen: event.target.checked ? [...labels.chosen, key] : labels.chosen.filter((chosen) => chosen !== key),
              })} />
            <span><span className="font-mono text-[13px] text-text">{key}</span> {description && <span className="text-soft">{description}</span>}</span>
          </label>
        ))}
      </fieldset>
    );
  }
  if (labels.source === 'subset') {
    const required = new Set(requiredLabels(listing));
    return (
      <fieldset className="flex flex-col gap-1.5">
        <legend className="mb-1.5 text-sm font-medium text-text">Labels to offer <span className="font-normal text-mute">(required ones always)</span></legend>
        {Object.entries(subsetLabels(listing)).map(([key, description]) => (
          <label key={key} className="flex items-start gap-2 text-sm">
            <input type="checkbox" aria-label={key} className="mt-1 accent-primary" checked={labels.chosen.includes(key)} disabled={required.has(key)}
              onChange={(event) => onChange({
                source: 'subset',
                chosen: event.target.checked ? [...labels.chosen, key] : labels.chosen.filter((chosen) => chosen !== key),
              })} />
            <span><span className="font-mono text-[13px] text-text">{key}</span> {description && <span className="text-soft">{description}</span>}</span>
          </label>
        ))}
      </fieldset>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      <p className="text-sm font-medium text-text">Labels <span className="font-normal text-mute">(describe each)</span></p>
      {describeKeys(listing).map((key) => (
        <div key={key} className="grid grid-cols-[minmax(0,10rem)_minmax(0,1fr)] items-center gap-2">
          <span className="font-mono text-[13px] text-text">{key}</span>
          <Input aria-label={`Label ${key} description`} value={describedLabel(listing, labels.descriptions, key)}
            onChange={(event) => onChange({ source: 'describe', descriptions: { ...labels.descriptions, [key]: event.target.value } })} />
        </div>
      ))}
    </div>
  );
}

function contextField(form: FormState, listing: FunctionListing, key: string): FieldValue {
  const field = form.context[key];
  if (field === undefined) throw new Error(`The form for ${listing.id} has no state for context field ${key}`);
  return field;
}

export interface FunctionFormProps {
  listing: FunctionListing;
  value: FormState;
  onChange: (next: FormState) => void;
}

export function FunctionForm({ listing, value, onChange }: FunctionFormProps) {
  return (
    <div className="flex flex-col gap-5">
      <FieldBlock id="function-input" label={`Input: ${listing.input.code_name}`} name={listing.input.code_name} description={listing.input.description}
        required field={value.input} rows={6} textOnly={listing.input.form === 'text'}
        onChange={(input) => onChange({ ...value, input })} />
      {listing.context.length > 0 && (
        <div className="flex flex-col gap-4">
          <p className="text-[10.5px] font-bold uppercase tracking-[0.14em] text-mute">Context</p>
          {listing.context.map((field) => (
            <FieldBlock key={field.code_name} id={`function-context-${field.code_name}`} label={`Context: ${field.code_name}`}
              name={field.code_name} description={field.description} required={field.required} rows={2} textOnly={false}
              field={contextField(value, listing, field.code_name)}
              onChange={(next) => onChange({ ...value, context: { ...value.context, [field.code_name]: next } })} />
          ))}
        </div>
      )}
      <LabelsEditor listing={listing} labels={value.labels} onChange={(labels) => onChange({ ...value, labels })} />
    </div>
  );
}
