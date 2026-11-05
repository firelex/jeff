"""POST /v1/systemone and GET /v1/models, the legacy jeff-server API, unchanged for existing clients: the request
{model, state, questions, images?, orders?} and the response {model, answers, usage, timing} with jeff-server's
answer shapes and confidence formulas (jeff.answers) and its two-order averaging (jeff.orders). The base is the engine
adapter "base"; option limits and token limits come from the engine; a busy engine is 529 with Retry-After; a
proprietary adapter needs the caller's licence. Ported from jeff-server's tests/test_server_limits.py,
test_server_tokens.py, test_server_queue.py and test_client.py's real-server test."""

import base64
import importlib.util
import json
import shutil
import socket
import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from jeff import contract, systemone
from jeff.answers import answer
from jeff.engine import EngineResult
from jeff.errors import Busy, EngineError, InvalidRequest
from jeff.orders import average_orders, reverse_question
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine, fake_adapter_folder, fake_tokens
from jeff_runtime.testing import Running

from jeff_server.app import create_app
from jeff_server.router import EngineRouter
from jeff_server.settings import ServerSettings

from server_testing import token as signed_token

ROOT = Path(__file__).resolve().parents[3]
CATALOGUE = ROOT / "packages" / "python" / "tests" / "fixtures" / "catalogue"
SERVED = {"base", "jeff.safety.spam", "jeff.support.triage"}
LIMITS = {"base": 26, "jeff.safety.spam": 26, "jeff.support.triage": 3}
PNG = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 32).decode()


def position_only(adapter: str, row: dict[str, Any]) -> list[float]:
    """A stand-in model that leans on position alone: the first option gets the most, then less down the list."""
    question = row["question"]
    count = len(question["criteria"]) if question["type"] != "noul" else 2
    weights = [float(count - index) for index in range(count)]
    return [weight / sum(weights) for weight in weights]


def uniform(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    count = len(question["criteria"]) if question["type"] != "noul" else 2
    return [1 / count] * count


def fake(answers: Callable[[str, dict[str, Any]], list[float]] = position_only,
         installed: set[str] = SERVED) -> FakeEngine:
    return FakeEngine(answers, set(installed), max_options={key: LIMITS[key] for key in installed})


def settings(**options: Any) -> ServerSettings:
    options.setdefault("catalogue", CATALOGUE)
    options.setdefault("model_name", "jeff-v1.3")
    return ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), **options)


def client(engine: Any = None, **options: Any) -> TestClient:
    return TestClient(create_app(settings(**options), engine=engine if engine is not None else fake()))


def body(model: str = "jeff", **extra: Any) -> dict[str, Any]:
    return {"model": model, "state": "Book me a flight.",
            "questions": {"q": {"type": "choice", "instructions": "Which destination?",
                                "criteria": {"Paris": None, "Rome": "sunny"}}}, **extra}


def options_request(count: int, model: str = "jeff") -> dict[str, Any]:
    return {"model": model, "state": "Book me a flight.",
            "questions": {"q": {"type": "choice", "instructions": "Which destination?",
                                "criteria": {f"City {i}": None for i in range(count)}}}}


def assert_close(given: Any, expected: Any) -> None:
    """Equal, with numbers compared approximately at any depth (pytest.approx does not nest)."""
    if isinstance(expected, dict):
        assert isinstance(given, dict) and list(given) == list(expected), (given, expected)
        for key in expected:
            assert_close(given[key], expected[key])
    elif isinstance(expected, float):
        assert given == pytest.approx(expected)
    else:
        assert given == expected


