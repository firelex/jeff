/** Remote mode: Jeff's surface over a Jeff HTTP server (port of jeff/remote.py). RemoteJeff keeps a local catalogue,
 * so binding, labels and policies are worked out as in local mode, and each operation is one POST /v1/decide. A
 * policy the server can run ("return", "error", function steps only) is sent with the request; one with an llm,
 * human or remote step runs here on the decision the server returns. Connecting reads GET /v1/functions once and
 * refuses a server serving another version of an adapter the local catalogue has. */

import { Agent, Pool, ProxyAgent, type Dispatcher, request as undiciRequest } from "undici";
import { ACCEPT_ENCODING, BodyTooLarge, DecodingError, MAX_ANSWER_BODY, decodedBody } from "./content-coding.js";
import type { Binding, Call, Labels, Row } from "./binding.js";
import { PhaseTimeout, PhaseTimers } from "./phase-timers.js";
import { Catalogue } from "./catalogue.js";
import {
  BoundFunction, Jeff, checkTimeoutMs, jeffParts, type CallOptions, type DispatchOptions, type JeffParts, type ShorthandOptions,
} from "./client.js";
import { type SchemaName, firstProblem, isValid } from "./contract.js";
import { Decision, type DecisionJSON } from "./decision.js";
import type { BaseFormat, Engine, EngineResult } from "./engine.js";
import {
  BackendUnavailable, ContractError, DeadlineExceeded, EngineError, InterfaceMismatch, InvalidPolicy, InvalidRequest,
  JeffError, Unauthorised, errorFromWire, lowConfidence, notInstalled,
} from "./errors.js";
import { AskStep, Escalate, FunctionStep, type Policy, RemoteStep, checkPolicy, unknownPolicy } from "./escalation.js";
import { type CodeFunction, type Graph, GraphResult, checkGraph, graphToDict, runChecked } from "./graph.js";
import { type Calibration, type FunctionSpec, calibrationFromWire, resolveCalibration } from "./manifest.js";
import { pyRepr, pySorted } from "./pyformat.js";
import { httpProxyFor } from "./proxy.js";
import { DEFAULT_ROUTER, leafCall, startRouter } from "./routing.js";
import type { JSONValue } from "./types.js";

/** The bearer token: a string, a function returning the current token (called before every HTTP request, so it can
 * refresh), or null for a server with no credentials configured. */
export type TokenSource = string | (() => string | Promise<string>) | null;
/** The wire form of a policy the server runs (contract/schemas/decide_request.schema.json, $defs/policy). */
export type WirePolicy = string | { escalate: string[]; budget_ms?: number };
export interface WireCall {
  function: string;
  input: JSONValue;
  context?: Record<string, JSONValue>;
  labels?: Record<string, string | null> | string[];
}
export type GraphResultJSON = {
  nodes: Record<string, Record<string, unknown>>; outputs: Record<string, Record<string, unknown>>; stages: number;
};
export type FunctionsResponse = { base_format: BaseFormat; functions: Array<Record<string, unknown>> };

/** The most calls one request carries (contract/schemas/decide_request); more are refused, never split. */
export const MAX_CALLS = 256;
export const REQUEST_ID_HEADER = "x-request-id";
/** Characters of a non-Jeff error body quoted in the EngineError. */
const BODY_EXCERPT = 300;
export { MAX_ANSWER_BODY } from "./content-coding.js";
/** What an HTTP header value may carry: printable ASCII. */
const HEADER_SAFE = /^[\x20-\x7e]+$/;

/** How RemoteJeff reaches the server: one round trip per method. Bodies and answers are the JSON shapes of
 * contract/schemas/decide_request and decide_response. */
export interface Transport {
  /** The decisions for `calls`, in order, with `policy` applied by the server. */
  decideCalls(calls: WireCall[], policy: WirePolicy): Promise<DecisionJSON[]>;
  /** The routed decision for a {dispatch, on_low_confidence} body. */
  dispatch(body: Record<string, unknown>): Promise<DecisionJSON>;
  /** The graph result for a {graph, input, params, ...} body. */
  graph(body: Record<string, unknown>): Promise<GraphResultJSON>;
  /** The server's GET /v1/functions answer (contract/schemas/functions_response). */
  functions(): Promise<FunctionsResponse>;
  close(): Promise<void>;
}

/** The wire form of a policy the server can run: "return", "error", or {escalate: [function refs], budget_ms?};
 * null when the policy has an llm, human or remote step, which only this process can run. */
