// /admin/adapters: the owner's view of the hidden adapters (every adapter the API serves that is not open, which the
// public catalogue never lists; owner, 4 November 2026): each one's catalogue entry with the API only tag the owner's
// catalogue view gave it, how it was measured (components/catalogue/Measured.tsx, as on a public adapter page) and its
// worked examples. Prerendered like every page of the hub,
// so the shipped HTML holds nothing but the heading: in the browser the page asks the hub's function
// (GET /api/admin/adapters), which answers only the owner (the session's email is OWNER_EMAIL); for anyone else it
// answers 404, and the page says Not found.
import { useEffect, useState } from 'react';
import { NotFound } from '../components/NotFound';
import { Measured } from '../components/catalogue/Measured';
import { Tag } from '../components/Tag';
import { CARD, PAGE_TITLE, SECTION_HEADING } from '../components/styles';
import { askHub, refusalOf } from '../lib/ask-hub';
import type { HiddenAdapter, HiddenExample } from '../lib/hidden-adapters';

export function meta() {
  return [{ title: 'Hidden adapters | Jeff' }, { name: 'robots', content: 'noindex' }];
}

type Loaded = { kind: 'loading' } | { kind: 'missing' } | { kind: 'failed'; message: string } | { kind: 'ready'; adapters: HiddenAdapter[] };

const isAdapters = (body: unknown): body is { adapters: HiddenAdapter[] } => Array.isArray((body as { adapters?: unknown } | null)?.adapters);

function Fact({ name, value }: { name: string; value: string }) {
  return (
    <div className="mt-2 flex flex-col gap-0.5 sm:flex-row sm:gap-3">
      <dt className="font-mono text-xs text-mute sm:w-40 sm:shrink-0">{name}</dt>
      <dd className="break-words text-sm text-text">{value}</dd>
    </div>
  );
}

function Example({ shown }: { shown: HiddenExample }) {
  const { example, shows, request } = shown;
  const answer = example.recorded === null
    ? 'not recorded'
    : `${String(example.recorded.value)} (confidence ${example.recorded.confidence === null ? 'none' : String(example.recorded.confidence)})`;
  return (
    <details className="mt-3 border border-ink p-3">
      <summary className="cursor-pointer text-sm font-semibold text-text">{example.name ?? example.id}</summary>
      <p className="mt-2 text-sm text-soft">{shows}</p>
      <dl className="mt-2">
        <Fact name="Example" value={example.id} />
        <Fact name="Function" value={example.function} />
        <Fact name="Recorded answer" value={answer} />
      </dl>
      <p className="mt-3 font-mono text-xs text-mute">POST /v1/decide</p>
      <pre className="mt-1 overflow-x-auto bg-panel p-3 font-mono text-xs text-text">{JSON.stringify(request, null, 2)}</pre>
    </details>
  );
}

function Adapter({ adapter }: { adapter: HiddenAdapter }) {
  const { entry, numbers, examples } = adapter;
  return (
    <section className={`mt-6 p-5 sm:p-6 ${CARD}`} aria-label={entry.title}>
      <div className="flex flex-wrap items-start justify-between gap-2">
        <h2 className={SECTION_HEADING}>{entry.title}</h2>
        <Tag tone="accent">API only</Tag>
      </div>
      <p className="mt-2 text-sm text-soft">{entry.summary}</p>
      <dl className="mt-3">
        <Fact name="ID" value={entry.id} />
        <Fact name="Name in calls" value={entry.name} />
        <Fact name="Version" value={`${entry.version}, base ${entry.base}`} />
        <Fact name="Functions" value={entry.functions.map((fn) => `${fn.id} (${fn.interface}${fn.labels === null ? ', caller labels' : `: ${fn.labels.join(', ')}`})`).join(' · ')} />
        <Fact name="Context" value={entry.context.length === 0 ? 'None' : entry.context.map((field) => field.codeName).join(', ')} />
        <Fact name="Input" value={entry.input.codeName} />
        <Fact name="Use when" value={entry.useWhen.join(' ') || 'Not given'} />
        <Fact name="Avoid when" value={entry.avoidWhen.join(' ') || 'Not given'} />
      </dl>
      <h3 className="mt-5 font-mono text-sm text-mute">How it was measured</h3>
      {numbers === null
        ? <p className="mt-2 text-sm text-soft">No approved numbers were given for this adapter.</p>
        : <Measured numbers={numbers} />}
      <h3 className="mt-5 font-mono text-sm text-mute">Examples ({examples.length})</h3>
      {examples.map((shown) => <Example key={shown.example.id} shown={shown} />)}
    </section>
  );
}

export default function AdminAdapters() {
  const [state, setState] = useState<Loaded>({ kind: 'loading' });

  useEffect(() => {
    void (async () => {
      const answer = await askHub('/api/admin/adapters', null);
      if (answer.kind === 'answer' && answer.status === 404) setState({ kind: 'missing' });
      else if (answer.kind === 'answer' && answer.status === 200 && isAdapters(answer.body)) setState({ kind: 'ready', adapters: answer.body.adapters });
      else setState({ kind: 'failed', message: refusalOf(answer) });
    })();
  }, []);

  if (state.kind === 'missing') return <NotFound />;
  return (
    <main id="main" className="mx-auto max-w-6xl px-4 py-12 sm:py-16">
      <h1 className={PAGE_TITLE}>Hidden adapters</h1>
      {state.kind === 'loading' && <p role="status" className="mt-4 text-sm text-soft">Loading…</p>}
      {state.kind === 'failed' && <p role="alert" className="mt-4 text-sm text-danger">{state.message}</p>}
      {state.kind === 'ready' && state.adapters.map((adapter) => <Adapter key={adapter.entry.id} adapter={adapter} />)}
    </main>
  );
}
