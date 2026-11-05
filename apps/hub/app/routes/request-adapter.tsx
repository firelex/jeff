// /request-adapter: a signed-in visitor asks for an adapter for a decision of their own, in eight sections (the
// decision, the kind of answer, the options, the input, context, examples, use, data and rights). Prerendered like
// every page of the hub, so the shipped HTML holds only the heading: in the browser the page asks the hub's function
// for the visitor's requests (GET /api/adapter-requests), which tells it whether they are signed in (a visitor who is
// not goes to /signin and comes back here afterwards), then shows the form.
//
// The form is checked as it is filled in with the same rules the hub's function applies (app/lib/adapter-request.ts:
// checkAdapterRequest); Send stays off until the examples meet the minimums, and the first Send with any other
// problem shows every problem in its section. Section 6 offers two ways to give the examples: a data set (one zip,
// checked in the browser, components/request/DataSetSection.tsx, and uploaded on Send straight to the private file
// store, app/lib/data-set-upload.ts), or typed, pasted or uploaded examples from which the training data is
// generated. Sent (POST /api/adapter-requests), the page shows the request number.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { Link, useNavigate } from 'react-router';
import { Choice, OptionsEditor, Problems, TextField, Tick } from '../components/request/fields';
import { DataSetSection, keyOf, type DataSetState } from '../components/request/DataSetSection';
import { ExamplesSection, type Shape } from '../components/request/ExamplesSection';
import { BUTTON_PRIMARY, CARD, PAGE_TITLE, SECTION_HEADING, TEXT_LINK } from '../components/styles';
import {
  ANSWER_KIND_LABELS, ANSWER_KINDS, bodyBytes, checkAdapterRequest, DATA_SET_NAME, EXAMPLE_SOURCE_LABELS, GENERATE_LINE, LIMITS,
  meetsExampleMinimums, NONE_KEY, OWN_MODEL_LINE, RETENTION_LINE, SECTIONS,
  type AnswerKind, type ContextField, type CreatedRequest, type Example, type ExampleSource, type InputForm, type NamedField, type NoneFits,
  type OptionDef, type OwnRequests, type Problem, type Section,
} from '../lib/adapter-request';
import { askHub, refusalOf } from '../lib/ask-hub';
import { signInFor } from '../lib/auth-shared';
import { uploadDataSet } from '../lib/data-set-upload';

export const CHECKING = 'Checking that you are signed in…';
// Sending up to 4 MB may take a while on a slow connection.
export const SEND_TIMEOUT_MS = 60_000;
export const SEND_LABEL = 'Send request';

export function meta() {
  return [
    { title: 'Request an adapter | Jeff' },
    { name: 'description', content: 'Ask for a Jeff adapter for a decision of your own: describe it, give labelled examples, and we train it for your account.' },
  ];
}

type YesNo = '' | 'yes' | 'no';

interface Draft {
  question: string; why: string;
  kind: AnswerKind | ''; options: OptionDef[]; noneFits: NoneFits | ''; exampleSets: OptionDef[][];
  inputForm: InputForm | ''; inputFields: NamedField[]; typicalLength: string; maxLength: string; languages: string;
  context: ContextField[];
  source: ExampleSource | ''; examples: Example[]; dataSet: DataSetState; moreLater: string;
  callsPerDay: string; speedMatters: YesNo; accuracy: string; abstainBetter: YesNo;
  mayShare: boolean; noPersonalData: boolean; mayImproveBase: boolean;
}

const twoOptions = (): OptionDef[] => [{ key: '', definition: '' }, { key: '', definition: '' }];

const EMPTY: Draft = {
  question: '', why: '', kind: '', options: twoOptions(), noneFits: '', exampleSets: [twoOptions(), twoOptions(), twoOptions()],
  inputForm: '', inputFields: [], typicalLength: '', maxLength: '', languages: '', context: [], source: '', examples: [], dataSet: { kind: 'none' }, moreLater: '',
  callsPerDay: '', speedMatters: '', accuracy: '', abstainBetter: '', mayShare: false, noPersonalData: false, mayImproveBase: false,
};

// A box's text as a whole number, or NaN (which the rules refuse, naming the field) when it is not one.
const wholeNumber = (text: string): number => (/^\s*\d+\s*$/.test(text) ? Number(text) : Number.NaN);
const yesNo = (value: YesNo): boolean | null => (value === '' ? null : value === 'yes');

