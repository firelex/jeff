/** Remote mode over the server's low-latency channel (contract/socket.md, "The channel"; port of
 * jeff/channel_client.py): Jeff.remote(url, token, {timeoutMs, channel: true}).
 *
 * One WebSocket to <url>/v1/channel (ws:// for http://, wss:// for https://), compression off. Connecting sends hello
 * and, when there is a token, auth; then every request is one frame carrying an id, and each reply is handed to the
 * request waiting for that id. Each WebSocket binary message is one frame of protocol.ts's format.
 *
 * A bound function (ChannelBoundFunction) is a handle on the server: bound once with `bind` (its function, context,
 * labels and policy), each call then a `call` or `call_many` frame carrying only the input. A bind answered with
 * another version of the function's adapter than the local catalogue has throws the ContractError connecting gives.
 * decide, dispatch and graphs use the `decide`, `dispatch` and `graph` frames. The list of served functions is read
 * over HTTP, GET /v1/functions, as without the channel: the channel has no operation for it.
 *
 * Token: a token function is called for the token on the WebSocket handshake (as Authorization: Bearer, for a proxy
 * in front of the server), for the token sent with auth, in each refresh window, and for each GET /v1/functions; not
 * before each request. When the token is a JWT, its exp is read from its payload without verifying the signature. From
 * REFRESH_BEFORE_S seconds before exp, the function is called and `refresh` sent with the token it returns once that
 * token expires later than the channel's current one; while it does not (or the function throws, or the server
 * refuses the refresh), the function is called again every REFRESH_RETRY_S seconds until exp. Each failure is kept: if
 * exp passes without a fresh token, the channel closes with Unauthorised carrying every failure as a note. A string
 * token, or a token that is not a JWT, is never refreshed. The refresh window is computed with this machine's clock.
 *
 * Proxies: the WebSocket goes through the proxy the environment names for it, chosen as the Python SDK's websockets
 * (proxy=True) chooses it (proxy.ts, channelProxyFor), in a CONNECT tunnel; GET /v1/functions goes through the proxy
 * httpx would choose (RestTransport).
 *
 * Timing: timeoutMs bounds opening the WebSocket (through the proxy's tunnel, if any), the hello, each request's wait
 * for its reply, and the closing handshake, each with its own timer. A request's timer starts once its whole message
 * has been accepted by the socket, as the Python SDK's starts once its send returns: a large message on a slow but
 * working link takes as long as it takes to send.
 *
 * Keepalive: as the Python SDK's websockets client (ping_interval=20, ping_timeout=20), a ping is sent every
 * PING_INTERVAL_MS once the channel is open and the last ping was answered; when its pong has not arrived
 * PING_TIMEOUT_MS after the ping was written, the channel ends with BackendUnavailable naming the missing pong, which
 * every waiting request fails with. A half-open connection (a NAT or load balancer that dropped it, a hung server) is
 * so found within 40 s even while no request is outstanding. Its timers do not keep the process alive.
 *
 * Sending: messages are sent one at a time, in the order of the requests, each in pieces of at most SEND_CHUNK bytes
 * (WebSocket fragments of one message, the next piece handed to the socket once it has accepted the last). As in the
 * Python SDK, timeoutMs bounds each stall, not the whole upload: when a message is being sent and the socket has
 * accepted no piece of it for timeoutMs (the server, or the network, stopped taking data), the socket is destroyed and
 * the channel ends with BackendUnavailable naming the stall, which every waiting request fails with.
 *
 * An error for a failure the server sent carries no request id (there is no HTTP response) but the server
 * connection's id as details.channel_id.
 *
 * When the channel closes (a failure frame with id 0, the socket closing, or close()), every request waiting for a
 * reply fails with that error (BackendUnavailable "the channel closed" when the socket closed without one), and every
 * later request throws the same error. There is no automatic reconnect: connect again with Jeff.remote. The socket
 * does not keep the process alive on its own (as the Python SDK's daemon threads): a request does, through the
 * send-stall watch's timer while messages are being sent and its own timer while it waits for its reply. */

import type { IncomingMessage } from "node:http";
import { type ClientOptions, WebSocket } from "ws";
import type { Binding, Call } from "./binding.js";
import { Catalogue } from "./catalogue.js";
import { BoundFunction, type CallOptions, type Jeff, MAX_TIMEOUT_MS, jeffParts } from "./client.js";
import { firstProblem, isValid } from "./contract.js";
import { Decision, type DecisionJSON } from "./decision.js";
import {
  BackendUnavailable, ContractError, DeadlineExceeded, EngineError, InvalidRequest, JeffError, Unauthorised, copyError,
  errorFromWire,
} from "./errors.js";
import { type Policy, checkPolicy } from "./escalation.js";
import {
  CHANNEL_PATH, HEADER_SIZE, MAX_BODY, MAX_FRAME, PREAUTH_MAX_BODY, checkHello, decodeBody, encodeFrame, hello,
  validateMessage,
} from "./protocol.js";
import { channelProxyFor, tunnelledConnection } from "./proxy.js";
import { pyRepr, pySorted } from "./pyformat.js";
import {
  type FunctionsResponse, type GraphResultJSON, RemoteBoundFunction, RemoteJeff, RestTransport, ServedFunctions,
  type TokenSource, type Transport, type WireCall, type WirePolicy, checkBatch, labelsToWire, readToken, versionMismatch,
} from "./remote.js";
import type { JSONValue } from "./types.js";

