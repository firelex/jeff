import { expect, test } from "vitest";
import type { DecisionCase } from "./decisions.js";
import { fixture } from "./fixtures.js";
import { replayDecisionCase } from "./run-decisions.js";

const groups = ["decide", "escalation", "dispatch"];
const cases = fixture<{ cases: DecisionCase[] }>("decisions.json").cases.filter((item) => groups.includes(item.group));

test.each(cases.map((item) => [`${item.group}: ${item.name}`, item] as const))("%s", async (_name, item) => {
  const { got, engine, questions, entryOrder } = await replayDecisionCase(item);
  expect(got).toEqual(item.expect);
  expect(engine.mismatches).toEqual([]);
  expect(engine.remaining()).toBe(0);
  expect(questions).toEqual(item.questions);
  expect(entryOrder).toEqual([]);
});
