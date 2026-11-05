"""Jeff.remote: local mode's surface over a Jeff server. Every operation is one request and answers exactly what local
mode answers over the same engine and catalogue; policies with function steps run on the server, llm and
human steps in the SDK; a catalogue whose adapter versions differ from the server's is refused at connect; every
server error is raised as the same JeffError class, carrying the response's request id over HTTP.

Every test taking the `mode` fixture runs twice: over HTTP (channel=False) and over the channel (channel=True), where
a request is one frame and binding a function is one `bind` frame.

The server runs in this process (served.jeff_server: jeff_server.create_app over a FakeEngine, served by a real uvicorn
on a free port)."""

import asyncio
import json
import re
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
import yaml
from starlette.applications import Starlette
from starlette.responses import JSONResponse, PlainTextResponse, Response
from starlette.routing import Route

from jeff import contract
from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.decision import Decision
from jeff.errors import (BackendUnavailable, Busy, ContractError, DeadlineExceeded, EngineError, GraphError,
                         InvalidRequest, JeffError, LowConfidence,
                         NotInstalled, NotLicensed, Unauthorised, UnknownFunction)
from jeff.escalation import Escalate, FunctionStep, Question, escalate, function_step, llm
from jeff.remote import MAX_CALLS, RestTransport, server_policy

from served import (CALLS, CATALOGUE, CONTEXT, FIXTURE_ADAPTERS, GRAPH, PARAMS, ROUTED, ROUTERS, TEAMS, TIMEOUT_S,
                    UNSURE, Mode, Requests, answers, fake, free_port, http_client, hub_keys, hub_token, jeff_server,
                    local, proprietary, remote, serving, without_timing)


def with_backup(tmp_path: Path, calibrated: bool = False) -> Path:
    """The fixture catalogue plus jeff.support.backup: a second rate function (backup.urgency), uncalibrated (it
    never abstains) unless `calibrated`, when it has triage.urgency's calibration."""
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    second = yaml.safe_load((CATALOGUE / "jeff.support.triage" / "manifest.yaml").read_text())
    second["id"], second["aliases"] = "jeff.support.backup", ["backup"]
    urgency = second["functions"]["urgency"]
    second["functions"] = {"urgency": urgency if calibrated else {**urgency, "calibration": None}}
    (catalogue / "jeff.support.backup").mkdir()
    (catalogue / "jeff.support.backup" / "manifest.yaml").write_text(yaml.safe_dump(second))
    return catalogue


def with_version(tmp_path: Path, adapter: str, version: str) -> Path:
    catalogue = tmp_path / "server-catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    path = catalogue / adapter / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["version"] = version
    path.write_text(yaml.safe_dump(manifest))
    return catalogue


def assert_request_id(error: JeffError, mode: Mode = Mode(False)) -> None:
    """Over HTTP the error carries the response's x-request-id; over the channel there is none, and the error
    carries the connection's id (channel_id) instead."""
    request_id = getattr(error, "request_id", None)
    if mode.request_ids:
        assert isinstance(request_id, str) and request_id, vars(error)
        assert not hasattr(error, "channel_id"), vars(error)
    else:
        assert request_id is None, vars(error)
        assert re.fullmatch(r"[0-9a-f]{8}", getattr(error, "channel_id", "")), vars(error)


# --- the same answers as local mode, one request per operation ------------------------------------------------------

def test_connecting_reads_the_function_list_once(mode: Mode) -> None:
    with jeff_server(fake()) as url, mode.remote(url) as client:
        assert client.engine.installed() == FIXTURE_ADAPTERS
        counter = Requests(client)
        assert client.engine.installed() == FIXTURE_ADAPTERS
        assert counter.take() == []
        assert client.engine.installed(refresh=True) == FIXTURE_ADAPTERS
        assert counter.take() == ["GET /v1/functions"]


def test_decide_equals_local_mode_field_for_field_in_one_request(mode: Mode) -> None:
    calls = [Call(**call) for call in CALLS]
    expected = local(fake()).decide(calls)
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter = Requests(client)
        decisions = client.decide(calls)
        assert counter.take() == mode.sent("decide")
    assert decisions == expected
    for got, want in zip(decisions, expected, strict=True):
        assert (got.value, got.confidence, got.probabilities, got.abstained, got.calibrated, got.route) == \
            (want.value, want.confidence, want.probabilities, want.abstained, want.calibrated, want.route)