/** How long before a token's exp the refresher starts asking the token function for a fresh token, and how often it
 * asks again while the token it returns does not expire later (or the function or the refresh fails). */
export const REFRESH_BEFORE_S = 60;
export const REFRESH_RETRY_S = 5;
/** The largest piece of a message handed to the socket at once (the Python SDK's SEND_CHUNK): each piece the socket
 * accepts counts as progress for the send-stall watch. */
export const SEND_CHUNK = 64 * 1024;
/** The keepalive (see "Keepalive" above): a ping every 20 s, its pong due within 20 s of the ping being written. */
export const PING_INTERVAL_MS = 20_000;
export const PING_TIMEOUT_MS = 20_000;

let pinnedKeepalive: { intervalMs: number; timeoutMs: number } | null = null;

/** For this package's own tests only (not exported from the package's entry point): the ping interval and pong
 * timeout of channels opened from now on; null restores PING_INTERVAL_MS and PING_TIMEOUT_MS. */
export function pinChannelKeepaliveForTests(keepalive: { intervalMs: number; timeoutMs: number } | null): void {
  pinnedKeepalive = keepalive === null ? null : { ...keepalive };
}

/** WebSocket close code 1006: the connection ended without a close frame (ws reports it; none is ever sent). */
const NO_CLOSE_FRAME = 1006;

/** The channel's WebSocket URL for a server URL: ws:// for http://, wss:// for https://, path /v1/channel. */
export function channelUrl(url: string): string {
  for (const [http, ws] of [["http://", "ws://"], ["https://", "wss://"]] as const) {
    if (url.startsWith(http)) return ws + url.slice(http.length).replace(/\/+$/, "") + CHANNEL_PATH;
  }
  throw new InvalidRequest(`The server URL ${pyRepr(url)} must start with http:// or https://`);
}

/** The exp of a JWT (header.payload.signature), read from its payload without verifying the signature; null for a
 * token that is not a JWT (an API key, a console session token). A JWT whose payload cannot be read or has no numeric
 * exp is Unauthorised: the server would refuse it too. */
export function tokenExpiry(token: string): number | null {
  const parts = token.split(".");
  if (parts.length !== 3) return null;
  let payload: unknown;
  try {
    payload = JSON.parse(Buffer.from(parts[1] as string, "base64url").toString("utf8"));
  } catch {
    throw new Unauthorised("The token is shaped like a JWT, but its payload is not base64-encoded JSON");
  }
  const fields = payload !== null && typeof payload === "object" && !Array.isArray(payload)
    ? payload as Record<string, unknown> : null;
  const expires = fields !== null && Object.hasOwn(fields, "exp") ? fields.exp : null;
  if (typeof expires !== "number") {
    throw new Unauthorised(`The token is a JWT without a numeric exp (got ${pyRepr(expires)}); the server needs one`);
  }
  return expires;
}

/** "Name: message" of anything thrown. */
function described(error: unknown): string {
  return error instanceof Error ? `${error.name}: ${error.message}` : String(error);
}

/** errorFromWire, with the same check RestTransport.error (remote.ts) makes for REST: a decision the error carries
 * (LowConfidence) must match contract/schemas/decision before the error is built, so a malformed decision from the
 * channel is a clear ContractError naming the problem, not a broken LowConfidence whose .decision getter fails or
 * misleads later. */
function wireError(wire: { type: unknown; message: unknown; details?: unknown }, url: string): JeffError {
  const made = errorFromWire(wire);
  if (Object.hasOwn(made.details, "decision") && !isValid("decision", made.details.decision)) {
    return new ContractError(`The ${made.type} error the channel ${url} sent carries a decision that does not match `
      + `contract/schemas/decision: ${firstProblem("decision", made.details.decision)}`);
  }
  return made;
}

function closing(code: number, reason: Buffer): string {
  if (code === NO_CLOSE_FRAME) return "no close frame received";
  return `WebSocket close code ${code}${reason.length > 0 ? `: ${reason.toString()}` : ""}`;
}

/** The current token of a channel that has one (the handshake header, auth, and the refresher). */
async function currentToken(source: TokenSource, url: string): Promise<string> {
  const token = await readToken(source, url);
  if (token === null) throw new Error("currentToken is called only for a channel with a token");
  return token;
}

interface Waiter<T> { resolve(value: T): void; reject(error: JeffError): void }
/** A request waiting for its reply; its timer runs from when its message has been sent. */
interface Pending extends Waiter<Record<string, unknown>> { timer: NodeJS.Timeout | null }
/** A message waiting to be sent: `sent` once the socket accepted all of it, `failed` if it cannot be sent. */
interface Outgoing { frame: Buffer; sent(): void; failed(error: JeffError): void }

/** The Transport (remote.ts) over the channel, plus the handle operations bind, call, callMany and release. Requests
 * are multiplexed by id on one WebSocket. GET /v1/functions goes over HTTP (a RestTransport with the same URL, token
 * and timeout). */
