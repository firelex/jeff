"""Operating the server: the adapter endpoints change what every engine process serves (refused on a public server),
/health reports each engine, /metrics is Prometheus text, and the access log records who called from where.

Engines here are real engine processes' servers (jeff_runtime.testing.Running over a FakeEngine) reached through
SocketEngine and the EngineRouter, as in production."""

import json
import os
import re
import socket as socket_module
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from jeff import contract, socket_engine
from jeff.engine import EngineResult
from jeff.errors import EngineError, GraphError, InvalidRequest
from jeff.protocol import encode, hello, ok, read_frame
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine, fake_adapter_folder
from jeff_runtime.engine_server import SERVED_FORMAT, SERVED_VERSION
from jeff_runtime.testing import Running

from jeff_server import operations, users
from jeff_server.access_log import PRIVATE, AccessLog
from jeff_server.app import create_app
from jeff_server.operations import PUBLIC_REFUSAL
from jeff_server.router import EngineRouter
from jeff_server.settings import ServerSettings

from server_testing import hub_keys, token

ROOT = Path(__file__).resolve().parents[3]
ROUTERS = ROOT / "packages" / "python" / "tests" / "fixtures" / "routers"
ALL = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
LEAVES = {"jeff.support.triage", "jeff.safety.spam"}
AVAILABLE = {adapter: Path("/weights") / adapter for adapter in ALL}
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
SPAM_CALL = {"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "email"}}
URGENCY_CALL = {"function": "triage.urgency", "input": "It is on fire", "context": CONTEXT}
DISPATCH = {"dispatch": {"input": "My wardrobe is broken", "interface": "rate", "context": CONTEXT}}
LOGIN = "menlo@scissero.com"
NEWER = "jeff.newer.adapter"  # a v1.4 adapter folder (fake_adapter_folder)


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


class Deployment:
    """Engine processes, one SocketEngine to each, and an API app over them (several apps may share them)."""

    def __init__(self, count: int, installed: set[str] = ALL, served_files: list[Path] | None = None,
                 available: list[dict[str, Path]] | None = None, base: tuple[str, str] = ("jeff-base", "v1.3"),
                 bases: list[tuple[str, str]] | None = None, adapter_roots: list[Path] | None = None) -> None:
        self.servers = [Running(FakeEngine(answers, set(installed)),
                                served_file=served_files[index] if served_files else None,
                                fixed=available[index] if available is not None else AVAILABLE,
                                base=bases[index] if bases is not None else base,
                                adapter_roots=adapter_roots if adapter_roots is not None else ())
                        for index in range(count)]
        self.sockets: list[SocketEngine] = []

    def app(self, **options: Any) -> TestClient:
        engines = [SocketEngine(server.path, timeout_s=5) for server in self.servers]
        self.sockets.extend(engines)
        settings = ServerSettings(engine_sockets=tuple(server.path for server in self.servers),
                                  model_name="jeff-v1.3", catalogue=ROUTERS, **options)
        return TestClient(create_app(settings, engine=EngineRouter(engines)))

    def served(self) -> list[set[str]]:
        return [set(server.server._snapshot[1]) for server in self.servers]

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


def assert_error(response: Any, status: int, error_type: str, *named: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["x-request-id"]
    error = response.json()["error"]
    assert error["type"] == error_type, error
    for text in named:
        assert text in error["message"], error["message"]
    return dict(error)


def served_flags(body: dict[str, Any]) -> dict[str, bool]:
    return {adapter["id"]: adapter["served"] for adapter in body["adapters"]}


# --- GET /v1/adapters, PUT /v1/adapters/served, POST /v1/adapters/reload --------------------------------------------

def test_adapters_lists_every_catalogue_adapter_marked_served_or_not(two: Deployment) -> None:
    with two.app() as http:
        body = http.get("/v1/adapters").json()
    assert served_flags(body) == {adapter: True for adapter in ALL}
    by_id = {adapter["id"]: adapter for adapter in body["adapters"]}
    assert by_id["jeff.safety.spam"] == {
        "id": "jeff.safety.spam", "aliases": ["spam"], "version": "1.3.0",
        "description": "Says whether a message is legitimate, spam or phishing.",
        "functions": [{"id": "jeff.safety.spam.message_type", "interface": "classify", "calibrated": True}],
        "served": True}
    # calibrated: whether the function has a threshold for the base the engines serve (here bfloat16)
    assert by_id["jeff.support.triage"]["functions"] == [
        {"id": "jeff.support.triage.route", "interface": "route", "calibrated": False},
        {"id": "jeff.support.triage.urgency", "interface": "rate", "calibrated": True},
        {"id": "jeff.support.triage.needs_human", "interface": "requires_escalation", "calibrated": False}]
    # an adapter with no thresholds at all
    assert by_id["jeff.support.router"]["functions"] == [
        {"id": "jeff.support.router.function", "interface": "route", "calibrated": False}]
    assert body["adapters_version"] == 0
    assert contract.is_valid("adapters_response", body)


def test_only_adapters_trained_for_the_engines_base_are_listed_and_the_others_are_still_refused(
        tmp_path: Path) -> None:
    """As on the live v1.4 server: the engines serve base v1.4 and the catalogue also holds v1.3 adapters, which no
    engine can serve. Only the v1.4 adapters are listed; serving a v1.3 one is refused by the engines exactly as
    before (404 UnknownFunction), with nothing changed."""
    root = tmp_path / "adapters"
    fake_adapter_folder(root / "newer", NEWER, base_version="v1.4")
    deployment = Deployment(2, installed={NEWER}, available=[{}, {}], base=("jeff-base", "v1.4"),
                            adapter_roots=[root])
    try:
        with deployment.app(adapter_dirs=(root,)) as http:
            body = http.get("/v1/adapters").json()
            assert served_flags(body) == {NEWER: True}
            assert contract.is_valid("adapters_response", body)
            assert_error(http.put("/v1/adapters/served", json={"adapters": ["spam"]}), 404, "UnknownFunction",
                         "No engine changed its adapters (serve)", "jeff.safety.spam")
            assert deployment.served() == [{NEWER}, {NEWER}]
            # switching the listed adapter off and on again works as before
            assert served_flags(http.put("/v1/adapters/served", json={"adapters": []}).json()) == {NEWER: False}
            assert served_flags(http.put("/v1/adapters/served", json={"adapters": [NEWER]}).json()) == {NEWER: True}
    finally:
        deployment.close()


def test_an_adapter_for_another_base_is_not_listed_even_from_an_adapter_folder(tmp_path: Path) -> None:
    """The engines serve base v1.3; a v1.4 adapter folder in the server's adapter dirs (which no engine scans) is
    not listed, and serving it is refused by the engines as before."""
    root = tmp_path / "adapters"
    fake_adapter_folder(root / "newer", NEWER, base_version="v1.4")
    deployment = Deployment(2)
    try:
        with deployment.app(adapter_dirs=(root,)) as http:
            assert set(served_flags(http.get("/v1/adapters").json())) == ALL
            assert_error(http.put("/v1/adapters/served", json={"adapters": [NEWER]}), 404, "UnknownFunction",
                         "No engine changed its adapters (serve)", NEWER)
            assert deployment.served() == [ALL, ALL]
    finally:
        deployment.close()


def test_engines_serving_different_bases_make_the_listing_a_500_naming_each() -> None:
    deployment = Deployment(2, installed=set(), bases=[("jeff-base", "v1.3"), ("jeff-base", "v1.4")])
    try:
        with deployment.app() as http:
            assert_error(http.get("/v1/adapters"), 500, "EngineError", "different bases", "jeff-base@v1.3",
                         "jeff-base@v1.4", str(deployment.servers[1].path))
    finally:
        deployment.close()


def test_serve_changes_every_engine_and_accepts_aliases_and_ids(two: Deployment) -> None:
    with two.app() as http:
        response = http.put("/v1/adapters/served", json={"adapters": ["spam", "jeff.support.triage", "triage"]})
        assert response.status_code == 200, response.text
        assert served_flags(response.json()) == {"jeff.core.router": False, "jeff.support.router": False,
                                                 "jeff.support.triage": True, "jeff.safety.spam": True}
        assert response.json()["adapters_version"] == 2  # each of the two engines changed once
        assert two.served() == [LEAVES, LEAVES]
        assert http.get("/v1/adapters").json() == response.json()
        assert http.post("/v1/decide", json={"calls": [SPAM_CALL]}).status_code == 200
        assert_error(http.post("/v1/decide", json=DISPATCH), 424, "NotInstalled", "jeff.core.router")


def test_an_unknown_adapter_is_404_and_changes_nothing(two: Deployment) -> None:
    with two.app() as http:
        assert_error(http.put("/v1/adapters/served", json={"adapters": ["spam", "jeff.unknown.adapter"]}), 404,
                     "UnknownFunction", "jeff.unknown.adapter")
    assert two.served() == [ALL, ALL]


def test_the_served_body_must_be_a_list_of_adapters(two: Deployment) -> None:
    with two.app() as http:
        assert_error(http.put("/v1/adapters/served", json={"adapters": "spam"}), 422, "InvalidRequest")
        assert_error(http.put("/v1/adapters/served", json={"adapters": [], "extra": 1}), 422, "InvalidRequest",
                     "extra")


def test_an_empty_adapter_name_is_422_naming_the_problem(two: Deployment) -> None:
    """contract/schemas/served_request.schema.json requires each adapter string to be non-empty; the body is
    validated against that schema (not a looser Pydantic model that would silently accept "") so the two agree."""
    with two.app() as http:
        assert_error(http.put("/v1/adapters/served", json={"adapters": ["spam", ""]}), 422, "InvalidRequest",
                     "/adapters/1")
    assert two.served() == [ALL, ALL]


@pytest.mark.parametrize("raising", ["is_valid", "first_problem"])
def test_a_served_body_too_deeply_nested_to_check_is_422_not_500(two: Deployment, raising: str,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Checking a body against the schema can run out of Python's recursion depth on deeply nested JSON (as
    /v1/decide's can). The served_request schema stops at the first level today, so the validator is made to raise
    RecursionError here, as a recursive schema would."""
    def too_deep(*args: object) -> object:
        raise RecursionError("maximum recursion depth exceeded")

    with two.app() as http:
        monkeypatch.setattr(f"jeff.contract.{raising}", too_deep)
        if raising == "first_problem":
            monkeypatch.setattr("jeff.contract.is_valid", lambda *args: False)
        assert_error(http.put("/v1/adapters/served", json={"adapters": ["spam"]}), 422, "InvalidRequest",
                     "nested too deeply")
    assert two.served() == [ALL, ALL]


def test_a_set_leaving_a_router_without_a_target_is_refused_before_any_engine_changes(two: Deployment) -> None:
    with two.app() as http:
        assert_error(http.put("/v1/adapters/served", json={"adapters": ["jeff.core.router", "spam", "triage"]}),
                     422, "InvalidRequest", "jeff.support.router", "No engine was changed")
    assert two.served() == [ALL, ALL]


def test_a_failure_on_one_engine_names_it_and_health_shows_the_engines_differ() -> None:
    only_leaves = {adapter: AVAILABLE[adapter] for adapter in LEAVES}
    deployment = Deployment(2, installed=LEAVES, available=[AVAILABLE, only_leaves])
    try:
        with deployment.app() as http:
            response = http.put("/v1/adapters/served", json={"adapters": sorted(ALL)})
            assert_error(response, 500, "EngineError", str(deployment.servers[1].path), "may now serve different")
            assert str(deployment.servers[0].path) not in response.json()["error"]["message"].split("Changed")[0]
            assert deployment.served() == [ALL, LEAVES]
            health = http.get("/health").json()
            # Every engine is up but they disagree: only requests for adapters every engine serves are answered
            # (EngineRouter.installed), and the deployment has no one served list, so it is broken, not degraded.
            assert health["status"] == "broken"
            assert "disagree" in health["cause"] and "PUT /v1/adapters/served" in health["cause"]
            assert [engine["status"] for engine in health["engines"]] == ["ready", "ready"]
            assert [engine["adapters"] for engine in health["engines"]] == [sorted(ALL), sorted(LEAVES)]
            assert_error(http.get("/v1/adapters"), 500, "EngineError", "serve different adapters")
            # Repeating a change every engine accepts brings them back in line.
            assert http.put("/v1/adapters/served", json={"adapters": sorted(LEAVES)}).status_code == 200
            assert http.get("/health").json()["status"] == "ready"
    finally:
        deployment.close()


def test_an_adapter_no_engine_may_serve_is_404_from_the_engines() -> None:
    only_leaves = {adapter: AVAILABLE[adapter] for adapter in LEAVES}
    deployment = Deployment(2, installed=LEAVES, available=[only_leaves, only_leaves])
    try:
        with deployment.app() as http:
            assert_error(http.put("/v1/adapters/served", json={"adapters": sorted(ALL)}), 404, "UnknownFunction",
                         "jeff.core.router")
        assert deployment.served() == [LEAVES, LEAVES]
    finally:
        deployment.close()


def served_file(path: Path, adapters: set[str]) -> Path:
    path.write_text(json.dumps({"format": SERVED_FORMAT, "version": SERVED_VERSION, "adapters": sorted(adapters)}))
    return path


def test_reload_reads_every_engines_served_file(tmp_path: Path) -> None:
    files = [served_file(tmp_path / f"served-{index}.json", LEAVES) for index in range(2)]
    deployment = Deployment(2, served_files=files)
    try:
        with deployment.app() as http:
            response = http.post("/v1/adapters/reload")
            assert response.status_code == 200, response.text
            assert {adapter for adapter, served in served_flags(response.json()).items() if served} == LEAVES
            assert deployment.served() == [LEAVES, LEAVES]
    finally:
        deployment.close()


def test_a_reload_leaving_a_router_without_a_target_says_the_engines_changed(tmp_path: Path) -> None:
    files = [served_file(tmp_path / f"served-{index}.json", ALL - {"jeff.support.router"}) for index in range(2)]
    deployment = Deployment(2, served_files=files)
    try:
        with deployment.app() as http:
            assert_error(http.post("/v1/adapters/reload"), 424, "NotInstalled", "jeff.support.router",
                         "already changed the engines", "PUT /v1/adapters/served")
        assert deployment.served() == [ALL - {"jeff.support.router"}] * 2
    finally:
        deployment.close()


def test_a_public_server_refuses_to_change_its_adapters(two: Deployment) -> None:
    with two.app(public=True, api_key="the-key", rate_limit=(100.0, 10)) as http:
        key = {"Authorization": "Bearer the-key"}
        for response in (http.put("/v1/adapters/served", json={"adapters": ["spam"]}, headers=key),
                         http.post("/v1/adapters/reload", headers=key)):
            error = assert_error(response, 422, "InvalidRequest")
            assert error["message"] == PUBLIC_REFUSAL
        assert http.get("/v1/adapters", headers=key).status_code == 200
    assert PUBLIC_REFUSAL == "This server is public: its adapters stay as started; change them on the private server"
    assert two.served() == [ALL, ALL]


def test_the_adapter_endpoints_need_credentials(two: Deployment) -> None:
    with two.app(api_key="the-key") as http:
        assert_error(http.get("/v1/adapters"), 401, "Unauthorised")
        assert_error(http.put("/v1/adapters/served", json={"adapters": ["spam"]}), 401, "Unauthorised")
        assert_error(http.post("/v1/adapters/reload"), 401, "Unauthorised")
    assert two.served() == [ALL, ALL]


# --- Review Focus 1: two API apps sharing one engine -----------------------------------------------------------------

def test_two_api_apps_on_one_engine_see_each_others_changes() -> None:
    deployment = Deployment(1, installed=LEAVES)
    try:
        with deployment.app() as first, deployment.app() as second:
            # The second app's engine list is cached: it has not heard of the routers.
            assert first.put("/v1/adapters/served", json={"adapters": sorted(ALL)}).status_code == 200
            # A router served by the first app is found by the second (re-checked once against the engine).
            response = second.post("/v1/decide", json=DISPATCH)
            assert response.status_code == 200, response.text
            assert response.json()["decision"]["function"] == "jeff.support.triage.urgency"
            # A removed adapter is 424 for the second app.
            assert first.put("/v1/adapters/served", json={"adapters": ["spam"]}).status_code == 200
            assert_error(second.post("/v1/decide", json={"calls": [URGENCY_CALL]}), 424, "NotInstalled",
                         "jeff.support.triage")
            assert second.post("/v1/decide", json={"calls": [SPAM_CALL]}).status_code == 200
    finally:
        deployment.close()


# --- GET /health (Review Focus 5) ------------------------------------------------------------------------------------

def test_health_is_ready_with_every_engine(two: Deployment) -> None:
    with two.app() as http:
        body = http.get("/health").json()
    assert body["status"] == "ready" and body["model"] == "jeff-v1.3"
    # jeff-server's fields, which its client's health() needs: the base's checkpoint and option limit, whether
    # credentials are needed, and what every ready engine can read.
    assert (body["checkpoint"], body["max_options"], body["authentication"], body["modalities"]) == (
        "fake-base", 255, False, ["text", "image"])
    assert [engine["socket"] for engine in body["engines"]] == [str(server.path) for server in two.servers]
    for engine in body["engines"]:
        assert engine["status"] == "ready" and engine["backend"] == "fake" and engine["adapters_version"] == 0
        assert "cause" not in engine


def calls_over_many_bindings() -> list[dict[str, Any]]:
    """Calls with different fixed contexts, so the router spreads them over both engines."""
    return [{"function": "spam.message_type", "input": "hi", "context": {"channel": f"channel {index}"}}
            for index in range(16)]


def test_an_engine_dying_under_a_running_api(two: Deployment) -> None:
    with two.app() as http:
        assert http.post("/v1/decide", json={"calls": calls_over_many_bindings()}).status_code == 200
        dead = two.servers[1]
        dead.close()
        started = time.monotonic()
        response = http.post("/v1/decide", json={"calls": calls_over_many_bindings()})
        assert time.monotonic() - started < 2  # at once, not after the 5 s engine timeout
        assert_error(response, 500, "EngineError", str(dead.path))
        body = http.get("/health").json()
        assert body["status"] == "degraded"
        alive, down = body["engines"]
        assert alive["status"] == "ready"
        # The decide above, or this check, found the connection closed and tried to reconnect; the cause names
        # that attempt's failure.
        assert down["socket"] == str(dead.path) and down["status"] == "down"
        assert f"No engine is listening at {dead.path}" in down["cause"] and "reconnect attempt" in down["cause"]
        two.servers[0].close()
        broken = http.get("/health").json()
        assert broken["status"] == "broken"
        assert (broken["checkpoint"], broken["max_options"], broken["modalities"]) == (None, None, None)


def test_a_restarted_engine_is_back_after_the_next_call_without_an_api_restart(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket_engine, "RECONNECT_INTERVAL_S", 0.2)
    deployment = Deployment(1)
    try:
        with deployment.app() as http:
            assert http.get("/health").json()["status"] == "ready"
            dead = deployment.servers[0]
            dead.close()
            while deployment.sockets[0]._closed is None:  # the reader notices the closed socket a moment later
                time.sleep(0.01)
            body = http.get("/health").json()
            assert body["status"] == "broken"
            (down,) = body["engines"]
            assert down["status"] == "down" and "reconnect attempt" in down["cause"]
            deployment.servers[0] = Running(FakeEngine(answers, set(ALL)), fixed=AVAILABLE, path=dead.path)
            time.sleep(0.25)  # past the reconnect interval since the failed attempt above
            body = http.get("/health").json()
            assert body["status"] == "ready" and body["engines"][0]["status"] == "ready"
            assert http.post("/v1/decide", json={"calls": [SPAM_CALL]}).status_code == 200
            families = parse_metrics(http.get("/metrics").text)
        assert families["jeff_engine_reconnects_total"] == [({"socket": str(dead.path)}, 1.0)]
    finally:
        deployment.close()


def test_public_health_shows_only_the_status_unless_signed_in(two: Deployment) -> None:
    with two.app(public=True, api_key="the-key", rate_limit=(100.0, 10)) as http:
        assert http.get("/health").json() == {"status": "ready"}
        signed_in = http.get("/health", headers={"Authorization": "Bearer the-key"}).json()
        assert signed_in["status"] == "ready" and len(signed_in["engines"]) == 2
        assert signed_in["authentication"] is True and signed_in["max_options"] == 255
        assert_error(http.get("/health", headers={"Authorization": "Bearer wrong"}), 401, "Unauthorised")


def test_private_health_needs_no_credentials(two: Deployment) -> None:
    with two.app(api_key="the-key") as http:
        body = http.get("/health").json()
    assert len(body["engines"]) == 2 and body["authentication"] is True


# --- GET /metrics --------------------------------------------------------------------------------------------------

SAMPLE = re.compile(r'^([a-zA-Z_:][a-zA-Z0-9_:]*)\{((?:[a-zA-Z_][a-zA-Z0-9_]*="(?:[^"\\]|\\.)*",?)*)\} (\S+)$')
LABEL = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def parse_metrics(text: str) -> dict[str, list[tuple[dict[str, str], float]]]:
    """A strict reader of the Prometheus text format: every line is a HELP, a TYPE or a sample of a declared
    family, and every value is a number."""
    assert text.endswith("\n")
    families: dict[str, list[tuple[dict[str, str], float]]] = {}
    for line in text.splitlines():
        if line.startswith("# HELP "):
            continue
        if line.startswith("# TYPE "):
            _, _, name, kind = line.split(" ")
            assert kind in {"gauge", "counter"}
            families[name] = []
            continue
        match = SAMPLE.match(line)
        assert match, f"not a Prometheus sample: {line!r}"
        name, labels, value = match.groups()
        assert name in families, f"{name} has no TYPE line"
        families[name].append((dict(LABEL.findall(labels)), float(value)))
    return families


def test_metrics_are_prometheus_text_per_engine_and_per_route(two: Deployment) -> None:
    with two.app() as http:
        assert http.post("/v1/decide", json={"calls": calls_over_many_bindings()}).status_code == 200
        http.get("/v1/graphs")
        http.get("/v1/no-such-route")
        response = http.get("/metrics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/plain")
        families = parse_metrics(response.text)
    sockets = {str(server.path) for server in two.servers}
    for name in ("jeff_engine_up", "jeff_queue_rows", "jeff_batches_total", "jeff_rows_total", "jeff_busy_total",
                 "jeff_deadline_exceeded_total"):
        assert {labels["socket"] for labels, _ in families[name]} == sockets, name
    assert sum(value for _, value in families["jeff_rows_total"]) == 16
    assert all(value == 1 for _, value in families["jeff_engine_up"])
    quantiles = {(labels["socket"], labels["quantile"]) for labels, _ in families["jeff_model_ms"]}
    assert quantiles == {(socket, quantile) for socket in sockets for quantile in ("0.5", "0.99")}
    requests = {(labels["route"], labels["status"]): value for labels, value in families["jeff_http_requests_total"]}
    assert requests[("/v1/decide", "200")] == 1 and requests[("/v1/graphs", "200")] == 1
    assert requests[("unmatched", "404")] == 1


def test_every_api_side_series_carries_the_worker_process_id(two: Deployment) -> None:
    """Each API worker process counts its own requests, so its series carry worker=<process id>: scrapes of
    different workers are distinct series, never one series that jumps between workers' counts."""
    with two.app() as http:
        http.get("/v1/graphs")
        families = parse_metrics(http.get("/metrics").text)
    samples = families["jeff_http_requests_total"]
    assert samples
    assert all(labels["worker"] == str(os.getpid()) for labels, _ in samples)


def test_metrics_report_an_unreachable_engine_as_down(two: Deployment) -> None:
    with two.app() as http:
        two.servers[1].close()
        families = parse_metrics(http.get("/metrics").text)
    up = {labels["socket"]: value for labels, value in families["jeff_engine_up"]}
    assert up == {str(two.servers[0].path): 1, str(two.servers[1].path): 0}
    assert {labels["socket"] for labels, _ in families["jeff_batches_total"]} == {str(two.servers[0].path)}


def test_metrics_need_credentials_like_the_api(two: Deployment) -> None:
    with two.app(api_key="the-key") as http:
        assert_error(http.get("/metrics"), 401, "Unauthorised")
        assert http.get("/metrics", headers={"Authorization": "Bearer the-key"}).status_code == 200


# --- the access log (ported from jeff-server's tests/test_ui_users.py) ---------------------------------------------

def test_the_access_log_records_who_from_where_and_only_admins_read_it(two: Deployment, tmp_path: Path) -> None:
    log = tmp_path / "access.jsonl"
    users_file = tmp_path / "users.json"
    users.write_file(users_file, "s" * 64, {LOGIN: users.hash_password("right horse")})
    with two.app(access_log=log, admins=frozenset({LOGIN}), users_file=users_file) as http:
        http.get("/v1/graphs", headers={"X-Forwarded-For": "10.9.9.9, 203.0.113.7"})  # the proxy's entry is last
        http.post("/ui/login", json={"email": LOGIN, "password": "wrong"}, headers={"X-Forwarded-For": "198.51.100.2"})
        token = http.post("/ui/login", json={"email": LOGIN, "password": "right horse"}).json()["token"]
        signed_in = {"Authorization": f"Bearer {token}", "X-Forwarded-For": "198.51.100.9"}
        assert http.post("/v1/decide", json={"calls": [SPAM_CALL]}, headers=signed_in).status_code == 200
        http.get("/health")  # not an API call: not logged
        app_log: AccessLog = http.app.state.access_log  # type: ignore[attr-defined]
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(e["event"], e["user"], e["ip"], e["path"], e["status"]) for e in entries] == [
        ("api", None, "203.0.113.7", "/v1/graphs", 401),
        ("signin-failed", LOGIN, "198.51.100.2", "/ui/login", 401),
        ("signin", LOGIN, "testclient", "/ui/login", 200),
        ("api", LOGIN, "198.51.100.9", "/v1/decide", 200)]
    assert entries[0]["forwarded"] == "10.9.9.9, 203.0.113.7" and entries[0]["peer"] == "testclient"
    assert entries[3]["detail"] == "POST console" and entries[3]["location"] is None  # no GeoIP database
    report = app_log.read(10)
    assert report["total"] == 4 and report["anonymous_ips"] == ["203.0.113.7"]
    assert report["people"][0]["ips"] == {"198.51.100.2": 1, "testclient": 1, "198.51.100.9": 1}
    assert report["people"][0]["sign_ins"] == 1 and report["people"][0]["failed_sign_ins"] == 1
    assert report["entries"][0]["path"] == "/v1/decide"  # newest first
    assert app_log.may_read(" Menlo@Scissero.com") and not app_log.may_read("mathias@scissero.com")


def test_no_access_log_setting_logs_nothing(two: Deployment, tmp_path: Path) -> None:
    with two.app() as http:
        assert http.app.state.access_log is None  # type: ignore[attr-defined]
        assert http.get("/v1/graphs").status_code == 200
    assert list(tmp_path.iterdir()) == []


class Lookups:
    def __init__(self) -> None:
        self.asked: list[str] = []

    def get(self, ip: str) -> dict[str, Any]:
        self.asked.append(ip)
        return {"city": {"names": {"en": "Zurich"}}, "subdivisions": [{"names": {"en": "Zurich"}}],
                "country": {"names": {"en": "Switzerland"}}}


def test_private_and_tailnet_addresses_are_not_looked_up(tmp_path: Path) -> None:
    lookups = Lookups()
    log = AccessLog(tmp_path / "access.jsonl", admins=frozenset(), geoip=lookups, server="private")
    for ip in ("192.168.0.165", "100.70.93.56", "127.0.0.1", "::1", "fe80::1"):
        assert log.locate(ip) == PRIVATE
    assert lookups.asked == []
    assert log.locate("8.8.8.8") == "Zurich, Zurich, Switzerland" and lookups.asked == ["8.8.8.8"]


def test_a_missing_geoip_database_stops_the_server_starting(two: Deployment, tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="JEFF_GEOIP_DB"):
        two.app(access_log=tmp_path / "access.jsonl", geoip_db=tmp_path / "missing.mmdb")


# --- fix round 1 -----------------------------------------------------------------------------------------------------


def hub_token(private: Ed25519PrivateKey) -> dict[str, str]:
    return {"Authorization": f"Bearer {token(private, sub='customer', licences=sorted(ALL))}"}


def test_changing_adapters_and_metrics_need_owner_credentials_not_a_hub_token(two: Deployment,
                                                                             tmp_path: Path) -> None:
    private, public = hub_keys(tmp_path)
    users_file = tmp_path / "users.json"
    users.write_file(users_file, "s" * 64, {LOGIN: users.hash_password("right horse")})
    with two.app(api_key="the-key", hub_public_key=public, users_file=users_file) as http:
        customer = hub_token(private)
        for response in (http.put("/v1/adapters/served", json={"adapters": ["spam", "triage"]}, headers=customer),
                         http.post("/v1/adapters/reload", headers=customer),
                         http.get("/metrics", headers=customer)):
            assert_error(response, 403, "NotLicensed", "API key", "console")
        assert two.served() == [ALL, ALL]
        assert http.get("/v1/adapters", headers=customer).status_code == 200  # reading what is served is fine
        assert http.post("/v1/decide", json={"calls": [SPAM_CALL]}, headers=customer).status_code == 200
        session = http.post("/ui/login", json={"email": LOGIN, "password": "right horse"}).json()["token"]
        console = {"Authorization": f"Bearer {session}"}
        assert http.get("/metrics", headers=console).status_code == 200
        assert http.put("/v1/adapters/served", json={"adapters": ["spam", "triage"]},
                        headers=console).status_code == 200
        key = {"Authorization": "Bearer the-key"}
        assert http.put("/v1/adapters/served", json={"adapters": sorted(ALL)}, headers=key).status_code == 200


class SilentEngine:
    """An engine socket that says hello and answers the first `installed` and `health` (so a SocketEngine connects),
    then reads every request and never answers again."""

    def __init__(self, adapters: set[str]) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="jeff")) / "silent.sock"
        self.listener = socket_module.socket(socket_module.AF_UNIX)
        self.listener.bind(str(self.path))
        self.listener.listen()
        self.adapters = sorted(adapters)
        self.connections: list[socket_module.socket] = []
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self) -> None:
        connection, _ = self.listener.accept()
        self.connections.append(connection)
        reader = connection.makefile("rb")

        def read(n: int) -> bytes:
            data = reader.read(n)
            if len(data) < n:
                raise EOFError
            return data

        try:
            read_frame(read)
            connection.sendall(encode(hello(0)))
            request = read_frame(read)
            connection.sendall(encode(ok(request["id"], {"adapters": self.adapters, "adapters_version": 0})))
            request = read_frame(read)  # the `health` a SocketEngine reads its base format from when it connects
            connection.sendall(encode(ok(request["id"], {
                "status": "ready", "backend": "fake", "adapters": self.adapters, "adapters_version": 0,
                "base": {"id": "jeff-base", "version": "v1.3", "checkpoint": "fake", "max_options": 32,
                         "format": "bf16"}})))
            while True:
                read_frame(read)  # never answered
        except (EngineError, EOFError, OSError):
            return

    def close(self) -> None:
        for connection in self.connections:
            connection.close()
        self.listener.close()


