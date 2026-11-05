// /playground: pick an adapter and one of its samples (the worked examples the playground takes,
// playground-samples.server.ts), edit the input, run it live, and see the answer with its confidence and the same
// call as code. Prerendered like every page of the hub: the samples and their code are fixed at build time, and every
// answer comes from a live call to the hub's function (POST /api/playground/decide, server/endpoints/playground.ts).
//
// What is learned in the browser, after hydration, so the first render matches the prerendered markup:
// - the query (?adapter=<adapter id>&sample=<sample id>, the "Open in playground" links), read once on arrival as the
//   catalogue reads its own; a query naming something the playground does not have is said on the page. Choosing an
//   adapter or a sample afterwards writes the query (replacing the history entry), so the address can be shared;
// - who is running: GET /api/signin-options, then, when signed in, GET /api/account, whose view says whether the
//   subscription may use keys (as the hub's function decides). A visitor, or an account without such a
//   subscription, is shown the bot check (Cloudflare Turnstile) before the first run; a subscriber runs on their own
//   account and needs none. Run waits until this is known, so the check does not flash. When it cannot be learned the
//   page says so and shows the check; the hub's function still decides, and asks for the check by its refusal.
//
// Every answer the endpoint gives is said in plain words: a run, a refusal (its own message; a countdown when it says
// when to try again), a hub error (its message), an answer that is not JSON (the status), and no answer at all. A run
// is also said in a polite live region, and the answer's heading takes the focus.
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useLoaderData, useSearchParams } from 'react-router';
import { CallCode } from '../components/docs/Example';
import { Result, type RunAnswer } from '../components/playground/Result';
import { BUTTON_PRIMARY, CARD, PAGE_TITLE, SECTION_HEADING } from '../components/styles';
import { Turnstile } from '../components/Turnstile';
import { localAdapters } from '../lib/catalogue-call.server';
import type { ExampleBlock } from '../lib/example-block';
import { callCode, localCallOf } from '../lib/examples.server';
import { MAX_INPUT_CHARACTERS, MAX_INPUT_DEPTH } from '../lib/playground.server';
import { playgroundLanguages, playgroundSamples } from '../lib/playground-samples.server';
import { publicSettings } from '../lib/public-settings.server';
import { settings } from '../lib/settings.server';
import { catalogue } from '../lib/site.server';
import { TURNSTILE_HEADER_NAME } from '../lib/turnstile-header';

// How long the page waits for the hub's answer to a run: the hub waits 30 seconds for the API (playground.server.ts:
// API_TIMEOUT_MS), and the rest is for the hub itself.
export const RUN_TIMEOUT_MS = 40_000;
// How long it waits for the hub to say who is running.
export const WHO_TIMEOUT_MS = 10_000;

export function meta() {
  return [
    { title: 'Playground | Jeff' },
    { name: 'description', content: 'Run a Jeff adapter live on a sample input, edit it, and see the answer and the call as code.' },
  ];
}

// The page's data is fixed at build time: choosing a sample (which writes the query) never needs it again.
export function shouldRevalidate() {
  return false;
}

interface SampleData {
  id: string;
  // What the case is, for the menu (the example's name).
  name: string;
  function: string;
  // The fixed context, as pretty JSON; null when the sample passes none.
  context: string | null;
  // The input as the box shows it: a string as itself, anything else as pretty JSON (then `inputIsJson`).
  input: string;
  inputIsJson: boolean;
  // What the input is, in the manifest's words (the function's own input when it has one, else the adapter's), shown
  // under the Input label: for some adapters the input makes sense only beside the context (assistant-routing's
  // "Reply to the user in words.").
  inputHint: string;
  code: RunAnswer['code'];
  curl: string;
  // The same call "On your machine" (examples.server.ts: localCallOf); null for a proprietary adapter or a
  // via-http sample, which offer no choice.
  local: ExampleBlock['local'];
}

interface AdapterData { id: string; title: string; samples: SampleData[] }

