import { createServer, type IncomingMessage, type ServerResponse } from "node:http";
import type { AddressInfo } from "node:net";
import { expect, test } from "vitest";
import { Catalogue } from "../src/catalogue.js";
import { Jeff } from "../src/client.js";
import {
  BackendUnavailable, ContractError, DeadlineExceeded, EngineError, InvalidRequest, LowConfidence, Unauthorised,
} from "../src/errors.js";
import { escalate, functionStep, llm } from "../src/escalation.js";
import { Agent, errors as undiciErrors, fetch as undiciFetch } from "undici";
import { PhaseTimeout, PhaseTimers } from "../src/phase-timers.js";
import {
  MAX_ANSWER_BODY, MAX_CALLS, RemoteJeff, RestTransport, checkBatch, noAnswerError, readToken, serverPolicy,
} from "../src/remote.js";
import { constants as zlibConstants, crc32, deflateRawSync, deflateSync, gzipSync } from "node:zlib";
import { BodyTooLarge, DecodingError, decodedBody } from "../src/content-coding.js";
import { fixture } from "./conformance/fixtures.js";
import { withoutProxies } from "./helpers/no-proxies.js";

withoutProxies();

type Handler = (request: IncomingMessage, body: string, response: ServerResponse) => void;

async function serve(handler: Handler) {
  const server = createServer((request, response) => {
    let body = "";
    request.on("data", (chunk) => { body += chunk; });
    request.on("end", () => handler(request, body, response));
  });
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  return { url: `http://127.0.0.1:${(server.address() as AddressInfo).port}`,
    close: () => new Promise<void>((resolve) => { server.closeAllConnections(); server.close(() => resolve()); }) };
}

/** The GET /v1/functions answer recorded in remote.json (the shipped catalogue's functions, base bf16). */
const LISTING = (fixture<any>("remote.json").cases[0].exchanges[0].response.body) as string;
const DECISION = JSON.parse(fixture<any>("remote.json").cases[1].exchanges[1].response.body).decisions[0];
const CTX = { company: "A furniture shop.", channel: "email" };

function json(response: ServerResponse, status: number, body: unknown, requestId = "rid-1") {
  response.writeHead(status, { "content-type": "application/json", "x-request-id": requestId }).end(
    typeof body === "string" ? body : JSON.stringify(body));
}

test("a server that cannot be reached is BackendUnavailable", async () => {
  await expect(Jeff.remote("http://127.0.0.1:9", null, { timeoutMs: 2000 })).rejects.toThrow(BackendUnavailable);
});

/** How long `promise` takes to reject, and what with. */
async function rejection(promise: Promise<unknown>): Promise<[number, unknown]> {
  const started = performance.now();
  const error = await promise.then(() => null, (caught: unknown) => caught);
  return [performance.now() - started, error];
}

test("a server that does not send headers in time is DeadlineExceeded, promptly", async () => {
  const server = await serve(() => {});
  const [elapsed, error] = await rejection(Jeff.remote(server.url, null, { timeoutMs: 200 }));
  expect(error).toEqual(new DeadlineExceeded(
    `The Jeff server at ${server.url} did not answer GET /v1/functions within 200 ms; the server may still have run the request`));
  expect(elapsed).toBeGreaterThanOrEqual(190);
  expect(elapsed).toBeLessThan(400);
  await server.close();
});

test("an answer that is not a Jeff error is an EngineError quoting it", async () => {
  const server = await serve((_request, _body, response) => response.writeHead(502).end("Bad gateway"));
  await expect(Jeff.remote(server.url, null, { timeoutMs: 2000 })).rejects.toThrow(new EngineError(
    `The Jeff server at ${server.url} answered GET /v1/functions with status 502 and a body that is not a Jeff error: Bad gateway`));
  await server.close();
});

