import { expect, test } from "vitest";
import { Catalogue } from "../src/catalogue.js";
import { ManifestError, UnknownFunction } from "../src/errors.js";
import { parseRef, versionMatches } from "../src/ids.js";
import { calibrationFor, codeNameOf, manifestFromDict, unversionedId } from "../src/manifest.js";

test.each([
  ["channel", "channel"], ["counterparty name", "counterparty_name"], ["Deal ID", "deal_id"],
  ["outreach  log -- v2", "outreach_log_v2"], ["_odd_", "odd"], ["clause type?", "clause_type"],
  ["company_", "company_"], ["a__b", "a__b"], ["x9_", "x9_"],
])("the code name of %j is %j", (key, code) => {
  expect(codeNameOf(key)).toBe(code);
});

test.each([
  ["jeff.example.router-v14-fast", "jeff.example.router-fast"],
  ["jeff.routing.router-open-v14", "jeff.routing.router-open"], ["jeff.v2.router-v3", "jeff.v2.router"],
  ["jeff.support.triage", null], ["jeff.routing.v14", null], ["jeff.routing.router-v14-v15", null],
  ["jeff.routing.router-v", null], ["jeff.routing.router-v1x", null], ["jeff.routing.router-14", null],
  ["jeff.routing.router-open-v14\n", null],
])("the unversioned ID of %j is %j, as in Python", (adapterId, unversioned) => {
  expect(unversionedId(adapterId)).toBe(unversioned);
});

test("references parse as in Python", () => {
  expect(parseRef("jeff.safety.spam.is_spam@1.3")).toEqual({ adapter: "jeff.safety.spam", key: "is_spam", version: "1.3" });
  expect(parseRef("spam")).toEqual({ adapter: "spam", key: null, version: null });
  expect(() => parseRef("jeff.x")).toThrow("Malformed reference 'jeff.x': use jeff.<domain>.<name>[.<function>]");
  expect(versionMatches("1.3", "1.3.0")).toBe(true);
  expect(versionMatches("1.30", "1.3.0")).toBe(false);
});

test("a numeric-looking fixed label key keeps its YAML order", () => {
  const raw = structuredClone(Catalogue.shipped().adapter("jeff.safety.spam").raw) as any;
  raw.functions.message_type.labels.fixed = new Map([["2", "Two"], ["1", "One"], ["legit", "Fine"]]);
  const manifest = manifestFromDict(raw, "test");
  expect([...(manifest.functions.get("message_type")?.labels.fixed.keys() ?? [])]).toEqual(["2", "1", "legit"]);
});

test("a key with no usable code name must declare one", () => {
  const raw = structuredClone(Catalogue.shipped().adapter("jeff.safety.spam").raw) as any;
  raw.context = [{ key: "2nd party", description: "A field.", required: true }];
  expect(() => manifestFromDict(raw, "test")).toThrow(ManifestError);
  expect(() => manifestFromDict(raw, "test")).toThrow(/2nd party.*code_name/);
});

test("the catalogue resolves aliases, keys and versions", () => {
  const catalogue = Catalogue.shipped();
  expect(catalogue.function("spam.is_spam").id).toBe("jeff.safety.spam.is_spam");
  expect(() => catalogue.function("spam")).toThrow(UnknownFunction);
  expect(catalogue.implementing("equivalent").map((fn) => fn.id)).toEqual(["jeff.compliance.sanctions.screening"]);
  expect(calibrationFor(catalogue.function("spam.is_spam"), null)?.threshold).toBeGreaterThan(0);
});

test("tests.id_patterns must each be a syntactically valid regular expression", () => {
  const raw = structuredClone(Catalogue.shipped().adapter("jeff.safety.spam").raw) as any;
  raw.tests.function_by = "id_pattern";
  raw.tests.id_patterns = { is_spam: "[", message_type: ".*" };
  expect(() => manifestFromDict(raw, "test")).toThrow(ManifestError);
  expect(() => manifestFromDict(raw, "test")).toThrow(/id_patterns is_spam.*not a regular expression/);
});

test("Catalogue.base looks up a version with Object.hasOwn, refusing an inherited property name", () => {
  const catalogue = Catalogue.shipped();
  expect(catalogue.base("jeff-base", "v1.4").repo).toBe("mstrasser/jeff-base");
  expect(() => catalogue.base("jeff-base", "v9")).toThrow(ManifestError);
  expect(() => catalogue.base("jeff-base", "toString")).toThrow(ManifestError);
  expect(() => catalogue.base("jeff-base", "constructor")).toThrow(ManifestError);
});

test("a manifest integer past 2**53 (a bigint from readYaml) is refused up front, naming the field and why", async () => {
  const { mkdtempSync, readFileSync, writeFileSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { repositoryRoot } = await import("../src/shipped.js");
  const text = readFileSync(join(repositoryRoot(), "catalogue", "jeff.safety.spam", "manifest.yaml"), "utf8")
    .replace("\n      rows: 1113\n", "\n      rows: 12345678901234567890\n");
  const path = join(mkdtempSync(join(tmpdir(), "jeff-sdk-big-")), "manifest.yaml");
  writeFileSync(path, text, "utf8");
  const { loadManifest } = await import("../src/manifest.js");
  const { ManifestError } = await import("../src/errors.js");
  expect(() => loadManifest(path)).toThrow(new ManifestError(`${path}: /functions/is_spam/calibration/rows is `
    + "12345678901234567890, an integer too large to keep exactly as a JavaScript number (more than 2**53 from zero); this "
    + "SDK refuses it: write a smaller number"));
});

test("a number that is not finite is refused before the schema check, wherever it is, naming its place and value", async () => {
  const { manifestFromDict } = await import("../src/manifest.js");
  const { ManifestError } = await import("../src/errors.js");
  const raw = new Map<string, unknown>([["changelog", [new Map([["note", -Infinity]])]]]);
  expect(() => manifestFromDict(raw, "x")).toThrow(new ManifestError("x: /changelog/0/note is -inf, not a finite number, "
    + "which JSON cannot hold; write a finite number"));
});
