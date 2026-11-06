// A worked example (lib/example-block.ts) as one framed screen, in two parts: the case and its answer -- the context
// fields, the input and the caller's labels it was given, then the answer recorded from the live API with its options
// of highest probability -- and the call that makes it, in each SDK language the site shows (content/
// sdk-languages.yaml: Python, Node, ...) or curl (tabs in the code's title bar, beside the copy button; the code wraps
// rather than running off the right edge). The case and the answer come first, with `children` (one sentence on what
// the example shows, when the caller passes it) right under the answer, then the code.
// The SDK language is the visitor's one choice for the whole site (lib/code-language.ts); curl is this block's own.
// An offline example (an adapter the API does not serve) has only the Python tab, running it locally, and no answer.
// An open adapter's recorded example also offers its call "On your machine" (CallCode: local); a proprietary
// adapter's or a via-http example's offers the same choice with "On your machine" visibly disabled, since it has
// none (CallCode: canLocal).
import { useEffect, useId, useState, type ReactNode } from 'react';
import { Link } from 'react-router';
import { isCodeLanguage, onCodeLanguageChange, readStoredCodeLanguage, storeCodeLanguage } from '../../lib/code-language';
import { CODE_MODE_NAMES, CODE_MODE_PRICES, CODE_MODES, DEFAULT_CODE_MODE, onCodeModeChange, readStoredCodeMode, storeCodeMode, type CodeMode } from '../../lib/code-mode';
import type { ExampleBlock, ExampleField } from '../../lib/example-block';
import { SDK_CODE_NAMES, SDK_LABELS, type SdkLanguage } from '../../lib/sdk-languages';
import { CodeBlock } from './CodeBlock';
import { ScreenTabs } from '../ScreenTabs';

type Language = SdkLanguage | 'curl';

const TERM = 'font-mono text-xs uppercase leading-6 tracking-wide text-screen-dim';
const VALUE = 'min-w-0 whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-xs leading-6 text-screen-text';
const GRID = 'grid gap-x-4 gap-y-1 sm:grid-cols-[10rem_minmax(0,1fr)]';

function Fields({ fields }: { fields: ExampleField[] }) {
  return (
    <>
      {fields.map((field) => (
        <div key={field.name} className="contents">
          <dt className="min-w-0 [overflow-wrap:anywhere] font-mono text-xs leading-6 text-screen-dim">{field.name}</dt>
          <dd className={VALUE}>{field.value}</dd>
        </div>
      ))}
    </>
  );
}

function Case({ block }: { block: ExampleBlock }) {
  return (
    <div className="space-y-3">
      {block.context.length > 0 && (
        <div>
          <p className={TERM}>Context</p>
          <dl className={GRID}><Fields fields={block.context} /></dl>
        </div>
      )}
      <div>
        <p className={TERM}>Input</p>
        <p className={VALUE}>{block.input}</p>
      </div>
      {block.labels !== null && (
        <div>
          <p className={TERM}>Labels (the options)</p>
          <dl className={GRID}><Fields fields={block.labels} /></dl>
        </div>
      )}
    </div>
  );
}

function Answer({ block }: { block: ExampleBlock }) {
  if (block.answer === null) {
    return (
      <div>
        <p className={TERM}>Answer</p>
        <p className="text-sm leading-6 text-screen-text">This adapter runs locally with <code className="font-mono text-xs">Jeff.local</code>; the API does not serve it, so no answer is recorded here.</p>
      </div>
    );
  }
  return (
    <dl className={GRID}>
      <dt className={TERM}>Answer (JSON)</dt>
      <dd className="font-mono text-xs leading-6 text-screen-text">
        value <span className="font-semibold text-white">{block.answer.value}</span>, confidence{' '}
        <span className="font-semibold text-white">{block.answer.confidence}</span>
      </dd>
      <dt className={TERM}>Top options</dt>
      <dd className="font-mono text-xs leading-6 text-screen-text">
        <ol className="flex flex-wrap gap-x-4">
          {block.answer.top.map(({ option, probability }) => (
            <li key={option}>{option} <span className="text-screen-dim">{probability}</span></li>
          ))}
        </ol>
      </dd>
    </dl>
  );
}

// Why "On your machine" is disabled, for a block whose adapter has no local form at all (a proprietary adapter or a
// via-http example): read by a screen reader through aria-describedby, and shown as the line in the code's place
// (CallCode) when the visitor's remembered choice is local even though this block cannot honour it.
const LOCAL_UNAVAILABLE = 'This adapter runs only through the Jeff API.';