export class ChannelTransport implements Transport {
  readonly url: string;
  private readonly pending = new Map<number, Pending>();
  /** Ids of releases sent for garbage-collected bound functions (no one waits for their replies). */
  private readonly unawaited = new Set<number>();
  /** Ids of requests whose caller stopped waiting (DeadlineExceeded): their late replies are dropped. */
  private readonly abandoned = new Set<number>();
  private readonly releases: number[] = [];
  private nextId = 1;
  private closedError: JeffError | null = null;
  private refreshFailures: string[] = [];
  /** Waiting for the WebSocket to open, then for the server's hello. */
  private opening: Waiter<void> | null = null;
  private greeting: Waiter<Record<string, unknown>> | null = null;
  /** Wakes the refresher from its wait when the channel closes. */
  private wake: (() => void) | null = null;
  private closeTimerArmed = false;
  private readonly wsClosed: Promise<void>;
  /** Messages waiting to be sent, after the one being sent (`sending`). */
  private readonly outbox: Outgoing[] = [];
  private sending = false;
  /** performance.now() when the message being sent was started or last had a piece accepted. */
  private progressAt = 0;
  private stallTimer: NodeJS.Timeout | null = null;
  /** The keepalive's timers (one runs at a time: waiting to send the next ping, or waiting for its pong), the number
   * of the last ping sent (its payload, 8 bytes big-endian), and the timings fixed when the channel was made. */
  private pingTimer: NodeJS.Timeout | null = null;
  private pongTimer: NodeJS.Timeout | null = null;
  private pingNumber = 0n;
  /** The number of the last ping answered. */
  private pongNumber = 0n;
  private readonly keepalive = pinnedKeepalive ?? { intervalMs: PING_INTERVAL_MS, timeoutMs: PING_TIMEOUT_MS };

  private constructor(private readonly rest: RestTransport, private readonly ws: WebSocket, readonly wsUrl: string,
    private readonly token: TokenSource, readonly timeoutMs: number) {
    this.url = rest.url;
    this.wsClosed = new Promise((resolve) => ws.once("close", () => resolve()));
    ws.on("upgrade", (response: IncomingMessage) => response.socket.unref());
    ws.on("open", () => {
      this.settleOpening(null);
      this.schedulePing();
    });
    ws.on("pong", (data: Buffer) => this.onPong(data));
    ws.on("unexpected-response", (_request, response: IncomingMessage) => {
      this.settleOpening(new EngineError(`The Jeff server at ${this.url} refused to open the channel ${wsUrl}: HTTP status `
        + `${response.statusCode}`));
      ws.terminate();
    });
    ws.on("error", (error: Error) => {
      if (this.opening !== null) {
        this.settleOpening(new BackendUnavailable(`Cannot reach the Jeff server at ${this.url} (${wsUrl}): ${described(error)}`));
      } else this.shutdown(new BackendUnavailable(`${wsUrl}: the channel closed (${described(error)})`));
    });
    ws.on("close", (code: number, reason: Buffer) => {
      const how = closing(code, reason);
      this.settleOpening(new BackendUnavailable(`Cannot reach the Jeff server at ${this.url} (${wsUrl}): the connection `
        + `closed before the channel opened (${how})`));
      if (this.greeting !== null) this.greeting.reject(new BackendUnavailable(`${wsUrl}: the channel closed during the hello (${how})`));
      this.shutdown(new BackendUnavailable(`${wsUrl}: the channel closed (${how})`));
    });
    ws.on("message", (data: Buffer, binary: boolean) => {
      if (this.greeting !== null) {
        const greeting = this.greeting;
        try {
          greeting.resolve(this.message(data, binary));
        } catch (error) {
          greeting.reject(this.asJeffError(error));
        }
      } else this.onMessage(data, binary);
    });
  }

  /** Opens the channel: the WebSocket, hello, and auth when there is a token; a function's JWT is then refreshed. */
  static async open(url: string, token: TokenSource, timeoutMs: number): Promise<ChannelTransport> {
    const rest = new RestTransport(url, token, timeoutMs); // also checks the URL, the token's type and the timeout
    let transport: ChannelTransport;
    try {
      const wsUrl = channelUrl(rest.url);
      // The token also goes on the handshake as Authorization: Bearer. The Jeff server ignores it there (it checks the
      // auth message sent after hello), but a proxy in front of the server may open the WebSocket only for a request
      // that carries credentials.
      const headers: Record<string, string> = token === null ? {} : { authorization: `Bearer ${await currentToken(token, rest.url)}` };
      const proxy = channelProxyFor(wsUrl);
      let ws: WebSocket;
      try {
        // ws's own handshakeTimeout is not used: it is a socket idle timer, and opening is timed below as a whole.
        ws = new WebSocket(wsUrl, {
          headers, perMessageDeflate: false, maxPayload: HEADER_SIZE + MAX_FRAME,
          // Node's ClientRequest takes the socket the function hands its callback (the tunnel's, once it is open).
          ...(proxy === null ? {} : {
            createConnection: tunnelledConnection(proxy, wsUrl.startsWith("wss://"), timeoutMs) as unknown as
              ClientOptions["createConnection"],
          }),
        });
      } catch (error) {
        throw new BackendUnavailable(`Cannot reach the Jeff server at ${rest.url} (${wsUrl}): ${described(error)}`);
      }
      transport = new ChannelTransport(rest, ws, wsUrl, token, timeoutMs);
    } catch (error) {
      await rest.close();
      throw error;
    }
    try {
      await transport.connect();
      await transport.handshake();
      const expires = await transport.authenticate();
      if (expires !== null) void transport.refreshLoop(expires);
    } catch (error) {
      await transport.close();
      throw error;
    }
    return transport;
  }