def test_the_interface_shorthands_equal_local_mode_in_one_request_each(mode: Mode) -> None:
    def ask(jeff: Jeff) -> list[Decision]:
        return [
            jeff.route("My wardrobe arrived broken", function="triage.route", context=CONTEXT, labels=TEAMS),
            jeff.rate("It is on fire", function="triage.urgency", context=CONTEXT),
            jeff.requires_escalation("Call me back", function="triage.needs_human", context=CONTEXT),
            jeff.classify("WIN NOW", function="spam.message_type", context={"channel": "email"}),
        ]

    expected = ask(local(fake()))
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter = Requests(client)
        got = ask(client)
        assert counter.take() == mode.sent("decide") * 4
    assert got == expected


def test_dispatch_from_the_default_and_a_named_router_equals_local_mode_in_one_request_each(mode: Mode) -> None:
    def ask(jeff: Jeff) -> list[Decision]:
        return [
            jeff.dispatch("My wardrobe is broken", interface="rate", context=CONTEXT),
            jeff.dispatch("My wardrobe is broken", interface="rate", context=CONTEXT, router="jeff.support.router"),
            jeff.rate("My wardrobe is broken", context=CONTEXT),  # no function=: routed
        ]

    expected = ask(local(fake(ROUTED), ROUTERS))
    with jeff_server(fake(ROUTED), ROUTERS) as url, mode.remote(url, catalogue=ROUTERS) as client:
        counter = Requests(client)
        got = ask(client)
        assert counter.take() == mode.sent("dispatch") * 3
    assert got == expected
    assert [step.router for step in got[0].route] == ["jeff.core.router.domain", "jeff.support.router.function"]


def test_a_bound_function_equals_local_mode_in_one_request_per_call(mode: Mode) -> None:
    """Over HTTP binding sends nothing and each call or many() is one POST /v1/decide; over the channel binding is
    one bind frame, then each call one call frame and each many() one call_many frame."""
    inputs = ["It is on fire", "unsure about this", "Whenever"]

    def ask(jeff: Jeff) -> tuple[Decision, list[Decision]]:
        bound = jeff.function("triage.urgency", context=CONTEXT)
        return bound("It is on fire"), bound.many(inputs)

    expected = ask(local(fake()))
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter = Requests(client)
        bound = client.function("triage.urgency", context=CONTEXT)
        assert counter.take() == mode.sent("bind")
        one = bound("It is on fire")
        assert counter.take() == mode.sent("call")
        many = bound.many(inputs)
        assert counter.take() == mode.sent("call_many")
    assert (one, many) == expected
    assert many[1].abstained is True


def test_no_calls_answer_nothing_without_a_request_as_local_mode(mode: Mode) -> None:
    jeff = local(fake())
    assert jeff.decide([]) == [] and jeff.function("triage.urgency", context=CONTEXT).many([]) == []
    with jeff_server(fake()) as url, mode.remote(url) as client:
        bound = client.function("triage.urgency", context=CONTEXT)
        counter = Requests(client)
        assert client.decide([]) == []
        assert bound.many([]) == []
        assert counter.take() == []


def test_more_than_256_calls_are_refused_naming_the_limit_without_a_request(mode: Mode) -> None:
    """The server answers at most 256 calls (or inputs) per request; more are refused here, never split. Local mode
    has no such limit: it is the server's."""
    schemas = contract.load_schema("decide_request"), contract.load_schema("socket")
    assert MAX_CALLS == schemas[0]["oneOf"][0]["properties"]["calls"]["maxItems"] == \
        schemas[1]["$defs"]["call_many"]["properties"]["inputs"]["maxItems"] == 256
    calls = [Call("triage.urgency", f"message {index}", CONTEXT) for index in range(257)]
    assert len(local(fake()).decide(calls)) == 257
    with jeff_server(fake()) as url, mode.remote(url) as client:
        bound = client.function("triage.urgency", context=CONTEXT)
        counter = Requests(client)
        with pytest.raises(InvalidRequest, match="257 calls in one request.*at most 256"):
            client.decide(calls)
        with pytest.raises(InvalidRequest, match="257 inputs in one request.*at most 256"
                           if mode.channel else "257 calls in one request.*at most 256"):
            bound.many([call.input for call in calls])
        assert counter.take() == []
        assert len(client.decide(calls[:256])) == 256
        assert len(bound.many([call.input for call in calls[:256]])) == 256