// "On your machine (free)" then "Through the API": two buttons, the pressed one the choice, on their own row above the language
// tabs (five languages and curl already fill the title bar beside the copy button at a desktop's width). `canLocal`
// false (a proprietary adapter or a via-http example) leaves "On your machine" visibly disabled -- aria-disabled, not
// a native `disabled` button, so it stays reachable by keyboard and a screen reader still reaches its reason -- and
// unclickable, so the visitor's site-wide choice (code-mode.ts) is never changed by a block that cannot act on it.
export function ModeChoice({ mode, canLocal, onSelect, reasonId }: { mode: CodeMode; canLocal: boolean; onSelect: (mode: CodeMode) => void; reasonId: string }) {
  return (
    <div role="group" aria-label="Where the call runs" className="flex px-2.5 pt-2">
      {CODE_MODES.map((value) => {
        const disabled = value === 'local' && !canLocal;
        return (
          <button
            key={value}
            type="button"
            aria-pressed={value === mode}
            aria-disabled={disabled}
            aria-describedby={disabled ? reasonId : undefined}
            onClick={disabled ? undefined : () => onSelect(value)}
            className={`whitespace-nowrap border border-white px-2 py-0.5 font-mono text-xs ${value === 'api' ? '-ml-px ' : ''}${
              disabled ? 'cursor-not-allowed text-white/40' : value === mode ? 'bg-white text-screen-frame' : 'text-white hover:bg-white/15'
            }`}
          >
            {/* Below 640 px the two full labels are wider than a phone's block, so the price note is left to
                screen readers there; the name is always shown. */}
            {CODE_MODE_NAMES[value]}{CODE_MODE_PRICES[value] !== null && <>{' '}<span className="max-sm:sr-only">{CODE_MODE_PRICES[value]}</span></>}
          </button>
        );
      })}
      {!canLocal && <span id={reasonId} className="sr-only">{LOCAL_UNAVAILABLE}</span>}
    </div>
  );
}

const NOTE_CODE = 'bg-white/10 px-1 font-mono text-xs text-white';

const NOTE_LINK = 'text-screen-text underline underline-offset-2 hover:text-white';

// Under the local code: what it needs on the reader's machine. Python runs the adapter in its own process; every
// other SDK finds the Jeff engine running for this user or starts it, which needs uv (contract/local-engine.md).
function LocalNote({ language }: { language: SdkLanguage }) {
  return (
    <p className="mx-1.5 mb-1.5 bg-screen px-4 pb-3 text-xs leading-5 text-screen-text">
      {language === 'python'
        ? <>Runs the adapter in this process, with the engine package <code className={NOTE_CODE}>jeff-runtime</code> installed; the first call downloads the open adapter and base model from Hugging Face.</>
        : <>Uses the Jeff engine running on your machine, or starts it, which needs <a href="https://docs.astral.sh/uv/" className={NOTE_LINK}>uv</a>; the first start downloads the engine and base model (about 2 GB, once). <Link to="/docs/sdk/connect#section-on-your-machine-free" className={NOTE_LINK}>Connect</Link> says more.</>}
    </p>
  );
}