def assert_error(response: Any, status: int, error_type: str, *named: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["x-request-id"]
    error = response.json()["error"]
    assert error["type"] == error_type, error
    for text in named:
        assert text in error["message"], error["message"]
    return dict(error)


# --- the legacy client's requests get jeff-server's response shape ------------------------------------------------

# Exactly what jeff-server's jeff.client builds for Client(model="jeff-latest", orders=2).ask(...) in its
# test_the_client_matches_the_real_server: options given as a list become keys o1, o2; yes_no_question has no
# criteria; score_question sends its levels; the state is an object whose changing field is last.
LEGACY_REQUEST = {
    "model": "jeff-latest", "orders": 2,
    "state": {"screen": "Inbox", "transcript": "open the last email"},
    "questions": {
        "route": {"type": "choice", "criteria": {"o1": "Open email", "o2": "Archive"},
                  "instructions": "What does the user want?"},
        "question": {"type": "noul", "instructions": "Is the user asking a question?"},
        "urgency": {"type": "score", "criteria": ["low", "medium", "high"]}}}


def read_like_the_legacy_client(request: dict[str, Any], response: dict[str, Any]) -> None:
    """What jeff-server's jeff.client.Answers reads from a response, for each question asked: a choice's
    probabilities under exactly the keys sent and a chosen key among them, a score's probabilities under "0".."n-1"
    and its score, a noul's number, and every confidence a number."""
    assert isinstance(response["answers"], dict)
    for key, question in request["questions"].items():
        given = response["answers"][key]
        assert given["type"] == question["type"]
        if question["type"] == "choice":
            assert set(given["probabilities"]) == set(question["criteria"]) and given["choice"] in question["criteria"]
            assert isinstance(given["confidence"], float)
        elif question["type"] == "score":
            assert set(given["probabilities"]) == {str(level) for level in range(len(question["criteria"]))}
            assert isinstance(given["score"], float) and isinstance(given["confidence"], float)
        else:
            assert isinstance(given["noul"], float)


def test_the_legacy_clients_request_gets_jeff_servers_response_shape() -> None:
    engine = fake(uniform)
    with client(engine) as http:
        response = http.post("/v1/systemone", json=LEGACY_REQUEST)
    assert response.status_code == 200, response.text
    result = response.json()
    contract.validate("systemone_response", result, "the /v1/systemone response")
    read_like_the_legacy_client(LEGACY_REQUEST, result)
    assert result["model"] == "jeff-v1.3"
    assert result["answers"]["route"]["probabilities"] == {"o1": 0.5, "o2": 0.5}
    assert result["answers"]["question"] == {"type": "noul", "noul": 0.5}
    assert result["answers"]["urgency"]["probabilities"] == pytest.approx({"0": 1 / 3, "1": 1 / 3, "2": 1 / 3})
    assert result["answers"]["urgency"]["legend"] == {"0": "low", "1": "medium", "2": "high"}
    [(adapter, rows)] = engine.calls
    assert adapter == "base" and len(rows) == 6  # three questions, each in both orders, in one engine call
    assert result["usage"] == {"input_tokens": sum(fake_tokens(row) for row in rows), "output_tokens": 0,
                               "orders": 2}
    assert result["timing"]["server_ms"] >= 0 and "model_ms" in result["timing"]


def test_every_answer_is_jeff_servers_formula_on_the_engines_probabilities() -> None:
    request = {"model": "jeff", "state": "s", "questions": {
        "c": {"type": "choice", "criteria": {"a": None, "b": "bee", "c": {"see": True}}},
        "s": {"type": "score", "criteria": ["low", "mid", "high", "top"]},
        "n": {"type": "noul", "criteria": {"true": "It rained", "false": None}}}}
    with client() as http:
        result = http.post("/v1/systemone", json=request).json()
    for key, question in request["questions"].items():
        expected = answer(question, position_only("base", {"question": question}))
        assert_close(result["answers"][key], expected)
    assert result["usage"]["orders"] == 1


def test_two_orders_send_the_reversed_questions_and_average_them_back() -> None:
    engine = fake()
    with client(engine) as http:
        once = http.post("/v1/systemone", json=body()).json()
        twice = http.post("/v1/systemone", json=body(orders=2)).json()
    question = body()["questions"]["q"]
    first, reversed_ = engine.calls[1][1]
    assert first["question"] == question and reversed_["question"] == reverse_question(question)
    assert once["answers"]["q"]["probabilities"] == pytest.approx({"Paris": 2 / 3, "Rome": 1 / 3})
    averaged = average_orders([question], [position_only("base", first)], [position_only("base", reversed_)])[0]
    assert_close(twice["answers"]["q"], answer(question, averaged))
    assert twice["answers"]["q"]["probabilities"] == pytest.approx({"Paris": 0.5, "Rome": 0.5})  # position cancelled
    assert twice["usage"]["input_tokens"] == fake_tokens(first) + fake_tokens(reversed_)
    assert twice["usage"]["orders"] == 2


def test_the_state_images_and_questions_reach_the_engine_as_sent() -> None:
    engine = fake()
    with client(engine) as http:
        for state in ("plain text", {"screen": "Inbox", "transcript": ""}, ["a", {"b": 1}]):
            request = body(state=state, images=[PNG])
            request["questions"]["q"]["instructions"] = None  # null is the same as leaving it out, as jeff-server
            assert http.post("/v1/systemone", json=request).status_code == 200
    for (_, [row]), state in zip(engine.calls, ("plain text", {"screen": "Inbox", "transcript": ""}, ["a", {"b": 1}]),
                                 strict=True):
        assert row == {"state": state, "images": [PNG],
                       "question": {"type": "choice", "criteria": {"Paris": None, "Rome": "sunny"}}}


# --- models ----------------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("model", ["jeff", "jeff-latest", "jeff-qwen3.8-27b", "jeff-v1.3"])
def test_the_base_answers_to_its_aliases_and_the_servers_model_name(model: str) -> None:
    engine = fake()
    with client(engine) as http:
        result = http.post("/v1/systemone", json=body(model)).json()
    assert engine.calls[0][0] == "base" and result["model"] == "jeff-v1.3"


@pytest.mark.parametrize("model", ["spam", "jeff.safety.spam"])
def test_an_adapter_answers_to_its_alias_and_its_id(model: str) -> None:
    engine = fake()
    with client(engine) as http:
        result = http.post("/v1/systemone", json=body(model)).json()
    assert engine.calls[0][0] == "jeff.safety.spam" and result["model"] == model


def model_detail(response: Any) -> None:
    """jeff-server's error detail for an unknown model, which its client raises as UnknownModel."""
    [problem] = response.json()["detail"]
    assert problem["loc"] == ["body", "model"] and problem["type"] == "value_error"
    assert problem["msg"] == response.json()["error"]["message"]


def test_an_unknown_model_is_422_naming_the_models_with_jeff_servers_detail() -> None:
    with client() as http:
        response = http.post("/v1/systemone", json=body("jeff-nowhere"))
    error = assert_error(response, 422, "InvalidRequest", "jeff-nowhere", "jeff-v1.3", "jeff-latest", "spam",
                         "triage")
    assert error["message"].startswith("Unknown model")
    model_detail(response)


def test_an_unknown_model_is_answered_without_asking_the_engines() -> None:
    """Engines that disagree on what they serve make every installed() call an EngineError; an unknown model is
    still the caller's 422, not a 500."""
    class Disagreeing(FakeEngine):
        def installed(self, refresh: bool = False) -> set[str]:
            raise EngineError("The engines disagree on what is installed")

    with TestClient(create_app(settings(), engine=fake())) as http:
        http.app.state.planner.jeff.engine = Disagreeing(uniform, SERVED)  # after startup's router check
        assert_error(http.post("/v1/systemone", json=body("jeff-nowhere")), 422, "InvalidRequest", "Unknown model")


def test_a_catalogue_adapter_the_engine_does_not_serve_is_an_unknown_model_as_in_jeff_server() -> None:
    engine = fake(installed={"base", "jeff.safety.spam"})
    with client(engine) as http:
        response = http.post("/v1/systemone", json=body("triage"))
    assert_error(response, 422, "InvalidRequest", "triage", "not served", "PUT /v1/adapters/served")
    model_detail(response)
    assert engine.calls == []


@contextmanager
def routed(installed: set[str], bases: list[tuple[str, str]] | None = None, engine: FakeEngine | None = None,
           **options: Any) -> Iterator[TestClient]:
    """An API app over engine processes (jeff_runtime.testing.Running), one per base in `bases` (default: one engine
    on jeff-base v1.3), each serving `installed` (or the one `engine` given), through SocketEngine and the
    EngineRouter as in production: GET /v1/models reports the base the engine processes serve."""
    servers = [Running(engine if engine is not None else fake(installed=installed),
                       fixed={adapter: Path("/weights") / adapter for adapter in installed - {"base"}}, base=base)
               for base in (bases if bases is not None else [("jeff-base", "v1.3")])]
    sockets = [SocketEngine(server.path, timeout_s=5) for server in servers]
    try:
        with client(EngineRouter(sockets), **options) as http:
            yield http
    finally:
        for engine in sockets:
            engine.close()
        for server in servers:
            server.close()


def test_models_lists_the_bases_names_as_jeff_server_then_each_served_adapter_by_its_alias() -> None:
    with routed({"base", "jeff.support.triage"}) as http:
        result = http.get("/v1/models").json()
    contract.validate("models_response", result, "the /v1/models response")
    base, adapter = "Local Jeff text and image decisions.", "A LoRA adapter on the base model."
    assert result == {"models": [
        {"name": "jeff", "description": base, "release_date": "2026-10-05"},
        {"name": "jeff-latest", "description": base, "release_date": "2026-10-05"},
        {"name": "jeff-v1.3", "description": base, "release_date": "2026-10-05"},
        {"name": "triage", "description": adapter, "release_date": "2026-10-05"}]}


def test_models_with_a_catalogue_of_no_adapters_lists_the_base(tmp_path: Path) -> None:
    catalogue = tmp_path / "catalogue"
    catalogue.mkdir()
    shutil.copy(CATALOGUE / "bases.yaml", catalogue / "bases.yaml")
    with routed({"base"}, catalogue=catalogue) as http:
        result = http.get("/v1/models")
    assert result.status_code == 200, result.text
    assert [model["name"] for model in result.json()["models"]] == ["jeff", "jeff-latest", "jeff-v1.3"]


def two_base_catalogue(tmp_path: Path) -> tuple[Path, Path]:
    """The fixture catalogue (v1.3 adapters) with a bases.yaml naming v1.3 and v1.4, and an adapter folder holding
    a v1.4 adapter: as on the live v1.4 server, whose shipped catalogue still has v1.3 entries."""
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    (catalogue / "bases.yaml").write_text(
        "jeff-base:\n  v1.3: { repo: mstrasser/jeff-base, revision: v1.3, released: \"2026-10-05\" }\n"
        "  v1.4: { repo: mstrasser/jeff-base, revision: v1.4, released: \"2026-10-24\" }\n")
    adapters = tmp_path / "adapters"
    fake_adapter_folder(adapters / "newer", "jeff.newer.adapter", base_version="v1.4")
    return catalogue, adapters


@pytest.mark.parametrize(("version", "released"), [("v1.3", "2026-10-05"), ("v1.4", "2026-10-24")])
def test_models_reports_the_base_the_engines_serve_when_the_catalogue_names_two(tmp_path: Path, version: str,
                                                                               released: str) -> None:
    catalogue, adapters = two_base_catalogue(tmp_path)
    with routed({"base"}, bases=[("jeff-base", version)], catalogue=catalogue, adapter_dirs=(adapters,)) as http:
        response = http.get("/v1/models")
    assert response.status_code == 200, response.text
    assert {model["release_date"] for model in response.json()["models"]} == {released}


def test_models_is_a_500_when_the_engines_serve_different_bases(tmp_path: Path) -> None:
    catalogue, adapters = two_base_catalogue(tmp_path)
    with routed({"base"}, bases=[("jeff-base", "v1.3"), ("jeff-base", "v1.4")], catalogue=catalogue,
                adapter_dirs=(adapters,)) as http:
        assert_error(http.get("/v1/models"), 500, "EngineError", "different bases", "jeff-base@v1.3",
                     "jeff-base@v1.4")


def test_models_needs_the_engine_processes_whose_base_it_reports() -> None:
    with client() as http:  # an in-process FakeEngine: no engine process, so no served base
        assert_error(http.get("/v1/models"), 500, "EngineError", "EngineRouter", "GET /v1/models")


def test_the_model_name_cannot_be_an_adapters_name() -> None:
    for name in ("spam", "jeff.safety.spam"):
        app = create_app(settings(model_name=name), engine=fake())
        with pytest.raises(InvalidRequest, match=f"JEFF_MODEL_NAME={name}.*jeff.safety.spam"):
            with TestClient(app):
                pass


def test_models_needs_the_served_base_to_have_a_release_date(tmp_path: Path) -> None:
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    (catalogue / "bases.yaml").write_text("jeff-base:\n  v1.3: { repo: mstrasser/jeff-base, revision: v1.3 }\n")
    with routed({"base"}, catalogue=catalogue) as http:
        assert_error(http.get("/v1/models"), 500, "ManifestError", "released", "jeff-base", "v1.3")


# --- request shapes --------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("orders", [0, 3, "2", 2.0, True, None, [2]])
def test_orders_must_be_one_or_two(orders: object) -> None:
    with client() as http:
        assert_error(http.post("/v1/systemone", json=body(orders=orders)), 422, "InvalidRequest", "orders")


@pytest.mark.parametrize(("change", "named"), [
    (lambda b: b.update(extra=1), "extra"),
    (lambda b: b.pop("state"), "state"),
    (lambda b: b.update(state=5), "/state"),
    (lambda b: b.update(questions={}), "/questions"),
    (lambda b: b["questions"]["q"].pop("criteria"), "criteria"),
    (lambda b: b["questions"].update(q={"type": "score", "criteria": ["only"]}), "/questions/q/criteria"),
    (lambda b: b["questions"].update(q={"type": "noul", "criteria": {"maybe": "x"}}), "/questions/q/criteria"),
    (lambda b: b["questions"].update(q={"type": "rank", "criteria": ["a", "b"]}), "/questions/q/type"),
    (lambda b: b.update(images=[PNG] * 5), "/images"),
    (lambda b: b.update(images=["data:image/gif;base64,R0lGOD"]), "/images/0"),
    (lambda b: b.update(images=["data:image/png;base64,not base64!"]), "image 1"),
    (lambda b: b.update(model=""), "/model"),
])
def test_a_body_of_the_wrong_shape_is_422_invalid_request_naming_the_problem(change: Callable[[dict], Any],
                                                                             named: str) -> None:
    request = body()
    change(request)
    with client() as http:
        response = http.post("/v1/systemone", json=request)
    assert_error(response, 422, "InvalidRequest", named)
    if named == "/model":
        model_detail(response)  # jeff-server's client: UnknownModel
    else:
        assert response.json()["detail"] == response.json()["error"]["message"]  # its client: InvalidRequest


def test_a_body_that_is_not_json_is_422_invalid_request() -> None:
    with client() as http:
        response = http.post("/v1/systemone", content=b"{nope", headers={"content-type": "application/json"})
        assert_error(response, 422, "InvalidRequest", "not valid JSON")


def test_an_image_over_8_mb_is_refused() -> None:
    big = "data:image/png;base64," + base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 8_000_000).decode()
    with client() as http:
        assert_error(http.post("/v1/systemone", json=body(images=[big])), 422, "InvalidRequest", "8 MB")