export function serverPolicy(policy: Policy): WirePolicy | null {
  if (policy === "return" || policy === "error") return policy;
  if (!(policy instanceof Escalate)) throw unknownPolicy(policy);
  const refs: string[] = [];
  for (const step of policy.steps) {
    if (step instanceof AskStep || step instanceof RemoteStep) return null;
    if (!(step instanceof FunctionStep)) {
      throw new InvalidPolicy(`escalate() got ${pyRepr(step)}, which is not a step; use functionStep(...), remote(...), `
        + "llm(...) or human(...)");
    }
    refs.push(step.ref);
  }
  return policy.budgetMs === null ? { escalate: refs } : { escalate: refs, budget_ms: policy.budgetMs };
}

/** What a token that is not one was, described without ever showing a token's text. */
function describeBadToken(token: unknown): string {
  if (token === "") return "an empty string";
  if (token === null) return "null";
  if (token === undefined) return "undefined";
  return `a value of type ${typeof token}`;
}

/** The current token: the string, or what the function returns now (called once per use); null when there is no
 * token. Unauthorised when it is not a non-empty string an HTTP header can carry. No message shows the token. */
export async function readToken(source: TokenSource, url: string): Promise<string | null> {
  if (source === null) return null;
  const fromFunction = typeof source === "function";
  const token: unknown = fromFunction ? await source() : source;
  const what = fromFunction ? "the token function returned" : "got";
  if (typeof token !== "string" || token === "") {
    throw new Unauthorised(`The token for ${url} must be a non-empty string; ${what} ${describeBadToken(token)}`);
  }
  if (!HEADER_SAFE.test(token)) {
    throw new Unauthorised(`The token for ${url} must be printable ASCII, as an HTTP header carries it; ${what} a string `
      + "with other characters (not shown)");
  }
  return token;
}

/** InvalidRequest when `count` calls or inputs are more than one request may carry (MAX_CALLS). */
export function checkBatch(count: number, what: string): void {
  if (count > MAX_CALLS) {
    throw new InvalidRequest(`${count} ${what} in one request: a Jeff server answers at most ${MAX_CALLS} per request; send `
      + `them in batches of at most ${MAX_CALLS}`);
  }
}

/** The error for a server serving another version of an adapter than the local catalogue has: labels would be mapped
 * by the wrong manifest. */
export function versionMismatch(adapterId: string, served: string, local: string): ContractError {
  return new ContractError(`${adapterId}: the server serves version ${served}, this client's catalogue has ${local}; pass `
    + "catalogue= with the server's manifests");
}

export function labelsToWire(labels: Labels): Record<string, string | null> | string[] {
  return Array.isArray(labels) ? [...(labels as readonly string[])] : { ...(labels as Record<string, string | null>) };
}

/** A call as the server takes it, naming the function by the ID the local catalogue resolved, and its context by the
 * code names binding accepted. */
function callToWire(call: Call, binding: Binding): WireCall {
  const wire: WireCall = { function: binding.function.id, input: call.input };
  if (call.context !== null && call.context !== undefined) wire.context = { ...call.context };
  if (call.labels !== null && call.labels !== undefined) wire.labels = labelsToWire(call.labels);
  return wire;
}

/** A decision the server answered, checked against contract/schemas/decision before it is built. */
function serverDecision(data: unknown, url: string): Decision {
  if (!isValid("decision", data)) {
    throw new ContractError(`A decision from ${url} does not match contract/schemas/decision: ${firstProblem("decision", data)}`);
  }
  return Decision.fromJSON(data as DecisionJSON);
}

/** A graph result the server answered, checked against contract/schemas/graph_result before it is built. */
function serverGraphResult(data: unknown, url: string): GraphResult {
  if (!isValid("graph_result", data)) {
    throw new ContractError(`A graph result from ${url} does not match contract/schemas/graph_result: `
      + firstProblem("graph_result", data));
  }
  return GraphResult.fromJSON(data as GraphResultJSON);
}

/** The body of an answer as sent, read chunk by chunk and given up (the stream destroyed, and with it the
 * connection) at the first chunk that takes it past MAX_ANSWER_BODY, whatever its framing (Content-Length, chunked,
 * or none, ended by the connection), so an endless body is never read past the limit. */
async function rawBody(body: AsyncIterable<Buffer> & { destroy(error?: Error): unknown }): Promise<Buffer> {
  const chunks: Buffer[] = [];
  let length = 0;
  for await (const chunk of body) {
    if (length + chunk.length > MAX_ANSWER_BODY) {
      const tooLarge = new BodyTooLarge(false);
      body.destroy(tooLarge);
      throw tooLarge;
    }
    chunks.push(chunk);
    length += chunk.length;
  }
  return Buffer.concat(chunks, length);
}