test("the URL, the token and the timeout are checked", async () => {
  expect(() => new RestTransport("ftp://x", null, 1000)).toThrow(new InvalidRequest(
    "The server URL 'ftp://x' must start with http:// or https://"));
  await expect(Jeff.remote("http://x", null, { timeoutMs: 0 })).rejects.toThrow(InvalidRequest);
  const transport = new RestTransport("http://127.0.0.1:9", () => "", 1000);
  await expect(transport.functions()).rejects.toThrow(new Unauthorised(
    "The token for http://127.0.0.1:9 must be a non-empty string; the token function returned an empty string"));
  await expect(readToken(42 as unknown as string, "http://h")).rejects.toThrow(new Unauthorised(
    "The token for http://h must be a non-empty string; got a value of type number"));
});

test("a token is never put in an error message", async () => {
  const secret = "s3cret\nvalue";
  const error = await readToken(() => secret, "http://h").catch((caught: unknown) => caught as Error);
  expect(error).toBeInstanceOf(Unauthorised);
  expect((error as Error).message).not.toContain("s3cret");
});

test("the token function is called before every request and sent as a bearer token", async () => {
  const seen: Array<string | undefined> = [];
  const server = await serve((request, _body, response) => {
    seen.push(request.headers.authorization);
    json(response, 200, LISTING);
  });
  let count = 0;
  const jeff = await Jeff.remote(server.url, async () => { count += 1; return `token-${count}`; }, { timeoutMs: 2000 });
  await jeff.engine.installed(true);
  expect(seen).toEqual(["Bearer token-1", "Bearer token-2"]);
  await jeff.close();
  await server.close();
});

test("a wire error becomes its JeffError with the answer's request id", async () => {
  const server = await serve((request, _body, response) => {
    if (request.method === "GET") return json(response, 200, LISTING);
    return json(response, 401, { error: { type: "Unauthorised", message: "The token has expired", details: {} } }, "rid-42");
  });
  const jeff = await Jeff.remote(server.url, "t", { timeoutMs: 2000 });
  const error = await jeff.decide([{ function: "spam.is_spam", input: "Hi", context: { channel: "sms" } }])
    .then(() => null, (caught: unknown) => caught as Unauthorised) as Unauthorised;
  expect(error).toBeInstanceOf(Unauthorised);
  expect([error.message, error.requestId]).toEqual(["The token has expired", "rid-42"]);
  await jeff.close();
  await server.close();
});

test("a shorthand naming a function of another interface is refused before any request", async () => {
  let posts = 0;
  const server = await serve((request, _body, response) => {
    if (request.method === "GET") return json(response, 200, LISTING);
    posts += 1;
    return json(response, 500, "unexpected");
  });
  const jeff = await Jeff.remote(server.url, null, { timeoutMs: 2000 });
  await expect(jeff.classify("x", { function: "jeff.support.triage.urgency", context: CTX })).rejects.toThrow(
    "implements rate, not classify");
  expect(posts).toBe(0);
  await jeff.close();
  await server.close();
});

test("a server decision that does not match the decision schema is a ContractError", async () => {
  const server = await serve((request, _body, response) => {
    if (request.method === "GET") return json(response, 200, LISTING);
    return json(response, 200, { decisions: [{ ...DECISION, abstained: "no" }] });
  });
  const jeff = await Jeff.remote(server.url, null, { timeoutMs: 2000 });
  await expect(jeff.rate("x", { function: "jeff.support.triage.urgency", context: CTX })).rejects.toThrow(ContractError);
  await jeff.close();
  await server.close();
});

test("a LowConfidence whose decision does not match the decision schema is a ContractError", async () => {
  const server = await serve((request, _body, response) => {
    if (request.method === "GET") return json(response, 200, LISTING);
    return json(response, 409, { error: { type: "LowConfidence", message: "abstained", details: { decision: { value: 1 } } } });
  });
  const jeff = await Jeff.remote(server.url, null, { timeoutMs: 2000 });
  const error = await jeff.rate("x", { function: "jeff.support.triage.urgency", context: CTX, onLowConfidence: "error" })
    .catch((caught: unknown) => caught as Error);
  expect(error).toBeInstanceOf(ContractError);
  expect(error).not.toBeInstanceOf(LowConfidence);
  expect((error as ContractError).requestId).toBe("rid-1");
  await jeff.close();
  await server.close();
});

