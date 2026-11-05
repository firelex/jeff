/** The engine socket protocol (contract/socket.md): each frame is a 4-byte big-endian length, then a MessagePack map.
 * Replies carry the request id and may arrive in any order (port of jeff/protocol.py). */

import { decode } from "@msgpack/msgpack";
import { validate } from "./contract.js";
import { ContractError, InvalidRequest, ProtocolMismatch } from "./errors.js";
import { encode } from "./msgpack.js";
import { pyRepr } from "./pyformat.js";

/** 2: the base served as adapter "base", and input_tokens in predict results. */
export const PROTOCOL_VERSION = 2;
export const MAX_FRAME = 64 * 1024 * 1024;
export const HEADER_SIZE = 4;
/** The server's low-latency channel (contract/socket.md, "The channel"): its path, the largest frame body it reads
 * (the HTTP API's body limit, 50 MiB) and the largest before the connection is authenticated. */
export const CHANNEL_PATH = "/v1/channel";
export const MAX_BODY = 50 * 1024 * 1024;
export const PREAUTH_MAX_BODY = 64 * 1024;

/** The socket schema's definition for each request op (contract/schemas/socket.schema.json). */
const OP_DEFINITIONS: Record<string, string> = {
  installed: "simple", reload: "simple", stats: "simple", health: "simple", predict: "predict", serve: "serve",
  auth: "auth", refresh: "auth", bind: "bind", call: "call", call_many: "call_many", release: "release",
  decide: "decide", dispatch: "dispatch", graph: "graph",
};

/** The one $defs entry of the socket schema a message can match, read from its tag (its op, or for a reply its ok);
 * null when the tag names no entry, and the whole schema is then checked. */
export function socketDefinition(message: unknown): string | null {
  if (message === null || typeof message !== "object" || Array.isArray(message)) return null;
  const record = message as Record<string, unknown>;
  if ("op" in record) {
    if (typeof record.op !== "string") return null;
    if (record.op === "hello") return "adapters_version" in record ? "engine_hello" : "hello";
    return Object.hasOwn(OP_DEFINITIONS, record.op) ? OP_DEFINITIONS[record.op] as string : null;
  }
  if (record.ok === true) return "ok_reply";
  if (record.ok === false) return "failure_reply";
  return null;
}

/** The message if it matches the socket schema (or, with `definition`, that one of its $defs); InvalidRequest, with
 * every problem listed, if not. */
export function validateMessage(message: unknown, where: string, definition?: string): Record<string, unknown> {
  const shape = definition ?? socketDefinition(message) ?? undefined;
  try {
    validate("socket", message, where, shape);
  } catch (error) {
    if (error instanceof ContractError) throw new InvalidRequest(error.message);
    throw error;
  }
  return message as Record<string, unknown>;
}

/** One frame: the message checked against the socket schema, packed (Maps in their insertion order), behind its
 * 4-byte length. InvalidRequest for a message that does not match, cannot be packed, or packs larger than 64 MiB. */
export function encodeFrame(message: Record<string, unknown>, definition?: string): Buffer {
  validateMessage(message, "outgoing socket message", definition);
  let payload: Uint8Array;
  try {
    payload = encode(message);
  } catch (error) {
    throw new InvalidRequest(`The outgoing socket message cannot be packed with msgpack: ${(error as Error).name}: ${(error as Error).message}`);
  }
  if (payload.length > MAX_FRAME) throw new InvalidRequest(`Socket message of ${payload.length} bytes is over the 64 MiB limit`);
  const header = Buffer.alloc(HEADER_SIZE);
  header.writeUInt32BE(payload.length);
  return Buffer.concat([header, payload]);
}

/** The body length a frame header announces; InvalidRequest over 64 MiB. */
export function frameLength(header: Uint8Array): number {
  const length = Buffer.from(header.buffer, header.byteOffset, HEADER_SIZE).readUInt32BE(0);
  if (length > MAX_FRAME) throw new InvalidRequest(`Socket frame of ${length} bytes is over the 64 MiB limit`);
  return length;
}

/** A frame body unpacked and checked against the socket schema. */
export function decodeBody(body: Uint8Array): Record<string, unknown> {
  let message: unknown;
  try {
    message = decode(body);
  } catch (error) {
    throw new InvalidRequest(`Socket frame is not valid msgpack: ${(error as Error).message}`);
  }
  return validateMessage(message, "incoming socket message");
}

/** Splits a byte stream into frame bodies. A header announcing more than 64 MiB is refused as soon as it is read:
 * push throws it, or, when frames before it came in the same chunk, returns those and keeps the error in `failure`
 * (every later push throws it). */
export class FrameReader {
  private chunks: Buffer[] = [];
  private size = 0;
  private expected: number | null = null;
  private broken: unknown = null;

  /** The error that ended the stream, once a push has returned the frames that came before it. */
  get failure(): unknown { return this.broken; }

  push(chunk: Buffer): Buffer[] {
    if (this.broken !== null) throw this.broken;
    this.chunks.push(chunk);
    this.size += chunk.length;
    const bodies: Buffer[] = [];
    for (;;) {
      if (this.expected === null) {
        if (this.size < HEADER_SIZE) break;
        try {
          this.expected = frameLength(this.take(HEADER_SIZE));
        } catch (error) {
          if (bodies.length === 0) throw error;
          this.broken = error;
          break;
        }
      }
      if (this.size < this.expected) break;
      bodies.push(this.take(this.expected));
      this.expected = null;
    }
    return bodies;
  }

  private take(count: number): Buffer {
    const all = this.chunks.length === 1 ? this.chunks[0] as Buffer : Buffer.concat(this.chunks, this.size);
    const out = all.subarray(0, count);
    const rest = all.subarray(count);
    this.chunks = rest.length > 0 ? [rest] : [];
    this.size = rest.length;
    return out;
  }
}

export function hello(adaptersVersion?: number): Record<string, unknown> {
  const message: Record<string, unknown> = { op: "hello", version: PROTOCOL_VERSION };
  if (adaptersVersion !== undefined) message.adapters_version = adaptersVersion;
  return message;
}

export function checkHello(message: Record<string, unknown>): void {
  if (message.op !== "hello") throw new ProtocolMismatch(`Expected a hello, got ${pyRepr(message.op)}`);
  if (message.version !== PROTOCOL_VERSION) {
    // Python formats the version with str(), not repr()
    const version = typeof message.version === "string" ? message.version : pyRepr(message.version);
    throw new ProtocolMismatch(`This side speaks protocol ${PROTOCOL_VERSION}, the other side ${version}; upgrade the older one`);
  }
}
