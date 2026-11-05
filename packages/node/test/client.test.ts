import { expect, test } from "vitest";
import { Catalogue } from "../src/catalogue.js";
import { Jeff } from "../src/client.js";
import type { Engine } from "../src/engine.js";
import { EngineError, EscalationBudgetExceeded, InvalidPolicy, InvalidRequest, UnknownContext } from "../src/errors.js";
import { type Policy, type Question, escalate, human, llm, remote } from "../src/escalation.js";
import { manifestFromDict } from "../src/manifest.js";
import { exactKeysOf, refuseExactKeys } from "../src/routing.js";
import { FakeEngine } from "../src/testing.js";
import { catalogueFor, fixture } from "./conformance/fixtures.js";

const CTX = { company: "A shop.", channel: "email" };
const unsure = () => [0.3, 0.2, 0.2, 0.2, 0.1];

test("a remote step asks another client with the same call", async () => {
  const catalogue = Catalogue.shipped();
  const jeff = await Jeff.create(new FakeEngine(unsure, ["jeff.support.triage"]), catalogue);
  const other = await Jeff.create(new FakeEngine(() => [0, 0, 0, 0.95, 0.05], ["jeff.support.triage"]), catalogue);
  const bound = await jeff.function("triage.urgency", { context: CTX, onLowConfidence: escalate([remote(other)]) });
  const decision = await bound.call("Hm");
  expect(decision.value).toBe(3);
  expect(decision.escalations.map((item) => item.decidedBy)).toEqual(["jeff.support.triage.urgency@1.4.0"]);
});

test("an escalation past its budget is refused", async () => {
  const jeff = await Jeff.create(new FakeEngine(unsure, ["jeff.support.triage"]), Catalogue.shipped());
  const slow = llm(() => new Promise((resolve) => setTimeout(() => resolve("2"), 30)));
  const bound = await jeff.function("triage.urgency", { context: CTX, onLowConfidence: escalate([slow], { budgetMs: 5 }) });
  await expect(bound.call("Hm")).rejects.toThrow(EscalationBudgetExceeded);
});

test("a context field given by its exact key is refused before routing", () => {
  const exact = new Map([["counterparty name", "counterparty_name"]]);
  expect(() => refuseExactKeys(exact, "jeff.core.router", { "counterparty name": "Acme" })).toThrow(
    new UnknownContext("context is given by code name; pass 'counterparty name' as counterparty_name (fields of adapters "
      + "reached from router jeff.core.router)"));
  expect(exactKeysOf(Catalogue.shipped(), "jeff.safety.spam").size).toBe(0);
});

function asking(answer: string, asked: Question[]) {
  return human((question) => {
    asked.push(question);
    return answer;
  });
}

const SPACED_CONTEXT = { ten: "x", deal_id: "D-7", counterparty_name: "Acme Ltd" };

test("a question's state entries keep the order the model reads, number-like keys included", async () => {
  const jeff = await Jeff.create(new FakeEngine(() => [0.5, 0.5], ["jeff.safety.spam"]), catalogueFor("spaced"));
  const asked: Question[] = [];
  const bound = await jeff.function("spam.is_spam", { context: SPACED_CONTEXT, onLowConfidence: escalate([asking("false", asked)]) });
  expect((await bound.call("Please sign")).decidedBy).toBe("human");
  expect(asked[0]?.stateEntries?.map(([key]) => key)).toEqual(["counterparty name", "Deal ID", "10", "message"]);
  expect(Object.keys(asked[0]?.state as object)).toEqual(["10", "counterparty name", "Deal ID", "message"]);
});

test("a question's option entries keep option order, number-like keys included", async () => {
  // spaced's spam adapter with message_type's labels replaced by "other" and "10", "other" an abstain label
  const raw = structuredClone(fixture<{ variants: Record<string, any[]> }>("catalogues.json").variants.spaced?.[0]);
  // a Map, as the YAML reader gives: an object would list "10" first
  raw.functions.message_type.labels.fixed = new Map([["other", "None of these"], ["10", "Ten"]]);
  raw.functions.message_type.abstain_labels = ["other"];
  const catalogue = Catalogue.shipped().replacing([manifestFromDict(raw, "test/number-like-labels/jeff.safety.spam")]);
  const fn = catalogue.function("spam.message_type");
  expect([...fn.labels.fixed.keys()]).toEqual(["other", "10"]);
  const jeff = await Jeff.create(new FakeEngine(() => [0.9, 0.1], ["jeff.safety.spam"]), catalogue);
  const asked: Question[] = [];
  const bound = await jeff.function("spam.message_type", { context: SPACED_CONTEXT, onLowConfidence: escalate([asking("10", asked)]) });
  expect((await bound.call("Please sign")).value).toBe("10");
  expect(asked[0]?.optionEntries).toEqual([["other", "None of these"], ["10", "Ten"]]);
  expect(Object.keys(asked[0]?.options as object)).toEqual(["10", "other"]);
});

test("an explicit null policy is refused, and an explicit null router is not replaced by the default", async () => {
  const jeff = await Jeff.create(new FakeEngine(unsure, ["jeff.support.triage"]), Catalogue.shipped());
  const nullPolicy = null as unknown as Policy;
  await expect(jeff.function("triage.urgency", { context: CTX, onLowConfidence: nullPolicy })).rejects.toThrow(
    new InvalidPolicy("Unknown on_low_confidence policy None; use 'return', 'error' or escalate(...)"));
  await expect(jeff.rate("Hm", { context: CTX, onLowConfidence: nullPolicy })).rejects.toThrow(InvalidPolicy);
  // a null router replaced by the default would raise a JeffError about jeff.core.router, not a TypeError
  await expect(jeff.dispatch("Hm", { interface: "rate", router: null as unknown as string })).rejects.toThrow(TypeError);
});

test("a predict that fails is reported before a later adapter's rows fail to build, as Python meets them", async () => {
  // the "text" catalogue's spam functions take plain text, so a number as input fails when its row is built
  let down = true;
  const engine: Engine = {
    baseFormat: { format: "bf16" },
    installed: async () => new Set(["jeff.support.triage", "jeff.safety.spam"]),
    predict: async (adapterId, rows) => {
      if (down) throw new EngineError(`${adapterId} is down`);
      return { probabilities: rows.map(() => [0.5, 0.5, 0, 0, 0]), timing: {}, inputTokens: null };
    },
  };
  const jeff = await Jeff.create(engine, catalogueFor("text"));
  const urgency = { function: "triage.urgency", input: "Hm", context: CTX };
  const spam = { function: "spam.is_spam", input: 5 };
  const bound = [[urgency, await jeff.bindCall(urgency)], [spam, await jeff.bindCall({ ...spam, input: "x" })]] as const;
  await expect(jeff.decideBound(bound)).rejects.toThrow(new EngineError("jeff.support.triage is down"));
  down = false;
  await expect(jeff.decideBound(bound)).rejects.toThrow(InvalidRequest);
});
