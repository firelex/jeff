import type { Decision, FunctionListing } from '../api';

// Listings as GET /v1/functions returns them (jeff.listing.function_listing), from catalogue/jeff.support.triage.
const TRIAGE_CONTEXT = [
  { key: 'company', code_name: 'company', description: 'One sentence about the organisation the message is sent to.', required: true },
  { key: 'channel', code_name: 'channel', description: 'Where the message came from.', required: true },
];
const TRIAGE_INPUT = { key: 'message', code_name: 'message', description: 'The ticket or email as received.', form: 'fields' } as const;
const CALIBRATION = {
  data: 'hf://mstrasser/jeff-adapter-triage@v1.3/calibration.jsonl', rows: 367, target_precision: 0.95,
  threshold: 0.405481, precision_at_threshold: 0.950954, coverage_at_threshold: 1.0,
};

export const TRIAGE_ROUTE: FunctionListing = {
  id: 'jeff.support.triage.route',
  interface: 'route',
  type: 'choice',
  labels: { source: 'caller', fixed_first: { other: 'Not for any of these teams' }, caller_keys: 'k{n}', max: 40 },
  context: TRIAGE_CONTEXT,
  input: TRIAGE_INPUT,
  calibration: CALIBRATION,
  adapter_version: '1.3.0',
  licence: { kind: 'open', adapter: 'Apache-2.0' },
};

export const TRIAGE_URGENCY: FunctionListing = {
  ...TRIAGE_ROUTE,
  id: 'jeff.support.triage.urgency',
  interface: 'rate',
  type: 'score',
  labels: { source: 'fixed', levels: ['No action needed', 'Can wait a few days', 'Handle within a day', 'Handle within hours', 'Handle immediately'] },
};

export const SPAM: FunctionListing = {
  id: 'jeff.safety.spam.is_spam',
  interface: 'detect',
  type: 'noul',
  labels: { source: 'fixed' },
  context: [{ key: 'channel', code_name: 'channel', description: 'Where the message arrived.', required: false }],
  input: { key: 'message', code_name: 'message', description: 'The message.', form: 'fields' },
  calibration: null,
  adapter_version: '1.3.0',
  licence: { kind: 'open', adapter: 'Apache-2.0' },
};

export function decision(overrides: Partial<Decision> = {}): Decision {
  return {
    value: 'k1',
    confidence: 0.91,
    abstained: false,
    calibrated: true,
    interface: 'route',
    function: 'jeff.support.triage.route',
    version: '1.3.0',
    probabilities: { other: 0.04, k1: 0.91, k2: 0.05 },
    score: null,
    route: [],
    decided_by: 'jeff.support.triage.route@1.3.0',
    escalations: [],
    timing: { queue_ms: 0.4, model_ms: 3.1 },
    evidence_refs: [],
    ...overrides,
  };
}