def test_an_adapter_the_local_catalogue_lacks_is_not_callable(tmp_path: Path, mode: Mode) -> None:
    server_catalogue = with_backup(tmp_path)
    installed = FIXTURE_ADAPTERS | {"jeff.support.backup"}
    with jeff_server(fake(installed), server_catalogue) as url, mode.remote(url) as client:
        assert "jeff.support.backup" in client.engine.installed()
        counter = Requests(client)
        with pytest.raises(UnknownFunction, match="jeff.support.backup"):
            client.decide([Call("jeff.support.backup.urgency", "x", CONTEXT)])
        assert counter.take() == []


# --- policies -------------------------------------------------------------------------------------------------------

def test_server_policy_gives_the_wire_form_of_policies_the_server_can_run() -> None:
    assert server_policy("return") == "return"
    assert server_policy("error") == "error"
    assert server_policy(escalate(function_step("a.b.c.d"), function_step("e.f.g.h"))) == \
        {"escalate": ["a.b.c.d", "e.f.g.h"]}
    assert server_policy(Escalate((FunctionStep("a.b.c.d"),), 250.0)) == {"escalate": ["a.b.c.d"], "budget_ms": 250.0}
    assert server_policy(escalate(function_step("a.b.c.d"), llm(lambda question: "x"))) is None


def test_on_low_confidence_error_raises_low_confidence_carrying_the_decision(mode: Mode) -> None:
    [expected] = local(fake()).decide([Call("triage.urgency", "unsure", CONTEXT)])
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter = Requests(client)
        with pytest.raises(LowConfidence) as raised:
            client.rate("unsure", function="triage.urgency", context=CONTEXT, on_low_confidence="error")
        assert counter.take() == mode.sent("decide")
        with pytest.raises(LowConfidence) as bound_raised:
            client.function("triage.urgency", context=CONTEXT, on_low_confidence="error")("unsure")
        assert counter.take() == mode.sent("bind") + mode.sent("call")
    for error in (raised.value, bound_raised.value):
        assert error.decision == expected
        assert_request_id(error, mode)


def test_an_escalation_through_a_function_step_runs_on_the_server_in_one_request(tmp_path: Path, mode: Mode) -> None:
    catalogue = with_backup(tmp_path)
    installed = FIXTURE_ADAPTERS | {"jeff.support.backup"}

    def answer(adapter: str, row: dict[str, Any]) -> list[float]:
        return UNSURE if adapter == "jeff.support.triage" else [0.0, 0.0, 0.0, 0.95, 0.05]

    policy = escalate(function_step("backup.urgency"), budget_ms=5000)
    expected = local(fake(installed, answer), catalogue).function("triage.urgency", context=CONTEXT,
                                                                  on_low_confidence=policy)("x")
    engine = fake(installed, answer)
    with jeff_server(engine, catalogue) as url, mode.remote(url, catalogue=catalogue) as client:
        counter = Requests(client)
        got = client.function("triage.urgency", context=CONTEXT, on_low_confidence=policy)("x")
        assert counter.take() == mode.sent("bind") + mode.sent("call")
    assert got == expected and got.decided_by == "jeff.support.backup.urgency@1.3.0"
    assert [adapter for adapter, _ in engine.calls] == ["jeff.support.triage", "jeff.support.backup"]


def test_an_llm_step_runs_in_the_sdk_with_the_same_question_as_local_mode(mode: Mode) -> None:
    asked: list[Question] = []

    def ask(question: Question) -> str:
        asked.append(question)
        return "3"

    policy = escalate(llm(ask))
    expected = local(fake()).rate("unsure", function="triage.urgency", context=CONTEXT, on_low_confidence=policy)
    with jeff_server(fake()) as url, mode.remote(url) as client:
        counter = Requests(client)
        got = client.rate("unsure", function="triage.urgency", context=CONTEXT, on_low_confidence=policy)
        assert counter.take() == mode.sent("decide")
        bound = client.function("triage.urgency", context=CONTEXT, on_low_confidence=policy)
        from_bound = bound.many(["unsure", "It is on fire"])
        assert counter.take() == mode.sent("bind") + mode.sent("call_many")
    assert len(asked) == 3 and asked[1] == asked[0] and asked[2] == asked[0]
    assert without_timing(got) == without_timing(expected) and got.decided_by == "llm" and got.value == 3
    assert without_timing(from_bound[0]) == without_timing(expected)
    assert from_bound[1].abstained is False and from_bound[1].decided_by == "jeff.support.triage.urgency@1.3.0"