export function loader() {
  // A sample's adapter is the name its function starts with (CatalogueEntry.name: a router's unversioned ID).
  const titles = new Map(catalogue().map((entry) => [entry.name, entry.title]));
  const entries = new Map(catalogue().map((entry) => [entry.name, entry]));
  const apiOrigin = settings().apiOrigin;
  const languages = playgroundLanguages();
  const localOf = localAdapters(catalogue());
  const adapters: AdapterData[] = [];
  for (const sample of playgroundSamples()) {
    const title = titles.get(sample.adapter);
    if (title === undefined) throw new Error(`Playground sample ${sample.id} is for ${sample.adapter}, which the catalogue does not list`);
    if (sample.example.name === null) throw new Error(`Playground sample ${sample.id} has no name in content/examples (the menu shows it)`);
    let group = adapters.find((adapter) => adapter.id === sample.adapter);
    if (group === undefined) {
      group = { id: sample.adapter, title, samples: [] };
      adapters.push(group);
    }
    const { code, curl } = callCode(sample.example, apiOrigin, languages);
    if (curl === null) throw new Error(`Playground sample ${sample.id} has no curl: the playground takes only examples the API serves`);
    const { input, context } = sample.example;
    const entry = entries.get(sample.adapter);
    if (entry === undefined) throw new Error(`Playground sample ${sample.id} is for ${sample.adapter}, which the catalogue does not list`);
    const fn = entry.functions.find((f) => f.id === sample.example.function);
    if (fn === undefined) throw new Error(`Playground sample ${sample.id} calls ${sample.example.function}, which ${entry.id} does not have`);
    // A function's own input replaces the adapter's (the manifest's rule), so this is no default.
    const inputField = fn.input === null ? entry.input : fn.input;
    group.samples.push({
      id: sample.id,
      name: sample.example.name,
      function: sample.example.function,
      context: context === null || Object.keys(context).length === 0 ? null : JSON.stringify(context, null, 2),
      input: typeof input === 'string' ? input : JSON.stringify(input, null, 2),
      inputIsJson: typeof input !== 'string',
      inputHint: inputField.description,
      code,
      curl,
      local: localCallOf(sample.example, languages, localOf),
    });
  }
  // The menu lists adapters by title, A to Z (owner, 5 November 2026); each adapter's samples keep their file order.
  adapters.sort((a, b) => a.title.localeCompare(b.title, 'en'));
  const shown = publicSettings();
  return { adapters, siteKey: shown.turnstileSiteKey, runsPerMinute: shown.playgroundRunsPerMinute,
    maxInput: MAX_INPUT_CHARACTERS, maxDepth: MAX_INPUT_DEPTH };
}

// Who runs: a visitor; an account signed in whose subscription may not use keys (it runs as a visitor); a subscriber
// (on their own account); or not known, and why.
type Who = { kind: 'checking' } | { kind: 'visitor' } | { kind: 'signed-in' } | { kind: 'subscriber' } | { kind: 'unknown'; reason: string };

// One GET to the hub's function: its status and JSON body, or why there was none.
async function getJson(path: string): Promise<{ status: number; body: unknown } | { failed: string }> {
  let status: number;
  let type: string | null;
  let text: string;
  try {
    const response = await fetch(path, { credentials: 'same-origin', headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(WHO_TIMEOUT_MS) });
    status = response.status;
    type = response.headers.get('content-type');
    text = await response.text();
  } catch (error) {
    if (typeof error === 'object' && error !== null && (error as { name?: unknown }).name === 'TimeoutError') {
      return { failed: `the hub did not answer within ${WHO_TIMEOUT_MS / 1000} seconds` };
    }
    if (error instanceof TypeError) return { failed: `the request failed (${error.message})` };
    throw error;
  }
  if (type === null || type.split(';')[0].trim() !== 'application/json') return { failed: `the hub answered ${status} without JSON` };
  try {
    return { status, body: JSON.parse(text) };
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return { failed: `the hub answered ${status} with JSON that does not parse` };
  }
}

