/** contract/golden/step1.json against a real engine: `jeff runtime serve` running on this machine (it needs the
 * weights; run on the Mac or the RTX). JEFF_ENGINE_SOCKET is its socket; JEFF_GOLDEN_ADAPTERS the comma-separated
 * adapter IDs it serves whose golden cases to check. Both are required: a missing one fails the run. */

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { expect, test } from "vitest";
import { Jeff } from "../src/client.js";
import { repositoryRoot } from "../src/shipped.js";
import { type GoldenCase, goldenProblems } from "../src/testing.js";

const socket = process.env.JEFF_ENGINE_SOCKET;
const adapters = process.env.JEFF_GOLDEN_ADAPTERS;
if (!socket || !adapters) {
  throw new Error("The golden test needs JEFF_ENGINE_SOCKET (the socket of a running `jeff runtime serve`) and "
    + "JEFF_GOLDEN_ADAPTERS (comma-separated adapter IDs that engine serves, whose golden cases to check)");
}
const golden = JSON.parse(readFileSync(join(repositoryRoot(), "contract", "golden", "step1.json"), "utf8")) as { cases: GoldenCase[] };

test.each(adapters.split(",").map((adapter) => adapter.trim()))("golden cases of %s", async (adapter) => {
  const jeff = await Jeff.local(socket, { timeoutMs: 600000 });
  try {
    const cases = golden.cases.filter((item) => jeff.catalogue.function(item.call.function).adapterId === adapter);
    if (cases.length === 0) throw new Error(`contract/golden/step1.json has no case for ${adapter}`);
    const decisions = await jeff.decide(cases.map((item) => item.call as any));
    cases.forEach((item, index) => {
      const fn = jeff.catalogue.function(item.call.function);
      const calibration = jeff.calibrationOf(fn);
      expect(goldenProblems(item, decisions[index]!, calibration ? calibration.threshold : null), item.name).toEqual([]);
    });
  } finally {
    await jeff.close();
  }
});