  // ---- opening

  private settleOpening(error: JeffError | null): void {
    const opening = this.opening;
    if (opening === null) return;
    this.opening = null;
    if (error === null) opening.resolve();
    else opening.reject(error);
  }

  /** Waits for the WebSocket to open, at most timeoutMs for the whole of it (TCP, TLS and the upgrade). */
  private connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.settleOpening(new BackendUnavailable(`Cannot reach the Jeff server at ${this.url}: opening ${this.wsUrl} took `
          + `more than ${this.timeoutMs} ms`));
        this.ws.terminate();
      }, this.timeoutMs);
      this.opening = {
        resolve: () => { clearTimeout(timer); resolve(); },
        reject: (error) => { clearTimeout(timer); reject(error); },
      };
    });
  }

  /** Sends hello and reads the server's (at most timeoutMs). */
  private async handshake(): Promise<void> {
    const reply = new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.greeting = null;
        reject(new BackendUnavailable(`No hello from the channel ${this.wsUrl} within ${this.timeoutMs} ms`));
      }, this.timeoutMs);
      this.greeting = {
        resolve: (message) => { clearTimeout(timer); this.greeting = null; resolve(message); },
        reject: (error) => { clearTimeout(timer); this.greeting = null; reject(error); },
      };
    });
    this.ws.send(encodeFrame(hello(), "hello"));
    const message = await reply;
    if (message.ok === false) throw wireError(message.error as { type: unknown; message: unknown; details?: unknown }, this.wsUrl);
    checkHello(message);
    validateMessage(message, `the hello of ${this.wsUrl}`, "hello");
  }

  /** Sends auth when there is a token; the token's exp when it is a function's JWT (to refresh), else null. */
  private async authenticate(): Promise<number | null> {
    if (this.token === null) return null;
    const token = await currentToken(this.token, this.url);
    await this.request({ op: "auth", token }, PREAUTH_MAX_BODY);
    return typeof this.token === "function" ? tokenExpiry(token) : null;
  }

  // ---- keepalive

  /** Sends the next ping keepalive.intervalMs from now. */
  private schedulePing(): void {
    if (this.closedError !== null) return;
    this.pingTimer = setTimeout(() => this.sendPing(), this.keepalive.intervalMs);
    this.pingTimer.unref();
  }

  /** Sends a numbered ping; once it is written, its pong is due within keepalive.timeoutMs. */
  private sendPing(): void {
    this.pingTimer = null;
    if (this.closedError !== null || this.ws.readyState !== WebSocket.OPEN) return;
    this.pingNumber += 1n;
    const number = this.pingNumber;
    const payload = Buffer.alloc(8);
    payload.writeBigUInt64BE(number);
    this.ws.ping(payload, undefined, (error) => {
      if (error !== undefined && error !== null) {
        this.shutdown(new BackendUnavailable(`${this.wsUrl}: the channel closed (${described(error)})`));
        return;
      }
      if (this.closedError !== null || this.pingNumber !== number) return;
      if (this.pongNumber >= number) {
        this.schedulePing(); // answered before the write was reported
        return;
      }
      this.pongTimer = setTimeout(() => {
        this.pongTimer = null;
        this.shutdown(new BackendUnavailable(`${this.wsUrl}: the server sent no pong within ${this.keepalive.timeoutMs / 1000} s `
          + "of a keepalive ping; the channel closed"));
      }, this.keepalive.timeoutMs);
      this.pongTimer.unref();
    });
  }

  /** A pong answering the last ping starts the wait for the next one; a pong answering no ping of this side is
   * unsolicited and ignored, as websockets ignores it. */
  private onPong(data: Buffer): void {
    if (data.length !== 8) return;
    const number = data.readBigUInt64BE(0);
    if (number > this.pingNumber || number <= this.pongNumber) return; // no ping of this side, or answered already
    this.pongNumber = number;
    if (number < this.pingNumber || this.pongTimer === null) return; // an earlier ping, or the write not yet reported
    clearTimeout(this.pongTimer);
    this.pongTimer = null;
    this.schedulePing();
  }

  private stopKeepalive(): void {
    if (this.pingTimer !== null) clearTimeout(this.pingTimer);
    if (this.pongTimer !== null) clearTimeout(this.pongTimer);
    this.pingTimer = null;
    this.pongTimer = null;
  }

  // ---- reading

  /** The checked message one WebSocket message carries (exactly one frame). */
  private message(data: Buffer, binary: boolean): Record<string, unknown> {
    if (!binary) throw new EngineError(`The channel ${this.wsUrl} sent a text message; it carries binary frames only`);
    if (data.length < HEADER_SIZE) {
      throw new EngineError(`The channel ${this.wsUrl} sent a message of ${data.length} bytes, shorter than a frame header`);
    }
    const length = data.readUInt32BE(0);
    if (length !== data.length - HEADER_SIZE) {
      throw new EngineError(`The channel ${this.wsUrl} sent a message that is not exactly one frame: its header announces `
        + `${length} bytes and the message carries ${data.length - HEADER_SIZE}`);
    }
    try {
      return decodeBody(data.subarray(HEADER_SIZE));
    } catch (error) {
      if (error instanceof InvalidRequest) {
        throw new EngineError(`The channel ${this.wsUrl} sent a frame that does not match the socket schema: ${error.message}`);
      }
      throw error;
    }
  }

  /** A JeffError as it is; anything else that went wrong reading the channel as an EngineError naming it. */
  private asJeffError(error: unknown): JeffError {
    return error instanceof JeffError ? error : new EngineError(`The channel ${this.wsUrl} failed: ${described(error)}`);
  }

  private onMessage(data: Buffer, binary: boolean): void {
    let message: Record<string, unknown>;
    try {
      message = this.message(data, binary);
    } catch (error) {
      this.shutdown(this.asJeffError(error));
      return;
    }
    if (!Object.hasOwn(message, "ok")) {
      this.shutdown(new EngineError(`The channel ${this.wsUrl} sent a message that is not a reply (op ${pyRepr(message.op)}); `
        + "the channel is closed"));
      return;
    }
    const id = message.id as number;
    if (id === 0 && message.ok === false) {
      // A failure of the whole connection (the token expired, a refresh for another caller, a frame the server could
      // not read): the server closes the channel after it.
      this.shutdown(wireError(message.error as { type: unknown; message: unknown; details?: unknown }, this.wsUrl));
      return;
    }
    const waiting = this.pending.get(id);
    if (waiting !== undefined) {
      this.pending.delete(id);
      if (waiting.timer !== null) clearTimeout(waiting.timer);
      waiting.resolve(message);
    } else if (this.unawaited.delete(id)) {
      if (message.ok !== true) {
        const error = message.error as Record<string, unknown>;
        console.error(`Releasing the handle of a garbage-collected bound function on ${this.wsUrl} failed: ${String(error.type)}: `
          + String(error.message));
      }
    } else if (!this.abandoned.delete(id)) {
      // A reply to an id this side never sent, or one already answered: the two sides disagree about what is
      // outstanding, so no later reply can be trusted to be matched right.
      this.shutdown(new EngineError(`The channel ${this.wsUrl} sent a reply for id ${id}, which no request is waiting for; `
        + "the channel is closed"));
    }
    // An abandoned id is a late reply to a request whose caller got DeadlineExceeded: dropped.
  }

  /** Ends the channel once: the first error wins; every waiting request fails with its own copy of it. */
  private shutdown(error: JeffError): void {
    if (this.closedError === null) {
      error.notes.push(...this.refreshFailures);
      this.closedError = error;
    }
    const closed = this.closedError;
    if (this.greeting !== null) this.greeting.reject(copyError(closed));
    for (const waiting of this.pending.values()) {
      if (waiting.timer !== null) clearTimeout(waiting.timer);
      waiting.reject(copyError(closed));
    }
    this.pending.clear();
    for (const outgoing of this.outbox.splice(0)) outgoing.failed(copyError(closed));
    this.stopStallWatch();
    this.stopKeepalive();
    this.unawaited.clear();
    this.abandoned.clear();
    if (this.wake !== null) this.wake();
    if (this.ws.readyState === WebSocket.CONNECTING) this.ws.terminate();
    else if (this.ws.readyState === WebSocket.OPEN || this.ws.readyState === WebSocket.CLOSING) {
      // CLOSING: the server started the closing handshake; it gets the same bound.
      if (this.ws.readyState === WebSocket.OPEN) this.ws.close(1000);
      this.endSocketLater();
    }
  }

  /** Ends the socket timeoutMs from now unless the closing handshake has finished (ws's own close timer is 30 s). */
  private endSocketLater(): void {
    if (this.closeTimerArmed) return;
    this.closeTimerArmed = true;
    const timer = setTimeout(() => this.ws.terminate(), this.timeoutMs);
    void this.wsClosed.then(() => clearTimeout(timer));
  }

  // ---- requests

  private register(message: Record<string, unknown>, limit: number): [number, Buffer] {
    const id = this.nextId++;
    const frame = encodeFrame({ ...message, id });
    if (frame.length - HEADER_SIZE > limit) {
      throw new InvalidRequest(`A ${pyRepr(message.op)} frame of ${frame.length - HEADER_SIZE} bytes is over the channel's `
        + `limit of ${limit} bytes${limit === PREAUTH_MAX_BODY ? " before auth" : " (50 MiB)"}`);
    }
    return [id, frame];
  }

  /** Queues a registered request's frame for sending (see "Sending" above): `sent` is called once the socket has
   * accepted all of it; `failed` gets the channel's error (BackendUnavailable "the channel closed" when none is
   * recorded yet) if the WebSocket cannot send it. */
  private send(frame: Buffer, sent: () => void, failed: (error: JeffError) => void): void {
    this.outbox.push({ frame, sent, failed });
    if (!this.sending) this.sendNext();
  }

  private sendNext(): void {
    const outgoing = this.outbox.shift();
    if (outgoing === undefined) {
      this.sending = false;
      this.stopStallWatch();
      return;
    }
    this.sending = true;
    this.progressAt = performance.now();
    this.watchStall();
    this.sendPiece(outgoing, 0);
  }

  /** Hands the socket the piece of `outgoing` from `offset`; the next once it is accepted. */
  private sendPiece(outgoing: Outgoing, offset: number): void {
    const end = Math.min(offset + SEND_CHUNK, outgoing.frame.length);
    const fin = end === outgoing.frame.length;
    this.ws.send(outgoing.frame.subarray(offset, end), { binary: true, fin }, (error) => {
      if (error !== undefined && error !== null) {
        const failure = this.closedError !== null ? copyError(this.closedError)
          : new BackendUnavailable(`${this.wsUrl}: the channel closed (${described(error)})`);
        // The socket is gone: nothing queued behind this message can be sent either.
        for (const item of [outgoing, ...this.outbox.splice(0)]) item.failed(copyError(failure));
        this.sending = false;
        this.stopStallWatch();
        return;
      }
      this.progressAt = performance.now();
      if (!fin) {
        this.sendPiece(outgoing, end);
        return;
      }
      outgoing.sent();
      this.sendNext();
    });
  }

  /** Ends the channel when the message being sent has had no piece accepted for timeoutMs. */
  private watchStall(): void {
    if (this.stallTimer !== null) return;
    const check = () => {
      this.stallTimer = null;
      if (!this.sending || this.closedError !== null) return;
      const idle = performance.now() - this.progressAt;
      if (idle < this.timeoutMs) {
        this.stallTimer = setTimeout(check, Math.max(1, Math.ceil(this.timeoutMs - idle)));
        return;
      }
      const error = new BackendUnavailable(`${this.wsUrl}: the socket accepted no bytes of a frame for ${this.timeoutMs} ms `
        + "(the server or the network stopped taking data); the channel closed");
      // The socket first: a close frame would only queue behind the stalled message.
      this.ws.terminate();
      this.shutdown(error);
    };
    this.stallTimer = setTimeout(check, this.timeoutMs);
  }

  private stopStallWatch(): void {
    if (this.stallTimer !== null) clearTimeout(this.stallTimer);
    this.stallTimer = null;
  }

  /** Sends `release` for each handle queued by releaseLater, without waiting for the replies (a failure is logged). */
  private sendReleases(): void {
    for (let handle = this.releases.shift(); handle !== undefined; handle = this.releases.shift()) {
      if (this.closedError !== null) return; // the handles ended with the connection
      const [id, frame] = this.register({ op: "release", handle }, MAX_BODY);
      this.unawaited.add(id);
      this.send(frame, () => {}, () => this.unawaited.delete(id));
    }
  }

  /** Sends one request and waits for its reply, at most timeoutMs from when its message has been sent (sending is
   * bounded by the send-stall watch); its result, or the error its failure names. */
  private async request(message: Record<string, unknown>, limit = MAX_BODY): Promise<Record<string, unknown>> {
    this.sendReleases();
    const [id, frame] = this.register(message, limit);
    if (this.closedError !== null) throw copyError(this.closedError);
    const reply = await new Promise<Record<string, unknown>>((resolve, reject) => {
      const waiting: Pending = { resolve, reject, timer: null };
      this.pending.set(id, waiting);
      this.send(frame, () => {
        if (this.pending.get(id) !== waiting) return; // answered, or failed with the channel, already
        waiting.timer = setTimeout(() => {
          if (this.pending.delete(id)) this.abandoned.add(id);
          reject(new DeadlineExceeded(`The channel ${this.wsUrl} did not answer ${pyRepr(message.op)} within ${this.timeoutMs} `
            + "ms; the server may still have run the request"));
        }, this.timeoutMs);
      }, (error) => {
        if (this.pending.get(id) === waiting) {
          this.pending.delete(id);
          reject(error);
        }
      });
    });
    if (reply.ok !== true) throw wireError(reply.error as { type: unknown; message: unknown; details?: unknown }, this.wsUrl);
    return reply.result as Record<string, unknown>;
  }

  /** A request answered as /v1/decide answers: its result checked against decide_response, holding `key`. */
  private async decideAnswer(message: Record<string, unknown>, key: string): Promise<unknown> {
    const result = await this.request(message);
    const valid = isValid("decide_response", result);
    if (!valid || !Object.hasOwn(result, key)) {
      const problem = valid ? pyRepr(pySorted(Object.keys(result))) : firstProblem("decide_response", result);
      throw new ContractError(`The answer of ${this.wsUrl} to ${pyRepr(message.op)} does not match contract/schemas/decide_response `
        + `with ${pyRepr(key)}: ${problem}`);
    }
    return result[key];
  }

  // ---- the Transport

  async decideCalls(calls: WireCall[], policy: WirePolicy): Promise<DecisionJSON[]> {
    const decisions = await this.decideAnswer({ op: "decide", calls, on_low_confidence: policy }, "decisions") as DecisionJSON[];
    if (decisions.length !== calls.length) {
      throw new ContractError(`${this.wsUrl} answered ${decisions.length} decisions for ${calls.length} calls`);
    }
    return decisions;
  }

  async dispatch(body: Record<string, unknown>): Promise<DecisionJSON> {
    return await this.decideAnswer({ op: "dispatch", ...body }, "decision") as DecisionJSON;
  }

  async graph(body: Record<string, unknown>): Promise<GraphResultJSON> {
    return await this.decideAnswer({ op: "graph", ...body }, "graph_result") as GraphResultJSON;
  }

  functions(): Promise<FunctionsResponse> {
    return this.rest.functions();
  }

  // ---- handles

  /** Binds `fn` (its full ID) with this context, labels (wire form) and policy; the handle's number and the version of
   * the function's adapter the server serves. */
  async bind(fn: string, context: Record<string, JSONValue> | null, labels: Record<string, string | null> | string[] | null,
    policy: WirePolicy): Promise<[number, string]> {
    const message: Record<string, unknown> = { op: "bind", function: fn, on_low_confidence: policy };
    if (context !== null) message.context = { ...context };
    if (labels !== null) message.labels = labels;
    const result = await this.request(message);
    const { handle, version } = result;
    if (!(Number.isInteger(handle) && (handle as number) >= 1) || result.function !== fn || typeof version !== "string") {
      throw new ContractError(`${this.wsUrl} answered bind ${pyRepr(fn)} with ${pyRepr(result)}; expected a handle (a whole number `
        + `from 1) for ${pyRepr(fn)} and its adapter's version`);
    }
    return [handle as number, version];
  }

  async call(handle: number, input: JSONValue): Promise<DecisionJSON> {
    return await this.decideAnswer({ op: "call", handle, input }, "decision") as DecisionJSON;
  }

  async callMany(handle: number, inputs: readonly JSONValue[]): Promise<DecisionJSON[]> {
    const decisions = await this.decideAnswer({ op: "call_many", handle, inputs: [...inputs] }, "decisions") as DecisionJSON[];
    if (decisions.length !== inputs.length) {
      throw new ContractError(`${this.wsUrl} answered ${decisions.length} decisions for ${inputs.length} inputs`);
    }
    return decisions;
  }

  async release(handle: number): Promise<void> {
    await this.request({ op: "release", handle });
  }

  /** Queues a release, sent before the next request: safe from a finalizer (it sends nothing itself). Nothing to do
   * once the channel is closed: its handles ended with it. */
  releaseLater(handle: number): void {
    if (this.closedError === null) this.releases.push(handle);
  }

  get closed(): boolean {
    return this.closedError !== null;
  }

  // ---- refreshing

  /** Waits `ms` (an unref'd timer: it does not keep the process alive); true when the channel closed meanwhile. */
  private wait(ms: number): Promise<boolean> {
    if (this.closedError !== null) return Promise.resolve(true);
    return new Promise((resolve) => {
      const timer = setTimeout(() => { this.wake = null; resolve(false); }, ms);
      timer.unref();
      this.wake = () => { clearTimeout(timer); this.wake = null; resolve(true); };
    });
  }

  /** One refresh attempt: the function's token, sent with `refresh` when it expires after `expires`; its exp. */
  private async refreshOnce(expires: number): Promise<number> {
    const token = await currentToken(this.token, this.url);
    const renewed = tokenExpiry(token);
    if (renewed === null || renewed <= expires) {
      throw new Unauthorised(`the token function returned a token expiring at ${pyRepr(renewed)}, not after the channel's `
        + `current token (exp ${expires})`);
    }
    await this.request({ op: "refresh", token });
    return renewed;
  }

  /** From REFRESH_BEFORE_S seconds before each token's exp (and at least REFRESH_RETRY_S after the last successful
   * refresh, so a function minting tokens shorter-lived than REFRESH_BEFORE_S is not called in a tight loop), asks the
   * token function for a token expiring later and sends `refresh` with it; each failure is kept (and logged) and the
   * function asked again every REFRESH_RETRY_S seconds. If exp passes first, the channel closes with Unauthorised
   * carrying every failure as a note. */
  private async refreshLoop(first: number): Promise<void> {
    let expires = first;
    let earliest = 0; // no refresh before this time: REFRESH_RETRY_S after the last successful one
    for (;;) {
      const waitMs = Math.max(0, (expires - REFRESH_BEFORE_S) * 1000 - Date.now(), earliest - Date.now());
      if (await this.wait(Math.min(waitMs, MAX_TIMEOUT_MS))) return;
      if (waitMs > MAX_TIMEOUT_MS) continue; // longer than one Node timer: wait again
      let renewed: number | null = null;
      while (renewed === null) {
        try {
          renewed = await this.refreshOnce(expires);
        } catch (error) {
          if (this.closedError !== null) return;
          const failure = `Refreshing the channel's token (exp ${expires}) at ${(Date.now() / 1000).toFixed(1)} failed: `
            + described(error);
          this.refreshFailures.push(failure);
          console.warn(`${this.wsUrl}: ${failure}`);
          const remaining = expires * 1000 - Date.now();
          if (remaining <= 0) {
            this.shutdown(new Unauthorised(`The channel's token expired at ${expires} before the token function returned one `
              + `expiring later (${this.refreshFailures.length} attempts failed; see the notes); the channel is closed`));
            return;
          }
          if (await this.wait(Math.min(REFRESH_RETRY_S * 1000, remaining))) return;
        }
      }
      this.refreshFailures = [];
      expires = renewed;
      earliest = Date.now() + REFRESH_RETRY_S * 1000;
    }
  }

  // ---- closing

  /** Ends the channel (with a closing handshake of at most timeoutMs) and the HTTP connections. */
  async close(): Promise<void> {
    this.shutdown(new InvalidRequest(`This remote client of ${this.url} is closed; connect again with Jeff.remote`));
    await this.rest.close();
    if (this.ws.readyState !== WebSocket.CLOSED) await this.wsClosed;
  }
}