async function whoRuns(): Promise<Who> {
  const options = await getJson('/api/signin-options');
  if ('failed' in options) return { kind: 'unknown', reason: options.failed };
  const signedIn = (options.body as { signedIn?: unknown } | null)?.signedIn;
  if (options.status !== 200 || typeof signedIn !== 'boolean') return { kind: 'unknown', reason: `the hub answered ${options.status}` };
  if (!signedIn) return { kind: 'visitor' };
  const account = await getJson('/api/account');
  if ('failed' in account) return { kind: 'unknown', reason: account.failed };
  // The session ended between the two requests.
  if (account.status === 401) return { kind: 'visitor' };
  const view = (account.body as { view?: unknown } | null)?.view;
  if (account.status === 200 && view === 'account') return { kind: 'subscriber' };
  if (account.status === 200 && view === 'subscribe') return { kind: 'signed-in' };
  return { kind: 'unknown', reason: `the hub answered ${account.status} for your account` };
}

type Refusal = { kind: string; message: string; retryAfterS: number | null };
type Outcome =
  | { kind: 'answer'; answer: RunAnswer }
  | { kind: 'refused'; refusal: Refusal }
  | { kind: 'failed'; message: string };

function isRun(body: unknown): body is RunAnswer {
  const run = body as Partial<RunAnswer> | null;
  return run !== null && typeof run === 'object' && (run.ran_as === 'visitor' || run.ran_as === 'account')
    && typeof run.latency_ms === 'number' && Array.isArray(run.code) && typeof run.curl === 'string'
    && (run.local === null || typeof run.local === 'object')
    && run.decision !== null && typeof run.decision === 'object' && typeof run.decision.probabilities === 'object'
    && run.decision.probabilities !== null;
}

function isRefusal(value: unknown): value is Refusal {
  const refusal = value as Partial<Refusal> | null;
  return refusal !== null && typeof refusal === 'object' && typeof refusal.kind === 'string' && typeof refusal.message === 'string'
    && (refusal.retryAfterS === null || typeof refusal.retryAfterS === 'number');
}

function sentence(text: string): string {
  const capital = text.charAt(0).toUpperCase() + text.slice(1);
  return /[.!?]$/.test(capital) ? capital : `${capital}.`;
}

// One run: the hub's answer in one of the shapes of server/endpoints/playground.ts, or why there was none.
async function ask(sample: string, input: unknown, token: string | null): Promise<Outcome> {
  const headers: Record<string, string> = { accept: 'application/json', 'content-type': 'application/json' };
  if (token !== null) headers[TURNSTILE_HEADER_NAME] = token;
  let status: number;
  let type: string | null;
  let text: string;
  try {
    const response = await fetch('/api/playground/decide', { method: 'POST', credentials: 'same-origin', headers,
      body: JSON.stringify({ sample, input }), signal: AbortSignal.timeout(RUN_TIMEOUT_MS) });
    status = response.status;
    type = response.headers.get('content-type');
    text = await response.text();
  } catch (error) {
    // AbortSignal.timeout's error is a DOMException named TimeoutError (compared by name, as /account does); a failed
    // send or a body cut off is a TypeError.
    if (typeof error === 'object' && error !== null && (error as { name?: unknown }).name === 'TimeoutError') {
      return { kind: 'failed', message: `The hub did not answer within ${RUN_TIMEOUT_MS / 1000} seconds. Run it again.` };
    }
    if (error instanceof TypeError) return { kind: 'failed', message: `The hub could not be reached: ${error.message}. Check your connection and run it again.` };
    throw error;
  }
  if (type === null || type.split(';')[0].trim() !== 'application/json') {
    return { kind: 'failed', message: `The playground failed: the hub answered ${status} without JSON. Try again in a minute.` };
  }
  let body: unknown;
  try {
    body = JSON.parse(text);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return { kind: 'failed', message: `The playground failed: the hub answered ${status} with JSON that does not parse. Try again in a minute.` };
  }
  const shaped = body as { refusal?: unknown; error?: { message?: unknown } } | null;
  if (status === 200 && isRun(body)) return { kind: 'answer', answer: body };
  if (shaped !== null && isRefusal(shaped.refusal)) return { kind: 'refused', refusal: shaped.refusal };
  if (shaped !== null && typeof shaped.error === 'object' && shaped.error !== null && typeof shaped.error.message === 'string') {
    return { kind: 'failed', message: `The playground failed: ${sentence(shaped.error.message)}` };
  }
  return { kind: 'failed', message: `The playground failed: the hub answered ${status} with nothing the page can read. Try again in a minute.` };
}

