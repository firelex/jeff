import { expect, test } from "vitest";
import { bind, buildRow } from "../../src/binding.js";
import { makeDecision } from "../../src/decision.js";
import { formatIdOf } from "../../src/engine.js";
import { calibrationFor } from "../../src/manifest.js";
import { encode } from "../../src/msgpack.js";
import { type DecisionCase, engineProbabilities } from "./decisions.js";
import { catalogueFor, fixture, toBase64, wireOf } from "./fixtures.js";

/** The decide cases of one call and one predict, replayed without the client: bind, build the row, make the decision
 * from the probabilities the engine answered. Task 10 replays every case through the client. */
const cases = fixture<{ cases: DecisionCase[] }>("decisions.json").cases
  .filter((item) => item.op === "decide" && item.args.calls.length === 1 && item.predicts.length === 1);

test("there are direct cases", () => expect(cases.length).toBeGreaterThan(150));

test.each(cases.map((item) => [item.name, item] as const))("%s", (_name, item) => {
  const catalogue = catalogueFor(item.catalogue);
  const call = item.args.calls[0];
  const predict = item.predicts[0]!;
  let got: unknown;
  try {
    const fn = catalogue.function(call.function);
    const binding = bind(catalogue.manifestOf(fn), fn, call.context, call.labels, calibrationFor(fn, formatIdOf(item.base_format)));
    const row = buildRow(binding, call.input);
    expect(toBase64(encode([row]))).toBe(predict.rows_msgpack);
    got = { result: [makeDecision(binding, engineProbabilities(predict)[0]!, predict.timing).toJSON()] };
  } catch (error) {
    got = { error: wireOf(error) };
  }
  expect(got).toEqual(item.expect);
});
