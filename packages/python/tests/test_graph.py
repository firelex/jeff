from pathlib import Path

import pytest

from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff import contract
from jeff.decision import Decision
from jeff.errors import GraphError, InvalidPolicy, LowConfidence, NotInstalled
from jeff.graph import Graph, GraphResult, Node, Skipped, graph_from_dict, load_graph
from jeff.testing import FakeEngine

ROOT = Path(__file__).parent / "fixtures"
GRAPH = ROOT / "graphs" / "support_intake.yaml"
PARAMS = {"company": "A furniture shop.", "channel": "email", "teams": {"deliveries": "D", "refunds": "R"}}


def engine(spam_first: bool) -> FakeEngine:
    def answers(adapter: str, row: dict) -> list[float]:
        question = row["question"]
        if question["type"] == "score":
            return [0.0, 0.0, 0.9, 0.1, 0.0]
        count = len(question["criteria"])
        if adapter == "jeff.safety.spam":
            pick = 0 if spam_first else 1  # legitimate | spam
        else:
            pick = count - 1
        return [1.0 if index == pick else 0.0 for index in range(count)]
    return FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})


def test_graph_runs_in_stages_and_batches(tmp_path: Path) -> None:
    fake = engine(spam_first=True)
    jeff = Jeff(fake, Catalogue.from_dir(ROOT / "catalogue"))
    result = jeff.run_graph(GRAPH, "My wardrobe is broken", PARAMS,
                            code={"make_ticket": lambda state: {"team": state["nodes"]["team"].value}})
    assert result.outputs["kind"].value == "legitimate"
    assert result.outputs["team"].value == "refunds"
    assert result.outputs["urgency"].value == 2
    assert result.outputs["ticket"] == {"team": "refunds"}
    assert result.stages == 4


def test_skips_propagate_with_reasons() -> None:
    jeff = Jeff(engine(spam_first=False), Catalogue.from_dir(ROOT / "catalogue"))
    result = jeff.run_graph(GRAPH, "WIN NOW", PARAMS, code={"make_ticket": lambda state: "never"})
    assert result.outputs["team"] == Skipped("condition not met")
    assert result.outputs["urgency"] == Skipped("depends on skipped node team")
    assert result.outputs["ticket"] == Skipped("depends on skipped node team")


def test_missing_params_and_code_are_errors() -> None:
    jeff = Jeff(engine(True), Catalogue.from_dir(ROOT / "catalogue"))
    with pytest.raises(GraphError, match="teams"):
        jeff.run_graph(GRAPH, "x", {"company": "c", "channel": "e"}, code={"make_ticket": lambda s: None})
    with pytest.raises(GraphError, match="make_ticket"):
        jeff.run_graph(GRAPH, "x", PARAMS)


def test_cycles_and_unknown_nodes_are_refused() -> None:
    base = {"graph": "jeff.graphs.loop", "version": "1.0.0", "input": "message", "params": [], "outputs": ["a"]}
    with pytest.raises(GraphError, match="cycle"):
        graph_from_dict({**base, "nodes": {"a": {"code": "f", "after": ["b"]}, "b": {"code": "g", "after": ["a"]}}}, "t")
    with pytest.raises(GraphError, match="zzz"):
        graph_from_dict({**base, "nodes": {"a": {"code": "f", "when": {"node": "zzz", "is": True}}}}, "t")


def test_load_graph_orders_nodes() -> None:
    graph = load_graph(GRAPH)
    assert graph.order.index("kind") < graph.order.index("team") < graph.order.index("urgency") < graph.order.index("ticket")


def small_graph(nodes: dict, params: list[str] | None = None) -> Graph:
    return graph_from_dict({"graph": "jeff.graphs.test", "version": "1.0.0", "input": "message",
                            "params": params or ["channel", "company"], "nodes": nodes, "outputs": list(nodes)}, "t")


KIND = {"call": "jeff.safety.spam.message_type", "context": {"channel": {"param": "channel"}}}
URGENCY = {"call": "jeff.support.triage.urgency",
           "context": {"company": {"param": "company"}, "channel": {"param": "channel"}}}
SMALL = {"company": "A furniture shop.", "channel": "email"}


def test_in_all_any_and_not_conditions() -> None:
    graph = small_graph({
        "kind": KIND,
        "in_hit": {**URGENCY, "when": {"node": "kind", "in": ["spam", "legitimate"]}},
        "in_miss": {**URGENCY, "when": {"node": "kind", "in": ["spam", "phishing"]}},
        "all_hit": {**URGENCY, "when": {"all": [{"node": "kind", "is": "legitimate"},
                                                {"not": {"node": "kind", "is": "spam"}}]}},
        "all_miss": {**URGENCY, "when": {"all": [{"node": "kind", "is": "legitimate"}, {"node": "kind", "is": "spam"}]}},
        "any_hit": {**URGENCY, "when": {"any": [{"node": "kind", "is": "spam"}, {"node": "kind", "abstained": False}]}},
        "any_miss": {**URGENCY, "when": {"any": [{"node": "kind", "is": "spam"}, {"node": "kind", "is": "phishing"}]}},
        "not_hit": {**URGENCY, "when": {"not": {"node": "kind", "in": ["spam"]}}},
        "not_miss": {**URGENCY, "when": {"not": {"node": "kind", "in": ["legitimate"]}}},
    })
    result = Jeff(engine(spam_first=True), Catalogue.from_dir(ROOT / "catalogue")).run_graph(graph, "x", SMALL)
    for name in ("in_hit", "all_hit", "any_hit", "not_hit"):
        assert isinstance(result.nodes[name], Decision), name
    for name in ("in_miss", "all_miss", "any_miss", "not_miss"):
        assert result.nodes[name] == Skipped("condition not met"), name