test("an answer whose body stops arriving is DeadlineExceeded, promptly", async () => {
  const server = await serve((_request, _body, response) => {
    response.writeHead(200, { "content-type": "application/json" });
    response.write("{\"base_format\":");
  });
  const [elapsed, error] = await rejection(Jeff.remote(server.url, null, { timeoutMs: 300 }));
  expect(error).toEqual(new DeadlineExceeded(
    `The Jeff server at ${server.url} did not answer GET /v1/functions within 300 ms; the server may still have run the request`));
  expect(elapsed).toBeLessThan(500);
  await server.close();
});

test("the connect timer runs until the request reaches a socket, and aborts with the connect phase", async () => {
  const timers = new PhaseTimers(100);
  // a dispatch that never gets a socket: onRequestStart is never called
  timers.interceptor()(() => true)({ origin: "http://h", path: "/", method: "GET" }, {});
  const started = performance.now();
  await new Promise((resolve) => timers.signal.addEventListener("abort", resolve));
  expect(performance.now() - started).toBeLessThan(250);
  expect(timers.signal.reason).toEqual(new PhaseTimeout("connect", 100));
  expect([timers.started, timers.running]).toEqual([false, false]);
});

test("a connection attempt that fails after the caller's connect timeout leaves no unhandled rejection", async () => {
  const unhandled: unknown[] = [];
  const listener = (reason: unknown) => { unhandled.push(reason); };
  process.on("unhandledRejection", listener);
  // a connector that fails only after the caller's connect timer has fired, as the backstop connect.timeout does
  const agent = new Agent({ connect: (_options, callback) => {
    setTimeout(() => callback(new undiciErrors.ConnectTimeoutError(), null), 250);
  }, headersTimeout: 0, bodyTimeout: 0 });
  const timers = new PhaseTimers(100);
  const error = await undiciFetch("http://127.0.0.1:59999/v1/functions", { dispatcher: agent.compose(timers.interceptor()),
    signal: timers.signal }).then(() => null, (caught: unknown) => caught);
  timers.finish();
  expect(noAnswerError(error, timers.started, "http://h", "GET", "/v1/functions", 100)).toEqual(new BackendUnavailable(
    "Cannot reach the Jeff server at http://h: no connection within 100 ms"));
  await new Promise((resolve) => setTimeout(resolve, 400));
  process.off("unhandledRejection", listener);
  expect(unhandled).toEqual([]);
  await agent.destroy();
});

test("no timer is left running once a request has its answer", async () => {
  const server = await serve((_request, _body, response) => json(response, 200, LISTING));
  const agent = new Agent({ connect: { timeout: 0 }, headersTimeout: 0, bodyTimeout: 0 });
  const timers = new PhaseTimers(5000);
  const response = await undiciFetch(`${server.url}/v1/functions`, { dispatcher: agent.compose(timers.interceptor()),
    signal: timers.signal });
  await response.text();
  expect([timers.started, timers.running]).toEqual([true, false]);
  await agent.destroy();
  await server.close();
});