def test_a_function_step_before_an_llm_step_costs_one_more_request(tmp_path: Path, mode: Mode) -> None:
    """The policy has an llm step, so the SDK runs all of it: the first request returns the abstained decision, the
    function step is one more request, and the llm answers when that one abstains too."""
    catalogue = with_backup(tmp_path, calibrated=True)
    installed = FIXTURE_ADAPTERS | {"jeff.support.backup"}
    asked: list[Question] = []

    def ask(question: Question) -> str:
        asked.append(question)
        return "4"

    def answer(adapter: str, row: dict[str, Any]) -> list[float]:
        return UNSURE

    policy = escalate(function_step("backup.urgency"), llm(ask))
    expected = local(fake(installed, answer), catalogue).rate("x", function="triage.urgency", context=CONTEXT,
                                                               on_low_confidence=policy)
    with jeff_server(fake(installed, answer), catalogue) as url, mode.remote(url, catalogue=catalogue) as client:
        counter = Requests(client)
        got = client.rate("x", function="triage.urgency", context=CONTEXT, on_low_confidence=policy)
        assert counter.take() == mode.sent("decide") * 2
    assert without_timing(got) == without_timing(expected)
    assert [item.decided_by for item in got.escalations] == ["jeff.support.triage.urgency@1.3.0",
                                                             "jeff.support.backup.urgency@1.3.0"]
    assert asked[0] == asked[1]


def test_a_dispatch_with_an_llm_step_escalates_the_leaf_in_the_sdk(mode: Mode) -> None:
    asked: list[Question] = []

    def ask(question: Question) -> str:
        asked.append(question)
        return "1"

    def answer(adapter: str, row: dict[str, Any]) -> list[float]:
        return UNSURE if row["question"]["type"] == "score" else answers(adapter, row)

    policy = escalate(llm(ask))
    expected = local(fake(ROUTED, answer), ROUTERS).dispatch("x", interface="rate", context=CONTEXT,
                                                             on_low_confidence=policy)
    with jeff_server(fake(ROUTED, answer), ROUTERS) as url, mode.remote(url, catalogue=ROUTERS) as client:
        counter = Requests(client)
        got = client.dispatch("x", interface="rate", context=CONTEXT, on_low_confidence=policy)
        assert counter.take() == mode.sent("dispatch")
    assert without_timing(got) == without_timing(expected) and got.decided_by == "llm"
    assert asked[0] == asked[1]


def test_run_server_graph_runs_a_registered_graph_in_one_request(tmp_path: Path, mode: Mode) -> None:
    raw = yaml.safe_load(GRAPH.read_text())
    del raw["nodes"]["ticket"]
    raw["outputs"] = ["kind", "team", "urgency"]
    folder = tmp_path / "graphs"
    folder.mkdir()
    (folder / "support_intake.yaml").write_text(yaml.safe_dump(raw))
    expected = local(fake()).run_graph(folder / "support_intake.yaml", "My wardrobe is broken", PARAMS)
    with jeff_server(fake(), graphs=folder) as url, mode.remote(url) as client:
        counter = Requests(client)
        got = client.run_server_graph("jeff.graphs.support_intake", "My wardrobe is broken", PARAMS)
        assert counter.take() == mode.sent("graph")
        with pytest.raises(GraphError, match="jeff.graphs.nope") as raised:  # 422
            client.run_server_graph("jeff.graphs.nope", "x", PARAMS)
    assert got == expected
    assert_request_id(raised.value, mode)


# --- versions (Review Focus 3) ---------------------------------------------------------------------------------------

def test_a_server_serving_another_version_of_an_adapter_is_refused_at_connect(tmp_path: Path, mode: Mode) -> None:
    server_catalogue = with_version(tmp_path, "jeff.support.triage", "1.4.0")
    with jeff_server(fake(), server_catalogue) as url:
        with pytest.raises(ContractError) as raised:
            mode.connect(url)
    message = str(raised.value)
    assert "jeff.support.triage" in message and "1.4.0" in message and "1.3.0" in message
    assert "catalogue=" in message


# --- errors ---------------------------------------------------------------------------------------------------------

