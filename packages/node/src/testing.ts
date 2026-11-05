/** Test helpers for code that uses the SDK: an engine whose answers come from a function you supply, and the golden
 * comparison (port of jeff.testing.FakeEngine and jeff.evaluate.same and golden_problems). */

import { type Row, rowToJSON } from "./binding.js";
import type { Decision, Value } from "./decision.js";
import { type BaseFormat, BF16, type Engine, type EngineResult, formatIdOf } from "./engine.js";
import { notInstalled } from "./errors.js";
import { pyFloat, pyRepr } from "./pyformat.js";

export type Answers = (adapterId: string, row: Row) => number[];

/** FakeEngine's stand-in for a row's prompt length, as Python's fake_tokens: one token per whitespace-separated word of
 * the row's JSON as Python's json.dumps writes it (", " and ": " separators; every other whitespace character inside a
 * string is escaped there, so only spaces separate words). */
export function fakeTokens(row: Row): number {
  const dumped = pyJsonDumps(rowToJSON(row));
  return dumped.split(" ").filter((word) => word.length > 0).length;
}

function pyJsonDumps(value: unknown): string {
  if (Array.isArray(value)) return `[${value.map(pyJsonDumps).join(", ")}]`;
  if (value !== null && typeof value === "object") {
    return `{${Object.entries(value).map(([key, item]) => `${JSON.stringify(key)}: ${pyJsonDumps(item)}`).join(", ")}}`;
  }
  return JSON.stringify(value);
}

/** answers: the probabilities for each row. installed: the adapter IDs it serves. baseFormat: the base's weight format
 * it reports; {format: "bf16"} unless given (pass {format: "int4-g64", format_id: "..."} to act as an INT4 base). */
export class FakeEngine implements Engine {
  readonly baseFormat: BaseFormat;
  readonly calls: Array<[string, Row[]]> = [];
  private readonly served: Set<string>;

  constructor(private readonly answers: Answers, installed: Iterable<string>, options: { baseFormat?: BaseFormat } = {}) {
    this.served = new Set(installed);
    this.baseFormat = options.baseFormat === undefined ? { format: BF16 } : { ...options.baseFormat };
    formatIdOf(this.baseFormat); // refuses a malformed format here, as a real engine's base folder would be
  }

  async installed(_refresh = false): Promise<Set<string>> {
    return new Set(this.served);
  }

  async predict(adapterId: string, rows: Row[]): Promise<EngineResult> {
    if (!this.served.has(adapterId)) throw notInstalled(adapterId, "FakeEngine has not loaded it");
    this.calls.push([adapterId, rows]);
    return {
      probabilities: rows.map((row) => this.answers(adapterId, row)),
      timing: { path: "fake", model_ms: 0, ...this.baseFormat },
      inputTokens: rows.map(fakeTokens),
    };
  }
}

/** A golden case (contract/golden/*.json). */
export interface GoldenCase {
  name: string;
  source?: string;
  call: { function: string; input: unknown; context?: unknown; labels?: unknown };
  expected: { value: Value; abstained: boolean; confidence: number };
  tolerance: number;
}

/** Equal and of the same JSON type, so that true does not match 1. */
export function same(value: unknown, expected: unknown): boolean {
  return typeof value === typeof expected && value === expected;
}

/** How a decision differs from a golden case; empty when it reproduces it. value must be the same, confidence within
 * the case's tolerance, and abstained must match except when the recorded confidence is within the tolerance of the
 * threshold (a confidence that may move by the tolerance can then land on either side of it). */
export function goldenProblems(item: GoldenCase, decision: Decision, threshold: number | null): string[] {
  const { expected, tolerance } = item;
  const problems: string[] = [];
  if (!same(decision.value, expected.value)) problems.push(`value ${pyRepr(decision.value)}, expected ${pyRepr(expected.value)}`);
  if (decision.confidence === null) problems.push("no confidence");
  else if (Math.abs(decision.confidence - expected.confidence) > tolerance) {
    problems.push(`confidence ${pyFloat(decision.confidence)}, expected ${pyFloat(expected.confidence)} +/- ${pyFloat(tolerance)}`);
  }
  const nearThreshold = threshold !== null && Math.abs(expected.confidence - threshold) <= tolerance;
  if (decision.abstained !== expected.abstained && !nearThreshold) {
    problems.push(`abstained ${pyRepr(decision.abstained)}, expected ${pyRepr(expected.abstained)} (the recorded confidence `
      + `is more than ${pyFloat(tolerance)} from the threshold ${threshold === null ? "None" : pyFloat(threshold)})`);
  }
  return problems;
}
