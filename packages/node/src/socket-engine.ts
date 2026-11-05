/** An Engine over the engine socket (contract/socket.md; port of jeff/socket_engine.py): one connection, requests
 * multiplexed by id and sent as they come, so the engine batches concurrent callers. After the engine closes the
 * connection (its process restarted, say), the next call makes one fresh connection (at most one attempt per second);
 * a call that was in flight when it closed fails and is never resent. It never starts an engine. */

import { createConnection, type Socket } from "node:net";
import type { Row } from "./binding.js";
import type { BaseFormat, Engine, EngineResult } from "./engine.js";
import { EngineError, JeffError, copyError, errorFromWire } from "./errors.js";
import { FrameReader, checkHello, decodeBody, encodeFrame, hello } from "./protocol.js";
import { pyFixed, pyFloat, pyRepr } from "./pyformat.js";

/** The fewest milliseconds between two reconnect attempts. A call made sooner after an attempt fails at once, naming
 * that attempt, so that while the engine is down every caller learns so immediately and its socket is not hammered. */
export const RECONNECT_INTERVAL_MS = 1000;

/** Replies to these ops carry the engine's whole installed list beside its version; any other reply carries only the
 * version, and a version different from the cached one means asking for the list with `installed`. */
const CARRY_ADAPTERS = new Set(["installed", "serve", "reload"]);

/** The command that starts an engine listening at `path`. */
export function startCommand(path: string): string {
  return `jeff runtime serve --socket ${path} --adapter <adapter id> --batch-window-ms <milliseconds> `
    + "--max-batch-rows <rows> --queue-limit-rows <rows> --prepare-workers <threads>";
}

interface Pending {
  resolve(reply: Record<string, unknown>): void;
  reject(error: JeffError): void;
  timer: NodeJS.Timeout;
}

/** Nothing listens at the path: the socket's own error, which connect and reconnect each word. */
class DialFailure extends Error {}

/** The error a failure reply describes. The engine puts its adapters_version in every failure's details; that is
 * protocol information, not an attribute of the error, so it is left out. */
function rebuild(body: Record<string, unknown>): JeffError {
  const details = { ...((body.details ?? {}) as Record<string, unknown>) };
  delete details.adapters_version;
  return errorFromWire({ ...body, details } as { type: unknown; message: unknown; details: unknown });
}

function described(error: unknown): string {
  return `${(error as Error).name}: ${(error as Error).message}`;
}

export class SocketEngine implements Engine {
  /** How many times a call has reconnected to the engine after its connection closed. */
  reconnects = 0;
  private socket: Socket | null = null;
  private closed: JeffError | null = null;
  private closedByCaller = false;
  private readonly pending = new Map<number, Pending>();
  private nextId = 1;
  private snapshot: [number, Set<string>] = [-1, new Set()];
  /** When the last reconnect attempt was made, and the error it failed with (null when it succeeded). */
  private lastAttempt: { at: number; failure: EngineError | null } | null = null;
  private reconnecting: Promise<void> | null = null;
  private format: BaseFormat = { format: "" };
  private served: [string, string] = ["", ""];

  private constructor(readonly path: string, readonly timeoutMs: number) {}

  /** Connects to the engine at `path`, exchanges hellos, and reads its installed list and base once. */
  static async connect(path: string, options: { timeoutMs: number }): Promise<SocketEngine> {
    const engine = new SocketEngine(path, options.timeoutMs);
    try {
      await engine.open();
    } catch (error) {
      if (error instanceof DialFailure) {
        throw new EngineError(`No engine is listening at ${path}: ${error.message}. Start one with: ${startCommand(path)}`);
      }
      throw error;
    }
    try {
      // no reconnect while connecting: a connection that closes now fails the connect, naming why
      engine.merge(...(await engine.installedNow(undefined, false)));
      [engine.format, engine.served] = await engine.baseNow(false);
    } catch (error) {
      await engine.close();
      throw error;
    }
    return engine;
  }

  /** The base's weight format, read once at connect. */
  get baseFormat(): BaseFormat { return { ...this.format }; }
  /** The base the engine serves as adapter manifests name it (id, version), read once at connect. */
  get base(): [string, string] { return [...this.served] as [string, string]; }
  /** The engine's adapters_version as of the last reply read from it (-1 from a reconnect until its first reply). */
  get adaptersVersion(): number { return this.snapshot[0]; }

  private dial(): Promise<Socket> {
    return new Promise<Socket>((resolve, reject) => {
      const socket = createConnection(this.path);
      const failed = (error: Error) => reject(new DialFailure(error.message));
      socket.once("error", failed);
      socket.once("connect", () => {
        socket.off("error", failed);
        resolve(socket);
      });
    });
  }

