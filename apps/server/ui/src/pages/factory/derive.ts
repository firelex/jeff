// Everything the page shows is derived from the run's events up to a moment: the live page passes "now", a replay
// passes its playback clock. The same code draws both, so a replay can only show what the run recorded.
import type { FactoryEvent } from './api';

export const STEP_LABELS: Record<string, string> = {
  description_check: 'Check the description (teacher)', confirm: 'Confirm the description (you)', interpret: 'Interpret the description', review: 'Review the spec (you)', data_prep: 'Prepare generation',
  generate: 'Write the rows (teacher)', checks: 'Check the data', train: 'Train', calibrate: 'Calibrate confidence',
  evaluate: 'Held-out synthetic test', independent_test: 'Independent test (your labels)', publish: 'Publish and serve',
};

/** A step's label; a step this page does not know is an error, never shown under a made-up name. */
export function stepLabel(step: string): string {
  const label = STEP_LABELS[step];
  if (label === undefined) throw new Error(`The factory reported a step this page does not know: ${step}`);
  return label;
}

export interface StepTiming { start?: number; end?: number; error?: string; running: boolean }
export interface GenerationPoint { t: number; rows: Record<string, number>; goal: Record<string, number>; done: number; calls: number; wave: number; failed: number; teacher?: Record<string, number> }
export interface TrainPoint { step: number; loss: number; t: number }
export interface EvalPoint { step: number; dev_accuracy: number; dev_loss: number; dev_ece: number; t: number }

export interface Derived {
  start?: number; end?: number; now: number;
  steps: Record<string, StepTiming>;
  generation: GenerationPoint[];
  trainSteps: TrainPoint[]; evals: EvalPoint[];
  launch?: FactoryEvent;
  teacher: { calls: number; prompt_tokens: number; completion_tokens: number; seconds: number; invalid: number };
  gpuSeconds: number;
  failed?: { step: string; error: string };
}

const time = (event: FactoryEvent) => Date.parse(event.t);
const TEACHER_FIELDS = ['calls', 'prompt_tokens', 'completion_tokens', 'seconds', 'invalid'] as const;

function number(event: FactoryEvent, field: string): number {
  const value = event[field];
  if (typeof value !== 'number') throw new Error(`The ${event.kind} event at ${event.t} has no number ${field}`);
  return value;
}

function counts(event: FactoryEvent, field: string): Record<string, number> {
  const value = event[field];
  if (typeof value !== 'object' || value === null) throw new Error(`The ${event.kind} event at ${event.t} has no ${field}`);
  return value as Record<string, number>;
}

export function derive(events: FactoryEvent[], cutoff: number): Derived {
  const result: Derived = { now: cutoff, steps: {}, generation: [], trainSteps: [], evals: [],
    teacher: { calls: 0, prompt_tokens: 0, completion_tokens: 0, seconds: 0, invalid: 0 }, gpuSeconds: 0 };
  for (const event of events) {
    const t = time(event);
    if (t > cutoff) break;
    const [step, phase] = event.kind.split('.');
    if (result.start === undefined) result.start = t; // the timeline starts with the description check
    if (event.kind === 'run.start') continue;
    else if (event.kind === 'run.end') result.end = t;
    else if (event.kind === 'step.failed') {
      const failed = event.step as string;
      result.steps[failed] = { ...result.steps[failed], end: t, error: event.error as string, running: false };
      result.failed = { step: failed, error: event.error as string };
    } else if (phase === 'start') {
      result.steps[step] = { start: t, running: true };
      if (result.failed?.step === step) result.failed = undefined;
    } else if (phase === 'end') {
      result.steps[step] = { ...result.steps[step], end: t, running: false };
      // Steps that ask the teacher record its use; the person's own steps (confirm, review) do not.
      if (event.teacher !== undefined) {
        const teacher = counts(event, 'teacher');
        for (const field of TEACHER_FIELDS) {
          if (typeof teacher[field] !== 'number') throw new Error(`The ${event.kind} event at ${event.t} has no teacher ${field}`);
          result.teacher[field] += teacher[field];
        }
      }
      if (event.gpu_seconds !== undefined) result.gpuSeconds += number(event, 'gpu_seconds');
    } else if (event.kind === 'generate.progress') {
      const previous = result.generation[result.generation.length - 1];
      // A wave's first line comes before any of its calls answer, so it carries no done, failed_calls or teacher
      // fields: none of its calls are done, and the failures and teacher use are those of the line before it (none
      // at all before the first wave's first line).
      const opening = event.phase === 'wave';
      result.generation.push({ t, rows: counts(event, 'rows'), goal: counts(event, 'goal'),
        done: opening ? 0 : number(event, 'done'), calls: number(event, 'calls'), wave: number(event, 'wave'),
        failed: opening ? (previous === undefined ? 0 : previous.failed) : number(event, 'failed_calls'),
        teacher: opening ? previous?.teacher : counts(event, 'teacher') });
    } else if (event.kind === 'train.step') {
      result.trainSteps.push({ step: number(event, 'step'), loss: number(event, 'loss'), t });
    } else if (event.kind === 'train.eval') {
      result.evals.push({ step: number(event, 'step'), dev_accuracy: number(event, 'dev_accuracy'),
        dev_loss: number(event, 'dev_loss'), dev_ece: number(event, 'dev_ece'), t });
    } else if (event.kind === 'train.launch') {
      result.launch = event;
    }
  }
  return result;
}

export function duration(timing: StepTiming | undefined, now: number): number | undefined {
  if (!timing?.start) return undefined;
  return (timing.end ?? now) - timing.start;
}

// The headline figures: build time without the person's own time (confirming the description, reviewing the spec),
// that human time on its own, and the build time with the evaluation-only step (independent test) added back.
export function headline(derived: Derived) {
  if (derived.start === undefined) return undefined;
  const elapsed = (derived.end ?? derived.now) - derived.start;
  const review = (duration(derived.steps.review, derived.now) ?? 0) + (duration(derived.steps.confirm, derived.now) ?? 0);
  const evaluationOnly = duration(derived.steps.independent_test, derived.now) ?? 0;
  return { build: elapsed - review - evaluationOnly, review, withEvaluation: elapsed - review, finished: derived.end !== undefined };
}

export function clock(ms: number | undefined): string {
  if (ms === undefined) return '–';
  const seconds = Math.max(0, Math.round(ms / 1000));
  const h = Math.floor(seconds / 3600), m = Math.floor((seconds % 3600) / 60), s = seconds % 60;
  return h ? `${h} h ${m} min` : m ? `${m} min ${s.toString().padStart(2, '0')} s` : `${s} s`;
}

export function timeOfDay(ms: number | undefined): string {
  return ms === undefined ? '' : new Date(ms).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}