/** A garbage-collected bound function has its handle released before the channel's next request. */
const handles = new FinalizationRegistry<{ transport: ChannelTransport; handle: number }>(
  ({ transport, handle }) => transport.releaseLater(handle));

/** A bound function that is a handle on the channel: bound here (context, labels, policy checked) and then on the
 * server with one `bind`; each call is one `call` frame, many() one `call_many`. A policy the server can run is bound
 * with the handle; one with an llm, human or remote step is bound as "return" and run here on each decision. close()
 * releases the handle (waiting for the server's reply). */
export class ChannelBoundFunction extends RemoteBoundFunction {
  private released = false;

  private constructor(jeff: Jeff, template: Call, policy: Policy, bound: Binding, private readonly transport: ChannelTransport,
    private readonly wire: WirePolicy | null, private readonly handle: number) {
    super(jeff, template, policy, bound);
  }

  static override async create(jeff: Jeff, template: Call, policy: Policy): Promise<ChannelBoundFunction> {
    if (!(jeff instanceof ChannelJeff)) throw new Error("ChannelBoundFunction.create needs a ChannelJeff");
    const bound = await jeff.bindCall(template);
    await checkPolicy(jeff, bound.function.interface, policy);
    const wire = jeff.wirePolicy(policy);
    const transport = jeff.transport;
    const context = template.context === null || template.context === undefined ? null : { ...template.context };
    const labels = template.labels === null || template.labels === undefined ? null : labelsToWire(template.labels);
    const [handle, version] = await transport.bind(bound.function.id, context, labels, wire === null ? "return" : wire);
    if (version !== bound.manifest.version) {
      // The adapter changed on the server since connecting: the handle is released before the next request.
      transport.releaseLater(handle);
      throw versionMismatch(bound.manifest.id, version, bound.manifest.version);
    }
    const fn = new ChannelBoundFunction(jeff, template, policy, bound, transport, wire, handle);
    handles.register(fn, { transport, handle }, fn);
    return fn;
  }