  /** Opens a connection, exchanges hellos (bounded by timeoutMs) and makes it the connection every call uses. A
   * failure closes the new socket and leaves the current state as it was. Request ids are not restarted; the cached
   * adapters_version is set below every real one (the list is kept), since a restarted engine counts from 0 again. */
  private async open(): Promise<void> {
    const socket = await this.dial();
    const reader = new FrameReader();
    // "handshake" until the hello is read; "early" from then until this connection is the current one, while any
    // frame (or stream failure) that came in the same bytes as the hello is kept; "ready" after.
    let stage: "handshake" | "early" | "ready" = "handshake";
    const early: Buffer[] = [];
    let earlyFailure: unknown = null;
    let settle!: { resolve(message: Record<string, unknown>): void; reject(error: unknown): void };
    const greeting = new Promise<Record<string, unknown>>((resolve, reject) => { settle = { resolve, reject }; });
    const timer = setTimeout(() => settle.reject(new EngineError(`No hello from the engine at ${this.path} within ${this.timeoutMs} ms`)),
      this.timeoutMs);
    const onHandshakeError = (error: Error) => settle.reject(
      new EngineError(`The engine at ${this.path} closed the connection during the handshake: ${error.message}`));
    const onHandshakeClose = () => settle.reject(new EngineError(`The engine at ${this.path} closed the connection during the handshake`));
    const broken = (error: unknown) => {
      if (stage === "handshake") settle.reject(error);
      else if (stage === "early") earlyFailure ??= error;
      else this.fail(socket, new EngineError(`The engine socket to ${this.path} failed: ${described(error)}`));
    };
    const deliver = (body: Buffer) => {
      if (stage === "handshake") {
        stage = "early";
        try {
          settle.resolve(decodeBody(body));
        } catch (error) {
          settle.reject(error);
        }
      } else if (stage === "early") early.push(body);
      else this.onReply(socket, body);
    };
    socket.on("error", onHandshakeError);
    socket.on("close", onHandshakeClose);
    socket.on("data", (chunk: Buffer) => {
      let bodies: Buffer[];
      try {
        bodies = reader.push(chunk);
      } catch (error) {
        broken(error);
        return;
      }
      for (const body of bodies) deliver(body);
      if (reader.failure !== null) broken(reader.failure);
    });
    try {
      socket.write(encodeFrame(hello()));
      const message = await greeting;
      if (message.ok === false) throw rebuild(message.error as Record<string, unknown>);
      checkHello(message);
      if (this.closedByCaller) throw this.dead();
    } catch (error) {
      socket.destroy();
      throw error;
    } finally {
      clearTimeout(timer);
      socket.off("error", onHandshakeError);
      socket.off("close", onHandshakeClose);
    }
    socket.on("close", () => this.fail(socket, new EngineError(`The engine at ${this.path} closed the connection`)));
    socket.on("error", (error) => this.fail(socket, new EngineError(`The engine socket to ${this.path} failed: ${described(error)}`)));
    // An idle connection does not keep the process alive; a call waiting for its reply does, through its timer.
    socket.unref();
    this.socket = socket;
    this.snapshot = [-1, this.snapshot[1]];
    this.closed = null;
    stage = "ready";
    for (const body of early) this.onReply(socket, body);
    if (earlyFailure !== null) broken(earlyFailure);
  }

  private onReply(socket: Socket, body: Buffer): void {
    let message: Record<string, unknown>;
    try {
      message = decodeBody(body);
    } catch (error) {
      this.fail(socket, new EngineError(`The engine socket to ${this.path} failed: ${described(error)}`));
      return;
    }
    const id = Number(message.id);
    if (id === 0 && message.ok === false) {
      // Not a reply to any call (this side's ids start at 1): a connection-level failure the engine reports, which
      // every waiting call fails with.
      this.fail(socket, rebuild(message.error as Record<string, unknown>));
      return;
    }
    const waiting = this.pending.get(id);
    if (waiting === undefined) return;
    this.pending.delete(id);
    clearTimeout(waiting.timer);
    waiting.resolve(message);
  }

  /** Ends the connection `socket`, if it is still the current one: the first error wins and every waiting call
   * fails with it. A socket that is no longer the current one is only destroyed. */
  private fail(socket: Socket, error: JeffError): void {
    if (socket !== this.socket) {
      socket.destroy();
      return;
    }
    if (this.closed === null) this.closed = error;
    const closed = this.closed;
    const waiting = [...this.pending.values()];
    this.pending.clear();
    for (const call of waiting) {
      clearTimeout(call.timer);
      call.reject(copyError(closed));
    }
    socket.destroy();
  }

  /** The error for a call made once the connection is closed for good. */
  private dead(): EngineError {
    const cause = this.closed as JeffError;
    return new EngineError(`The engine at ${this.path} closed the connection after: ${described(cause)}`);
  }

