"""/v1/channel: the engine socket's frames over a WebSocket. Hello both ways, auth once (required when the server has
credentials, within AUTH_TIMEOUT_S), refresh for the same caller, bound-function handles whose calls answer exactly
what /v1/decide answers, decide/dispatch/graph frames equal to their HTTP answers, the per-connection limits
(handles, outstanding requests), the caller's rate limit counted per frame, licences and served adapters checked on
every call, a token expiring with calls in flight, and a disconnect freeing everything."""

import asyncio
import gc
import json
import os
import re
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import anyio
import jwt
import msgpack
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from jeff import contract
from jeff.decision import Decision
from jeff.engine import EngineResult
from jeff.errors import EngineError
from jeff.graph import GraphResult
from jeff.protocol import HEADER, PROTOCOL_VERSION, decode, encode
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine
from jeff_runtime.testing import Running

from jeff_server import channel as channel_module
from jeff_server.app import create_app
from jeff_server.router import EngineRouter
from jeff_server.settings import ServerSettings
from jeff_server.users import hash_password, read_file, write_file

from channel_testing import Channel, assert_error, start
from server_testing import proprietary, token

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "python" / "tests" / "fixtures"
CATALOGUE = FIXTURES / "catalogue"
ROUTERS = FIXTURES / "routers"
GRAPH = FIXTURES / "graphs" / "support_intake.yaml"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
TEAMS = {"deliveries": "Deliveries", "refunds": "Refunds"}
PARAMS: dict[str, Any] = {"company": "A furniture shop.", "channel": "email", "teams": TEAMS}
FIXTURE_ADAPTERS = {"jeff.safety.spam", "jeff.support.triage"}
ROUTED = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
UNSURE = [0.0, 0.5, 0.5, 0.0, 0.0]  # below triage.urgency's calibration threshold 0.6
URGENCY = {"function": "triage.urgency", "context": CONTEXT}
API_KEY = "the-key"


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return UNSURE if "unsure" in json.dumps(row["state"]) else [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def fake(installed: set[str] = FIXTURE_ADAPTERS) -> FakeEngine:
    return FakeEngine(answers, set(installed))


class GatedEngine(FakeEngine):
    """A fake engine whose predict waits until `gate` is set, counting the calls that have started waiting."""

    def __init__(self, installed: set[str] = FIXTURE_ADAPTERS) -> None:
        super().__init__(answers, set(installed))
        self.gate = threading.Event()
        self.waiting = 0
        self._lock = threading.Lock()

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        with self._lock:
            self.waiting += 1
        if not self.gate.wait(20):
            raise EngineError("GatedEngine: the test never opened the gate")
        return super().predict(adapter_id, rows)


def settings(catalogue: Path = CATALOGUE, **options: Any) -> ServerSettings:
    return ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3", catalogue=catalogue,
                          **options)


def client(engine: FakeEngine | EngineRouter, catalogue: Path = CATALOGUE, **options: Any) -> TestClient:
    return TestClient(create_app(settings(catalogue, **options), engine=engine))


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


def assert_closed_with(channel: Channel, error_type: str, *named: str) -> None:
    final = channel.receive()
    assert final["id"] == 0
    assert_error(final, error_type, *named)
    channel.closed()


def decide_http(http: TestClient, body: dict[str, Any], token: str | None = None) -> dict[str, Any]:
    headers = {"authorization": f"Bearer {token}"} if token is not None else {}
    response = http.post("/v1/decide", json=body, headers=headers)
    return {"status": response.status_code, **response.json()}


def code_free_graph(tmp_path: Path) -> Path:
    raw = yaml.safe_load(GRAPH.read_text())
    del raw["nodes"]["ticket"]
    raw["outputs"] = ["kind", "team", "urgency"]
    folder = tmp_path / "graphs"
    folder.mkdir()
    (folder / "support_intake.yaml").write_text(yaml.safe_dump(raw))
    return folder


def channel_tasks(http: TestClient) -> list[str]:
    async def names() -> list[str]:
        return [task.get_name() for task in asyncio.all_tasks()
                if task.get_name().startswith(channel_module.TASK_NAME)]
    return http.portal.call(names)  # type: ignore[union-attr, no-any-return]


# --- opening, framing ------------------------------------------------------------------------------------------------

def test_hello_with_another_version_is_refused_and_closes() -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = Channel(ws)
        channel.send({"op": "hello", "version": 99})
        assert_closed_with(channel, "ProtocolMismatch", "99")


def test_a_first_message_that_is_not_a_hello_is_refused_and_closes() -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = Channel(ws)
        channel.send({"op": "auth", "id": 1, "token": "x"})
        assert_closed_with(channel, "ProtocolMismatch", "hello")


NO_ID = msgpack.packb({"op": "call", "handle": 1, "input": "x"})


def packed(message: object) -> bytes:
    body = msgpack.packb(message, use_bin_type=True)
    return HEADER.pack(len(body)) + body


NOT_JSON = [  # values JSON cannot hold: the frame cannot be read, so it has no id to be answered by
    {"op": "bind", "id": 8, "function": "triage.urgency", "context": {"company": b"bytes"}},
    {"op": "call", "id": 8, "handle": 1, "input": b""},
    {"op": "call", "id": 8, "handle": 1, "input": {b"key": 1}},
    {"op": "call", "id": 8, "handle": 1, "input": msgpack.ExtType(3, b"x")},
    {"op": "call", "id": 8, "handle": 1, "input": msgpack.Timestamp(1)},
    {"op": "bind", "id": 1, "function": "triage.urgency", "context": CONTEXT, "on_low_confidence": {"escalate": [b"x"]}},
    {"op": "decide", "id": 1, "calls": [{"function": "triage.urgency", "input": "x", "context": CONTEXT}],
     "on_low_confidence": {"escalate": [b"x"]}},
]


@pytest.mark.parametrize(("send", "named"), [
    (lambda ws: ws.send_text("hello"), "binary"),
    (lambda ws: ws.send_bytes(b"\x00\x00"), "header"),
    (lambda ws: ws.send_bytes(HEADER.pack(10) + b"\x80"), "exactly one frame"),
    (lambda ws: ws.send_bytes(HEADER.pack(50 * 1024 * 1024 + 1)), "50 MiB"),
    (lambda ws: ws.send_bytes(HEADER.pack(1) + b"\xc1"), "msgpack"),
    (lambda ws: ws.send_bytes(HEADER.pack(len(NO_ID)) + NO_ID), "id"),
    (lambda ws: ws.send_bytes(encode({"op": "hello", "version": PROTOCOL_VERSION})), "hello"),
    *[((lambda ws, message=message: ws.send_bytes(packed(message))), "JSON") for message in NOT_JSON],
])
def test_a_broken_message_after_hello_fails_the_connection_with_id_0(send: Callable[[Any], None],
                                                                      named: str) -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        send(ws)
        assert_closed_with(channel, "InvalidRequest", named)


