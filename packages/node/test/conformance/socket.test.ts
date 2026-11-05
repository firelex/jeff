import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { expect, test } from "vitest";
import { JeffError } from "../../src/errors.js";
import { pySorted } from "../../src/pyformat.js";
import { SocketEngine } from "../../src/socket-engine.js";
import { fixture, fromBase64, toBase64 } from "./fixtures.js";

type Step = { client: string } | { engine: string } | { close: true };
interface SocketCase { name: string; actions: any[]; steps: Step[]; results: unknown[] }

/** Reads one raw frame (header and body) at a time; null when the client closed, "<timeout>" after 5 s. */
function frames(socket: Socket) {
  let buffer = Buffer.alloc(0);
  let ended = false;
  let wake: () => void = () => {};
  socket.on("data", (chunk) => { buffer = Buffer.concat([buffer, chunk]); wake(); });
  socket.on("end", () => { ended = true; wake(); });
  return async (): Promise<Buffer | null | "<timeout>"> => {
    const deadline = Date.now() + 5000;
    for (;;) {
      if (buffer.length >= 4 && buffer.length >= 4 + buffer.readUInt32BE(0)) {
        const length = 4 + buffer.readUInt32BE(0);
        const out = buffer.subarray(0, length);
        buffer = buffer.subarray(length);
        return out;
      }
      if (ended) return null;
      const left = deadline - Date.now();
      if (left <= 0) return "<timeout>";
      await new Promise<void>((resolve) => { wake = resolve; setTimeout(resolve, left); });
    }
  };
}

/** An engine that plays the case's steps on the first connection and records every frame it reads. */
async function scripted(steps: Step[]) {
  const directory = mkdtempSync(join(tmpdir(), "jeffconf"));
  const path = join(directory, "e.sock");
  const received: string[] = [];
  let finished!: () => void;
  const done = new Promise<void>((resolve) => { finished = resolve; });
  const server = createServer(async (socket) => {
    socket.on("error", () => {});  // the client may reset the connection once the script is over
    const next = frames(socket);
    for (const step of steps) {
      if ("client" in step) {
        const got = await next();
        received.push(got === null || got === "<timeout>" ? String(got) : toBase64(got));
        if (got === null || got === "<timeout>") break;
      } else if ("engine" in step) socket.write(fromBase64(step.engine));
      else { socket.end(); break; }
    }
    finished();
  });
  await new Promise<void>((resolve) => server.listen(path, resolve));
  return {
    path, received, done,
    close: () => new Promise<void>((resolve) => server.close(() => {
      rmSync(directory, { recursive: true, force: true });
      resolve();
    })),
  };
}

function rowsOf(action: any): any[] {
  if ("rows" in action) return action.rows;
  return [{ state: "x".repeat(action.rows_generated.state_length), question: action.rows_generated.question }];
}

async function act(engine: SocketEngine, action: any): Promise<unknown> {
  if (action.op === "predict") {
    const result = await engine.predict(action.adapter, rowsOf(action), action.deadline_ms);
    return { probabilities: result.probabilities, timing: result.timing, input_tokens: result.inputTokens };
  }
  if (action.op === "installed") return { installed: pySorted(await engine.installed(true)) };
  if (action.op === "health") return engine.health();
  throw new Error(`Unknown socket action ${action.op}`);
}

const cases = fixture<{ cases: SocketCase[] }>("socket.json").cases;

test.each(cases.map((item) => [item.name, item] as const))("%s", async (_name, item) => {
  const server = await scripted(item.steps);
  const failed = (error: unknown) => {
    if (!(error instanceof JeffError)) throw error;
    return { error: { type: error.type, message: error.message.replaceAll(server.path, "{socket}") } };
  };
  const results: unknown[] = [];
  let engine: SocketEngine | null = null;
  try {
    try {
      engine = await SocketEngine.connect(server.path, { timeoutMs: 5000 });
      results.push({ result: { base_format: engine.baseFormat, base: engine.base, installed: pySorted(await engine.installed()) } });
    } catch (error) {
      results.push(failed(error));
    }
    let index = 1;
    while (engine !== null && index < item.actions.length) {
      const group = [index];
      while (item.actions[group.at(-1)!]?.concurrent && item.actions[group.at(-1)! + 1]?.concurrent) group.push(group.at(-1)! + 1);
      const running = group.map((member) => act(engine as SocketEngine, item.actions[member]).then((result) => ({ result }), failed));
      results.push(...(await Promise.all(running)));
      index = group.at(-1)! + 1;
    }
    await engine?.close();
    await server.done;
  } finally {
    await server.close();
  }
  expect(results).toEqual(item.results);
  expect(server.received).toEqual(item.steps.flatMap((step) => ("client" in step ? [step.client] : [])));
});
