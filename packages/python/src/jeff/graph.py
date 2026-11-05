"""Decision graphs: functions wired together with declarative conditions, run in batched stages (spec Section 10)."""

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from jeff import contract
from jeff.binding import JSON, Binding
from jeff.decision import Decision
from jeff.errors import ContractError, GraphError, JeffError, NotInstalled, NotLicensed, UnknownFunction

if TYPE_CHECKING:
    from jeff.client import Call, Jeff
    from jeff.escalation import Policy


@dataclass(frozen=True)
class Node:
    name: str
    call: str | None
    code: str | None
    after: tuple[str, ...]
    context: dict[str, str]
    labels: str | None
    when: dict[str, Any] | None
    on_low_confidence: str


@dataclass(frozen=True)
class Graph:
    id: str
    version: str
    input: str
    params: tuple[str, ...]
    nodes: dict[str, Node]
    outputs: tuple[str, ...]
    order: tuple[str, ...]


@dataclass(frozen=True)
class Skipped:
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {"skipped": True, "reason": self.reason}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "Skipped":
        return cls(data["reason"])


type Result = Decision | Skipped | JSON


def _result_to_json(result: Result) -> dict[str, Any]:
    """One node's result as contract/schemas/graph_result.schema.json writes it: {"decision": ...} for a model node,
    {"skipped": true, "reason": ...} for a skipped node, {"code": ...} for what a code node returned."""
    if isinstance(result, Decision):
        return {"decision": result.to_json()}
    if isinstance(result, Skipped):
        return result.to_json()
    return {"code": result}


def _result_from_json(data: Mapping[str, Any]) -> Result:
    if "decision" in data:
        return Decision.from_json(data["decision"])
    if "skipped" in data:
        return Skipped.from_json(data)
    if "code" in data:
        result: JSON = data["code"]
        return result
    raise GraphError(f"A graph result node must hold 'decision', 'skipped' or 'code'; got keys {sorted(data)}")


@dataclass(frozen=True)
class GraphResult:
    nodes: dict[str, Result]
    outputs: dict[str, Result]
    stages: int

    def to_json(self) -> dict[str, Any]:
        return {"nodes": {name: _result_to_json(result) for name, result in self.nodes.items()},
                "outputs": {name: _result_to_json(result) for name, result in self.outputs.items()},
                "stages": self.stages}

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> "GraphResult":
        return cls({name: _result_from_json(item) for name, item in data["nodes"].items()},
                   {name: _result_from_json(item) for name, item in data["outputs"].items()}, data["stages"])


def _condition_nodes(condition: dict[str, Any] | None) -> set[str]:
    if condition is None:
        return set()
    if "all" in condition or "any" in condition:
        return set().union(*(_condition_nodes(item) for item in condition.get("all", condition.get("any", []))))
    if "not" in condition:
        return _condition_nodes(condition["not"])
    return {condition["node"]}


def _params_used(raw: dict[str, Any]) -> set[str]:
    used = {ref["param"] for ref in raw.get("context", {}).values()}
    if "labels" in raw:
        used.add(raw["labels"]["param"])
    return used


def graph_from_dict(raw: dict[str, Any], where: str) -> Graph:
    try:
        contract.validate("graph", raw, where)
    except ContractError as error:
        raise GraphError(str(error)) from error
    nodes: dict[str, Node] = {}
    for name, body in raw["nodes"].items():
        nodes[name] = Node(name, body.get("call"), body.get("code"), tuple(body.get("after", [])),
                           {key: ref["param"] for key, ref in body.get("context", {}).items()},
                           body["labels"]["param"] if "labels" in body else None, body.get("when"),
                           body.get("on_low_confidence", "return"))
        unknown = (_condition_nodes(body.get("when")) | set(body.get("after", []))) - set(raw["nodes"])
        if unknown:
            raise GraphError(f"{where}: node {name} refers to unknown node(s) {', '.join(sorted(unknown))}")
        undeclared = _params_used(body) - set(raw["params"])
        if undeclared:
            raise GraphError(f"{where}: node {name} uses undeclared param(s) {', '.join(sorted(undeclared))}")
    missing_outputs = set(raw["outputs"]) - set(nodes)
    if missing_outputs:
        raise GraphError(f"{where}: outputs name unknown node(s) {', '.join(sorted(missing_outputs))}")
    order: list[str] = []
    state: dict[str, str] = {}

    def visit(name: str, path: tuple[str, ...]) -> None:
        if state.get(name) == "done":
            return
        if state.get(name) == "active":
            raise GraphError(f"{where}: the nodes form a cycle: {' -> '.join(path + (name,))}")
        state[name] = "active"
        for dependency in sorted(dependencies(nodes[name])):
            visit(dependency, path + (name,))
        state[name] = "done"
        order.append(name)

    for name in nodes:
        visit(name, ())
    return Graph(raw["graph"], raw["version"], raw["input"], tuple(raw["params"]), nodes, tuple(raw["outputs"]),
                 tuple(order))


