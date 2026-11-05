/** Jeff.remote(..., {channel: true}): bound functions are handles on the server's /v1/channel (port of
 * packages/python/tests/test_channel_client.py, against a fake channel server: there are no recorded channel
 * exchanges). A bound function sends one bind and then only call frames; close or a queued release frees its handle;
 * a function token that is a JWT is refreshed before its exp; a request with no answer is DeadlineExceeded, promptly;
 * the server going away fails every request with BackendUnavailable. */

import { createServer as createTcpServer } from "node:net";
import type { AddressInfo } from "node:net";
import { expect, test } from "vitest";
import { Jeff } from "../src/client.js";
import {
  BackendUnavailable, ContractError, DeadlineExceeded, EngineError, InvalidRequest, LowConfidence, RateLimited,
  Unauthorised,
} from "../src/errors.js";
import { escalate, functionStep, llm } from "../src/escalation.js";
import { ChannelTransport, channelUrl, tokenExpiry } from "../src/channel.js";
import type { ChannelJeff } from "../src/channel.js";
import { Catalogue } from "../src/catalogue.js";
import type { Decision } from "../src/decision.js";
import { FakeEngine } from "../src/testing.js";
import { INSTALLED, answers, startChannelServer } from "./helpers/channel-server.js";
import { startRelay } from "./helpers/proxies.js";
import { withoutProxies } from "./helpers/no-proxies.js";

withoutProxies();

const CTX = { company: "A shop.", channel: "email" };
const ops = (received: any[]) => received.map((message) => message.op);

function jwt(exp: number): string {
  const part = (value: unknown) => Buffer.from(JSON.stringify(value)).toString("base64url");
  return `${part({ alg: "EdDSA" })}.${part({ sub: "alice", exp })}.c2ln`;
}

const open = (url: string, token: any = "secret-token", timeoutMs = 2000) =>
  Jeff.remote(url, token, { timeoutMs, channel: true }) as Promise<ChannelJeff>;

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

/** How long `promise` takes to reject, and what with. */
async function rejection(promise: Promise<unknown>): Promise<[number, unknown]> {
  const started = performance.now();
  const error = await promise.then(() => null, (caught: unknown) => caught);
  return [performance.now() - started, error];
}

test("the channel URL follows the server URL, and a JWT's exp is read without verifying it", () => {
  expect(channelUrl("http://h:1/jeff/")).toBe("ws://h:1/jeff/v1/channel");
  expect(channelUrl("https://h")).toBe("wss://h/v1/channel");
  expect(() => channelUrl("ftp://h")).toThrow(new InvalidRequest("The server URL 'ftp://h' must start with http:// or https://"));
  expect(tokenExpiry("an-api-key")).toBeNull();
  expect(tokenExpiry(jwt(1234))).toBe(1234);
  // The thrown message names only that the payload is not JSON; it never embeds JSON.parse's own message, which would
  // quote back part of the (possibly secret-bearing) token payload.
  expect(() => tokenExpiry("a.!!!.c")).toThrow(
    new Unauthorised("The token is shaped like a JWT, but its payload is not base64-encoded JSON"));
  const noExp = `a.${Buffer.from(JSON.stringify({ sub: "alice" })).toString("base64url")}.c`;
  expect(() => tokenExpiry(noExp)).toThrow(new Unauthorised("The token is a JWT without a numeric exp (got None); the server needs one"));
});

test("a bound function sends one bind, then only call frames; close releases its handle", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url);
  const bound = await jeff.function("triage.urgency", { context: CTX });
  expect((await bound.call("Fire")).value).toBe(2);
  expect((await bound.many(["a", "b"])).map((decision) => decision.value)).toEqual([2, 2]);
  await bound.close();
  await expect(bound.call("again")).rejects.toThrow(InvalidRequest);
  expect(ops(server.received)).toEqual(["hello", "auth", "bind", "call", "call_many", "release"]);
  expect(server.received[2]).toEqual({ op: "bind", function: "jeff.support.triage.urgency", on_low_confidence: "return",
    context: CTX, id: 2 });
  expect(server.headers).toEqual(["Bearer secret-token"]);
  await jeff.close();
  await server.close();
});

test("without a token there is no auth and no Authorization header", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url, null);
  await jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }]);
  expect(ops(server.received)).toEqual(["hello", "decide"]);
  expect(server.headers).toEqual([""]);
  await jeff.close();
  await server.close();
});