  private live(): number {
    if (this.released) {
      throw new InvalidRequest(`This bound function of ${this.bound.function.id} is closed; bind it again with jeff.function`);
    }
    return this.handle;
  }

  override async call(input: JSONValue): Promise<Decision> {
    const decision = Decision.fromJSON(await this.transport.call(this.live(), input));
    if (this.wire !== null) return decision;
    return this.jeff.applyPolicy(this.callOf(input), decision, this.policy);
  }

  override async many(inputs: readonly JSONValue[]): Promise<Decision[]> {
    const handle = this.live();
    if (inputs.length === 0) return [];
    checkBatch(inputs.length, "inputs");
    const decisions = (await this.transport.callMany(handle, inputs)).map((item) => Decision.fromJSON(item));
    if (this.wire !== null) return decisions;
    const out: Decision[] = [];
    for (const [index, input] of inputs.entries()) {
      out.push(await this.jeff.applyPolicy(this.callOf(input), decisions[index] as Decision, this.policy));
    }
    return out;
  }

  /** Releases the handle. Nothing to release once the channel is closed: its handles ended with it. */
  override async close(): Promise<void> {
    if (this.released) return;
    this.released = true;
    handles.unregister(this);
    if (!this.transport.closed) await this.transport.release(this.handle);
  }
}

