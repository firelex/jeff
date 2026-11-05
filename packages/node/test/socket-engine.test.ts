import { execFile } from "node:child_process";
import { createServer, type Socket } from "node:net";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";
import { decode } from "@msgpack/msgpack";
import { expect, test } from "vitest";
import { Catalogue } from "../src/catalogue.js";
import { Jeff, checkTimeoutMs } from "../src/client.js";
import { EngineError, InvalidRequest } from "../src/errors.js";
import { FrameReader, encodeFrame } from "../src/protocol.js";
import { SocketEngine } from "../src/socket-engine.js";
import { socketDirectory, startEngineServer } from "./helpers/engine-server.js";

const run = promisify(execFile);
const CTX = { company: "A shop.", channel: "email" };
const levelOf = (row: any) => [0, 0, 0, 0, 0].map((_, index) => (index === Number(row.state.message.at(-1)) ? 1 : 0));

test("Jeff.local decides over the engine socket", async () => {
  const server = await startEngineServer({ answers: (_adapter, row) => levelOf(row), installed: ["jeff.support.triage"] });
  const jeff = await Jeff.local(server.path, { timeoutMs: 5000 });
  const [decision] = await jeff.decide([{ function: "triage.urgency", input: "level 3", context: CTX }]);
  expect(decision?.value).toBe(3);
  await jeff.close();
  await server.close();
});

test("concurrent callers share one connection and each gets its own answer", async () => {
  const server = await startEngineServer({ answers: (_adapter, row) => levelOf(row), installed: ["jeff.support.triage"] });
  const jeff = await Jeff.local(server.path, { timeoutMs: 5000 });
  const inputs = Array.from({ length: 40 }, (_, index) => `case ${index % 5}`);
  const decisions = await Promise.all(inputs.map((input) =>
    jeff.decide([{ function: "triage.urgency", input, context: CTX }]).then(([decision]) => decision?.value)));
  expect(decisions).toEqual(inputs.map((input) => Number(input.at(-1))));
  await jeff.close();
  await server.close();
});

test("no engine on the socket names the path and the command that starts one", async () => {
  await expect(Jeff.local("/tmp/jeff-no-such-engine.sock", { timeoutMs: 1000 })).rejects.toThrow(
    /No engine is listening at \/tmp\/jeff-no-such-engine\.sock: .*Start one with: jeff runtime serve --socket \/tmp\/jeff-no-such-engine\.sock/);
  await expect(Jeff.local("/tmp/x.sock", { timeoutMs: 0 })).rejects.toThrow(InvalidRequest);
});

test("a call without a reply within the timeout fails, naming the engine", async () => {
  const server = await startEngineServer({ answers: () => [1, 0], installed: ["jeff.safety.spam"] });
  const engine = await SocketEngine.connect(server.path, { timeoutMs: 200 });
  server.stopAnswering();
  await expect(engine.predict("jeff.safety.spam", [{ state: "x", question: { type: "noul", instructions: "?" } }]))
    .rejects.toThrow(new EngineError(`No reply from the engine at ${server.path} within 200 ms`));
  await engine.close();
  await server.close();
});

test("after the engine restarts the next call reconnects once; a call right after a failed attempt fails at once", async () => {
  const directory = socketDirectory();
  try {
    const first = await startEngineServer({ answers: () => [0.2, 0.8], installed: ["jeff.safety.spam"], path: directory.path });
    const engine = await SocketEngine.connect(first.path, { timeoutMs: 2000 });
    const row = { state: "x", question: { type: "noul" as const, instructions: "?" } };
    await first.close();
    await new Promise((resolve) => setTimeout(resolve, 50));
    await expect(engine.predict("jeff.safety.spam", [row])).rejects.toThrow(/\(reconnect attempt\)/);
    await expect(engine.predict("jeff.safety.spam", [row])).rejects.toThrow(/the last reconnect attempt/);
    await new Promise((resolve) => setTimeout(resolve, 1100));
    const second = await startEngineServer({ answers: () => [0.2, 0.8], installed: ["jeff.safety.spam"], path: directory.path });
    expect((await engine.predict("jeff.safety.spam", [row])).probabilities).toEqual([[0.2, 0.8]]);
    expect(engine.reconnects).toBe(1);
    await engine.close();
    await second.close();
  } finally {
    directory.remove();
  }
});

test("a closed engine refuses later calls", async () => {
  const server = await startEngineServer({ answers: () => [1, 0], installed: [] });
  const engine = await SocketEngine.connect(server.path, { timeoutMs: 1000 });
  await engine.close();
  await expect(engine.health()).rejects.toThrow(/was closed by its caller/);
  await server.close();
  expect(Catalogue.shipped().manifests().length).toBeGreaterThan(0);
});

/** An engine that writes `onHello` after reading the hello and `onInstalled` after reading an `installed` request;
 * it counts connections and records each op it reads. */