/** JSON.stringify's replacer for a bigint (an integer past 2**53 read from YAML, readYaml): its exact digits, as
 * Python's json.dumps writes a large int, instead of the TypeError JSON.stringify raises for one. */
function exactIntegers(_key: string, value: unknown): unknown {
  // JSON.rawJSON is in Node 21 and later (engines: node >= 22); TypeScript 5.8's lib does not declare it yet
  return typeof value === "bigint" ? (JSON as unknown as { rawJSON(text: string): unknown }).rawJSON(value.toString()) : value;
}

/** A response header as fetch's headers.get gives it: several fields joined by ", "; null when there is none. */
function headerOf(headers: Record<string, string | string[] | undefined>, name: string): string | null {
  const value = headers[name];
  if (value === undefined) return null;
  return Array.isArray(value) ? value.join(", ") : value;
}

function excerpt(text: string): string {
  return Array.from(text).slice(0, BODY_EXCERPT).join("");
}

/** The undici error behind a failed request (undici's fetch, which the tests use too, rejects with "fetch failed"
 * and the real error as its cause; undici's request rejects with the error itself). */
function causeOf(error: unknown): unknown {
  return error instanceof TypeError && error.cause !== undefined ? error.cause : error;
}

function codeOf(error: unknown): string | undefined {
  const code = error instanceof Error ? (error as { code?: unknown }).code : undefined;
  return typeof code === "string" ? code : undefined;
}

