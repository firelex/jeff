// The adapter factory's endpoints (/v1/factory, apps/server/src/jeff_server/factory/routes.py), as the factory page
// uses them. Every call is the owner's: it sends the console session (a hub token is refused, 403 NotLicensed).
// Build calls answer 403 FactoryOff when the server runs with JEFF_FACTORY=off; the mode, the replays and, when the
// server has teacher settings, description checks and previews work either way.
import type { Api, Decision } from '../../api';

export interface FactoryEvent { t: string; kind: string; [field: string]: unknown }

export interface SpecOption { key: string; description: string }
export interface SpecQuestion {
  id: string; type: 'choice' | 'noul' | 'score'; instruction: string; options: SpecOption[]; applies_when: string | null;
  requires?: { question: string; equals: string }[]; imported?: boolean;
}
export interface TaskSpec {
  name: string;
  summary: string;
  family: { noun: string; description: string; fields: { name: string; description: string }[] };
  live_field: { name: string; description: string };
  questions: SpecQuestion[];
  hard_cases: { kind: string; description: string }[];
  styles: string[];
  sizes: { train: number; updates: number; dev: number; calibration: number; test: number; families: number };
}

export interface DescriptionCheckResult {
  ready: boolean; summary: string; problems: { severity: 'blocking' | 'important' | 'minor'; text: string }[];
  questions: string[]; improved_description: string; changes: string[];
}
export interface DescriptionCheck { id: string; started: string; ended: string; description: string; teacher_model: string; result: DescriptionCheckResult }
export interface ConfirmedCheck {
  id: string; choice: 'original' | 'improved' | 'edited'; original: string; improved: string; ready: boolean; summary: string;
  problems: DescriptionCheckResult['problems']; questions: string[]; changes: string[]; teacher_model: string;
}

export interface StepState { status: 'running' | 'done' | 'failed'; started?: string; ended?: string; error?: string | null }
/** A run's state as a replay records it (replay.public_state). `description_check` is null for an imported run,
 * which has no description check. */
export interface RecordedRunState {
  id: string; created: string; description: string; status: string; clean: boolean; approved: string | null;
  spec_sha256: string | null; gate: { min_accuracy: number } | null;
  independent_test: { rows: number; uploaded: string; format: string } | null;
  steps: Record<string, StepState>; description_check: ConfirmedCheck | null;
  mode: 'prompt' | 'import'; reference: string | null; spec_from: string | null;
}
/** A live run's or preview's state (routes.view): the recorded state plus why it is not a clean timed run. */
export interface RunState extends RecordedRunState { unclean_reasons: string[] }

export interface TestResult {
  label: string; rows: number; accuracy: number; interval: [number, number]; ece: number; log_loss: number;
  temperature: number; seconds: number;
  questions: Record<string, { rows: number; accuracy: number; interval: [number, number]; ece: number; by_option: Record<string, { rows: number; accuracy: number }> }>;
  calls: { calls: number; exact_match: number; interval: [number, number]; questions_per_call: number; confidence_ece: number } | null;
}
export interface Metrics {
  calibration?: { temperature: number; trainer_temperature: number; rows: number; seconds: number };
  synthetic?: TestResult;
  independent?: TestResult;
}
export interface Checks {
  rows: Record<string, number>; duplicates_dropped: Record<string, number>;
  near_copies_dropped: Record<string, number>; near_copy_rule: string; families: Record<string, number>;
  balance: Record<string, Record<string, number>>; instructions: Record<string, Record<string, number>>;
  tokens: { min: number; median: number; p95: number; max: number };
  sample: { family: string; state: unknown; question: { type: string; instructions?: string }; label: unknown; kind: string; style: string; instruction: string }[];
}
export interface TrainSummary { steps: number; temperature: number; git_commit: string; trainer_commit: string; gpu_seconds: number; passes: number; adapter: string }

/** publish.json: the adapter folder, its catalogue ID (jeff.factory.<name>), the held-out example asked as
 * /v1/systemone takes it, and its first question decided as `function` through /v1/decide's own path. */
export interface PublishResult {
  folder: string; adapter: string; served: unknown; function: string;
  example: { model: string; state: Record<string, unknown>; questions: Record<string, unknown> };
  answer: Decision; server_ms: number; warning: string | null;
}

/** A queued build job: waiting for the worker, or running. */
export interface Job { id: string; run: string; steps: string[]; queued: string; state: 'waiting' | 'running' }

// A live run (owner mode) or a preview, as GET /v1/factory/{runs,previews}/<id> answers (routes.view). The result
// files (checks, metrics, train, publish) exist only once their step has written them. `job` is null for a preview,
// which is never queued, and for a run with no job waiting or running.
export interface RunView {
  run: RunState; spec: TaskSpec | null;
  checks?: Checks; metrics?: Metrics; train?: TrainSummary; publish?: PublishResult;
  rest: string[]; busy: string | null; job: Job | null;
}
// A recorded run, as GET /v1/factory/replays/<id> answers (replay.detail): a finished run, so every file is there.
export interface ReplayView {
  run: RecordedRunState; spec: TaskSpec; events: FactoryEvent[];
  checks: Checks; metrics: Metrics; train: TrainSummary; publish: PublishResult;
}
/** What the run's cards draw, from either. */
export type RunContent = Pick<RunView, 'checks' | 'metrics' | 'publish'> & { run: RecordedRunState; spec: TaskSpec | null };
export interface Reference { name: string; questions: number; description: string; calls: boolean; readme: string | null; files: string[] }
export interface ReplaySummary { id: string; name: string; summary: string; created: string; accuracy: Record<string, number> }
export interface RunSummary { id: string; created: string; status: string; clean: boolean; approved: string | null; name: string | null }
export interface FactoryMode { mode: 'off' | 'owner'; replays: boolean; description_check: boolean; preview: boolean }
export interface WorkerStatus { running: boolean; heartbeat_age_s: number | null; current_job: Omit<Job, 'state'> | null }

