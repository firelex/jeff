// The playground. "Functions" runs a served function through POST /v1/decide, from a form generated from its listing
// or from the raw request in a JSON editor; "Base model" sends a POST /v1/systemone request to the base model or an
// adapter, with questions written by hand.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Play } from 'lucide-react';
import { Badge, Button, EmptyState, JsonEditor, Select, Spinner, Tabs, tabPanelProps } from '@jeff/ui';
import {
  shownError, type Api, type DecideCall, type DecideResponse, type Example, type FunctionListing, type Health,
  type ShownError, type Timed, type Value,
} from '../api';
import { areaTitle, byArea } from '../areas';
import { BaseModelPlayground } from '../components/BaseModelPlayground';
import { DecisionView } from '../components/DecisionView';
import { ErrorAlert } from '../components/ErrorAlert';
import { FormError, FunctionForm, buildCall, emptyForm, formFromCall, type FormState } from '../components/FunctionForm';
import { Card, Page } from '../components/Page';
import { pretty } from '../json';
import { RUN_SHORTCUT } from '../shortcut';
import type { TryFunction } from './Home';

function DecideResults({ result }: { result: Timed<DecideResponse> }) {
  const { body, requestId, roundTripMs } = result;
  if ('decisions' in body) {
    return (
      <div className="flex flex-col gap-4">
        {body.decisions.map((decision, index) => (
          <DecisionView key={index} decision={decision} requestId={requestId} roundTripMs={roundTripMs}
            title={body.decisions.length > 1 ? `Call ${index + 1}` : undefined} />
        ))}
      </div>
    );
  }
  if ('decision' in body) return <DecisionView decision={body.decision} requestId={requestId} roundTripMs={roundTripMs} title="Dispatch" />;
  const outputs = Object.entries(body.graph_result.outputs);
  return (
    <div className="flex flex-col gap-4">
      <p className="m-0 text-sm text-soft">
        Graph outputs, in {body.graph_result.stages} batched stage{body.graph_result.stages === 1 ? '' : 's'} · request id{' '}
        <span className="font-mono">{requestId}</span>
      </p>
      {outputs.map(([node, outcome]) => {
        if ('decision' in outcome) return <DecisionView key={node} decision={outcome.decision} requestId={requestId} roundTripMs={roundTripMs} title={`Node ${node}`} />;
        return (
          <Card key={node}>
            <p className="m-0 font-mono text-xs text-mute">Node {node}</p>
            {'skipped' in outcome
              ? <p className="mt-1 mb-0 text-sm text-soft">Skipped: {outcome.reason}</p>
              : <pre className="mt-1 mb-0 overflow-auto font-mono text-xs">{pretty(outcome.code)}</pre>}
          </Card>
        );
      })}
    </div>
  );
}

type Mode = 'form' | 'json';

type Licence = 'all' | 'open' | 'proprietary';
const LICENCES: { value: Licence; label: string }[] = [
  { value: 'all', label: 'All adapters' }, { value: 'open', label: 'Open source' }, { value: 'proprietary', label: 'Proprietary' },
];

// A golden case is named "<function id> (<where its call came from>)"; under its function only the second part tells it apart.
function exampleLabel(example: Example): string {
  const prefix = `${example.call.function} (`;
  return example.name.startsWith(prefix) ? `Reference case (${example.name.slice(prefix.length)}` : example.name;
}

function showValue(value: Value): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

// The test set's answer beside the decision, when the call is a test-set example that has not been edited since.
function ExpectedAnswer({ expected, result }: { expected: Value; result: Timed<DecideResponse> | null }) {
  const decided = result && 'decisions' in result.body && result.body.decisions.length === 1 ? result.body.decisions[0] : null;
  return (
    <Card className="flex flex-wrap items-center gap-2 py-3">
      <span className="text-sm text-soft">Test set answer</span>
      <span className="font-mono text-sm font-semibold text-text">{showValue(expected)}</span>
      {decided && (decided.abstained
        ? <Badge tone="empty">Jeff abstained</Badge>
        : decided.value === expected ? <Badge tone="ok">Jeff agrees</Badge> : <Badge tone="proprietary">Jeff answered {showValue(decided.value)}</Badge>)}
    </Card>
  );
}

