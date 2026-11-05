// The factory page ("Make an adapter"): describe a decision, confirm the teacher's check of the description, review
// and approve the task spec it writes, then generate data, train, test and publish, each step timed by the server's
// clock. Two ways to look: recorded runs (replays, played back from their events), and, when the server builds
// adapters (JEFF_FACTORY=owner), live runs whose steps the factory worker runs. With building off, the replays are
// shown with a note, and the cheap steps can still be tried as a preview when the server has teacher settings.
// Every failure shows the server's typed error.
import { useCallback, useEffect, useId, useMemo, useRef, useState, type ReactNode } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { ArrowRight, Check, CheckCircle2, CircleDashed, Copy, FileText, Loader2, Pause, Play, Save, Search, Upload, XCircle } from 'lucide-react';
import { Alert, Badge, Button, Dialog, Input, Select, Spinner, Tabs, Textarea, tabPanelProps } from '@jeff/ui';
import { shownError, type Api, type DecideCall, type Json, type ShownError } from '../../api';
import { ErrorAlert } from '../../components/ErrorAlert';
import { Card, Page } from '../../components/Page';
import { markdownComponents } from '../../components/Markdown';
import type { TryFunction } from '../Home';
import {
  factoryApi, followEvents, type Choice, type DescriptionCheck, type DescriptionCheckResult, type FactoryApi, type FactoryEvent,
  type FactoryMode, type Job, type PublishResult, type Reference, type ReplaySummary, type ReplayView, type RunContent, type RunSummary, type RunView,
  type Scope, type TaskSpec, type TestResult, type WorkerStatus,
} from './api';
import { clock, derive, duration, headline, stepLabel, timeOfDay, type Derived, type StepTiming } from './derive';
import { Inspect } from './Inspect';
import { LineChart, SERIES_COLORS } from './LineChart';
import { SpecForm } from './SpecForm';
import { TEST_PROMPT } from './testPrompts';
import { TestSets } from './TestSets';

const SPEEDS = [1, 10, 60];
const SPLITS = ['train', 'dev', 'calibration', 'test'];
const WORKER_EVERY_MS = 5_000;
const percent = (value: number) => `${(value * 100).toFixed(1)}%`;

function Loading({ children }: { children: string }) {
  return <div className="flex items-center gap-2 text-sm text-mute"><Spinner /> {children}</div>;
}

export function Factory({ api, onTry }: { api: Api; onTry: (request: TryFunction) => void }) {
  const factory = useMemo(() => factoryApi(api), [api]);
  const [mode, setMode] = useState<FactoryMode | null>(null);
  const [modeError, setModeError] = useState<ShownError | null>(null);
  const [tab, setTab] = useState<'replay' | 'live'>('replay');
  const tabs = useId();
  useEffect(() => {
    let current = true;
    factory.mode().then((answer) => current && setMode(answer), (failure: unknown) => current && setModeError(shownError(failure)));
    return () => {
      current = false;
    };
  }, [factory]);

  const owner = mode?.mode === 'owner';
  return (
    <Page eyebrow="Build" title="Make an adapter"
      lead="Describe a decision in plain English. Jeff turns it into a task spec you approve once, writes training data with a teacher model, trains and tests an adapter, and serves it. Every step is timed by the server's clock."
      actions={owner ? (
        <Tabs id={tabs} value={tab} onChange={(value) => setTab(value as 'replay' | 'live')}
          items={[{ value: 'replay', label: 'Replay' }, { value: 'live', label: 'Live' }]} />
      ) : undefined}>
      <p className="m-0 text-xs text-mute">
        Times cover the run only. One-time setup is not counted: building this feature, installing the trainer, choosing
        settings, trial runs and debugging.
      </p>
      {modeError && <ErrorAlert error={modeError} />}
      {!mode && !modeError && <Loading>Loading the factory</Loading>}
      {mode && owner && <WorkerLine factory={factory} />}
      {mode && !owner && (
        <Alert tone="info" title="Building adapters is switched off on this server (JEFF_FACTORY=off)">
          Recorded runs can be played back here{mode.preview ? ', and the steps up to data generation can be tried for real' : ''}.
          To build adapters, start the server with JEFF_FACTORY=owner and run the factory worker beside it.
        </Alert>
      )}
      {mode && (owner ? (
        <>
          <div {...tabPanelProps(tabs, 'replay')} hidden={tab !== 'replay'}>
            {tab === 'replay' && <ReplayMode factory={factory} mode={mode} api={api} />}
          </div>
          <div {...tabPanelProps(tabs, 'live')} hidden={tab !== 'live'}>
            {tab === 'live' && <LiveMode factory={factory} api={api} onTry={onTry} />}
          </div>
        </>
      ) : <ReplayMode factory={factory} mode={mode} api={api} />)}
    </Page>
  );
}

// The factory worker runs every build step; without it, queued jobs wait. Polled while the page is open.
function WorkerLine({ factory }: { factory: FactoryApi }) {
  const [status, setStatus] = useState<WorkerStatus | null>(null);
  const [error, setError] = useState<ShownError | null>(null);
  useEffect(() => {
    let current = true;
    const check = async () => {
      try {
        const answer = await factory.worker();
        if (!current) return;
        setStatus(answer);
        setError(null);
      } catch (failure) {
        if (current) setError(shownError(failure));
      }
    };
    void check();
    const timer = window.setInterval(() => void check(), WORKER_EVERY_MS);
    return () => {
      current = false;
      window.clearInterval(timer);
    };
  }, [factory]);
  if (error) return <ErrorAlert error={error} />;
  if (!status) return <Loading>Checking the factory worker</Loading>;
  if (!status.running) {
    return (
      <Alert tone="warning" title={<>Worker not running: start <code className="font-mono">jeff-server factory worker</code></>}>
        {status.heartbeat_age_s === null
          ? 'No factory worker has run on this server yet. Build steps are queued and wait until one starts.'
          : `Its last heartbeat was ${clock(status.heartbeat_age_s * 1000)} ago. Build steps are queued and wait until it starts again.`}
      </Alert>
    );
  }
  if (status.heartbeat_age_s === null) throw new Error('GET /v1/factory/worker says a worker is running but gives no heartbeat age');
  return (
    <p className="m-0 flex items-center gap-2 text-xs text-mute" role="status">
      <span className="inline-block size-2 rounded-full bg-success" aria-hidden="true" />
      Factory worker running · heartbeat {clock(status.heartbeat_age_s * 1000)} ago ·{' '}
      {status.current_job ? `running ${status.current_job.steps.map(stepLabel).join(' → ')} of ${status.current_job.run}` : 'idle'}
    </p>
  );
}

function CopyPrompt({ text }: { text: string }) {
  const [copied, setCopied] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  return (
    <>
      <Button size="sm" variant="ghost" title="Copy the description this run started from, to try it in the description check"
        onClick={async () => {
          setFailure(null);
          try {
            await navigator.clipboard.writeText(text);
          } catch (error) {
            setFailure(error instanceof Error ? error.message : String(error));
            return;
          }
          setCopied(true);
          setTimeout(() => setCopied(false), 1500);
        }}>
        {copied ? <Check className="size-4" aria-hidden="true" /> : <Copy className="size-4" aria-hidden="true" />}
        {copied ? 'Copied' : 'Copy test prompt'}
      </Button>
      {failure && <span role="alert" className="text-xs text-danger">Copy failed: {failure}</span>}
    </>
  );
}

