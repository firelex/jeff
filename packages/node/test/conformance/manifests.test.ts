/** contract/conformance/manifests.json: manifests every SDK refuses, read from their YAML text as a manifest file is
 * read (readYaml, then manifestFromDict), each refused with exactly the Python SDK's error. */

import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { afterAll, expect, test } from "vitest";
import { readYaml } from "../../src/contract.js";
import { JeffError } from "../../src/errors.js";
import { manifestFromDict } from "../../src/manifest.js";
import { fixture } from "./fixtures.js";

interface RefusedCase { name: string; path: string; yaml: string; expect: { error: { type: string; message: string } } }

const cases = fixture<{ cases: RefusedCase[] }>("manifests.json").cases;
const dir = mkdtempSync(join(tmpdir(), "jeff-sdk-refused-"));
afterAll(() => rmSync(dir, { recursive: true, force: true }));

test("the fixture has the non-finite cases, the refused aliases and the refused accesses", () => {
  expect(cases).toHaveLength(16);
});

test.each(cases.map((item) => [item.name, item] as const))("%s is refused with the Python SDK's error", (_name, item) => {
  const file = join(dir, `${item.name.replaceAll(/[^a-z0-9]+/gi, "_")}.yaml`);
  writeFileSync(file, item.yaml, "utf8");
  let caught: unknown = null;
  try {
    manifestFromDict(readYaml(file), item.path);
  } catch (error) {
    caught = error;
  }
  expect(caught).toBeInstanceOf(JeffError);
  const message = (caught as Error).message;
  if (item.expect.error.type === "ContractError") {
    // A schema refusal: this SDK's schema checker (Ajv) words each problem its own way, so the message is checked
    // for the Python SDK's first line and the field it names, /access.
    expect((caught as JeffError).type).toBe("ContractError");
    expect(message.split("\n")[0]).toBe(item.expect.error.message.split("\n")[0]);
    expect(message).toContain("at /access");
    return;
  }
  expect({ type: (caught as JeffError).type, message }).toEqual({ type: item.expect.error.type, message: item.expect.error.message });
});

test.each(["access: owner\n", "access: {accounts: [acct1, B-2_x]}\n"])("a manifest with %s is read (access is for a server only)", (access) => {
  const refused = cases.find((item) => item.name === "an access that is a string other than owner");
  expect(refused).toBeDefined();
  const file = join(dir, `access_${access.length}.yaml`);
  writeFileSync(file, refused!.yaml.replace("access: everyone\n", access), "utf8");
  expect(manifestFromDict(readYaml(file), "m.yaml").id).toBe("jeff.safety.spam");
});
