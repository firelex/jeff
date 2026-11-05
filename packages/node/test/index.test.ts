import { expect, test } from "vitest";
import * as sdk from "../src/index.js";
import * as testing from "../src/testing.js";

test("the package exports the SDK surface", () => {
  for (const name of ["Jeff", "BoundFunction", "RemoteJeff", "ChannelJeff", "Catalogue", "Decision", "GraphResult", "Skipped",
    "SocketEngine", "escalate", "functionStep", "llm", "human", "remote", "graphFromDict", "loadGraph", "JeffError",
    "NotInstalled", "LowConfidence", "EscalationFailed", "DEFAULT_ROUTER"]) {
    expect(sdk, name).toHaveProperty(name);
  }
  expect(testing).toHaveProperty("FakeEngine");
  expect(testing).toHaveProperty("goldenProblems");
});
