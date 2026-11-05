export function percentile(sortedMs: readonly number[], q: number): number {
  if (sortedMs.length === 0) throw new Error("percentile of no measurements");
  return sortedMs[Math.min(sortedMs.length - 1, Math.max(0, Math.ceil(q * sortedMs.length) - 1))] as number;
}

export interface Summary { p50: number; p95: number; p99: number; mean: number }

function ordered(ms: readonly number[]): number[] {
  return [...ms].sort((a, b) => a - b);
}

export function summary(ms: readonly number[]): Summary {
  const values = ordered(ms);
  return { p50: percentile(values, 0.5), p95: percentile(values, 0.95), p99: percentile(values, 0.99),
    mean: values.reduce((total, value) => total + value, 0) / values.length };
}

/** What one call needs to return so its timing can be read, besides the round trip measured around it: the
 * Decision's `timing` (model_ms, queue_ms, batch_rows, where the engine reports them). */
export interface TimedCall { readonly timing: Readonly<Record<string, unknown>> }

export interface TimedSample {
  readonly roundTripMs: number;
  readonly modelMs: number | null;
  readonly queueMs: number | null;
  readonly batchRows: number | null;
}

function numberField(timing: Readonly<Record<string, unknown>>, key: string): number | null {
  const value = timing[key];
  return typeof value === "number" ? value : null;
}

/** Times one call (concurrency 1: nothing else is running) and reads the engine's own timing from its Decision. */
export async function sampleOf(call: () => Promise<TimedCall>): Promise<TimedSample> {
  const started = performance.now();
  const result = await call();
  return { roundTripMs: performance.now() - started, modelMs: numberField(result.timing, "model_ms"),
    queueMs: numberField(result.timing, "queue_ms"), batchRows: numberField(result.timing, "batch_rows") };
}

export interface TimingStats {
  /** One call after another, with nothing else running: the round trip as the caller sees it. */
  round_trip_ms: Summary;
  /** The engine's own `model_ms` from each sample's timing, when it reported one; null when none did. */
  model_ms: { p50: number; p95: number } | null;
  /** Round trip minus the engine's `model_ms`, sample by sample: everything that is not the model (binding, the
   * wire, the engine's queue, the server, if any). Null when no sample reported `model_ms`. */
  overhead_ms: Summary | null;
  /** The engine's own `queue_ms` (time a request waited before its micro-batch formed), when it reported one. */
  queue_ms: { p50: number } | null;
  /** The engine's own `batch_rows` (rows in the micro-batch a sample was part of), when it reported one. */
  batch_rows: { p50: number; max: number } | null;
}

/** `samples`, each measured one call at a time (concurrency 1), into the stats the benchmark reports. Exported on
 * its own (besides inside `measure`) so an interleaved run (latency.ts's `--rounds`) can collect each path's own
 * samples by alternating `sampleOf` calls across paths, then summarise each path's samples separately. */
export function summariseSamples(samples: readonly TimedSample[]): TimingStats {
  const roundTrips = samples.map((s) => s.roundTripMs);
  const modelMs = samples.map((s) => s.modelMs).filter((v): v is number => v !== null);
  const overheadMs = samples.filter((s) => s.modelMs !== null).map((s) => s.roundTripMs - (s.modelMs as number));
  const queueMs = samples.map((s) => s.queueMs).filter((v): v is number => v !== null);
  const batchRows = samples.map((s) => s.batchRows).filter((v): v is number => v !== null);
  return {
    round_trip_ms: summary(roundTrips),
    model_ms: modelMs.length > 0 ? { p50: percentile(ordered(modelMs), 0.5), p95: percentile(ordered(modelMs), 0.95) } : null,
    overhead_ms: overheadMs.length > 0 ? summary(overheadMs) : null,
    queue_ms: queueMs.length > 0 ? { p50: percentile(ordered(queueMs), 0.5) } : null,
    batch_rows: batchRows.length > 0 ? { p50: percentile(ordered(batchRows), 0.5), max: Math.max(...batchRows) } : null,
  };
}

/** `calls` calls spread over `concurrency` concurrent callers, divided by the wall time that took. */
export async function measureThroughput(call: () => Promise<unknown>, calls: number, concurrency: number): Promise<number> {
  let next = 0;
  const started = performance.now();
  await Promise.all(Array.from({ length: concurrency }, async () => {
    while (next < calls) {
      next += 1;
      await call();
    }
  }));
  return calls / ((performance.now() - started) / 1000);
}

export interface MeasureResult extends TimingStats {
  /** `calls` calls spread over `concurrency` concurrent callers, divided by the wall time that took. */
  throughput_per_s: number;
  /** round_trip_ms, model_ms, overhead_ms, queue_ms and batch_rows are always measured one call at a time. */
  round_trip_concurrency: 1;
  /** throughput_per_s is measured at this concurrency (the `concurrency` argument). */
  throughput_concurrency: number;
}

/** `warmup` calls, discarded, then `calls` calls one after another (round trip, model_ms, overhead_ms, queue_ms and
 * batch_rows, every one of them at concurrency 1), then `calls` more spread over `concurrency` concurrent callers
 * (throughput only). */
export async function measure(call: () => Promise<TimedCall>, calls: number, concurrency: number,
  warmup = 10): Promise<MeasureResult> {
  for (let index = 0; index < warmup; index += 1) await call();
  const samples: TimedSample[] = [];
  for (let index = 0; index < calls; index += 1) samples.push(await sampleOf(call));
  const throughputPerS = await measureThroughput(call, calls, concurrency);
  return { ...summariseSamples(samples), throughput_per_s: throughputPerS, round_trip_concurrency: 1,
    throughput_concurrency: concurrency };
}
