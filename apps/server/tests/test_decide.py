"""POST /v1/decide, GET /v1/functions and GET /v1/graphs: every request shape answers exactly what the in-process
SDK (jeff.Jeff) answers over the same engine and catalogue; wrong shapes are 422 InvalidRequest naming the problem;
on_low_confidence works over HTTP ("error" -> 409 carrying the decision, escalation through function steps); graphs
run by id (registered from settings.graphs at startup) or inline, never with code nodes."""

import hashlib
import json
import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from jeff import contract
from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.contract import shipped_dir
from jeff.decision import Decision
from jeff.errors import GraphError
from jeff.escalation import escalate, function_step
from jeff.graph import GraphResult, load_graph
from jeff.testing import FakeEngine

from jeff_server.app import create_app
from jeff_server.settings import ServerSettings

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "python" / "tests" / "fixtures"
CATALOGUE = FIXTURES / "catalogue"
ROUTERS = FIXTURES / "routers"
GRAPH = FIXTURES / "graphs" / "support_intake.yaml"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
TEAMS = {"deliveries": "Deliveries", "refunds": "Refunds"}
FIXTURE_ADAPTERS = {"jeff.safety.spam", "jeff.support.triage"}
UNSURE = [0.0, 0.5, 0.5, 0.0, 0.0]  # below triage.urgency's calibration threshold 0.6


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    """Deterministic, row-dependent probabilities: the first option wins except for a score question, which is
    confident (level 2 at 0.9) unless the input contains "unsure"."""
    question = row["question"]
    if question["type"] == "score":
        return UNSURE if "unsure" in json.dumps(row["state"]) else [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2  # a noul question may not describe its two
    return [1.0] + [0.0] * (count - 1)


def settings(catalogue: Path = CATALOGUE, graphs: Path | None = None) -> ServerSettings:
    return ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                          catalogue=catalogue, graphs=graphs)


def client(engine: FakeEngine, catalogue: Path = CATALOGUE, graphs: Path | None = None) -> TestClient:
    return TestClient(create_app(settings(catalogue, graphs), engine=engine))


def local(engine: FakeEngine, catalogue: Path = CATALOGUE) -> Jeff:
    return Jeff(engine, Catalogue.from_dir(catalogue))


def fake(installed: set[str] = FIXTURE_ADAPTERS,
         answer: Callable[[str, dict[str, Any]], list[float]] = answers) -> FakeEngine:
    return FakeEngine(answer, set(installed))


def code_free_graph(tmp_path: Path) -> Path:
    """The fixture graph without its code node, registered in a graphs folder."""
    raw = yaml.safe_load(GRAPH.read_text())
    del raw["nodes"]["ticket"]
    raw["outputs"] = ["kind", "team", "urgency"]
    folder = tmp_path / "graphs"
    folder.mkdir()
    (folder / "support_intake.yaml").write_text(yaml.safe_dump(raw))
    return folder