test("a token the server refuses is Unauthorised at connect, and the message never shows a token", async () => {
  const server = await startChannelServer({ token: "the-key" });
  const error = await open(server.url, "the-kez").then(() => null, (caught: unknown) => caught);
  expect(error).toBeInstanceOf(Unauthorised);
  expect(String((error as Error).message)).not.toContain("the-kez");
  const jeff = await open(server.url, "the-key");
  expect((await jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }]))[0]?.value).toBe(2);
  await jeff.close();
  await server.close();
});

test("the bind carries the policy the server runs; one with an llm step is bound as return and run here", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url);
  await jeff.function("triage.urgency", { context: CTX, onLowConfidence: escalate([functionStep("triage.urgency")]) });
  await jeff.function("triage.urgency", { context: CTX, onLowConfidence: escalate([llm(() => "3")]) });
  const binds = server.received.filter((message) => message.op === "bind");
  expect(binds.map((message) => message.on_low_confidence)).toEqual([{ escalate: ["jeff.support.triage.urgency"] }, "return"]);
  await jeff.close();
  await server.close();
});

test("a bind answered with another adapter version is refused, and its handle released before the next request", async () => {
  const server = await startChannelServer({ version: "9.9.9" });
  const jeff = await open(server.url);
  const local = jeff.catalogue.adapter("jeff.support.triage").version;
  await expect(jeff.function("triage.urgency", { context: CTX })).rejects.toThrow(new ContractError(
    `jeff.support.triage: the server serves version 9.9.9, this client's catalogue has ${local}; pass catalogue= with the `
    + "server's manifests"));
  await jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }]);
  expect(ops(server.received)).toEqual(["hello", "auth", "bind", "release", "decide"]);
  await jeff.close();
  await server.close();
});

test("a queued release is sent before the next request (as for a bound function the garbage collector took)", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url);
  const bound = await jeff.function("triage.urgency", { context: CTX });
  (jeff.transport as ChannelTransport).releaseLater(1);
  await jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }]);
  expect(ops(server.received).slice(-2)).toEqual(["release", "decide"]);
  void bound;
  await jeff.close();
  await server.close();
});

test("a failure the server sends carries the connection's id as channel_id", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url);
  const bound = await jeff.function("triage.urgency", { context: CTX });
  await (jeff.transport as ChannelTransport).release(1);
  const error = await bound.call("x").then(() => null, (caught: unknown) => caught);
  expect(error).toEqual(new InvalidRequest("handle 1 is not bound", { channel_id: "0badc0de" }));
  expect((error as InvalidRequest).requestId).toBeNull();
  await jeff.close();
  await server.close();
});

test("a LowConfidence arriving over the channel whose decision does not match the decision schema is a ContractError, "
  + "not built as a LowConfidence", async () => {
  const server = await startChannelServer({ badLowConfidenceDecision: true });
  const jeff = await open(server.url);
  const bound = await jeff.function("triage.urgency", { context: CTX });
  const error = await bound.call("x").then(() => null, (caught: unknown) => caught);
  expect(error).toBeInstanceOf(ContractError);
  expect(error).not.toBeInstanceOf(LowConfidence);
  await jeff.close();
  await server.close();
});

test("a function token that is a JWT is refreshed before its exp", async () => {
  const server = await startChannelServer();
  const now = Math.floor(Date.now() / 1000);
  const tokens = [jwt(now + 61), jwt(now + 3600)];
  let calls = 0;
  // the handshake header and the auth read the first token; GET /v1/functions and the refresh read the second
  const jeff = await open(server.url, () => (calls++ < 2 ? tokens[0] : tokens[1]) as string);
  await pause(1600);
  expect(ops(server.received)).toContain("refresh");
  expect(server.received.find((message) => message.op === "refresh").token).toBe(tokens[1]);
  await jeff.close();
  await server.close();
});

test("a string token is never refreshed, even a JWT inside the refresh window", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url, jwt(Math.floor(Date.now() / 1000) + 30));
  await pause(300);
  expect(ops(server.received)).toEqual(["hello", "auth"]);
  await jeff.close();
  await server.close();
});

test("a token function that never renews closes the channel at exp with Unauthorised carrying every failure", async () => {
  const server = await startChannelServer();
  const fixed = jwt(Math.floor(Date.now() / 1000) + 2);
  const warned: string[] = [];
  const warn = console.warn;
  console.warn = (...args: unknown[]) => { warned.push(args.join(" ")); };
  try {
    const jeff = await open(server.url, () => fixed);
    await pause(3200);
    const error = await jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }]).then(() => null,
      (caught: unknown) => caught);
    expect(error).toBeInstanceOf(Unauthorised);
    expect((error as Unauthorised).message).toMatch(/^The channel's token expired at \d+ before the token function returned one/);
    const notes = (error as Unauthorised).notes;
    expect(notes.length).toBeGreaterThanOrEqual(2);
    expect(notes.every((note) => note.includes("not after the channel's current token"))).toBe(true);
    expect(warned.length).toBe(notes.length);
    expect(ops(server.received)).not.toContain("refresh");
    await jeff.close();
  } finally {
    console.warn = warn;
  }
  await server.close();
});