// --- replay -----------------------------------------------------------------------------------------------------------

function ReplayMode({ factory, mode, api }: { factory: FactoryApi; mode: FactoryMode; api: Api }) {
  const [replays, setReplays] = useState<ReplaySummary[] | null>(null);
  const [error, setError] = useState<ShownError | null>(null);
  const [chosen, setChosen] = useState<string | null>(null);
  const [view, setView] = useState<ReplayView | null>(null);

  useEffect(() => {
    if (!mode.replays) return;
    factory.replays().then((r) => {
      setReplays(r.replays);
      if (r.replays.length) setChosen(r.replays[0].id);
    }, (failure: unknown) => setError(shownError(failure)));
  }, [factory, mode.replays]);
  useEffect(() => {
    if (!chosen) return;
    setView(null);
    factory.replay(chosen).then(setView, (failure: unknown) => setError(shownError(failure)));
  }, [factory, chosen]);

  return (
    <div className="flex flex-col gap-6">
      {mode.preview ? <TryIt factory={factory} api={api} buildingOff={mode.mode === 'off'} />
        : <p className="m-0 text-sm text-mute">Trying the factory is not set up on this server: it has no teacher settings.</p>}
      <h2 id="recorded-run" className="m-0 pt-4 text-lg font-semibold text-text">Recorded runs</h2>
      {!mode.replays && <p className="m-0 text-sm text-mute">This server keeps no recorded runs: JEFF_FACTORY_DIR is not set.</p>}
      {error && <ErrorAlert error={error} />}
      {mode.replays && replays === null && !error && <Loading>Loading recorded runs</Loading>}
      {replays?.length === 0 && <p className="m-0 text-sm text-mute">No recorded runs yet.</p>}
      {replays && replays.length > 1 && (
        <div className="w-96">
          <Select aria-label="Recorded run" value={chosen ?? ''} onChange={(e) => setChosen(e.target.value)}>
            {replays.map((r) => <option key={r.id} value={r.id}>{r.name} · {new Date(r.created).toLocaleDateString()}</option>)}
          </Select>
        </div>
      )}
      {chosen && !view && !error && <Loading>Loading the run</Loading>}
      {view && <Player factory={factory} view={view} />}
    </div>
  );
}

// Try the factory up to data generation: the description check and interpretation, or an import and its data plan,
// then edit and approve the spec, all for real (a few teacher calls). Data generation and training stop with a dialog.
function TryIt({ factory, api, buildingOff }: { factory: FactoryApi; api: Api; buildingOff: boolean }) {
  const [runId, setRunId] = useState<string | null>(null);
  return (
    <div className="flex flex-col gap-3">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <h2 className="m-0 text-lg font-semibold text-text">Try it yourself</h2>
        <span className="text-xs text-mute">
          {buildingOff ? 'Runs for real up to data generation; building adapters is switched off on this server.' : 'A preview: runs for real up to data generation. Start a full build in Live.'}
        </span>
      </div>
      {runId === null
        ? <NewRunTabs factory={factory} scope="previews" onStarted={setRunId} />
        : (
          <>
            <div><Button variant="ghost" size="sm" onClick={() => setRunId(null)}>Start over</Button></div>
            <LiveRun key={runId} factory={factory} api={api} scope="previews" runId={runId} onChanged={() => undefined} />
          </>
        )}
    </div>
  );
}

function Player({ factory, view }: { factory: FactoryApi; view: ReplayView }) {
  const [first, last] = span(view);
  const [position, setPosition] = useState(first);
  const [speed, setSpeed] = useState(60);
  const [playing, setPlaying] = useState(false);
  const tick = useRef<number | null>(null);

  useEffect(() => {
    setPosition(first);
    setPlaying(false);
  }, [first]);
  useEffect(() => {
    if (!playing) return;
    let previous = performance.now();
    const step = (now: number) => {
      setPosition((p) => {
        const next = Math.min(last, p + (now - previous) * speed);
        if (next >= last) setPlaying(false);
        return next;
      });
      previous = now;
      tick.current = requestAnimationFrame(step);
    };
    tick.current = requestAnimationFrame(step);
    return () => {
      if (tick.current !== null) cancelAnimationFrame(tick.current);
    };
  }, [playing, speed, last]);

  const derived = useMemo(() => derive(view.events, position), [view.events, position]);
  // An imported run has no description check: its task description is what it started from.
  const original = view.run.description_check === null ? view.run.description : view.run.description_check.original;
  return (
    <div className="flex flex-col gap-6">
      <div className="sticky top-0 z-10 flex flex-wrap items-center gap-3 rounded-card border border-line bg-panel/95 px-4 py-3 shadow-card backdrop-blur">
        <Badge tone="brand">Recorded run, {new Date(first).toLocaleDateString([], { day: 'numeric', month: 'long', year: 'numeric' })}</Badge>
        <CopyPrompt text={original} />
        <Button size="sm" variant="ghost" onClick={() => {
          if (position >= last) setPosition(first);
          setPlaying(!playing);
        }}>
          {playing ? <Pause className="size-4" aria-hidden="true" /> : <Play className="size-4" aria-hidden="true" />}
          {playing ? 'Pause' : position >= last ? 'Replay' : 'Play'}
        </Button>
        <div className="flex gap-1" role="group" aria-label="Speed">
          {SPEEDS.map((value) => (
            <Button key={value} size="sm" variant={speed === value ? 'primary' : 'ghost'} aria-pressed={speed === value} onClick={() => setSpeed(value)}>{value}×</Button>
          ))}
        </div>
        <input type="range" className="min-w-40 flex-1 accent-primary" min={first} max={last} value={position}
          aria-label="Position in the run" onChange={(e) => { setPosition(Number(e.target.value)); setPlaying(false); }} />
        <span className="font-mono text-sm text-text" title="Real elapsed time of the run">{clock(position - first)}</span>
        <span className="font-mono text-xs text-mute">{timeOfDay(position)}</span>
      </div>
      <RunBody factory={factory} view={view} derived={derived} live={false} />
    </div>
  );
}

function span(view: ReplayView): [number, number] {
  if (view.events.length === 0) throw new Error(`The recorded run ${view.run.id} has no events`);
  return [Date.parse(view.events[0].t), Date.parse(view.events[view.events.length - 1].t)];
}

// --- live -------------------------------------------------------------------------------------------------------------

