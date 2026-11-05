import { expect, test } from "vitest";
import { bind } from "../src/binding.js";
import { Catalogue } from "../src/catalogue.js";
import { Decision, makeDecision } from "../src/decision.js";
import { EngineError, LowConfidence, lowConfidence } from "../src/errors.js";
import { manifestFromDict } from "../src/manifest.js";
import { pySum } from "../src/pyformat.js";

const catalogue = Catalogue.shipped();

test("a tie goes to the first option in option order, also for keys that look like numbers", () => {
  const raw = structuredClone(catalogue.adapter("jeff.safety.spam").raw) as any;
  raw.functions.message_type.labels.fixed = new Map([["2", "Two"], ["1", "One"], ["legit", "Fine"]]);
  const manifest = manifestFromDict(raw, "test");
  const fn = manifest.functions.get("message_type")!;
  const decision = makeDecision(bind(manifest, fn, { channel: "sms" }, null, null), [0.4, 0.4, 0.2], {});
  expect(decision.value).toBe("2");
});

test("probabilities are normalised with Python's sum", () => {
  const fn = catalogue.function("triage.urgency");
  const binding = bind(catalogue.manifestOf(fn), fn, { company: "c", channel: "e" }, null, fn.calibration);
  const decision = makeDecision(binding, Array(5).fill(0.2), {});
  expect(decision.value).toBe(0);
  expect(decision.confidence).toBe(0.2);
  expect(decision.score).toBeCloseTo(2, 12);
});

test("an engine answer of the wrong length is refused", () => {
  const fn = catalogue.function("spam.is_spam");
  const binding = bind(catalogue.manifestOf(fn), fn, { channel: "sms" }, null, fn.calibration);
  expect(() => makeDecision(binding, [0.5], {})).toThrow(
    new EngineError("jeff.safety.spam.is_spam: the engine returned 1 probabilities for 2 options"));
  expect(() => makeDecision(binding, [0, 0], {})).toThrow(
    "jeff.safety.spam.is_spam: the engine returned invalid probabilities [0.0, 0.0]");
});

test("a decision survives its JSON, and LowConfidence carries it", () => {
  const fn = catalogue.function("spam.is_spam");
  const binding = bind(catalogue.manifestOf(fn), fn, { channel: "sms" }, null, fn.calibration);
  const decision = makeDecision(binding, [0.47, 0.53], { path: "fake" });
  expect(Decision.fromJSON(decision.toJSON()).toJSON()).toEqual(decision.toJSON());
  const error = lowConfidence(decision);
  expect(error).toBeInstanceOf(LowConfidence);
  expect(error.message).toBe("jeff.safety.spam.is_spam abstained (confidence 0.53); on_low_confidence='error'");
  expect(error.decision.toJSON()).toEqual(decision.toJSON());
});

function isSpam() {
  const fn = catalogue.function("spam.is_spam");
  return bind(catalogue.manifestOf(fn), fn, { channel: "sms" }, null, fn.calibration);
}

test.each([
  [[-0.1, 1.1], "[-0.1, 1.1]", "[-0.1, 1.1]"],
  [[NaN, 0.5], "[nan, 0.5]", "[nan]"],
  [[Infinity, 0], "[inf, 0.0]", "[inf]"],
  [[0.5, -Infinity], "[0.5, -inf]", "[-inf]"],
  [[1.00005, 0], "[1.00005, 0.0]", "[1.00005]"],
  [[1e308, 1e308], "[1e+308, 1e+308]", "[1e+308, 1e+308]"], // a sum that overflows
])("a probability outside 0 to 1 is refused, never repaired: %j", (probabilities, shown, offending) => {
  expect(() => makeDecision(isSpam(), probabilities, {})).toThrow(new EngineError(
    `jeff.safety.spam.is_spam: the engine returned invalid probabilities ${shown}; expected each to be a finite `
    + `number from 0 to 1, which ${offending} are not`));
});

test.each([
  [[0, 0], "[0.0, 0.0]", "0.0"],
  [[0.5, 0.4], "[0.5, 0.4]", "0.9"],
  [[0.6, 0.4002], "[0.6, 0.4002]", "1.0002"],
])("a sum outside the tolerance of 1 is refused: %j", (probabilities, shown, total) => {
  expect(() => makeDecision(isSpam(), probabilities, {})).toThrow(new EngineError(
    `jeff.safety.spam.is_spam: the engine returned invalid probabilities ${shown}; expected their sum to be within `
    + `0.0001 of 1, got ${total}`));
});

test("a sum within the tolerance is divided by its sum, exactly as Python does", () => {
  const probabilities = [0.10000000149011612, 0.20000000298023224, 0.699999988079071, 0, 0];
  const fn = catalogue.function("triage.urgency");
  const binding = bind(catalogue.manifestOf(fn), fn, { company: "c", channel: "e" }, null, fn.calibration);
  const decision = makeDecision(binding, probabilities, {});
  const total = pySum(probabilities);
  expect(Object.values(decision.probabilities)).toEqual(probabilities.map((p) => p / total));
});
