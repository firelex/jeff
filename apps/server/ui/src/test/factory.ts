import type { FactoryEvent, ReplayView, RunState, RunView, TaskSpec } from '../pages/factory/api';

// A small finished factory run, in the shapes GET /v1/factory/replays/<id> and /runs/<id> answer
// (apps/server/src/jeff_server/factory/routes.py), for the factory page's tests.

export const SPEC: TaskSpec = {
  name: 'parcel-claims',
  summary: 'Which claim a customer makes about a parcel',
  family: { noun: 'shop', description: 'An online shop', fields: [{ name: 'shop', description: 'The shop' }] },
  live_field: { name: 'message', description: 'The customer\'s message' },
  questions: [{
    id: 'claim', type: 'choice', instruction: 'What does the customer claim happened to the parcel?', applies_when: null,
    options: [{ key: 'damaged', description: 'It arrived broken' }, { key: 'lost', description: 'It never arrived' }],
  }],
  hard_cases: [{ kind: 'negation', description: 'Not damaged, only late' }],
  styles: ['terse'],
  sizes: { train: 2000, updates: 250, dev: 200, calibration: 300, test: 400, families: 60 },
};

const at = (seconds: number) => new Date(Date.UTC(2026, 9, 20, 9, 0, seconds)).toISOString();
const TEACHER = { calls: 1, cached: 0, invalid: 0, retries: 0, prompt_tokens: 1000, completion_tokens: 500, seconds: 10 };
const IDLE = { ...TEACHER, calls: 0, prompt_tokens: 0, completion_tokens: 0, seconds: 0 };
const GOAL = { train: 2240, dev: 224, calibration: 336, test: 448 };

export const EVENTS: FactoryEvent[] = [
  { t: at(0), kind: 'description_check.start' },
  { t: at(30), kind: 'description_check.end', teacher: TEACHER, ready: true, problems: 0 },
  { t: at(30), kind: 'confirm.start' },
  { t: at(60), kind: 'confirm.end', choice: 'original' },
  { t: at(61), kind: 'run.start', description_chars: 120 },
  { t: at(61), kind: 'interpret.start' },
  { t: at(90), kind: 'interpret.end', seconds: 29, teacher: TEACHER },
  { t: at(90), kind: 'review.start' },
  { t: at(200), kind: 'review.end', spec_sha256: 'ab'.repeat(32) },
  { t: at(201), kind: 'data_prep.start' },
  { t: at(260), kind: 'data_prep.end', seconds: 59, teacher: TEACHER },
  { t: at(261), kind: 'generate.start' },
  { t: at(262), kind: 'generate.progress', phase: 'wave', wave: 1, calls: 20, rows: {}, goal: GOAL },
  { t: at(400), kind: 'generate.progress', phase: 'rows', wave: 1, done: 20, calls: 20, rows: GOAL, goal: GOAL, failed_calls: 1, teacher: TEACHER },
  { t: at(401), kind: 'generate.end', seconds: 140, teacher: TEACHER },
  { t: at(402), kind: 'checks.start' },
  { t: at(420), kind: 'checks.end', seconds: 18, teacher: IDLE },
  { t: at(421), kind: 'train.start' },
  { t: at(425), kind: 'train.launch', trainer_commit: '4e1c9a2', passes: 2, updates: 250, gpu_free_gb: 60, gpu_total_gb: 80 },
  { t: at(500), kind: 'train.step', step: 125, loss: 0.4 },
  { t: at(510), kind: 'train.eval', step: 125, dev_accuracy: 0.9, dev_loss: 0.45, dev_ece: 0.03 },
  { t: at(600), kind: 'train.step', step: 250, loss: 0.1 },
  { t: at(610), kind: 'train.end', seconds: 189, teacher: IDLE, gpu_seconds: 180 },
  { t: at(611), kind: 'calibrate.start' },
  { t: at(620), kind: 'calibrate.end', seconds: 9, teacher: IDLE, gpu_seconds: 8 },
  { t: at(621), kind: 'evaluate.start' },
  { t: at(640), kind: 'evaluate.end', seconds: 19, teacher: IDLE, gpu_seconds: 18 },
  { t: at(641), kind: 'publish.start' },
  { t: at(650), kind: 'publish.end', seconds: 9, teacher: IDLE },
  { t: at(650), kind: 'run.end', adapter: 'parcel-claims' },
];

export const RUN: RunState = {
  id: 'parcel-claims-20261020-090101', created: at(61), description: 'Read a customer message about a parcel.', status: 'published',
  clean: true, approved: at(200), spec_sha256: 'ab'.repeat(32), gate: { min_accuracy: 0.9 }, independent_test: null,
  steps: {}, unclean_reasons: [], description_check: null, mode: 'prompt', reference: null, spec_from: null,
};

const RESULT = {
  label: 'synthetic held-out test', rows: 400, accuracy: 0.96, interval: [0.94, 0.98] as [number, number], ece: 0.02,
  log_loss: 0.1, temperature: 1.1, seconds: 18, calls: null,
  questions: { claim: { rows: 400, accuracy: 0.96, interval: [0.94, 0.98] as [number, number], ece: 0.02, by_option: {} } },
};

export const REPLAY: ReplayView = {
  run: RUN, spec: SPEC, events: EVENTS,
  checks: {
    rows: { train: 2100, dev: 210, calibration: 320, test: 420 }, duplicates_dropped: { train: 3 }, near_copies_dropped: { test: 2 },
    near_copy_rule: 'word 5-grams overlapping 60%', families: { train: 46, dev: 4, calibration: 4, test: 6 },
    balance: { claim: { damaged: 1100, lost: 1000 } }, instructions: { train: { canonical: 700, reworded: 1400 } },
    tokens: { min: 100, median: 180, p95: 240, max: 300 }, sample: [],
  },
  metrics: { calibration: { temperature: 1.08, trainer_temperature: 1.1, rows: 320, seconds: 8 }, synthetic: RESULT },
  train: { steps: 250, temperature: 1.1, git_commit: '4e1c9a2', trainer_commit: '4e1c9a2', gpu_seconds: 180, passes: 2, adapter: '/a' },
  publish: {
    folder: '/adapters/parcel-claims', adapter: 'jeff.factory.parcel-claims', served: [], function: 'jeff.factory.parcel-claims.claim',
    example: { model: 'parcel-claims', state: { shop: 'A shoe shop', message: 'The sole came off' }, questions: {} },
    answer: {
      value: 'damaged', confidence: 0.98, abstained: false, calibrated: true, interface: 'classify', function: 'jeff.factory.parcel-claims.claim',
      version: '1.0.0', probabilities: { damaged: 0.98, lost: 0.02 }, score: null, route: [], decided_by: 'jeff.factory.parcel-claims.claim@1.0.0',
      escalations: [], timing: {}, evidence_refs: [],
    },
    server_ms: 4.2, warning: null,
  },
};

/** A build run approved and waiting for its build steps: "Run the rest" is offered. */
export const APPROVED: RunView = {
  run: { ...RUN, status: 'review done', approved: at(200), gate: null },
  spec: SPEC, rest: ['data_prep', 'generate', 'checks', 'train', 'calibrate', 'evaluate', 'publish'], busy: null, job: null,
};