# --- limits, busy and failures ---------------------------------------------------------------------------------------

def test_questions_up_to_the_trained_limit_are_answered_and_more_are_refused_clearly() -> None:
    with client() as http:
        answered = http.post("/v1/systemone", json=options_request(26))
        assert answered.status_code == 200 and len(answered.json()["answers"]["q"]["probabilities"]) == 26
        refused = http.post("/v1/systemone", json=options_request(27))
        assert_error(refused, 422, "TooManyLabels", "Question 'q' has 27 options, but this model handles at most 26",
                     "jeff-v1.3", "Shortlist the options first, or split the question")
        assert refused.json()["detail"] == refused.json()["error"]["message"]  # jeff-server's string detail
        assert_error(http.post("/v1/systemone", json=options_request(4, "triage")), 422, "TooManyLabels",
                     "Question 'q' has 4 options, but this model handles at most 3", "triage")


def test_more_options_than_any_model_has_are_too_many_options_as_in_jeff_server() -> None:
    """jeff-server's schema allowed at most 255 choice options and 10 score levels; its client raised more as
    TooManyOptions."""
    score = {"model": "jeff", "state": "s", "questions": {"level": {"type": "score", "criteria": ["x"] * 11}}}
    with client() as http:
        for request, count, limit in ((options_request(256), 256, 255), (score, 11, 10)):
            refused = http.post("/v1/systemone", json=request)
            assert_error(refused, 422, "TooManyLabels", f"has {count} options, but this model handles at most {limit}")
            assert refused.json()["detail"] == refused.json()["error"]["message"]


