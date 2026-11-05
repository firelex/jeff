/** A fake Jeff server for the channel tests: GET /v1/functions answers the listing recorded in remote.json, and a
 * WebSocket at /v1/channel answers hello, auth, refresh, bind, call, call_many, decide and release over a local Jeff
 * with a fake engine (as jeff-server's channel does; contract/socket.md, "The channel"). */

import { type ServerResponse, createServer } from "node:http";
import { createServer as createTlsServer } from "node:https";
import type { AddressInfo } from "node:net";
import { decode } from "@msgpack/msgpack";
import { WebSocket, WebSocketServer } from "ws";
import type { Row, RowQuestion } from "../../src/binding.js";
import { Catalogue } from "../../src/catalogue.js";
import { Jeff } from "../../src/client.js";
import { encode } from "../../src/msgpack.js";
import { FakeEngine } from "../../src/testing.js";
import { fixture } from "../conformance/fixtures.js";

/** The most requests a connection may have outstanding (contract/socket.md, "Limits"). */
const MAX_OUTSTANDING = 256;

export const INSTALLED = ["jeff.support.triage", "jeff.safety.spam"];

/** The GET /v1/functions answer recorded in remote.json (the shipped catalogue's functions, base bf16). */
const LISTING = (fixture<any>("remote.json").cases[0].exchanges[0].response.body) as string;

function frame(message: unknown): Buffer {
  const body = Buffer.from(encode(message));
  const header = Buffer.alloc(4);
  header.writeUInt32BE(body.length);
  return Buffer.concat([header, body]);
}

/** Below triage.urgency's calibration threshold 0.6 (as the Python tests' UNSURE). */
const UNSURE = [0, 0.5, 0.5, 0, 0];

/** The fake engine's answers (as the Python tests' `answers`): a score function's level 2, confidently, unless the
 * input contains "unsure"; a choice's first criterion; every other type's first option. */
export function answers(_adapter: string, row: Row): number[] {
  const question: RowQuestion = row.question;
  if (question.type === "score") {
    const state = JSON.stringify(row.state instanceof Map ? [...row.state] : row.state);
    return state.includes("unsure") ? UNSURE : [0, 0, 0.9, 0.1, 0];
  }
  const criteria = question.criteria;
  const count = criteria === undefined ? 2 : criteria instanceof Map ? criteria.size : criteria.length;
  return [1, ...Array(count - 1).fill(0)];
}

export interface ChannelServer {
  url: string;
  /** Every message the server received, decoded, in order. */
  received: any[];
  /** The Authorization header of each WebSocket handshake ("" when there was none). */
  headers: string[];
  /** Sends `message` as one frame on every open connection. */
  send(message: unknown): void;
  /** Lets every request of a gated op go on. */
  openGate(): void;
  /** How many requests of gated ops are waiting at the gate. */
  waiting(): number;
  /** Ends every connection at once, without a close frame. */
  dropConnections(): void;
  close(): Promise<void>;
}

/** `silent`: ops that get no answer; `version`: what bind reports for every adapter (the catalogue's when not given);
 * `token`: the only token auth and refresh accept (any token when not given); `gated`: ops whose requests wait until
 * openGate(); `reorder`: the replies to call, call_many and decide are held until that many are ready, then sent in
 * reverse order; `badLowConfidenceDecision`: every call, call_many and decide fails at once with LowConfidence whose
 * decision (details.decision) does not match contract/schemas/decision, to test that the SDK checks it before
 * building the error; `tls`: serve https:// and wss:// with this key and certificate. As jeff-server, a request arriving
 * while 256 are outstanding fails at once with RateLimited. */
