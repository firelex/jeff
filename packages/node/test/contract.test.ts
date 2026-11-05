import { mkdtempSync, readdirSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, expect, test } from "vitest";
import { SCHEMAS, firstProblem, interfaces, isValid, loadSchema, readYaml, requireYamlString, validate } from "../src/contract.js";
import { ContractError, ManifestError } from "../src/errors.js";
import { repositoryRoot } from "../src/shipped.js";

/** A one-line YAML document (`key: <text>`), as a file readYaml can read. */
const yamlDirectories: string[] = [];
afterAll(() => {
  for (const dir of yamlDirectories) rmSync(dir, { recursive: true, force: true });
});

function yamlOf(text: string): string {
  const dir = mkdtempSync(join(tmpdir(), "jeff-sdk-yaml-"));
  yamlDirectories.push(dir);
  const path = join(dir, "doc.yaml");
  writeFileSync(path, `d: ${text}\n`, "utf8");
  return path;
}

test("every schema file is loaded and registered", () => {
  const files = readdirSync(join(repositoryRoot(), "contract", "schemas")).map((name) => name.replace(".schema.json", ""));
  expect([...SCHEMAS].sort()).toEqual(files.sort());
  for (const name of SCHEMAS) expect(loadSchema(name).$id).toBe(`https://jeff.dev/schemas/${name}.schema.json`);
});

test("validate names where the document is wrong", () => {
  expect(() => validate("decision", { value: 1 }, "a decision")).toThrow(ContractError);
  expect(() => validate("decision", { value: 1 }, "a decision")).toThrow(/^a decision does not match the decision schema/);
});

test("one definition of a schema can be checked, across schemas", () => {
  expect(isValid("socket", { op: "predict", id: 1, adapter: "a", rows: [{}] }, "predict")).toBe(true);
  expect(isValid("socket", { op: "predict", id: 1, adapter: "a", rows: [] }, "predict")).toBe(false);
  expect(isValid("decide_response", { decisions: [] })).toBe(true);
  expect(firstProblem("socket", { op: "predict", id: 1, adapter: "a" }, "predict")).toMatch(/rows/);
});

test("the interfaces have their question types and values", () => {
  const found = interfaces();
  expect(found.get("detect")?.questionType).toBe("noul");
  expect(found.get("equivalent")?.values).toEqual(["match", "possible_match", "no_match"]);
  expect(found.get("classify")?.values).toBeNull();
});

// firstProblem on the socket schema is rooted in a 15-way oneOf (one alternative per op). Picking the alternative
// the document comes closest to matching, rather than just resorting Ajv's flat (and, for a oneOf, misleading)
// error list, is what lets these name the one field actually wrong instead of some unrelated alternative's 'op'
// mismatch. Cases ported from the Python SDK's own first_problem tests (test_protocol.py, test_contract.py).
test.each([
  [{ op: "predict", id: 4, adapter: "a" }, "rows"],
  [{ op: "stats", id: 4, extra: 1 }, "extra"],
  [{ op: "nope", id: 3 }, "nope"],
])("firstProblem names the field actually wrong, not an unrelated alternative (%#)", (message, named) => {
  const problem = firstProblem("socket", message);
  expect(problem).toContain(named);
  expect(problem).not.toContain("\n");
  expect(problem.length).toBeLessThan(300);
});

test("firstProblem follows a nested oneOf (on_low_confidence's policy) to the alternative that matches closest", () => {
  const problem = firstProblem("decide_request", { calls: [{ function: "f", input: "x" }], on_low_confidence: "maybe" });
  expect(problem).toContain("on_low_confidence");
  expect(problem).toContain("maybe");
});

test("firstProblem prefers the oneOf alternative the document matched furthest into", () => {
  // Three calls each with a bad label are deep inside the "calls" shape; "dispatch" and "graph" only fail at the
  // top (a missing/unexpected top-level key), which is a shallower, less specific mismatch.
  const calls = [{ function: "f", input: "x", labels: 5 }, { function: "f", input: "x", labels: 5 }, { function: "f", input: "x", labels: 5 }];
  const problem = firstProblem("decide_request", { calls });
  expect(problem).toContain("labels");
  expect(problem).not.toContain("dispatch");
});

test("requireYamlString throws naming the missing field instead of coercing it to the text 'undefined'", () => {
  const spec = new Map<string, unknown>([["status", 500]]);
  expect(() => requireYamlString(spec, "meaning", "contract/errors.yaml: Example")).toThrow(ContractError);
  expect(() => requireYamlString(spec, "meaning", "contract/errors.yaml: Example")).toThrow(/'meaning' string/);
});