test("an unanswered request is DeadlineExceeded promptly; its late reply is dropped and the channel stays open", async () => {
  const server = await startChannelServer({ silent: ["decide"] });
  const jeff = await open(server.url, "secret-token", 300);
  const [elapsed, error] = await rejection(jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }]));
  expect(error).toEqual(new DeadlineExceeded(`The channel ${channelUrl(server.url)} did not answer 'decide' within 300 ms; `
    + "the server may still have run the request"));
  expect(elapsed).toBeGreaterThanOrEqual(290);
  expect(elapsed).toBeLessThan(500);
  const late = server.received.find((message) => message.op === "decide");
  server.send({ id: late.id, ok: true, result: { decisions: [] } });
  const bound = await jeff.function("triage.urgency", { context: CTX });
  expect((await bound.call("Fire")).value).toBe(2);
  await jeff.close();
  await server.close();
});

test("a large message on a slow but steady link takes as long as it needs: the timeout starts once it is sent", async () => {
  const server = await startChannelServer();
  const relay = await startRelay(Number(new URL(server.url).port));
  relay.rate = 2 * 1024 * 1024; // 2 MiB/s

  // How long the stall window and the message size need to be is derived from what this machine actually achieves
  // right now, not from an assumed-idle machine: under CPU load, both the relay's pacing timers and the channel's
  // own timers fire late, so a fixed small stall window (sized for an idle machine) can trip on a slow but genuinely
  // steady link. A short calibration transfer over the same throttled relay measures today's real, loaded
  // throughput; a generous timeout well above a single calibration run, and a message several multiples of that
  // timeout's worth of bytes, keep the two phenomena this test tells apart (a reply timer that wrongly starts
  // counting the upload vs. one that starts once the send is done) comfortably apart however busy the machine is.
  const CALIBRATION_BYTES = 1024 * 1024; // 1 MiB: several dozen of the relay's pacing steps, so a single step's
  // jitter does not dominate the measurement
  const calibration = await open(relay.url, "secret-token", 15000); // generous: calibration must not itself time out
  const calibrationBound = await calibration.function("spam.message_type", { context: { channel: "email" } });
  const calibrationStarted = performance.now();
  await calibrationBound.call("x".repeat(CALIBRATION_BYTES));
  const calibrationMs = performance.now() - calibrationStarted;
  await calibration.close();

  // Both bounds below scale off the one calibration measurement, so their ratio (and so what this test proves)
  // holds however fast or slow, idle or loaded, this machine is right now: a bigger timeout and a message several
  // times bigger than a fixed 6 MiB would have been on an idle machine, if this machine is currently slower.
  const MESSAGE_BYTES = 16 * CALIBRATION_BYTES;
  const timeoutMs = Math.round(calibrationMs * 5); // well above any single pacing step actually observed just now
  const expectedUploadMs = (MESSAGE_BYTES / CALIBRATION_BYTES) * calibrationMs; // 16x calibrationMs: well above timeoutMs
  expect(timeoutMs).toBeLessThan(expectedUploadMs); // sanity on the derivation: the reply timeout must stay well below it

  const jeff = await open(relay.url, "secret-token", timeoutMs);
  const bound = await jeff.function("spam.message_type", { context: { channel: "email" } });
  const started = performance.now();
  expect((await bound.call("x".repeat(MESSAGE_BYTES))).value).toBe("legitimate");
  expect(performance.now() - started).toBeGreaterThan(timeoutMs * 2.5); // several times the (measured) timeout
  await jeff.close();
  await relay.close();
  await server.close();
}, 30000);

