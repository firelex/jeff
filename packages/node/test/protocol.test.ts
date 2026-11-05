import { afterEach, expect, test, vi } from "vitest";
import { InvalidRequest } from "../src/errors.js";
import { FrameReader, decodeBody, encodeFrame } from "../src/protocol.js";

afterEach(() => { vi.restoreAllMocks(); });

const INSTALLED = { op: "installed", id: 1 };
const HEALTH = { op: "health", id: 2 };

test("a header split byte by byte gives the frame once it is whole", () => {
  const frame = encodeFrame(INSTALLED);
  const reader = new FrameReader();
  const bodies: Buffer[] = [];
  for (const byte of frame) bodies.push(...reader.push(Buffer.of(byte)));
  expect(bodies.map(decodeBody)).toEqual([INSTALLED]);
});

test("two frames in one chunk are both read", () => {
  const reader = new FrameReader();
  expect(reader.push(Buffer.concat([encodeFrame(INSTALLED), encodeFrame(HEALTH)])).map(decodeBody)).toEqual([INSTALLED, HEALTH]);
});

test("an 8 MiB body in 64 KiB chunks is read with one concatenation, not one per chunk", () => {
  const rows = [{ state: "x".repeat(8 * 1024 * 1024), question: { type: "noul", instructions: "?" } }];
  const message = { op: "predict", adapter: "jeff.safety.spam", rows, id: 3 };
  const frame = encodeFrame(message);
  const reader = new FrameReader();
  const concat = vi.spyOn(Buffer, "concat");
  const bodies: Buffer[] = [];
  for (let start = 0; start < frame.length; start += 64 * 1024) bodies.push(...reader.push(frame.subarray(start, start + 64 * 1024)));
  expect(concat.mock.calls.length).toBeLessThanOrEqual(2);
  concat.mockRestore();
  expect(bodies.length).toBe(1);
  expect(decodeBody(bodies[0] as Buffer)).toEqual(message);
});

test("frames before an oversized header in the same chunk are returned, and the header's error is kept", () => {
  const reader = new FrameReader();
  const oversized = Buffer.alloc(4);
  oversized.writeUInt32BE(64 * 1024 * 1024 + 1);
  expect(reader.push(Buffer.concat([encodeFrame(INSTALLED), oversized])).map(decodeBody)).toEqual([INSTALLED]);
  const refused = new InvalidRequest("Socket frame of 67108865 bytes is over the 64 MiB limit");
  expect(reader.failure).toEqual(refused);
  expect(() => reader.push(Buffer.of(0))).toThrow(refused);
});
