/** Precise per-phase timeouts for one HTTP request over undici (used by remote.ts's RestTransport).
 *
 * undici's own connect, headers and body timeouts run on a coarse clock (about one second), so a 200 ms timeout fires
 * after about a second. RestTransport turns them off and gives each request a PhaseTimers instead: native timers, as
 * httpx's Timeout(timeout_s) has them, each aborting the request's AbortController with a PhaseTimeout naming its
 * phase:
 * - connect: from dispatch until the request reaches a socket (onRequestStart);
 * - headers: from then until the answer's headers arrive (onResponseStart);
 * - body: restarted on every chunk of the answer's body (onResponseData).
 * The undici interceptor that observes those events passes these handler callbacks through unchanged, and no others:
 * onRequestStart, onRequestUpgrade, onResponseStarted, onResponseStart, onResponseData, onResponseEnd and
 * onResponseError. A PhaseTimers times exactly one request: dispatching through it a second time (as a followed redirect
 * would) is a programming error and throws. */

import type { Dispatcher } from "undici";

export type Phase = "connect" | "headers" | "body";

/** The abort reason of a request whose phase did not finish in time. */
export class PhaseTimeout extends Error {
  constructor(readonly phase: Phase, readonly timeoutMs: number) {
    super(`no ${phase === "connect" ? "connection" : phase === "headers" ? "answer headers" : "further answer body"} `
      + `within ${timeoutMs} ms`);
    this.name = "PhaseTimeout";
  }
}

export class PhaseTimers {
  private readonly controller = new AbortController();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private done = false;
  private dispatched = false;
  /** Whether the request reached a socket: a failure before that is a failure to connect. */
  started = false;

  constructor(readonly timeoutMs: number) {}

  get signal(): AbortSignal {
    return this.controller.signal;
  }

  /** Whether a timer is still running (false once the request ended, failed, timed out or was finished). */
  get running(): boolean {
    return this.timer !== null;
  }

  private start(phase: Phase): void {
    this.stopTimer();
    if (this.done) return;
    this.timer = setTimeout(() => {
      this.timer = null;
      this.done = true;
      this.controller.abort(new PhaseTimeout(phase, this.timeoutMs));
    }, this.timeoutMs);
  }

  private stopTimer(): void {
    if (this.timer !== null) clearTimeout(this.timer);
    this.timer = null;
  }

  /** Stops every timer: the request settled, failed or is being given up. */
  finish(): void {
    this.done = true;
    this.stopTimer();
  }

  /** The undici interceptor that drives the timers for this one request. */
  interceptor(): Dispatcher.DispatcherComposeInterceptor {
    return (dispatch) => (options, handler) => {
      if (this.dispatched) {
        throw new Error("This PhaseTimers already timed a request; each request needs its own (a followed redirect would "
          + "dispatch twice: RestTransport sends every request with redirect: \"manual\")");
      }
      this.dispatched = true;
      this.start("connect");
      const wrapped: Dispatcher.DispatchHandler = {
        onRequestStart: (controller, context) => {
          this.started = true;
          this.start("headers");
          handler.onRequestStart?.(controller, context);
        },
        onRequestUpgrade: (controller, statusCode, headers, socket) => {
          this.finish();
          handler.onRequestUpgrade?.(controller, statusCode, headers, socket);
        },
        onResponseStarted: () => {
          handler.onResponseStarted?.();
        },
        onResponseStart: (controller, statusCode, headers, statusMessage) => {
          this.start("body");
          handler.onResponseStart?.(controller, statusCode, headers, statusMessage);
        },
        onResponseData: (controller, chunk) => {
          this.start("body");
          handler.onResponseData?.(controller, chunk);
        },
        onResponseEnd: (controller, trailers) => {
          this.finish();
          handler.onResponseEnd?.(controller, trailers);
        },
        onResponseError: (controller, error) => {
          this.finish();
          handler.onResponseError?.(controller, error);
        },
      };
      return dispatch(options, wrapped);
    };
  }
}