export interface Call { function: string; arguments: Record<string, unknown> }
export interface TestItem {
  id: string; command: string; writer_call: Call; judge_call?: Call | null; judge_error?: string; status: 'agreed' | 'disputed' | 'cookbook';
  pattern: string; function: string; writer: string; source?: string;
}
export interface Review { verdict: 'confirm' | 'correct' | 'drop'; call: Call | null; at: string }
export interface TestSet {
  summary: Record<string, unknown>; candidates: TestItem[]; reviews: Record<string, Review>;
  frozen: { frozen: string; items: number; checked_by_owner: number; sha256: string } | null; min_owner_checks: number;
}
export interface TestSetSummary { name: string; items: number; reviewed: number; frozen: TestSet['frozen']; summary: Record<string, unknown> }

export type Choice = 'original' | 'improved' | 'edited';
export interface StartBody { check_id: string; choice: Choice; description: string; name?: string }
export interface ImportBody { reference: string; name: string; description: string }
/** Where a run lives: build runs (owner mode, queued for the worker) or previews (interpretation only, in the API). */
export type Scope = 'runs' | 'previews';

const part = encodeURIComponent;

export type FactoryApi = ReturnType<typeof factoryApi>;

export function factoryApi(api: Api) {
  const get = <T>(path: string) => api.factory<T>('GET', path);
  const post = <T>(path: string, body?: unknown) => api.factory<T>('POST', path, body);
  const put = <T>(path: string, body: unknown) => api.factory<T>('PUT', path, body);
  return {
    mode: () => get<FactoryMode>('/mode'),
    worker: () => get<WorkerStatus>('/worker'),
    replays: () => get<{ replays: ReplaySummary[] }>('/replays'),
    replay: (id: string) => get<ReplayView>(`/replays/${part(id)}`),
    runs: () => get<{ runs: RunSummary[]; busy: string | null }>('/runs'),
    checkDescription: (body: { description: string; examples?: string }) => post<DescriptionCheck>('/description-checks', body),
    references: () => get<{ references: Reference[] }>('/references'),
    referenceFile: (reference: string, file: string) =>
      api.factoryText(`/references/${part(reference)}/files/${file.split('/').map(part).join('/')}`),
    // A run or a preview: the same calls under /runs or /previews.
    start: (scope: Scope, body: StartBody) => post<RunView>(`/${scope}`, body),
    startImport: (scope: Scope, body: ImportBody) => post<RunView>(scope === 'runs' ? '/runs/import' : '/previews/import', body),
    run: (scope: Scope, id: string) => get<RunView>(`/${scope}/${part(id)}`),
    saveSpec: (scope: Scope, id: string, spec: TaskSpec) => put<RunView>(`/${scope}/${part(id)}/spec`, { spec }),
    approve: (scope: Scope, id: string) => post<RunView>(`/${scope}/${part(id)}/approve`),
    // Build runs only: each queues a job for the factory worker.
    step: (id: string, step: string) => post<RunView>(`/runs/${part(id)}/steps/${part(step)}`),
    runRest: (id: string) => post<RunView>(`/runs/${part(id)}/run-rest`),
    gate: (id: string, min_accuracy: number) => put<RunView>(`/runs/${part(id)}/gate`, { min_accuracy }),
    testSet: (id: string, format: 'csv' | 'jsonl' | 'calls', content: string) => post<RunView>(`/runs/${part(id)}/test-set`, { format, content }),
    attachTestset: (id: string, name: string) => post<RunView>(`/runs/${part(id)}/testset/${part(name)}`),
    saveReplay: (id: string) => post<ReplaySummary>(`/runs/${part(id)}/replay`),
    testsets: () => get<{ testsets: TestSetSummary[] }>('/testsets'),
    testset: (name: string) => get<TestSet>(`/testsets/${part(name)}`),
    review: (name: string, body: { item_id: string; verdict: Review['verdict']; call?: Call }) => post<Review>(`/testsets/${part(name)}/review`, body),
    freeze: (name: string) => post<TestSet['frozen']>(`/testsets/${part(name)}/freeze`),
  };
}

/**
 * Follows a run's events (server-sent events) from the first line on. Returns a stop function. A failure to open
 * the stream, or the stream breaking, goes to `onError`; stopping is not a failure.
 */
export function followEvents(api: Api, scope: Scope, id: string, onEvent: (event: FactoryEvent) => void,
                             onError: (error: unknown) => void): () => void {
  const controller = new AbortController();
  void (async () => {
    const response = await api.factoryStream(`/${scope}/${part(id)}/events`, controller.signal);
    if (!response.body) throw new Error(`GET /v1/factory/${scope}/${id}/events answered without a body`);
    const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        if (!controller.signal.aborted) throw new Error(`The event stream of ${id} ended; reload the page to follow it again`);
        return;
      }
      buffer += value;
      let end: number;
      while ((end = buffer.indexOf('\n\n')) >= 0) {
        const block = buffer.slice(0, end);
        buffer = buffer.slice(end + 2);
        const data = block.split('\n').find((line) => line.startsWith('data: '));
        if (data) onEvent(JSON.parse(data.slice(6)) as FactoryEvent);
      }
    }
  })().catch((error: unknown) => {
    if (!controller.signal.aborted) onError(error);
  });
  return () => controller.abort();
}