// What the request says of a checked data set before it is uploaded: the name it is asked for as (the hub picks the
// zip's own name when it issues the upload, and the page puts the stored name in its place on Send), its size and the rows counted in it.
function dataSetOf(d: Draft, folder: string) {
  if (d.dataSet.kind !== 'checked') return null;
  return { pathname: `${folder}${DATA_SET_NAME}`, bytes: d.dataSet.file.size, rows: d.dataSet.result.rows, counts: d.dataSet.result.counts };
}

// The draft as the request the rules check and the hub receives. A choice not made yet is null, which they refuse.
function requestOf(d: Draft, folder: string) {
  const fixed = d.kind !== '' && d.kind !== 'caller';
  return {
    decision: { question: d.question, why: d.why },
    answer: { kind: d.kind === '' ? null : d.kind, options: fixed ? d.options : [], noneFits: d.noneFits === '' ? null : d.noneFits,
      exampleSets: d.kind === 'caller' ? d.exampleSets : [] },
    input: { form: d.inputForm === '' ? null : d.inputForm, fields: d.inputForm === 'fields' ? d.inputFields : [],
      typicalLength: wholeNumber(d.typicalLength), maxLength: wholeNumber(d.maxLength), languages: d.languages },
    context: d.context,
    source: d.source === '' ? null : d.source,
    examples: d.source === 'data_set' ? [] : d.examples,
    dataSet: d.source === 'data_set' ? dataSetOf(d, folder) : null,
    moreExamplesLater: wholeNumber(d.moreLater),
    use: { callsPerDay: wholeNumber(d.callsPerDay), speedMatters: yesNo(d.speedMatters), accuracy: d.accuracy, abstainBetter: yesNo(d.abstainBetter) },
    rights: { mayShare: d.mayShare, noPersonalData: d.noPersonalData, mayImproveBase: d.mayImproveBase },
  };
}

// The options of a fixed kind as the "none" choice needs them: a "none" option added when it is chosen.
function withNone(options: OptionDef[], noneFits: NoneFits | ''): OptionDef[] {
  if (noneFits !== 'none_option' || options.some((o) => o.key === NONE_KEY)) return options;
  return [...options, { key: NONE_KEY, definition: '' }];
}

function yesNoOptions(options: OptionDef[], noneFits: NoneFits | ''): OptionDef[] {
  const definition = (key: string) => options.find((o) => o.key === key)?.definition ?? '';
  return withNone([{ key: 'yes', definition: definition('yes') }, { key: 'no', definition: definition('no') }], noneFits)
    .map((o) => (o.key === NONE_KEY ? { key: NONE_KEY, definition: definition(NONE_KEY) } : o));
}

const YES_NO = [{ value: 'yes', label: 'Yes' }, { value: 'no', label: 'No' }] as const;

function Card({ number, children, problems }: { number: Section; children: ReactNode; problems: Problem[] }) {
  return (
    <section className={`mt-8 p-5 sm:p-6 ${CARD}`} aria-labelledby={`section-${number}`}>
      <h2 id={`section-${number}`} className={SECTION_HEADING}>{`${number}. ${SECTIONS[number]}`}</h2>
      {children}
      <Problems problems={problems} />
    </section>
  );
}