test("failures map to Python's errors: connecting is BackendUnavailable, waiting for an answer DeadlineExceeded", () => {
  const failed = (cause: Error) => new TypeError("fetch failed", { cause });
  const map = (error: unknown, started: boolean) => noAnswerError(error, started, "http://h", "GET", "/v1/functions", 500);
  const refused = Object.assign(new Error("connect ECONNREFUSED 10.0.0.1:80"), { code: "ECONNREFUSED", syscall: "connect" });
  expect(map(new PhaseTimeout("connect", 500), false)).toEqual(new BackendUnavailable(
    "Cannot reach the Jeff server at http://h: no connection within 500 ms"));
  expect(map(failed(refused), false)).toEqual(new BackendUnavailable(
    "Cannot reach the Jeff server at http://h: connect ECONNREFUSED 10.0.0.1:80"));
  expect(map(failed(new undiciErrors.ConnectTimeoutError()), true)).toEqual(new BackendUnavailable(
    "Cannot reach the Jeff server at http://h: Connect Timeout Error"));
  const deadline = new DeadlineExceeded(
    "The Jeff server at http://h did not answer GET /v1/functions within 500 ms; the server may still have run the request");
  expect(map(new PhaseTimeout("headers", 500), true)).toEqual(deadline);
  expect(map(new PhaseTimeout("body", 500), true)).toEqual(deadline);
  expect(map(failed(new undiciErrors.SocketError("other side closed")), true)).toEqual(new BackendUnavailable(
    "The connection to the Jeff server at http://h failed during GET /v1/functions: SocketError: other side closed"));
  const parser = Object.assign(new Error("Response does not match the HTTP/1.1 protocol"), { name: "HTTPParserError", code: "HPE_INVALID_CONSTANT" });
  expect(map(failed(parser), true)).toEqual(new BackendUnavailable("The connection to the Jeff server at http://h failed "
    + "during GET /v1/functions: HTTPParserError: Response does not match the HTTP/1.1 protocol"));
});

test("a redirect is not followed: a 302 is an answer that is not a Jeff error, within the timeout", async () => {
  const paths: string[] = [];
  const server = await serve((request, _body, response) => {
    paths.push(request.url as string);
    if (request.url === "/v1/functions") response.writeHead(302, { location: "/never" }).end("moved");
  });
  const [elapsed, error] = await rejection(Jeff.remote(server.url, null, { timeoutMs: 200 }));
  expect(error).toEqual(new EngineError(`The Jeff server at ${server.url} answered GET /v1/functions with status 302 and a `
    + "body that is not a Jeff error: moved"));
  expect(elapsed).toBeLessThan(200);
  expect(paths).toEqual(["/v1/functions"]);
  await server.close();
});

test("a PhaseTimers times one request: a second dispatch is a programming error", () => {
  const timers = new PhaseTimers(1000);
  const dispatch = timers.interceptor()(() => true);
  dispatch({ origin: "http://h", path: "/", method: "GET" }, {});
  expect(() => dispatch({ origin: "http://h", path: "/", method: "GET" }, {})).toThrow("already timed a request");
  timers.finish();
});