def _node_to_dict(graph_id: str, key: str, node: Node) -> dict[str, Any]:
    if node.name != key:
        raise GraphError(f"{graph_id}: the node under {key!r} is named {node.name}; a graph document names each node "
                         "by its key only")
    if node.call is not None and node.code is not None:
        raise GraphError(f"{graph_id}: node {key} has both call and code; a node is a model call or a code function")
    if node.call is None and node.code is None:
        raise GraphError(f"{graph_id}: node {key} has neither call nor code")
    if node.on_low_confidence not in ("return", "error"):
        raise GraphError(f"{graph_id}: node {key} has on_low_confidence {node.on_low_confidence!r}; a graph document "
                         "holds only 'return' or 'error' (pass other policies in policies=)")
    body: dict[str, Any] = {"call": node.call} if node.call is not None else {"code": node.code}
    if node.after:
        body["after"] = list(node.after)
    if node.context:
        body["context"] = {field: {"param": param} for field, param in node.context.items()}
    if node.labels is not None:
        body["labels"] = {"param": node.labels}
    if node.when is not None:
        body["when"] = copy.deepcopy(node.when)
    if node.on_low_confidence != "return":
        body["on_low_confidence"] = node.on_low_confidence
    return body


def graph_to_dict(graph: Graph) -> dict[str, Any]:
    """The graph as the document graph_from_dict reads (contract/schemas/graph.schema.json):
    graph_from_dict(graph_to_dict(graph), where) == graph. A Graph the document cannot hold exactly (a node with both
    or neither of call and code, a policy other than 'return' or 'error', a node keyed by another name, an order other
    than the one its dependencies give) raises GraphError naming what cannot be written."""
    raw: dict[str, Any] = {"graph": graph.id, "version": graph.version, "input": graph.input,
                           "params": list(graph.params),
                           "nodes": {key: _node_to_dict(graph.id, key, node) for key, node in graph.nodes.items()},
                           "outputs": list(graph.outputs)}
    rebuilt = graph_from_dict(copy.deepcopy(raw), f"graph {graph.id}")
    if rebuilt != graph:
        differing = [field for field in ("id", "version", "input", "params", "nodes", "outputs", "order")
                     if getattr(rebuilt, field) != getattr(graph, field)]
        detail = (f"its order {', '.join(graph.order)} is not the order its dependencies give "
                  f"({', '.join(rebuilt.order)})" if differing == ["order"] else f"field(s) {', '.join(differing)} differ")
        raise GraphError(f"{graph.id} cannot be written as a graph document that reads back the same: {detail}")
    return raw


def load_graph(path: Path) -> Graph:
    return graph_from_dict(yaml.safe_load(path.read_text(encoding="utf-8")), str(path))


def dependencies(node: Node) -> set[str]:
    return _condition_nodes(node.when) | set(node.after)


def _value(name: str, result: Result) -> Any:
    return result.value if isinstance(result, Decision) else result


def _holds(condition: dict[str, Any], results: Mapping[str, Result]) -> bool:
    if "all" in condition:
        return all(_holds(item, results) for item in condition["all"])
    if "any" in condition:
        return any(_holds(item, results) for item in condition["any"])
    if "not" in condition:
        return not _holds(condition["not"], results)
    name = condition["node"]
    result = results[name]
    if "is" in condition:
        return bool(_value(name, result) == condition["is"])
    if "in" in condition:
        return _value(name, result) in condition["in"]
    if not isinstance(result, Decision):
        raise GraphError(f"Condition on {name} needs a model decision; {name} is a code node")
    if "abstained" in condition:
        return result.abstained is condition["abstained"]
    return result.confidence is not None and result.confidence < condition["confidence_below"]