function NamedFieldsEditor({ title, fields, onChange, withExample }: {
  title: string; fields: (NamedField | ContextField)[]; onChange: (fields: ContextField[]) => void; withExample: boolean;
}) {
  const set = (i: number, change: Partial<ContextField>) => onChange(fields.map((f, j) => (j === i ? { ...f, ...change } as ContextField : f as ContextField)));
  return (
    <div className="mt-3 flex flex-col gap-3">
      {fields.map((field, i) => (
        <div key={i} className="flex flex-col gap-2 border border-rule p-3">
          <div className="flex flex-col gap-2 sm:flex-row">
            <input aria-label={`${title} ${i + 1}: name`} value={field.name} onChange={(e) => set(i, { name: e.target.value })} placeholder="name, e.g. subject"
              autoComplete="off" className="w-full min-w-0 border border-ink bg-background px-3 py-2 font-mono text-sm placeholder:text-faint sm:w-44" />
            <input aria-label={`${title} ${i + 1}: description`} value={field.description} onChange={(e) => set(i, { description: e.target.value })}
              placeholder="What it holds, in one line" autoComplete="off" className="w-full min-w-0 border border-ink bg-background px-3 py-2 text-sm placeholder:text-faint" />
          </div>
          {withExample && (
            <input aria-label={`${title} ${i + 1}: example value`} value={(field as ContextField).example ?? ''} onChange={(e) => set(i, { example: e.target.value })}
              placeholder="An example value" autoComplete="off" className="w-full min-w-0 border border-ink bg-background px-3 py-2 text-sm placeholder:text-faint" />
          )}
          <div>
            <button type="button" className="font-mono text-xs text-primary underline" onClick={() => onChange(fields.filter((_, j) => j !== i) as ContextField[])}>
              {`Remove ${title.toLowerCase()} ${i + 1}`}
            </button>
          </div>
        </div>
      ))}
    </div>
  );
}

type PageState = { kind: 'checking' } | { kind: 'failed'; message: string } | { kind: 'form'; folder: string } | { kind: 'sent'; id: string };