def test_a_message_with_an_id_that_does_not_match_its_shape_fails_only_that_request() -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.send_raw(msgpack.packb({"op": "call", "id": 7, "handle": 1}))
        error = assert_error(channel.receive(), "InvalidRequest", "'call'", "'input' is a required property")
        assert "\n" not in error["message"]
        for id_, op in ((10, [1]), (11, {"a": 1}), (12, None), (13, 5)):  # an op that is not even a string
            channel.send_raw(msgpack.packb({"op": op, "id": id_}))
            assert_error(channel.receive(), "InvalidRequest", "op must be one of")
        assert channel.result({"op": "bind", "id": 9, **URGENCY})["handle"] == 1  # still open


@pytest.mark.parametrize("message", [
    {"op": "predict", "id": 2, "adapter": "jeff.safety.spam", "rows": [{"state": {}, "question": {}}]},
    {"op": "installed", "id": 2}, {"op": "health", "id": 2}, {"op": "stats", "id": 2}, {"op": "reload", "id": 2},
    {"op": "serve", "id": 2, "adapters": []},
])
def test_engine_socket_operations_are_refused(message: dict[str, Any]) -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.error(message, "InvalidRequest", repr(message["op"]), "engine socket")


# --- auth ------------------------------------------------------------------------------------------------------------

def test_auth_is_optional_on_a_server_without_credentials() -> None:
    with client(fake()) as http:
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws)
            assert channel.result({"op": "bind", "id": 1, **URGENCY})["handle"] == 1
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws, token="anything")
            assert channel.result({"op": "bind", "id": 2, **URGENCY})["handle"] == 1


@pytest.mark.parametrize("public", [False, True])
def test_auth_is_required_when_the_server_has_credentials(public: bool) -> None:
    options: dict[str, Any] = {"api_key": API_KEY, "public": public}
    if public:
        options["rate_limit"] = (1000.0, 100)
    with client(fake(), **options) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.error({"op": "bind", "id": 1, **URGENCY}, "Unauthorised", "auth")
        channel.error({"op": "refresh", "id": 2, "token": API_KEY}, "Unauthorised", "auth")
        channel.error({"op": "auth", "id": 3, "token": "wrong"}, "Unauthorised")
        assert channel.result({"op": "auth", "id": 4, "token": API_KEY}) == {}
        channel.error({"op": "auth", "id": 5, "token": API_KEY}, "InvalidRequest", "refresh")
        assert channel.result({"op": "bind", "id": 6, **URGENCY})["handle"] == 1


def test_no_auth_within_the_timeout_closes_the_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channel_module, "AUTH_TIMEOUT_S", 0.3)
    with client(fake(), api_key=API_KEY) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.error({"op": "auth", "id": 1, "token": "wrong"}, "Unauthorised")
        assert_closed_with(channel, "Unauthorised", "0.3 seconds")


def test_no_hello_within_the_timeout_closes_the_channel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channel_module, "AUTH_TIMEOUT_S", 0.3)
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        assert_closed_with(Channel(ws), "InvalidRequest", "hello", "0.3 seconds")


def test_an_authenticated_channel_is_not_closed_by_the_auth_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(channel_module, "AUTH_TIMEOUT_S", 0.2)
    with client(fake(), api_key=API_KEY) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, token=API_KEY)
        time.sleep(0.4)
        assert channel.result({"op": "bind", "id": 2, **URGENCY})["handle"] == 1


# --- bind and call -----------------------------------------------------------------------------------------------------

def test_bind_then_call_equals_v1_decide_for_the_same_call() -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        bound = channel.result({"op": "bind", "id": 1, "function": "triage.route", "context": CONTEXT,
                                "labels": TEAMS})
        assert bound["handle"] == 1 and bound["function"] == "jeff.support.triage.route"
        functions = http.get("/v1/functions").json()["functions"]
        assert bound["version"] == next(item["adapter_version"] for item in functions
                                        if item["id"] == "jeff.support.triage.route")
        answer = channel.result({"op": "call", "id": 2, "handle": 1, "input": "My wardrobe arrived broken"})
        http_answer = decide_http(http, {"calls": [{"function": "triage.route", "input": "My wardrobe arrived broken",
                                                    "context": CONTEXT, "labels": TEAMS}]})
    assert http_answer["status"] == 200
    assert Decision.from_json(answer["decision"]) == Decision.from_json(http_answer["decisions"][0])


def test_call_many_equals_v1_decide_with_those_calls_in_order() -> None:
    inputs = ["It is on fire", "unsure", "Call me back"]
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.result({"op": "bind", "id": 1, **URGENCY})
        answer = channel.result({"op": "call_many", "id": 2, "handle": 1, "inputs": inputs})
        http_answer = decide_http(http, {"calls": [{**URGENCY, "input": item} for item in inputs]})
    assert [Decision.from_json(item) for item in answer["decisions"]] == \
        [Decision.from_json(item) for item in http_answer["decisions"]]
    assert answer["decisions"][1]["abstained"] is True


def test_a_bound_error_policy_answers_low_confidence_carrying_the_decision_as_v1_decide_does() -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.result({"op": "bind", "id": 1, **URGENCY, "on_low_confidence": "error"})
        error = channel.error({"op": "call", "id": 2, "handle": 1, "input": "unsure"}, "LowConfidence")
        http_answer = decide_http(http, {"calls": [{**URGENCY, "input": "unsure"}], "on_low_confidence": "error"})
    assert http_answer["status"] == 409
    assert error == http_answer["error"]


def test_a_bound_escalation_policy_runs_on_the_server() -> None:
    policy = {"escalate": ["jeff.support.triage.urgency"]}
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.result({"op": "bind", "id": 1, **URGENCY, "on_low_confidence": policy})
        answer = channel.result({"op": "call", "id": 2, "handle": 1, "input": "unsure"})
        http_answer = decide_http(http, {"calls": [{**URGENCY, "input": "unsure"}], "on_low_confidence": policy})
    assert Decision.from_json(answer["decision"]) == Decision.from_json(http_answer["decisions"][0])


