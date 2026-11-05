import type { FixtureError } from "./fixtures.js";

/** A recorded probability: a number, or for one that is not finite (which JSON cannot write) "NaN", "Infinity" or
 * "-Infinity" (contract/socket.md, "The probabilities of a predict"). */
export type RecordedProbability = number | "NaN" | "Infinity" | "-Infinity";

export interface Predict { adapter: string; rows: unknown; rows_msgpack: string; probabilities: RecordedProbability[][]; timing: Record<string, unknown> }

const NOT_FINITE: Record<string, number> = { NaN: NaN, Infinity: Infinity, "-Infinity": -Infinity };

/** A predict's probabilities as the engine sent them: the strings for values that are not finite read back as
 * numbers; any other string is an error in the fixture. */
export function engineProbabilities(predict: Predict): number[][] {
  return predict.probabilities.map((row) => row.map((value) => {
    if (typeof value === "number") return value;
    const number = NOT_FINITE[value];
    if (number === undefined) throw new Error(`decisions.json: a probability ${JSON.stringify(value)} is not a number`);
    return number;
  }));
}

export interface DecisionCase {
  name: string;
  group: "decide" | "escalation" | "dispatch" | "graph";
  catalogue: string;
  base_format: { format: string; format_id?: string };
  installed: string[];
  op: "decide" | "function" | "shorthand" | "dispatch" | "graph";
  args: any;
  predicts: Predict[];
  questions: Array<{ step: string; question: unknown }>;
  expect: { result: unknown } | { error: FixtureError };
}
