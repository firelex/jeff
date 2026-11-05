/** MessagePack as the Python SDK packs it (msgpack.packb(value, use_bin_type=True)), with Maps written as maps in
 * their insertion order. @msgpack/msgpack writes a Map as an empty map (it reads only own enumerable properties), and a
 * plain object moves integer-like keys such as "10" first, so rows keep their trained key order only as Maps.
 * Scalars, strings and binary are written by @msgpack/msgpack, whose choice of format matches msgpack-python's. */

import { encode as encodeScalar } from "@msgpack/msgpack";

function header(size: number, fix: number, fixLimit: number, code16: number, code32: number, what: string): Uint8Array {
  if (size < fixLimit) return Uint8Array.of(fix + size);
  if (size < 0x10000) return Uint8Array.of(code16, size >> 8, size & 0xff);
  if (size < 0x100000000) return Uint8Array.of(code32, (size >>> 24) & 0xff, (size >>> 16) & 0xff, (size >>> 8) & 0xff, size & 0xff);
  throw new Error(`Too large ${what} to pack: ${size}`);
}

function encodeInto(value: unknown, chunks: Uint8Array[]): void {
  if (value === undefined) throw new Error("Cannot pack undefined: MessagePack rows hold JSON values only");
  if (value instanceof Map) {
    chunks.push(header(value.size, 0x80, 16, 0xde, 0xdf, "map"));
    for (const [key, item] of value) {
      if (typeof key !== "string") throw new Error(`Cannot pack a map key that is not a string: ${String(key)}`);
      chunks.push(encodeScalar(key));
      encodeInto(item, chunks);
    }
    return;
  }
  if (Array.isArray(value)) {
    chunks.push(header(value.length, 0x90, 16, 0xdc, 0xdd, "array"));
    for (const item of value) encodeInto(item, chunks);
    return;
  }
  if (value !== null && typeof value === "object" && !ArrayBuffer.isView(value)) {
    const entries = Object.entries(value);
    chunks.push(header(entries.length, 0x80, 16, 0xde, 0xdf, "map"));
    for (const [key, item] of entries) {
      chunks.push(encodeScalar(key));
      encodeInto(item, chunks);
    }
    return;
  }
  chunks.push(encodeScalar(value));
}

/** The value packed as MessagePack bytes; Maps keep their insertion order. */
export function encode(value: unknown): Uint8Array {
  const chunks: Uint8Array[] = [];
  encodeInto(value, chunks);
  return new Uint8Array(Buffer.concat(chunks));
}