export async function startChannelServer(options: { version?: string; silent?: string[]; token?: string; gated?: string[];
  reorder?: number; badLowConfidenceDecision?: boolean; tls?: { key: string; cert: string } } = {}): Promise<ChannelServer> {
  const catalogue = Catalogue.shipped();
  const jeff = await Jeff.create(new FakeEngine(answers, INSTALLED), catalogue);
  let openGate!: () => void;
  const gate = new Promise<void>((resolve) => { openGate = resolve; });
  let waiting = 0;
  const held: Buffer[] = [];
  const received: any[] = [];
  const headers: string[] = [];
  const handles = new Map<number, any>();
  let nextHandle = 1;
  const sockets = new Set<WebSocket>();
  const outstanding = new Map<WebSocket, number>();
  const listing = (request: { url?: string }, response: ServerResponse) => {
    if (request.url === "/v1/functions") response.writeHead(200, { "content-type": "application/json" }).end(LISTING);
    else response.writeHead(404).end();
  };
  const http = options.tls === undefined ? createServer(listing) : createTlsServer(options.tls, listing);
  const wss = new WebSocketServer({ server: http, path: "/v1/channel" });
  wss.on("connection", (ws, request) => {
    sockets.add(ws);
    outstanding.set(ws, 0);
    ws.on("close", () => sockets.delete(ws));
    headers.push(request.headers.authorization === undefined ? "" : request.headers.authorization);
    ws.on("message", async (data: Buffer) => {
      const message = decode(data.subarray(4)) as any;
      received.push(message);
      if (options.silent !== undefined && options.silent.includes(message.op)) return;
      if (message.op !== "hello" && outstanding.get(ws) as number >= MAX_OUTSTANDING) {
        ws.send(frame({ id: message.id, ok: false, error: { type: "RateLimited", message: `${MAX_OUTSTANDING} requests are `
          + "outstanding on this connection", details: { retry_after_s: 1, channel_id: "0badc0de" } } }));
        return;
      }
      const reply = (body: Record<string, unknown>) => {
        if (message.op !== "hello") outstanding.set(ws, outstanding.get(ws) as number - 1);
        const bytes = frame({ id: message.id, ...body });
        if (options.reorder === undefined || !["call", "call_many", "decide"].includes(message.op)) return ws.send(bytes);
        held.push(bytes);
        if (held.length === options.reorder) for (const item of held.splice(0).reverse()) ws.send(item);
      };
      const ok = (result: unknown) => reply({ ok: true, result });
      const fail = (type: string, text: string, details: Record<string, unknown> = {}) => reply({ ok: false,
        error: { type, message: text, details: { channel_id: "0badc0de", ...details } } });
      if (message.op !== "hello") outstanding.set(ws, outstanding.get(ws) as number + 1);
      if (options.gated !== undefined && options.gated.includes(message.op)) {
        waiting += 1;
        await gate;
        waiting -= 1;
      }
      if (options.badLowConfidenceDecision === true && ["call", "call_many", "decide"].includes(message.op)) {
        return fail("LowConfidence", "abstained", { decision: { value: 1 } });
      }
      if (message.op === "hello") ws.send(frame({ op: "hello", version: 2 }));
      else if (message.op === "auth" || message.op === "refresh") {
        if (options.token !== undefined && message.token !== options.token) return fail("Unauthorised", "the token is not valid");
        ok({});
      } else if (message.op === "bind") {
        const handle = nextHandle++;
        handles.set(handle, message);
        const adapter = catalogue.function(message.function).adapterId;
        ok({ handle, function: message.function,
          version: options.version === undefined ? catalogue.adapter(adapter).version : options.version });
      } else if (message.op === "call" || message.op === "call_many") {
        const bound = handles.get(message.handle);
        if (bound === undefined) return fail("InvalidRequest", `handle ${message.handle} is not bound`);
        const inputs = message.op === "call" ? [message.input] : message.inputs;
        const decisions = await jeff.decide(inputs.map((input: unknown) => ({ function: bound.function, input,
          context: bound.context, labels: bound.labels })));
        ok(message.op === "call" ? { decision: decisions[0]?.toJSON() } : { decisions: decisions.map((item) => item.toJSON()) });
      } else if (message.op === "decide") ok({ decisions: (await jeff.decide(message.calls)).map((item) => item.toJSON()) });
      else if (message.op === "release") {
        if (!handles.delete(message.handle)) return fail("InvalidRequest", `handle ${message.handle} is not bound`);
        ok({});
      }
    });
  });
  await new Promise<void>((resolve) => http.listen(0, "127.0.0.1", resolve));
  return {
    url: `${options.tls === undefined ? "http" : "https"}://127.0.0.1:${(http.address() as AddressInfo).port}`, received, headers,
    send: (message) => { for (const ws of sockets) ws.send(frame(message)); },
    openGate, waiting: () => waiting,
    dropConnections: () => { for (const ws of sockets) ws.terminate(); },
    close: () => new Promise<void>((resolve) => {
      for (const ws of sockets) ws.terminate();
      wss.close();
      http.closeAllConnections();
      http.close(() => resolve());
    }),
  };
}