// readYaml must read an unquoted YAML date exactly as PyYAML does (a string, by its own isoformat(), never a JS
// Date, which cannot hold what PyYAML read without a day of information loss or outright corruption: see the block
// comment in contract.ts above readYaml for why).
test("readYaml reads an unquoted date as PyYAML's own ISO string, not a JS Date", () => {
  const document = readYaml(yamlOf("2026-10-03")) as Map<string, unknown>;
  expect(document.get("d")).toBe("2026-10-03");
});

test("readYaml keeps a quoted date a plain string", () => {
  const document = readYaml(yamlOf("'2026-10-03'")) as Map<string, unknown>;
  expect(document.get("d")).toBe("2026-10-03");
});

test("readYaml refuses an unquoted datetime (a time part), since a manifest date is never one", () => {
  expect(() => readYaml(yamlOf("2026-10-03T10:11:12Z"))).toThrow(ManifestError);
  expect(() => readYaml(yamlOf("2026-10-03T10:11:12Z"))).toThrow(/time part/);
});

test("readYaml refuses a day that is not on the calendar", () => {
  expect(() => readYaml(yamlOf("2026-02-30"))).toThrow(ManifestError);
  expect(() => readYaml(yamlOf("2026-02-30"))).toThrow(/real calendar date/);
});

test("readYaml reads a year below 100 exactly, without the Date constructor's +1900", () => {
  const document = readYaml(yamlOf("0099-01-01")) as Map<string, unknown>;
  expect(document.get("d")).toBe("0099-01-01");
});

// ---- YAML aliases: at most 1,000,000 values added by aliases (ALIAS_EXPANSION_LIMIT), as the other SDKs count them

/** A whole YAML document as a file readYaml can read. */
function yamlFile(text: string): string {
  const dir = mkdtempSync(join(tmpdir(), "jeff-sdk-yaml-"));
  yamlDirectories.push(dir);
  const path = join(dir, "doc.yaml");
  writeFileSync(path, text, "utf8");
  return path;
}

/** `levels` lines, each a list of 10 aliases to the line before (the first: 10 plain values). */
function laughs(levels: number): string {
  let text = "a0: &a0 [x, x, x, x, x, x, x, x, x, x]\n";
  for (let level = 1; level < levels; level++) {
    text += `a${level}: &a${level} [${Array(10).fill(`*a${level - 1}`).join(", ")}]\n`;
  }
  return text;
}

test("readYaml refuses aliases that expand past 1,000,000 values with a ManifestError naming the line", () => {
  // 8 levels: about 450 bytes that would expand to 11 million values; line 6 takes the count past the limit
  const path = yamlFile(laughs(8));
  expect(() => readYaml(path)).toThrow(new ManifestError(`${path} is not valid YAML: line 6: the aliases expand to more than `
    + "1,000,000 values; write the repeated parts out or use fewer aliases"));
});

test("readYaml reads aliases below the limit in full, past the yaml package's own default of 100", () => {
  // 5 levels: 123,440 values from aliases, which the yaml package's default guard refuses
  const read = readYaml(yamlFile(laughs(5))) as Map<string, unknown[]>;
  expect((read.get("a4") as unknown[][][][]).flat(3)).toHaveLength(10_000);
  // a harmless file with 60 aliases
  let text = "base: &base {kind: choice, values: [a, b, c]}\nitems:\n";
  for (let index = 0; index < 60; index++) text += `  - {name: n${index}, spec: *base}\n`;
  const items = (readYaml(yamlFile(text)) as Map<string, Array<Map<string, unknown>>>).get("items") as Array<Map<string, unknown>>;
  expect(items).toHaveLength(60);
  expect((items[59]?.get("spec") as Map<string, unknown>).get("values")).toEqual(["a", "b", "c"]);
});

test("readYaml refuses an alias that names no anchor before it with a ManifestError", () => {
  const path = yamlFile("a: *missing\nb: &missing 1\n");
  expect(() => readYaml(path)).toThrow(new ManifestError(`${path} is not valid YAML: line 1: the alias *missing names no `
    + "anchor before it"));
});

test("readYaml reads 1,000,000 aliases in time growing with their number, and refuses one more by the limit only", () => {
  const aliases = (count: number) => `a: &a 1\nb: [${Array(count).fill("*a").join(", ")}]\n`;
  const started = performance.now();
  const read = readYaml(yamlFile(aliases(1_000_000))) as Map<string, unknown[]>;
  const elapsed = performance.now() - started;
  expect(read.get("b")).toHaveLength(1_000_000);
  // the yaml package's parser takes about 2 s for these 5 MB; building the value adds little (the yaml package's own
  // toJS, with its alias guard off, took 7 s for 40,000 aliases and about 75 minutes for 1,000,000)
  expect(elapsed).toBeLessThan(15_000);
  const path = yamlFile(aliases(1_000_001));
  expect(() => readYaml(path)).toThrow(new ManifestError(`${path} is not valid YAML: line 2: the aliases expand to more than `
    + "1,000,000 values; write the repeated parts out or use fewer aliases"));
});