@pytest.mark.parametrize(("bind", "error_type"), [
    ({"function": "jeff.nope.adapter.fn"}, "UnknownFunction"),
    ({"function": "triage.urgency"}, "MissingContext"),
    ({"function": "triage.urgency", "context": {**CONTEXT, "extra": 1}}, "UnknownContext"),
    ({"function": "triage.route", "context": CONTEXT}, "MissingLabels"),
    ({"function": "triage.urgency", "context": CONTEXT, "labels": TEAMS}, "InvalidLabels"),
    ({"function": "triage.urgency", "context": CONTEXT, "on_low_confidence": {"escalate": ["spam.message_type"]}},
     "InterfaceMismatch"),
    ({"function": "triage.urgency", "context": CONTEXT, "on_low_confidence": {"escalate": ["llm"]}},
     "InvalidRequest"),
])
def test_bind_checks_everything_with_the_errors_v1_decide_gives(bind: dict[str, Any], error_type: str) -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        error = channel.error({"op": "bind", "id": 1, **bind}, error_type)
        call = {key: value for key, value in bind.items() if key != "on_low_confidence"}
        body: dict[str, Any] = {"calls": [{**call, "input": "x"}]}
        if "on_low_confidence" in bind:
            body["on_low_confidence"] = bind["on_low_confidence"]
        http_answer = decide_http(http, body)
    assert http_answer["error"] == error


def test_bind_refuses_a_function_whose_adapter_is_not_served() -> None:
    with client(fake({"jeff.safety.spam"})) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        error = channel.error({"op": "bind", "id": 1, **URGENCY}, "NotInstalled")
        assert error == decide_http(http, {"calls": [{**URGENCY, "input": "x"}]})["error"]


def test_release_ends_a_handle_and_unknown_handles_are_named() -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.result({"op": "bind", "id": 1, **URGENCY})
        channel.result({"op": "bind", "id": 2, **URGENCY})
        assert channel.result({"op": "release", "id": 3, "handle": 1}) == {}
        channel.error({"op": "call", "id": 4, "handle": 1, "input": "x"}, "InvalidRequest", "handle 1")
        channel.error({"op": "call_many", "id": 5, "handle": 9, "inputs": ["x"]}, "InvalidRequest", "handle 9")
        channel.error({"op": "release", "id": 6, "handle": 1}, "InvalidRequest", "handle 1")
        channel.result({"op": "call", "id": 7, "handle": 2, "input": "x"})
        assert channel.result({"op": "bind", "id": 8, **URGENCY})["handle"] == 3  # numbers are never reused


# --- decide, dispatch, graph frames ------------------------------------------------------------------------------------

def test_decide_frames_equal_their_http_answers() -> None:
    calls = [{**URGENCY, "input": "It is on fire"}, {"function": "spam.message_type", "input": "WIN NOW",
                                                     "context": {"channel": "email"}}]
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        answer = channel.result({"op": "decide", "id": 1, "calls": calls})
        error = channel.error({"op": "decide", "id": 2, "calls": [{**URGENCY, "input": "unsure"}],
                               "on_low_confidence": "error"}, "LowConfidence")
        http_answer = decide_http(http, {"calls": calls})
        http_error = decide_http(http, {"calls": [{**URGENCY, "input": "unsure"}], "on_low_confidence": "error"})
    assert [Decision.from_json(item) for item in answer["decisions"]] == \
        [Decision.from_json(item) for item in http_answer["decisions"]]
    assert error == http_error["error"]


def test_dispatch_frames_equal_their_http_answers() -> None:
    dispatch = {"input": "My wardrobe is broken", "interface": "rate", "context": CONTEXT}
    with client(fake(ROUTED), ROUTERS) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        answer = channel.result({"op": "dispatch", "id": 1, "dispatch": dispatch})
        error = channel.error({"op": "dispatch", "id": 2, "dispatch": {**dispatch, "interface": "nope"}},
                              "InvalidRequest", "nope")
        http_answer = decide_http(http, {"dispatch": dispatch})
        http_error = decide_http(http, {"dispatch": {**dispatch, "interface": "nope"}})
    assert Decision.from_json(answer["decision"]) == Decision.from_json(http_answer["decision"])
    assert error == http_error["error"]


def test_graph_frames_equal_their_http_answers(tmp_path: Path) -> None:
    folder = code_free_graph(tmp_path)
    raw = yaml.safe_load((folder / "support_intake.yaml").read_text())
    by_id = {"graph": {"id": "jeff.graphs.support_intake"}, "input": "My wardrobe is broken", "params": PARAMS}
    inline = {"graph": {"inline": raw}, "input": "unsure", "params": PARAMS,
              "on_low_confidence_by_node": {"urgency": "error"}}
    with client(fake(), graphs=folder) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        answer = channel.result({"op": "graph", "id": 1, **by_id})
        refused = channel.request({"op": "graph", "id": 2, **inline})
        unknown = channel.error({"op": "graph", "id": 3, **by_id, "graph": {"id": "nope"}}, "GraphError", "nope")
        http_answer = decide_http(http, by_id)
        http_refused = decide_http(http, inline)
        http_unknown = decide_http(http, {**by_id, "graph": {"id": "nope"}})
    assert GraphResult.from_json(answer["graph_result"]) == GraphResult.from_json(http_answer["graph_result"])
    assert assert_error(refused, http_refused["error"]["type"]) == http_refused["error"]
    assert unknown == http_unknown["error"]


# --- limits --------------------------------------------------------------------------------------------------------------

def test_1024_handles_are_allowed_and_the_1025th_is_too_many_handles() -> None:
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        for start_id in range(1, 1025, 256):  # in batches, under the 256 outstanding requests
            for id_ in range(start_id, start_id + 256):
                channel.send({"op": "bind", "id": id_, **URGENCY})
            replies = channel.receive_all(256)
            assert all(reply["ok"] for reply in replies.values())
        error = channel.error({"op": "bind", "id": 2000, **URGENCY}, "TooManyHandles", "1024")
        assert error["type"] == "TooManyHandles"
        channel.result({"op": "release", "id": 2001, "handle": 5})
        assert channel.result({"op": "bind", "id": 2002, **URGENCY})["handle"] == 1025


def test_256_outstanding_requests_are_allowed_and_the_257th_is_rate_limited() -> None:
    engine = GatedEngine()
    try:
        with client(engine) as http, http.websocket_connect("/v1/channel") as ws:
            channel = start(ws)
            channel.result({"op": "bind", "id": 1, **URGENCY})
            for id_ in range(2, 258):
                channel.send({"op": "call", "id": id_, "handle": 1, "input": "x"})
            wait_until(lambda: engine.waiting > 0)
            error = channel.error({"op": "call", "id": 999, "handle": 1, "input": "x"}, "RateLimited", "256")
            assert error["details"]["retry_after_s"] == 1
            engine.gate.set()
            replies = channel.receive_all(256)
            assert set(replies) == set(range(2, 258)) and all(reply["ok"] for reply in replies.values())
            channel.result({"op": "call", "id": 1000, "handle": 1, "input": "x"})
    finally:
        engine.gate.set()


