import { expect, test } from "vitest";
import type { DecisionCase } from "./decisions.js";
import { fixture } from "./fixtures.js";

test("every group of decisions.json is replayed (decisions-client and decisions-graph)", () => {
  const groups = new Set(fixture<{ cases: DecisionCase[] }>("decisions.json").cases.map((item) => item.group));
  expect([...groups].sort()).toEqual(["decide", "dispatch", "escalation", "graph"]);
});