test("an answer that is not HTTP is a broken connection, not an unreachable server", async () => {
  const { createServer: createTcpServer } = await import("node:net");
  const sockets = new Set<import("node:net").Socket>();
  const tcp = createTcpServer((socket) => { sockets.add(socket); socket.end("this is not HTTP\r\n\r\n"); });
  await new Promise<void>((resolve) => tcp.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(tcp.address() as AddressInfo).port}`;
  const error = await rejection(Jeff.remote(url, null, { timeoutMs: 2000 })).then(([, caught]) => caught as Error);
  expect(error).toBeInstanceOf(BackendUnavailable);
  expect(error.message).toMatch(new RegExp(`^The connection to the Jeff server at ${url} failed during GET /v1/functions: `));
  for (const socket of sockets) socket.destroy();
  await new Promise<void>((resolve) => tcp.close(() => resolve()));
});

test("closing aborts a request in flight with the closed error", async () => {
  const server = await serve(() => {});
  const transport = new RestTransport(server.url, null, 5000);
  const pending = transport.functions();
  await new Promise((resolve) => setTimeout(resolve, 50));
  await transport.close();
  expect((transport as unknown as { inFlight: Set<unknown> }).inFlight.size).toBe(0);
  await expect(pending).rejects.toThrow(new InvalidRequest(
    `This remote client of ${server.url} is closed; connect again with Jeff.remote`));
  await server.close();
});

test("a closed client refuses requests", async () => {
  const server = await serve((_request, _body, response) => json(response, 200, LISTING));
  const jeff = await Jeff.remote(server.url, null, { timeoutMs: 2000 }) as RemoteJeff;
  await jeff.close();
  await expect(jeff.transport.functions()).rejects.toThrow(new InvalidRequest(
    `This remote client of ${server.url} is closed; connect again with Jeff.remote`));
  await server.close();
});

test("policies: the server runs return, error and function steps; llm steps run here", () => {
  expect(serverPolicy("return")).toBe("return");
  expect(serverPolicy(escalate([functionStep("a.b.c")], { budgetMs: 50 }))).toEqual({ escalate: ["a.b.c"], budget_ms: 50 });
  expect(serverPolicy(escalate([functionStep("a.b.c"), llm(() => "x")]))).toBeNull();
});

test("more calls than one request carries are refused, not split", async () => {
  expect(() => checkBatch(MAX_CALLS, "calls")).not.toThrow();
  expect(() => checkBatch(MAX_CALLS + 1, "calls")).toThrow(new InvalidRequest(
    "257 calls in one request: a Jeff server answers at most 256 per request; send them in batches of at most 256"));
});

test("a registered graph with an llm node policy is refused before any request", async () => {
  const server = await serve((_request, _body, response) => json(response, 200, LISTING));
  const jeff = await Jeff.remote(server.url, null, { timeoutMs: 2000, catalogue: Catalogue.shipped() }) as RemoteJeff;
  await expect(jeff.runServerGraph("g", "x", {}, { n: escalate([llm(() => "1")]) })).rejects.toThrow(
    "Node n: a graph registered on the server runs there");
  await jeff.close();
  await server.close();
});

// ---- the answer-size limit (MAX_ANSWER_BODY, 64 MiB as sent and once decoded)

/** 64 MiB and one byte of zeros, gzipped once (about 65 KB) and twice (about 1 KB). */
const PAST_LIMIT = Buffer.alloc(MAX_ANSWER_BODY + 1);
const GZIPPED = gzipSync(PAST_LIMIT);
const GZIPPED_TWICE = gzipSync(GZIPPED);

/** A server answering every request with `head` (status line and headers) then `body`, which counts the connections
 * that ended. */
async function rawServer(head: string, body: Buffer) {
  const { createServer: createTcpServer } = await import("node:net");
  const sockets = new Set<import("node:net").Socket>();
  let ended = 0;
  const tcp = createTcpServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => { ended += 1; });
    socket.on("error", () => {});
    socket.once("data", () => { socket.write(head); socket.end(body); });
  });
  await new Promise<void>((resolve) => tcp.listen(0, "127.0.0.1", resolve));
  return {
    url: `http://127.0.0.1:${(tcp.address() as AddressInfo).port}`,
    ended: () => ended,
    close: () => new Promise<void>((resolve) => { for (const socket of sockets) socket.destroy(); tcp.close(() => resolve()); }),
  };
}

for (const [encoding, body] of [["gzip", GZIPPED], ["gzip, gzip", GZIPPED_TWICE]] as const) {
  test(`an answer whose Content-Encoding "${encoding}" decodes past the limit is refused by name and closes the client`, async () => {
    expect(body.length).toBeLessThan(100_000);
    const server = await rawServer("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-encoding: "
      + `${encoding}\r\ncontent-length: ${body.length}\r\n\r\n`, body);
    const transport = new RestTransport(server.url, null, 10000);
    await expect(transport.functions()).rejects.toThrow(new BackendUnavailable(`The Jeff server at ${server.url} answered `
      + "GET /v1/functions with a body that decodes (Content-Encoding) to more than 67108864 bytes, the most this client "
      + "reads; the client is closed, connect again with Jeff.remote"));
    await expect(transport.functions()).rejects.toThrow(new InvalidRequest(
      `This remote client of ${server.url} is closed; connect again with Jeff.remote`));
    await expect.poll(server.ended).toBe(1);
    await server.close();
  });
}

test("a body with no Content-Length, ended by the connection, is held to the limit", async () => {
  const server = await rawServer("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\nconnection: close\r\n\r\n",
    Buffer.alloc(MAX_ANSWER_BODY + 1024 * 1024, 0x20));
  const [, error] = await rejection(Jeff.remote(server.url, null, { timeoutMs: 10000 }));
  expect(error).toEqual(new BackendUnavailable(`The Jeff server at ${server.url} answered GET /v1/functions with a body `
    + "of more than 67108864 bytes, the most this client reads; the client is closed, connect again with Jeff.remote"));
  await server.close();
});

