// A request for an adapter (the /request-adapter page): what a signed-in visitor sends, and the one set of rules it
// must meet. The page checks the form with checkAdapterRequest as it is filled in, and the hub's function
// (server/endpoints/adapter-requests.ts) checks what arrives with the same function, so the two can never disagree.
// This module is shared by the page's browser code and the function, so it imports nothing.
//
// Every problem names the field in plain words ("Example 4: the answer ..."), and carries the section of the form it
// belongs to, so the page can show it there.

export const ANSWER_KINDS = ['one', 'yes_no', 'several', 'caller'] as const;
export type AnswerKind = (typeof ANSWER_KINDS)[number];

// The kinds as the form and the owner's email name them.
export const ANSWER_KIND_LABELS: Record<AnswerKind, string> = {
  one: 'One option from a fixed list',
  yes_no: 'Yes or no',
  several: 'Several options from a fixed list',
  caller: 'Options that differ on every call, given by the caller',
};

// What should happen when no option fits: a "none" option among the options, or no answer at all (abstain).
export const NONE_FITS = ['none_option', 'abstain'] as const;
export type NoneFits = (typeof NONE_FITS)[number];
export const NONE_KEY = 'none';

export const INPUT_FORMS = ['text', 'fields'] as const;
export type InputForm = (typeof INPUT_FORMS)[number];

export interface OptionDef { key: string; definition: string }
export interface NamedField { name: string; description: string }
export interface ContextField { name: string; description: string; example: string }

// One labelled example. `input` is a text when the input is one text, or an object of the named fields' values.
// `answer` is a list of keys for "several options", one key otherwise. `options` is the option set the example was
// asked with, for "options given by the caller" only (absent otherwise). `reason` is empty when none is given.
export interface Example {
  input: string | Record<string, string>;
  context: Record<string, string>;
  answer: string | string[];
  reason: string;
  tricky: boolean;
  options?: OptionDef[];
}

// How the examples are given (section 6): a data set the requester uploads (a zip of CSV and JSON Lines files, one
// labelled row per case, stored in the hub's private file store), or a few examples typed, pasted or uploaded here,
// from which the training data is written in the same style.
export const EXAMPLE_SOURCES = ['data_set', 'generate'] as const;
export type ExampleSource = (typeof EXAMPLE_SOURCES)[number];
export const EXAMPLE_SOURCE_LABELS: Record<ExampleSource, string> = {
  data_set: 'I have a data set',
  generate: 'Generate the data for me',
};

// The uploaded data set as the request carries it: where the zip is in the file store, its size in bytes, and the
// rows the browser counted in it (in all, and per option of a fixed kind, in the options' order; empty for options
// given by the caller).
export interface DataSetUpload { pathname: string; bytes: number; rows: number; counts: { key: string; count: number }[] }

export interface AdapterRequestForm {
  decision: { question: string; why: string };
  // `options` for the fixed kinds (empty for "given by the caller"); `exampleSets`, three option sets, for "given by
  // the caller" only (empty otherwise).
  answer: { kind: AnswerKind; options: OptionDef[]; noneFits: NoneFits; exampleSets: OptionDef[][] };
  // `fields` for named fields (empty for one text).
  input: { form: InputForm; fields: NamedField[]; typicalLength: number; maxLength: number; languages: string };
  context: ContextField[];
  // `examples` for "Generate the data for me" (empty with a data set); `dataSet` for "I have a data set" (null
  // otherwise).
  source: ExampleSource;
  examples: Example[];
  dataSet: DataSetUpload | null;
  moreExamplesLater: number;
  use: { callsPerDay: number; speedMatters: boolean; accuracy: string; abstainBetter: boolean };
  rights: { mayShare: boolean; noPersonalData: boolean; mayImproveBase: boolean };
}

export const LIMITS = {
  questionMin: 10,
  questionMax: 300,
  whyMax: 600,
  optionsMin: 2,
  optionsMax: 30,
  keyMax: 40,
  definitionMax: 300,
  exampleSets: 3,
  inputFieldsMax: 20,
  contextFieldsMax: 20,
  fieldDescriptionMax: 300,
  contextValueMax: 20_000,
  maxLengthMax: 100_000,
  languagesMax: 200,
  examplesMin: 30,
  examplesMax: 2000,
  perOptionMin: 3,
  reasonMax: 300,
  accuracyMax: 200,
  callsPerDayMax: 1_000_000_000,
  moreExamplesMax: 10_000_000,
  declineReasonMax: 600,
  // The whole request as sent (its JSON, in bytes). Vercel refuses a request body over 4.5 MB.
  bodyBytesMax: 4_000_000,
  // A data set: at least 300 rows and 20 for every option of a fixed kind, in one zip of at most 500 MB.
  dataSetRowsMin: 300,
  dataSetPerOptionMin: 20,
  dataSetBytesMax: 500_000_000,
} as const;