// Seconds left of a wait of `wait.seconds` that ends at `wait.until` (a time in milliseconds), counting down once a
// second; null when there is nothing to wait for, or the wait is over. Never more than the wait itself: the clock this
// component last read may be from before the wait began.
function useCountdown(wait: { until: number; seconds: number } | null): number | null {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (wait === null) return;
    setNow(Date.now());
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [wait]);
  if (wait === null) return null;
  const left = Math.min(wait.seconds, Math.ceil((wait.until - now) / 1000));
  return left > 0 ? left : null;
}

// The deepest nesting of lists and maps in a value (a plain value is 0), counted no further than `limit` + 1: the
// server's own rule (playground.server.ts: depthOf).
function depthOf(value: unknown, limit: number): number {
  if (value === null || typeof value !== 'object') return 0;
  if (limit < 0) return 1;
  let deepest = 0;
  for (const item of Array.isArray(value) ? value : Object.values(value)) {
    deepest = Math.max(deepest, depthOf(item, limit - 1));
    if (deepest > limit) break;
  }
  return deepest + 1;
}

// The input as the hub's function measures it: text, its characters (code points); JSON, its value's JSON written
// out again. JSON that does not parse is measured as text (Run then says it is not valid JSON).
function measure(text: string, asJson: boolean, maxDepth: number): { length: number; depth: number } {
  if (!asJson) return { length: [...text].length, depth: 0 };
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error;
    return { length: [...text].length, depth: 0 };
  }
  return { length: [...JSON.stringify(value)].length, depth: depthOf(value, maxDepth) };
}

const FIELD_LABEL = 'block font-mono text-sm text-soft';
const SELECT = 'mt-1 w-full min-w-0 border border-ink bg-panel px-3 py-2 font-mono text-sm text-text';
const numbers = (n: number) => n.toLocaleString('en-US');
// Shown under the code panel whenever it shows "On your machine": the Run button above always calls the API, so
// this is the same call made on the reader's own machine, not what the run above just did.
const RUN_NOTE = 'Running above always calls the API, whichever code this panel shows; this is the same call made on your own machine.';

