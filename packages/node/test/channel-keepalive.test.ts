/** The channel's WebSocket keepalive and frame rules, against a bare ws server that only answers hello: a ping every
 * interval and a missing pong ending the channel (as the Python SDK's websockets client does, 20 s and 20 s, shortened
 * here with pinChannelKeepaliveForTests), a ping between the fragments of a message, and a close code RFC 6455 does
 * not allow on the wire. */

import type { AddressInfo } from "node:net";
import { afterEach, expect, test } from "vitest";
import { WebSocket, WebSocketServer } from "ws";
import { ChannelTransport, PING_INTERVAL_MS, PING_TIMEOUT_MS, pinChannelKeepaliveForTests } from "../src/channel.js";
import { BackendUnavailable } from "../src/errors.js";
import { encode } from "../src/msgpack.js";
import { PROTOCOL_VERSION } from "../src/protocol.js";
import { withoutProxies } from "./helpers/no-proxies.js";

afterEach(() => pinChannelKeepaliveForTests(null));

withoutProxies();

function frame(message: unknown): Buffer {
  const body = Buffer.from(encode(message));
  const header = Buffer.alloc(4);
  header.writeUInt32BE(body.length);
  return Buffer.concat([header, body]);
}

const HELLO = frame({ op: "hello", version: PROTOCOL_VERSION });

/** A ws server at /v1/channel that answers hello and nothing else. `autoPong`: whether it answers pings; `onHello`:
 * how it sends its hello (whole, by default). */
async function bareServer(options: { autoPong: boolean; onHello?: (ws: WebSocket) => void }) {
  const wss = new WebSocketServer({ port: 0, host: "127.0.0.1", path: "/v1/channel", autoPong: options.autoPong });
  await new Promise<void>((resolve) => wss.once("listening", () => resolve()));
  const pings: Buffer[] = [];
  const pongs: Buffer[] = [];
  const closes: number[] = [];
  const sockets: WebSocket[] = [];
  wss.on("connection", (ws) => {
    sockets.push(ws);
    ws.on("ping", (data: Buffer) => pings.push(data));
    ws.on("pong", (data: Buffer) => pongs.push(data));
    ws.on("close", (code: number) => closes.push(code));
    ws.on("error", () => {});
    ws.once("message", () => (options.onHello ?? ((socket) => socket.send(HELLO)))(ws));
  });
  return {
    url: `http://127.0.0.1:${(wss.address() as AddressInfo).port}`,
    pings, pongs, closes, sockets,
    close: () => new Promise<void>((resolve) => {
      for (const ws of wss.clients) ws.terminate();
      wss.close(() => resolve());
    }),
  };
}

const pause = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

test("the keepalive is a ping every 20 s with its pong due within 20 s, as the Python SDK's websockets", () => {
  expect([PING_INTERVAL_MS, PING_TIMEOUT_MS]).toEqual([20_000, 20_000]);
});

test("answered pings keep an idle channel open", async () => {
  pinChannelKeepaliveForTests({ intervalMs: 40, timeoutMs: 100 });
  const server = await bareServer({ autoPong: true });
  const transport = await ChannelTransport.open(server.url, null, 2000);
  await pause(600);
  expect(server.pings.length).toBeGreaterThanOrEqual(5);
  // numbered pings, one at a time
  expect(server.pings.slice(0, 3).map((data) => data.readBigUInt64BE(0))).toEqual([1n, 2n, 3n]);
  expect((transport as unknown as { closedError: unknown }).closedError).toBeNull();
  expect(server.closes).toEqual([]);
  await transport.close();
  await server.close();
});

test("an ignored ping ends the channel with BackendUnavailable naming the missing pong, failing a waiting request", async () => {
  pinChannelKeepaliveForTests({ intervalMs: 50, timeoutMs: 150 });
  const server = await bareServer({ autoPong: false });
  const transport = await ChannelTransport.open(server.url, null, 5000);
  const wsUrl = `${server.url.replace("http://", "ws://")}/v1/channel`;
  const started = performance.now();
  // the server never answers this request: the keepalive, not the 5 s request timer, ends it
  const error = await transport.decideCalls([{ function: "jeff.support.triage", input: "x" }], "return")
    .then(() => null, (caught: unknown) => caught);
  expect(error).toEqual(new BackendUnavailable(`${wsUrl}: the server sent no pong within 0.15 s of a keepalive ping; the `
    + "channel closed"));
  expect(performance.now() - started).toBeLessThan(1000);
  expect(server.pings).toHaveLength(1);
  await expect(transport.decideCalls([{ function: "jeff.support.triage", input: "x" }], "return")).rejects.toThrow(
    "the server sent no pong");
  await transport.close();
  await server.close();
});

test("a ping between the fragments of a message is answered and the message read whole (ws's frame parser)", async () => {
  const server = await bareServer({
    autoPong: true,
    onHello: (ws) => {
      const middle = 5;
      ws.send(HELLO.subarray(0, middle), { binary: true, fin: false });
      ws.ping(Buffer.from("between"));
      ws.send(HELLO.subarray(middle), { binary: true, fin: true });
    },
  });
  const transport = await ChannelTransport.open(server.url, null, 2000);
  await expect.poll(() => server.pongs.map((data) => data.toString())).toEqual(["between"]);
  await transport.close();
  await server.close();
});

test("a close code RFC 6455 does not allow on the wire is answered with 1002 and ends the channel (ws's validation)",
  async () => {
    const server = await bareServer({ autoPong: true });
    const transport = await ChannelTransport.open(server.url, null, 2000);
    const pending = transport.decideCalls([{ function: "jeff.support.triage", input: "x" }], "return")
      .then(() => null, (caught: unknown) => caught);
    // a close frame with code 1005, which only reports "no code" and is never sent: written raw, as ws refuses to send it
    const socket = (server.sockets[0] as unknown as { _socket: import("node:net").Socket })._socket;
    socket.write(Buffer.from([0x88, 0x02, 0x03, 0xed]));
    const error = await pending;
    expect(error).toBeInstanceOf(BackendUnavailable);
    expect((error as Error).message).toMatch(/the channel closed/);
    await expect.poll(() => server.closes).toEqual([1002]);
    await transport.close();
    await server.close();
  });