test("decodedBody undoes gzip and deflate (zlib-wrapped or raw) layer by layer, each held to the limit", async () => {
  const data = Buffer.alloc(1000, 7);
  for (const [coding, body] of [["gzip, gzip", gzipSync(gzipSync(data))], ["deflate", deflateSync(data)],
    ["deflate", deflateRawSync(data)], ["gzip, deflate", deflateSync(gzipSync(data))]] as const) {
    expect(await decodedBody(body, coding, 1000)).toEqual(data);
    await expect(decodedBody(body, coding, 999)).rejects.toThrow(BodyTooLarge);
  }
  // br, zstd, x-gzip, identity and unknown codings are passed over, as httpx passes them over
  const gzipped = gzipSync(Buffer.from("{}"));
  expect((await decodedBody(gzipped, "br, x-gzip, zstd, gzip, identity, unknown", 10)).toString()).toBe("{}");
  expect(await decodedBody(gzipped, "br", 100)).toEqual(gzipped);
  expect((await decodedBody(Buffer.from("{}"), null, 10)).toString()).toBe("{}");
});

test("decodedBody reads one gzip member as zlib's decompressobj does: bytes after it passed over, a cut one partly", async () => {
  const whole = gzipSync(Buffer.from("hello world"));
  expect((await decodedBody(Buffer.concat([whole, Buffer.from("junk after the member")]), "gzip")).toString()).toBe("hello world");
  expect((await decodedBody(Buffer.concat([whole, gzipSync(Buffer.from("second"))]), "gzip")).toString()).toBe("hello world");
  const cut = (await decodedBody(whole.subarray(0, whole.length - 8), "gzip")).toString();
  expect("hello world".startsWith(cut)).toBe(true);
  expect(await decodedBody(Buffer.alloc(0), "gzip")).toHaveLength(0);
});

test("decodedBody refuses a body that is not its coding with zlib's message as Python words it", async () => {
  const refusal = async (body: Buffer, coding: string) => {
    try {
      await decodedBody(body, coding);
    } catch (error) {
      expect(error).toBeInstanceOf(DecodingError);
      return (error as Error).message;
    }
    throw new Error("decodedBody accepted it");
  };
  expect(await refusal(Buffer.from("not gzip"), "gzip")).toBe("Error -3 while decompressing data: incorrect header check");
  expect(await refusal(Buffer.from([0xff, 0xff, 0xff]), "deflate")).toBe("Error -3 while decompressing data: invalid block type");
  const wrongCheck = gzipSync(Buffer.from("hello"));
  wrongCheck.writeUInt32LE(crc32(Buffer.from("other")), wrongCheck.length - 8);
  expect(await refusal(wrongCheck, "gzip")).toBe("Error -3 while decompressing data: incorrect data check");
  const wrongLength = gzipSync(Buffer.from("hello"));
  wrongLength.writeUInt32LE(6, wrongLength.length - 4);
  expect(await refusal(wrongLength, "gzip")).toBe("Error -3 while decompressing data: incorrect length check");
  expect(zlibConstants.Z_NEED_DICT).toBe(2);
});

test("a gzip body that is not gzip is an EngineError without a request id, as httpx's DecodingError", async () => {
  const server = await rawServer("HTTP/1.1 200 OK\r\ncontent-encoding: gzip\r\nx-request-id: r\r\ncontent-length: 8\r\n\r\n",
    Buffer.from("not gzip"));
  const transport = new RestTransport(server.url, null, 2000);
  const error = await transport.functions().then(() => null, (caught: unknown) => caught as EngineError);
  expect(error).toEqual(new EngineError(`The answer of the Jeff server at ${server.url} to GET /v1/functions could not be `
    + "read: DecodingError: Error -3 while decompressing data: incorrect header check"));
  expect(error?.requestId).toBeNull();
  await transport.close();
  await server.close();
});