def test_a_wrong_token_is_unauthorised_at_connect(mode: Mode) -> None:
    with jeff_server(fake(), api_key="the-key") as url:
        with pytest.raises(Unauthorised) as raised:
            mode.connect(url, "the-kez")
        assert_request_id(raised.value, mode)
        with pytest.raises(Unauthorised):
            mode.connect(url, None)
        with mode.remote(url, "the-key") as client:
            assert client.decide([Call(**CALLS[2])])[0].value


def test_a_callable_token_is_called_before_every_request() -> None:
    issued: list[str] = []

    def token() -> str:
        issued.append("the-key")
        return "the-key"

    with jeff_server(fake(), api_key="the-key") as url, remote(url, token) as client:
        assert len(issued) == 1  # GET /v1/functions
        client.decide([Call(**CALLS[2])])
        client.function("spam.message_type", context={"channel": "email"})("x")
        assert len(issued) == 3


def test_a_hub_token_without_the_licence_is_not_licensed(tmp_path: Path, mode: Mode) -> None:
    private, public = hub_keys(tmp_path)
    catalogue = proprietary(tmp_path, "jeff.support.triage")
    with jeff_server(fake(), catalogue, hub_public_key=public) as url:
        with mode.remote(url, hub_token(private, licences=[]), catalogue) as client:
            with pytest.raises(NotLicensed) as raised:
                client.rate("x", function="triage.urgency", context=CONTEXT)
            assert_request_id(raised.value, mode)
            assert client.decide([Call(**CALLS[2])])[0].value  # an open adapter still answers
        with mode.remote(url, hub_token(private, licences=["jeff.support.triage"]), catalogue) as licensed:
            assert licensed.rate("x", function="triage.urgency", context=CONTEXT).value == 2


def test_server_errors_are_raised_as_the_same_class_with_the_request_id(mode: Mode) -> None:
    engine = fake()
    with jeff_server(engine) as url, mode.remote(url) as client:
        with pytest.raises(UnknownFunction, match="nope") as unknown:  # 404
            client.transport.decide_calls([{"function": "nope.fn", "input": "x"}], "return")
        with pytest.raises(JeffError) as missing:  # 422
            client.transport.decide_calls([{"function": "triage.urgency", "input": "x"}], "return")
        engine.remove_adapter("jeff.support.triage")  # the client's list still has it
        with pytest.raises(NotInstalled, match="jeff.support.triage") as not_installed:  # 424
            client.rate("x", function="triage.urgency", context=CONTEXT)
    assert type(missing.value).__name__ == "MissingContext" and "company" in str(missing.value)
    for error in (unknown.value, missing.value, not_installed.value):
        assert_request_id(error, mode)


def test_busy_is_raised_with_its_retry_after(mode: Mode) -> None:
    def busy(adapter: str, row: dict[str, Any]) -> list[float]:
        raise Busy("The engine queue is full", retry_after_s=2.5)

    with jeff_server(fake(answer=busy)) as url, mode.remote(url) as client:
        with pytest.raises(Busy) as raised:  # 529
            client.decide([Call(**CALLS[2])])
    assert raised.value.retry_after_s == 2.5 and "queue is full" in str(raised.value)
    assert_request_id(raised.value, mode)


def test_a_refused_connection_is_backend_unavailable_naming_the_url(mode: Mode) -> None:
    url = f"http://127.0.0.1:{free_port()}"
    with pytest.raises(BackendUnavailable, match=url.replace(".", r"\.")):
        mode.connect(url)


def test_a_body_that_is_not_a_jeff_error_is_an_engine_error_naming_the_status() -> None:
    def broken(request: Any) -> PlainTextResponse:
        return PlainTextResponse("Bad gateway " + "x" * 1000, status_code=502)

    app = Starlette(routes=[Route("/v1/functions", broken)])
    with serving(app) as url:
        with pytest.raises(EngineError) as raised:
            Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(CATALOGUE))
    message = str(raised.value)
    assert "502" in message and "Bad gateway" in message and "x" * 300 not in message


