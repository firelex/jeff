import { expect, test } from "vitest";
import { measure, measureThroughput, percentile, sampleOf, summariseSamples, summary } from "../bench/stats.js";

test("percentiles are nearest-rank", () => {
  const sorted = Array.from({ length: 100 }, (_, index) => index + 1);
  expect(percentile(sorted, 0.5)).toBe(50);
  expect(percentile(sorted, 0.99)).toBe(99);
  expect(percentile([7], 0.9)).toBe(7);
  expect(summary([3, 1, 2])).toEqual({ p50: 2, p95: 3, p99: 3, mean: 2 });
  expect(() => percentile([], 0.5)).toThrow("no measurements");
});

test("measure discards warmup calls, then runs the call sequentially and then concurrently", async () => {
  let count = 0;
  const result = await measure(async () => {
    count += 1;
    return { timing: {} };
  }, 20, 4, 5);
  expect(count).toBe(5 + 20 + 20); // warmup + sequential round trip + concurrent throughput
  expect(result.throughput_per_s).toBeGreaterThan(0);
  expect(result.round_trip_concurrency).toBe(1);
  expect(result.throughput_concurrency).toBe(4);
});

test("measure defaults warmup to 10 discarded calls", async () => {
  let count = 0;
  await measure(async () => {
    count += 1;
    return { timing: {} };
  }, 3, 1);
  expect(count).toBe(10 + 3 + 3);
});

test("measure reads model_ms, queue_ms and batch_rows from each call's timing", async () => {
  const result = await measure(async () => ({ timing: { model_ms: 10, queue_ms: 1, batch_rows: 4 } }), 20, 1, 0);
  expect(result.model_ms).toEqual({ p50: 10, p95: 10 });
  expect(result.queue_ms).toEqual({ p50: 1 });
  expect(result.batch_rows).toEqual({ p50: 4, max: 4 });
  // overhead_ms is round trip minus model_ms, so it is present whenever model_ms was reported.
  expect(result.overhead_ms).not.toBeNull();
});

test("measure reports null stats for timing fields the engine never sent", async () => {
  const result = await measure(async () => ({ timing: {} }), 5, 1, 0);
  expect(result.model_ms).toBeNull();
  expect(result.overhead_ms).toBeNull();
  expect(result.queue_ms).toBeNull();
  expect(result.batch_rows).toBeNull();
});

test("sampleOf times one call and reads its timing", async () => {
  const sample = await sampleOf(async () => ({ timing: { model_ms: 5 } }));
  expect(sample.modelMs).toBe(5);
  expect(sample.queueMs).toBeNull();
  expect(sample.batchRows).toBeNull();
  expect(sample.roundTripMs).toBeGreaterThanOrEqual(0);
});

test("summariseSamples computes stats directly from samples (what an interleaved run uses per path)", () => {
  const stats = summariseSamples([
    { roundTripMs: 12, modelMs: 10, queueMs: 1, batchRows: 2 },
    { roundTripMs: 14, modelMs: 11, queueMs: 2, batchRows: 3 },
  ]);
  expect(stats.model_ms).toEqual({ p50: 10, p95: 11 });
  expect(stats.queue_ms).toEqual({ p50: 1 });
  expect(stats.batch_rows).toEqual({ p50: 2, max: 3 });
  expect(stats.overhead_ms).not.toBeNull();
});

test("measureThroughput divides calls by wall time at the given concurrency", async () => {
  let count = 0;
  const throughput = await measureThroughput(async () => { count += 1; }, 10, 2);
  expect(count).toBe(10);
  expect(throughput).toBeGreaterThan(0);
});