def test_a_connected_engine_that_never_answers_is_down_within_the_health_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    assert operations.HEALTH_TIMEOUT_S == 2.0
    monkeypatch.setattr(operations, "HEALTH_TIMEOUT_S", 0.3)
    deployment = Deployment(1, installed=LEAVES)
    silent = SilentEngine(LEAVES)
    try:
        engines = [SocketEngine(deployment.servers[0].path, timeout_s=30),
                   SocketEngine(silent.path, timeout_s=30)]
        deployment.sockets.extend(engines)
        settings = ServerSettings(engine_sockets=(deployment.servers[0].path, silent.path), model_name="jeff-v1.3",
                                  catalogue=ROUTERS)
        with TestClient(create_app(settings, engine=EngineRouter(engines))) as http:
            started = time.monotonic()
            body = http.get("/health").json()
            assert time.monotonic() - started < 5  # not the 30 s prediction timeout
            assert body["status"] == "degraded"
            down = body["engines"][1]
            assert down["status"] == "down" and str(silent.path) in down["cause"] and "0.3s" in down["cause"]
            started = time.monotonic()
            families = parse_metrics(http.get("/metrics").text)
            assert time.monotonic() - started < 5
            assert {labels["socket"]: value for labels, value in families["jeff_engine_up"]} == {
                str(deployment.servers[0].path): 1, str(silent.path): 0}
            started = time.monotonic()
            assert_error(http.get("/v1/adapters"), 500, "EngineError", str(silent.path))
            assert time.monotonic() - started < 5
    finally:
        deployment.close()
        silent.close()


