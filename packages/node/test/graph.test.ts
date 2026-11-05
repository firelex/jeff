import { expect, test } from "vitest";
import { Catalogue } from "../src/catalogue.js";
import { Jeff } from "../src/client.js";
import { GraphError } from "../src/errors.js";
import { type CodeFunction, type Graph, type GraphNode, graphFromDict, graphToDict, runGraph } from "../src/graph.js";
import { FakeEngine } from "../src/testing.js";

const doc = {
  graph: "jeff.graphs.t", version: "1.0.0", input: "message", params: ["channel"],
  nodes: { kind: { call: "spam.message_type", context: { channel: { param: "channel" } } },
    flag: { code: "flag", when: { node: "kind", in: ["spam", true] } } },
  outputs: ["kind", "flag"],
};

test("a graph document reads back the same after graphToDict", () => {
  const graph = graphFromDict(structuredClone(doc), "t");
  expect(graph.order).toEqual(["kind", "flag"]);
  expect(graphToDict(graph)).toEqual(doc);
});

test("a node naming an unknown node or param is refused", () => {
  const bad = structuredClone(doc) as any;
  bad.nodes.flag.after = ["ghost"];
  expect(() => graphFromDict(bad, "t")).toThrow(new GraphError("t: node flag refers to unknown node(s) ghost"));
  const undeclared = structuredClone(doc) as any;
  undeclared.params = [];
  expect(() => graphFromDict(undeclared, "t")).toThrow(new GraphError("t: node kind uses undeclared param(s) channel"));
});

test("a graph document read as Maps keeps its node order even for number-like node names", () => {
  const raw = new Map<string, unknown>([["graph", "jeff.graphs.t"], ["version", "1.0.0"], ["input", "message"],
    ["params", []], ["nodes", new Map([["b", new Map([["code", "b"]])], ["10", new Map([["code", "ten"]])],
      ["2", new Map([["code", "two"]])]])], ["outputs", ["b"]]]);
  const graph = graphFromDict(raw, "t");
  expect([...graph.nodes.keys()]).toEqual(["b", "10", "2"]);
  expect(graph.order).toEqual(["b", "10", "2"]);
});

test("a Graph whose order is not the order its dependencies give cannot be written", () => {
  const graph = graphFromDict(structuredClone(doc), "t");
  const reordered: Graph = { ...graph, order: ["flag", "kind"] };
  expect(() => graphToDict(reordered)).toThrow(new GraphError("jeff.graphs.t cannot be written as a graph document that "
    + "reads back the same: its order flag, kind is not the order its dependencies give (kind, flag)"));
});

test("a node with a policy the document cannot hold is refused by graphToDict", () => {
  const graph = graphFromDict(structuredClone(doc), "t");
  const nodes = new Map(graph.nodes);
  nodes.set("kind", { ...(graph.nodes.get("kind") as GraphNode), onLowConfidence: "escalate" });
  expect(() => graphToDict({ ...graph, nodes })).toThrow(new GraphError("jeff.graphs.t: node kind has on_low_confidence "
    + "'escalate'; a graph document holds only 'return' or 'error' (pass other policies in policies=)"));
});

const numberLike = (names: string[]) => new Map<string, unknown>([["graph", "jeff.graphs.t"], ["version", "1.0.0"],
  ["input", "message"], ["params", []], ["nodes", new Map(names.map((name) => [name, new Map([["code", `c${name}`]])]))],
  ["outputs", [names[0]]]]);

test("a graph whose number-like node names a plain object would reorder cannot be written", () => {
  const graph = graphFromDict(numberLike(["b", "10", "2"]), "t");
  expect(() => graphToDict(graph)).toThrow(new GraphError("jeff.graphs.t: node name(s) 10, 2 look like numbers, and a "
    + "plain object cannot keep their order (b, 10, 2); the graph can still be read and run from a Map document "
    + "(loadGraph), but this SDK cannot write it back"));
});

test("a graph whose number-like node names are already in plain-object order writes and reads back", () => {
  const graph = graphFromDict(numberLike(["1", "2", "a"]), "t");
  const written = graphToDict(graph);
  expect(Object.keys(written.nodes as object)).toEqual(["1", "2", "a"]);
  expect([...graphFromDict(written, "t").nodes.keys()]).toEqual(["1", "2", "a"]);
});

const codeOnlyDoc = { graph: "jeff.graphs.code", version: "1.0.0", input: "message", params: [],
  nodes: { value: { code: "makeValue" } }, outputs: ["value"] };

test("a code function returning undefined is a GraphError naming it and the node", async () => {
  const jeff = await Jeff.create(new FakeEngine(() => [], []), Catalogue.shipped());
  const graph = graphFromDict(structuredClone(codeOnlyDoc), "t");
  // CodeFunction's type says JSONValue (never undefined), but a plain JS function the SDK's own types cannot see
  // into (no explicit return, or one that forgot it) can still return undefined at runtime, so the cast here
  // stands in for that caller.
  const makeValue = (() => undefined) as unknown as CodeFunction;
  await expect(runGraph(jeff, graph, "hi", {}, { makeValue })).rejects.toThrow(
    new GraphError("jeff.graphs.code: code function makeValue (node value) returned undefined"));
});

test("a code function returning null stores it, as Python stores a code function's None", async () => {
  const jeff = await Jeff.create(new FakeEngine(() => [], []), Catalogue.shipped());
  const graph = graphFromDict(structuredClone(codeOnlyDoc), "t");
  const result = await runGraph(jeff, graph, "hi", {}, { makeValue: () => null });
  expect(result.outputs.get("value")).toBeNull();
  expect(result.nodes.get("value")).toBeNull();
});

test("a graph file with an integer past 2**53 is refused up front with a GraphError naming the field and why", async () => {
  const { mkdtempSync, writeFileSync } = await import("node:fs");
  const { tmpdir } = await import("node:os");
  const { join } = await import("node:path");
  const { loadGraph } = await import("../src/graph.js");
  const path = join(mkdtempSync(join(tmpdir(), "jeff-sdk-graph-")), "graph.yaml");
  writeFileSync(path, "graph: jeff.graphs.t\nversion: 1.0.0\ninput: message\nnodes:\n  kind:\n    call: spam.message_type\n"
    + "    when: {node: kind, in: [spam, 9007199254740993]}\noutputs: [kind]\n", "utf8");
  expect(() => loadGraph(path)).toThrow(new GraphError(`${path}: /nodes/kind/when/in/1 is 9007199254740993, an integer too `
    + "large to keep exactly as a JavaScript number (more than 2**53 from zero); this SDK refuses it: write a smaller number"));
});