function FunctionsPlayground({ api, seed, onSeedApplied }: {
  api: Api; seed: (TryFunction & { nonce: number }) | null; onSeedApplied: () => void;
}) {
  const [functions, setFunctions] = useState<FunctionListing[] | null>(null);
  const [examples, setExamples] = useState<Example[] | null>(null);
  const [loadError, setLoadError] = useState<ShownError | null>(null);
  const [selected, setSelected] = useState<string>('');
  const [licence, setLicence] = useState<Licence>('all');
  // the test set's answer for the example shown, until the call is edited
  const [expected, setExpected] = useState<Value | null>(null);
  const [form, setForm] = useState<FormState | null>(null);
  const [mode, setMode] = useState<Mode>('form');
  const modeTabs = useId();
  const [jsonText, setJsonText] = useState('');
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<Timed<DecideResponse> | null>(null);
  const [error, setError] = useState<ShownError | null>(null);

  useEffect(() => {
    let current = true;
    void (async () => {
      try {
        const [listing, shipped] = await Promise.all([api.functions(), api.examples()]);
        if (!current) return;
        setFunctions(listing.functions);
        setExamples(shipped.examples);
      } catch (failure) {
        if (current) setLoadError(shownError(failure));
      }
    })();
    return () => {
      current = false;
    };
  }, [api]);

  const listing = useMemo(() => functions?.find((entry) => entry.id === selected) ?? null, [functions, selected]);
  const shownFunctions = useMemo(
    () => functions?.filter((entry) => licence === 'all' || entry.licence.kind === licence) ?? [], [functions, licence]);
  const functionExamples = useMemo(
    () => examples?.filter((example) => example.call.function === selected) ?? [], [examples, selected]);

  const listingFor = useCallback((id: string): FunctionListing => {
    const found = functions?.find((entry) => entry.id === id);
    if (!found) throw new FormError(`${id} is not served by this server now`);
    return found;
  }, [functions]);

  // Shows a call (an example, or what "try it" on the home page sent): its function, in the form and the editor.
  const showCall = useCallback((functionId: string, call: DecideCall | null, answer: Value | null = null) => {
    setResult(null);
    setError(null);
    setExpected(answer);
    try {
      const target = listingFor(functionId);
      const next = call === null ? emptyForm(target) : formFromCall(target, call);
      setSelected(functionId);
      setForm(next);
      setJsonText(call === null ? '' : pretty({ calls: [call] }));
      setMode('form');
    } catch (failure) {
      setError(shownError(failure));
    }
  }, [listingFor]);

  // A "try it" from the home page: shown once, then cleared, so a later visit to the playground starts empty rather
  // than loading that example again.
  useEffect(() => {
    if (!seed || !functions) return;
    showCall(seed.functionId, seed.call);
    onSeedApplied();
  }, [seed, functions, showCall, onSeedApplied]);

  const choose = (id: string) => {
    setExpected(null);
    setSelected(id);
    setForm(emptyForm(listingFor(id)));
    setJsonText('');
    setResult(null);
    setError(null);
  };

  const switchMode = (next: string) => {
    if (next === mode) return;
    setError(null);
    try {
      if (next === 'json') {
        if (listing && form) setJsonText(pretty({ calls: [buildCall(listing, form)] }));
        setMode('json');
        return;
      }
      const request = JSON.parse(jsonText) as { calls?: DecideCall[] };
      if (!Array.isArray(request.calls) || request.calls.length !== 1 || Object.keys(request).length !== 1) {
        throw new FormError('The form shows a request with exactly one call ({"calls": [one call]}); edit this one as JSON');
      }
      const call = request.calls[0];
      const target = listingFor(call.function);
      setSelected(call.function);
      setForm(formFromCall(target, call));
      setMode('form');
    } catch (failure) {
      setError(shownError(failure));
    }
  };

  const run = async () => {
    setError(null);
    let body: unknown;
    try {
      if (mode === 'form') {
        if (!listing || !form) throw new FormError('Choose a function first');
        body = { calls: [buildCall(listing, form)] };
      } else {
        body = JSON.parse(jsonText);
      }
    } catch (failure) {
      setError(shownError(failure));
      return;
    }
    setRunning(true);
    try {
      setResult(await api.decide(body));
    } catch (failure) {
      setResult(null);
      setError(shownError(failure));
    } finally {
      setRunning(false);
    }
  };

  const runRef = useRef(run);
  runRef.current = run;

  if (loadError) return <ErrorAlert error={loadError} />;
  if (!functions || !examples) return <div className="flex items-center gap-2 text-sm text-mute"><Spinner /> Loading the served functions</div>;
  if (functions.length === 0) {
    return <EmptyState title="This server serves no functions" description="No adapter is switched on. Serve one from the Adapters page." />;
  }

  const canRun = !running && (mode === 'json' ? jsonText.trim() !== '' : listing !== null);

  return (
    <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,560px)_minmax(0,1fr)]"
      onKeyDown={(event) => {
        if ((event.metaKey || event.ctrlKey) && event.key === 'Enter') {
          event.preventDefault();
          if (canRun) void runRef.current();
        }
      }}>
      <Card className="flex min-w-0 flex-col gap-5">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-[11rem_minmax(0,1fr)]">
          <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
            Licence
            <Select aria-label="Licence" value={licence} onChange={(event) => setLicence(event.target.value as Licence)}>
              {LICENCES.map((entry) => {
                const count = functions.filter((listed) => entry.value === 'all' || listed.licence.kind === entry.value).length;
                return <option key={entry.value} value={entry.value}>{entry.label} ({count})</option>;
              })}
            </Select>
          </label>
          <label className="flex flex-col gap-1.5 text-sm font-medium text-text">
            Function
            <Select aria-label="Function" value={shownFunctions.some((entry) => entry.id === selected) ? selected : ''}
              onChange={(event) => choose(event.target.value)} className="font-mono text-[13px]">
              <option value="" disabled>{shownFunctions.length === 0 ? 'No served function has this licence' : 'Choose a function'}</option>
              {byArea(shownFunctions).map(([area, entries]) => (
                <optgroup key={area} label={areaTitle(area)}>
                  {entries.map((entry) => <option key={entry.id} value={entry.id}>{entry.id}</option>)}
                </optgroup>
              ))}
            </Select>
          </label>
          <label className="flex flex-col gap-1.5 text-sm font-medium text-text sm:col-span-2">
            Example
            <Select aria-label="Example" value="" disabled={functionExamples.length === 0}
              onChange={(event) => {
                const example = functionExamples[Number(event.target.value)];
                showCall(example.call.function, example.call, example.expected ?? null);
              }}>
              <option value="" disabled>
                {!listing ? 'Choose a function first' : functionExamples.length === 0
                  ? 'No examples for this function'
                  : `Load an example (${functionExamples.length})`}
              </option>
              {functionExamples.map((example, index) => <option key={index} value={index}>{exampleLabel(example)}</option>)}
            </Select>
          </label>
        </div>

        {listing && (
          <div className="flex flex-wrap items-center gap-1.5">
            <Badge tone="neutral">{listing.type}</Badge>
            <Badge tone="brand">{listing.interface}</Badge>
            <span className="font-mono text-xs text-mute">version {listing.adapter_version}</span>
          </div>
        )}

        <Tabs id={modeTabs} items={[{ value: 'form', label: 'Form' }, { value: 'json', label: 'JSON request' }]} value={mode} onChange={switchMode} />

        {mode === 'form' ? (
          <div {...tabPanelProps(modeTabs, 'form')}>
            {listing && form
              ? <FunctionForm listing={listing} value={form} onChange={(next) => { setExpected(null); setForm(next); }} />
              : <p className="m-0 text-sm text-mute">Choose a function, or load an example.</p>}
          </div>
        ) : (
          <div {...tabPanelProps(modeTabs, 'json')} className="flex flex-col gap-1.5">
            <p className="m-0 text-xs text-mute">The whole POST /v1/decide body: calls, a dispatch or a graph.</p>
            <div className="h-[420px]">
              <JsonEditor label="POST /v1/decide request body" value={jsonText} onChange={(next) => { setExpected(null); setJsonText(next); }} />
            </div>
          </div>
        )}

        <div className="flex items-center justify-end gap-3 border-t border-line pt-4">
          <span className="text-xs text-faint">{RUN_SHORTCUT}</span>
          <Button pending={running} disabled={!canRun} onClick={() => void run()}>
            <Play className="size-4" aria-hidden="true" />
            Run
          </Button>
        </div>
      </Card>

      <div aria-live="polite" className="flex min-w-0 flex-col gap-4 xl:sticky xl:top-6 xl:self-start">
        {error && <ErrorAlert error={error} />}
        {expected !== null && <ExpectedAnswer expected={expected} result={result} />}
        {result
          ? <DecideResults result={result} />
          : !error && <EmptyState title="No decision yet" description="Run a function: its value, confidence, probabilities and timing appear here." />}
      </div>
    </div>
  );
}

export function Playground({ api, health, seed, onSeedApplied }: {
  api: Api; health: Health | null; seed: (TryFunction & { nonce: number }) | null; onSeedApplied: () => void;
}) {
  const [tab, setTab] = useState('functions');
  const tabs = useId();
  useEffect(() => {
    if (seed) setTab('functions');
  }, [seed]);
  return (
    <Page eyebrow="Playground" title="Run a function"
      lead="Pick a served function, fill in its input, context and labels, and run it through POST /v1/decide. The base model tab asks free-form questions through POST /v1/systemone.">
      <Tabs id={tabs} items={[{ value: 'functions', label: 'Functions' }, { value: 'base', label: 'Base model' }]} value={tab} onChange={setTab} />
      <div {...tabPanelProps(tabs, 'functions')} hidden={tab !== 'functions'}><FunctionsPlayground api={api} seed={seed} onSeedApplied={onSeedApplied} /></div>
      <div {...tabPanelProps(tabs, 'base')} hidden={tab !== 'base'}><BaseModelPlayground api={api} health={health} /></div>
    </Page>
  );
}
