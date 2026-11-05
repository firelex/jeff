---
title: Graphs
---
# Graphs

A **decision graph** wires several functions together with conditions written as data: a node runs only when the
results it depends on meet those conditions. The graph document format (nodes, `params`, `when` conditions, outputs) is
described in [Graphs](/docs/guides/graphs#section-writing-a-graph); this page shows how to load and run one.

This graph checks an incoming email: first whether it is spam, then, only for a legitimate email, whether it is
trying to take control of the AI model that will draft a reply, and, only for such an attempt, what kind of attempt it
is. A code node, `choose_reply`, then decides what to do with it:

<!-- snippet: sdk/graphs-message-check.yaml -->

A node's `context` and `labels` read the graph's `params`, which the caller supplies once per run. A code node names a
function of your own that you pass when you run the graph.

## Loading a graph

`load_graph(path)` reads a graph document and returns a `Graph` (`loadGraph` in Node, `jeff_sdk::graph::load_graph`
in Rust, `Graphs.Load` in C#, `Graphs.load` in Java). It checks the document against the graph schema, that every
node it refers to exists, that every param it reads is declared, and that the nodes form no cycles; any problem raises
`GraphError`. Loading needs no server or model:

<!-- snippet: sdk/graphs-1 -->

A `Graph` has `id`, `version`, `input` (the name of its input), `params`, `nodes`, `outputs`, and `order`: every node
listed after the nodes it depends on. A graph you already hold as a document is read the same way by `graphFromDict` in
Node, `graph_from_json` in Rust, `Graphs.FromDict` in C# and `Graphs.fromMap` in Java.

## Running a graph

`client.run_graph(graph, input, params, code=None, policies=None)` runs a graph and returns a `GraphResult`.

- `graph` is a `Graph`, the path of a graph document, or a graph document.
- `input` is the input every model node is called with.
- `params` gives a value for each of the graph's params; a missing one raises `GraphError`.
- `code` maps each code node's name to a function of your own. The function is given the graph's `input`, its
  `params` and `nodes` (the results so far, by node name), and returns any JSON value; in Node, Rust and C# it may be
  async. A code node without its function, or a function no node uses, raises `GraphError`.
- `policies` maps node names to an `on_low_confidence` policy (see [Escalation](/docs/sdk/escalation)), overriding
  the node's own `on_low_confidence` from the document.

Before anything is decided, the whole graph is checked: every function exists, is installed and accepts its context
and labels, and every policy is valid. Then each round runs every node whose dependencies have completed, with the model
nodes of a round decided together.

<!-- snippet: sdk/graphs-2 -->

Through the API, a graph with no code nodes, whose policies the server can run, is sent as one request and runs on the
server. A graph with code nodes runs from your process, one request per round that has model nodes, because the code
can only run where it is defined. [Through the API](/docs/guides/remote#section-what-costs-a-request) has the details.
A graph registered on the server runs there by its ID: `client.run_server_graph(graph_id, input, params,
policies=None)` (`runServerGraph` in Node and Java, `run_server_graph` in Rust, `RunServerGraphAsync` in C#; through
the API only).

## The result

A `GraphResult` has:

- `nodes`: every node's result, by name;
- `outputs`: the results of the nodes the graph lists as outputs;
- `stages`: how many rounds the run took.

A model node's result is a `Decision`; a code node's is whatever its function returned. A node whose `when`
condition did not hold, or that depends on a skipped node, is `Skipped`, whose `reason` explains why (for example
`condition not met`, or `depends on skipped node attack`). In Rust the three are the variants of `NodeResult`
(`Decision`, `Skipped`, `Code`), in C# the records `DecisionResult`, `SkippedResult` and `CodeResult`, and in Java
`NodeResult.Decided`, `NodeResult.Skipped` and `NodeResult.Code`. `result.to_json()` gives the same JSON as the
server's `graph_result`, and `GraphResult.from_json(data)` reads it back.

## Node names that look like numbers, in Node

Avoid graphs whose node names look like whole numbers (`"2"`, `"10"`) in Node because of JavaScript's
key order (a plain object lists such keys first, in numeric order). Every other SDK keeps the document's order for
such names.

- `loadGraph` preserves the original order, and such a graph runs as it should on your machine.
- `graphToDict(graph)`, which writes a `Graph` back out as a document, raises `GraphError`, naming the node, when
  putting that node first, as a plain object would, would change the graph.
- Through the API, a graph without code nodes is sent to the server by writing it out with `graphToDict`, so such a
  graph is refused there with the same `GraphError`, even though it runs locally.
- The `nodes` object a code node's function is given, and the objects `result.toJSON()` returns, list such names
  first. Read the run order from the graph's `order`, not from these objects.

Ordinary node names, such as the ones above, are not affected.

## Next steps

- [Graphs](/docs/guides/graphs): the graph document format and conditions in full.
- [Escalation](/docs/sdk/escalation): per-node policies.
