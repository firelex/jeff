"""RemoteJeff.run_graph: a graph with no code nodes and only policies the server can run is one POST /v1/decide carrying
the graph inline (graph_to_dict); any other graph runs on the client stage by stage, one request per stage that has
model nodes, code nodes and llm, human or remote escalation steps in this process. Either way the result equals local
mode's over the same engine. Every test taking the `mode` fixture runs over HTTP and over the channel."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from jeff.decision import Decision
from jeff.errors import GraphError, LowConfidence
from jeff.escalation import Question, escalate, function_step, llm
from jeff.graph import Graph, Node, Skipped, graph_from_dict, graph_to_dict, load_graph

from served import FIXTURES, GRAPH, PARAMS, Bodies, Mode, Requests, answers, fake, jeff_server, local, without_timing

REPOSITORY = Path(__file__).resolve().parents[3]
GRAPH_FILES = sorted((REPOSITORY / "catalogue" / "graphs").glob("*.yaml")) + sorted((FIXTURES / "graphs").glob("*.yaml"))


def make_ticket(state: dict[str, Any]) -> dict[str, Any]:
    team = state["nodes"]["team"]
    return {"team": team.value if isinstance(team, Decision) else None}


def code_free() -> Graph:
    """The fixture graph without its code node."""
    raw = yaml.safe_load(GRAPH.read_text())
    del raw["nodes"]["ticket"]
    raw["outputs"] = ["kind", "team", "urgency"]
    return graph_from_dict(raw, "code-free fixture graph")


# --- graph_to_dict ----------------------------------------------------------------------------------------------------

def test_there_are_graph_files_to_round_trip() -> None:
    assert GRAPH in GRAPH_FILES


@pytest.mark.parametrize("path", GRAPH_FILES, ids=lambda path: path.name)
def test_graph_to_dict_round_trips_every_graph_file(path: Path) -> None:
    graph = load_graph(path)
    assert graph_from_dict(graph_to_dict(graph), "round trip") == graph


def test_graph_to_dict_writes_every_node_field_and_condition() -> None:
    raw = {"graph": "jeff.graphs.everything", "version": "2.1.0", "input": "message", "params": ["channel", "teams"],
           "nodes": {
               "kind": {"call": "jeff.safety.spam.message_type", "context": {"channel": {"param": "channel"}},
                        "on_low_confidence": "error"},
               "team": {"call": "triage.route", "labels": {"param": "teams"}, "after": ["kind"],
                        "when": {"all": [{"node": "kind", "is": "legitimate"},
                                         {"not": {"node": "kind", "in": ["spam", "phishing"]}},
                                         {"any": [{"node": "kind", "abstained": False},
                                                  {"node": "kind", "confidence_below": 0.4}]}]}},
               "ticket": {"code": "make_ticket", "after": ["team", "kind"]}},
           "outputs": ["team", "ticket"]}
    graph = graph_from_dict(raw, "test")
    assert graph_to_dict(graph) == raw
    assert graph_from_dict(graph_to_dict(graph), "round trip") == graph


def test_a_graph_the_dict_form_cannot_hold_is_a_graph_error() -> None:
    graph = code_free()
    kind = graph.nodes["kind"]
    both = Node("kind", kind.call, "make_ticket", (), kind.context, None, None, "return")
    neither = Node("kind", None, None, (), {}, None, None, "return")
    renamed = Node("other", kind.call, None, (), kind.context, None, None, "return")
    escalating = Node("kind", kind.call, None, (), kind.context, None, None,
                      escalate(function_step("triage.urgency")))  # type: ignore[arg-type]
    for node, match in ((both, "kind.*both call and code"), (neither, "kind.*neither call nor code"),
                        (renamed, "kind.*named other"), (escalating, "kind.*on_low_confidence")):
        broken = Graph(graph.id, graph.version, graph.input, graph.params, {**graph.nodes, "kind": node},
                       graph.outputs, graph.order)
        with pytest.raises(GraphError, match=match):
            graph_to_dict(broken)
    reordered = Graph(graph.id, graph.version, graph.input, graph.params, graph.nodes, graph.outputs,
                      tuple(reversed(graph.order)))
    with pytest.raises(GraphError, match="order"):
        graph_to_dict(reordered)


# --- a code-free graph: one request ------------------------------------------------------------------------------------

def test_a_code_free_graph_is_one_request_carrying_the_graph_inline_and_equals_local(mode: Mode) -> None:
    graph = code_free()
    expected = local(fake()).run_graph(graph, "My wardrobe is broken", PARAMS)
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter, bodies = Requests(client), Bodies(client)
        got = client.run_graph(graph, "My wardrobe is broken", PARAMS)
        assert counter.take() == mode.sent("graph")
    assert got == expected and got.stages == 3
    [body] = bodies.sent
    assert body == {"graph": {"inline": graph_to_dict(graph)}, "input": "My wardrobe is broken", "params": PARAMS}


def test_a_code_free_graph_from_a_file_is_one_request(tmp_path: Path, mode: Mode) -> None:
    path = tmp_path / "graph.yaml"
    path.write_text(yaml.safe_dump(graph_to_dict(code_free())))
    expected = local(fake()).run_graph(path, "x", PARAMS)
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter = Requests(client)
        got = client.run_graph(str(path), "x", PARAMS)
        assert counter.take() == mode.sent("graph")
    assert got == expected


def test_server_policies_go_with_the_one_request_by_node(mode: Mode) -> None:
    graph = code_free()
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter, bodies = Requests(client), Bodies(client)
        with pytest.raises(LowConfidence) as remote_error:
            client.run_graph(graph, "unsure", PARAMS, policies={"urgency": "error"})
        assert counter.take() == mode.sent("graph")
        client.run_graph(graph, "x", PARAMS, policies={"urgency": escalate(function_step("triage.urgency"))})
        assert counter.take() == mode.sent("graph")
    with pytest.raises(LowConfidence) as local_error:
        local(fake()).run_graph(graph, "unsure", PARAMS, policies={"urgency": "error"})
    assert str(remote_error.value) == str(local_error.value)
    assert bodies.sent[0]["on_low_confidence_by_node"] == {"urgency": "error"}
    assert bodies.sent[1]["on_low_confidence_by_node"] == {"urgency": {"escalate": ["jeff.support.triage.urgency"]}}


def test_the_inline_graph_names_functions_by_the_ids_the_local_catalogue_resolves(mode: Mode) -> None:
    raw = graph_to_dict(code_free())
    raw["nodes"]["urgency"]["call"] = "triage.urgency"
    graph = graph_from_dict(raw, "short refs")
    with jeff_server(fake()) as url, mode.remote(url) as client:
        bodies = Bodies(client)
        client.run_graph(graph, "x", PARAMS)
    assert bodies.sent[0]["graph"]["inline"]["nodes"]["urgency"]["call"] == "jeff.support.triage.urgency"


def test_graph_problems_are_raised_before_any_request(mode: Mode) -> None:
    graph = code_free()
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter = Requests(client)
        with pytest.raises(GraphError, match="teams"):
            client.run_graph(graph, "x", {"company": "c", "channel": "e"})
        with pytest.raises(GraphError, match="code.*make_ticket"):
            client.run_graph(graph, "x", PARAMS, code={"make_ticket": make_ticket})
        with pytest.raises(GraphError, match="policies.*urgncy"):
            client.run_graph(graph, "x", PARAMS, policies={"urgncy": "error"})
        assert counter.take() == []


# --- other graphs: stage by stage on the client ------------------------------------------------------------------------

@pytest.mark.parametrize("message", ["My wardrobe is broken", "unsure"])
def test_a_graph_with_a_code_node_runs_stage_by_stage_and_equals_local(message: str, mode: Mode) -> None:
    expected = local(fake()).run_graph(GRAPH, message, PARAMS, code={"make_ticket": make_ticket})
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter = Requests(client)
        got = client.run_graph(GRAPH, message, PARAMS, code={"make_ticket": make_ticket})
        sent = counter.take()
    assert got == expected and got.stages == 4
    # kind, team and urgency: one stage each; the code node's stage sends nothing
    assert sent == mode.sent("decide") * 3


def test_a_node_with_an_llm_policy_runs_it_in_the_sdk_stage_by_stage(mode: Mode) -> None:
    asked: list[Question] = []

    def ask(question: Question) -> str:
        asked.append(question)
        return "3"

    graph = code_free()
    policies = {"urgency": escalate(llm(ask))}
    expected = local(fake()).run_graph(graph, "unsure", PARAMS, policies=policies)
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter, bodies = Requests(client), Bodies(client)
        got = client.run_graph(graph, "unsure", PARAMS, policies=policies)
        assert counter.take() == mode.sent("decide") * 3
    assert len(asked) == 2 and asked[0] == asked[1]
    assert all("calls" in body and body["on_low_confidence"] == "return" for body in bodies.sent)
    urgency = got.nodes["urgency"]
    assert isinstance(urgency, Decision) and urgency.decided_by == "llm" and urgency.value == 3
    assert without_timing(urgency) == without_timing(expected.nodes["urgency"])  # type: ignore[arg-type]
    assert {name: got.nodes[name] for name in ("kind", "team")} == {name: expected.nodes[name] for name in ("kind", "team")}
    assert got.stages == expected.stages == 3


# --- skipped nodes and node policies from the graph document, on both paths --------------------------------------------

def spam_answers(adapter: str, row: dict[str, Any]) -> list[float]:
    """The spam adapter answers its second option (spam), so `kind` is not legitimate; everything else as answers."""
    if adapter == "jeff.safety.spam":
        count = len(row["question"]["criteria"])
        return [1.0 if index == 1 else 0.0 for index in range(count)]
    return answers(adapter, row)


def with_code_node(graph: Graph) -> Graph:
    raw = graph_to_dict(graph)
    raw["nodes"]["ticket"] = {"code": "make_ticket", "after": ["team", "urgency"]}
    raw["outputs"] = [*raw["outputs"], "ticket"]
    return graph_from_dict(raw, "with a code node")


@pytest.mark.parametrize("path", ["one request", "stage by stage"])
def test_skipped_nodes_equal_local_with_their_reasons(path: str, mode: Mode) -> None:
    graph = code_free() if path == "one request" else with_code_node(code_free())
    code = {} if path == "one request" else {"make_ticket": make_ticket}
    expected = local(fake(answer=spam_answers)).run_graph(graph, "WIN NOW", PARAMS, code=code)
    with jeff_server(fake(answer=spam_answers)) as url, mode.remote(url) as client:
        counter = Requests(client)
        got = client.run_graph(graph, "WIN NOW", PARAMS, code=code)
        # one request; stage by stage only kind's stage has a model node to run
        assert counter.take() == mode.sent("graph" if path == "one request" else "decide")
    assert got.nodes["kind"].value == "spam"  # type: ignore[union-attr]
    assert got.nodes["team"] == Skipped("condition not met")
    assert got.nodes["urgency"] == Skipped("depends on skipped node team")
    if path == "stage by stage":
        assert got.nodes["ticket"] == Skipped("depends on skipped node team")
    assert got == expected


def error_in_the_document() -> Graph:
    raw = graph_to_dict(code_free())
    raw["nodes"]["urgency"]["on_low_confidence"] = "error"
    return graph_from_dict(raw, "urgency errors")


@pytest.mark.parametrize("path", ["one request", "stage by stage"])
def test_a_node_policy_from_the_graph_document_equals_local(path: str, mode: Mode) -> None:
    graph = error_in_the_document() if path == "one request" else with_code_node(error_in_the_document())
    code = {} if path == "one request" else {"make_ticket": make_ticket}
    with pytest.raises(LowConfidence) as local_error:
        local(fake()).run_graph(graph, "unsure", PARAMS, code=code)
    overridden = local(fake()).run_graph(graph, "unsure", PARAMS, code=code, policies={"urgency": "return"})
    confident = local(fake()).run_graph(graph, "My wardrobe is broken", PARAMS, code=code)
    with jeff_server(fake()) as url, mode.remote(url) as client:
        with pytest.raises(LowConfidence) as remote_error:
            client.run_graph(graph, "unsure", PARAMS, code=code)
        assert client.run_graph(graph, "unsure", PARAMS, code=code, policies={"urgency": "return"}) == overridden
        assert client.run_graph(graph, "My wardrobe is broken", PARAMS, code=code) == confident
    assert type(remote_error.value) is LowConfidence and str(remote_error.value) == str(local_error.value)
    assert remote_error.value.decision == local_error.value.decision  # type: ignore[attr-defined]
    assert overridden.nodes["urgency"].abstained is True  # type: ignore[union-attr]


def test_the_stage_by_stage_path_binds_each_model_node_once(mode: Mode) -> None:
    with jeff_server(fake()) as url, mode.remote(url) as client:
        bound: list[str] = []
        binding = client._binding

        def counted(call: Any) -> Any:
            bound.append(call.function)
            return binding(call)

        client._binding = counted  # type: ignore[method-assign]
        client.run_graph(GRAPH, "x", PARAMS, code={"make_ticket": make_ticket})
    assert sorted(bound) == sorted(["jeff.safety.spam.message_type", "jeff.support.triage.route",
                                    "jeff.support.triage.urgency"])