// Where an account's data sets go in the file store: adapter-requests/<the account's folder>/. The page asks for an
// upload as <folder>data-set.zip; the hub's function picks the zip's own name, data-set-<random>.zip, and signs the
// token for it alone (re-review R1). The function tells the page the folder (GET /api/adapter-requests).
// The upload endpoint's answer: @vercel/blob's client token, for the one pathname it may create (re-review R1).
export interface UploadToken { type: 'blob.generate-client-token'; clientToken: string; pathname: string }

export const DATA_SET_ROOT = 'adapter-requests';
export const DATA_SET_NAME = 'data-set.zip';
export const DATA_SET_CONTENT_TYPE = 'application/zip';
export const DATA_SET_PATH = /^adapter-requests\/[A-Za-z0-9_-]+\/data-set(-[A-Za-z0-9]+)?\.zip$/;

export function megabytes(bytes: number): string {
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

// An option key, a field name: lower-case letters, digits and underscores.
export const KEY_PATTERN = /^[a-z0-9_]+$/;
// Column and property names an example already uses, which a field may not take.
export const RESERVED_NAMES = ['input', 'context', 'answer', 'reason', 'tricky', 'options'] as const;

export const SECTIONS = {
  1: 'The decision',
  2: 'The kind of answer',
  3: 'The answer options',
  4: 'The input',
  5: 'Context',
  6: 'Examples',
  7: 'Use',
  8: 'Data and rights',
} as const;
export type Section = keyof typeof SECTIONS;

export interface Problem { section: Section; field: string; message: string }

export type Checked = { ok: true; form: AdapterRequestForm } | { ok: false; problems: Problem[] };

const isObject = (value: unknown): value is Record<string, unknown> =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const plural = (n: number, one: string, many = `${one}s`) => `${n.toLocaleString('en-US')} ${n === 1 ? one : many}`;

// What the counts per option depend on: the kind, the fixed options and the examples.
export interface CountsOf { answer: { kind: AnswerKind; options: OptionDef[] }; examples: Example[] }

// The length of an example's input in characters: the text, or every field's value together.
export function inputLength(input: Example['input']): number {
  return typeof input === 'string' ? input.length : Object.values(input).reduce((sum, value) => sum + value.length, 0);
}

// How many examples have each option as (one of) their answer, for the fixed kinds; in the options' order.
export function countsPerOption(form: CountsOf): { key: string; count: number }[] {
  const counts = new Map(form.answer.options.map((option) => [option.key, 0]));
  for (const example of form.examples) {
    for (const key of Array.isArray(example.answer) ? new Set(example.answer) : [example.answer]) {
      const now = counts.get(key);
      if (now !== undefined) counts.set(key, now + 1);
    }
  }
  return [...counts].map(([key, count]) => ({ key, count }));
}

// Whether the examples meet the minimums (at least 30, and 3 for every option of a fixed kind): the page refuses to
// send until they do.
export function meetsExampleMinimums(form: CountsOf): boolean {
  if (form.examples.length < LIMITS.examplesMin) return false;
  if (form.answer.kind === 'caller') return true;
  return countsPerOption(form).every(({ count }) => count >= LIMITS.perOptionMin);
}

// The size of the whole request as it is sent, in bytes.
export function bodyBytes(form: unknown): number {
  return new TextEncoder().encode(JSON.stringify(form)).length;
}

// Checks a request, whatever was sent: the page's own form, or any JSON at the function. Returns the request when it
// meets every rule, or every problem found (each naming its field), in the form's order.
export function checkAdapterRequest(value: unknown): Checked {
  const problems: Problem[] = [];
  const say = (section: Section, field: string, message: string) => { problems.push({ section, field, message }); };

  if (!isObject(value)) {
    say(1, 'request', 'The request must be a JSON object with the form\'s eight sections.');
    return { ok: false, problems };
  }

  // A text of `min` to `max` characters, on one line when `oneLine`. Returns it trimmed, or null after saying why.
  const text = (section: Section, field: string, name: string, raw: unknown, min: number, max: number, oneLine: boolean): string | null => {
    if (typeof raw !== 'string') { say(section, field, `${name} must be text.`); return null; }
    const trimmed = raw.trim();
    if (trimmed.length < min || trimmed.length > max) {
      const range = min <= 1 ? `${min === 0 ? 'at most' : '1 to'} ${max.toLocaleString('en-US')}` : `${min} to ${max.toLocaleString('en-US')}`;
      const required = min >= 1 && trimmed.length === 0;
      say(section, field, required ? `${name} is required.` : `${name} must be ${range} characters; it has ${trimmed.length.toLocaleString('en-US')}.`);
      return null;
    }
    if (oneLine && /[\r\n]/.test(trimmed)) { say(section, field, `${name} must be on one line.`); return null; }
    return trimmed;
  };
  const wholeNumber = (section: Section, field: string, name: string, raw: unknown, min: number, max: number): number | null => {
    if (typeof raw !== 'number' || !Number.isInteger(raw) || raw < min || raw > max) {
      say(section, field, `${name} must be a whole number from ${min.toLocaleString('en-US')} to ${max.toLocaleString('en-US')}.`);
      return null;
    }
    return raw;
  };
  const yesNo = (section: Section, field: string, name: string, raw: unknown): boolean | null => {
    if (typeof raw !== 'boolean') { say(section, field, `${name}: choose yes or no.`); return null; }
    return raw;
  };
  const oneOf = <T extends string>(section: Section, field: string, name: string, raw: unknown, allowed: readonly T[]): T | null => {
    if (typeof raw !== 'string' || !(allowed as readonly string[]).includes(raw)) { say(section, field, `${name}: choose one.`); return null; }
    return raw as T;
  };
  const section = (name: string, number: Section): Record<string, unknown> => {
    const raw = value[name];
    if (isObject(raw)) return raw;
    say(number, name, `${SECTIONS[number]}: the section is missing.`);
    return {};
  };
  const list = (sectionNo: Section, field: string, name: string, raw: unknown): unknown[] | null => {
    if (!Array.isArray(raw)) { say(sectionNo, field, `${name} must be a list.`); return null; }
    return raw;
  };

  // A list of options: 2 to 30, each a unique key and a one-line definition. `where` names the list.
  const options = (sectionNo: Section, field: string, where: string, raw: unknown): OptionDef[] | null => {
    const items = list(sectionNo, field, where, raw);
    if (items === null) return null;
    const before = problems.length;
    if (items.length < LIMITS.optionsMin || items.length > LIMITS.optionsMax) {
      say(sectionNo, field, `${where} must have ${LIMITS.optionsMin} to ${LIMITS.optionsMax} options; it has ${items.length}.`);
    }
    const seen = new Set<string>();
    const out: OptionDef[] = [];
    items.forEach((item, i) => {
      const name = `${where}, option ${i + 1}`;
      if (!isObject(item)) { say(sectionNo, `${field}[${i}]`, `${name} must have a key and a definition.`); return; }
      const key = text(sectionNo, `${field}[${i}].key`, `${name}: the key`, item.key, 1, LIMITS.keyMax, true);
      if (key !== null && !KEY_PATTERN.test(key)) {
        say(sectionNo, `${field}[${i}].key`, `${name}: the key "${key}" may use only lower-case letters, digits and underscores.`);
      } else if (key !== null && seen.has(key)) {
        say(sectionNo, `${field}[${i}].key`, `${name}: the key "${key}" is used twice; every key must be different.`);
      }
      if (key !== null) seen.add(key);
      const definition = text(sectionNo, `${field}[${i}].definition`, `${name}: the definition`, item.definition, 1, LIMITS.definitionMax, true);
      if (key !== null && definition !== null) out.push({ key, definition });
    });
    return problems.length === before ? out : null;
  };

  // 1. The decision.
  const decisionRaw = section('decision', 1);
  const question = text(1, 'decision.question', 'The question', decisionRaw.question, LIMITS.questionMin, LIMITS.questionMax, true);
  const why = text(1, 'decision.why', 'Why it matters', decisionRaw.why, 1, LIMITS.whyMax, false);

  // 2 and 3. The kind of answer and the options.
  const answerRaw = section('answer', 2);
  const kind = oneOf(2, 'answer.kind', 'The kind of answer', answerRaw.kind, ANSWER_KINDS);
  const noneFits = oneOf(3, 'answer.noneFits', 'What should happen when no option fits', answerRaw.noneFits, NONE_FITS);
  let fixed: OptionDef[] | null = null;
  let exampleSets: OptionDef[][] | null = null;
  // The rule about the "none" key, for one list of options.
  const noneRule = (field: string, where: string, keys: string[]) => {
    if (noneFits === 'none_option' && !keys.includes(NONE_KEY)) {
      say(3, field, `${where} must include the option "${NONE_KEY}", since you chose a "${NONE_KEY}" option for when no option fits.`);
    }
    if (noneFits === 'abstain' && keys.includes(NONE_KEY)) {
      say(3, field, `${where} has the option "${NONE_KEY}", but you chose to abstain when no option fits; remove it or choose a "${NONE_KEY}" option.`);
    }
  };
  if (kind !== null) {
    if (kind === 'caller') {
      const options0 = list(3, 'answer.options', 'The fixed options', answerRaw.options);
      if (options0 !== null && options0.length > 0) say(3, 'answer.options', 'Options given by the caller have no fixed list; give three example option sets instead.');
      const sets = list(3, 'answer.exampleSets', 'The example option sets', answerRaw.exampleSets);
      if (sets !== null) {
        if (sets.length !== LIMITS.exampleSets) {
          say(3, 'answer.exampleSets', `Give exactly ${LIMITS.exampleSets} example option sets; there are ${sets.length}.`);
        } else {
          const got = sets.map((set, i) => options(3, `answer.exampleSets[${i}]`, `Example option set ${i + 1}`, set));
          got.forEach((set, i) => { if (set !== null) noneRule(`answer.exampleSets[${i}]`, `Example option set ${i + 1}`, set.map((o) => o.key)); });
          if (got.every((set) => set !== null)) exampleSets = got as OptionDef[][];
        }
      }
    } else {
      const sets = list(3, 'answer.exampleSets', 'The example option sets', answerRaw.exampleSets);
      if (sets !== null && sets.length > 0) say(3, 'answer.exampleSets', 'Example option sets are only for options given by the caller.');
      fixed = options(3, 'answer.options', 'The options', answerRaw.options);
      if (fixed !== null) {
        const keys = fixed.map((o) => o.key);
        if (kind === 'yes_no') {
          const expected = noneFits === 'none_option' ? ['yes', 'no', NONE_KEY] : ['yes', 'no'];
          if (keys.length !== expected.length || !expected.every((key) => keys.includes(key))) {
            say(3, 'answer.options', `A yes-or-no answer has exactly the options ${expected.map((k) => `"${k}"`).join(', ')}.`);
          }
        } else {
          noneRule('answer.options', 'The options', keys);
        }
      }
    }
  }

  // 4. The input.
  const inputRaw = section('input', 4);
  const form = oneOf(4, 'input.form', 'The form of the input', inputRaw.form, INPUT_FORMS);
  let inputFields: NamedField[] | null = null;
  const fieldsRaw = list(4, 'input.fields', 'The input fields', inputRaw.fields);
  // Every name an example uses: the input's fields and the context's, unique together.
  const names = new Set<string>();
  const fieldName = (sectionNo: Section, field: string, where: string, raw: unknown): string | null => {
    const name = text(sectionNo, field, `${where}: the name`, raw, 1, LIMITS.keyMax, true);
    if (name === null) return null;
    if (!KEY_PATTERN.test(name)) { say(sectionNo, field, `${where}: the name "${name}" may use only lower-case letters, digits and underscores.`); return null; }
    if ((RESERVED_NAMES as readonly string[]).includes(name)) { say(sectionNo, field, `${where}: the name "${name}" is taken by the examples' own columns; choose another.`); return null; }
    if (names.has(name)) { say(sectionNo, field, `${where}: the name "${name}" is used twice; every input and context field needs its own name.`); return null; }
    names.add(name);
    return name;
  };
  if (form !== null && fieldsRaw !== null) {
    if (form === 'text') {
      if (fieldsRaw.length > 0) say(4, 'input.fields', 'An input of one text has no named fields; remove them or choose named fields.');
      else inputFields = [];
    } else if (fieldsRaw.length < 1 || fieldsRaw.length > LIMITS.inputFieldsMax) {
      say(4, 'input.fields', `Named fields need 1 to ${LIMITS.inputFieldsMax} fields; there are ${fieldsRaw.length}.`);
    } else {
      const before = problems.length;
      const out: NamedField[] = [];
      fieldsRaw.forEach((item, i) => {
        const where = `Input field ${i + 1}`;
        if (!isObject(item)) { say(4, `input.fields[${i}]`, `${where} must have a name and a description.`); return; }
        const name = fieldName(4, `input.fields[${i}].name`, where, item.name);
        const description = text(4, `input.fields[${i}].description`, `${where}: the description`, item.description, 1, LIMITS.fieldDescriptionMax, true);
        if (name !== null && description !== null) out.push({ name, description });
      });
      if (problems.length === before) inputFields = out;
    }
  }
  const maxLength = wholeNumber(4, 'input.maxLength', 'The maximum length', inputRaw.maxLength, 1, LIMITS.maxLengthMax);
  const typicalLength = wholeNumber(4, 'input.typicalLength', 'The typical length', inputRaw.typicalLength, 1, LIMITS.maxLengthMax);
  if (maxLength !== null && typicalLength !== null && typicalLength > maxLength) {
    say(4, 'input.typicalLength', `The typical length (${typicalLength.toLocaleString('en-US')}) is more than the maximum length (${maxLength.toLocaleString('en-US')}).`);
  }
  const languages = text(4, 'input.languages', 'The language or languages', inputRaw.languages, 1, LIMITS.languagesMax, true);

  // 5. Context.
  let context: ContextField[] | null = null;
  const contextRaw = list(5, 'context', 'The context fields', value.context);
  if (contextRaw !== null) {
    if (contextRaw.length > LIMITS.contextFieldsMax) {
      say(5, 'context', `Give at most ${LIMITS.contextFieldsMax} context fields; there are ${contextRaw.length}.`);
    } else {
      const before = problems.length;
      const out: ContextField[] = [];
      contextRaw.forEach((item, i) => {
        const where = `Context field ${i + 1}`;
        if (!isObject(item)) { say(5, `context[${i}]`, `${where} must have a name, a description and an example value.`); return; }
        const name = fieldName(5, `context[${i}].name`, where, item.name);
        const description = text(5, `context[${i}].description`, `${where}: the description`, item.description, 1, LIMITS.fieldDescriptionMax, true);
        const example = text(5, `context[${i}].example`, `${where}: the example value`, item.example, 1, LIMITS.contextValueMax, false);
        if (name !== null && description !== null && example !== null) out.push({ name, description, example });
      });
      if (problems.length === before) context = out;
    }
  }

  // 6. Examples: how they are given, then the typed examples or the data set. Each is checked against the sections
  // above only once those are right, so a problem there is said once, not again for every example.
  const source = oneOf(6, 'source', 'How you give the examples', value.source, EXAMPLE_SOURCES);
  let examples: Example[] | null = null;
  let dataSet: DataSetUpload | null | undefined;
  const examplesRaw = list(6, 'examples', 'The examples', value.examples);
  const ready = kind !== null && noneFits !== null && form !== null && inputFields !== null && context !== null && maxLength !== null
    && (kind === 'caller' ? exampleSets !== null : fixed !== null);
  if (source === 'data_set') {
    if (examplesRaw !== null && examplesRaw.length > 0) {
      say(6, 'examples', 'With a data set the examples are in the zip; remove the typed examples, or choose "Generate the data for me".');
    } else if (examplesRaw !== null) {
      examples = [];
    }
    if (value.dataSet === null || value.dataSet === undefined) {
      say(6, 'dataSet', 'Choose the zip of your data set and let the page check it.');
    } else if (ready) {
      const checked = checkDataSet(value.dataSet, kind, fixed, say);
      if (checked !== null) dataSet = checked;
    }
  } else if (source === 'generate') {
    if (value.dataSet !== null) say(6, 'dataSet', 'A data set goes only with "I have a data set"; remove it, or choose that option.');
    else dataSet = null;
    if (examplesRaw !== null) {
      if (examplesRaw.length < LIMITS.examplesMin) {
        say(6, 'examples', `Give at least ${LIMITS.examplesMin} labelled examples; there ${examplesRaw.length === 1 ? 'is' : 'are'} ${examplesRaw.length}.`);
      }
      if (examplesRaw.length > LIMITS.examplesMax) {
        say(6, 'examples', `Give at most ${LIMITS.examplesMax.toLocaleString('en-US')} examples; there are ${examplesRaw.length.toLocaleString('en-US')}.`);
      }
      if (ready && examplesRaw.length <= LIMITS.examplesMax) {
        const before = problems.length;
        const out: Example[] = [];
        const rules: ExampleRules = { kind, noneFits, form, inputFields: inputFields!, context: context!, maxLength, fixed };
        examplesRaw.forEach((item, i) => {
          const example = checkExample(item, `Example ${i + 1}`, `examples[${i}]`, rules, (field, message) => say(6, field, message));
          if (example !== null) out.push(example);
        });
        if (problems.length === before) {
          const candidate = { answer: { kind, options: fixed ?? [] }, examples: out };
          if (kind !== 'caller') {
            for (const { key, count } of countsPerOption(candidate)) {
              if (count < LIMITS.perOptionMin) {
                say(6, 'examples', `The option "${key}" has ${plural(count, 'example')}; every option needs at least ${LIMITS.perOptionMin}.`);
              }
            }
          }
          if (problems.length === before) examples = out;
        }
      }
    }
  }
  const moreExamplesLater = wholeNumber(6, 'moreExamplesLater', 'How many more real examples you could supply later',
    value.moreExamplesLater, 0, LIMITS.moreExamplesMax);

  // 7. Use.
  const useRaw = section('use', 7);
  const callsPerDay = wholeNumber(7, 'use.callsPerDay', 'Calls a day', useRaw.callsPerDay, 0, LIMITS.callsPerDayMax);
  const speedMatters = yesNo(7, 'use.speedMatters', 'Whether speed matters', useRaw.speedMatters);
  const accuracy = text(7, 'use.accuracy', 'The accuracy that would make it useful', useRaw.accuracy, 1, LIMITS.accuracyMax, true);
  const abstainBetter = yesNo(7, 'use.abstainBetter', 'Whether no answer is better than a wrong one', useRaw.abstainBetter);

  // 8. Data and rights.
  const rightsRaw = section('rights', 8);
  if (rightsRaw.mayShare !== true) say(8, 'rights.mayShare', 'Tick that you have the right to share these examples.');
  if (rightsRaw.noPersonalData !== true) say(8, 'rights.noPersonalData', 'Tick that the examples contain no personal data, or that it has been removed.');
  const mayImproveBase = yesNo(8, 'rights.mayImproveBase', 'Whether we may use the examples to improve Jeff\'s base model', rightsRaw.mayImproveBase);

  for (const name of Object.keys(value)) {
    if (!['decision', 'answer', 'input', 'context', 'source', 'examples', 'dataSet', 'moreExamplesLater', 'use', 'rights'].includes(name)) {
      say(1, name, `The request has a field the form does not have: "${name}".`);
    }
  }

  if (problems.length > 0) return { ok: false, problems: problems.sort((a, b) => a.section - b.section) };
  return {
    ok: true,
    form: {
      decision: { question: question!, why: why! },
      answer: { kind: kind!, options: fixed ?? [], noneFits: noneFits!, exampleSets: exampleSets ?? [] },
      input: { form: form!, fields: inputFields!, typicalLength: typicalLength!, maxLength: maxLength!, languages: languages! },
      context: context!,
      source: source!,
      examples: examples!,
      dataSet: dataSet!,
      moreExamplesLater: moreExamplesLater!,
      use: { callsPerDay: callsPerDay!, speedMatters: speedMatters!, accuracy: accuracy!, abstainBetter: abstainBetter! },
      rights: { mayShare: true, noPersonalData: true, mayImproveBase: mayImproveBase! },
    },
  };
}

// What an example (or a row of a data set) is checked against: sections 2 to 5, once they are right.
export interface ExampleRules {
  kind: AnswerKind;
  noneFits: NoneFits;
  form: InputForm;
  inputFields: NamedField[];
  context: ContextField[];
  maxLength: number;
  fixed: OptionDef[] | null;
}

// One example, against the sections it depends on. `where` names it in the problems ("Example 4", or a data set's
// "examples.csv, row 12"), and `field` is where it is in the request.
export function checkExample(item: unknown, where: string, field: string, rules: ExampleRules, say: (field: string, message: string) => void): Example | null {
  if (!isObject(item)) { say(field, `${where} must have an input and an answer.`); return null; }
  let ok = true;
  const fail = (sub: string, message: string) => { ok = false; say(`${field}${sub}`, `${where}: ${message}`); };

  for (const name of Object.keys(item)) {
    if (!['input', 'context', 'answer', 'reason', 'tricky', 'options'].includes(name)) fail(`.${name}`, `it has a field the form does not have: "${name}".`);
  }

  // The input, in the form section 4 gives.
  let input: Example['input'] | null = null;
  if (rules.form === 'text') {
    if (typeof item.input !== 'string' || item.input.trim() === '') fail('.input', 'the input text is required.');
    else input = item.input;
  } else if (!isObject(item.input)) {
    fail('.input', `the input must give each field: ${rules.inputFields.map((f) => f.name).join(', ')}.`);
  } else {
    const values: Record<string, string> = {};
    const raw = item.input;
    for (const { name } of rules.inputFields) {
      if (typeof raw[name] !== 'string') fail(`.input.${name}`, `the input field "${name}" is missing.`);
      else values[name] = raw[name] as string;
    }
    for (const name of Object.keys(raw)) {
      if (!rules.inputFields.some((f) => f.name === name)) fail(`.input.${name}`, `the input has a field "${name}" that section 4 does not name.`);
    }
    if (ok && Object.values(values).every((v) => v.trim() === '')) fail('.input', 'every input field is empty.');
    if (ok) input = values;
  }
  if (input !== null && inputLength(input) > rules.maxLength) {
    fail('.input', `the input has ${inputLength(input).toLocaleString('en-US')} characters, more than the maximum length of ${rules.maxLength.toLocaleString('en-US')}.`);
  }

  // The context values, one for every context field.
  const context: Record<string, string> = {};
  if (!isObject(item.context)) {
    fail('.context', rules.context.length === 0 ? 'the context must be empty ({}), since there are no context fields.' : 'the context values are missing.');
  } else {
    const raw = item.context;
    for (const { name } of rules.context) {
      const v = raw[name];
      if (typeof v !== 'string' || v.trim() === '') fail(`.context.${name}`, `the context value "${name}" is required.`);
      else if (v.length > LIMITS.contextValueMax) fail(`.context.${name}`, `the context value "${name}" must be at most ${LIMITS.contextValueMax.toLocaleString('en-US')} characters.`);
      else context[name] = v;
    }
    for (const name of Object.keys(raw)) {
      if (!rules.context.some((f) => f.name === name)) fail(`.context.${name}`, `the context has a field "${name}" that section 5 does not name.`);
    }
  }

  // The option set this example was asked with (given by the caller), or the fixed options.
  let options: OptionDef[] | null = rules.fixed;
  if (rules.kind === 'caller') {
    options = exampleOptions(item.options, (message) => fail('.options', message), rules.noneFits);
  } else if (item.options !== undefined) {
    fail('.options', 'only options given by the caller carry their own options with each example.');
  }

  // The answer: one of the options (several of them for "several options").
  let answer: Example['answer'] | null = null;
  const keys = options === null ? null : options.map((o) => o.key);
  const known = (key: string) => keys === null || keys.includes(key);
  if (rules.kind === 'several') {
    if (!Array.isArray(item.answer) || item.answer.length === 0 || !item.answer.every((k) => typeof k === 'string')) {
      fail('.answer', 'the answer must be one or more of the options.');
    } else if (new Set(item.answer).size !== item.answer.length) {
      fail('.answer', 'the answer names one option twice.');
    } else {
      const unknown = (item.answer as string[]).filter((k) => !known(k));
      if (unknown.length > 0) fail('.answer', `the answer ${unknown.map((k) => `"${k}"`).join(', ')} ${unknown.length === 1 ? 'is' : 'are'} not one of the options.`);
      else answer = item.answer as string[];
    }
  } else if (typeof item.answer !== 'string' || item.answer.trim() === '') {
    fail('.answer', 'the correct answer is required.');
  } else if (!known(item.answer)) {
    fail('.answer', `the answer "${item.answer}" is not one of the options.`);
  } else {
    answer = item.answer;
  }

  let reason = '';
  if (item.reason !== undefined) {
    if (typeof item.reason !== 'string') fail('.reason', 'the reason must be text.');
    else if (/[\r\n]/.test(item.reason.trim())) fail('.reason', 'the reason must be on one line.');
    else if (item.reason.trim().length > LIMITS.reasonMax) fail('.reason', `the reason must be at most ${LIMITS.reasonMax} characters.`);
    else reason = item.reason.trim();
  }
  let tricky = false;
  if (item.tricky !== undefined) {
    if (typeof item.tricky !== 'boolean') fail('.tricky', 'the tricky mark must be yes or no.');
    else tricky = item.tricky;
  }

  if (!ok || input === null || answer === null || options === null) return null;
  return rules.kind === 'caller' ? { input, context, answer, reason, tricky, options } : { input, context, answer, reason, tricky };
}

// The option set an example of "options given by the caller" was asked with.
function exampleOptions(raw: unknown, fail: (message: string) => void, noneFits: NoneFits): OptionDef[] | null {
  if (!Array.isArray(raw)) { fail('the options it was asked with are required (options given by the caller).'); return null; }
  if (raw.length < LIMITS.optionsMin || raw.length > LIMITS.optionsMax) {
    fail(`it must have ${LIMITS.optionsMin} to ${LIMITS.optionsMax} options; it has ${raw.length}.`);
    return null;
  }
  const out: OptionDef[] = [];
  for (const [j, item] of raw.entries()) {
    const key = isObject(item) && typeof item.key === 'string' ? item.key.trim() : null;
    const definition = isObject(item) && typeof item.definition === 'string' ? item.definition.trim() : null;
    if (key === null || key === '' || definition === null || definition === '') { fail(`its option ${j + 1} needs a key and a definition.`); return null; }
    if (!KEY_PATTERN.test(key) || key.length > LIMITS.keyMax) { fail(`its option key "${key}" may use only lower-case letters, digits and underscores, at most ${LIMITS.keyMax}.`); return null; }
    if (/[\r\n]/.test(definition) || definition.length > LIMITS.definitionMax) { fail(`the definition of its option "${key}" must be one line of at most ${LIMITS.definitionMax} characters.`); return null; }
    if (out.some((o) => o.key === key)) { fail(`its option key "${key}" is used twice.`); return null; }
    out.push({ key, definition });
  }
  const hasNone = out.some((o) => o.key === NONE_KEY);
  if (noneFits === 'none_option' && !hasNone) { fail(`its options must include "${NONE_KEY}", since you chose a "${NONE_KEY}" option for when no option fits.`); return null; }
  if (noneFits === 'abstain' && hasNone) { fail(`its options include "${NONE_KEY}", but you chose to abstain when no option fits.`); return null; }
  return out;
}

// The data set as the request describes it: where it was uploaded, its size, and the rows counted in it, which must
// meet the minimums (300 rows, 20 for every option of a fixed kind).
function checkDataSet(raw: unknown, kind: AnswerKind, fixed: OptionDef[] | null, say: (section: Section, field: string, message: string) => void): DataSetUpload | null {
  let count = 0;
  const fail = (field: string, message: string) => { count += 1; say(6, `dataSet${field}`, message); };
  if (!isObject(raw)) { fail('', 'The data set must say where the zip was uploaded, its size and its rows.'); return null; }
  for (const name of Object.keys(raw)) {
    if (!['pathname', 'bytes', 'rows', 'counts'].includes(name)) fail(`.${name}`, `The data set has a field the form does not have: "${name}".`);
  }
  const { pathname, bytes, rows, counts } = raw;
  if (typeof pathname !== 'string' || !DATA_SET_PATH.test(pathname)) fail('.pathname', 'The data set must be a zip uploaded from this page; upload it again.');
  if (typeof bytes !== 'number' || !Number.isInteger(bytes) || bytes < 1) {
    fail('.bytes', 'The size of the data set must be a whole number of bytes.');
  } else if (bytes > LIMITS.dataSetBytesMax) {
    fail('.bytes', `The data set is ${megabytes(bytes)}; it may be at most ${megabytes(LIMITS.dataSetBytesMax)}.`);
  }
  if (typeof rows !== 'number' || !Number.isInteger(rows) || rows < 0) {
    fail('.rows', 'The number of rows in the data set must be a whole number.');
  } else if (rows < LIMITS.dataSetRowsMin) {
    fail('.rows', `The data set has ${plural(rows, 'row')}; it needs at least ${LIMITS.dataSetRowsMin}.`);
  }
  const keys = kind === 'caller' ? [] : (fixed ?? []).map((o) => o.key);
  if (!Array.isArray(counts)) {
    fail('.counts', 'The rows per option must be a list.');
  } else if (counts.length !== keys.length || counts.some((c, i) => !isObject(c) || c.key !== keys[i] || typeof c.count !== 'number' || !Number.isInteger(c.count) || c.count < 0)) {
    fail('.counts', kind === 'caller'
      ? 'Options given by the caller have no fixed options to count rows for; the rows per option must be empty.'
      : `The rows per option must give a whole number for each option, in order: ${keys.join(', ')}.`);
  } else if (typeof rows === 'number' && Number.isInteger(rows)) {
    const sum = (counts as { count: number }[]).reduce((total, c) => total + c.count, 0);
    for (const c of counts as { key: string; count: number }[]) {
      if (c.count < LIMITS.dataSetPerOptionMin) fail('.counts', `The option "${c.key}" has ${plural(c.count, 'row')} in the data set; every option needs at least ${LIMITS.dataSetPerOptionMin}.`);
      if (c.count > rows) fail('.counts', `The option "${c.key}" has more rows (${c.count}) than the data set (${rows}).`);
    }
    if (kind !== 'several' && kind !== 'caller' && sum !== rows) fail('.counts', `The rows per option add up to ${sum.toLocaleString('en-US')}, but the data set has ${plural(rows, 'row')}.`);
    if (kind === 'several' && sum < rows) fail('.counts', `The rows per option add up to ${sum.toLocaleString('en-US')}, fewer than the data set's ${plural(rows, 'row')}; every row has at least one answer.`);
  }
  if (count > 0) return null;
  return { pathname: pathname as string, bytes: bytes as number, rows: rows as number, counts: (counts as { key: string; count: number }[]).map(({ key, count: n }) => ({ key, count: n })) };
}

// --- What the endpoints answer, shared with the pages. ---

export const REQUEST_STATUSES = ['received', 'in_review', 'training', 'delivered', 'declined'] as const;
export type RequestStatus = (typeof REQUEST_STATUSES)[number];
// The statuses of a request that is still open (the limit of open requests counts these).
export const OPEN_STATUSES: readonly RequestStatus[] = ['received', 'in_review', 'training'];
// The statuses of a closed request: its examples are deleted RETENTION_DAYS after it was closed.
export const CLOSED_STATUSES: readonly RequestStatus[] = ['delivered', 'declined'];
export const STATUS_LABELS: Record<RequestStatus, string> = {
  received: 'Received',
  in_review: 'In review',
  training: 'Training',
  delivered: 'Delivered',
  declined: 'Declined',
};

// At most this many open requests per account, and this many sent in any 24 hours.
export const MAX_OPEN_REQUESTS = 3;
export const MAX_SENDS_PER_DAY = 5;
// At most this many data set upload tokens per account in any 24 hours (final review I1).
export const MAX_UPLOADS_PER_DAY = 5;

// How long the examples are kept after a request is closed (clearExpiredExamples, adapter-requests.server.ts), and
// the line the form shows (section 8).
export const RETENTION_DAYS = 90;
export const RETENTION_LINE = 'We keep your examples until the adapter is delivered and for 90 days after, and delete them sooner if you ask.';
// Where the examples go (section 8, for both ways of giving them), and what "Generate the data for me" does (section 6).
export const OWN_MODEL_LINE = 'Your examples are given to our own language model to write the training data; they are not sent to any outside service.';
export const GENERATE_LINE = 'We write the training data in the same style with our own language model, and test the adapter on a share of your examples it never saw.';

// POST /api/adapter-requests answers 201 with this.
export interface CreatedRequest { id: string }

// GET /api/adapter-requests: the requester's own, newest first.
export interface OwnRequest { id: string; question: string; status: RequestStatus; createdAt: string; updatedAt: string; declineReason: string | null }
// `dataSetFolder` is where this account's data sets are uploaded (adapter-requests/<folder>/), for the page's upload.
export interface OwnRequests { requests: OwnRequest[]; dataSetFolder: string }

// GET /api/admin/adapter-requests: every request, newest first.
export interface AdminRequestRow {
  id: string; email: string; question: string; status: RequestStatus; kind: AnswerKind;
  // `exampleCount` is the typed examples' count (0 with a data set); `dataSet` the data set's size and rows (null
  // for "Generate the data for me").
  source: ExampleSource; optionCount: number; exampleCount: number; dataSet: { bytes: number; rows: number } | null; createdAt: string;
  // When the typed examples and the data set's zip were deleted, 90 days after the request was closed; null while kept.
  examplesDeletedAt: string | null;
}
export interface AdminRequests { requests: AdminRequestRow[] }

// What is stored of the form as one JSON document: everything but how the examples were given, the examples, the
// data set and the consent (each kept in columns of their own).
export type StoredForm = Omit<AdapterRequestForm, 'source' | 'examples' | 'dataSet' | 'rights'>;

// GET /api/admin/adapter-requests/:id.
export interface AdminRequestDetail {
  request: AdminRequestRow & {
    updatedAt: string; declineReason: string | null;
    // When it was delivered or declined; null while it is open.
    closedAt: string | null;
    form: StoredForm;
    examples: Example[];
    // The data set's rows per option, as the browser counted them (null for "Generate the data for me").
    dataSetCounts: DataSetUpload['counts'] | null;
    consent: AdapterRequestForm['rights'];
  };
}

// The number of options a request has: the fixed options, or the largest example option set.
export function optionCount(answer: AdapterRequestForm['answer']): number {
  return answer.kind === 'caller' ? Math.max(...answer.exampleSets.map((set) => set.length)) : answer.options.length;
}