class BreaksOn(FakeEngine):
    """Answers like the others, except that a prediction for `adapter` returns nothing at all, which kills the
    engine's model thread: the engine then reports itself broken."""

    def __init__(self, adapter: str) -> None:
        super().__init__(answers, set(ALL))
        self.adapter = adapter

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        if adapter_id == self.adapter:
            return None  # type: ignore[return-value]
        return super().predict(adapter_id, rows)


def test_an_engine_reporting_itself_broken(two: Deployment) -> None:
    broken = Running(BreaksOn("jeff.safety.spam"), fixed=AVAILABLE)
    healthy = two.servers[0]
    try:
        direct = SocketEngine(broken.path, timeout_s=5)
        with pytest.raises(EngineError):
            direct.predict("jeff.safety.spam", [{"state": {"text": "x"}, "question": {"type": "noul",
                                                                                     "instructions": "?"}}])
        direct.close()
        engines = [SocketEngine(healthy.path, timeout_s=5), SocketEngine(broken.path, timeout_s=5)]
        two.sockets.extend(engines)
        settings = ServerSettings(engine_sockets=(healthy.path, broken.path), model_name="jeff-v1.3",
                                  catalogue=ROUTERS)
        with TestClient(create_app(settings, engine=EngineRouter(engines))) as http:
            body = http.get("/health").json()
        assert body["status"] == "degraded"
        entry = body["engines"][1]
        assert entry["status"] == "broken" and "GPU thread failed" in entry["cause"]
        assert entry["backend"] == "fake"
    finally:
        broken.close()