/** A ChannelJeff dropped without close() closes its channel when the garbage collector takes it: the transport holds
 * no reference to it. */
const channels = new FinalizationRegistry<ChannelTransport>((transport) => { void transport.close(); });

/** A RemoteJeff over the channel: everything as RemoteJeff, with bound functions as handles. */
export class ChannelJeff extends RemoteJeff {
  declare readonly transport: ChannelTransport;

  static async open(transport: ChannelTransport, catalogue: Catalogue, url: string): Promise<ChannelJeff> {
    const served = await ServedFunctions.read(transport, catalogue, url);
    const jeff = new ChannelJeff(await jeffParts(served, catalogue), transport, url);
    channels.register(jeff, transport, jeff);
    return jeff;
  }

  override function(ref: string, options: CallOptions = {}): Promise<BoundFunction> {
    return ChannelBoundFunction.create(this, { function: ref, input: null, context: options.context, labels: options.labels },
      options.onLowConfidence === undefined ? "return" : options.onLowConfidence);
  }

  /** Closes the channel; calling it again does nothing more. */
  override async close(): Promise<void> {
    channels.unregister(this);
    await this.transport.close();
  }
}

/** Jeff.remote with channel: true. */
export async function connectChannel(url: string, token: TokenSource, timeoutMs: number, catalogue: Catalogue): Promise<ChannelJeff> {
  const transport = await ChannelTransport.open(url, token, timeoutMs);
  try {
    return await ChannelJeff.open(transport, catalogue, transport.url);
  } catch (error) {
    await transport.close();
    throw error;
  }
}
