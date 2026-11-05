/** Python's own text forms and arithmetic, so that every message, reason and number this SDK produces is the one the
 * Python SDK (the reference) produces. contract/conformance pins them. */

const NOT_PRINTABLE = /^[\p{Cc}\p{Cf}\p{Cs}\p{Co}\p{Cn}\p{Zl}\p{Zp}\p{Zs}]$/u;

/** The characters Python's `\s` matches in a str pattern (str.isspace), as the body of a character class. */
export const PY_SPACE = "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";

function hex(code: number, width: number): string {
  return code.toString(16).padStart(width, "0");
}

/** repr() of a str: single quotes unless the text holds a single quote and no double quote. */
export function pyStr(text: string): string {
  const quote = text.includes("'") && !text.includes('"') ? '"' : "'";
  let out = quote;
  for (const char of text) {
    const code = char.codePointAt(0) as number;
    if (char === quote || char === "\\") out += `\\${char}`;
    else if (char === "\t") out += "\\t";
    else if (char === "\n") out += "\\n";
    else if (char === "\r") out += "\\r";
    else if (char !== " " && NOT_PRINTABLE.test(char)) {
      out += code <= 0xff ? `\\x${hex(code, 2)}` : code <= 0xffff ? `\\u${hex(code, 4)}` : `\\U${hex(code, 8)}`;
    } else out += char;
  }
  return out + quote;
}

/** repr() of a float: the shortest digits that read back the same, in scientific form below 1e-4 and from 1e16. */
export function pyFloat(value: number): string {
  if (Number.isNaN(value)) return "nan";
  if (value === Infinity) return "inf";
  if (value === -Infinity) return "-inf";
  if (value === 0) return Object.is(value, -0) ? "-0.0" : "0.0";
  const [mantissa, exponentText] = value.toExponential().split("e") as [string, string];
  const exponent = Number(exponentText);
  const negative = mantissa.startsWith("-");
  const digits = mantissa.replace("-", "").replace(".", "");
  let body: string;
  if (exponent < -4 || exponent >= 16) {
    const shown = digits.length > 1 ? `${digits[0]}.${digits.slice(1)}` : digits;
    body = `${shown}e${exponent < 0 ? "-" : "+"}${String(Math.abs(exponent)).padStart(2, "0")}`;
  } else if (exponent < 0) {
    body = `0.${"0".repeat(-exponent - 1)}${digits}`;
  } else if (digits.length <= exponent + 1) {
    body = `${digits}${"0".repeat(exponent + 1 - digits.length)}.0`;
  } else {
    body = `${digits.slice(0, exponent + 1)}.${digits.slice(exponent + 1)}`;
  }
  return negative ? `-${body}` : body;
}

/** format(value, f".{digits}f"). Number.toFixed rounds an exact half up; Python rounds it to even. An exact half at
 * the last digit happens exactly when value * 2**(digits + 1) is an odd integer. */
export function pyFixed(value: number, digits: number): string {
  if (!Number.isFinite(value)) return pyFloat(value);
  if (Math.abs(value) >= 1e21) {
    // Number.toFixed switches to exponent form here; every such double is a whole number, so print all its digits.
    const whole = BigInt(value).toString();
    return digits === 0 ? whole : `${whole}.${"0".repeat(digits)}`;
  }
  const scaled = value * 2 ** (digits + 1);
  if (Number.isSafeInteger(scaled) && scaled % 2 !== 0) {
    const twice = BigInt(scaled) * 5n ** BigInt(digits); // value * 10**digits * 2, an odd integer
    const negative = twice < 0n;
    let lower = ((negative ? -twice : twice) - 1n) / 2n;
    if (lower % 2n === 1n) lower += 1n;
    const text = lower.toString().padStart(digits + 1, "0");
    const body = digits === 0 ? text : `${text.slice(0, -digits)}.${text.slice(-digits)}`;
    return negative ? `-${body}` : body;
  }
  return value.toFixed(digits);
}

/** repr() of a JSON-like value. Whole numbers print as Python ints; use pyFloat where Python holds a float. */
export function pyRepr(value: unknown): string {
  if (value === null || value === undefined) return "None";
  if (typeof value === "boolean") return value ? "True" : "False";
  if (typeof value === "number") return Number.isInteger(value) && Math.abs(value) < 1e16 ? String(value) : pyFloat(value);
  if (typeof value === "string") return pyStr(value);
  if (Array.isArray(value)) return `[${value.map(pyRepr).join(", ")}]`;
  if (value instanceof Map) return `{${[...value].map(([k, v]) => `${pyRepr(k)}: ${pyRepr(v)}`).join(", ")}}`;
  if (typeof value === "object") {
    return `{${Object.entries(value).map(([k, v]) => `${pyStr(k)}: ${pyRepr(v)}`).join(", ")}}`;
  }
  return String(value);
}

/** sum() of a list of Python floats, as CPython 3.12 and later computes it (Neumaier's compensated summation).
 * CPython's real sum() only runs this compensated loop while every item stays a float; a non-float (such as a
 * plain Python int) breaks it, and the rest of the sum is added back in without compensation -- so a Python list
 * that mixes an int among floats, e.g. [1e100, 1, -1e100], can give a different (less accurate) answer than
 * summing the same values as floats throughout. JavaScript has no separate int type, so pySum always runs the
 * compensated loop and does not reproduce that particular loss; it is exact for a list of Python floats, which is
 * what every caller in this SDK passes it (probabilities, a score's weighted sum). */
export function pySum(values: readonly number[]): number {
  let total = 0;
  let compensation = 0;
  for (const value of values) {
    const next = total + value;
    if (Math.abs(total) >= Math.abs(value)) compensation += (total - next) + value;
    else compensation += (value - next) + total;
    total = next;
  }
  if (compensation !== 0 && Number.isFinite(compensation)) total += compensation;
  return total;
}

/** Python's ordering of str: by code point (JavaScript's default sort compares UTF-16 code units). */
export function compareCodePoints(a: string, b: string): number {
  const x = Array.from(a, (char) => char.codePointAt(0) as number);
  const y = Array.from(b, (char) => char.codePointAt(0) as number);
  for (let index = 0; index < Math.min(x.length, y.length); index += 1) {
    if (x[index] !== y[index]) return (x[index] as number) - (y[index] as number);
  }
  return x.length - y.length;
}

export function pySorted(values: Iterable<string>): string[] {
  return [...values].sort(compareCodePoints);
}

/** type(value).__name__ of the Python value JSON would give. */
export function pyTypeName(value: unknown): string {
  if (value === null || value === undefined) return "NoneType";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return Number.isInteger(value) ? "int" : "float";
  if (typeof value === "string") return "str";
  if (Array.isArray(value)) return "list";
  return "dict";
}

/** Python's == between decision values and condition values (str, bool, int): True == 1 and False == 0. */
export function pyEquals(a: unknown, b: unknown): boolean {
  const numeric = (value: unknown) => typeof value === "number" || typeof value === "boolean";
  if (numeric(a) && numeric(b)) return Number(a) === Number(b);
  return a === b;
}