def test_public_unsigned_health_stays_status_only_when_degraded(two: Deployment) -> None:
    with two.app(public=True, api_key="the-key", rate_limit=(100.0, 10)) as http:
        two.servers[1].close()
        assert http.get("/health").json() == {"status": "degraded"}


def test_public_unsigned_health_is_cached_for_a_second(two: Deployment, monkeypatch: pytest.MonkeyPatch) -> None:
    asked: list[Path] = []
    original = SocketEngine.health

    def counting(self: SocketEngine, timeout_s: float | None = None) -> dict[str, Any]:
        asked.append(self.path)
        return original(self, timeout_s)

    monkeypatch.setattr(SocketEngine, "health", counting)
    clock = [1000.0]
    monkeypatch.setattr(operations, "monotonic", lambda: clock[0])
    with two.app(public=True, api_key="the-key", rate_limit=(100.0, 10)) as http:
        for _ in range(5):
            assert http.get("/health").json() == {"status": "ready"}
        assert len(asked) == 2  # one question to each engine for five unsigned requests
        http.get("/health", headers={"Authorization": "Bearer the-key"})
        assert len(asked) == 4  # a signed-in caller always gets a fresh report
        clock[0] += 0.5
        http.get("/health")
        assert len(asked) == 4
        clock[0] += 0.6
        http.get("/health")
        assert len(asked) == 6