def _check(jeff: "Jeff", graph: Graph, input: JSON, params: Mapping[str, JSON], code: Mapping[str, Any],
           policies: Mapping[str, "Policy"]) -> dict[str, tuple["Call", Binding, "Policy"]]:
    """Everything that can be checked before the first decision: params, code and policies name real things, and
    every model node resolves in the catalogue, is installed, binds its context and labels, and has a valid policy.
    Returns each model node's call, binding and policy."""
    from jeff.client import Call
    from jeff.escalation import check_policy

    missing = [name for name in graph.params if name not in params]
    if missing:
        raise GraphError(f"{graph.id} needs param(s) {', '.join(missing)}")
    unknown_policies = sorted(set(policies) - set(graph.nodes))
    if unknown_policies:
        raise GraphError(f"{graph.id}: policies= names node(s) {', '.join(unknown_policies)}, which the graph does "
                         f"not have (its nodes: {', '.join(graph.nodes)})")
    used_code = {node.code for node in graph.nodes.values() if node.code is not None}
    unknown_code = sorted(set(code) - used_code)
    if unknown_code:
        raise GraphError(f"{graph.id}: code= names function(s) {', '.join(unknown_code)}, which no node uses "
                         f"(code nodes use: {', '.join(sorted(used_code)) or 'none'})")
    missing_code = sorted(used_code - set(code))
    if missing_code:
        raise GraphError(f"{graph.id} needs code function(s) {', '.join(missing_code)}; pass them in code=")
    model: dict[str, tuple[Call, Binding, Policy]] = {}
    for name in graph.order:
        node = graph.nodes[name]
        if node.call is None:
            if node.code is None:
                raise GraphError(f"{graph.id}: node {name} has neither call nor code")
            continue
        try:
            function = jeff.catalogue.function(node.call)
        except UnknownFunction as error:
            raise GraphError(f"{graph.id}: node {name} calls {node.call}: {error}") from error
        if function.adapter_id not in jeff.engine.installed():
            raise NotInstalled(function.id, f"node {name} of graph {graph.id} calls it")
        context = {key: params[param] for key, param in node.context.items()}
        labels = params[node.labels] if node.labels else None
        call = Call(node.call, input, context or None, labels)  # type: ignore[arg-type]
        try:
            binding = jeff._binding(call)
        except NotLicensed:
            raise  # a refused licence is the caller's, not a problem with the graph
        except JeffError as error:
            raise GraphError(f"{graph.id}: node {name} ({function.id}): {error}") from error
        policy = policies[name] if name in policies else node.on_low_confidence
        check_policy(jeff, function.interface, policy)
        model[name] = (call, binding, policy)  # type: ignore[assignment]
    return model


def run_graph(jeff: "Jeff", graph: Graph, input: JSON, params: Mapping[str, JSON],
              code: Mapping[str, Callable[[dict[str, Any]], JSON]] | None = None,
              policies: "Mapping[str, Policy] | None" = None) -> GraphResult:
    code, policies = dict(code or {}), dict(policies or {})
    return _run_checked(jeff, graph, input, params, code, _check(jeff, graph, input, params, code, policies))


def _run_checked(jeff: "Jeff", graph: Graph, input: JSON, params: Mapping[str, JSON],
                 code: Mapping[str, Callable[[dict[str, Any]], JSON]],
                 model: Mapping[str, tuple["Call", Binding, "Policy"]]) -> GraphResult:
    """Run a graph whose model nodes _check has already resolved (`model`: each node's call, binding and policy)."""
    results: dict[str, Result] = {}
    stages = 0
    while len(results) < len(graph.nodes):
        ready = [name for name in graph.order if name not in results
                 and all(dependency in results for dependency in dependencies(graph.nodes[name]))]
        if not ready:
            waiting = sorted(name for name in graph.nodes if name not in results)
            raise GraphError(f"{graph.id} makes no progress: node(s) {', '.join(waiting)} wait on each other or on "
                             "nodes the graph does not have")
        stages += 1
        batch: list[str] = []
        coded: list[tuple[str, str]] = []
        for name in ready:
            node = graph.nodes[name]
            skipped = sorted(d for d in dependencies(node) if isinstance(results[d], Skipped))
            if skipped:
                results[name] = Skipped(f"depends on skipped node {skipped[0]}")
            elif node.when is not None and not _holds(node.when, results):
                results[name] = Skipped("condition not met")
            elif name in model:
                batch.append(name)
            elif node.code is not None:
                coded.append((name, node.code))
            else:
                raise GraphError(f"{graph.id}: node {name} has neither call nor code")
        if batch:
            decisions = jeff._decide_bound([(model[name][0], model[name][1]) for name in batch])
            for name, decision in zip(batch, decisions, strict=True):
                call, _, policy = model[name]
                results[name] = jeff._apply(call, decision, policy)
        for name, code_name in coded:
            results[name] = code[code_name]({"input": input, "params": dict(params), "nodes": dict(results)})
    return GraphResult(dict(results), {name: results[name] for name in graph.outputs}, stages)