@pytest.mark.parametrize(("retry_after_s", "header"), [(0.2, "1"), (1.0, "1"), (2.5, "3")])
def test_a_busy_engine_is_529_at_once_with_retry_after(retry_after_s: float, header: str) -> None:
    class Full(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            raise Busy("The engine queue is full (4096 of 4096 rows); retry shortly", retry_after_s)

    with client(Full(uniform, SERVED)) as http:
        started = time.monotonic()
        response = http.post("/v1/systemone", json=body())
    assert_error(response, 529, "Busy", "queue is full")
    assert response.json()["detail"] == response.json()["error"]["message"]
    assert response.headers["Retry-After"] == header and time.monotonic() - started < 0.5


def test_an_engine_that_does_not_count_input_tokens_is_an_engine_error() -> None:
    class Untokenised(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            return EngineResult(super().predict(adapter_id, rows).probabilities, {"model_ms": 0.0})

    with client(Untokenised(uniform, SERVED)) as http:
        assert_error(http.post("/v1/systemone", json=body()), 500, "EngineError", "input token")


class Deployment:
    """Real engine servers (jeff_runtime.testing.Running over FakeEngines) reached through SocketEngine and the
    EngineRouter, as in production: the engine checks each request (options, token limit) before queueing it."""

    def __init__(self, count: int) -> None:
        self.servers = [Running(fake(uniform), fixed={key: Path("/weights") / key for key in SERVED if key != "base"})
                        for _ in range(count)]
        self.engines = [SocketEngine(server.path, timeout_s=5) for server in self.servers]

    def client(self) -> TestClient:
        return client(EngineRouter(self.engines))

    def close(self) -> None:
        for engine in self.engines:
            engine.close()
        for server in self.servers:
            server.close()


@pytest.fixture
def deployment() -> Iterator[Deployment]:
    running = Deployment(2)
    yield running
    running.close()


def test_through_engine_processes_the_engine_checks_options_and_length_and_counts_tokens(
        deployment: Deployment) -> None:
    with deployment.client() as http:
        answered = http.post("/v1/systemone", json=LEGACY_REQUEST)
        assert answered.status_code == 200, answered.text
        read_like_the_legacy_client(LEGACY_REQUEST, answered.json())
        assert answered.json()["usage"]["input_tokens"] > 0
        assert_error(http.post("/v1/systemone", json=options_request(27)), 422, "TooManyLabels",
                     "Question 'q' has 27 options, but this model handles at most 26", "jeff-v1.3")
        too_long = body(state={"__too_long__": 1, "text": "long"})  # FakeEngine's stand-in for an over-long prompt
        assert_error(http.post("/v1/systemone", json=too_long), 422, "InputTooLong", "no input was truncated")
        assert [model["name"] for model in http.get("/v1/models").json()["models"]] == [
            "jeff", "jeff-latest", "jeff-v1.3", "spam", "triage"]


# --- credentials and licences ----------------------------------------------------------------------------------------

def token(private: Ed25519PrivateKey, licences: list[str]) -> dict[str, str]:
    return {"Authorization": f"Bearer {signed_token(private, licences=licences)}"}


def test_a_proprietary_adapter_needs_the_callers_licence_before_the_engine_runs(
        keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    private, public = keys
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    path = catalogue / "jeff.support.triage" / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["licence"] = {"kind": "proprietary", "adapter": "Commercial"}
    manifest["weights"] = {"proprietary": {"repo": "jeffhub/private", "path": "jeff.support.triage"}}
    path.write_text(yaml.safe_dump(manifest))
    engine = fake()
    with routed(SERVED, engine=engine, catalogue=catalogue, hub_public_key=public, api_key="the-key") as http:
        unsigned = http.post("/v1/systemone", json=body("triage"))
        assert_error(unsigned, 401, "Unauthorised")
        assert unsigned.json()["detail"] == unsigned.json()["error"]["message"]
        assert http.get("/v1/models").json()["detail"]  # /v1/models carries jeff-server's detail too
        refused = http.post("/v1/systemone", json=body("triage"), headers=token(private, []))
        assert_error(refused, 403, "NotLicensed", "jeff.support.triage", "alice")
        assert engine.calls == []  # refused before the engine saw a row
        assert http.post("/v1/systemone", json=body("triage"),
                         headers=token(private, ["jeff.support.triage"])).status_code == 200
        assert http.post("/v1/systemone", json=body("spam"), headers=token(private, [])).status_code == 200
        assert http.post("/v1/systemone", json=body("jeff"), headers=token(private, [])).status_code == 200
        owner = {"Authorization": "Bearer the-key"}
        assert http.post("/v1/systemone", json=body("triage"), headers=owner).status_code == 200
        assert http.get("/v1/models", headers=token(private, [])).status_code == 200


# --- the real model (slow: loads the 1.7 GB base; run alone) ----------------------------------------------------------

@pytest.mark.slow
def test_the_legacy_request_on_the_real_base_and_an_adapter() -> None:
    from huggingface_hub import snapshot_download

    from jeff_runtime import LocalEngine, select_backend

    base = Path(snapshot_download("mstrasser/jeff-base", revision="v1.3"))
    triage = Path(snapshot_download("mstrasser/jeff-adapter-triage", revision="v1.3"))
    # the fast backend (auto on an NVIDIA GPU) needs room for its adapters and prefixes; the others refuse them
    sized = {"max_adapters": 1, "prefix_pool_mb": 2048} if select_backend("auto") == "fast" else {}
    engine = LocalEngine(base, {"jeff.support.triage": triage}, backend="auto", prepare_workers=1, **sized)
    with client(engine, catalogue=None) as http:
        for model in ("jeff-latest", "triage"):
            response = http.post("/v1/systemone", json=LEGACY_REQUEST | {"model": model})
            assert response.status_code == 200, response.text
            result = response.json()
            contract.validate("systemone_response", result, "the /v1/systemone response")
            read_like_the_legacy_client(LEGACY_REQUEST, result)
            assert result["model"] == ("jeff-v1.3" if model == "jeff-latest" else "triage")
            assert result["usage"]["input_tokens"] > 50 and result["usage"]["orders"] == 2
        example = json.loads((triage / "example.json").read_text())
        routed = http.post("/v1/systemone", json={"model": "triage", "state": example["state"],
                                                  "questions": {"route": example["questions"]["route"]}}).json()
        assert routed["answers"]["route"]["choice"] == list(example["questions"]["route"]["criteria"])[1]
        assert_error(http.post("/v1/systemone", json=options_request(255)), 422, "TooManyLabels", "at most 254")
        assert [model["name"] for model in http.get("/v1/models").json()["models"]] == [
            "jeff", "jeff-latest", "jeff-v1.3", "triage"]


# --- jeff-server's own client against the new server -----------------------------------------------------------------

def legacy_client() -> Any:
    """jeff-server's client module (a verbatim copy in tests/legacy), imported under its own name."""
    spec = importlib.util.spec_from_file_location("jeff_server_client",
                                                  Path(__file__).parent / "legacy" / "jeff_server_client.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["jeff_server_client"] = module  # its dataclasses look their module up
    spec.loader.exec_module(module)
    return module


@contextmanager
def real_server(max_options: int = 3, api_key: str = "secret") -> Iterator[tuple[str, dict[str, bool]]]:
    """A real app over a real engine process (as /health needs), reachable over HTTP: the base serves `max_options`
    choice options and nothing else; setting the yielded `busy["on"]` to True makes every further request answer
    529 Busy, as a full engine queue would."""
    busy = {"on": False}

    class Model(FakeEngine):
        def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
            if busy["on"]:
                raise Busy("The engine queue is full (4096 of 4096 rows); retry shortly", 0.4)
            return super().predict(adapter_id, rows)

    server = Running(Model(uniform, {"base"}, max_options={"base": max_options}), fixed={})
    socket_engine = SocketEngine(server.path, timeout_s=5)
    app = create_app(settings(api_key=api_key), engine=EngineRouter([socket_engine]))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    running = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    try:
        for _ in range(250):
            if running.started:
                break
            time.sleep(0.02)
        assert running.started
        yield f"http://127.0.0.1:{port}", busy
    finally:
        running.should_exit = True
        thread.join(timeout=5)
        socket_engine.close()
        server.close()


def assert_matches_jeff_server(module: Any, url: str, busy: dict[str, bool]) -> None:
    """Every check that a client module written to jeff-server's API must pass against the new server: the same
    successful answers, the same model list and health, and the same exception type and status for each error --
    checked here for jeff-server's own client and for jeff.systemone, the same port of it into the jeff package, so
    the two are seen to agree rather than merely each being asserted against separately."""
    jeff = module.Client(url, model="jeff-latest", api_key="secret", orders=2)
    answers = jeff.ask({"screen": "Inbox", "transcript": "open the last email"}, {
        "route": module.choice_question(["Open email", "Archive"], "What does the user want?"),
        "question": module.yes_no_question("Is the user asking a question?"),
        "urgency": module.score_question(["low", "medium", "high"])})
    assert answers.choice("route").probabilities == {"o1": 0.5, "o2": 0.5}
    assert answers.yes_no("question") == 0.5
    assert answers.score("urgency").probabilities == pytest.approx([1 / 3] * 3)
    assert answers.response["usage"]["orders"] == 2
    assert jeff.health()["max_options"] == 3
    assert "jeff-latest" in [model["name"] for model in jeff.models()]
    with pytest.raises(module.TooManyOptions, match="at most 3"):
        jeff.choose("s", ["a", "b", "c", "d"])
    with pytest.raises(module.UnknownModel):
        jeff.choose("s", ["a", "b"], model="jeff-nowhere")
    with pytest.raises(module.UnknownModel):
        jeff.choose("s", ["a", "b"], model="spam")  # in the catalogue, not served
    with pytest.raises(module.TooManyOptions):
        jeff.decide({"state": "s", "questions": {"q": {"type": "score", "criteria": ["x"] * 11}}})
    with pytest.raises(module.InvalidRequest):
        jeff.decide({"state": "s", "questions": {}})
    with pytest.raises(module.Unauthorised):
        module.Client(url, model="jeff").choose("s", ["a", "b"])
    busy["on"] = True
    try:
        with pytest.raises(module.Busy) as raised:
            jeff.choose("s", ["a", "b"])
        assert raised.value.retry_after == 1.0
    finally:
        busy["on"] = False


def test_jeff_servers_client_matches_the_new_server() -> None:
    """jeff-server's tests/test_client.py test_the_client_matches_the_real_server, run against the new app: the same
    client, the same calls, the same exception types, through a real engine process (as /health needs)."""
    with real_server() as (url, busy):
        assert_matches_jeff_server(legacy_client(), url, busy)


def test_the_ported_systemone_client_matches_jeff_servers_legacy_client() -> None:
    """jeff.systemone -- jeff-server's client, ported unchanged into the jeff package -- passes every one of those
    checks against the same server, the same way jeff-server's own client does."""
    with real_server() as (url, busy):
        assert_matches_jeff_server(systemone, url, busy)