def test_prometheus_labels_are_escaped(two: Deployment) -> None:
    counts = operations.RequestCounts()
    counts.add('/a"b\\c\nd', 200)
    engine = SocketEngine(two.servers[0].path, timeout_s=5)
    two.sockets.append(engine)
    text = operations.metrics_text([engine], counts)
    families = parse_metrics(text)
    (labels, value), = families["jeff_http_requests_total"]
    assert labels["route"] == '/a\\"b\\\\c\\nd' and value == 1
    assert 'route="/a\\"b\\\\c\\nd"' in text


def test_an_access_log_that_cannot_be_opened_stops_the_server_starting(two: Deployment, tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="JEFF_ACCESS_LOG"):
        two.app(access_log=tmp_path / "no-such-folder" / "access.jsonl")


class DivergesAfterChange(EngineRouter):
    """Reports the engines disagreeing when asked after a change, as when another API process changed one engine
    in between."""

    def installed(self, refresh: bool = False) -> set[str]:
        if refresh:
            raise EngineError("The engines disagree on what is installed: e0=[a], e1=[b]")
        return super().installed(refresh)


def test_engines_diverging_during_a_change_say_it_was_applied_and_how_to_repair(two: Deployment) -> None:
    engines = [SocketEngine(server.path, timeout_s=5) for server in two.servers]
    two.sockets.extend(engines)
    settings = ServerSettings(engine_sockets=tuple(server.path for server in two.servers), model_name="jeff-v1.3",
                              catalogue=ROUTERS)
    with TestClient(create_app(settings, engine=DivergesAfterChange(engines))) as http:
        assert_error(http.put("/v1/adapters/served", json={"adapters": sorted(LEAVES)}), 500, "EngineError",
                     "serve was applied", "disagree", "repeating PUT /v1/adapters/served")