def test_every_call_frame_counts_against_the_callers_rate_limit() -> None:
    """With one request in flight allowed per caller: a call held by the engine on one connection makes the same
    caller's call, decide and bind on another connection, and its HTTP request, RateLimited; a burst over the rate
    is refused too."""
    engine = GatedEngine()
    try:
        with client(engine, api_key=API_KEY, rate_limit=(1000.0, 1)) as http, \
                http.websocket_connect("/v1/channel") as first_ws, http.websocket_connect("/v1/channel") as second_ws:
            first, second = start(first_ws, API_KEY), start(second_ws, API_KEY)
            first.result({"op": "bind", "id": 2, **URGENCY})
            second.result({"op": "bind", "id": 2, **URGENCY})
            first.send({"op": "call", "id": 3, "handle": 1, "input": "x"})
            wait_until(lambda: engine.waiting == 1)
            error = second.error({"op": "call", "id": 3, "handle": 1, "input": "x"}, "RateLimited", "in flight")
            assert error["details"]["retry_after_s"] == 1
            second.error({"op": "decide", "id": 4, "calls": [{**URGENCY, "input": "x"}]}, "RateLimited")
            second.error({"op": "bind", "id": 6, **URGENCY}, "RateLimited", "in flight")
            assert decide_http(http, {"calls": [{**URGENCY, "input": "x"}]}, API_KEY)["status"] == 429
            engine.gate.set()
            assert first.receive()["ok"] is True
            second.result({"op": "call", "id": 5, "handle": 1, "input": "x"})
            assert http.app.state.limiter.in_flight == {}  # type: ignore[attr-defined]
    finally:
        engine.gate.set()
    with client(fake(), api_key=API_KEY, rate_limit=(2.0, 100)) as http, \
            http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, API_KEY)
        channel.result({"op": "bind", "id": 2, **URGENCY})
        for id_ in range(3, 8):
            channel.send({"op": "call", "id": id_, "handle": 1, "input": "x"})
        replies = channel.receive_all(5)
        limited = [reply for reply in replies.values() if not reply["ok"]]
        assert len(limited) >= 3 and all(reply["error"]["type"] == "RateLimited" for reply in limited)
        assert all(reply["error"]["details"]["retry_after_s"] > 0 for reply in limited)


# --- licences, refresh, expiry -------------------------------------------------------------------------------------------

def test_a_refresh_without_the_licence_makes_the_next_call_not_licensed(keys: tuple[Ed25519PrivateKey, Path],
                                                                         tmp_path: Path) -> None:
    private, public = keys
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.support.triage")
    with client(fake(), catalogue, hub_public_key=public) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, token(private, licences=["jeff.support.triage"]))
        channel.result({"op": "bind", "id": 2, **URGENCY})
        channel.result({"op": "call", "id": 3, "handle": 1, "input": "x"})
        assert channel.result({"op": "refresh", "id": 4, "token": token(private, licences=[])}) == {}
        channel.error({"op": "call", "id": 5, "handle": 1, "input": "x"}, "NotLicensed", "jeff.support.triage")
        channel.error({"op": "decide", "id": 6, "calls": [{**URGENCY, "input": "x"}]}, "NotLicensed")
        channel.error({"op": "bind", "id": 7, **URGENCY}, "NotLicensed")
        channel.error({"op": "refresh", "id": 8, "token": "not-a-token"}, "Unauthorised")
        channel.result({"op": "bind", "id": 9, "function": "spam.message_type", "context": {"channel": "email"}})


def test_a_refresh_for_another_caller_closes_the_channel(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    with client(fake(), hub_public_key=public) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, token(private, sub="alice"))
        channel.error({"op": "refresh", "id": 2, "token": token(private, sub="mallory")}, "Unauthorised",
                      "alice", "mallory")
        assert_closed_with(channel, "Unauthorised", "mallory")