export default function Playground() {
  const { adapters, siteKey, runsPerMinute, maxInput, maxDepth } = useLoaderData<typeof loader>();
  const [params, setParams] = useSearchParams();
  const [hydrated, setHydrated] = useState(false);
  const [who, setWho] = useState<Who>({ kind: 'checking' });
  const [adapterId, setAdapterId] = useState(adapters[0].id);
  const [sampleId, setSampleId] = useState(adapters[0].samples[0].id);
  const [inputText, setInputText] = useState(adapters[0].samples[0].input);
  const [asJson, setAsJson] = useState(adapters[0].samples[0].inputIsJson);
  const [linkNote, setLinkNote] = useState<string | null>(null);
  const [token, setToken] = useState<string | null>(null);
  const [widget, setWidget] = useState(0);
  // A visitor needs the bot check until the hub has given the pass cookie (it does once it accepts a token, so after
  // any answer but a bot-check or input refusal), and again whenever the hub asks for it.
  const [passed, setPassed] = useState(false);
  const [checkAsked, setCheckAsked] = useState(false);
  const [running, setRunning] = useState(false);
  const [outcome, setOutcome] = useState<Outcome | null>(null);
  // Each run's number, so a refusal said twice in a row is announced twice.
  const [attempt, setAttempt] = useState(0);
  const [live, setLive] = useState('');
  const [wait, setWait] = useState<{ until: number; seconds: number } | null>(null);
  const answerHeading = useRef<HTMLHeadingElement>(null);
  const onToken = useCallback((value: string | null) => setToken(value), []);
  const waitLeft = useCountdown(wait);

  const adapter = adapters.find((a) => a.id === adapterId);
  if (adapter === undefined) throw new Error(`The playground has no adapter ${adapterId}`);
  const sample = adapter.samples.find((s) => s.id === sampleId);
  if (sample === undefined) throw new Error(`The playground's adapter ${adapterId} has no sample ${sampleId}`);

  const select = useCallback((group: AdapterData, picked: SampleData) => {
    setAdapterId(group.id);
    setSampleId(picked.id);
    setInputText(picked.input);
    setAsJson(picked.inputIsJson);
    setOutcome(null);
  }, []);

  // Chosen on the page: the query follows, so the address names what is shown.
  const choose = (group: AdapterData, picked: SampleData) => {
    select(group, picked);
    setLinkNote(null);
    setParams({ adapter: group.id, sample: picked.id }, { replace: true, preventScrollReset: true });
  };

  // After hydration, once: the adapter and sample the address names on arrival (a sample alone names its adapter; an
  // adapter alone, its first sample). The empty dependency list is the point: choosing on the page writes the query
  // (choose above), and that must not be read back here.
  useEffect(() => {
    setHydrated(true);
    const wantAdapter = params.get('adapter');
    const wantSample = params.get('sample');
    if (wantAdapter === null && wantSample === null) return;
    const group = wantSample !== null
      ? adapters.find((a) => a.samples.some((s) => s.id === wantSample))
      : adapters.find((a) => a.id === wantAdapter);
    if (group === undefined || (wantAdapter !== null && group.id !== wantAdapter)) {
      const named = [wantAdapter === null ? null : `adapter ${wantAdapter}`, wantSample === null ? null : `sample ${wantSample}`]
        .filter((part) => part !== null).join(' and ');
      setLinkNote(`The playground has no ${named}. Choose an adapter and a sample below.`);
      return;
    }
    const picked = wantSample === null ? group.samples[0] : group.samples.find((s) => s.id === wantSample);
    if (picked === undefined) throw new Error(`The playground's adapter ${group.id} lost its sample ${wantSample}`);
    select(group, picked);
  }, []);

  // After hydration: who runs.
  useEffect(() => {
    let current = true;
    whoRuns().then((found) => { if (current) setWho(found); }, (error: unknown) => {
      if (current) setWho({ kind: 'unknown', reason: error instanceof Error ? error.message : String(error) });
      throw error;
    });
    return () => { current = false; };
  }, []);

  // A countdown that has run out is cleared.
  useEffect(() => {
    if (wait !== null && waitLeft === null) setWait(null);
  }, [wait, waitLeft]);

  const answer = outcome !== null && outcome.kind === 'answer' ? outcome.answer : null;
  // The answer's heading takes the focus when an answer arrives.
  useEffect(() => {
    if (answer !== null) answerHeading.current?.focus();
  }, [answer]);

  const { length, depth } = useMemo(() => measure(inputText, asJson, maxDepth), [inputText, asJson, maxDepth]);
  const inputProblem = length > maxInput
    ? `The input is ${numbers(length)} characters; the playground takes at most ${numbers(maxInput)}.`
    : depth > maxDepth ? `The input nests lists and maps more than ${maxDepth} levels deep; the playground takes at most ${maxDepth}.` : null;

  const checking = who.kind === 'checking';
  const showCheck = hydrated && !checking && (checkAsked || (who.kind !== 'subscriber' && !passed));
  // The bot check is shown and has not given a token yet: a run would only be refused for want of one.
  const needToken = showCheck && token === null;

  const run = async () => {
    setOutcome(null);
    setWait(null);
    setLive('');
    setAttempt((n) => n + 1);
    let input: unknown = inputText;
    if (asJson) {
      try {
        input = JSON.parse(inputText);
      } catch (error) {
        if (!(error instanceof SyntaxError)) throw error;
        setOutcome({ kind: 'failed', message: `The input is not valid JSON: ${error.message}. Fix it, or untick "Input as JSON" to send it as text.` });
        return;
      }
    }
    setRunning(true);
    const sent = token;
    let got: Outcome;
    try {
      got = await ask(sample.id, input, sent);
    } finally {
      setRunning(false);
    }
    setOutcome(got);
    // The hub checks the body (input, sample) before the bot check, so a token sent with a refused body is unused.
    const unused = got.kind === 'refused' && (got.refusal.kind === 'input' || got.refusal.kind === 'sample');
    if (sent !== null && !unused) setToken(null);
    if (got.kind === 'answer') {
      setPassed(true);
      setCheckAsked(false);
      const { decision } = got.answer;
      setLive(`${String(decision.value)}, ${decision.confidence === null ? 'no confidence' : `confidence ${decision.confidence}`}, ${got.answer.latency_ms} ms`);
    } else if (got.kind === 'refused') {
      if (got.refusal.kind === 'bot-check') {
        setCheckAsked(true);
        setPassed(false);
        if (sent !== null) setWidget((n) => n + 1);
      } else if (!unused) {
        // Any other refusal comes after the bot check: the hub has given the pass cookie.
        setPassed(true);
        setCheckAsked(false);
      }
      const seconds = got.refusal.retryAfterS;
      if (seconds !== null) setWait({ until: Date.now() + seconds * 1000, seconds });
    } else if (sent !== null) {
      // No answer the page can read: whether the token was used is not known, so the check is drawn afresh.
      setWidget((n) => n + 1);
    }
  };

  const notice = outcome === null || outcome.kind === 'answer' ? null
    : outcome.kind === 'refused' ? outcome.refusal.message : outcome.message;
  const whoLine = who.kind === 'subscriber'
    ? 'You run on your own account: runs count against your allowance, with no limit a minute.'
    : who.kind === 'signed-in'
      ? `You are signed in without an active subscription, so you run as a visitor: ${runsPerMinute} a minute, after a bot check.`
      : `Visitors can run ${runsPerMinute} a minute after a bot check; subscribers run on their own account.`;
  const holdUp = checking ? (hydrated ? 'Checking whether you are signed in…' : null)
    : inputProblem !== null ? inputProblem
      : needToken ? 'Complete the bot check to run.' : null;

  return (
    <main id="main" className="mx-auto max-w-6xl px-4 py-10 sm:py-12">
      <h1 className={PAGE_TITLE}>Playground</h1>
      <p className="mt-3 max-w-[72ch] text-sm leading-7 text-soft">
        Every answer here is a live call to the Jeff API. Pick an adapter and a sample, change the input, and run it.
      </p>
      <p className="mt-1 max-w-[72ch] text-sm leading-7 text-soft">{hydrated && !checking && who.kind !== 'unknown' ? whoLine : ' '}</p>
      {who.kind === 'unknown' && (
        <p role="status" className="mt-1 max-w-[72ch] text-sm text-danger">
          {`Could not check whether you are signed in: ${who.reason}. You can still run as a visitor, after the bot check.`}
        </p>
      )}
      {linkNote !== null && <p role="status" className="mt-3 text-sm text-danger">{linkNote}</p>}

      <div className="mt-8 grid gap-8 lg:grid-cols-[minmax(0,5fr)_minmax(0,6fr)]">
        <section aria-labelledby="playground-case" className="min-w-0">
          <h2 id="playground-case" className={SECTION_HEADING}>The case</h2>
          <div className="mt-4 grid gap-4 sm:grid-cols-2 lg:grid-cols-1 xl:grid-cols-2">
            <div className="min-w-0">
              <label className={FIELD_LABEL} htmlFor="playground-adapter">Adapter</label>
              <select id="playground-adapter" value={adapter.id} className={SELECT}
                onChange={(e) => {
                  const group = adapters.find((a) => a.id === e.target.value);
                  if (group === undefined) throw new Error(`The playground has no adapter ${e.target.value}`);
                  choose(group, group.samples[0]);
                }}>
                {adapters.map((a) => <option key={a.id} value={a.id}>{a.title}</option>)}
              </select>
            </div>
            <div className="min-w-0">
              <label className={FIELD_LABEL} htmlFor="playground-sample">Sample</label>
              <select id="playground-sample" value={sample.id} className={SELECT}
                onChange={(e) => {
                  const picked = adapter.samples.find((s) => s.id === e.target.value);
                  if (picked === undefined) throw new Error(`The playground's adapter ${adapter.id} has no sample ${e.target.value}`);
                  choose(adapter, picked);
                }}>
                {adapter.samples.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
            </div>
          </div>
          <p className="mt-3 break-all font-mono text-xs text-mute">{sample.function}</p>
          {sample.context !== null && (
            // Open from the start: a sample's context often holds what the input is about (a conversation, a query).
            <details open className={`mt-4 ${CARD} px-3 py-2`}>
              <summary className="cursor-pointer font-mono text-sm text-soft">Context (fixed for this sample)</summary>
              <pre className="mt-2 whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-xs leading-5 text-text">{sample.context}</pre>
            </details>
          )}
          <div className="mt-4 flex items-end justify-between gap-3">
            <label className={FIELD_LABEL} htmlFor="playground-input">Input</label>
            <span className={`font-mono text-xs ${length > maxInput ? 'text-danger' : 'text-mute'}`}>
              {`${numbers(length)} / ${numbers(maxInput)}`}
            </span>
          </div>
          <p id="playground-input-hint" className="mt-1 max-w-[72ch] text-xs text-soft">{sample.inputHint}</p>
          <textarea id="playground-input" aria-describedby="playground-input-hint" value={inputText} rows={8} spellCheck={false}
            onChange={(e) => setInputText(e.target.value)}
            className="mt-1 w-full min-w-0 resize-y border border-ink bg-panel p-3 font-mono text-sm leading-6 text-text" />
          <label className="mt-2 flex items-center gap-2 text-sm text-soft">
            <input type="checkbox" checked={asJson} onChange={(e) => setAsJson(e.target.checked)} className="size-4 accent-[var(--v)]" />
            Input as JSON
          </label>
          {showCheck && <div className="mt-4"><Turnstile key={widget} siteKey={siteKey} onToken={onToken} /></div>}
          <div className="mt-5 flex flex-wrap items-center gap-4">
            <button type="button" className={`${BUTTON_PRIMARY} px-5 py-2 disabled:opacity-60`} onClick={() => void run()}
              disabled={running || checking || waitLeft !== null || needToken || inputProblem !== null}>Run</button>
            {holdUp !== null && <span className="font-mono text-sm text-soft">{holdUp}</span>}
            {running && <span role="status" className="font-mono text-sm text-soft">Running…</span>}
            {waitLeft !== null && <span role="timer" className="font-mono text-sm text-soft">{`You can run again in ${waitLeft} s`}</span>}
          </div>
          {notice !== null && <p key={attempt} role="alert" className="mt-4 text-sm text-danger">{notice}</p>}
        </section>

        <section aria-labelledby="playground-answer" className="min-w-0">
          <h2 id="playground-answer" ref={answerHeading} tabIndex={-1} className={`${SECTION_HEADING} focus:outline-none`}>The answer</h2>
          <p aria-live="polite" className="sr-only" data-testid="playground-live">{live}</p>
          <div className="mt-4">
            {answer === null
              ? <p className={`${CARD} p-4 text-sm text-soft`}>Run the sample to see the adapter's answer, its confidence and every option's probability.</p>
              : <Result answer={answer} />}
          </div>
          <h2 className={`mt-8 ${SECTION_HEADING}`}>The same call in code</h2>
          <p className="mt-2 text-sm text-soft">
            {answer === null ? 'The call for this sample, as given.' : 'The call for the input you ran.'}
          </p>
          <figure className="on-screen mt-3 border-2 border-ink bg-screen-frame">
            {answer === null
              ? <CallCode key={sample.id} code={sample.code} curl={sample.curl} local={sample.local} name={`sample ${sample.id}`} runNote={RUN_NOTE} />
              : <CallCode code={answer.code} curl={answer.curl} local={answer.local} name={`the run of sample ${sample.id}`} runNote={RUN_NOTE} />}
          </figure>
        </section>
      </div>
    </main>
  );
}