  /** The error for a call made within RECONNECT_INTERVAL_MS of the last attempt, naming what it found. */
  private tooSoon(now: number): EngineError {
    const { at, failure } = this.lastAttempt as { at: number; failure: EngineError | null };
    const ago = pyFixed((now - at) / 1000, 2);
    const interval = pyFloat(RECONNECT_INTERVAL_MS / 1000);
    if (failure !== null) {
      return new EngineError(`No engine is listening at ${this.path}: the last reconnect attempt, ${ago}s ago, failed: `
        + `${failure.message}. The next attempt may be made ${interval}s after it`);
    }
    return new EngineError(`The engine at ${this.path} closed the connection ${ago}s ago, just after this side reconnected: `
      + `${described(this.closed)}. The next attempt may be made ${interval}s after the last`);
  }

  /** Called by a call that found the connection closed: one attempt at a time makes one fresh connection (connect,
   * hello, installed, health); a call that waited meanwhile then finds the connection open and uses it, or fails as
   * Python's would, naming that attempt. At most one attempt per RECONNECT_INTERVAL_MS; never after close(). */
  private async reconnect(): Promise<void> {
    while (this.reconnecting !== null) {
      try {
        await this.reconnecting;
      } catch {
        // the attempt's own caller gets its error; this call checks the state afresh below
      }
    }
    if (this.closedByCaller) throw this.dead();
    if (this.closed === null) return;
    const now = performance.now();
    if (this.lastAttempt !== null && now - this.lastAttempt.at < RECONNECT_INTERVAL_MS) throw this.tooSoon(now);
    const attempt: { at: number; failure: EngineError | null } = { at: now, failure: null };
    this.lastAttempt = attempt;
    this.reconnecting = this.attempt(attempt);
    try {
      await this.reconnecting;
    } finally {
      this.reconnecting = null;
    }
  }

  private async attempt(attempt: { at: number; failure: EngineError | null }): Promise<void> {
    let version: number;
    let installed: Set<string>;
    let format: BaseFormat;
    let base: [string, string];
    try {
      await this.open();
      [version, installed] = await this.installedNow(undefined, false);
      [format, base] = await this.baseNow(false);
    } catch (cause) {
      const error = new EngineError(`No engine is listening at ${this.path}: ${(cause as Error).message} (reconnect attempt)`);
      attempt.failure = error;
      this.shutdown(error);
      throw error;
    }
    if (pyRepr(format) !== pyRepr(this.format) || base[0] !== this.served[0] || base[1] !== this.served[1]) {
      // Functions bound through this engine chose their calibration for the format read at connect; the restarted
      // engine serves other weights, so nothing more is sent to it from this process.
      const error = new EngineError(`The engine at ${this.path} restarted serving a base of format ${pyRepr(format)} `
        + `(${base[0]}@${base[1]}), not ${pyRepr(this.format)} (${this.served[0]}@${this.served[1]}) as when this process `
        + "connected; the functions bound here use that format's calibration, and the adapters listed are the ones "
        + "trained for that base. Restart this process to serve the new base");
      attempt.failure = error;
      this.shutdown(error);
      throw error;
    }
    this.merge(version, installed);
    this.reconnects += 1;
  }

  private shutdown(error: JeffError): void {
    if (this.socket !== null) this.fail(this.socket, error);
  }

  private version(body: Record<string, unknown>, op: string): number {
    if (!("adapters_version" in body)) {
      throw new EngineError(`The engine at ${this.path} answered ${pyRepr(op)} without an adapters_version; every reply must `
        + "carry one (contract/socket.md)");
    }
    return Number(body.adapters_version);
  }