/** The review's "gzip, gzip" answer: its middle layer is 100 MB of empty stored deflate blocks (5 bytes each, 20
 * million of them) around a small listing, so the last layer is small and only the middle one passes the limit. */
function middleLayerBomb(): Buffer {
  const listing = Buffer.from('{"base_format":{"format":"bf16"},"functions":[]}');
  const empty = Buffer.alloc(100_000_000);
  empty.fill(Buffer.from([0x00, 0x00, 0x00, 0xff, 0xff]));
  const last = Buffer.alloc(5);
  last.writeUInt8(1, 0);
  last.writeUInt16LE(listing.length, 1);
  last.writeUInt16LE(0xffff ^ listing.length, 3);
  const trailer = Buffer.alloc(8);
  trailer.writeUInt32LE(crc32(listing), 0);
  trailer.writeUInt32LE(listing.length, 4);
  const middle = Buffer.concat([Buffer.from([0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff]), empty, last, listing, trailer]);
  return gzipSync(middle, { level: 1 });
}

test("a \"gzip, gzip\" answer whose middle layer is past the limit is refused, though its last layer is small", async () => {
  const bomb = middleLayerBomb();
  expect(bomb.length).toBeLessThan(1_000_000);
  const server = await rawServer("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-encoding: gzip, gzip\r\n"
    + `content-length: ${bomb.length}\r\n\r\n`, bomb);
  const transport = new RestTransport(server.url, null, 10000);
  await expect(transport.functions()).rejects.toThrow(new BackendUnavailable(`The Jeff server at ${server.url} answered `
    + "GET /v1/functions with a body that decodes (Content-Encoding) to more than 67108864 bytes, the most this client "
    + "reads; the client is closed, connect again with Jeff.remote"));
  await server.close();
});

test("an endless gzip stream of empty blocks ends at the limit of the body as sent, not by a timer", async () => {
  const { createServer: createTcpServer } = await import("node:net");
  const block = Buffer.alloc(5 * 200_000);
  block.fill(Buffer.from([0x00, 0x00, 0x00, 0xff, 0xff]));
  let sent = 0;
  const sockets = new Set<import("node:net").Socket>();
  const tcp = createTcpServer((socket) => {
    sockets.add(socket);
    socket.on("error", () => {});
    socket.once("data", () => {
      socket.write("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-encoding: gzip\r\nconnection: close\r\n\r\n");
      socket.write(Buffer.from([0x1f, 0x8b, 8, 0, 0, 0, 0, 0, 0, 0xff]));
      const pump = () => {
        while (!socket.destroyed && socket.write(block)) sent += block.length;
        if (!socket.destroyed) socket.once("drain", () => { sent += block.length; pump(); });
      };
      pump();
    });
  });
  await new Promise<void>((resolve) => tcp.listen(0, "127.0.0.1", resolve));
  const url = `http://127.0.0.1:${(tcp.address() as AddressInfo).port}`;
  const transport = new RestTransport(url, null, 5000);
  await expect(transport.functions()).rejects.toThrow(new BackendUnavailable(`The Jeff server at ${url} answered GET `
    + "/v1/functions with a body of more than 67108864 bytes, the most this client reads; the client is closed, connect "
    + "again with Jeff.remote"));
  // the client stopped reading at the limit: the server could send little more than it (socket buffers) before the
  // connection was dropped
  expect(sent).toBeLessThan(MAX_ANSWER_BODY + 16 * 1024 * 1024);
  for (const socket of sockets) socket.destroy();
  await new Promise<void>((resolve) => tcp.close(() => resolve()));
});

test("requests ask for gzip and deflate only, as httpx does, and a gzipped answer is read", async () => {
  const seen: Array<string | undefined> = [];
  const server = await serve((request, _body, response) => {
    seen.push(request.headers["accept-encoding"]);
    response.writeHead(200, { "content-type": "application/json", "content-encoding": "gzip" }).end(gzipSync(LISTING));
  });
  const transport = new RestTransport(server.url, null, 2000);
  expect((await transport.functions()).base_format).toEqual({ format: "bf16" });
  expect(seen).toEqual(["gzip, deflate"]);
  await transport.close();
  await server.close();
});