def test_a_token_expiring_with_calls_in_flight_answers_them_then_closes(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    """Review focus 1: the calls already running when the token expires get their answers, a frame sent after it
    gets none, then the server sends Unauthorised with id 0 and closes."""
    private, public = keys
    engine = GatedEngine()
    try:
        with client(engine, hub_public_key=public) as http, http.websocket_connect("/v1/channel") as ws:
            short = token(private, ttl_s=2)
            expires = jwt.decode(short, options={"verify_signature": False})["exp"]
            channel = start(ws, short)
            channel.result({"op": "bind", "id": 2, **URGENCY})
            for id_ in (3, 4, 5):
                channel.send({"op": "call", "id": id_, "handle": 1, "input": "x"})
            wait_until(lambda: engine.waiting == 3)
            wait_until(lambda: time.time() > expires + 0.3, timeout=5)
            channel.send({"op": "call", "id": 6, "handle": 1, "input": "x"})  # after expiry: never read
            engine.gate.set()
            replies = channel.receive_all(3)
            assert set(replies) == {3, 4, 5} and all(reply["ok"] for reply in replies.values())
            assert_closed_with(channel, "Unauthorised", "the channel's token expired")
            assert http.app.state.limiter is None  # type: ignore[attr-defined]
            wait_until(lambda: channel_tasks(http) == [])
    finally:
        engine.gate.set()


# --- served adapters, engines, disconnects ---------------------------------------------------------------------------

class Deployment:
    """Engine processes over real sockets (jeff_runtime.testing.Running), one SocketEngine each, and an API app over
    them through the EngineRouter, as in production."""

    def __init__(self, count: int, installed: set[str] = FIXTURE_ADAPTERS) -> None:
        self.engines = [FakeEngine(answers, set(installed)) for _ in range(count)]
        self.servers = [Running(engine, fixed={adapter: Path("/weights") / adapter for adapter in installed})
                        for engine in self.engines]
        self.sockets = [SocketEngine(server.path, timeout_s=5) for server in self.servers]

    def app(self) -> TestClient:
        return client(EngineRouter(self.sockets))

    def close(self) -> None:
        for engine in self.sockets:
            engine.close()
        for server in self.servers:
            server.close()


@pytest.fixture
def two() -> Iterator[Deployment]:
    deployment = Deployment(2)
    yield deployment
    deployment.close()


def test_a_handle_whose_adapter_stops_being_served_answers_not_installed(two: Deployment) -> None:
    """Review focus 2: another client removes the adapter (PUT /v1/adapters/served); the next call on the handle is
    NotInstalled and the connection keeps serving the other handles."""
    with two.app() as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.result({"op": "bind", "id": 1, **URGENCY})
        channel.result({"op": "bind", "id": 2, "function": "spam.message_type", "context": {"channel": "email"}})
        channel.result({"op": "call", "id": 3, "handle": 1, "input": "x"})
        response = http.put("/v1/adapters/served", json={"adapters": ["jeff.safety.spam"]})
        assert response.status_code == 200, response.text
        channel.error({"op": "call", "id": 4, "handle": 1, "input": "x"}, "NotInstalled", "jeff.support.triage")
        channel.result({"op": "call", "id": 5, "handle": 2, "input": "WIN NOW"})


def test_every_call_on_a_handle_lands_on_the_same_engine(two: Deployment) -> None:
    """The engine router picks the engine by the binding (adapter, question, context), which is the same for every
    call on a handle; handles with different contexts spread over both engines."""
    companies = [f"Shop number {index}." for index in range(8)]
    with two.app() as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        for index, company in enumerate(companies, start=1):
            channel.result({"op": "bind", "id": index, "function": "triage.urgency",
                            "context": {"company": company, "channel": "email"}})
        id_ = 100
        for handle in range(1, len(companies) + 1):
            for text in ("first", "second", "third"):
                id_ += 1
                channel.result({"op": "call", "id": id_, "handle": handle, "input": f"{text} {handle}"})
            id_ += 1
            channel.result({"op": "call_many", "id": id_, "handle": handle, "inputs": ["a", "b", "c"]})
    engines_by_company: dict[str, set[int]] = {}
    for index, engine in enumerate(two.engines):
        for _, rows in engine.calls:
            for row in rows:
                engines_by_company.setdefault(row["state"]["company"], set()).add(index)
    assert set(engines_by_company) == set(companies)
    assert all(len(engines) == 1 for engines in engines_by_company.values()), engines_by_company
    assert set.union(*engines_by_company.values()) == {0, 1}


def borrowed_threads(http: TestClient) -> int:
    async def borrowed() -> int:
        return int(anyio.to_thread.current_default_thread_limiter().borrowed_tokens)
    return http.portal.call(borrowed)  # type: ignore[union-attr]


def set_thread_pool(http: TestClient, size: int) -> None:
    async def resize() -> None:
        anyio.to_thread.current_default_thread_limiter().total_tokens = size
    http.portal.call(resize)  # type: ignore[union-attr]


def test_a_disconnect_keeps_running_calls_slots_until_the_engine_returns_then_frees_everything() -> None:
    """Review focus 4, as on the HTTP API: after the client goes away, a call whose engine call is running keeps its
    rate-limit slot, its task and its decide thread until the engine returns (so a caller cannot reconnect and put
    more work in the engine than its limit allows); then everything is freed and no task is left."""
    engine = GatedEngine()
    try:
        with client(engine, api_key=API_KEY, rate_limit=(1000.0, 10)) as http:
            limiter = http.app.state.limiter  # type: ignore[attr-defined]
            with http.websocket_connect("/v1/channel") as ws:
                channel = start(ws, API_KEY)
                channel.result({"op": "bind", "id": 2, **URGENCY})
                for id_ in range(3, 13):
                    channel.send({"op": "call", "id": id_, "handle": 1, "input": "x"})
                wait_until(lambda: engine.waiting == 10)
                ws.close()
                time.sleep(0.2)
                assert limiter.in_flight == {"api-key:api-key": 10}
                assert len(channel_tasks(http)) == 10
                assert borrowed_threads(http) == 10
            with http.websocket_connect("/v1/channel") as ws:  # the same caller, at its limit of 10 in flight
                channel = start(ws, API_KEY)
                channel.error({"op": "bind", "id": 2, **URGENCY}, "RateLimited", "in flight")
            engine.gate.set()
            wait_until(lambda: limiter.in_flight == {} and channel_tasks(http) == []
                       and not http.app.state.channels)  # type: ignore[attr-defined]
            assert borrowed_threads(http) == 0
            with http.websocket_connect("/v1/channel") as ws:  # the server is still serving
                channel = start(ws, API_KEY)
                channel.result({"op": "bind", "id": 2, **URGENCY})
                channel.result({"op": "call", "id": 3, "handle": 1, "input": "x"})
            assert limiter.in_flight == {}
    finally:
        engine.gate.set()


def test_a_disconnect_frees_at_once_the_calls_still_waiting_for_a_thread() -> None:
    """With two decide threads and eight calls, two run in the engine and six wait for a thread: when the client
    goes away the six give back their slots at once, the two when the engine returns."""
    engine = GatedEngine()
    try:
        with client(engine, api_key=API_KEY, rate_limit=(1000.0, 100)) as http:
            set_thread_pool(http, 2)
            limiter = http.app.state.limiter  # type: ignore[attr-defined]
            with http.websocket_connect("/v1/channel") as ws:
                channel = start(ws, API_KEY)
                channel.result({"op": "bind", "id": 2, **URGENCY})
                for id_ in range(3, 11):
                    channel.send({"op": "call", "id": id_, "handle": 1, "input": "x"})
                wait_until(lambda: engine.waiting == 2 and limiter.in_flight == {"api-key:api-key": 8})
                ws.close()
                wait_until(lambda: limiter.in_flight == {"api-key:api-key": 2} and len(channel_tasks(http)) == 2)
                assert borrowed_threads(http) == 2
            engine.gate.set()
            wait_until(lambda: limiter.in_flight == {} and channel_tasks(http) == []
                       and not http.app.state.channels)  # type: ignore[attr-defined]
            assert engine.waiting == 2  # the six never reached the engine
    finally:
        engine.gate.set()


# --- reading frames without holding up the server ------------------------------------------------------------------

def health_seconds(http: TestClient) -> float:
    """How long GET /health takes. Over a FakeEngine it answers 500 (health needs engine processes), but it is
    answered on the event loop all the same, which is what is timed: a loop held up by a frame answers late."""
    started = time.monotonic()
    http.get("/health")
    return time.monotonic() - started


def test_a_frame_over_the_pre_auth_cap_closes_the_channel_unread() -> None:
    with client(fake(), api_key=API_KEY) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.send({"op": "auth", "id": 1, "token": "x" * (channel_module.PREAUTH_MAX_BODY + 1)})
        assert_closed_with(channel, "InvalidRequest", "before auth", str(channel_module.PREAUTH_MAX_BODY))
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:  # no credentials: the normal cap
        channel = start(ws)
        channel.result({"op": "bind", "id": 1, **URGENCY})
        big = "x" * (channel_module.PREAUTH_MAX_BODY * 4)
        assert channel.result({"op": "call", "id": 2, "handle": 1, "input": big})["decision"]


def test_an_unauthenticated_big_frame_never_holds_up_the_server() -> None:
    """Review C1: a 16 MB frame of eight million tiny values, sent without credentials, is refused from its length
    alone; /health answers at once meanwhile."""
    big = msgpack.packb({"op": "call", "id": 5, "handle": 1, "input": [[1]] * 8_000_000})
    with client(fake(), api_key=API_KEY) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.send_raw(big)
        assert health_seconds(http) < 0.5
        assert_closed_with(channel, "InvalidRequest", "before auth")


def test_a_big_frame_after_auth_is_read_off_the_event_loop() -> None:
    """A 20 MB input is unpacked and checked on the decide thread pool; /health answers quickly meanwhile, and the
    frame is answered."""
    with client(fake(), api_key=API_KEY) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, API_KEY)
        channel.result({"op": "bind", "id": 2, **URGENCY})
        channel.send({"op": "call", "id": 3, "handle": 1, "input": "word " * 4_000_000})
        assert health_seconds(http) < 0.5
        assert channel.receive(timeout=30)["ok"] is True