def test_a_server_error_carrying_a_decision_that_fails_the_schema_is_contract_error() -> None:
    """s08 (jeff-dev-sdk-dotnet task-12 review, 2026-11-03): a 409 LowConfidence whose decision is missing a
    required field must be raised as ContractError naming what is wrong, as the Node SDK does for the same answer
    (packages/node/src/remote.ts), not a bare KeyError 'route' with no request id."""
    decision = local(fake()).decide([Call("triage.urgency", "unsure", CONTEXT)])[0].to_json()
    del decision["route"]

    def functions(request: Any) -> JSONResponse:
        return JSONResponse({"base_format": {"format": "bf16"}, "functions": []})

    def broken(request: Any) -> JSONResponse:
        return JSONResponse({"error": {"type": "LowConfidence", "message": "x", "details": {"decision": decision}}},
                            status_code=409, headers={"x-request-id": "rid-8"})

    app = Starlette(routes=[Route("/v1/functions", functions), Route("/v1/decide", broken, methods=["POST"])])
    with serving(app) as url:
        with Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(CATALOGUE)) as client:
            with pytest.raises(ContractError) as raised:
                client.transport.decide_calls([{"function": "triage.urgency", "input": "x"}], "error")
    message = str(raised.value)
    assert "LowConfidence" in message and "decision" in message and "route" in message
    assert raised.value.request_id == "rid-8"


def test_close_and_the_context_manager_close_the_connection() -> None:
    with jeff_server(fake()) as url:
        client = Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(CATALOGUE))
        client.close()
        assert http_client(client).is_closed
        with remote(url) as managed:
            pass
        assert http_client(managed).is_closed


# --- the SDK side of dispatch, canonical step IDs, refreshes, transport failures ------------------------------------

def router_unsure(adapter: str, row: dict[str, Any]) -> list[float]:
    """The first router abstains (0.5 is below jeff.core.router.domain's threshold 0.8); everything else as answers."""
    if adapter == "jeff.core.router":
        return [0.5, 0.5] + [0.0] * (len(row["question"]["criteria"]) - 2)
    return answers(adapter, row)


def test_a_router_abstaining_under_an_llm_policy_raises_low_confidence_as_local_mode_does(mode: Mode) -> None:
    asked: list[Question] = []

    def ask(question: Question) -> str:
        asked.append(question)
        return "1"

    policy = escalate(llm(ask))
    with pytest.raises(LowConfidence) as local_raised:
        local(fake(ROUTED, router_unsure), ROUTERS).dispatch("x", interface="rate", context=CONTEXT,
                                                             on_low_confidence=policy)
    with jeff_server(fake(ROUTED, router_unsure), ROUTERS) as url, mode.remote(url, catalogue=ROUTERS) as client:
        counter = Requests(client)
        with pytest.raises(LowConfidence) as raised:
            client.dispatch("x", interface="rate", context=CONTEXT, on_low_confidence=policy)
        assert counter.take() == mode.sent("dispatch")
    assert raised.value.decision == local_raised.value.decision
    assert raised.value.decision.function == "jeff.core.router.domain" and asked == []


def with_router_context(tmp_path: Path) -> Path:
    """The routers fixture with a context field (region) that only jeff.core.router takes."""
    catalogue = tmp_path / "routers"
    shutil.copytree(ROUTERS, catalogue)
    path = catalogue / "jeff.core.router" / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["context"] = [{"key": "region", "description": "Where the sender is.", "required": False}]
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))
    return catalogue


def test_a_dispatched_leaf_escalated_in_the_sdk_gets_only_its_own_context(tmp_path: Path, mode: Mode) -> None:
    catalogue = with_router_context(tmp_path)
    asked: list[Question] = []

    def ask(question: Question) -> str:
        asked.append(question)
        return "1"

    def answer(adapter: str, row: dict[str, Any]) -> list[float]:
        return UNSURE if row["question"]["type"] == "score" else answers(adapter, row)

    context = {**CONTEXT, "region": "EU"}
    policy = escalate(llm(ask))
    expected = local(fake(ROUTED, answer), catalogue).dispatch("x", interface="rate", context=context,
                                                               on_low_confidence=policy)
    with jeff_server(fake(ROUTED, answer), catalogue) as url, mode.remote(url, catalogue=catalogue) as client:
        got = client.dispatch("x", interface="rate", context=context, on_low_confidence=policy)
    assert len(asked) == 2 and asked[1] == asked[0] and "region" not in asked[0].state
    assert without_timing(got) == without_timing(expected) and got.decided_by == "llm"