test("readYaml builds what the yaml package built: shared aliases, the last anchor definition, merges, repeated keys", () => {
  const read = readYaml(yamlFile("x: &a [1, 2]\ny: *a\nz: &a 3\nw: *a\nk: 1\nk: 2\nbase: &b {p: 1, q: 2}\n"
    + "m:\n  p: 9\n  <<: *b\n  r: 3\n")) as Map<string, unknown>;
  expect(read.get("y")).toBe(read.get("x")); // one value, as PyYAML shares it
  expect(read.get("w")).toBe(3);
  expect([...read.keys()]).toEqual(["x", "y", "z", "w", "k", "base", "m"]);
  expect(read.get("k")).toBe(2);
  expect([...(read.get("m") as Map<string, unknown>)]).toEqual([["p", 9], ["q", 2], ["r", 3]]);
  expect(readYaml(yamlFile(""))).toBeNull();
});

test("readYaml reads y, Y, n and N as strings and yes/no/on/off/true/false as booleans, as PyYAML does", () => {
  const read = readYaml(yamlFile("a: [y, Y, n, N, yes, No, on, OFF, true]\ny: 1\n")) as Map<string, unknown>;
  expect(read.get("a")).toEqual(["y", "Y", "n", "N", true, false, true, false, true]);
  expect(read.get("y")).toBe(1);
});

test("readYaml refuses a mapping key that is not a string, by line, as PyYAML and the other SDKs do", () => {
  for (const [text, key] of [["a: 1\n1: x\n", "1"], ["a: 1\ntrue: x\n", "True"], ["a: 1\n~: x\n", "None"],
    ["a: 1\n? [x, x]\n: y\n", "['x', 'x']"], ["a: 1\n{p: 1}: y\n", "{'p': 1}"]] as const) {
    const path = yamlFile(text);
    expect(() => readYaml(path), text).toThrow(new ManifestError(`${path} is not valid YAML: line 2: a mapping key that is not `
      + `a string (${key}); quote it`));
  }
  // a key that looks like a number but is quoted is a string
  expect([...(readYaml(yamlFile("'1': x\n")) as Map<string, unknown>).keys()]).toEqual(["1"]);
});

test("readYaml refuses an alias naming a collection it is inside, which has no JSON form, by line", () => {
  for (const text of ["a: &a [*a]\n", "a: &a 1\nb: &a [*a, *a]\n"]) {
    const path = yamlFile(text);
    expect(() => readYaml(path), text).toThrow(new ManifestError(`${path} is not valid YAML: line ${text.split("\n").length - 1}: `
      + "the alias *a names a collection it is inside, which has no JSON form; write the value out"));
  }
});

// ---- scalars as PyYAML's safe_load reads them (each expectation is what `uv run --with pyyaml python -c "import yaml;
// print(repr(yaml.safe_load(...)))"` printed)

/** The value of `a` in a one-line document `a: <text>`. */
const valueOf = (text: string) => (readYaml(yamlFile(`a: ${text}\n`)) as Map<string, unknown>).get("a");

test("readYaml reads 08, 09 and 019 as strings (not octal, not decimal) and 010 as octal 8, as PyYAML", () => {
  expect(valueOf("08")).toBe("08"); // PyYAML: '08'
  expect(valueOf("09")).toBe("09"); // '09'
  expect(valueOf("019")).toBe("019"); // '019'
  expect(valueOf("010")).toBe(8); // 8
  expect(valueOf("00")).toBe(0); // 0
  expect(valueOf("0o10")).toBe("0o10"); // '0o10' (YAML 1.1 has no 0o)
  expect(valueOf("0x1F")).toBe(31); // 31
  expect(valueOf("0b101")).toBe(5); // 5
  expect(valueOf("1_000")).toBe(1000); // 1000
  expect(valueOf("1:20")).toBe(80); // 80 (sexagesimal)
  expect(Object.is(valueOf("-0"), 0)).toBe(true); // 0
});