def test_reusing_an_outstanding_id_closes_the_channel() -> None:
    engine = GatedEngine()
    try:
        with client(engine) as http, http.websocket_connect("/v1/channel") as ws:
            channel = start(ws)
            channel.result({"op": "bind", "id": 1, **URGENCY})
            channel.send({"op": "call", "id": 5, "handle": 1, "input": "x"})
            wait_until(lambda: engine.waiting == 1)
            channel.send({"op": "call", "id": 5, "handle": 1, "input": "y"})
            final = channel.receive()
            assert final["id"] == 0
            assert_error(final, "InvalidRequest", "id 5", "outstanding")
            channel.closed()
            engine.gate.set()
            wait_until(lambda: channel_tasks(http) == [])
        with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:  # once answered, an id may return
            channel = start(ws)
            channel.result({"op": "bind", "id": 1, **URGENCY})
            channel.result({"op": "call", "id": 1, "handle": 1, "input": "x"})
    finally:
        engine.gate.set()


# --- more auth and handle cases ----------------------------------------------------------------------------------------

def test_two_binds_racing_for_the_last_handle_keep_the_limit() -> None:
    """Both arrive while 1,023 handles are live, so both pass the check on arrival; the check when a bind finishes
    keeps the 1,025th out."""
    with client(fake()) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        for start_id in range(1, 1024, 256):
            batch = range(start_id, min(start_id + 256, 1024))
            for id_ in batch:
                channel.send({"op": "bind", "id": id_, **URGENCY})
            assert all(reply["ok"] for reply in channel.receive_all(len(batch)).values())
        channel.send({"op": "bind", "id": 2000, **URGENCY})
        channel.send({"op": "bind", "id": 2001, **URGENCY})
        replies = channel.receive_all(2)
        assert sorted(reply["ok"] for reply in replies.values()) == [False, True]
        assert next(reply for reply in replies.values() if not reply["ok"])["error"]["type"] == "TooManyHandles"