async function rawEngine(onHello: Buffer, onInstalled?: (id: number) => Buffer) {
  const directory = socketDirectory();
  const ops: string[] = [];
  let connections = 0;
  const server = createServer((socket) => {
    connections += 1;
    socket.on("error", () => {});  // the client destroys the connection once it fails
    const reader = new FrameReader();
    socket.on("data", (chunk: Buffer) => {
      for (const body of reader.push(chunk)) {
        const message = decode(body) as { op: string; id: number };
        ops.push(message.op);
        if (message.op === "hello") socket.write(onHello);
        else if (message.op === "installed" && onInstalled !== undefined) socket.write(onInstalled(message.id));
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(directory.path, resolve));
  return {
    path: directory.path, ops, connections: () => connections,
    close: () => new Promise<void>((resolve) => server.close(() => { directory.remove(); resolve(); })),
  };
}

const frameOf = (message: Record<string, unknown>) => encodeFrame(message);
const ENGINE_HELLO = frameOf({ op: "hello", version: 2, adapters_version: 1 });
const ID_ZERO_FAILURE = frameOf({ id: 0, ok: false, error: { type: "InvalidRequest", message: "Frame could not be read",
  details: { adapters_version: 1 } } });

test("a failure with id 0 sent in the same bytes as the hello fails the connect with it, without a reconnect", async () => {
  const engine = await rawEngine(Buffer.concat([ENGINE_HELLO, ID_ZERO_FAILURE]));
  try {
    await expect(SocketEngine.connect(engine.path, { timeoutMs: 2000 })).rejects.toThrow(new EngineError(
      `The engine at ${engine.path} closed the connection after: InvalidRequest: Frame could not be read`));
    expect(engine.connections()).toBe(1);
  } finally {
    await engine.close();
  }
});

test("a connection that closes while connecting fails the connect at once, without a reconnect", async () => {
  const installedReply = (id: number) => Buffer.concat([
    frameOf({ id, ok: true, result: { adapters: ["base"], adapters_version: 1 } }), ID_ZERO_FAILURE]);
  const engine = await rawEngine(ENGINE_HELLO, installedReply);
  try {
    await expect(SocketEngine.connect(engine.path, { timeoutMs: 2000 })).rejects.toThrow(new EngineError(
      `The engine at ${engine.path} closed the connection after: InvalidRequest: Frame could not be read`));
    expect(engine.ops).toEqual(["hello", "installed"]);
    expect(engine.connections()).toBe(1);
  } finally {
    await engine.close();
  }
});

test("a refresh made during a reconnect attempt does not wait on that attempt (regression for a self-wait hang)",
  async () => {
    // Scenario from the dotnet SDK port's review (task-11-review.md, "Node SDK" section): during a reconnect
    // attempt, the attempt's own `health` call comes back with a different adapters_version, and in the very same
    // write the engine also sends an id-0 failure frame that ends the connection. Node delivers both frames from
    // one "data" event, so the health call's continuation (which refreshes the adapter list) always finds the
    // connection already closed, and must not start (or wait on) another reconnect attempt: it is already inside
    // one. Before the fix, it waited on the very attempt it is inside of, forever, and every later call joined the
    // same wait.
    const directory = socketDirectory();
    const sockets: Socket[] = [];
    let connections = 0;
    const server = createServer((socket) => {
      connections += 1;
      const connectionNumber = connections;
      sockets.push(socket);
      socket.on("error", () => {});
      const reader = new FrameReader();
      socket.on("data", (chunk: Buffer) => {
        for (const body of reader.push(chunk)) {
          const message = decode(body) as { op: string; id: number };
          if (message.op === "hello") {
            socket.write(ENGINE_HELLO);
          } else if (message.op === "installed") {
            socket.write(frameOf({ id: message.id, ok: true, result: { adapters: ["jeff.safety.spam"], adapters_version: 1 } }));
          } else if (message.op === "health") {
            const base = { format: "bf16", id: "base", version: "v1" };
            if (connectionNumber === 1) {
              socket.write(frameOf({ id: message.id, ok: true, result: { adapters_version: 1, base } }));
            } else {
              // The reconnect attempt's own health call: a bumped adapters_version and an id-0 failure, both in
              // one write, exactly as in the hang's reproduction.
              socket.write(Buffer.concat([
                frameOf({ id: message.id, ok: true, result: { adapters_version: 2, base } }),
                frameOf({ id: 0, ok: false, error: { type: "InvalidRequest", message: "boom", details: { adapters_version: 2 } } }),
              ]));
            }
          }
        }
      });
    });
    await new Promise<void>((resolve) => server.listen(directory.path, resolve));
    try {
      const engine = await SocketEngine.connect(directory.path, { timeoutMs: 2000 });
      // Simulate the engine restarting: end the connection the client is using.
      sockets[0].destroy();
      await new Promise((resolve) => setTimeout(resolve, 50));
      // This call finds the connection closed and starts a reconnect attempt. The attempt's own health call is the
      // one the fake engine answers with the bumped version plus the id-0 failure described above.
      await expect(engine.health(1000)).rejects.toThrow(/reconnect attempt/);
      // Without the fix, the call above never settles at all (no timer is running while it waits on the
      // self-wait), and this second call would join the same unresolved wait.
      await expect(engine.health(1000)).rejects.toThrow();
      await engine.close();
    } finally {
      server.close();
      directory.remove();
    }
  }, 3000);

test("timeoutMs over the longest Node timer is refused", async () => {
  expect(() => checkTimeoutMs(2147483647)).not.toThrow();
  expect(() => checkTimeoutMs(2147483648)).toThrow(new InvalidRequest("timeoutMs must be at most 2147483647 milliseconds "
    + "(about 24.8 days), the longest timeout Node keeps; got 2147483648"));
  await expect(Jeff.local("/tmp/x.sock", { timeoutMs: 3e9 })).rejects.toThrow(InvalidRequest);
});

test("a waiting call keeps the process alive although the idle socket does not", async () => {
  const server = await startEngineServer({ answers: () => [0.3, 0.7], installed: ["jeff.safety.spam"], predictDelayMs: 300 });
  try {
    const script = fileURLToPath(new URL("./helpers/one-call.ts", import.meta.url));
    const { stdout } = await run(process.execPath, ["--import", "tsx", script, server.path], { timeout: 20000 });
    expect(stdout.trim()).toBe("[[0.3,0.7]]");
  } finally {
    await server.close();
  }
});