# --- engine connections the app opens are the app's to close -------------------------------------------------------

class RecordedSocketEngine(SocketEngine):
    """A SocketEngine that records every one opened and whether it was closed."""

    opened: list["RecordedSocketEngine"] = []

    def __init__(self, path: Path | str, *, timeout_s: float) -> None:
        super().__init__(path, timeout_s=timeout_s)
        self.closed = False
        RecordedSocketEngine.opened.append(self)

    def close(self) -> None:
        self.closed = True
        super().close()


@pytest.fixture
def recorded(monkeypatch: pytest.MonkeyPatch) -> list[RecordedSocketEngine]:
    RecordedSocketEngine.opened = []
    monkeypatch.setattr("jeff.socket_engine.SocketEngine", RecordedSocketEngine)
    return RecordedSocketEngine.opened


def own_engines_settings(sockets: list[Path], **options: Any) -> ServerSettings:
    return ServerSettings(engine_sockets=tuple(sockets), model_name="jeff-v1.3", catalogue=ROUTERS, **options)


def test_the_app_closes_the_engine_connections_it_opened_on_shutdown(two: Deployment,
                                                                     recorded: list[RecordedSocketEngine]) -> None:
    with TestClient(create_app(own_engines_settings([server.path for server in two.servers]))) as http:
        assert http.get("/v1/adapters").status_code == 200
        assert len(recorded) == 2 and not any(engine.closed for engine in recorded)
    assert all(engine.closed for engine in recorded)