function LiveMode({ factory, api, onTry }: { factory: FactoryApi; api: Api; onTry: (request: TryFunction) => void }) {
  const [runs, setRuns] = useState<RunSummary[] | null>(null);
  const [runId, setRunId] = useState<string>('new');
  const [section, setSection] = useState<'runs' | 'testsets'>('runs');
  const [error, setError] = useState<ShownError | null>(null);
  const sections = useId();

  const loadRuns = useCallback(() => {
    factory.runs().then((r) => {
      setRuns(r.runs);
      setError(null);
    }, (failure: unknown) => setError(shownError(failure)));
  }, [factory]);
  useEffect(loadRuns, [loadRuns]);

  return (
    <div className="flex flex-col gap-6">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <Tabs id={sections} value={section} onChange={(value) => setSection(value as 'runs' | 'testsets')}
          items={[{ value: 'runs', label: 'Runs' }, { value: 'testsets', label: 'Test sets' }]} />
        {section === 'runs' && runs && (
          <div className="w-96">
            <Select aria-label="Run" value={runId} onChange={(e) => setRunId(e.target.value)}>
              <option value="new">New run</option>
              {runs.map((r) => <option key={r.id} value={r.id}>{r.name === null ? `${r.id} (no spec yet)` : r.name} · {r.status}{r.clean ? '' : ' · not clean'}</option>)}
            </Select>
          </div>
        )}
      </div>
      {error && <ErrorAlert error={error} />}
      <div {...tabPanelProps(sections, section)}>
        {section === 'testsets' && <TestSets factory={factory} />}
        {section === 'runs' && !runs && !error && <Loading>Loading the runs</Loading>}
        {section === 'runs' && runs && (runId === 'new'
          ? <NewRunTabs factory={factory} scope="runs" onStarted={(id) => { loadRuns(); setRunId(id); }} />
          : <LiveRun key={runId} factory={factory} api={api} scope="runs" runId={runId} onTry={onTry} onChanged={loadRuns} />)}
      </div>
    </div>
  );
}

// Two ways in: a plain-English description (the teacher writes the questions), or questions already written for a
// decision API, imported verbatim (the teacher only writes the training data). Results of the two are never merged.
function NewRunTabs({ factory, scope, onStarted }: { factory: FactoryApi; scope: Scope; onStarted: (id: string) => void }) {
  const [way, setWay] = useState<'prompt' | 'import'>('prompt');
  const tabs = useId();
  return (
    <div className="flex flex-col gap-3">
      <div>
        <Tabs id={tabs} value={way} onChange={(value) => setWay(value as 'prompt' | 'import')}
          items={[{ value: 'prompt', label: 'From a description' }, { value: 'import', label: 'Import questions' }]} />
      </div>
      <div {...tabPanelProps(tabs, way)}>
        {way === 'prompt' ? <NewRun factory={factory} scope={scope} onStarted={onStarted} /> : <ImportRun factory={factory} scope={scope} onStarted={onStarted} />}
      </div>
    </div>
  );
}

const COOKBOOK = 'typesafe-function-calling';