  /** Sends one request and waits for its reply. Every reply carries the engine's adapters_version: a reply to
   * installed/serve/reload sets the cached list from its own; any other reply with another version makes this side
   * ask for the list (except a failed `installed`). A failure is raised as the error it describes, after that.
   * `reconnect` false is a call made by a reconnect attempt itself: a connection already closed again fails at once. */
  private async call(message: Record<string, unknown>, timeoutMs = this.timeoutMs, reconnect = true): Promise<Record<string, unknown>> {
    const op = String(message.op);
    const id = this.nextId++;
    // encoded before anything is registered: an oversized or invalid message leaves nothing behind
    const payload = encodeFrame({ ...message, id });
    if (this.closed !== null || this.socket === null) {
      // without reconnect (connect, or a reconnect attempt's own calls) a closed connection fails at once
      if (!reconnect) throw this.closedByCaller || this.lastAttempt === null ? this.dead() : this.tooSoon(performance.now());
      await this.reconnect();
      return this.call(message, timeoutMs, reconnect);
    }
    const socket = this.socket;
    const reply = await new Promise<Record<string, unknown>>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new EngineError(`No reply from the engine at ${this.path} within ${timeoutMs} ms`));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      socket.write(payload);
    });
    if (reply.ok !== true) {
      // The engine's own error always wins; a failure reading its version or refreshing the list is a note on it.
      const failure = reply.error as Record<string, unknown>;
      const original = rebuild(failure);
      let version: number;
      try {
        version = this.version((failure.details ?? {}) as Record<string, unknown>, op);
      } catch (error) {
        original.notes.push(`Also failed to read adapters_version from this failure reply: ${described(error)}`);
        throw original;
      }
      if (op !== "installed") {
        try {
          await this.follow(version, timeoutMs, reconnect);
        } catch (error) {
          original.notes.push(`Also failed to refresh the adapter list after this error: ${described(error)}`);
        }
      }
      throw original;
    }
    const result = reply.result as Record<string, unknown>;
    const version = this.version(result, op);
    if (CARRY_ADAPTERS.has(op)) this.merge(version, new Set(result.adapters as string[]));
    else await this.follow(version, timeoutMs, reconnect);
    return result;
  }

  /** `reconnect` false is a refresh made by a reconnect attempt itself (or by connect()): it must never start, or
   * wait on, a reconnect attempt, since it may be running inside the very one it would wait on. */
  private async follow(version: number, timeoutMs: number | undefined, reconnect: boolean): Promise<void> {
    if (version !== this.snapshot[0]) this.merge(...(await this.installedNow(timeoutMs, reconnect)));
  }

  /** The engine's current (version, installed) pair; every caller routes it through merge. */
  private async installedNow(timeoutMs?: number, reconnect = true): Promise<[number, Set<string>]> {
    const result = await this.call({ op: "installed" }, timeoutMs, reconnect);
    return [Number(result.adapters_version), new Set(result.adapters as string[])];
  }

  /** The one place the cached pair is written. Replies may arrive out of order, so an older version never replaces a
   * newer one. */
  private merge(version: number, installed: Set<string>): void {
    if (version >= this.snapshot[0]) this.snapshot = [version, new Set(installed)];
  }

  /** From the engine's health: its base format (format, and format_id for an INT4 base) and the base it serves. */
  private async baseNow(reconnect = true): Promise<[BaseFormat, [string, string]]> {
    const health = await this.call({ op: "health" }, undefined, reconnect);
    const base = health.base as Record<string, unknown> | undefined;
    if (base === null || typeof base !== "object" || Array.isArray(base) || typeof base.format !== "string") {
      throw new EngineError(`The engine at ${this.path} answered 'health' without base.format (got base ${pyRepr(base)}); every `
        + "engine reports its base's weight format (contract/socket.md)");
    }
    if (typeof base.id !== "string" || typeof base.version !== "string") {
      throw new EngineError(`The engine at ${this.path} answered 'health' without base.id and base.version (got base `
        + `${pyRepr(base)}); every engine reports the base it serves as adapter manifests name it (contract/socket.md)`);
    }
    const format: BaseFormat = "format_id" in base
      ? { format: base.format, format_id: base.format_id as string } : { format: base.format };
    return [format, [base.id, base.version]];
  }

  /** The cached list of adapters the engine has loaded; with refresh, asked of the engine first. */
  async installed(refresh = false): Promise<Set<string>> {
    if (refresh) this.merge(...(await this.installedNow()));
    return new Set(this.snapshot[1]);
  }

  /** The engine's (adapters_version, installed list) pair from one reply, which also updates the cache. */
  async installedVersion(timeoutMs?: number): Promise<[number, Set<string>]> {
    const [version, installed] = await this.installedNow(timeoutMs);
    this.merge(version, installed);
    return [version, new Set(installed)];
  }

  async predict(adapterId: string, rows: Row[], deadlineMs?: number): Promise<EngineResult> {
    const message: Record<string, unknown> = { op: "predict", adapter: adapterId, rows };
    if (deadlineMs !== undefined) message.deadline_ms = deadlineMs;
    const result = await this.call(message);
    return {
      probabilities: (result.probabilities as number[][]).map((row) => row.map(Number)),
      timing: { ...(result.timing as Record<string, unknown>) },
      // optional in the reply: an engine that does not count tokens leaves it out
      inputTokens: "input_tokens" in result ? (result.input_tokens as number[]).map(Number) : null,
    };
  }

  health(timeoutMs?: number): Promise<Record<string, unknown>> { return this.call({ op: "health" }, timeoutMs); }
  stats(timeoutMs?: number): Promise<Record<string, unknown>> { return this.call({ op: "stats" }, timeoutMs); }

  /** Final: later calls fail, and there is no reconnect. */
  async close(): Promise<void> {
    this.closedByCaller = true;
    const socket = this.socket;
    if (socket === null) return;
    this.fail(socket, new EngineError(`The SocketEngine for ${this.path} was closed by its caller`));
    if (!socket.closed) await new Promise<void>((resolve) => socket.once("close", () => resolve()));
  }
}