def test_a_socket_failing_at_startup_closes_the_ones_already_opened(two: Deployment, tmp_path: Path,
                                                                    recorded: list[RecordedSocketEngine]) -> None:
    sockets = [two.servers[0].path, tmp_path / "no-engine.sock"]
    with pytest.raises(EngineError), TestClient(create_app(own_engines_settings(sockets))):
        pass
    assert len(recorded) == 1 and recorded[0].closed


def test_a_later_startup_failure_closes_every_engine_opened(two: Deployment, tmp_path: Path,
                                                            recorded: list[RecordedSocketEngine]) -> None:
    settings = own_engines_settings([server.path for server in two.servers], graphs=tmp_path / "no-such-folder")
    with pytest.raises(GraphError, match="JEFF_GRAPHS"), TestClient(create_app(settings)):
        pass
    assert len(recorded) == 2 and all(engine.closed for engine in recorded)


def test_an_engine_passed_in_is_not_closed_by_the_app(two: Deployment) -> None:
    with two.app() as http:
        assert http.get("/v1/adapters").status_code == 200
    # The SocketEngines given to the app still answer after it has shut down: they are the caller's to close.
    assert all(engine.installed(refresh=True) == ALL for engine in two.sockets)


# --- the operations endpoints keep their own threads ----------------------------------------------------------------