def test_function_steps_are_sent_by_their_canonical_ids(tmp_path: Path) -> None:
    catalogue = with_backup(tmp_path)
    installed = FIXTURE_ADAPTERS | {"jeff.support.backup"}
    bodies: list[Any] = []
    policy = escalate(function_step("backup.urgency"))
    with jeff_server(fake(installed), catalogue) as url, remote(url, catalogue=catalogue) as client:
        http_client(client).event_hooks["request"].append(lambda request: bodies.append(json.loads(request.read())))
        client.rate("x", function="triage.urgency", context=CONTEXT, on_low_confidence=policy)
        client.function("triage.urgency", context=CONTEXT, on_low_confidence=policy).many(["x"])
    assert [body["on_low_confidence"] for body in bodies] == [{"escalate": ["jeff.support.backup.urgency"]}] * 2
    assert all(call["function"] == "jeff.support.triage.urgency" for body in bodies for call in body["calls"])


def test_a_refresh_that_finds_another_version_raises_contract_error(tmp_path: Path, mode: Mode) -> None:
    """The server's catalogue has jeff.support.backup at 2.0.0, not served at connect; once it is added, a refresh
    of a client whose catalogue has 1.3.0 refuses it."""
    client_catalogue = with_backup(tmp_path)
    server_catalogue = tmp_path / "server"
    shutil.copytree(client_catalogue, server_catalogue)
    path = server_catalogue / "jeff.support.backup" / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["version"] = "2.0.0"
    path.write_text(yaml.safe_dump(manifest))
    engine = fake()
    with jeff_server(engine, server_catalogue) as url, mode.remote(url, catalogue=client_catalogue) as client:
        assert client.engine.installed() == FIXTURE_ADAPTERS
        engine.add_adapter("jeff.support.backup", tmp_path)
        with pytest.raises(ContractError) as raised:
            client.engine.installed(refresh=True)
    message = str(raised.value)
    assert "jeff.support.backup" in message and "2.0.0" in message and "1.3.0" in message


def test_every_jeff_error_has_a_request_id() -> None:
    assert JeffError("x").request_id is None and UnknownFunction("x").request_id is None


def test_a_success_body_that_breaks_the_contract_is_contract_error_with_the_request_id() -> None:
    def functions(request: Any) -> JSONResponse:
        return JSONResponse({"functions": "none"}, headers={"x-request-id": "rid-1"})

    with serving(Starlette(routes=[Route("/v1/functions", functions)])) as url:
        with pytest.raises(ContractError, match="functions_response") as raised:
            Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(CATALOGUE))
    assert raised.value.request_id == "rid-1"


def stub_server() -> Starlette:
    """GET /v1/functions lists nothing; POST /v1/decide answers after 1 s; GET /v1/garbled sends a body that is not
    the gzip it says it is."""
    async def functions(request: Any) -> JSONResponse:
        return JSONResponse({"base_format": {"format": "bf16"}, "functions": []})

    async def slow(request: Any) -> JSONResponse:
        await asyncio.sleep(1)
        return JSONResponse({"decisions": []})

    async def garbled(request: Any) -> Response:
        return Response(b"not gzip", headers={"content-encoding": "gzip"})

    return Starlette(routes=[Route("/v1/functions", functions), Route("/v1/decide", slow, methods=["POST"]),
                             Route("/v1/garbled", garbled)])


def test_a_read_timeout_is_deadline_exceeded_saying_the_server_may_have_run_it() -> None:
    with serving(stub_server()) as url:
        with Jeff.remote(url, None, timeout_s=0.2, catalogue=Catalogue.from_dir(CATALOGUE)) as client:
            with pytest.raises(DeadlineExceeded) as raised:
                client.transport.decide_calls([{"function": "spam.message_type", "input": "x"}], "return")
    message = str(raised.value)
    assert url in message and "may" in message and "run" in message


def test_a_response_httpx_cannot_decode_is_a_jeff_error_naming_the_url() -> None:
    with serving(stub_server()) as url:
        with Jeff.remote(url, None, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(CATALOGUE)) as client:
            transport = cast(RestTransport, client.transport)
            with pytest.raises(EngineError) as raised:
                transport._request("GET", "/v1/garbled")
    assert url in str(raised.value)


def test_a_closed_client_raises_invalid_request(mode: Mode) -> None:
    with jeff_server(fake()) as url:
        client = mode.connect(url)
        client.close()
        with pytest.raises(InvalidRequest, match="closed"):
            client.decide([Call(**CALLS[2])])
        with pytest.raises(InvalidRequest, match="closed"):
            client.engine.installed(refresh=True)