def test_a_call_in_flight_when_a_refresh_lands_keeps_the_licences_it_arrived_with(
        keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    private, public = keys
    engine = GatedEngine()
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.support.triage")
    try:
        with client(engine, catalogue, hub_public_key=public) as http, http.websocket_connect("/v1/channel") as ws:
            channel = start(ws, token(private, licences=["jeff.support.triage"]))
            channel.result({"op": "bind", "id": 2, **URGENCY})
            channel.send({"op": "call", "id": 3, "handle": 1, "input": "x"})
            wait_until(lambda: engine.waiting == 1)
            assert channel.result({"op": "refresh", "id": 4, "token": token(private, licences=[])}) == {}
            engine.gate.set()
            assert channel.receive()["ok"] is True
            channel.error({"op": "call", "id": 5, "handle": 1, "input": "x"}, "NotLicensed")
    finally:
        engine.gate.set()


def test_auth_refuses_a_forged_hs256_token_and_an_expired_one(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    now = int(time.time())
    forged = jwt.encode({"sub": "x", "licences": ["jeff.support.triage"], "iat": now, "exp": now + 60}, "secret",
                        algorithm="HS256")
    expired = jwt.encode({"sub": "x", "licences": [], "iat": now - 100, "exp": now - 10}, private, algorithm="EdDSA")
    with client(fake(), hub_public_key=public) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.error({"op": "auth", "id": 1, "token": forged}, "Unauthorised", "not valid")
        channel.error({"op": "auth", "id": 2, "token": expired}, "Unauthorised", "expired")
        channel.error({"op": "bind", "id": 3, **URGENCY}, "Unauthorised", "not authenticated")


def test_before_auth_a_request_is_refused_without_its_shape_being_checked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Review: a caller without credentials must not make the server check a request (a 34 KB decide frame took
    12.5 ms on the event loop). Every request but auth is refused Unauthorised before its shape is checked."""
    checked: list[str] = []
    real = channel_module._checked
    monkeypatch.setattr(channel_module, "_checked", lambda op, raw: (checked.append(op), real(op, raw))[1])
    calls = [{**URGENCY, "input": "x" * 100}] * 256
    with client(fake(), api_key=API_KEY) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.error({"op": "decide", "id": 1, "calls": calls}, "Unauthorised", "not authenticated")
        channel.error({"op": "predict", "id": 2, "adapter": "a", "rows": [{}]}, "Unauthorised")
        assert checked == []
        channel.result({"op": "auth", "id": 3, "token": API_KEY})
        channel.result({"op": "decide", "id": 4, "calls": [{**URGENCY, "input": "x"}]})
        assert checked == ["auth", "decide"]


class SlowInstalled(FakeEngine):
    """A fake engine whose installed() waits for `gate` once `block` is set (a bind asks it whether the adapter is
    served), so a bind can be held in its thread."""

    def __init__(self) -> None:
        super().__init__(answers, set(FIXTURE_ADAPTERS))
        self.block = False
        self.gate = threading.Event()
        self.waiting = 0

    def installed(self, refresh: bool = False) -> set[str]:
        if self.block:
            self.waiting += 1
            if not self.gate.wait(20):
                raise EngineError("SlowInstalled: the test never opened the gate")
        return super().installed(refresh)


def test_a_bind_that_finishes_after_the_connection_closed_keeps_no_handle() -> None:
    engine = SlowInstalled()
    try:
        with client(engine) as http:
            with http.websocket_connect("/v1/channel") as ws:
                channel = start(ws)
                engine.block = True
                channel.send({"op": "bind", "id": 1, **URGENCY})
                wait_until(lambda: engine.waiting >= 1)
                [connection] = http.app.state.channels  # type: ignore[attr-defined]
                ws.close()
                wait_until(lambda: connection.closed)
            engine.gate.set()
            wait_until(lambda: not http.app.state.channels)  # type: ignore[attr-defined]
            assert connection.handles == {}
    finally:
        engine.gate.set()


def test_binds_count_against_the_callers_rate_limit_as_http_requests_do() -> None:
    """50 binds of a function whose adapter is not served, at 1 request per second: as the same call over HTTP
    (424, then 429), the first is NotInstalled and the rest RateLimited, without asking the engine again."""
    engine = fake({"jeff.safety.spam"})
    refreshes = {"count": 0}
    real = engine.installed

    def counting(refresh: bool = False) -> set[str]:
        refreshes["count"] += refresh
        return real(refresh=refresh)

    engine.installed = counting  # type: ignore[method-assign]
    with client(engine, api_key=API_KEY, rate_limit=(1.0, 1)) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, API_KEY)
        before = refreshes["count"]
        for id_ in range(2, 52):
            channel.send({"op": "bind", "id": id_, **URGENCY})
        replies = channel.receive_all(50)
        types = sorted(reply["error"]["type"] for reply in replies.values())
        assert types == ["NotInstalled"] + ["RateLimited"] * 49
        assert refreshes["count"] - before <= 1
        assert [decide_http(http, {"calls": [{**URGENCY, "input": "x"}]}, API_KEY)["status"] for _ in range(2)] == \
            [429, 429]


# --- console sessions, the access log, /metrics ------------------------------------------------------------------------

CONSOLE_LOGIN, CONSOLE_PASSWORD = "alice@example.com", "pw-123456789"


def users_file(tmp_path: Path) -> Path:
    path = tmp_path / "users.json"
    write_file(path, "s" * 64, {CONSOLE_LOGIN: hash_password(CONSOLE_PASSWORD)})
    return path


def remove_login(path: Path) -> None:
    secret, users = read_file(path)
    del users[CONSOLE_LOGIN]
    write_file(path, secret, users)
    later = time.time() + 5  # a modification time the store has not seen, however coarse the file system's clock
    os.utime(path, (later, later))


def sign_in(http: TestClient) -> str:
    response = http.post("/ui/login", json={"email": CONSOLE_LOGIN, "password": CONSOLE_PASSWORD})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


@pytest.mark.parametrize("frame", [
    {"op": "call", "id": 3, "handle": 1, "input": "x"},
    {"op": "bind", "id": 3, **URGENCY},
    {"op": "decide", "id": 3, "calls": [{**URGENCY, "input": "x"}]},
])
def test_a_console_session_ended_on_the_server_closes_the_channel_at_its_next_frame(
        tmp_path: Path, frame: dict[str, Any]) -> None:
    """Removing the login from the users file ends its sessions: HTTP answers 401 at once, and the channel, at the
    next bind or call, closes with Unauthorised (id 0) instead of answering as the owner."""
    path = users_file(tmp_path)
    with client(fake(), users_file=path) as http, http.websocket_connect("/v1/channel") as ws:
        session = sign_in(http)
        channel = start(ws, session)
        channel.result({"op": "bind", "id": 2, **URGENCY})
        remove_login(path)
        assert decide_http(http, {"calls": [{**URGENCY, "input": "x"}]}, session)["status"] == 401
        channel.send(frame)
        assert_closed_with(channel, "Unauthorised", "console session has ended")


def test_a_current_console_session_keeps_the_channel_open(tmp_path: Path) -> None:
    path = users_file(tmp_path)
    with client(fake(), users_file=path) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, sign_in(http))
        channel.result({"op": "bind", "id": 2, **URGENCY})
        secret, users = read_file(path)
        users["bob@example.com"] = users[CONSOLE_LOGIN]  # another change to the file: alice's session still holds
        write_file(path, secret, users)
        channel.result({"op": "call", "id": 3, "handle": 1, "input": "x"})


def test_channel_connections_are_written_to_the_access_log(keys: tuple[Ed25519PrivateKey, Path],
                                                           tmp_path: Path) -> None:
    """One entry per connection: when it is authenticated (200, with the caller), or, when it ends without having
    been, at its end (401), counting the failed auth attempts."""
    private, public = keys
    log = tmp_path / "access.jsonl"
    ids = []
    with client(fake(), hub_public_key=public, access_log=log) as http:
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws)
            ids.append(channel.request({"op": "auth", "id": 1, "token": "wrong"})["error"]["details"]["channel_id"])
            channel.result({"op": "auth", "id": 2, "token": token(private, sub="alice")})
            channel.result({"op": "bind", "id": 3, **URGENCY})
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws)
            ids.append(channel.request({"op": "auth", "id": 1, "token": "wrong"})["error"]["details"]["channel_id"])
            channel.error({"op": "auth", "id": 2, "token": "wrong again"}, "Unauthorised")
        wait_until(lambda: not http.app.state.channels)  # type: ignore[attr-defined]
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["event"], e["user"], e["path"], e["status"], e["detail"]) for e in entries] == [
        ("api", "hub:alice", "/v1/channel", 200, f"WEBSOCKET hub, channel {ids[0]}, 1 failed auth"),
        ("api", None, "/v1/channel", 401, f"WEBSOCKET, channel {ids[1]}, 2 failed auth")]
    assert all(entry["ip"] == "testclient" for entry in entries)


def test_an_open_servers_channel_is_logged_when_it_opens(tmp_path: Path) -> None:
    log = tmp_path / "access.jsonl"
    with client(fake(), access_log=log) as http, http.websocket_connect("/v1/channel") as ws:
        start(ws)
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["user"], e["path"], e["status"]) for e in entries] == [(None, "/v1/channel", 200)]
    assert re.fullmatch(r"WEBSOCKET open, channel [0-9a-f]{8}", entries[0]["detail"])


def test_channel_connections_are_counted_in_metrics_by_close_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """Each connection is counted under /v1/channel once it has ended, by its WebSocket close code: the client's
    (1000, a normal close), 1008 when the server closed it for credentials, 1002 for a protocol error."""
    monkeypatch.setattr(channel_module, "AUTH_TIMEOUT_S", 0.3)
    with client(fake(), api_key=API_KEY) as http:
        with http.websocket_connect("/v1/channel") as ws:
            start(ws, API_KEY)
        with http.websocket_connect("/v1/channel") as ws:
            assert_closed_with(start(ws), "Unauthorised", "0.3 seconds")
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws, API_KEY)
            ws.send_text("not binary")
            assert_closed_with(channel, "InvalidRequest", "binary")
        wait_until(lambda: not http.app.state.channels)  # type: ignore[attr-defined]
        counts = http.app.state.request_counts.snapshot()  # type: ignore[attr-defined]
    channel_counts = {status: count for (route, status), count in counts.items() if route == "/v1/channel"}
    assert channel_counts == {1000: 1, 1008: 1, 1002: 1}


class Failing(FakeEngine):
    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        raise RuntimeError("the engine broke")


def test_every_failure_names_the_connection_id_as_the_server_log_does(caplog: pytest.LogCaptureFixture) -> None:
    """Each connection has an id (8 hex characters, different per connection): every failure it sends carries it
    in its details as channel_id, the id-0 failure that closes it too, and the server's log lines name it."""
    with client(Failing(answers, set(FIXTURE_ADAPTERS))) as http:
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws)
            [connection] = http.app.state.channels  # type: ignore[attr-defined]
            refused = channel.request({"op": "call", "id": 2, "handle": 9, "input": "x"})
            assert refused["error"]["details"] == {"channel_id": connection.id}
            assert re.fullmatch(r"[0-9a-f]{8}", connection.id)
            channel.result({"op": "bind", "id": 3, **URGENCY})
            with caplog.at_level("ERROR", logger="jeff_server"):
                broken = channel.request({"op": "call", "id": 4, "handle": 1, "input": "x"})
            assert broken["error"]["type"] == "EngineError"
            assert broken["error"]["details"]["channel_id"] == connection.id
            assert any(connection.id in record.getMessage() for record in caplog.records)
            ws.send_text("not binary")
            final = channel.receive()
            assert final["id"] == 0 and final["error"]["details"]["channel_id"] == connection.id
        with http.websocket_connect("/v1/channel") as ws:
            other = start(ws).request({"op": "call", "id": 2, "handle": 9, "input": "x"})
            assert other["error"]["details"]["channel_id"] != connection.id


# --- shutdown ------------------------------------------------------------------------------------------------------------

class ClosingEngine(GatedEngine):
    """A GatedEngine the app owns (built in place of the engine router), recording when predictions finish and when
    it is closed."""

    def __init__(self) -> None:
        super().__init__()
        self.events: list[str] = []

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        result = super().predict(adapter_id, rows)
        self.events.append("predicted")
        return result

    def close(self) -> None:
        self.events.append("closed")


def owned_by_the_app(monkeypatch: pytest.MonkeyPatch, engine: ClosingEngine) -> None:
    from jeff_server import app as app_module
    monkeypatch.setattr(app_module, "_build_engine_router", lambda settings: engine)


def test_shutdown_waits_for_in_flight_channel_calls_before_closing_the_engines(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    engine = ClosingEngine()
    owned_by_the_app(monkeypatch, engine)
    timer = threading.Timer(0.5, engine.gate.set)
    try:
        with caplog.at_level("ERROR"):
            with TestClient(create_app(settings())) as http:
                with http.websocket_connect("/v1/channel") as ws:
                    channel = start(ws)
                    channel.result({"op": "bind", "id": 1, **URGENCY})
                    for id_ in (2, 3):
                        channel.send({"op": "call", "id": id_, "handle": 1, "input": "x"})
                    wait_until(lambda: engine.waiting == 2)
                timer.start()  # the client has gone; the two calls are still in the engine
            gc.collect()
        assert engine.events == ["predicted", "predicted", "closed"]
        assert not [record for record in caplog.records if "destroyed but it is pending" in record.getMessage()]
    finally:
        timer.cancel()
        engine.gate.set()


def test_shutdown_waits_at_most_the_engine_timeout_then_cancels_and_logs(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    engine = ClosingEngine()
    owned_by_the_app(monkeypatch, engine)
    try:
        with caplog.at_level("ERROR", logger="jeff_server"):
            with TestClient(create_app(settings(engine_timeout_s=0.5))) as http:
                with http.websocket_connect("/v1/channel") as ws:
                    channel = start(ws)
                    channel.result({"op": "bind", "id": 1, **URGENCY})
                    channel.send({"op": "call", "id": 2, "handle": 1, "input": "x"})
                    wait_until(lambda: engine.waiting == 1)
                started = time.monotonic()
                threading.Timer(1.5, engine.gate.set).start()  # the engine call outlasts the wait
        assert time.monotonic() - started < 5
        assert engine.events[-1] == "closed"
        assert any("1 channel request(s)" in record.getMessage() and "0.5" in record.getMessage()
                   for record in caplog.records), [record.getMessage() for record in caplog.records]
    finally:
        engine.gate.set()


# --- decide, dispatch and graph frames are checked once ----------------------------------------------------------------

def test_the_socket_schemas_decide_entries_are_the_v1_decide_body_shapes_with_op_and_id() -> None:
    """Why a decide, dispatch or graph frame is not checked against decide_request again after its socket schema
    entry: each entry, without op and id, is exactly one of decide_request's three shapes."""
    socket_definitions = contract.load_schema("socket")["$defs"]
    shapes = contract.load_schema("decide_request")["oneOf"]
    remote_ref = "https://jeff.dev/schemas/decide_request.schema.json#"

    def local_refs(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: (item.replace(remote_ref, "#") if key == "$ref" else local_refs(item))
                    for key, item in value.items()}
        return [local_refs(item) for item in value] if isinstance(value, list) else value

    for op, shape in zip(("decide", "dispatch", "graph"), shapes, strict=True):
        entry = local_refs(socket_definitions[op])
        properties = {key: value for key, value in entry["properties"].items() if key not in ("op", "id")}
        assert properties == shape["properties"], op
        assert set(entry["required"]) - {"op", "id"} == set(shape["required"]), op
        assert entry["additionalProperties"] is False and shape["additionalProperties"] is False


def test_a_decide_frame_is_checked_against_the_schema_once(monkeypatch: pytest.MonkeyPatch) -> None:
    checked: list[str] = []
    real = contract.is_valid

    def counting(name: Any, document: object) -> bool:
        checked.append(name)
        return real(name, document)

    monkeypatch.setattr(contract, "is_valid", counting)
    with client(fake(ROUTED), ROUTERS) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        channel.result({"op": "decide", "id": 1, "calls": [{**URGENCY, "input": "x"}]})
        channel.result({"op": "dispatch", "id": 2, "dispatch": {"input": "x", "interface": "rate", "context": CONTEXT}})
        channel.error({"op": "dispatch", "id": 3, "dispatch": {"input": "x", "interface": "nope"}}, "InvalidRequest",
                      "nope")
    assert "decide_request" not in checked