export default function RequestAdapter() {
  const navigate = useNavigate();
  const [page, setPage] = useState<PageState>({ kind: 'checking' });
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [tried, setTried] = useState(false);
  const [sending, setSending] = useState(false);
  // The data set's upload, in percent, while it runs.
  const [uploading, setUploading] = useState<number | null>(null);
  const [refusal, setRefusal] = useState<{ message: string; problems: Problem[] } | null>(null);
  const summaryRef = useRef<HTMLDivElement>(null);
  const sentRef = useRef<HTMLHeadingElement>(null);

  useEffect(() => {
    let live = true;
    void askHub('/api/adapter-requests', null).then((answer) => {
      if (!live) return;
      if (answer.kind === 'answer' && answer.status === 401) { navigate(signInFor('/request-adapter'), { replace: true }); return; }
      const folder = (answer.kind === 'answer' && answer.status === 200 ? answer.body as Partial<OwnRequests> | null : null)?.dataSetFolder;
      if (typeof folder === 'string') { setPage({ kind: 'form', folder }); return; }
      setPage({ kind: 'failed', message: `Could not check that you are signed in. ${refusalOf(answer)}` });
    });
    return () => { live = false; };
  }, [navigate]);

  useEffect(() => { if (page.kind === 'sent') sentRef.current?.focus(); }, [page.kind]);

  const folder = page.kind === 'form' ? page.folder : '';
  const shape: Shape = draft.kind === '' || draft.noneFits === '' || draft.inputForm === '' ? null : {
    answer: { kind: draft.kind, options: draft.kind === 'caller' ? [] : draft.options, noneFits: draft.noneFits, exampleSets: draft.kind === 'caller' ? draft.exampleSets : [] },
    input: { form: draft.inputForm, fields: draft.inputForm === 'fields' ? draft.inputFields : [], typicalLength: 0, maxLength: 0, languages: '' },
    context: draft.context,
  };

  const request = useMemo(() => requestOf(draft, folder), [draft, folder]);
  const checked = useMemo(() => checkAdapterRequest(request), [request]);
  const bytes = useMemo(() => bodyBytes(request), [request]);
  const problems = checked.ok ? [] : checked.problems;
  const inSection = (n: Section) => (tried ? problems.filter((p) => p.section === n) : []);
  // Sections 2 to 5 as the files of a data set must follow them, once they are right (null until then).
  const shapeRight = !problems.some((p) => p.section >= 2 && p.section <= 5);
  const maxLength = /^\s*\d+\s*$/.test(draft.maxLength) ? Number(draft.maxLength) : null;
  const dataSetFresh = draft.dataSet.kind === 'checked' && shape !== null && maxLength !== null && draft.dataSet.key === keyOf(shape, maxLength);
  const ready = draft.kind !== '' && bytes <= LIMITS.bodyBytesMax && (draft.source === 'data_set'
    ? dataSetFresh
    : draft.source === 'generate'
      && meetsExampleMinimums({ answer: { kind: draft.kind, options: draft.kind === 'caller' ? [] : draft.options }, examples: draft.examples }));

  const update = (change: Partial<Draft>) => setDraft((d) => ({ ...d, ...change }));
  const setKind = (kind: AnswerKind) => setDraft((d) => ({
    ...d, kind,
    options: kind === 'yes_no' ? yesNoOptions(d.options, d.noneFits) : d.kind === 'yes_no' ? withNone(twoOptions(), d.noneFits) : withNone(d.options, d.noneFits),
  }));
  const setNoneFits = (noneFits: NoneFits) => setDraft((d) => ({
    ...d, noneFits,
    options: d.kind === 'yes_no' ? yesNoOptions(d.options, noneFits) : withNone(d.options, noneFits),
    exampleSets: d.exampleSets.map((set) => withNone(set, noneFits)),
  }));

  const send = async () => {
    setRefusal(null);
    if (!checked.ok) {
      setTried(true);
      summaryRef.current?.focus();
      return;
    }
    setSending(true);
    try {
      let form = checked.form;
      if (form.dataSet !== null && draft.dataSet.kind === 'checked') {
        setUploading(0);
        const uploaded = await uploadDataSet(draft.dataSet.file, folder, setUploading);
        setUploading(null);
        if (uploaded.kind === 'refused') { setRefusal({ message: uploaded.message, problems: [] }); return; }
        form = { ...form, dataSet: { ...form.dataSet, pathname: uploaded.pathname } };
      }
      const answer = await askHub('/api/adapter-requests', { body: form }, SEND_TIMEOUT_MS);
      if (answer.kind === 'answer' && answer.status === 401) { navigate(signInFor('/request-adapter'), { replace: true }); return; }
      if (answer.kind === 'answer' && answer.status === 201 && typeof (answer.body as CreatedRequest | null)?.id === 'string') {
        setPage({ kind: 'sent', id: (answer.body as CreatedRequest).id });
        window.scrollTo(0, 0);
        return;
      }
      const details = answer.kind === 'answer' ? (answer.body as { error?: { details?: { problems?: Problem[] } } } | null)?.error?.details : undefined;
      setRefusal({ message: refusalOf(answer), problems: Array.isArray(details?.problems) ? details.problems : [] });
    } finally {
      setSending(false);
      setUploading(null);
    }
  };

  const intro = (
    <p className="mt-3 max-w-[72ch] text-sm leading-6 text-soft">
      Need a decision that is not in the <Link to="/catalogue" className={TEXT_LINK}>catalogue</Link>? Describe it and give
      labelled examples. We train the adapter and deliver it privately to your account.
    </p>
  );

  if (page.kind === 'sent') {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-12 sm:py-16">
        <h1 ref={sentRef} tabIndex={-1} className={PAGE_TITLE}>Request sent</h1>
        <section className={`mt-8 p-5 sm:p-6 ${CARD}`} aria-label="Your request">
          <p className="text-text">{"Thank you. We'll reply within a few days by email."}</p>
          <p className="mt-3 font-mono text-sm text-soft">Request number: <span className="text-text">{page.id}</span></p>
          <p className="mt-4 text-sm text-soft">
            You can follow it on <Link to="/account" className={TEXT_LINK}>your account</Link>.
          </p>
        </section>
      </main>
    );
  }

  if (page.kind !== 'form') {
    return (
      <main id="main" className="mx-auto max-w-3xl px-4 py-12 sm:py-16">
        <h1 className={PAGE_TITLE}>Request an adapter</h1>
        {intro}
        {page.kind === 'checking'
          ? <p role="status" className="mt-6 text-sm text-soft">{CHECKING}</p>
          : <p role="alert" className="mt-6 text-sm text-danger">{page.message}</p>}
      </main>
    );
  }

  const fixed = draft.kind !== '' && draft.kind !== 'caller';
  return (
    <main id="main" className="mx-auto max-w-3xl px-4 py-12 sm:py-16">
      <h1 className={PAGE_TITLE}>Request an adapter</h1>
      {intro}

      <Card number={1} problems={inSection(1)}>
        <TextField label="The question, in one sentence" value={draft.question} onChange={(question) => update({ question })}
          maxLength={LIMITS.questionMax} placeholder="e.g. Which team should handle this support email?"
          hint={`${LIMITS.questionMin} to ${LIMITS.questionMax} characters.`} />
        <TextField label="Why it matters, in one or two sentences" value={draft.why} onChange={(why) => update({ why })} multiline maxLength={LIMITS.whyMax} />
      </Card>

      <Card number={2} problems={inSection(2)}>
        <Choice legend="What kind of answer should it give?" value={draft.kind} onChange={setKind}
          options={ANSWER_KINDS.map((kind) => ({ value: kind, label: ANSWER_KIND_LABELS[kind] }))} />
      </Card>

      <Card number={3} problems={inSection(3)}>
        {draft.kind === '' && <p className="mt-3 text-sm text-soft">Choose the kind of answer first.</p>}
        {fixed && (
          <>
            <p className="mt-3 text-sm text-soft">
              {draft.kind === 'yes_no'
                ? 'Say when yes is right and when no is right.'
                : `${LIMITS.optionsMin} to ${LIMITS.optionsMax} options, each a short key (lower-case letters, digits and underscores) and when it is right.`}
            </p>
            <OptionsEditor title="Options" options={draft.options} onChange={(options) => update({ options })} fixedKeys={draft.kind === 'yes_no'} max={LIMITS.optionsMax} />
          </>
        )}
        {draft.kind === 'caller' && (
          <>
            <p className="mt-3 text-sm text-soft">The caller gives the options with every call. Give three example option sets, as a caller would.</p>
            {draft.exampleSets.map((set, i) => (
              <OptionsEditor key={i} title={`Example option set ${i + 1}`} options={set} max={LIMITS.optionsMax}
                onChange={(options) => update({ exampleSets: draft.exampleSets.map((s, j) => (j === i ? options : s)) })} />
            ))}
          </>
        )}
        <Choice legend="What should happen when no option fits?" value={draft.noneFits} onChange={setNoneFits}
          options={[{ value: 'none_option', label: `A "${NONE_KEY}" option` }, { value: 'abstain', label: 'No answer (abstain)' }]} />
      </Card>

      <Card number={4} problems={inSection(4)}>
        <Choice legend="What does the adapter decide on?" value={draft.inputForm}
          onChange={(inputForm) => update({ inputForm, inputFields: inputForm === 'fields' && draft.inputFields.length === 0 ? [{ name: '', description: '' }] : draft.inputFields })}
          options={[{ value: 'text', label: 'One text' }, { value: 'fields', label: 'Named fields' }]} />
        {draft.inputForm === 'fields' && (
          <>
            <NamedFieldsEditor title="Input field" fields={draft.inputFields} withExample={false}
              onChange={(fields) => update({ inputFields: fields.map(({ name, description }) => ({ name, description })) })} />
            {draft.inputFields.length < LIMITS.inputFieldsMax && (
              <button type="button" className="mt-3 font-mono text-sm text-primary underline"
                onClick={() => update({ inputFields: [...draft.inputFields, { name: '', description: '' }] })}>Add an input field</button>
            )}
          </>
        )}
        <div className="grid gap-x-4 sm:grid-cols-2">
          <TextField label="Typical length (characters)" value={draft.typicalLength} onChange={(typicalLength) => update({ typicalLength })} inputMode="numeric" placeholder="e.g. 800" />
          <TextField label="Maximum length (characters)" value={draft.maxLength} onChange={(maxLength) => update({ maxLength })} inputMode="numeric" placeholder="e.g. 5000" />
        </div>
        <TextField label="Language or languages" value={draft.languages} onChange={(languages) => update({ languages })} maxLength={LIMITS.languagesMax} placeholder="e.g. English and German" />
      </Card>

      <Card number={5} problems={inSection(5)}>
        <p className="mt-3 text-sm text-soft">
          Optional. Fields that stay the same across many calls, such as a list of teams or a policy, each with an
          example value.
        </p>
        <NamedFieldsEditor title="Context field" fields={draft.context} withExample onChange={(context) => update({ context })} />
        {draft.context.length < LIMITS.contextFieldsMax && (
          <button type="button" className="mt-3 font-mono text-sm text-primary underline"
            onClick={() => update({ context: [...draft.context, { name: '', description: '', example: '' }] })}>Add a context field</button>
        )}
      </Card>

      <Card number={6} problems={inSection(6)}>
        <Choice legend="How will you give the examples?" value={draft.source} onChange={(source) => update({ source })}
          options={(['data_set', 'generate'] as const).map((source) => ({ value: source, label: EXAMPLE_SOURCE_LABELS[source] }))} />
        {draft.source === 'data_set' && (
          <DataSetSection shape={shapeRight ? shape : null} maxLength={shapeRight ? maxLength : null}
            state={draft.dataSet} onChange={(dataSet) => setDraft((d) => ({ ...d, dataSet }))} />
        )}
        {draft.source === 'generate' && (
          <>
            <p className="mt-4 text-sm text-soft">{GENERATE_LINE}</p>
            <ExamplesSection shape={shape} examples={draft.examples} onChange={(examples) => update({ examples })} bytes={bytes} />
          </>
        )}
        {draft.source !== '' && (
          <TextField label="How many more real examples could you supply later?" value={draft.moreLater} onChange={(moreLater) => update({ moreLater })}
            inputMode="numeric" placeholder="e.g. 500" />
        )}
      </Card>

      <Card number={7} problems={inSection(7)}>
        <TextField label="Calls a day, roughly" value={draft.callsPerDay} onChange={(callsPerDay) => update({ callsPerDay })} inputMode="numeric" placeholder="e.g. 2000" />
        <Choice legend="Does speed matter (an answer in under 50 ms)?" value={draft.speedMatters} onChange={(speedMatters) => update({ speedMatters })} options={YES_NO} />
        <TextField label="What accuracy would make it useful?" value={draft.accuracy} onChange={(accuracy) => update({ accuracy })} maxLength={LIMITS.accuracyMax}
          placeholder="e.g. right 95 times in 100" />
        <Choice legend="Is no answer better than a wrong answer?" value={draft.abstainBetter} onChange={(abstainBetter) => update({ abstainBetter })} options={YES_NO} />
      </Card>

      <Card number={8} problems={inSection(8)}>
        <Tick checked={draft.mayShare} onChange={(mayShare) => update({ mayShare })}>I have the right to share these examples.</Tick>
        <Tick checked={draft.noPersonalData} onChange={(noPersonalData) => update({ noPersonalData })}>They contain no personal data, or it has been removed.</Tick>
        <Tick checked={draft.mayImproveBase} onChange={(mayImproveBase) => update({ mayImproveBase })}>
          {"Optional: you may also use these examples to improve Jeff's base model."}
        </Tick>
        <p className="mt-4 text-sm text-soft">{OWN_MODEL_LINE}</p>
        <p className="mt-2 text-sm text-soft">{RETENTION_LINE}</p>
      </Card>

      <div ref={summaryRef} tabIndex={-1} className="mt-8 outline-none">
        {tried && problems.length > 0 && (
          <p role="alert" className="text-sm text-danger">
            {`${problems.length} ${problems.length === 1 ? 'thing needs' : 'things need'} fixing before the request can be sent; each is shown in its section above.`}
          </p>
        )}
        {refusal !== null && (
          <div role="alert" className="text-sm text-danger">
            <p>{refusal.message}</p>
            {refusal.problems.length > 1 && <ul className="mt-2 list-disc pl-5">{refusal.problems.slice(0, 10).map((p, i) => <li key={i}>{p.message}</li>)}</ul>}
          </div>
        )}
        {!ready && (
          <p className="mt-2 text-sm text-soft">
            {draft.source === ''
              ? 'Send opens once you choose how to give the examples (section 6) and they meet the minimums.'
              : draft.source === 'data_set'
                ? `Send opens once your data set is checked and has at least ${LIMITS.dataSetRowsMin} rows and ${LIMITS.dataSetPerOptionMin} for every option.`
                : `Send opens once there are at least ${LIMITS.examplesMin} examples and ${LIMITS.perOptionMin} for every option.`}
          </p>
        )}
        {uploading !== null && <p role="status" className="mt-2 font-mono text-sm text-soft">{`Uploading the data set: ${uploading}%`}</p>}
        <button type="button" disabled={!ready || sending} onClick={() => void send()} className={`mt-4 ${BUTTON_PRIMARY} px-4 py-2 disabled:opacity-60`}>
          {sending ? 'Sending' : SEND_LABEL}
        </button>
      </div>
    </main>
  );
}