def unsure_jeff() -> Jeff:
    def answers(adapter: str, row: dict) -> list[float]:
        if row["question"]["type"] == "score":
            return [0.0, 0.5, 0.5, 0.0, 0.0]  # below urgency's 0.6 threshold
        return [1.0] + [0.0] * (len(row["question"]["criteria"]) - 1)
    return Jeff(FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"}), Catalogue.from_dir(ROOT / "catalogue"))


def test_policies_override_the_node_policy() -> None:
    graph = small_graph({"urgency": URGENCY})
    assert unsure_jeff().run_graph(graph, "x", SMALL).nodes["urgency"].abstained is True  # type: ignore[union-attr]
    with pytest.raises(LowConfidence):
        unsure_jeff().run_graph(graph, "x", SMALL, policies={"urgency": "error"})


def test_policies_and_code_for_unknown_nodes_are_refused() -> None:
    jeff = unsure_jeff()
    graph = small_graph({"urgency": URGENCY})
    with pytest.raises(GraphError, match="policies.*urgncy"):
        jeff.run_graph(graph, "x", SMALL, policies={"urgncy": "error"})
    with pytest.raises(GraphError, match="code.*make_tiket"):
        jeff.run_graph(graph, "x", SMALL, code={"make_tiket": lambda state: None})
    assert jeff.engine.calls == []  # type: ignore[attr-defined]


def test_an_invalid_policy_is_refused_before_anything_runs() -> None:
    jeff = unsure_jeff()
    with pytest.raises(InvalidPolicy, match="eror"):
        jeff.run_graph(small_graph({"kind": KIND, "urgency": URGENCY}), "x", SMALL,
                       policies={"urgency": "eror"})  # type: ignore[dict-item]
    assert jeff.engine.calls == []  # type: ignore[attr-defined]


def test_nodes_are_checked_against_the_catalogue_before_anything_runs() -> None:
    jeff = unsure_jeff()
    cases = [
        ({"kind": KIND, "bad": {"call": "jeff.support.triage.nope"}}, GraphError, "bad.*nope"),
        ({"kind": KIND, "bad": {"call": "jeff.support.triage.urgency", "context": {"colour": {"param": "channel"}}}},
         GraphError, "bad.*colour"),
        ({"kind": KIND, "bad": {**URGENCY, "labels": {"param": "channel"}}}, GraphError, "bad.*labels"),
        ({"kind": KIND, "bad": {"call": "jeff.support.triage.urgency"}}, GraphError, "bad.*company"),
    ]
    for nodes, error, match in cases:
        with pytest.raises(error, match=match):
            jeff.run_graph(small_graph(nodes), "x", SMALL)
    only_spam = Jeff(FakeEngine(lambda a, r: [1.0, 0.0, 0.0], {"jeff.safety.spam"}), Catalogue.from_dir(ROOT / "catalogue"))
    with pytest.raises(NotInstalled, match="jeff.support.triage.urgency"):
        only_spam.run_graph(small_graph({"kind": KIND, "urgency": URGENCY}), "x", SMALL)
    assert jeff.engine.calls == [] and only_spam.engine.calls == []  # type: ignore[attr-defined]


def test_a_stage_without_progress_is_an_error_not_a_hang() -> None:
    def code_node(name: str, after: str) -> Node:
        return Node(name, None, "f", (after,), {}, None, None, "return")

    cyclic = Graph("jeff.graphs.cyclic", "1.0.0", "message", (), {"a": code_node("a", "b"), "b": code_node("b", "a")},
                   ("a",), ("a", "b"))
    jeff = unsure_jeff()
    with pytest.raises(GraphError, match="no progress.*a, b"):
        jeff.run_graph(cyclic, "x", {}, code={"f": lambda state: None})


def test_graph_result_round_trips_and_matches_its_schema() -> None:
    result = Jeff(engine(spam_first=True), Catalogue.from_dir(ROOT / "catalogue")).run_graph(
        GRAPH, "x", PARAMS, code={"make_ticket": lambda state: {"team": state["nodes"]["team"].value}})
    skipped = Jeff(engine(spam_first=False), Catalogue.from_dir(ROOT / "catalogue")).run_graph(
        GRAPH, "x", PARAMS, code={"make_ticket": lambda state: None})
    for item in (result, skipped):
        data = item.to_json()
        contract.validate("graph_result", data, "test")
        assert GraphResult.from_json(data) == item
    assert skipped.to_json()["outputs"]["team"] == {"skipped": True, "reason": "condition not met"}
    assert Skipped.from_json(Skipped("x").to_json()) == Skipped("x")
