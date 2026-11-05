import { mkdtempSync, rmSync } from "node:fs";
import { createServer, type Socket } from "node:net";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { decode } from "@msgpack/msgpack";
import { encode } from "../../src/msgpack.js";

function frame(message: unknown): Buffer {
  const body = Buffer.from(encode(message));
  const header = Buffer.alloc(4);
  header.writeUInt32BE(body.length);
  return Buffer.concat([header, body]);
}

export interface EngineServer { path: string; close(): Promise<void>; stopAnswering(): void }

/** A fake engine on a Unix socket: answers hello, installed, health and predict (with `answers`), like
 * `jeff runtime serve` over a fake model; with predictDelayMs, each predict is answered that long after it arrives. Without `path` it listens in a temporary directory of its own, removed by
 * close(); with `path` the caller owns that path's directory. */
export async function startEngineServer(options: { answers: (adapter: string, row: any) => number[];
  installed: string[]; path?: string; predictDelayMs?: number }): Promise<EngineServer> {
  const ownDirectory = options.path === undefined ? mkdtempSync(join(tmpdir(), "jeffnode")) : null;
  const path = options.path ?? join(ownDirectory as string, "e.sock");
  const sockets = new Set<Socket>();
  let answering = true;
  const server = createServer((socket) => {
    sockets.add(socket);
    socket.on("close", () => sockets.delete(socket));
    let buffer = Buffer.alloc(0);
    socket.on("data", (chunk) => {
      buffer = Buffer.concat([buffer, chunk]);
      while (buffer.length >= 4 && buffer.length >= 4 + buffer.readUInt32BE(0)) {
        const length = buffer.readUInt32BE(0);
        const message = decode(buffer.subarray(4, 4 + length)) as any;
        buffer = buffer.subarray(4 + length);
        const version = 1;
        if (message.op === "hello") socket.write(frame({ op: "hello", version: 2, adapters_version: version }));
        else if (message.op === "installed") {
          socket.write(frame({ id: message.id, ok: true, result: { adapters: ["base", ...options.installed], adapters_version: version } }));
        } else if (message.op === "health") {
          socket.write(frame({ id: message.id, ok: true, result: { status: "ready", backend: "fake", adapters: options.installed,
            adapters_version: version, base: { id: "jeff-base", version: "v1.3", checkpoint: "fake", max_options: 252, format: "bf16" } } }));
        } else if (message.op === "predict" && answering) {
          const reply = frame({ id: message.id, ok: true, result: {
            probabilities: message.rows.map((row: any) => options.answers(message.adapter, row)),
            timing: { path: "fake", model_ms: 0.5, format: "bf16" }, adapters_version: version } });
          if (options.predictDelayMs === undefined) socket.write(reply);
          else setTimeout(() => socket.write(reply), options.predictDelayMs);
        }
      }
    });
  });
  await new Promise<void>((resolve) => server.listen(path, resolve));
  return {
    path,
    stopAnswering: () => { answering = false; },
    close: () => new Promise<void>((resolve) => {
      for (const socket of sockets) socket.destroy();
      server.close(() => {
        if (ownDirectory !== null) rmSync(ownDirectory, { recursive: true, force: true });
        resolve();
      });
    }),
  };
}

/** A temporary directory for a socket path the test owns (and removes with `remove`). */
export function socketDirectory(): { path: string; remove(): void } {
  const directory = mkdtempSync(join(tmpdir(), "jeffnode"));
  return { path: join(directory, "e.sock"), remove: () => rmSync(directory, { recursive: true, force: true }) };
}
