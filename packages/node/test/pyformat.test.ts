import { expect, test } from "vitest";
import { pyEquals, pyFixed, pyFloat, pyRepr, pySorted, pyStr, pySum, pyTypeName } from "../src/pyformat.js";

test("repr of a str picks Python's quotes and escapes", () => {
  expect(pyStr("plain")).toBe("'plain'");
  expect(pyStr("it's")).toBe(`"it's"`);
  expect(pyStr(`it's "x"`)).toBe(`'it\\'s "x"'`);
  expect(pyStr("a\nb\tc\\")).toBe("'a\\nb\\tc\\\\'");
  expect(pyStr("\u0007  ")).toBe("'\\x07\\xa0\\u2028'");
  expect(pyStr("café 💶")).toBe("'café 💶'");
});

test("repr of a float is Python's shortest form", () => {
  expect(pyFloat(1)).toBe("1.0");
  expect(pyFloat(0.1)).toBe("0.1");
  expect(pyFloat(0.771902)).toBe("0.771902");
  expect(pyFloat(1e-5)).toBe("1e-05");
  expect(pyFloat(0.0001)).toBe("0.0001");
  expect(pyFloat(1e16)).toBe("1e+16");
  expect(pyFloat(123456789.5)).toBe("123456789.5");
  expect(pyFloat(-2.5e-7)).toBe("-2.5e-07");
  expect(pyFloat(0)).toBe("0.0");
});

test("fixed digits round exact halves to even, as Python's format does", () => {
  expect(pyFixed(0.3125, 3)).toBe("0.312");
  expect(pyFixed(0.4375, 3)).toBe("0.438");
  expect(pyFixed(0.0625, 3)).toBe("0.062");
  expect(pyFixed(0.689444, 3)).toBe("0.689");
  expect(pyFixed(0.1, 3)).toBe("0.100");
});

test("fixed digits print nan, infinities and huge values as Python's format does", () => {
  expect(pyFixed(Number.NaN, 3)).toBe("nan");
  expect(pyFixed(Number.POSITIVE_INFINITY, 3)).toBe("inf");
  expect(pyFixed(Number.NEGATIVE_INFINITY, 2)).toBe("-inf");
  expect(pyFixed(1e21, 2)).toBe("1000000000000000000000.00");
  expect(pyFixed(-1.5e22, 0)).toBe("-15000000000000000000000");
  expect(pyFixed(2 ** 70, 1)).toBe("1180591620717411303424.0");
});

test("repr of containers and None", () => {
  expect(pyRepr(["a", 1, 2.5, true, null])).toBe("['a', 1, 2.5, True, None]");
  expect(pyRepr({ format: "bf16", format_id: "x" })).toBe("{'format': 'bf16', 'format_id': 'x'}");
});

test("sum uses Python's compensated summation", () => {
  const tenths = Array(10).fill(0.1);
  expect(tenths.reduce((a, b) => a + b, 0)).toBe(0.9999999999999999);
  expect(pySum(tenths)).toBe(1);
  // pySum is exact for a list of Python floats (what every caller passes it). [1e100, 1, -1e100] is not one: in
  // real Python, the "1" is an int, and CPython 3.13's sum() loses its compensated summation as soon as it meets a
  // non-float item, giving sum([1e100, 1, -1e100]) == 0.0 in Python -- not 1. JavaScript has no int/float
  // distinction, so pySum runs its compensated loop throughout and gives 1.0, the answer for the all-floats case.
  expect(pySum([1e100, 1, -1e100])).toBe(1.0);
  expect(pySum([])).toBe(0);
});

test("sorted compares code points", () => {
  expect(pySorted(["b", "a", "\u{1f4b6}", "￿"])).toEqual(["a", "b", "￿", "\u{1f4b6}"]);
});

test("type names and equality follow Python", () => {
  expect([null, true, 1, 1.5, "s", [], {}].map(pyTypeName)).toEqual(
    ["NoneType", "bool", "int", "float", "str", "list", "dict"]);
  expect(pyEquals(true, 1)).toBe(true);
  expect(pyEquals(false, 0)).toBe(true);
  expect(pyEquals(1, "1")).toBe(false);
  expect(pyEquals("a", "a")).toBe(true);
});
