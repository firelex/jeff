import { expect, test } from "vitest";
import { errorCatalogue } from "../src/contract.js";
import {
  Busy, ContractError, ERROR_CLASSES, EngineError, JeffError, MissingContext, NotInstalled, UsageLimitReached,
  copyError, errorFromWire, errorToWire, missingContext, notInstalled,
} from "../src/errors.js";

test("there is one error class per entry of contract/errors.yaml, each a JeffError", () => {
  expect([...ERROR_CLASSES.keys()].sort()).toEqual([...errorCatalogue().keys()].sort());
  for (const [name, ErrorClass] of ERROR_CLASSES) {
    const error = new ErrorClass("m");
    expect(error).toBeInstanceOf(JeffError);
    expect(error.type).toBe(name);
    expect(error.name).toBe(name);
  }
});

test("the typed constructors write the Python messages", () => {
  const error = notInstalled("jeff.support.triage.urgency", "it is an escalation step");
  expect(error.message).toBe(
    "jeff.support.triage.urgency is not installed: it is an escalation step. Install it with: jeff pull jeff.support.triage");
  expect(error.details).toEqual({ function_id: "jeff.support.triage.urgency" });
  expect(notInstalled("jeff.core.router").message).toBe("jeff.core.router is not installed. Install it with: jeff pull jeff.core.router");
  const missing = missingContext("spam.x", ["channel", "region"]);
  expect(missing).toBeInstanceOf(MissingContext);
  expect(missing.message).toBe("spam.x needs context field(s) channel, region; pass them in context=");
  expect(missing.fields).toEqual(["channel", "region"]);
});

test("an error on the wire becomes the same class with the same message and details", () => {
  const error = errorFromWire({ type: "Busy", message: "full", details: { retry_after_s: 2.5 } });
  expect(error).toBeInstanceOf(Busy);
  expect((error as Busy).retryAfterS).toBe(2.5);
  expect(errorToWire(error)).toEqual({ type: "Busy", message: "full", details: { retry_after_s: 2.5 } });
  const copied = errorFromWire(errorToWire(new NotInstalled("x", { function_id: "x" })));
  expect(copied).toBeInstanceOf(NotInstalled);
});

test("an unknown type on the wire is an EngineError naming it", () => {
  const error = errorFromWire({ type: "Weird", message: "Something odd" });
  expect(error).toBeInstanceOf(EngineError);
  expect(error.message).toBe("unknown error type 'Weird' from the engine: Something odd");
  expect(error.details).toEqual({});
});

test("a typed getter throws naming the missing field instead of coercing it to the text 'undefined'", () => {
  const broken = new NotInstalled("boom", {});
  expect(() => broken.functionId).toThrow(ContractError);
  expect(() => broken.functionId).toThrow(/NotInstalled\.details is missing its 'function_id' string/);
});

test("copyError gives each waiting caller its own instance, carrying the same notes and requestId", () => {
  const original = notInstalled("jeff.core.router");
  original.requestId = "req-1";
  original.notes.push("also tried the fallback router");
  const copy = copyError(original);
  expect(copy).not.toBe(original);
  expect(copy).toBeInstanceOf(NotInstalled);
  expect(copy.message).toBe(original.message);
  expect(copy.details).toEqual(original.details);
  expect(copy.requestId).toBe("req-1");
  expect(copy.notes).toEqual(["also tried the fallback router"]);
  copy.notes.push("only on the copy");
  expect(original.notes).toEqual(["also tried the fallback router"]);
});

test("UsageLimitReached reads its four details", () => {
  const error = errorFromWire({
    type: "UsageLimitReached",
    message: "This account has used its 50,000,000 input tokens for the period ending 2026-12-03 10:00 UTC; calls resume then",
    details: { account: "u_1", used: 50_000_123, allowance: 50_000_000, period_end: "2026-12-03T10:00:00Z" },
  });
  expect(error).toBeInstanceOf(UsageLimitReached);
  const limit = error as UsageLimitReached;
  expect([limit.account, limit.used, limit.allowance, limit.periodEnd])
    .toEqual(["u_1", 50_000_123, 50_000_000, "2026-12-03T10:00:00Z"]);
  expect(() => new UsageLimitReached("m", {}).periodEnd).toThrow(/UsageLimitReached\.details is missing its 'period_end' string/);
});
