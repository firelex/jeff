import { expect, test } from "vitest";
import type { Row } from "../src/binding.js";
import { Decision } from "../src/decision.js";
import { type Engine, serves } from "../src/engine.js";
import { EngineError, notInstalled } from "../src/errors.js";
import { FakeEngine, fakeTokens, goldenProblems } from "../src/testing.js";

// The expected values below were printed by the Python SDK (jeff.testing.fake_tokens, jeff.evaluate.golden_problems).
const row: Row = {
  state: new Map([["channel", "sms"], ["message", "Win  a prize\tnow\n café x"]]),
  question: { type: "noul", instructions: "Is this spam?", criteria: new Map([["false", "Not spam."], ["true", "Spam."]]) },
};

test("FakeEngine answers from the function, records its calls and refuses an adapter it does not serve", async () => {
  const engine = new FakeEngine(() => [0.25, 0.75], ["jeff.safety.spam"]);
  const result = await engine.predict("jeff.safety.spam", [row]);
  expect(result).toEqual({ probabilities: [[0.25, 0.75]], timing: { path: "fake", model_ms: 0, format: "bf16" }, inputTokens: [21] });
  expect(engine.calls).toEqual([["jeff.safety.spam", [row]]]);
  await expect(engine.predict("jeff.support.triage", [row])).rejects.toThrow(notInstalled(
    "jeff.support.triage", "FakeEngine has not loaded it"));
  expect(fakeTokens(row)).toBe(21);
});

test("FakeEngine refuses a malformed base format", () => {
  expect(() => new FakeEngine(() => [], [], { baseFormat: { format: "int4-g64" } })).toThrow(EngineError);
});

test("serves asks a cached list once more, with refresh", async () => {
  const asked: boolean[] = [];
  const engine: Engine = {
    baseFormat: { format: "bf16" },
    installed: async (refresh = false) => { asked.push(refresh); return new Set(refresh ? ["a"] : []); },
    predict: async () => { throw new Error("not called"); },
  };
  expect(await serves(engine, "a")).toBe(true);
  expect(asked).toEqual([false, true]);
});

test("goldenProblems words each difference as Python does", () => {
  const decision = new Decision({ value: true, confidence: 0.6, abstained: false, calibrated: true, interface: "i",
    function: "f", version: "1", probabilities: {}, score: null, route: [], decidedBy: "x", escalations: [], timing: {},
    evidenceRefs: [] });
  const call = { function: "f", input: "x" };
  expect(goldenProblems({ name: "n", call, expected: { value: 1, abstained: true, confidence: 0.9 }, tolerance: 0.05 },
    decision, 0.7)).toEqual(["value True, expected 1", "confidence 0.6, expected 0.9 +/- 0.05",
    "abstained False, expected True (the recorded confidence is more than 0.05 from the threshold 0.7)"]);
  expect(goldenProblems({ name: "n", call, expected: { value: true, abstained: true, confidence: 0.62 }, tolerance: 0.05 },
    decision, null)).toEqual(["abstained False, expected True (the recorded confidence is more than 0.05 from the threshold None)"]);
  expect(goldenProblems({ name: "n", call, expected: { value: true, abstained: true, confidence: 0.62 }, tolerance: 0.05 },
    decision, 0.65)).toEqual([]);
});