test("a send that has had no bytes accepted for the timeout ends the channel, failing it and every request behind it", async () => {
  const server = await startChannelServer();
  const relay = await startRelay(Number(new URL(server.url).port));
  const jeff = await open(relay.url, "secret-token", 1000);
  const bound = await jeff.function("spam.message_type", { context: { channel: "email" } });
  relay.paused = true;
  const started = performance.now();
  const big = rejection(bound.call("x".repeat(20 * 1024 * 1024)));
  await pause(200); // the big message is filling the socket buffers
  const small = [1, 2, 3].map(() => rejection(bound.call("hi")));
  const results = await Promise.all([big, ...small]);
  const stalled = performance.now() - started;
  const expected = new BackendUnavailable(`${channelUrl(relay.url)}: the socket accepted no bytes of a frame for 1000 ms `
    + "(the server or the network stopped taking data); the channel closed");
  for (const [, error] of results) expect(error).toEqual(expected);
  expect(stalled).toBeGreaterThanOrEqual(1000);
  expect(stalled).toBeLessThan(4000);
  const closing = performance.now();
  await jeff.close();
  expect(performance.now() - closing).toBeLessThan(2000);
  await expect(bound.call("later")).rejects.toThrow(expected);
  await relay.close();
  await server.close();
}, 20000);

