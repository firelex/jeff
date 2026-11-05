---
title: Graphs
---
# Graphs

A decision graph wires several functions together with declarative conditions — data, not code — so the same graph
runs the same way locally or through the server. Each node either calls a function or runs the caller's own code;
a node may depend on others (`after`, or implicitly through a `when` condition that reads another node's result),
and a node's `when` can skip it based on an earlier node's value or confidence.

## Calling a graph

`POST /v1/decide` accepts either the ID of a graph registered on the server (`JEFF_GRAPHS`, see
[Running the server](/docs/guides/running-server)), or inline:

```json
{"graph": {"id": "support_intake"}, "input": "My wardrobe arrived broken",
 "params": {"company": "A furniture shop.", "channel": "email", "teams": {"deliveries": "Deliveries", "refunds": "Refunds"}}}
```

```json
{"graph_result": {"nodes": {"...": "..."}, "outputs": {"kind": {"decision": "..."}, "team": {"decision": "..."}}, "stages": 2}}
```

An inline graph is given under `"inline"` instead of `"id"`, as the full graph document (below) — at most 256 nodes.
`GET /v1/graphs` lists every graph this server has registered.

## Writing a graph

A graph document names its input, its params (values the caller supplies once per run, not per node), one or more
nodes, and which nodes' results are its outputs:

```yaml
# support_intake.yaml
graph: jeff.graphs.support_intake
version: 1.0.0
input: message
params: [company, channel, teams]
nodes:
  kind:    { call: jeff.safety.spam.message_type, context: { channel: { param: channel } } }
  team:    { call: jeff.support.triage.route,
             context: { company: { param: company }, channel: { param: channel } }, labels: { param: teams },
             when: { node: kind, is: legitimate } }
  urgency: { call: jeff.support.triage.urgency,
             context: { company: { param: company }, channel: { param: channel } },
             when: { node: team, abstained: false } }
  ticket:  { code: make_ticket, after: [team, urgency] }
outputs: [kind, team, urgency, ticket]
```

Each node is either `{call: <function>, context?, labels?, after?, when?, on_low_confidence?}` (a model node) or
`{code: <name>, after?, when?}` (a code node, run by the caller — the server can run a graph only when it has no
code nodes; see [Through the API](/docs/guides/remote) for graphs that do). A node's values always come from the graph's declared
`params`, never written into the graph: `context` maps each of the function's context fields, by its code name, to
`{param: <name>}`, and `labels` is `{param: <name>}`. A literal value there is refused (`GraphError`), as is a param
the graph does not declare.

`when` conditions read an earlier node's result:

| Condition | Meaning |
|---|---|
| `{node, is: <value>}` | That node's value equals `<value>` |
| `{node, in: [<values>]}` | That node's value is one of `<values>` |
| `{node, abstained: <bool>}` | That node abstained, or did not |
| `{node, confidence_below: <n>}` | That node's confidence is below `<n>` |
| `{all: [...]}`, `{any: [...]}`, `{not: ...}` | Combine other conditions |

A node whose `when` does not hold is skipped — its result in `nodes` is `{skipped: true, reason}`, and it contributes
nothing to any node that depends on it. `on_low_confidence` on a node overrides the request's own
`on_low_confidence_by_node` entry for that node (only `"return"` or `"error"`; escalation steps run per node through
the request's `on_low_confidence_by_node`, not inside the graph document itself).

## Running order

Before any decisions are made, the whole graph is checked against the catalogue — every called function must be known,
installed and reachable through its interface, and every policy must be valid — so an invalid graph fails before any model runs,
rather than partway through. The runner then batches every node whose inputs are ready into one call to the engine (so
independent branches share model passes, and on the fast path share cached prompt prefixes), evaluates each node's
`when` condition, and repeats until every node has run or been skipped. `graph_result.stages` counts how many such
batched rounds it took.

## Next steps

- [Functions and /v1/decide](/docs/guides/decide): the `calls` and `dispatch` shapes a graph's nodes build on.
- [Through the API](/docs/guides/remote): running a graph with code nodes from Python, stage by stage.
- [Errors](/docs/guides/errors): `GraphError`, for a graph that is invalid or cannot run.

