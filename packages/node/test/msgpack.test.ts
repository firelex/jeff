import { encode as libraryEncode } from "@msgpack/msgpack";
import { expect, test } from "vitest";
import { encode } from "../src/msgpack.js";

test("a Map keeps its insertion order, number-like keys included", () => {
  const packed = encode(new Map<string, unknown>([["b", 1], ["10", 2]]));
  // fixmap of 2: "b" -> 1, "10" -> 2 (a plain object would put "10" first)
  expect([...packed]).toEqual([0x82, 0xa1, 0x62, 0x01, 0xa2, 0x31, 0x30, 0x02]);
});

test("everything else packs as @msgpack/msgpack packs it, map and array sizes past the fix formats included", () => {
  const big = Object.fromEntries(Array.from({ length: 20 }, (_, index) => [`k${index}`, index * 1.5]));
  const value = { text: "é".repeat(40), list: Array.from({ length: 17 }, (_, index) => index - 8), big, none: null, yes: true };
  expect(encode(value)).toEqual(libraryEncode(value));
  expect(encode(new Map(Object.entries(big)))).toEqual(libraryEncode(big));
});

test("undefined is refused, not packed as nil", () => {
  expect(() => encode({ field: undefined })).toThrow("Cannot pack undefined");
});