test("readYaml reads 1e5 and 1.0e5 as strings (PyYAML's float needs a dot and a signed exponent) and 1.0e+5 as a float", () => {
  expect(valueOf("1e5")).toBe("1e5"); // '1e5'
  expect(valueOf("1.0e5")).toBe("1.0e5"); // '1.0e5'
  expect(valueOf("1.0e+5")).toBe(100000); // 100000.0
  expect(valueOf("6.8523015e+5")).toBe(685230.15); // 685230.15
  expect(valueOf("190:20:30.15")).toBe(685230.15); // 685230.15 (sexagesimal)
  expect(valueOf("1_0.5")).toBe(10.5); // 10.5
  expect(valueOf("1.")).toBe(1); // 1.0
  expect(valueOf(".5")).toBe(0.5); // 0.5
  expect(Object.is(valueOf("-0.0"), -0)).toBe(true); // -0.0
});

test("readYaml reads .inf, -.inf and .nan as Python's inf, -inf and nan floats (JavaScript's Infinity and NaN)", () => {
  expect(valueOf(".inf")).toBe(Infinity); // inf
  expect(valueOf("-.inf")).toBe(-Infinity); // -inf
  expect(valueOf(".NaN")).toBeNaN(); // nan
  expect(valueOf(".nan")).toBeNaN(); // nan
});

test("readYaml keeps an integer past 2**53 exact, as PyYAML's int, as a bigint", () => {
  expect(valueOf("9007199254740993")).toBe(9007199254740993n); // 9007199254740993
  expect(valueOf("-9007199254740993")).toBe(-9007199254740993n); // -9007199254740993
  expect(valueOf("123456789012345678901234567890")).toBe(123456789012345678901234567890n);
  expect(valueOf("9007199254740991")).toBe(9007199254740991); // within 2**53: a number
});

test("readYaml refuses a tag PyYAML's safe loader cannot construct, and = as a value, and reads the core tags", () => {
  const refusal = (text: string) => {
    const path = yamlFile(`a: ${text}\n`);
    try {
      readYaml(path);
    } catch (error) {
      expect(error).toBeInstanceOf(ManifestError);
      return (error as Error).message.slice(path.length);
    }
    throw new Error(`readYaml read ${text}`);
  };
  // PyYAML: ConstructorError could not determine a constructor for the tag '!foo'
  expect(refusal("!foo bar")).toBe(" is not valid YAML: line 1: the tag !foo is not supported; write the value without a tag");
  // PyYAML: ConstructorError could not determine a constructor for the tag 'tag:yaml.org,2002:value'
  expect(refusal("=")).toBe(" is not valid YAML: line 1: the value key (=) is not supported; quote it");
  expect(refusal("[=]")).toBe(" is not valid YAML: line 1: the value key (=) is not supported; quote it");
  expect(refusal("!!binary aGk=")).toBe(" is not valid YAML: line 1: the tag !!binary is not supported; write the value "
    + "without a tag"); // PyYAML: b'hi', bytes, which JSON cannot hold
  expect(refusal("!!bool y")).toMatch(/'y' is not a boolean/); // PyYAML: KeyError 'y'
  expect(refusal("!!int 08")).toMatch(/'08' is not an integer/); // PyYAML: ValueError invalid literal for int() with base 8
  // as a key, = is the string "=" (PyYAML: {'=': 1})
  expect([...(readYaml(yamlFile("=: 1\n")) as Map<string, unknown>)]).toEqual([["=", 1]]);
  expect(valueOf("!!str 1")).toBe("1"); // '1'
  expect(valueOf("!!int '5'")).toBe(5); // 5
  expect(valueOf("!!float 1")).toBe(1); // 1.0
  expect(valueOf("!!float 1e5")).toBe(100000); // 100000.0 (an explicit !!float is Python's float(), which reads 1e5)
  expect(valueOf("!!bool On")).toBe(true); // True
  expect(valueOf("!!null x")).toBeNull(); // None
});

test("readYaml puts merged keys first, as PyYAML's flatten_mapping, whose own keys' values win", () => {
  const m = (text: string) => [...((readYaml(yamlFile(text)) as Map<string, unknown>).get("m") as Map<string, unknown>)];
  // PyYAML: {'m': {'p': 9, 'q': 2, 'r': 3}}
  expect(m("m:\n  p: 9\n  <<: {p: 1, q: 2}\n  r: 3\n")).toEqual([["p", 9], ["q", 2], ["r", 3]]);
  // PyYAML: {'m': {'a': 1, 'b': 2, 'z': 0}}: of a list, the last mapping's keys first, an earlier mapping's value wins
  expect(m("m:\n  z: 0\n  <<: [{a: 1}, {a: 2, b: 2}]\n")).toEqual([["a", 1], ["b", 2], ["z", 0]]);
  // PyYAML: {'m': {'y': 2, 'x': 1}}
  expect(m("m:\n  <<: [{x: 1}, {y: 2}]\n")).toEqual([["y", 2], ["x", 1]]);
});