function textOf(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** How long after the caller's connect timeout undici gives up a connection attempt nobody waits for any more. */
export const CONNECT_BACKSTOP_MS = 1000;

/** Error codes and system calls of a failure to open a connection. */
const CONNECT_CODES = new Set(["ECONNREFUSED", "ENOTFOUND", "EAI_AGAIN", "EHOSTUNREACH", "ENETUNREACH", "UND_ERR_CONNECT_TIMEOUT"]);
const CONNECT_SYSCALLS = new Set(["connect", "getaddrinfo"]);

/** The JeffError for a request that got no answer, as jeff/remote.py maps httpx's exceptions. `started`: whether the
 * request reached a socket (PhaseTimers.started).
 * - the connect timer (PhaseTimeout "connect"), or a failure before the request reached a socket or with a connect
 *   error code: BackendUnavailable "Cannot reach ..." (httpx ConnectTimeout / ConnectError);
 * - the headers or body timer: DeadlineExceeded (httpx ReadTimeout);
 * - any other failure once the connection was open (a reset socket, an HTTP parser error HPE_*): BackendUnavailable
 *   "The connection ... failed during ..." (httpx's other TransportErrors, such as RemoteProtocolError). */
export function noAnswerError(error: unknown, started: boolean, url: string, method: string, path: string,
  timeoutMs: number): JeffError {
  const cause = causeOf(error);
  if (cause instanceof PhaseTimeout) {
    if (cause.phase === "connect") return new BackendUnavailable(`Cannot reach the Jeff server at ${url}: ${cause.message}`);
    return new DeadlineExceeded(`The Jeff server at ${url} did not answer ${method} ${path} within ${timeoutMs} ms; `
      + "the server may still have run the request");
  }
  const code = codeOf(cause);
  const syscall = cause instanceof Error ? (cause as { syscall?: unknown }).syscall : undefined;
  if (!started || (code !== undefined && CONNECT_CODES.has(code))
    || (typeof syscall === "string" && CONNECT_SYSCALLS.has(syscall))) {
    return new BackendUnavailable(`Cannot reach the Jeff server at ${url}: ${textOf(cause)}`);
  }
  const name = cause instanceof Error ? cause.name : typeof cause;
  return new BackendUnavailable(`The connection to the Jeff server at ${url} failed during ${method} ${path}: `
    + `${name}: ${textOf(cause)}`);
}

/** The undici dispatcher for a transport to `url`: an Agent, or a ProxyAgent when the environment names a proxy for
 * it (proxy.ts, httpProxyFor: as httpx with trust_env=True). Through a proxy, an http:// URL is sent to the proxy as a
 * forwarded request and an https:// URL through a CONNECT tunnel, as httpx does. undici's own headers and body
 * timeouts are off for every connection it makes (the server's and the proxy's): PhaseTimers times them. */
function proxiedAgent(url: string, timeoutMs: number): Dispatcher {
  // connect.timeout is only a backstop that cleans up an abandoned connection attempt: the caller has already been
  // rejected by the request's own connect timer (PhaseTimers) at timeoutMs, so it is not a second timing source for
  // the caller; when it fires, undici fails a request fetch has already given up, and nothing surfaces.
  const connect = { timeout: timeoutMs + CONNECT_BACKSTOP_MS };
  const proxy = httpProxyFor(url);
  if (proxy === null) return new Agent({ connect, headersTimeout: 0, bodyTimeout: 0 });
  return new ProxyAgent({
    uri: proxy, proxyTunnel: false, proxyTls: connect, requestTls: connect, headersTimeout: 0, bodyTimeout: 0,
    factory: (origin: string | URL, options: object) => new Pool(origin, { ...options, headersTimeout: 0, bodyTimeout: 0 }),
  });
}

/** The Transport over HTTP (Node's fetch). Every non-2xx answer is thrown as the JeffError its body names
 * (errorFromWire), with the response's x-request-id as the error's requestId (null if the response carried none).
 *
 *
 * Each transport owns one undici Agent (connections kept alive; a ProxyAgent when the environment names a proxy, see
 * proxiedAgent) with undici's own timeouts off (they run on a
 * one-second clock); each request gets precise timers instead (phase-timers.ts), timeoutMs for each phase as httpx's
 * Timeout(timeout_s) has it: connecting (httpx connect), the answer's headers once the request is on a socket and
 * each wait for more of the body (together httpx read). httpx's write and pool timeouts have no counterpart here.
 * close() destroys the agent, which aborts requests still in flight, as closing the httpx client closes its
 * connections, and stops their timers. */
export class RestTransport implements Transport {
  readonly url: string;
  private closed = false;
  private readonly agent: Dispatcher;
  private readonly inFlight = new Set<PhaseTimers>();
  /** Aborted by close(): stops an answer being decoded (decodedBody checks it before each layer). */
  private readonly closing = new AbortController();

  constructor(url: string, private readonly token: TokenSource, readonly timeoutMs: number) {
    if (!url.startsWith("http://") && !url.startsWith("https://")) {
      throw new InvalidRequest(`The server URL ${pyRepr(url)} must start with http:// or https://`);
    }
    if (!(token === null || typeof token === "string" || typeof token === "function")) {
      throw new InvalidRequest(`token must be a string, a function returning one, or null; got ${typeof token}`);
    }
    checkTimeoutMs(timeoutMs);
    this.url = url.replace(/\/+$/, "");
    this.agent = proxiedAgent(this.url, timeoutMs);
  }

  private closedError(): InvalidRequest {
    return new InvalidRequest(`This remote client of ${this.url} is closed; connect again with Jeff.remote`);
  }

  /** One request; the answer's JSON object, checked against `schema` when given. Every error thrown after the server
   * answered carries the response's x-request-id as requestId. */
  private async request(method: string, path: string, body: Record<string, unknown> | undefined,
    schema: SchemaName): Promise<Record<string, unknown>> {
    if (this.closed) throw this.closedError();
    const token = await readToken(this.token, this.url);
    const headers: Record<string, string> = { "accept-encoding": ACCEPT_ENCODING };
    if (token !== null) headers.authorization = `Bearer ${token}`;
    if (body !== undefined) headers["content-type"] = "application/json";
    const timers = new PhaseTimers(this.timeoutMs);
    this.inFlight.add(timers);
    let status: number;
    let raw: Buffer;
    let requestId: string | null;
    let contentEncoding: string | null;
    try {
      try {
        // undici's request, not fetch: fetch undoes Content-Encoding itself and shows only the last layer's output, so
        // the bytes as sent and each layer could not be held to the limit. A redirect is not followed (as httpx's
        // Client in jeff/remote.py), so a 3xx is a non-2xx answer like any other.
        const response = await undiciRequest(this.url + path, {
          method: method as Dispatcher.HttpMethod, headers, dispatcher: this.agent.compose(timers.interceptor()),
          signal: timers.signal, ...(body === undefined ? {} : { body: JSON.stringify(body, exactIntegers) }),
        });
        status = response.statusCode;
        requestId = headerOf(response.headers, REQUEST_ID_HEADER);
        contentEncoding = headerOf(response.headers, "content-encoding");
        raw = await rawBody(response.body);
      } catch (error) {
        if (this.closed) throw this.closedError();
        if (error instanceof BodyTooLarge) throw await this.tooLarge(method, path, error);
        throw noAnswerError(error, timers.started, this.url, method, path, this.timeoutMs);
      }
    } finally {
      timers.finish();
      this.inFlight.delete(timers);
    }
    let text: string;
    try {
      // UTF-8, a byte order mark dropped and bytes that do not decode replaced, as httpx's Response.text reads JSON
      text = new TextDecoder("utf-8").decode(await decodedBody(raw, contentEncoding, MAX_ANSWER_BODY, this.closing.signal));
    } catch (error) {
      // close() during decoding stops it between layers (the abort's reason is thrown): the closed error, as for a
      // request in flight
      if (this.closed) throw this.closedError();
      if (error instanceof BodyTooLarge) throw await this.tooLarge(method, path, error);
      if (error instanceof DecodingError) {
        // httpx decodes while reading, so this is jeff/remote.py's RequestError branch, before any answer
        throw new EngineError(`The answer of the Jeff server at ${this.url} to ${method} ${path} could not be read: `
          + `DecodingError: ${error.message}`);
      }
      throw error;
    }
    if (this.closed) throw this.closedError();
    try {
      return this.answer(method, path, status, text, schema);
    } catch (error) {
      if (error instanceof JeffError) error.requestId = requestId;
      throw error;
    }
  }

  private answer(method: string, path: string, status: number, text: string, schema: SchemaName): Record<string, unknown> {
    if (status < 200 || status > 299) throw this.error(method, path, status, text);
    let answer: unknown;
    try {
      answer = JSON.parse(text);
    } catch {
      throw new EngineError(`The Jeff server at ${this.url} answered ${method} ${path} with status ${status} and a body `
        + `that is not JSON: ${excerpt(text)}`);
    }
    if (answer === null || typeof answer !== "object" || Array.isArray(answer)) {
      throw new EngineError(`The Jeff server at ${this.url} answered ${method} ${path} with JSON that is not an object: `
        + excerpt(text));
    }
    if (!isValid(schema, answer)) {
      throw new ContractError(`The answer of ${this.url} to ${method} ${path} does not match contract/schemas/${schema}: `
        + firstProblem(schema, answer));
    }
    return answer as Record<string, unknown>;
  }

  /** The JeffError a non-2xx answer names; a decision it carries (LowConfidence) is checked against the decision
   * schema first, as the Python SDK builds it at once. */
  private error(method: string, path: string, status: number, text: string): JeffError {
    let body: unknown = null;
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
    const error = body !== null && typeof body === "object" && !Array.isArray(body)
      ? (body as Record<string, unknown>).error : undefined;
    if (error !== null && typeof error === "object" && !Array.isArray(error)) {
      const wire = error as Record<string, unknown>;
      if (typeof wire.type === "string" && typeof wire.message === "string") {
        const made = errorFromWire({ type: wire.type, message: wire.message, details: wire.details });
        if (Object.hasOwn(made.details, "decision") && !isValid("decision", made.details.decision)) {
          return new ContractError(`The ${made.type} error the Jeff server at ${this.url} answered ${method} ${path} with `
            + `carries a decision that does not match contract/schemas/decision: ${firstProblem("decision", made.details.decision)}`);
        }
        return made;
      }
    }
    return new EngineError(`The Jeff server at ${this.url} answered ${method} ${path} with status ${status} and a body that is `
      + `not a Jeff error: ${excerpt(text)}`);
  }

  private async decideAnswer(body: Record<string, unknown>, key: string): Promise<unknown> {
    const answer = await this.request("POST", "/v1/decide", body, "decide_response");
    if (!Object.hasOwn(answer, key)) {
      throw new ContractError(`The answer of ${this.url} to POST /v1/decide has ${pyRepr(pySorted(Object.keys(answer)))}, `
        + `not ${pyRepr(key)}`);
    }
    return answer[key];
  }

  async decideCalls(calls: WireCall[], policy: WirePolicy): Promise<DecisionJSON[]> {
    const decisions = await this.decideAnswer({ calls, on_low_confidence: policy }, "decisions") as DecisionJSON[];
    if (decisions.length !== calls.length) {
      throw new ContractError(`${this.url} answered ${decisions.length} decisions for ${calls.length} calls`);
    }
    return decisions;
  }

  async dispatch(body: Record<string, unknown>): Promise<DecisionJSON> {
    return await this.decideAnswer(body, "decision") as DecisionJSON;
  }

  async graph(body: Record<string, unknown>): Promise<GraphResultJSON> {
    return await this.decideAnswer(body, "graph_result") as GraphResultJSON;
  }

  async functions(): Promise<FunctionsResponse> {
    return await this.request("GET", "/v1/functions", undefined, "functions_response") as unknown as FunctionsResponse;
  }

  /** An answer body past MAX_ANSWER_BODY: the transport is closed (a server that sends one is not trusted with later
   * requests, as the channel ends on a message past its limit), and the error names the limit. */
  private async tooLarge(method: string, path: string, tooLarge: BodyTooLarge): Promise<BackendUnavailable> {
    await this.close();
    const what = tooLarge.decoded ? "a body that decodes (Content-Encoding) to more than" : "a body of more than";
    return new BackendUnavailable(`The Jeff server at ${this.url} answered ${method} ${path} with ${what} ${MAX_ANSWER_BODY} `
      + "bytes, the most this client reads; the client is closed, connect again with Jeff.remote");
  }

  /** Closes the connections; a request still in flight fails with the closed error. */
  async close(): Promise<void> {
    if (this.closed) return;
    this.closed = true;
    this.closing.abort();
    for (const timers of this.inFlight) timers.finish();
    await this.agent.destroy();
  }
}

function sameFormat(a: BaseFormat, b: BaseFormat): boolean {
  const keys = pySorted(Object.keys(a));
  return JSON.stringify(keys) === JSON.stringify(pySorted(Object.keys(b)))
    && keys.every((key) => (a as Record<string, unknown>)[key] === (b as Record<string, unknown>)[key]);
}

type ServedCalibrations = Map<string, [Calibration | null, Map<string, Calibration>]>;

/** The engine a RemoteJeff holds: the adapters the server serves and the base format its engines serve, read from
 * GET /v1/functions when it is built and again on installed(true). Each read checks that every served adapter the
 * local catalogue also has is the same version, and that the base format has not changed since the first. It never
 * predicts: RemoteJeff sends every decision to the server. */
export class ServedFunctions implements Engine {
  readonly baseFormat: BaseFormat;
  /** Function ID -> its bfloat16 calibration and its calibration_by_format entries, as the server lists them. */
  calibrations: ServedCalibrations = new Map();
  private served: Set<string>;

  private constructor(private readonly transport: Transport, private readonly catalogue: Catalogue,
    private readonly url: string, answer: FunctionsResponse) {
    this.baseFormat = { ...answer.base_format };
    this.served = this.readListing(answer);
  }

  static async read(transport: Transport, catalogue: Catalogue, url: string): Promise<ServedFunctions> {
    return new ServedFunctions(transport, catalogue, url, await transport.functions());
  }

  private readListing(answer: FunctionsResponse): Set<string> {
    if (!sameFormat(answer.base_format, this.baseFormat)) {
      throw new ContractError(`The server at ${this.url} now serves a base of format ${pyRepr(answer.base_format)}, not `
        + `${pyRepr(this.baseFormat)} as when this client connected; the functions bound here use that format's `
        + "calibration. Connect again");
    }
    const served = new Map<string, string>();
    const calibrations: ServedCalibrations = new Map();
    for (const listed of answer.functions) {
      const id = listed.id as string;
      served.set(id.slice(0, id.lastIndexOf(".")), listed.adapter_version as string);
      const block = listed.calibration as Record<string, unknown> | null;
      // calibration_by_format is optional in functions_response: a function listed without it has no INT4 entries
      const byFormat = listed.calibration_by_format === undefined
        ? {} : listed.calibration_by_format as Record<string, Record<string, unknown>>;
      calibrations.set(id, [block === null ? null : calibrationFromWire(block),
        new Map(Object.entries(byFormat).map(([formatId, entry]) => [formatId, calibrationFromWire(entry)]))]);
    }
    this.calibrations = calibrations;
    const local = new Map(this.catalogue.manifests().map((manifest) => [manifest.id, manifest.version]));
    for (const [adapterId, version] of served) {
      const mine = local.get(adapterId);
      if (mine !== undefined && mine !== version) throw versionMismatch(adapterId, version, mine);
    }
    return new Set(served.keys());
  }

  async installed(refresh = false): Promise<Set<string>> {
    if (refresh) this.served = this.readListing(await this.transport.functions());
    return new Set(this.served);
  }

  async predict(adapterId: string, rows: Row[]): Promise<EngineResult> {
    throw new EngineError(`RemoteJeff sends every decision to the server at ${this.url}; nothing may call its engine's predict `
      + `(asked for ${rows.length} row(s) of ${adapterId})`);
  }
}

/** A bound function of a RemoteJeff: bound here once (context, labels and policy checked), each call or many() one
 * request. */
export class RemoteBoundFunction extends BoundFunction {
  static override async create(jeff: Jeff, template: Call, policy: Policy): Promise<RemoteBoundFunction> {
    const bound = await jeff.bindCall(template);
    await checkPolicy(jeff, bound.function.interface, policy);
    return new RemoteBoundFunction(jeff, template, policy, bound);
  }

  override async many(inputs: readonly JSONValue[]): Promise<Decision[]> {
    const remote = this.jeff as RemoteJeff;
    return remote.decidePolicy(inputs.map((input) => [this.callOf(input), this.bound] as const), this.policy);
  }
}

/** Jeff over a server. Overrides every method that would reach an engine so each operation is one request. Close it
 * to stop using the connection. */
export class RemoteJeff extends Jeff {
  protected constructor(parts: JeffParts, readonly transport: Transport, readonly url: string) {
    super(parts);
  }

  static async connect(transport: Transport, catalogue: Catalogue, url: string): Promise<RemoteJeff> {
    const served = await ServedFunctions.read(transport, catalogue, url);
    return new RemoteJeff(await jeffParts(served, catalogue), transport, url);
  }

  override async close(): Promise<void> {
    await this.transport.close();
  }

  /** The calibration the server decides `fn` with, from its listing; NotInstalled for a function it does not list. */
  override resolveCalibrationOf(fn: FunctionSpec): Calibration | null {
    const listed = (this.engine as ServedFunctions).calibrations.get(fn.id);
    if (listed === undefined) throw notInstalled(fn.id, `the server at ${this.url} does not list it`);
    return resolveCalibration(fn.id, listed[0], listed[1], this.formatId);
  }

  /** serverPolicy with every function step named by the full ID the local catalogue resolves, so the server runs
   * exactly the steps this client checked. */
  wirePolicy(policy: Policy): WirePolicy | null {
    const wire = serverPolicy(policy);
    if (wire !== null && typeof wire === "object") {
      return { ...wire, escalate: wire.escalate.map((ref) => this.catalogue.function(ref).id) };
    }
    return wire;
  }

  /** One request for these calls; none for no calls (as local mode, which answers [] without the engine). */
  private async post(bound: ReadonlyArray<readonly [Call, Binding]>, policy: WirePolicy): Promise<Decision[]> {
    if (bound.length === 0) return [];
    checkBatch(bound.length, "calls");
    const answered = await this.transport.decideCalls(bound.map(([call, binding]) => callToWire(call, binding)), policy);
    return answered.map((item) => serverDecision(item, this.url));
  }

  override decideBound(bound: ReadonlyArray<readonly [Call, Binding]>): Promise<Decision[]> {
    return this.post(bound, "return");
  }

  /** Already-bound calls in one request: with the policy when the server can run it, else with "return" and the
   * policy then run here on each decision. */
  async decidePolicy(bound: ReadonlyArray<readonly [Call, Binding]>, policy: Policy): Promise<Decision[]> {
    const wire = this.wirePolicy(policy);
    if (wire !== null) return this.post(bound, wire);
    const decisions = await this.post(bound, "return");
    const out: Decision[] = [];
    for (const [index, [call]] of bound.entries()) out.push(await this.applyPolicy(call, decisions[index] as Decision, policy));
    return out;
  }

  override function(ref: string, options: CallOptions = {}): Promise<BoundFunction> {
    return RemoteBoundFunction.create(this, { function: ref, input: null, context: options.context, labels: options.labels },
      options.onLowConfidence === undefined ? "return" : options.onLowConfidence);
  }

  override async interfaceCall(iface: string, input: JSONValue, options: ShorthandOptions): Promise<Decision> {
    const policy = options.onLowConfidence === undefined ? "return" : options.onLowConfidence;
    if (options.function === undefined) {
      return this.dispatch(input, { interface: iface, context: options.context, labels: options.labels, onLowConfidence: policy });
    }
    const spec = this.catalogue.function(options.function);
    if (spec.interface !== iface) {
      throw new InterfaceMismatch(`${spec.id} implements ${spec.interface}, not ${iface}; call jeff.${spec.interface}`);
    }
    await checkPolicy(this, iface, policy);
    const call: Call = { function: options.function, input, context: options.context, labels: options.labels };
    return (await this.decidePolicy([[call, await this.bindCall(call)]], policy))[0] as Decision;
  }

  override async dispatch(input: JSONValue, options: DispatchOptions): Promise<Decision> {
    const policy = options.onLowConfidence === undefined ? "return" : options.onLowConfidence;
    await checkPolicy(this, options.interface, policy);
    const routerId = await startRouter(this, options.router === undefined ? DEFAULT_ROUTER : options.router);
    const wire = this.wirePolicy(policy);
    const request: Record<string, unknown> = { input, interface: options.interface, router: routerId };
    if (options.context !== null && options.context !== undefined) request.context = { ...options.context };
    if (options.labels !== null && options.labels !== undefined) request.labels = labelsToWire(options.labels);
    const decision = serverDecision(await this.transport.dispatch(
      { dispatch: request, on_low_confidence: wire === null ? "return" : wire }), this.url);
    if (wire !== null || !decision.abstained) return decision;
    // The policy has a step only this process can run. As in local mode, a router that abstains raises; an abstained
    // leaf is escalated with the call dispatch made to it.
    const fn = this.catalogue.function(decision.function);
    if (this.catalogue.manifestOf(fn).isRouter) throw lowConfidence(decision);
    return this.applyPolicy(leafCall(fn, input, options.context, options.labels), decision, policy);
  }

  /** A graph with no code nodes, whose node policies the server can run, is one request carrying the graph inline
   * (each node's function by the ID the local catalogue resolves); any other graph runs here stage by stage.
   * Everything local mode checks before the first decision is checked here first, without a request. */
  override async runGraph(graph: Graph | string | Record<string, unknown> | ReadonlyMap<string, unknown>, input: JSONValue,
    params: Record<string, JSONValue>, code: Record<string, CodeFunction> = {},
    policies: Record<string, Policy> = {}): Promise<GraphResult> {
    const loaded = RemoteJeff.graphOf(graph);
    const model = await checkGraph(this, loaded, input, params, code, policies);
    const wire = Object.fromEntries(Object.entries(policies).map(([name, policy]) => [name, this.wirePolicy(policy)]));
    const hasCode = [...loaded.nodes.values()].some((node) => node.code !== null);
    if (hasCode || Object.values(wire).some((item) => item === null)) return runChecked(this, loaded, input, params, code, model);
    const inline = graphToDict(loaded) as { nodes: Record<string, Record<string, unknown>> };
    for (const [name, [, binding]] of model) (inline.nodes[name] as Record<string, unknown>).call = binding.function.id;
    const body: Record<string, unknown> = { graph: { inline }, input, params: { ...params } };
    if (Object.keys(wire).length > 0) body.on_low_confidence_by_node = wire;
    return serverGraphResult(await this.transport.graph(body), this.url);
  }

  /** A graph registered on the server, by id, in one request; every node policy must be one the server can run. */
  async runServerGraph(graphId: string, input: JSONValue, params: Record<string, JSONValue>,
    policies: Record<string, Policy> = {}): Promise<GraphResult> {
    const byNode: Record<string, WirePolicy> = {};
    for (const [name, policy] of Object.entries(policies)) {
      const wire = this.wirePolicy(policy);
      if (wire === null) {
        throw new InvalidPolicy(`Node ${name}: a graph registered on the server runs there, and the server cannot call llm, `
          + "human or remote escalation steps; use 'return', 'error' or escalate([functionStep(...), ...]) for this node");
      }
      byNode[name] = wire;
    }
    const body: Record<string, unknown> = { graph: { id: graphId }, input, params: { ...params } };
    if (Object.keys(byNode).length > 0) body.on_low_confidence_by_node = byNode;
    return serverGraphResult(await this.transport.graph(body), this.url);
  }
}

export interface RemoteOptions {
  /** How long each phase of a request may take, in milliseconds: connecting, the answer's headers, and each wait for
   * more of the answer's body. The headers phase also covers sending the request body (httpx, in the Python SDK, times
   * writing separately). */
  timeoutMs: number;
  /** The manifests calls are bound with; the catalogue shipped with the package when not given. */
  catalogue?: Catalogue;
  /** Use the server's WebSocket channel (lower latency; bound functions become handles on the server) instead of one
   * HTTP request per operation (channel.ts). timeoutMs then bounds opening the channel, its hello, each request's
   * wait for its reply and the closing handshake. */
  channel?: boolean;
}

/** Jeff.remote: connect to the server at `url` (reads GET /v1/functions once). */
export async function remote(url: string, token: TokenSource, options: RemoteOptions): Promise<RemoteJeff> {
  checkTimeoutMs(options.timeoutMs);
  const catalogue = options.catalogue === undefined ? Catalogue.shipped() : options.catalogue;
  if (options.channel === true) {
    // loaded here: channel.ts extends this module's classes
    const { connectChannel } = await import("./channel.js");
    return connectChannel(url, token, options.timeoutMs, catalogue);
  }
  const transport = new RestTransport(url, token, options.timeoutMs);
  try {
    return await RemoteJeff.connect(transport, catalogue, transport.url);
  } catch (error) {
    await transport.close();
    throw error;
  }
}