test("a server that never answers the opening handshake is BackendUnavailable, promptly", async () => {
  const silent = createTcpServer((socket) => socket.resume()); // reads, so it sees the client end the connection
  await new Promise<void>((resolve) => silent.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(silent.address() as AddressInfo).port}`;
  const [elapsed, error] = await rejection(open(url, "secret-token", 200));
  expect(error).toEqual(new BackendUnavailable(`Cannot reach the Jeff server at ${url}: opening ${channelUrl(url)} took more `
    + "than 200 ms"));
  expect(elapsed).toBeGreaterThanOrEqual(190);
  expect(elapsed).toBeLessThan(400);
  await new Promise((resolve) => silent.close(resolve));
});

test("a server that sends no hello is BackendUnavailable, promptly", async () => {
  const server = await startChannelServer({ silent: ["hello"] });
  const [elapsed, error] = await rejection(open(server.url, "secret-token", 200));
  expect(error).toEqual(new BackendUnavailable(`No hello from the channel ${channelUrl(server.url)} within 200 ms`));
  expect(elapsed).toBeLessThan(400);
  await server.close();
});

test("a server refusing the WebSocket is an EngineError naming the HTTP status", async () => {
  const server = await startChannelServer();
  await expect(open(`${server.url}/elsewhere`)).rejects.toThrow(new EngineError(`The Jeff server at ${server.url}/elsewhere `
    + `refused to open the channel ${channelUrl(`${server.url}/elsewhere`)}: HTTP status 400`));
  await server.close();
});

test("a reply for an id nobody sent ends the channel with EngineError", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url);
  server.send({ id: 999, ok: true, result: {} });
  await pause(50);
  await expect(jeff.function("triage.urgency", { context: CTX })).rejects.toThrow(new EngineError(
    `The channel ${channelUrl(server.url)} sent a reply for id 999, which no request is waiting for; the channel is closed`));
  await jeff.close();
  await server.close();
});

test("a failure with id 0 ends the channel with the error it names", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url);
  server.send({ id: 0, ok: false, error: { type: "Unauthorised", message: "the channel's token expired",
    details: { channel_id: "0badc0de" } } });
  await pause(50);
  const error = await jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }]).then(() => null,
    (caught: unknown) => caught);
  expect(error).toEqual(new Unauthorised("the channel's token expired", { channel_id: "0badc0de" }));
  await jeff.close();
  await server.close();
});

test("the server going away fails waiting and later requests with BackendUnavailable", async () => {
  const server = await startChannelServer({ silent: ["decide"] });
  const jeff = await open(server.url, "secret-token", 5000);
  const waiting = jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }]);
  await pause(50);
  server.dropConnections();
  await expect(waiting).rejects.toThrow(new BackendUnavailable(`${channelUrl(server.url)}: the channel closed (no close frame `
    + "received)"));
  await expect(jeff.function("triage.urgency", { context: CTX })).rejects.toThrow(BackendUnavailable);
  await jeff.close();
  await server.close();
});

test("close ends the channel with a close frame; later requests are InvalidRequest", async () => {
  const server = await startChannelServer();
  const jeff = await open(server.url);
  const bound = await jeff.function("triage.urgency", { context: CTX });
  await jeff.close();
  await expect(jeff.decide([{ function: "triage.urgency", input: "x", context: CTX }])).rejects.toThrow(
    `This remote client of ${server.url} is closed; connect again with Jeff.remote`);
  await expect(bound.call("x")).rejects.toThrow(InvalidRequest);
  await bound.close(); // nothing to release: the handles ended with the connection
  expect(ops(server.received)).toEqual(["hello", "auth", "bind"]);
  await server.close();
});

/** A decision's JSON without its timing (which differs between runs). */
function withoutTiming(decision: Decision): Record<string, unknown> {
  const { timing: _timing, ...rest } = decision.toJSON();
  return rest;
}

async function until(condition: () => boolean, timeoutMs = 5000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > deadline) throw new Error("condition not met in time");
    await pause(10);
  }
}

test("concurrent requests share one channel and each gets its own answer, whatever order the replies come in", async () => {
  const inputs = Array.from({ length: 160 }, (_, index) => `message ${index}${index % 3 === 0 ? " unsure" : ""}`);
  const local = await Jeff.create(new FakeEngine(answers, INSTALLED), Catalogue.shipped());
  const expectedBound = await local.function("triage.urgency", { context: CTX });
  const expected = new Map<string, Record<string, unknown>>();
  for (const input of inputs) expected.set(input, withoutTiming(await expectedBound.call(input)));
  expect(new Set([...expected.values()].map((decision) => decision.abstained))).toEqual(new Set([true, false]));
  // 160 calls, 2 call_many and 2 decide: every reply is held until all 164 are ready, then sent in reverse order
  const server = await startChannelServer({ reorder: 164 });
  const jeff = await open(server.url, "secret-token", 10000);
  const bound = await jeff.function("triage.urgency", { context: CTX });
  const [calls, many, decided] = await Promise.all([
    Promise.all(inputs.map((input) => bound.call(input))),
    Promise.all([bound.many(inputs.slice(0, 5)), bound.many(inputs.slice(5, 12))]),
    Promise.all([12, 13].map((index) => jeff.decide([{ function: "triage.urgency", input: inputs[index] as string, context: CTX }]))),
  ]);
  expect(calls.map(withoutTiming)).toEqual(inputs.map((input) => expected.get(input)));
  expect(many.flat().map(withoutTiming)).toEqual(inputs.slice(0, 12).map((input) => expected.get(input)));
  expect(decided.flat().map(withoutTiming)).toEqual([12, 13].map((index) => expected.get(inputs[index] as string)));
  expect(server.headers.length).toBe(1);
  const ids = server.received.filter((message) => message.op !== "hello").map((message) => message.id);
  expect(new Set(ids).size).toBe(ids.length);
  expect(ids.length).toBe(2 + 164); // auth and bind, then the requests above
  await jeff.close();
  await server.close();
});

test("requests waiting when the channel drops each fail once with BackendUnavailable, and so does every later one", async () => {
  const server = await startChannelServer({ gated: ["call"] });
  const jeff = await open(server.url, "secret-token", 5000);
  const bound = await jeff.function("triage.urgency", { context: CTX });
  const waiting = Array.from({ length: 4 }, (_, index) => bound.call(`pending ${index}`).then(() => null,
    (caught: unknown) => caught));
  await until(() => server.waiting() === 4);
  server.dropConnections();
  const closed = new BackendUnavailable(`${channelUrl(server.url)}: the channel closed (no close frame received)`);
  const errors = await Promise.all(waiting);
  for (const error of errors) expect(error).toEqual(closed);
  expect(new Set(errors).size).toBe(4); // each waiter gets its own copy
  await expect(bound.call("later")).rejects.toThrow(closed);
  server.openGate();
  await jeff.close();
  await server.close();
});

test("the 257th outstanding request is RateLimited and the channel stays open", async () => {
  const server = await startChannelServer({ gated: ["call"] });
  const jeff = await open(server.url, "secret-token", 10000);
  const bound = await jeff.function("triage.urgency", { context: CTX });
  const results = Array.from({ length: 257 }, (_, index) => bound.call(`call ${index}`).then((decision) => decision.value,
    (caught: unknown) => caught));
  await until(() => server.waiting() === 256);
  const limited = await Promise.race(results.map((result) => result.then((value) => value instanceof RateLimited ? value : new Promise(() => {}))));
  expect(limited).toBeInstanceOf(RateLimited);
  server.openGate();
  const settled = await Promise.all(results);
  const refused = settled.filter((value) => value instanceof RateLimited) as RateLimited[];
  expect(refused.length).toBe(1);
  expect(refused[0]?.retryAfterS).toBe(1);
  expect(refused[0]?.details.channel_id).toBe("0badc0de");
  expect(settled.filter((value) => !(value instanceof RateLimited))).toEqual(Array(256).fill(2));
  expect((jeff.transport as ChannelTransport).closed).toBe(false);
  expect((await bound.call("after")).value).toBe(2);
  await jeff.close();
  await server.close();
});