class SlowRouter(EngineRouter):
    """An EngineRouter whose predict takes PREDICT_S, as a busy engine does; it counts the predicts running."""

    PREDICT_S = 2.0

    def __init__(self, engines: list[SocketEngine]) -> None:
        super().__init__(engines)
        self.running = 0
        self.lock = threading.Lock()

    def predict(self, adapter_id: str, rows: list[dict[str, Any]], deadline_ms: float | None = None) -> EngineResult:
        with self.lock:
            self.running += 1
        time.sleep(self.PREDICT_S)
        return super().predict(adapter_id, rows, deadline_ms)


def test_many_slow_decides_do_not_hold_up_health_metrics_or_the_adapter_list(two: Deployment) -> None:
    """45 slow /v1/decide requests (more than the decide pool's threads) are in flight; GET /health, GET /metrics
    and GET /v1/adapters still answer at once, on the operations endpoints' own threads."""
    engines = [SocketEngine(server.path, timeout_s=10) for server in two.servers]
    two.sockets.extend(engines)
    slow = SlowRouter(engines)
    settings = ServerSettings(engine_sockets=tuple(server.path for server in two.servers), model_name="jeff-v1.3",
                              catalogue=ROUTERS)
    with TestClient(create_app(settings, engine=slow)) as http:
        statuses: list[int] = []
        decides = [threading.Thread(target=lambda: statuses.append(
            http.post("/v1/decide", json={"calls": [SPAM_CALL]}).status_code)) for _ in range(45)]
        for thread in decides:
            thread.start()
        deadline = time.monotonic() + 5
        while slow.running < app_decide_threads() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert slow.running == app_decide_threads()  # every decide thread is taken by a slow predict
        for path in ("/health", "/metrics", "/v1/adapters"):
            started = time.monotonic()
            assert http.get(path).status_code == 200, path
            assert time.monotonic() - started < 0.5, path
        for thread in decides:
            thread.join()
    assert statuses == [200] * 45


def app_decide_threads() -> int:
    from jeff_server.app import DECIDE_THREADS
    return DECIDE_THREADS


# --- request body size ----------------------------------------------------------------------------------------------

BODY_ROUTES = [("POST", "/v1/decide"), ("POST", "/v1/systemone"), ("PUT", "/v1/adapters/served")]


def test_the_body_cap_fits_the_largest_systemone_request() -> None:
    """Four images at the schema's longest data URL (12,000,000 characters each, room for an 8 MB image in
    base64) must fit, with room left for the state and questions."""
    from jeff_server.decide import MAX_BODY_BYTES
    assert MAX_BODY_BYTES >= 4 * 12_000_000 + 4_000_000


@pytest.mark.parametrize("method, path", BODY_ROUTES)
@pytest.mark.parametrize("chunked", [False, True], ids=["content-length", "chunked"])
def test_a_body_over_the_cap_is_422_invalid_request(two: Deployment, method: str, path: str, chunked: bool,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("jeff_server.decide.MAX_BODY_BYTES", 1000)
    over = json.dumps({"adapters": ["spam"], "padding": "x" * 1000}).encode()
    content: Any = iter([over[:500], over[500:]]) if chunked else over
    with two.app() as http:
        response = http.request(method, path, content=content, headers={"content-type": "application/json"})
        at_cap = http.put("/v1/adapters/served", content=json.dumps({"adapters": sorted(ALL)}).encode(),
                          headers={"content-type": "application/json"})
    assert_error(response, 422, "InvalidRequest", "larger than", "1000 bytes")
    assert at_cap.status_code == 200, at_cap.text
    assert two.served() == [ALL, ALL]


def test_a_malformed_content_length_is_422(two: Deployment) -> None:
    with two.app() as http:
        response = http.post("/v1/decide", content=b"{}", headers={"content-type": "application/json",
                                                                   "content-length": "lots"})
    assert_error(response, 422, "InvalidRequest", "Content-Length", "'lots'")