// The call in each SDK language the block has code in, then curl: tabs in the code's title bar, beside the copy button.
// `code` and `curl` are an ExampleBlock's (a page's worked example, or the playground's call for the input being
// edited, which has the same shape). It draws no frame of its own: it sits in its caller's screen frame. The choice of
// "Through the API" (code and curl) or "On your machine" (local's code, no curl: a local call makes no HTTP request),
// the visitor's one choice for the site (lib/code-mode.ts), shows on every block that has an API form to offer it
// beside (curl not null) -- which is every block but an offline example's, whose code is already local and so offers
// no choice at all. `local` null with curl not null (a proprietary adapter or a via-http example) leaves "On your
// machine" visibly disabled: the block keeps showing its API form, except when the visitor's remembered choice is
// already local, when it shows why there is nothing to run locally in the code's place -- a block that cannot honour
// the stored choice never changes it back, it only differs from it on screen.
// `runNote`, when given, is shown under the code only while local is the mode shown: the playground's own note that
// its Run button always calls the API, so a reader on "On your machine" does not take its last run for a local one.
export function CallCode({ code, curl, local, name, runNote }: {
  code: ExampleBlock['code'];
  curl: string | null;
  local: ExampleBlock['local'];
  name: string;
  runNote?: ReactNode;
}) {
  const id = useId();
  // The SDK language shown, and whether this block's curl tab is chosen instead (curl is this block's own choice, and
  // only through the API): kept apart, so going local and back returns to curl.
  const [sdkLanguage, setSdkLanguage] = useState<SdkLanguage>('python');
  const [onCurl, setOnCurl] = useState(false);
  const [mode, setMode] = useState<CodeMode>(DEFAULT_CODE_MODE);

  const canLocal = local !== null;
  // Whether this block offers the choice at all: an offline example (no curl, its code already local) does not.
  const showChoice = canLocal || curl !== null;
  // A block with no choice (an offline example, whose `code` is already its local call) shows `code` as it is.
  const shownMode: CodeMode = showChoice ? mode : 'api';
  // True when the visitor's choice is local but this block has no local form to show: the code's place shows why
  // instead, rather than the API code it is not showing on request, or a non-null assertion on the local it lacks.
  const noLocal = shownMode === 'local' && !canLocal;
  const sdkCode = shownMode === 'local' && canLocal ? local.code : code;
  if (local !== null) {
    const api = code.map((c) => c.language).join(' ');
    const there = local.code.map((c) => c.language).join(' ');
    if (api !== there) throw new Error(`${name}: its local code is in ${there}, its API code in ${api}`);
  }
  const tabs: { value: Language; label: string; code: string }[] = [
    ...sdkCode.map(({ language: value, code: text }) => ({ value, label: SDK_LABELS[value], code: text })),
    ...(curl === null || shownMode === 'local' ? [] : [{ value: 'curl' as const, label: 'curl', code: curl }]),
  ];
  const language: Language = onCurl && shownMode === 'api' ? 'curl' : sdkLanguage;
  // The languages this block has code in, as a key for the effect below (a new array each render would re-run it).
  const offered = code.map((c) => c.language).join(' ');

  // After hydration (the prerendered page always shows Python through the API): the visitor's stored choices, and any
  // change of them made by another block on the page -- a language only when this block has code in it (an offline
  // example has only Python, so it stays on Python).
  useEffect(() => {
    const has = new Set(offered.split(' '));
    const follow = (next: SdkLanguage) => {
      if (!has.has(next)) return;
      setSdkLanguage(next);
      setOnCurl(false);
    };
    const stored = readStoredCodeLanguage();
    if (stored !== null) follow(stored);
    return onCodeLanguageChange(follow);
  }, [offered]);
  useEffect(() => {
    if (!showChoice) return undefined;
    const stored = readStoredCodeMode();
    if (stored !== null) setMode(stored);
    return onCodeModeChange(setMode);
  }, [showChoice]);

  const shownTab = tabs.find((tab) => tab.value === language);
  if (shownTab === undefined) throw new Error(`${name} has no ${language} code`);
  const select = (next: Language) => {
    if (isCodeLanguage(next)) storeCodeLanguage(next);
    else setOnCurl(true);
  };
  const tablist = <ScreenTabs idBase={id} tabs={tabs} selected={language} onSelect={select} controls={`${id}-panel`} />;
  return (
    <>
      {showChoice && <ModeChoice mode={shownMode} canLocal={canLocal} onSelect={storeCodeMode} reasonId={`${id}-local-unavailable`} />}
      {noLocal ? (
        <p className="mx-1.5 mb-1.5 bg-screen p-4 text-xs leading-5 text-screen-text">Not available: {LOCAL_UNAVAILABLE}</p>
      ) : (
        <>
          <CodeBlock
            code={shownTab.code}
            language={language === 'curl' ? 'bash' : SDK_CODE_NAMES[language]}
            bar={tablist}
            framed={false}
            wrap
            className=""
            panel={{ id: `${id}-panel`, labelledBy: `${id}-tab-${language}` }}
          />
          {shownMode === 'local' && language !== 'curl' && <LocalNote language={language} />}
          {shownMode === 'local' && runNote !== undefined && (
            <p className="mx-1.5 mb-1.5 bg-screen px-4 pb-3 text-xs leading-5 text-screen-dim">{runNote}</p>
          )}
        </>
      )}
    </>
  );
}

// "Open in playground" (block.playground), when the playground has a sample of the example's adapter: light on the screen.
function PlaygroundLink({ to }: { to: string }) {
  return (
    <p className="text-right">
      <Link to={to} className="font-mono text-xs text-screen-text underline underline-offset-2 hover:text-white">Open in playground</Link>
    </p>
  );
}

export function Example({ block, children }: { block: ExampleBlock; children?: ReactNode }) {
  const caseAndAnswer = (
    <div className="mx-1.5 mt-1.5 space-y-4 bg-screen p-4">
      <Case block={block} />
      <div className="border-t border-white/20 pt-4">
        <Answer block={block} />
      </div>
      {children !== undefined && <div className="text-sm leading-6 text-screen-text">{children}</div>}
      {block.playground !== null && <PlaygroundLink to={block.playground} />}
    </div>
  );
  return (
    <figure className="on-screen my-6 border-2 border-ink bg-screen-frame" data-example={block.id}>
      {caseAndAnswer}
      <CallCode code={block.code} curl={block.curl} local={block.local} name={`example ${block.id}`} />
    </figure>
  );
}