def assert_error(response: Any, status: int, error_type: str, *named: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["x-request-id"]
    error = response.json()["error"]
    assert error["type"] == error_type, error
    for text in named:
        assert text in error["message"], error["message"]
    return dict(error)


# --- calls ---------------------------------------------------------------------------------------------------------

CALLS: list[dict[str, Any]] = [
    {"function": "triage.route", "input": "My wardrobe arrived broken", "context": CONTEXT, "labels": TEAMS},
    {"function": "jeff.support.triage.urgency", "input": "It is on fire", "context": CONTEXT},
    {"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "email"}},
    {"function": "triage.needs_human", "input": "Call me back", "context": CONTEXT},
]


def test_calls_answer_the_same_decisions_as_the_sdk_in_order() -> None:
    with client(fake()) as http:
        response = http.post("/v1/decide", json={"calls": CALLS})
    assert response.status_code == 200, response.text
    body = response.json()
    contract.validate("decide_response", body, "response")
    expected = local(fake()).decide([Call(**call) for call in CALLS])
    assert [Decision.from_json(item) for item in body["decisions"]] == expected


def test_calls_with_subset_labels_as_a_list() -> None:
    call = {"function": "jeff.core.router.domain", "input": "x", "labels": ["jeff.support.router", "jeff.safety.spam"]}
    engine_installed = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
    with client(fake(engine_installed), ROUTERS) as http:
        response = http.post("/v1/decide", json={"calls": [call]})
    assert response.status_code == 200, response.text
    expected = local(fake(engine_installed), ROUTERS).decide([Call(**call)])  # type: ignore[arg-type]
    assert [Decision.from_json(item) for item in response.json()["decisions"]] == expected


def test_on_low_confidence_return_is_the_default_and_returns_the_abstained_decision() -> None:
    call = {"function": "triage.urgency", "input": "unsure", "context": CONTEXT}
    with client(fake()) as http:
        default = http.post("/v1/decide", json={"calls": [call]})
        explicit = http.post("/v1/decide", json={"calls": [call], "on_low_confidence": "return"})
    assert default.status_code == 200 and explicit.status_code == 200
    assert default.json()["decisions"][0]["abstained"] is True
    assert default.json()["decisions"] == explicit.json()["decisions"]


def test_on_low_confidence_error_is_409_low_confidence_carrying_the_decision() -> None:
    call = {"function": "triage.urgency", "input": "unsure", "context": CONTEXT}
    with client(fake()) as http:
        response = http.post("/v1/decide", json={"calls": [call], "on_low_confidence": "error"})
    error = assert_error(response, 409, "LowConfidence")
    decision = Decision.from_json(error["details"]["decision"])
    assert decision.abstained is True and decision.function == "jeff.support.triage.urgency"
    [expected] = local(fake()).decide([Call(**call)])  # type: ignore[arg-type]
    assert decision == expected


def test_escalation_through_a_function_step_matches_the_sdk(tmp_path: Path) -> None:
    """A second rate function on another adapter answers when triage.urgency abstains; the escalated decision lists
    the abstained attempt."""
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    second = yaml.safe_load((CATALOGUE / "jeff.support.triage" / "manifest.yaml").read_text())
    second["id"], second["aliases"] = "jeff.support.backup", ["backup"]
    second["functions"] = {"urgency": {**second["functions"]["urgency"], "calibration": None}}
    (catalogue / "jeff.support.backup").mkdir()
    (catalogue / "jeff.support.backup" / "manifest.yaml").write_text(yaml.safe_dump(second))
    installed = FIXTURE_ADAPTERS | {"jeff.support.backup"}

    def answer(adapter: str, row: dict[str, Any]) -> list[float]:
        return UNSURE if adapter == "jeff.support.triage" else [0.0, 0.0, 0.0, 0.95, 0.05]

    call = {"function": "triage.urgency", "input": "x", "context": CONTEXT}
    with client(fake(installed, answer), catalogue) as http:
        response = http.post("/v1/decide", json={
            "calls": [call], "on_low_confidence": {"escalate": ["backup.urgency"], "budget_ms": 5000}})
    assert response.status_code == 200, response.text
    decision = Decision.from_json(response.json()["decisions"][0])
    assert decision.value == 3 and decision.decided_by == "jeff.support.backup.urgency@1.3.0"
    assert [item.decided_by for item in decision.escalations] == ["jeff.support.triage.urgency@1.3.0"]
    expected = local(fake(installed, answer), catalogue).function(
        "triage.urgency", context=CONTEXT,
        on_low_confidence=escalate(function_step("backup.urgency"), budget_ms=5000))("x")
    assert decision == expected


def test_escalation_step_of_another_interface_is_refused_before_anything_is_decided() -> None:
    engine = fake()
    with client(engine) as http:
        response = http.post("/v1/decide", json={
            "calls": [{"function": "triage.urgency", "input": "x", "context": CONTEXT}],
            "on_low_confidence": {"escalate": ["spam.message_type"]}})
    assert_error(response, 422, "InterfaceMismatch", "spam.message_type")
    assert engine.calls == []


def test_the_policy_is_checked_once_per_interface_not_once_per_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """256 calls on two interfaces check the escalation policy twice, and a bad step among many calls is still
    refused before anything is decided."""
    from jeff_server import decide as decide_module

    checked: list[str] = []
    real = decide_module.check_policy

    def counting(jeff: Any, interface: str, policy: Any) -> None:
        checked.append(interface)
        real(jeff, interface, policy)

    monkeypatch.setattr(decide_module, "check_policy", counting)
    urgency = {"function": "triage.urgency", "input": "x", "context": CONTEXT}
    spam = {"function": "spam.message_type", "input": "x", "context": {"channel": "email"}}
    engine = fake()
    with client(engine) as http:
        mixed = http.post("/v1/decide", json={"calls": [urgency, spam] * 128, "on_low_confidence": "return"})
        assert mixed.status_code == 200, mixed.text
        assert len(checked) == 2 and len(set(checked)) == 2, checked
        refused = http.post("/v1/decide", json={"calls": [urgency] * 200,
                                                "on_low_confidence": {"escalate": ["spam.message_type"]}})
    assert_error(refused, 422, "InterfaceMismatch", "spam.message_type")
    assert len(engine.calls) == 2  # the mixed request's two adapters; the refused request reached no engine


def test_sdk_errors_keep_their_catalogued_status() -> None:
    with client(fake()) as http:
        missing = http.post("/v1/decide", json={"calls": [{"function": "triage.urgency", "input": "x"}]})
        unknown = http.post("/v1/decide", json={"calls": [{"function": "nope.fn", "input": "x"}]})
    with client(fake({"jeff.safety.spam"})) as http:
        not_installed = http.post("/v1/decide", json={"calls": [
            {"function": "triage.urgency", "input": "x", "context": CONTEXT}]})
    assert_error(missing, 422, "MissingContext", "company")
    assert_error(unknown, 404, "UnknownFunction", "nope")
    assert_error(not_installed, 424, "NotInstalled", "jeff.support.triage")


# --- wrong shapes (Review Focus 3) ---------------------------------------------------------------------------------

@pytest.mark.parametrize("body, named", [
    ({"calls": 5}, "/calls"),
    ({"calls": [{"function": "triage.urgency", "input": "x"}], "extra": 1}, "'extra'"),
    ({"calls": [{"function": "triage.urgency"}]}, "'input' is a required property"),
    ({"calls": [{"function": 7, "input": "x"}]}, "/calls/0/function"),
    ({"calls": [{"function": "f", "input": "x", "labels": "k1"}]}, "/calls/0/labels"),
    ({"calls": []}, "/calls"),
    ({"calls": [{"function": "f", "input": "x"}], "on_low_confidence": "maybe"}, "'maybe' is not one of"),
    ({"calls": [{"function": "f", "input": "x"}], "on_low_confidence": {"escalate": []}}, "/on_low_confidence"),
    ({"calls": [{"function": "f", "input": "x"}], "on_low_confidence": {"escalate": ["f"], "ms": 1}}, "'ms'"),
    ({"dispatch": {"input": "x"}}, "'interface' is a required property"),
    ({"dispatch": {"input": "x", "interface": "classify"}, "calls": []}, "'calls'"),
    ({"graph": {"id": "g"}, "input": "x"}, "'params' is a required property"),
    ({"graph": {"id": "g", "inline": {}}, "input": "x", "params": {}}, "/graph"),
    ({}, "required property"),
    ([1, 2], "is not of type 'object'"),
    (42, "is not of type 'object'"),
])
def test_valid_json_of_the_wrong_shape_is_422_invalid_request_naming_the_problem(body: Any, named: str) -> None:
    engine = fake()
    with client(engine) as http:
        response = http.post("/v1/decide", json=body)
    assert_error(response, 422, "InvalidRequest", named)
    assert engine.calls == []


def test_body_that_is_not_json_is_422_invalid_request() -> None:
    with client(fake()) as http:
        response = http.post("/v1/decide", content=b"{not json", headers={"content-type": "application/json"})
    assert_error(response, 422, "InvalidRequest", "not valid JSON")


def test_at_most_256_calls() -> None:
    call = {"function": "spam.message_type", "input": "x", "context": {"channel": "email"}}
    engine = fake()
    with client(engine) as http:
        allowed = http.post("/v1/decide", json={"calls": [call] * 256})
        refused = http.post("/v1/decide", json={"calls": [call] * 257})
    assert allowed.status_code == 200 and len(allowed.json()["decisions"]) == 256
    assert_error(refused, 422, "InvalidRequest", "257 items", "at most 256", "/calls")
    assert sum(len(rows) for _, rows in engine.calls) == 256


@pytest.mark.parametrize("content", [b'{"calls": "\xff\xfe"}', b"\xff" * 8], ids=["in-string", "bare"])
def test_body_that_is_not_utf8_is_422_invalid_request(content: bytes) -> None:
    with client(fake()) as http:
        response = http.post("/v1/decide", content=content, headers={"content-type": "application/json"})
    assert_error(response, 422, "InvalidRequest", "UTF-8")


@pytest.mark.parametrize("content", [b"[" * 100000, b'{"calls": ' + b"[" * 100000], ids=["array", "in-calls"])
def test_json_nested_too_deeply_is_422_invalid_request(content: bytes) -> None:
    with client(fake()) as http:
        response = http.post("/v1/decide", content=content, headers={"content-type": "application/json"})
    assert_error(response, 422, "InvalidRequest", "nested too deeply")


def test_deep_but_parseable_nesting_in_an_inline_graph_is_never_a_500(tmp_path: Path) -> None:
    raw = yaml.safe_load((code_free_graph(tmp_path) / "support_intake.yaml").read_text())
    condition: dict[str, Any] = {"node": "kind", "is": "legitimate"}
    for _ in range(400):
        condition = {"not": condition}
    raw["nodes"]["team"]["when"] = condition
    with client(fake()) as http:
        response = http.post("/v1/decide", json={"graph": {"inline": raw}, "input": "x", "params": PARAMS})
    assert response.status_code in (200, 422), response.text


def test_context_null_is_the_same_as_leaving_it_out() -> None:
    call = {"function": "spam.message_type", "input": "x"}
    with client(fake()) as http:
        explicit = http.post("/v1/decide", json={"calls": [{**call, "context": None}]})
        absent = http.post("/v1/decide", json={"calls": [call]})
    assert_error(explicit, 422, "MissingContext", "channel")
    assert explicit.json()["error"] == absent.json()["error"]


def test_context_null_with_a_function_that_has_no_context_fields() -> None:
    call = {"function": "jeff.core.router.domain", "input": "x", "labels": ["jeff.safety.spam"]}
    with client(fake(ROUTED), ROUTERS) as http:
        explicit = http.post("/v1/decide", json={"calls": [{**call, "context": None, "labels": call["labels"]}]})
        absent = http.post("/v1/decide", json={"calls": [call]})
    assert explicit.status_code == 200, explicit.text
    assert explicit.json() == absent.json()


def test_labels_of_the_wrong_type_are_not_reported_against_null() -> None:
    with client(fake()) as http:
        response = http.post("/v1/decide", json={"calls": [{"function": "f", "input": "x", "labels": 5}]})
    error = assert_error(response, 422, "InvalidRequest", "/calls/0/labels")
    assert "'null'" not in error["message"]


def test_escalation_steps_are_capped_at_256() -> None:
    call = {"function": "triage.urgency", "input": "sure", "context": CONTEXT}
    with client(fake()) as http:
        allowed = http.post("/v1/decide", json={"calls": [call],
                                                "on_low_confidence": {"escalate": ["triage.urgency"] * 256}})
        refused = http.post("/v1/decide", json={"calls": [call],
                                                "on_low_confidence": {"escalate": ["triage.urgency"] * 257}})
    assert allowed.status_code == 200, allowed.text
    assert_error(refused, 422, "InvalidRequest", "257 items", "at most 256", "/on_low_confidence/escalate")


def many_node_graph(count: int) -> dict[str, Any]:
    nodes = {f"n{index}": {"call": "jeff.safety.spam.message_type", "context": {"channel": {"param": "channel"}}}
             for index in range(count)}
    return {"graph": "many", "version": "1.0.0", "input": "message", "params": ["channel"], "nodes": nodes,
            "outputs": ["n0"]}


def test_inline_graphs_and_per_node_policies_are_capped_at_256_nodes() -> None:
    params = {"channel": "email"}
    allowed_graph = many_node_graph(256)
    by_node = {name: "error" for name in allowed_graph["nodes"]}
    with client(fake()) as http:
        allowed = http.post("/v1/decide", json={"graph": {"inline": allowed_graph}, "input": "x", "params": params,
                                                "on_low_confidence_by_node": by_node})
        too_many_nodes = http.post("/v1/decide", json={"graph": {"inline": many_node_graph(257)}, "input": "x",
                                                       "params": params})
        too_many_policies = http.post("/v1/decide", json={
            "graph": {"inline": allowed_graph}, "input": "x", "params": params,
            "on_low_confidence_by_node": {**by_node, "extra": "error"}})
    assert allowed.status_code == 200, allowed.text
    assert len(allowed.json()["graph_result"]["nodes"]) == 256
    assert_error(too_many_nodes, 422, "InvalidRequest", "257 properties", "at most 256", "/graph/inline/nodes")
    assert_error(too_many_policies, 422, "InvalidRequest", "257 properties", "at most 256",
                 "/on_low_confidence_by_node")


@pytest.mark.parametrize("step", ["llm", "human", "remote", {"llm": "gpt"}, {"function": "triage.urgency"}])
def test_llm_human_and_remote_escalation_steps_are_refused_over_http(step: Any) -> None:
    call = {"function": "triage.urgency", "input": "x", "context": CONTEXT}
    engine = fake()
    with client(engine, graphs=None) as http:
        calls = http.post("/v1/decide", json={"calls": [call], "on_low_confidence": {"escalate": [step]}})
        by_node = http.post("/v1/decide", json={"graph": {"id": "g"}, "input": "x", "params": {},
                                                "on_low_confidence_by_node": {"urgency": {"escalate": [step]}}})
    for response in (calls, by_node):
        assert_error(response, 422, "InvalidRequest", "function references only", "run in the SDK")
    assert engine.calls == []


@pytest.mark.parametrize("interface", ["detect", "equivalent", "is_supported", "classify"])
def test_dispatch_to_an_interface_the_routing_tree_does_not_reach_is_invalid_request(interface: str) -> None:
    engine = fake(ROUTED)
    with client(engine, ROUTERS) as http:
        response = http.post("/v1/decide", json={"dispatch": {"input": "x", "interface": interface,
                                                              "router": "jeff.support.router"}})
    assert_error(response, 422, "InvalidRequest", "jeff.support.router", interface)
    assert engine.calls == []


def test_dispatch_from_the_default_router_to_an_unreached_interface_is_invalid_request() -> None:
    with client(fake(ROUTED), ROUTERS) as http:
        response = http.post("/v1/decide", json={"dispatch": {"input": "x", "interface": "detect"}})
    assert_error(response, 422, "InvalidRequest", "jeff.core.router", "detect")


def test_unknown_interface_in_dispatch_is_invalid_request() -> None:
    with client(fake()) as http:
        response = http.post("/v1/decide", json={"dispatch": {"input": "x", "interface": "summarise"}})
    assert_error(response, 422, "InvalidRequest", "summarise", "classify")


# --- dispatch ------------------------------------------------------------------------------------------------------

ROUTED = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}


def test_dispatch_answers_the_same_routed_decision_as_the_sdk() -> None:
    with client(fake(ROUTED), ROUTERS) as http:
        response = http.post("/v1/decide", json={"dispatch": {"input": "My wardrobe is broken", "interface": "rate",
                                                              "context": CONTEXT}})
    assert response.status_code == 200, response.text
    body = response.json()
    contract.validate("decide_response", body, "response")
    decision = Decision.from_json(body["decision"])
    assert decision == local(fake(ROUTED), ROUTERS).dispatch("My wardrobe is broken", interface="rate",
                                                             context=CONTEXT)
    assert [step.router for step in decision.route] == ["jeff.core.router.domain", "jeff.support.router.function"]


def test_dispatch_from_a_named_router_with_labels() -> None:
    request = {"input": "x", "interface": "route", "context": CONTEXT, "labels": TEAMS,
               "router": "jeff.support.router"}
    with client(fake(ROUTED), ROUTERS) as http:
        response = http.post("/v1/decide", json={"dispatch": request})
    assert response.status_code == 200, response.text
    decision = Decision.from_json(response.json()["decision"])
    assert decision.function == "jeff.support.triage.route"
    assert decision == local(fake(ROUTED), ROUTERS).dispatch("x", interface="route", context=CONTEXT, labels=TEAMS,
                                                             router="jeff.support.router")


def test_dispatch_with_an_abstaining_router_returns_it_or_answers_409() -> None:
    def answer(adapter: str, row: dict[str, Any]) -> list[float]:
        count = len(row["question"]["criteria"])
        return [1.0 / count] * count  # jeff.core.router's threshold is 0.8: with two targets it abstains

    request = {"input": "x", "interface": "rate", "context": CONTEXT}
    with client(fake(ROUTED, answer), ROUTERS) as http:
        returned = http.post("/v1/decide", json={"dispatch": request})
        refused = http.post("/v1/decide", json={"dispatch": request, "on_low_confidence": "error"})
    expected = local(fake(ROUTED, answer), ROUTERS).dispatch("x", interface="rate", context=CONTEXT)
    assert expected.abstained is True and expected.function == "jeff.core.router.domain"
    assert returned.status_code == 200, returned.text
    assert Decision.from_json(returned.json()["decision"]) == expected
    error = assert_error(refused, 409, "LowConfidence")
    assert Decision.from_json(error["details"]["decision"]) == expected


# --- graphs --------------------------------------------------------------------------------------------------------

PARAMS: dict[str, Any] = {"company": "A furniture shop.", "channel": "email", "teams": TEAMS}


def test_graph_by_id_answers_the_same_result_as_the_sdk(tmp_path: Path) -> None:
    folder = code_free_graph(tmp_path)
    with client(fake(), graphs=folder) as http:
        response = http.post("/v1/decide", json={"graph": {"id": "jeff.graphs.support_intake"},
                                                 "input": "My wardrobe is broken", "params": PARAMS})
    assert response.status_code == 200, response.text
    body = response.json()
    contract.validate("decide_response", body, "response")
    expected = local(fake()).run_graph(folder / "support_intake.yaml", "My wardrobe is broken", PARAMS)
    assert GraphResult.from_json(body["graph_result"]) == expected
    assert body["graph_result"]["stages"] == 3


def test_graph_inline_with_per_node_policies(tmp_path: Path) -> None:
    raw = yaml.safe_load((code_free_graph(tmp_path) / "support_intake.yaml").read_text())
    with client(fake()) as http:
        returned = http.post("/v1/decide", json={"graph": {"inline": raw}, "input": "unsure", "params": PARAMS})
        refused = http.post("/v1/decide", json={"graph": {"inline": raw}, "input": "unsure", "params": PARAMS,
                                                "on_low_confidence_by_node": {"urgency": "error"}})
        unknown_node = http.post("/v1/decide", json={"graph": {"inline": raw}, "input": "x", "params": PARAMS,
                                                     "on_low_confidence_by_node": {"nope": "error"}})
    assert returned.status_code == 200, returned.text
    expected = local(fake()).run_graph(tmp_path / "graphs" / "support_intake.yaml", "unsure", PARAMS)
    assert GraphResult.from_json(returned.json()["graph_result"]) == expected
    error = assert_error(refused, 409, "LowConfidence")
    assert error["details"]["decision"]["function"] == "jeff.support.triage.urgency"
    assert_error(unknown_node, 422, "GraphError", "nope")


def test_inline_graph_with_a_code_node_is_422_graph_error() -> None:
    raw = yaml.safe_load(GRAPH.read_text())
    engine = fake()
    with client(engine) as http:
        response = http.post("/v1/decide", json={"graph": {"inline": raw}, "input": "x", "params": PARAMS})
    assert_error(response, 422, "GraphError", "ticket", "code")
    assert engine.calls == []


def test_unknown_graph_id_is_graph_error_naming_the_registered_ones(tmp_path: Path) -> None:
    with client(fake(), graphs=code_free_graph(tmp_path)) as http:
        response = http.post("/v1/decide", json={"graph": {"id": "jeff.graphs.nope"}, "input": "x", "params": {}})
    assert_error(response, 422, "GraphError", "jeff.graphs.nope", "jeff.graphs.support_intake")


def test_graphs_lists_the_registered_graphs(tmp_path: Path) -> None:
    folder = code_free_graph(tmp_path)
    with client(fake(), graphs=folder) as http:
        listed = http.get("/v1/graphs")
    with client(fake()) as http:
        none = http.get("/v1/graphs")
    assert listed.status_code == 200
    contract.validate("graphs_response", listed.json(), "response")
    assert listed.json() == {"graphs": [yaml.safe_load((folder / "support_intake.yaml").read_text())]}
    assert none.json() == {"graphs": []}


def test_a_registered_graph_with_code_nodes_is_refused_at_startup(tmp_path: Path) -> None:
    folder = tmp_path / "graphs"
    folder.mkdir()
    shutil.copy(GRAPH, folder / "support_intake.yaml")
    with pytest.raises(GraphError, match="ticket"):
        with client(fake(), graphs=folder):
            pass


def test_two_registered_graphs_with_one_id_are_refused_at_startup(tmp_path: Path) -> None:
    folder = code_free_graph(tmp_path)
    shutil.copy(folder / "support_intake.yaml", folder / "again.yaml")
    with pytest.raises(GraphError, match="jeff.graphs.support_intake"):
        with client(fake(), graphs=folder):
            pass


def test_a_missing_graphs_folder_is_refused_at_startup(tmp_path: Path) -> None:
    with pytest.raises(GraphError, match="nowhere"):
        with client(fake(), graphs=tmp_path / "nowhere"):
            pass


@pytest.mark.parametrize("text, named", [("graph: [unclosed\n", "not valid YAML"),
                                          ("- just\n- a list\n", "not a mapping"),
                                          ("", "not a mapping")])
def test_a_registered_graph_file_that_is_not_a_yaml_mapping_is_refused_at_startup(
        tmp_path: Path, text: str, named: str) -> None:
    folder = code_free_graph(tmp_path)
    (folder / "broken.yaml").write_text(text)
    with pytest.raises(GraphError, match=named) as error:
        with client(fake(), graphs=folder):
            pass
    assert "broken.yaml" in str(error.value)


# --- functions -----------------------------------------------------------------------------------------------------

def test_functions_lists_exactly_the_installed_adapters_functions() -> None:
    with client(fake({"jeff.support.triage"})) as http:
        response = http.get("/v1/functions")
    assert response.status_code == 200
    body = response.json()
    contract.validate("functions_response", body, "response")
    by_id = {item["id"]: item for item in body["functions"]}
    assert set(by_id) == {"jeff.support.triage.route", "jeff.support.triage.urgency",
                          "jeff.support.triage.needs_human"}
    raw = yaml.safe_load((CATALOGUE / "jeff.support.triage" / "manifest.yaml").read_text())
    urgency = by_id["jeff.support.triage.urgency"]
    assert urgency == {"id": "jeff.support.triage.urgency", "aliases": ["triage.urgency"], "interface": "rate",
                       "type": "score",
                       "labels": raw["functions"]["urgency"]["labels"],
                       "context": [{**field, "code_name": field["key"]} for field in raw["context"]],
                       "input": {**raw["input"], "code_name": raw["input"]["key"]},
                       "calibration": raw["functions"]["urgency"]["calibration"],
                       "adapter_version": "1.3.0", "licence": raw["licence"]}
    assert by_id["jeff.support.triage.route"]["calibration"] is None


def versioned_triage(tmp_path: Path) -> Path:
    """The fixture catalogue with triage under an ID that has a version part, jeff.support.triage-v14, answering to
    its unversioned ID jeff.support.triage as well as to triage."""
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    shutil.move(catalogue / "jeff.support.triage", catalogue / "jeff.support.triage-v14")
    path = catalogue / "jeff.support.triage-v14" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["id"], raw["aliases"] = "jeff.support.triage-v14", ["triage", "jeff.support.triage"]
    path.write_text(yaml.safe_dump(raw))
    return catalogue


def test_a_call_through_the_unversioned_id_is_answered_by_the_versioned_function(tmp_path: Path) -> None:
    catalogue = versioned_triage(tmp_path)
    engine = fake({"jeff.safety.spam", "jeff.support.triage-v14"})
    calls = [{"function": "jeff.support.triage.urgency", "input": "It is on fire", "context": CONTEXT},
             {"function": "jeff.support.triage-v14.urgency", "input": "It is on fire", "context": CONTEXT}]
    with client(engine, catalogue) as http:
        response = http.post("/v1/decide", json={"calls": calls})
        listed = http.get("/v1/functions").json()
    assert response.status_code == 200, response.text
    contract.validate("decide_response", response.json(), "response")
    through_alias, pinned = response.json()["decisions"]
    assert through_alias["function"] == pinned["function"] == "jeff.support.triage-v14.urgency"
    assert through_alias["decided_by"] == "jeff.support.triage-v14.urgency@1.3.0"
    assert {adapter for adapter, _ in engine.calls} == {"jeff.support.triage-v14"}
    contract.validate("functions_response", listed, "response")
    by_id = {item["id"]: item for item in listed["functions"]}
    assert by_id["jeff.support.triage-v14.urgency"]["aliases"] == ["triage.urgency", "jeff.support.triage.urgency"]


def test_a_versioned_id_no_adapter_has_is_an_unknown_function(tmp_path: Path) -> None:
    """Another version part than the adapter's is not resolved to it: the alias is only the ID without its version."""
    with client(fake({"jeff.safety.spam", "jeff.support.triage-v14"}), versioned_triage(tmp_path)) as http:
        response = http.post("/v1/decide", json={
            "calls": [{"function": "jeff.support.triage-v15.urgency", "input": "x", "context": CONTEXT}]})
    assert_error(response, 404, "UnknownFunction", "jeff.support.triage-v15")


def test_a_field_with_spaces_is_listed_and_called_by_code_name_and_reaches_the_engine_by_its_key(
        tmp_path: Path) -> None:
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    path = catalogue / "jeff.safety.spam" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["context"] = [{"key": "counterparty name", "description": "Who the message is from.", "required": True}]
    path.write_text(yaml.safe_dump(raw))
    engine = fake({"jeff.safety.spam"})
    call = {"function": "spam.message_type", "input": "WIN NOW"}
    with client(engine, catalogue) as http:
        listed = http.get("/v1/functions").json()["functions"][0]
        decided = http.post("/v1/decide", json={"calls": [{**call, "context": {"counterparty_name": "Acme"}}]})
        exact = http.post("/v1/decide", json={"calls": [{**call, "context": {"counterparty name": "Acme"}}]})
    assert listed["context"] == [{**raw["context"][0], "code_name": "counterparty_name"}]
    assert decided.status_code == 200, decided.text
    assert engine.calls[0][1][0]["state"] == {"counterparty name": "Acme", "message": "WIN NOW"}
    assert_error(exact, 422, "UnknownContext", "'counterparty name' as counterparty_name")


def test_dispatch_over_http_takes_code_names_and_refuses_an_exact_key(tmp_path: Path) -> None:
    routers = tmp_path / "routers"
    shutil.copytree(ROUTERS, routers)
    path = routers / "jeff.support.triage" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["context"][0]["key"] = "company name"
    path.write_text(yaml.safe_dump(raw))
    engine = fake(ROUTED)
    dispatch = {"input": "My wardrobe is broken", "interface": "rate"}
    with client(engine, routers) as http:
        by_code = http.post("/v1/decide", json={"dispatch": {
            **dispatch, "context": {"company_name": "A shop.", "channel": "email"}}})
        calls = len(engine.calls)
        exact = http.post("/v1/decide", json={"dispatch": {
            **dispatch, "context": {"company name": "A shop.", "channel": "email"}}})
    assert by_code.status_code == 200, by_code.text
    assert by_code.json()["decision"]["function"] == "jeff.support.triage.urgency"
    assert engine.calls[-1][1][0]["state"] == {"company name": "A shop.", "channel": "email",
                                               "message": "My wardrobe is broken"}
    assert_error(exact, 422, "UnknownContext", "'company name' as company_name")
    assert len(engine.calls) == calls


def test_functions_does_not_list_an_installed_adapter_without_a_catalogue_manifest() -> None:
    with client(fake({"jeff.support.triage", "jeff.unknown.adapter"})) as http:
        response = http.get("/v1/functions")
    assert response.status_code == 200
    ids = {item["id"] for item in response.json()["functions"]}
    assert ids == {"jeff.support.triage.route", "jeff.support.triage.urgency", "jeff.support.triage.needs_human"}


# --- golden cases (fast): HTTP equals local mode over the same engine -----------------------------------------------

def hashed(adapter: str, row: dict[str, Any]) -> list[float]:
    """Probabilities that depend on the whole row, so a request that reached the engine differently would differ."""
    question = row["question"]
    count = len(question["criteria"]) if "criteria" in question else 2
    digest = hashlib.sha256(json.dumps([adapter, row], sort_keys=True).encode()).digest()
    weights = [1.0 + digest[index % len(digest)] for index in range(count)]
    return [weight / sum(weights) for weight in weights]  # summing to 1, as the engine's softmax does


def test_golden_calls_through_http_equal_local_mode() -> None:
    """The golden cases' expected values come from the real model (Task 9 checks those over HTTP, slow); here every
    golden call goes through POST /v1/decide and must answer exactly what Jeff in-process answers over the same
    FakeEngine and the shipped catalogue."""
    cases = json.loads((shipped_dir() / "contract" / "golden" / "step1.json").read_text())["cases"]
    catalogue = shipped_dir() / "catalogue"
    installed = {manifest.id for manifest in Catalogue.from_dir(catalogue).manifests()}
    calls = [case["call"] for case in cases]
    with client(fake(installed, hashed), catalogue) as http:
        response = http.post("/v1/decide", json={"calls": calls})
    assert response.status_code == 200, response.text
    expected = local(fake(installed, hashed), catalogue).decide([Call(**call) for call in calls])
    assert [Decision.from_json(item) for item in response.json()["decisions"]] == expected