test("an integer past 2**53 (a bigint, as readYaml reads one) is sent with its exact digits, as Python's json.dumps", async () => {
  const bodies: string[] = [];
  const server = await serve((_request, body, response) => {
    bodies.push(body);
    json(response, 500, { error: { type: "EngineError", message: "stop here" } });
  });
  const transport = new RestTransport(server.url, null, 2000);
  await expect(transport.dispatch({ dispatch: { input: { n: 9007199254740993n } } })).rejects.toThrow("stop here");
  expect(bodies).toEqual(['{"dispatch":{"input":{"n":9007199254740993}}}']);
  await transport.close();
  await server.close();
});

test("decoding a 50-layer answer leaves the event loop running: a timer fires while it decodes", async () => {
  // 50 layers of deflate over 2 MiB, each layer a little larger than the one inside it (stored blocks at level 0)
  let body = Buffer.alloc(2 * 1024 * 1024, 7);
  const inner = body;
  for (let layer = 0; layer < 50; layer++) body = deflateSync(body, { level: 0 });
  let ticks = 0;
  const timer = setInterval(() => { ticks += 1; }, 1);
  const started = performance.now();
  let decodedAt = 0;
  const decoding = decodedBody(body, Array(50).fill("deflate").join(", ")).then((decoded) => {
    decodedAt = performance.now() - started;
    return decoded;
  });
  const decoded = await decoding;
  clearInterval(timer);
  expect(decoded).toEqual(inner);
  // the timer ran while the layers were decoded: each layer is decoded on zlib's thread pool, between event-loop turns
  expect(ticks).toBeGreaterThan(0);
  expect(decodedAt).toBeGreaterThan(0);
});

test("decodedBody stops before the next layer once its signal is aborted", async () => {
  let body = Buffer.from(LISTING);
  for (let layer = 0; layer < 20; layer++) body = deflateSync(body, { level: 0 });
  const stop = new AbortController();
  stop.abort(new Error("stopped"));
  await expect(decodedBody(body, Array(20).fill("deflate").join(", "), MAX_ANSWER_BODY, stop.signal)).rejects.toThrow("stopped");
});

test("closing the client while an answer is being decoded stops it, and the request fails with the closed error", async () => {
  // 1,500 stored deflate layers (about what fits in the header limit) over 512 KiB: some hundreds of milliseconds of
  // decoding, layer after layer
  const layers = 1500;
  let body = Buffer.alloc(512 * 1024, 0x20);
  for (let layer = 0; layer < layers; layer++) body = deflateSync(body, { level: 0 });
  const coding = Array(layers).fill("deflate").join(", ");
  const started = performance.now();
  await decodedBody(body, coding);
  const wholeDecode = performance.now() - started;
  const server = await rawServer("HTTP/1.1 200 OK\r\ncontent-type: application/json\r\ncontent-encoding: "
    + `${coding}\r\ncontent-length: ${body.length}\r\n\r\n`, body);
  const transport = new RestTransport(server.url, null, 10000);
  const requested = performance.now();
  const pending = transport.functions().then(() => null, (caught: unknown) => [performance.now() - requested, caught] as const);
  // the body arrives in a few milliseconds on this machine; close part way through its decoding
  await new Promise((resolve) => setTimeout(resolve, Math.min(100, wholeDecode / 4)));
  await transport.close();
  const outcome = await pending;
  expect(outcome).not.toBeNull();
  const [elapsed, error] = outcome as readonly [number, unknown];
  expect(error).toEqual(new InvalidRequest(`This remote client of ${server.url} is closed; connect again with Jeff.remote`));
  // stopped between layers, well before the whole answer would have been decoded
  expect(elapsed).toBeLessThan(wholeDecode * 0.75);
  await server.close();
});