function ImportRun({ factory, scope, onStarted }: { factory: FactoryApi; scope: Scope; onStarted: (id: string) => void }) {
  const [references, setReferences] = useState<Reference[] | null>(null);
  const [reference, setReference] = useState<string>('');
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<ShownError | null>(null);
  useEffect(() => {
    factory.references().then((r) => setReferences(r.references), (failure: unknown) => setError(shownError(failure)));
  }, [factory]);
  const chosen = references?.find((r) => r.name === reference);
  const choose = (value: string) => {
    const found = references?.find((r) => r.name === value);
    if (!found) throw new Error(`The question set ${value} is not on this server`);
    setReference(value);
    setDescription(found.description);
  };
  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const view = await factory.startImport(scope, { reference, name, description });
      onStarted(view.run.id);
    } catch (failure) {
      setError(shownError(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Card className="flex flex-col gap-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <label className="flex w-96 flex-col gap-1.5">
          <span className="text-xs font-medium text-mute">Questions to import (verbatim)</span>
          <Select value={reference} disabled={!references} onChange={(e) => choose(e.target.value)}>
            <option value="" disabled>{references ? 'Choose a question set' : 'Loading…'}</option>
            {references?.map((r) => <option key={r.name} value={r.name}>{r.name} · {r.questions} questions</option>)}
          </Select>
        </label>
        <Button variant="ghost" size="sm" disabled={!references?.some((r) => r.name === COOKBOOK)}
          onClick={() => { choose(COOKBOOK); setName('trading-function-calls'); }}>
          <FileText className="size-4" aria-hidden="true" />Use the TypeSafe cookbook
        </Button>
      </div>
      {chosen && <Inspect factory={factory} reference={chosen.name} files={chosen.files} />}
      <div className="grid gap-4 md:grid-cols-[16rem_1fr]">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-mute">Adapter name</span>
          <Input className="font-mono" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-mute">Short task description: who writes the requests and the hard patterns (the teacher uses it only to write training data)</span>
          <Textarea rows={8} value={description} onChange={(e) => setDescription(e.target.value)} />
        </label>
      </div>
      {error && <ErrorAlert error={error} />}
      <div className="flex flex-wrap items-center justify-end gap-3">
        <span className="text-xs text-mute">The questions are taken exactly as written; you approve the teacher's data plan next.</span>
        <Button pending={busy} disabled={!reference || !name || !description.trim()} onClick={() => void start()}>
          <Play className="size-4" aria-hidden="true" />Import and plan the data
        </Button>
      </div>
    </Card>
  );
}

function NewRun({ factory, scope, onStarted }: { factory: FactoryApi; scope: Scope; onStarted: (id: string) => void }) {
  const [description, setDescription] = useState('');
  const [descriptionView, setDescriptionView] = useState<'write' | 'preview'>('write');
  const [name, setName] = useState('');
  const [examples, setExamples] = useState('');
  const [checked, setChecked] = useState<DescriptionCheck | null>(null);
  const [edited, setEdited] = useState('');
  const [busy, setBusy] = useState<'check' | 'start' | null>(null);
  const [error, setError] = useState<ShownError | null>(null);
  const viewTabs = useId();

  const check = async () => {
    setBusy('check');
    setError(null);
    try {
      const result = await factory.checkDescription({ description, ...(examples ? { examples } : {}) });
      setChecked(result);
      setEdited(result.result.improved_description);
    } catch (failure) {
      setError(shownError(failure));
    } finally {
      setBusy(null);
    }
  };
  const start = async (choice: Choice) => {
    if (!checked) throw new Error('Check the description before starting a run');
    setBusy('start');
    setError(null);
    const text = choice === 'original' ? checked.description : choice === 'improved' ? checked.result.improved_description : edited;
    try {
      const view = await factory.start(scope, { check_id: checked.id, choice, description: text, ...(name ? { name } : {}) });
      onStarted(view.run.id);
    } catch (failure) {
      setError(shownError(failure));
    } finally {
      setBusy(null);
    }
  };
  const editedChoice: Choice = edited === checked?.result.improved_description ? 'improved' : 'edited';

  return (
    <Card className="flex flex-col gap-4">
      <div className="flex flex-col gap-1.5">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <label htmlFor="factory-description" className="text-sm font-medium text-text">Describe the decision you want Jeff to make</label>
          <div className="flex items-center gap-2">
            <Tabs id={viewTabs} value={descriptionView} onChange={(value) => setDescriptionView(value as 'write' | 'preview')}
              items={[{ value: 'write', label: 'Write' }, { value: 'preview', label: 'Preview' }]} />
            <Button variant="ghost" size="sm" disabled={checked !== null} onClick={() => { setDescription(TEST_PROMPT); setDescriptionView('preview'); }}>
              <FileText className="size-4" aria-hidden="true" />Use test prompt
            </Button>
          </div>
        </div>
        <div {...tabPanelProps(viewTabs, descriptionView)}>
          {descriptionView === 'write' ? (
            <Textarea id="factory-description" rows={10} value={description} disabled={checked !== null}
              onChange={(e) => setDescription(e.target.value)}
              placeholder="What is decided, the options and their meanings, what the state holds, hard cases to include… Markdown works." />
          ) : (
            <div className="max-h-[32rem] min-h-[15rem] overflow-y-auto rounded-button border border-line bg-panel px-5 py-1"
              onDoubleClick={() => checked === null && setDescriptionView('write')} title={checked === null ? 'Double-click to edit' : undefined}>
              {description.trim()
                ? <ReactMarkdown remarkPlugins={[remarkGfm]} components={markdownComponents}>{description}</ReactMarkdown>
                : <p className="py-4 text-sm text-mute">Nothing to preview yet.</p>}
            </div>
          )}
        </div>
      </div>
      <div className="grid gap-4 md:grid-cols-[16rem_1fr]">
        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-mute">Adapter name (optional)</span>
          <Input className="font-mono" placeholder="from the description" value={name} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="flex flex-col gap-1.5">
          <span className="text-xs font-medium text-mute">Up to 10 example cases with their answers (optional)</span>
          <Textarea rows={2} value={examples} disabled={checked !== null} onChange={(e) => setExamples(e.target.value)} />
        </label>
      </div>
      {error && <ErrorAlert error={error} />}
      {!checked ? (
        <div className="flex flex-wrap items-center justify-end gap-3">
          <span className="text-xs text-mute">The teacher checks the description first; nothing is built until you confirm.</span>
          <Button pending={busy === 'check'} disabled={!description.trim()} onClick={() => void check()}>
            <Search className="size-4" aria-hidden="true" />Check the description
          </Button>
        </div>
      ) : (
        <CheckResult check={checked} edited={edited} onEdit={setEdited} />
      )}
      {checked && (
        <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line pt-4">
          <Button variant="ghost" disabled={busy !== null} onClick={() => { setChecked(null); setError(null); }}>Change my description</Button>
          <Button variant="ghost" pending={busy === 'start'} disabled={busy !== null} onClick={() => void start('original')}>
            Keep mine and start
          </Button>
          <Button pending={busy === 'start'} disabled={busy !== null || !edited.trim()} onClick={() => void start(editedChoice)}>
            <Play className="size-4" aria-hidden="true" />{editedChoice === 'improved' ? 'Use the improved description and start' : 'Use my edited version and start'}
          </Button>
        </div>
      )}
    </Card>
  );
}

const SEVERITY_CLASS: Record<DescriptionCheckResult['problems'][number]['severity'], string> = {
  blocking: 'bg-danger/10 text-danger', important: 'bg-[#fff7e0] text-[#8a5a00]', minor: 'bg-[#f1f1f4] text-[#5c6270]',
};

function CheckResult({ check, edited, onEdit }: {
  check: { result: DescriptionCheckResult; teacher_model: string; started: string | null; ended: string | null };
  edited?: string; onEdit?: (text: string) => void;
}) {
  const result = check.result;
  return (
    <div className="flex flex-col gap-4">
      <div className="flex flex-wrap items-center gap-2">
        <Badge tone={result.ready ? 'ok' : 'proprietary'}>{result.ready ? 'Ready to build' : 'Needs attention'}</Badge>
        <span className="text-sm text-text">{result.summary}</span>
        <span className="ml-auto text-xs text-mute">
          {check.teacher_model}{check.started && check.ended ? ` · ${clock(Date.parse(check.ended) - Date.parse(check.started))}` : ''}
        </span>
      </div>
      {result.problems.length > 0 && (
        <div className="flex flex-col gap-1.5">
          <p className="m-0 text-xs font-medium text-text">Problems found</p>
          {result.problems.map((problem, index) => (
            <div key={index} className="flex items-start gap-2 text-sm">
              <span className={`mt-0.5 inline-block shrink-0 rounded-tag px-[6px] py-px text-[10.5px] font-bold ${SEVERITY_CLASS[problem.severity]}`}>{problem.severity}</span>
              <span className="text-text">{problem.text}</span>
            </div>
          ))}
        </div>
      )}
      {result.questions.length > 0 && (
        <div className="flex flex-col gap-1">
          <p className="m-0 text-xs font-medium text-text">Questions to consider</p>
          <ul className="m-0 list-disc pl-5 text-sm text-mute">{result.questions.map((q, i) => <li key={i}>{q}</li>)}</ul>
        </div>
      )}
      <div className="flex flex-col gap-1.5">
        <p className="m-0 text-xs font-medium text-text">Improved description <span className="font-normal text-mute">· additions are marked "(assumed)"{onEdit ? '; edit freely' : ''}</span></p>
        {onEdit ? (
          <Textarea rows={12} aria-label="Improved description" value={edited} onChange={(e) => onEdit(e.target.value)} />
        ) : (
          <p className="m-0 whitespace-pre-wrap rounded-button bg-background p-3 text-sm text-text">{result.improved_description}</p>
        )}
        {result.changes.length > 0 && (
          <ul className="m-0 list-disc pl-5 text-xs text-mute">{result.changes.map((c, i) => <li key={i}>{c}</li>)}</ul>
        )}
      </div>
    </div>
  );
}

function LiveRun({ factory, api, scope, runId, onTry, onChanged }: {
  factory: FactoryApi; api: Api; scope: Scope; runId: string; onTry?: (request: TryFunction) => void; onChanged: () => void;
}) {
  const [view, setView] = useState<RunView | null>(null);
  const [events, setEvents] = useState<FactoryEvent[]>([]);
  const [now, setNow] = useState(Date.now());
  const [error, setError] = useState<ShownError | null>(null);
  const [actionError, setActionError] = useState<ShownError | null>(null);
  const [acting, setActing] = useState<string | null>(null);
  const [blocked, setBlocked] = useState(false); // a preview reached data generation: say it stops here
  const editedSpec = useRef<TaskSpec | null>(null);
  const preview = scope === 'previews';

  const refresh = useCallback(() => {
    factory.run(scope, runId).then((v) => { setView(v); setError(null); }, (failure: unknown) => setError(shownError(failure)));
  }, [factory, scope, runId]);
  useEffect(() => {
    refresh();
    setEvents([]);
    const stop = followEvents(api, scope, runId, (event) => {
      setEvents((list) => [...list, event]);
      if (event.kind.endsWith('.end') || event.kind.endsWith('.start') || event.kind === 'step.failed' || event.kind === 'job.failed') refresh();
    }, (failure) => setError(shownError(failure)));
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => {
      stop();
      window.clearInterval(timer);
    };
  }, [api, factory, scope, runId, refresh]);

  const act = async (name: string, work: () => Promise<unknown>) => {
    setActing(name);
    setActionError(null);
    try {
      await work();
      refresh();
      onChanged();
    } catch (failure) {
      setActionError(shownError(failure));
    } finally {
      setActing(null);
    }
  };

  const derived = useMemo(() => derive(events, now), [events, now]);
  if (error) return <ErrorAlert error={error} />;
  if (!view) return <Loading>Loading the run</Loading>;
  const build = (work: () => void) => () => (preview ? setBlocked(true) : work());
  const controls: Controls = {
    acting, queued: view.job, rest: view.rest,
    saveSpec: (spec) => { editedSpec.current = spec; },
    approve: () => void act('approve', async () => {
      if (editedSpec.current) await factory.saveSpec(scope, runId, editedSpec.current);
      await factory.approve(scope, runId);
    }),
    runRest: build(() => void act('rest', () => factory.runRest(runId))),
    runStep: (step) => build(() => void act(step, () => factory.step(runId, step)))(),
    setGate: (value) => build(() => void act('gate', () => factory.gate(runId, value)))(),
    uploadTest: (format, content) => build(() => void act('test', () => factory.testSet(runId, format, content)))(),
    saveReplay: build(() => void act('replay', () => factory.saveReplay(runId))),
    ...(preview ? {} : {
      attachTestset: (name: string) => void act('test', () => factory.attachTestset(runId, name)),
      listTestsets: () => factory.testsets().then((r) => r.testsets.filter((t) => t.frozen).map((t) => t.name)),
    }),
  };
  return (
    <div className="flex flex-col gap-6">
      {!view.run.clean && (
        <Alert tone="warning" title="Not a clean timed run">
          {view.run.unclean_reasons.join('; ')}. It can be used, but not saved as the timed replay.
        </Alert>
      )}
      {actionError && <ErrorAlert error={actionError} />}
      <RunBody factory={factory} view={view} derived={derived} live controls={controls} preview={preview} onTry={onTry} />
      <Dialog open={blocked} onClose={() => setBlocked(false)} title="A preview stops before data generation" className="w-[min(36rem,92vw)]">
        <div className="flex flex-col gap-4">
          <p className="m-0 text-sm text-soft">
            Generating the training data, training and testing the adapter take about an hour of teacher and GPU time, so
            a preview stops here. Everything up to here ran for real. To see the rest, play the recorded run: the same
            steps, with their real timings and results.
          </p>
          <div className="flex justify-end">
            <Button onClick={() => { setBlocked(false); document.getElementById('recorded-run')?.scrollIntoView({ behavior: 'smooth' }); }}>
              <Play className="size-4" aria-hidden="true" />Watch the recorded run
            </Button>
          </div>
        </div>
      </Dialog>
    </div>
  );
}

interface Controls {
  acting: string | null;
  /** This run's job, waiting for the worker or running; another step cannot be queued while it is there. */
  queued: Job | null;
  /** The build steps still to run, in order (empty until the spec is approved). */
  rest: string[];
  saveSpec: (spec: TaskSpec) => void; approve: () => void; runRest: () => void; runStep: (step: string) => void;
  // in a preview, runRest, runStep, setGate, uploadTest and saveReplay open the "a preview stops here" dialog instead
  setGate: (value: number) => void; attachTestset?: (name: string) => void; listTestsets?: () => Promise<string[]>;
  uploadTest: (format: 'csv' | 'jsonl' | 'calls', content: string) => void; saveReplay: () => void;
}

// --- the run ----------------------------------------------------------------------------------------------------------

function RunBody({ factory, view, derived, live, controls, preview = false, onTry }: {
  factory: FactoryApi; view: RunContent; derived: Derived; live: boolean; controls?: Controls; preview?: boolean;
  onTry?: (request: TryFunction) => void;
}) {
  const ended = (step: string) => derived.steps[step]?.end !== undefined && !derived.steps[step]?.error;
  const figures = headline(derived);
  const approved = Boolean(view.run.approved) && (live || ended('review'));
  const job = controls === undefined ? null : controls.queued;
  return (
    <div className="flex flex-col gap-5">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        <Tile label="Description → served adapter" value={clock(figures?.build)} note={figures?.finished ? 'without your review time' : 'so far, without your review time'} />
        <Tile label="Your review" value={clock(figures?.review)} note="reading and approving the spec" />
        <Tile label="With the independent test" value={clock(figures?.withEvaluation)} note="evaluation, not customer time" />
        <Tile label="Compute" value={`${(derived.gpuSeconds / 60).toFixed(1)} GPU-min`}
          note={`teacher: ${derived.teacher.calls.toLocaleString()} calls, ${((derived.teacher.prompt_tokens + derived.teacher.completion_tokens) / 1000).toFixed(0)}k tokens`} />
      </div>
      {preview && <div><Badge tone="preview">Preview: everything up to data generation runs for real</Badge></div>}
      {live && controls && approved && (controls.rest.length > 0 || job) && (
        <Card accent className="flex flex-wrap items-center justify-between gap-3 py-3">
          <p className="m-0 text-sm text-text">
            {job
              ? <><Loader2 className="mr-2 inline size-4 animate-spin text-primary" aria-hidden="true" />{job.state === 'running' ? 'The worker is running: ' : 'Queued for the worker: '}{job.steps.map(stepLabel).join(' → ')}</>
              : <>Still to run: {controls.rest.map(stepLabel).join(' → ')}</>}
          </p>
          {!job && (
            <Button pending={controls.acting === 'rest'} onClick={controls.runRest}>
              <Play className="size-4" aria-hidden="true" />Run the rest
            </Button>
          )}
        </Card>
      )}
      {derived.failed && (
        <Alert tone="error" title={`${stepLabel(derived.failed.step)} failed`}>
          <span className="block whitespace-pre-wrap break-words font-mono text-xs">{derived.failed.error}</span>
        </Alert>
      )}

      <StepCard number={1} title={view.run.mode === 'import' ? 'Import and confirm' : 'Describe and confirm'}
        steps={view.run.mode === 'import' ? ['interpret', 'review'] : ['description_check', 'confirm', 'interpret', 'review']} derived={derived}>
        {view.run.mode === 'import' && view.run.reference && (
          <>
            <p className="m-0 text-xs text-mute">Questions imported verbatim from <span className="font-mono text-text">{view.run.reference}</span>;
              the teacher planned the training data from this task description.</p>
            {live && <ReferenceFiles factory={factory} reference={view.run.reference} />}
          </>
        )}
        {view.run.description_check && ended('confirm') && (
          <details className="rounded-card border border-line">
            <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-text">
              Description check: {view.run.description_check.problems.length} problems found ·{' '}
              {{ original: 'kept the original', improved: 'took the improved description', edited: 'edited the improved description' }[view.run.description_check.choice]}
            </summary>
            <div className="flex flex-col gap-3 border-t border-line p-3">
              <p className="m-0 text-xs text-mute">Original description</p>
              <p className="m-0 whitespace-pre-wrap text-sm text-text">{view.run.description_check.original}</p>
              <CheckResult check={{ result: { ...view.run.description_check, improved_description: view.run.description_check.improved },
                teacher_model: view.run.description_check.teacher_model,
                started: isoOf(derived.steps.description_check?.start), ended: isoOf(derived.steps.description_check?.end) }} />
            </div>
          </details>
        )}
        <p className="m-0 text-xs text-mute">{view.run.mode === 'import' ? 'The task description' : 'The confirmed description'}</p>
        <p className="m-0 whitespace-pre-wrap rounded-button bg-background p-3 text-sm text-text">{view.run.description}</p>
        {view.spec && ended('interpret') ? (
          <>
            <SpecForm spec={view.spec} readOnly={!live || Boolean(view.run.approved)} onChange={(spec) => controls?.saveSpec(spec)} />
            {view.run.spec_sha256 && approved && <p className="m-0 font-mono text-xs text-mute">Frozen · sha256 {view.run.spec_sha256.slice(0, 16)}…</p>}
            {live && controls && !view.run.approved && (
              <div className="flex justify-end">
                <Button pending={controls.acting === 'approve'} onClick={controls.approve}>
                  <CheckCircle2 className="size-4" aria-hidden="true" />Approve and freeze
                </Button>
              </div>
            )}
          </>
        ) : derived.steps.interpret?.running && <Loading>The teacher is reading the description</Loading>}
      </StepCard>

      <StepCard number={2} title="Generate the data" steps={['data_prep', 'generate', 'checks']} derived={derived}
        action={live && controls && approved ? <StepButtons steps={['data_prep', 'generate', 'checks']} view={view} controls={controls} /> : undefined}>
        <Generation derived={derived} />
        {view.checks && ended('checks') && <ChecksReport checks={view.checks} />}
      </StepCard>

      <StepCard number={3} title="Train" steps={['train', 'calibrate']} derived={derived}
        action={live && controls && approved ? <StepButtons steps={['train', 'calibrate']} view={view} controls={controls} /> : undefined}>
        <Training derived={derived} />
        {view.metrics?.calibration && ended('calibrate') && (
          <p className="m-0 text-xs text-mute">
            Confidence temperature {view.metrics.calibration.temperature.toFixed(3)} fitted on {view.metrics.calibration.rows} calibration rows
            (the trainer's own fit: {view.metrics.calibration.trainer_temperature.toFixed(3)}).
          </p>
        )}
      </StepCard>

      <StepCard number={4} title="Evaluate" steps={['evaluate', 'independent_test']} derived={derived}
        action={live && controls && approved ? <StepButtons steps={['evaluate', ...(view.run.independent_test ? ['independent_test'] : [])]} view={view} controls={controls} /> : undefined}>
        {view.metrics?.synthetic && ended('evaluate') && <TestCard result={view.metrics.synthetic} />}
        {view.metrics?.independent && ended('independent_test') && <TestCard result={view.metrics.independent} />}
        {live && controls && approved && <EvaluationControls view={view} controls={controls} />}
        {!live && view.run.gate && ended('publish') && (
          <p className="m-0 text-xs text-mute">Publish gate: at least {percent(view.run.gate.min_accuracy)} accuracy.</p>
        )}
      </StepCard>

      <StepCard number={5} title="Publish" steps={['publish']} derived={derived}
        action={live && controls && approved ? <StepButtons steps={['publish']} view={view} controls={controls} /> : undefined}>
        {view.publish && ended('publish') && (
          <Published publish={view.publish} spec={view.spec} live={live} controls={controls} clean={view.run.clean} onTry={onTry} />
        )}
      </StepCard>
    </div>
  );
}

const isoOf = (ms: number | undefined) => (ms === undefined ? null : new Date(ms).toISOString());

/** The /v1/decide request for the published example: its live field is the input, the other fields the context. */
function exampleCall(publish: PublishResult, spec: TaskSpec): DecideCall {
  const live = spec.live_field.name;
  if (!(live in publish.example.state)) throw new Error(`The published example's state has no ${live} field`);
  const { [live]: input, ...context } = publish.example.state;
  return { function: publish.function, input: input as Json, context: context as Record<string, Json> };
}

function Published({ publish, spec, live, controls, clean, onTry }: {
  publish: PublishResult; spec: TaskSpec | null; live: boolean; controls?: Controls; clean: boolean; onTry?: (request: TryFunction) => void;
}) {
  if (!spec) throw new Error('A published run has no spec');
  const call = exampleCall(publish, spec);
  const decision = publish.answer;
  return (
    <div className="flex flex-col gap-3">
      {publish.warning && <div><Badge tone="proprietary">{publish.warning}</Badge></div>}
      <p className="m-0 text-sm text-text">
        Published as <span className="font-mono font-semibold">{publish.adapter}</span>. The example's first question,
        decided as <span className="font-mono">{publish.function}</span>, answered in {publish.server_ms} ms of server time:
      </p>
      <div className="w-fit rounded-button border border-line px-3 py-2 text-sm">
        <span className="font-mono text-xs text-mute">{publish.function}</span>
        <p className="m-0 font-semibold text-text">
          {String(decision.value)}
          <span className="ml-2 font-normal text-mute">
            {decision.abstained ? 'abstained' : decision.confidence === null ? 'no confidence' : `confidence ${percent(decision.confidence)}`}
          </span>
        </p>
      </div>
      <p className="m-0 text-xs text-mute">
        Call it like any other function, with <span className="font-mono">POST /v1/decide</span>
        {live ? ' (it is served on this server now):' : ' on the server that published it:'}
      </p>
      <pre className="m-0 overflow-x-auto rounded-button border border-line bg-background p-3 font-mono text-xs text-text">
        {JSON.stringify({ calls: [call] }, null, 2)}
      </pre>
      <div className="flex flex-wrap gap-2">
        {live && onTry && (
          <Button variant="ghost" onClick={() => onTry({ functionId: publish.function, call })}>
            Try it in the playground<ArrowRight className="size-3.5" aria-hidden="true" />
          </Button>
        )}
        {live && controls && (
          <Button variant="ghost" pending={controls.acting === 'replay'} disabled={!clean} onClick={controls.saveReplay}
            title={clean ? 'Save as the recorded run' : 'Only a clean timed run can be saved as a replay'}>
            <Save className="size-4" aria-hidden="true" />Save as replay
          </Button>
        )}
      </div>
    </div>
  );
}

function ReferenceFiles({ factory, reference }: { factory: FactoryApi; reference: string }) {
  const [files, setFiles] = useState<string[] | null>(null);
  const [error, setError] = useState<ShownError | null>(null);
  useEffect(() => {
    factory.references().then((r) => {
      const found = r.references.find((item) => item.name === reference);
      if (!found) throw new Error(`The question set ${reference} is not on this server`);
      setFiles(found.files);
    }).catch((failure: unknown) => setError(shownError(failure)));
  }, [factory, reference]);
  if (error) return <ErrorAlert error={error} />;
  return files ? <Inspect factory={factory} reference={reference} files={files} /> : null;
}

function Tile({ label, value, note }: { label: string; value: string; note: string }) {
  return (
    <Card className="px-4 py-3">
      <p className="m-0 text-xs text-mute">{label}</p>
      <p className="m-0 mt-1 font-mono text-2xl font-semibold tracking-tight text-text">{value}</p>
      <p className="m-0 mt-0.5 text-xs text-mute">{note}</p>
    </Card>
  );
}

function StatusIcon({ timing }: { timing: StepTiming | undefined }) {
  if (!timing?.start) return <CircleDashed className="size-4 text-faint" aria-label="waiting" />;
  if (timing.error) return <XCircle className="size-4 text-danger" aria-label="failed" />;
  if (timing.running) return <Loader2 className="size-4 animate-spin text-primary" aria-label="running" />;
  return <CheckCircle2 className="size-4 text-success" aria-label="done" />;
}

function StepCard({ number, title, steps, derived, action, children }: {
  number: number; title: string; steps: string[]; derived: Derived; action?: ReactNode; children: ReactNode;
}) {
  return (
    <section className="overflow-hidden rounded-card border border-line bg-panel shadow-card" aria-label={title}>
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-4">
        <div className="min-w-0">
          <h2 className="m-0 text-lg font-semibold text-text"><span className="mr-2 text-mute">{number}.</span>{title}</h2>
          <ul className="m-0 mt-2 flex list-none flex-col gap-1 p-0" aria-label={`${title} steps`}>
            {steps.map((step) => {
              const timing = derived.steps[step];
              return (
                <li key={step} className="flex items-center gap-2 text-xs">
                  <StatusIcon timing={timing} />
                  <span className="w-56 text-text">{stepLabel(step)}</span>
                  <span className="font-mono text-mute">
                    {timing?.start ? `${timeOfDay(timing.start)}${timing.end ? ` → ${timeOfDay(timing.end)}` : ''} · ${clock(duration(timing, derived.now))}` : ''}
                  </span>
                </li>
              );
            })}
          </ul>
        </div>
        {action}
      </div>
      <div className="flex flex-col gap-5 px-5 py-5">{children}</div>
    </section>
  );
}

function StepButtons({ steps, view, controls }: { steps: string[]; view: RunContent; controls: Controls }) {
  const next = steps.find((step) => controls.rest[0] === step);
  const failed = steps.filter((step) => view.run.steps[step]?.status === 'failed');
  const shown = [...new Set([...(next ? [next] : []), ...failed])];
  if (!shown.length) return null;
  return (
    <div className="flex flex-wrap gap-2">
      {shown.map((step) => (
        <Button key={step} size="sm" variant="ghost" pending={controls.acting === step} disabled={controls.queued !== null} onClick={() => controls.runStep(step)}>
          {view.run.steps[step]?.status === 'failed' ? 'Retry' : 'Run'}: {stepLabel(step)}
        </Button>
      ))}
    </div>
  );
}

// --- step contents ----------------------------------------------------------------------------------------------------

function Generation({ derived }: { derived: Derived }) {
  const points = derived.generation;
  if (!points.length) {
    return derived.steps.data_prep?.running ? <Loading>Inventing families and writing the instruction pool</Loading> : null;
  }
  const latest = points[points.length - 1];
  const start = derived.steps.generate?.start;
  if (start === undefined) throw new Error('Data generation reported progress before its generate.start event');
  const minutes = (t: number) => (t - start) / 60000;
  const xMax = Math.max(0.5, minutes(derived.steps.generate?.end ?? derived.now));
  const series = SPLITS.map((split, index) => ({ name: split, color: SERIES_COLORS[index], points: points.map((p) => ({ x: minutes(p.t), y: rowsIn(p.rows, split) })) }));
  const teacher = latest.teacher;
  return (
    <div className="flex flex-col gap-4">
      <div className="grid gap-3 md:grid-cols-4">
        {SPLITS.map((split, index) => {
          const rows = rowsIn(latest.rows, split);
          const goal = latest.goal[split];
          return (
            <div key={split} className="flex flex-col gap-1.5">
              <div className="flex justify-between text-xs"><span className="text-text">{split}</span><span className="font-mono text-mute">{rows.toLocaleString()} / {goal.toLocaleString()}</span></div>
              <div className="h-2 overflow-hidden rounded-pill bg-background">
                <div className="h-full rounded-pill" style={{ width: `${Math.min(100, (rows / goal) * 100)}%`, background: SERIES_COLORS[index] }} />
              </div>
            </div>
          );
        })}
      </div>
      <p className="m-0 text-xs text-mute">
        Wave {latest.wave} · {latest.done} of {latest.calls} teacher calls this wave · {latest.failed} calls stayed invalid (recorded, made up later)
        {teacher && ` · ${teacher.calls} calls, ${teacher.invalid} invalid replies retried, ${((teacher.prompt_tokens + teacher.completion_tokens) / 1000).toFixed(0)}k tokens`}
        {' '}· goals include about 12% extra for the checks to drop duplicates
      </p>
      <LineChart series={series} xLabel="minutes" yLabel="rows written" xMax={Number(xMax.toFixed(1))} yMin={0} format={(v) => Math.round(v).toLocaleString()} />
    </div>
  );
}

/** Rows written so far for a split. The server counts rows per split as they are written, so a split with no rows
 * yet (every split in a wave's first progress line) is absent from its counts: that absence means 0 rows. */
function rowsIn(rows: Record<string, number>, split: string): number {
  return split in rows ? rows[split] : 0;
}

function ChecksReport({ checks }: { checks: NonNullable<RunView['checks']> }) {
  const firstQuestion = Object.keys(checks.balance)[0];
  const balance = Object.entries(checks.balance[firstQuestion]);
  const most = Math.max(...balance.map(([, n]) => n));
  const totalDropped = (record: Record<string, number>) => Object.values(record).reduce((a, b) => a + b, 0);
  const instructions = checks.instructions.train;
  const instructionTotal = Object.values(instructions).reduce((a, b) => a + b, 0);
  return (
    <div className="flex flex-col gap-4 border-t border-line pt-4">
      <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
        {SPLITS.map((split) => (
          <div key={split} className="rounded-button border border-line px-3 py-2">
            <p className="m-0 text-xs text-mute">{split}</p>
            <p className="m-0 font-mono text-lg font-semibold text-text">{checks.rows[split].toLocaleString()} <span className="text-xs font-normal text-mute">rows · {checks.families[split]} families</span></p>
          </div>
        ))}
      </div>
      <p className="m-0 text-xs text-mute">
        Dropped {totalDropped(checks.duplicates_dropped)} exact duplicates and {totalDropped(checks.near_copies_dropped)} near-copies of training requests
        ({checks.near_copy_rule}). Test families never appear in training. Tokens per row: median {checks.tokens.median}, 95th percentile {checks.tokens.p95}, longest {checks.tokens.max}.
        Instructions in training: {Object.entries(instructions).map(([kind, n]) => `${kind} ${percent(n / instructionTotal)}`).join(', ')}.
      </p>
      <div className="flex flex-col gap-1.5">
        <p className="m-0 text-xs font-medium text-text">Answers to <span className="font-mono">{firstQuestion}</span> in training</p>
        {balance.map(([key, count]) => (
          <div key={key} className="grid grid-cols-[12rem_1fr_3rem] items-center gap-3 text-xs">
            <span className="truncate font-mono text-mute">{key}</span>
            <div className="h-2 overflow-hidden rounded-pill bg-background"><div className="h-full rounded-pill bg-primary" style={{ width: `${(count / most) * 100}%` }} /></div>
            <span className="text-right font-mono text-mute">{count}</span>
          </div>
        ))}
      </div>
      <details className="rounded-card border border-line">
        <summary className="cursor-pointer px-3 py-2 text-xs font-medium text-text">Read {checks.sample.length} sample rows</summary>
        <div className="divide-y divide-line">
          {checks.sample.map((row, index) => (
            <div key={index} className="flex flex-col gap-1 px-3 py-2 text-xs">
              <p className="m-0 font-mono text-text">{JSON.stringify(row.state)}</p>
              <p className="m-0 text-mute">{row.question.instructions === undefined ? '(no instructions: a row asked with the options only)' : row.question.instructions} → <span className="font-mono font-semibold text-text">{String(row.label)}</span>
                <span className="ml-2">{row.kind} · {row.style} · {row.instruction} wording</span></p>
            </div>
          ))}
        </div>
      </details>
    </div>
  );
}

function Training({ derived }: { derived: Derived }) {
  const steps = derived.trainSteps;
  const evals = derived.evals;
  if (!steps.length && !evals.length) {
    return derived.steps.train?.running ? <Loading>Loading the model and the data</Loading> : null;
  }
  const launch = derived.launch;
  if (!launch) throw new Error('Training reported progress before its train.launch event');
  const updates = launch.updates;
  if (typeof updates !== 'number') throw new Error('The train.launch event has no number of updates');
  const latest = evals[evals.length - 1];
  return (
    <div className="flex flex-col gap-5">
      <p className="m-0 text-xs text-mute">
        Trainer commit <span className="font-mono">{String(launch.trainer_commit)}</span> · {updates} updates at batch 16 ·
        training rows repeated {String(launch.passes)}× · GPU {String(launch.gpu_free_gb)} of {String(launch.gpu_total_gb)} GB free at launch
      </p>
      <div className="grid gap-6 lg:grid-cols-2">
        <div>
          <p className="m-0 mb-1 text-sm font-medium text-text">Loss</p>
          <LineChart xLabel="update" yLabel="loss" xMax={updates} yMin={0} series={[
            { name: 'training loss', color: SERIES_COLORS[0], points: steps.map((p) => ({ x: p.step, y: p.loss })) },
            { name: 'eval loss (dev)', color: SERIES_COLORS[1], points: evals.map((p) => ({ x: p.step, y: p.dev_loss })), markers: true },
          ]} />
        </div>
        <div>
          <p className="m-0 mb-1 text-sm font-medium text-text">Dev accuracy {latest && <span className="font-mono text-mute">· {percent(latest.dev_accuracy)}</span>}</p>
          <LineChart xLabel="update" yLabel="dev accuracy" xMax={updates} yMin={0} yMax={1} format={(v) => `${Math.round(v * 100)}%`} series={[
            { name: 'dev accuracy', color: SERIES_COLORS[0], points: evals.map((p) => ({ x: p.step, y: p.dev_accuracy })), markers: true },
          ]} />
        </div>
      </div>
    </div>
  );
}

function TestCard({ result }: { result: TestResult }) {
  return (
    <div className="flex flex-col gap-3 rounded-card border border-line p-4">
      <div className="flex flex-wrap items-baseline justify-between gap-3">
        <Badge tone={result.label.startsWith('independent') ? 'ok' : 'info'}>{result.label}</Badge>
        <span className="text-xs text-mute">{result.rows.toLocaleString()} rows · calibration error {percent(result.ece)} (10 bins)</span>
      </div>
      {result.calls && (
        <div>
          <p className="m-0 text-xs text-mute">Whole call exactly right ({result.calls.calls.toLocaleString()} commands, {result.calls.questions_per_call} questions each on average)</p>
          <p className="m-0 font-mono text-3xl font-semibold text-text">{percent(result.calls.exact_match)}
            <span className="ml-2 text-sm font-normal text-mute">95% interval {percent(result.calls.interval[0])}–{percent(result.calls.interval[1])} · call confidence calibration error {percent(result.calls.confidence_ece)}</span></p>
        </div>
      )}
      <div>
        <p className="m-0 text-xs text-mute">Per question</p>
        <p className={`m-0 font-mono font-semibold text-text ${result.calls ? 'text-xl' : 'text-3xl'}`}>{percent(result.accuracy)}
          <span className="ml-2 text-sm font-normal text-mute">95% interval {percent(result.interval[0])}–{percent(result.interval[1])}</span></p>
      </div>
      <div className="divide-y divide-line text-xs">
        {Object.entries(result.questions).map(([qid, q]) => (
          <div key={qid} className="grid grid-cols-[1fr_6rem_10rem] gap-3 py-1.5">
            <span className="truncate font-mono text-text" title={Object.entries(q.by_option).map(([k, v]) => `${k}: ${percent(v.accuracy)} of ${v.rows}`).join('\n')}>{qid}</span>
            <span className="text-right font-mono text-text">{percent(q.accuracy)}</span>
            <span className="text-right font-mono text-mute">{q.rows} rows · {percent(q.interval[0])}–{percent(q.interval[1])}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

function AttachTestset({ controls }: { controls: Controls }) {
  const [names, setNames] = useState<string[] | null>(null);
  const [chosen, setChosen] = useState('');
  const [error, setError] = useState<ShownError | null>(null);
  const list = controls.listTestsets;
  useEffect(() => {
    list?.().then(setNames, (failure: unknown) => setError(shownError(failure)));
    // once, when the control appears: the controls object is rebuilt on every render
  }, []);
  if (error) return <ErrorAlert error={error} />;
  if (!controls.attachTestset || !names?.length) return null;
  return (
    <div className="flex items-center gap-2 pt-1">
      <div className="w-72">
        <Select aria-label="Frozen test set" value={chosen} onChange={(e) => setChosen(e.target.value)}>
          <option value="" disabled>Use a frozen test set</option>
          {names.map((n) => <option key={n} value={n}>{n}</option>)}
        </Select>
      </div>
      <Button size="sm" variant="ghost" disabled={!chosen} onClick={() => controls.attachTestset?.(chosen)}>Attach</Button>
    </div>
  );
}

function EvaluationControls({ view, controls }: { view: RunContent; controls: Controls }) {
  const [gate, setGate] = useState(view.run.gate ? String(view.run.gate.min_accuracy * 100) : '');
  const file = useRef<HTMLInputElement | null>(null);
  const gateId = useId();
  return (
    <div className="grid gap-4 border-t border-line pt-4 md:grid-cols-2">
      <div className="flex flex-col gap-1.5">
        <p className="m-0 text-xs text-mute">{view.run.mode === 'import'
          ? 'Independent test set: JSON Lines of {"id", "command", "function", "arguments"}, each call checked against the functions before scoring'
          : 'Independent test set (CSV or JSON Lines: question_id, label, optional call_id, and the state fields)'}</p>
        <div className="flex items-center gap-2">
          <Button variant="ghost" size="sm" onClick={() => file.current?.click()} disabled={controls.queued !== null}>
            <Upload className="size-4" aria-hidden="true" />{view.run.independent_test ? 'Replace test set' : 'Upload test set'}
          </Button>
          {view.run.independent_test && <span className="text-xs text-mute">{view.run.independent_test.rows} rows, {view.run.independent_test.format}</span>}
        </div>
        {view.run.mode === 'import' && <AttachTestset controls={controls} />}
        <input ref={file} type="file" hidden accept=".csv,.jsonl" aria-label="Independent test set file" onChange={async (e) => {
          const chosen = e.target.files?.[0];
          e.target.value = '';
          if (chosen) controls.uploadTest(chosen.name.endsWith('.csv') ? 'csv' : view.run.mode === 'import' ? 'calls' : 'jsonl', await chosen.text());
        }} />
      </div>
      <div className="flex flex-col gap-1.5">
        <label htmlFor={gateId} className="text-xs text-mute">Publish only at or above this accuracy (%) {view.run.independent_test ? 'on the independent test' : 'on the synthetic test (no independent test: shown with a warning)'}</label>
        <div className="flex gap-2">
          <Input id={gateId} type="number" min={1} max={100} className="w-28" value={gate} onChange={(e) => setGate(e.target.value)} />
          <Button variant="ghost" size="sm" pending={controls.acting === 'gate'} disabled={!gate}
            onClick={() => controls.setGate(Number(gate) / 100)}>Set gate</Button>
          {view.run.gate && <span className="self-center text-xs text-mute">now {percent(view.run.gate.min_accuracy)}</span>}
        </div>
      </div>
    </div>
  );
}
