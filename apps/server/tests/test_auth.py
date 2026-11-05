"""Credentials, licences and per-caller rate limits on /v1/*: the API key, console sessions (test_users.py) and
hub-signed Ed25519 tokens; a proprietary adapter's function answers only for a caller holding its licence, checked
before the engine sees a row for it (a call, the leaf a dispatch reaches, an escalation step, a graph node); each
caller has its own token bucket and in-flight limit; `jeff-server token keygen|issue` makes development tokens."""

import base64
import hashlib
import hmac
import json
import stat
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import jwt
import pytest
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.asymmetric.rsa import generate_private_key
from fastapi import Depends
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from jeff.errors import EngineError, InvalidRequest, RateLimited
from jeff.testing import FakeEngine

from jeff_server import auth
from jeff_server.app import create_app
from jeff_server.cli import main as cli
from jeff_server.settings import ServerSettings

from server_testing import proprietary, token

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "python" / "tests" / "fixtures"
CATALOGUE = FIXTURES / "catalogue"
ROUTERS = FIXTURES / "routers"
GRAPH = FIXTURES / "graphs" / "support_intake.yaml"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
PARAMS: dict[str, Any] = {"company": "A furniture shop.", "channel": "email", "teams": {"deliveries": "D", "refunds": "R"}}
FIXTURE_ADAPTERS = {"jeff.safety.spam", "jeff.support.triage"}
ROUTED = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
UNSURE = [0.0, 0.5, 0.5, 0.0, 0.0]  # below triage.urgency's calibration threshold 0.6
SPAM_CALL = {"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "email"}}
URGENCY_CALL = {"function": "triage.urgency", "input": "It is on fire", "context": CONTEXT}


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return UNSURE if adapter == "jeff.support.triage" and "unsure" in json.dumps(row["state"]) \
            else [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def fake(installed: set[str] = FIXTURE_ADAPTERS,
         answer: Callable[[str, dict[str, Any]], list[float]] = answers) -> FakeEngine:
    return FakeEngine(answer, set(installed))


def settings(**options: Any) -> ServerSettings:
    return ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3", **options)


def client(engine: FakeEngine | None = None, **options: Any) -> TestClient:
    options.setdefault("catalogue", CATALOGUE)
    return TestClient(create_app(settings(**options), engine=engine if engine is not None else fake()))


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def assert_error(response: Any, status: int, error_type: str, *named: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    assert response.headers["x-request-id"]
    error = response.json()["error"]
    assert error["type"] == error_type, error
    for text in named:
        assert text in error["message"], error["message"]
    return dict(error)


def decide_calls(http: TestClient, headers: dict[str, str], *calls: dict[str, Any], **extra: Any) -> Any:
    return http.post("/v1/decide", json={"calls": list(calls), **extra}, headers=headers)


# --- credentials ---------------------------------------------------------------------------------------------------

def test_with_no_credential_configured_the_api_is_open() -> None:
    with client() as http:
        assert http.get("/v1/graphs").status_code == 200
        assert decide_calls(http, {}, SPAM_CALL).status_code == 200


def test_the_api_key_opens_the_api_and_anything_else_is_401_asking_for_a_bearer_token() -> None:
    with client(api_key="the-key") as http:
        assert http.get("/v1/graphs", headers=bearer("the-key")).status_code == 200
        for headers in ({}, bearer("the-kez"), bearer(""), {"Authorization": "the-key"},
                        {"Authorization": "Basic the-key"}):
            response = http.get("/v1/graphs", headers=headers)
            assert_error(response, 401, "Unauthorised")
            assert response.headers["WWW-Authenticate"] == "Bearer"


def test_the_bearer_scheme_is_case_insensitive() -> None:
    with client(api_key="the-key") as http:
        assert http.get("/v1/graphs", headers={"Authorization": "bearer the-key"}).status_code == 200


def api_routes(routes: Any) -> Iterator[APIRoute]:
    """Every APIRoute, through included routers (FastAPI 0.141 keeps an included router as one route holding the
    original router, rather than copying its routes into the app)."""
    for route in routes:
        if hasattr(route, "original_router"):
            yield from api_routes(route.original_router.routes)
        elif isinstance(route, APIRoute):
            yield route


def test_every_v1_route_needs_credentials() -> None:
    """Guards the routes later tasks add: each /v1 route carries the authentication dependency."""
    app = create_app(settings(api_key="the-key", catalogue=CATALOGUE), engine=fake())
    v1 = [route for route in api_routes(app.routes) if route.path.startswith("/v1/")]
    assert v1
    with TestClient(app) as http:
        for route in v1:
            assert route.methods
            for method in route.methods:
                response = http.request(method, route.path.replace("{", "").replace("}", ""))
                assert_error(response, 401, "Unauthorised")


def test_a_hub_token_opens_the_api(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    with client(hub_public_key=public) as http:
        assert http.get("/v1/graphs", headers=bearer(token(private))).status_code == 200
        assert decide_calls(http, bearer(token(private)), SPAM_CALL).status_code == 200


def test_a_hub_caller_carries_its_tokens_expiry(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    """The channel closes a connection whose token has expired, so a hub caller carries the token's exp."""
    private, public = keys
    issued = token(private, ttl_s=120)
    caller = auth.Authenticator(None, None, auth.load_hub_public_key(public), None).caller(f"Bearer {issued}", None)
    assert caller.expires == jwt.decode(issued, options={"verify_signature": False})["exp"]


def hs256(public: Path, payload: dict[str, Any]) -> str:
    """An HS256 token whose HMAC secret is the hub's public key file: the algorithm-confusion attack."""
    def part(value: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()
    signing_input = f"{part({'alg': 'HS256', 'typ': 'JWT'})}.{part(payload)}"
    signature = hmac.new(public.read_bytes(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"


def tampered(value: str) -> str:
    """The token with the first character of its signature changed (the last base64 character can carry ignored
    padding bits, so changing it may not change the signature)."""
    head, payload, signature = value.split(".")
    return f"{head}.{payload}.{'B' if signature[0] == 'A' else 'A'}{signature[1:]}"


BAD_TOKENS: dict[str, Callable[[Ed25519PrivateKey, Path], str]] = {
    "another key": lambda private, public: token(Ed25519PrivateKey.generate()),
    "expired": lambda private, public: token(private, iat=int(time.time()) - 700, exp=int(time.time()) - 100),
    "longer than an hour": lambda private, public: token(private, ttl_s=3601),
    "unsigned": lambda private, public: jwt.encode(
        {"sub": "alice", "licences": [], "iat": int(time.time()), "exp": int(time.time()) + 60}, None,  # type: ignore[arg-type]
        algorithm="none"),
    "HS256": lambda private, public: hs256(public, {"sub": "alice", "licences": [], "iat": int(time.time()),
                                                    "exp": int(time.time()) + 60}),
    "no exp": lambda private, public: token(private, exp=None),
    "no iat": lambda private, public: token(private, iat=None),
    "no sub": lambda private, public: jwt.encode(
        {"licences": [], "iat": int(time.time()), "exp": int(time.time()) + 60}, private, algorithm="EdDSA"),
    "empty sub": lambda private, public: token(private, sub=""),
    "sub not a string": lambda private, public: jwt.encode(
        {"sub": 7, "licences": [], "iat": int(time.time()), "exp": int(time.time()) + 60}, private,
        algorithm="EdDSA"),
    "no licences": lambda private, public: jwt.encode(
        {"sub": "alice", "iat": int(time.time()), "exp": int(time.time()) + 60}, private, algorithm="EdDSA"),
    "licences not a list": lambda private, public: token(private, licences="jeff.support.triage"),  # type: ignore[arg-type]
    "licences not strings": lambda private, public: token(private, licences=[1]),  # type: ignore[list-item]
    "issued in the future": lambda private, public: token(private, iat=int(time.time()) + 3000,
                                                          exp=int(time.time()) + 3600),
    "iat and exp as strings": lambda private, public: token(private, iat=str(int(time.time()) - 10**6),
                                                            exp=str(int(time.time()) + 60)),
    "garbage": lambda private, public: "a.b.c",
    "tampered": lambda private, public: tampered(token(private)),
}


@pytest.mark.parametrize("kind", sorted(BAD_TOKENS))
def test_a_bad_hub_token_is_401(keys: tuple[Ed25519PrivateKey, Path], kind: str) -> None:
    private, public = keys
    with client(hub_public_key=public) as http:
        response = http.get("/v1/graphs", headers=bearer(BAD_TOKENS[kind](private, public)))
    assert_error(response, 401, "Unauthorised")
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_a_hub_token_is_refused_when_the_server_has_no_hub_key(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, _ = keys
    with client(api_key="the-key") as http:
        assert_error(http.get("/v1/graphs", headers=bearer(token(private))), 401, "Unauthorised", "JEFF_HUB_PUBLIC_KEY")


def test_a_missing_or_wrong_hub_key_stops_the_server_starting(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="JEFF_HUB_PUBLIC_KEY"):
        client(hub_public_key=tmp_path / "missing.pem")
    (tmp_path / "not.pem").write_text("not a key")
    with pytest.raises(InvalidRequest, match="JEFF_HUB_PUBLIC_KEY"):
        client(hub_public_key=tmp_path / "not.pem")
    rsa = generate_private_key(public_exponent=65537, key_size=2048).public_key()
    (tmp_path / "rsa.pem").write_bytes(rsa.public_bytes(serialization.Encoding.PEM,
                                                        serialization.PublicFormat.SubjectPublicKeyInfo))
    with pytest.raises(InvalidRequest, match="Ed25519"):
        client(hub_public_key=tmp_path / "rsa.pem")


def test_the_authenticated_dependency_guards_any_route() -> None:
    """Task 8 serves /openapi.json behind this same dependency."""
    app = create_app(settings(api_key="the-key", catalogue=CATALOGUE), engine=fake())

    @app.get("/guarded")
    async def _guarded(caller: auth.Caller = Depends(auth.authenticated)) -> dict[str, str]:
        return {"subject": caller.subject}

    with TestClient(app) as http:
        assert_error(http.get("/guarded"), 401, "Unauthorised")
        assert http.get("/guarded", headers=bearer("the-key")).json() == {"subject": "api-key"}


def test_openapi_json_is_open_when_no_credential_is_configured() -> None:
    with client() as http:  # settings() has no api_key, hub_public_key or users_file
        response = http.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["openapi"] == "3.1.0"


def test_openapi_json_needs_credentials_once_any_are_configured() -> None:
    with client(api_key="the-key") as http:
        assert_error(http.get("/openapi.json"), 401, "Unauthorised")
        response = http.get("/openapi.json", headers=bearer("the-key"))
    assert response.status_code == 200


def test_openapi_json_on_a_public_server_needs_a_signed_in_caller() -> None:
    """A public server always has a credential configured (ServerSettings refuses one without), so authenticated()
    already requires sign-in here -- the same rule as any /v1 route, which is what the brief asks for."""
    with client(public=True, api_key="the-key", rate_limit=(100.0, 10)) as http:
        assert_error(http.get("/openapi.json"), 401, "Unauthorised")
        response = http.get("/openapi.json", headers=bearer("the-key"))
    assert response.status_code == 200


# --- licences ------------------------------------------------------------------------------------------------------

def adapters_seen(engine: FakeEngine) -> set[str]:
    return {adapter for adapter, _ in engine.calls}


def test_a_call_needs_the_licence_of_a_proprietary_adapter(keys: tuple[Ed25519PrivateKey, Path],
                                                           tmp_path: Path) -> None:
    private, public = keys
    engine = fake()
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.support.triage")
    with client(engine, catalogue=catalogue, hub_public_key=public, api_key="the-key") as http:
        refused = decide_calls(http, bearer(token(private)), SPAM_CALL, URGENCY_CALL)
        assert_error(refused, 403, "NotLicensed", "jeff.support.triage")
        assert engine.calls == []  # refused before planning: not even the licensed call ran
        licensed = token(private, licences=["jeff.support.triage"])
        assert decide_calls(http, bearer(licensed), SPAM_CALL, URGENCY_CALL).status_code == 200
        assert decide_calls(http, bearer("the-key"), URGENCY_CALL).status_code == 200  # the owner holds every licence


def test_open_adapters_need_no_licence(keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    private, public = keys
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.support.triage")
    with client(fake(), catalogue=catalogue, hub_public_key=public) as http:
        assert decide_calls(http, bearer(token(private)), SPAM_CALL).status_code == 200


def test_a_dispatched_leaf_needs_its_licence_checked_after_routing_before_it_runs(
        keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    """Review Focus 2: the leaf is known only after the routers have chosen it."""
    private, public = keys
    engine = fake(ROUTED)
    catalogue = proprietary(ROUTERS, tmp_path, "jeff.support.triage")
    request = {"dispatch": {"input": "My wardrobe is broken", "interface": "rate", "context": CONTEXT}}
    with client(engine, catalogue=catalogue, hub_public_key=public) as http:
        refused = http.post("/v1/decide", json=request, headers=bearer(token(private)))
        assert_error(refused, 403, "NotLicensed", "jeff.support.triage")
        assert adapters_seen(engine) == {"jeff.core.router", "jeff.support.router"}
        licensed = http.post("/v1/decide", json=request,
                             headers=bearer(token(private, licences=["jeff.support.triage"])))
    assert licensed.status_code == 200, licensed.text
    assert licensed.json()["decision"]["function"] == "jeff.support.triage.urgency"


def test_an_escalation_step_needs_its_licence(keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    private, public = keys
    installed = FIXTURE_ADAPTERS | {"jeff.support.backup"}
    unsure = {**URGENCY_CALL, "input": "unsure"}
    policy = {"escalate": ["backup.urgency"]}
    engine = fake(installed)
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.support.backup", backup=True)
    with client(engine, catalogue=catalogue, hub_public_key=public) as http:
        # a call's escalation steps are checked with the call, before anything is decided
        refused = decide_calls(http, bearer(token(private)), unsure, on_low_confidence=policy)
        assert_error(refused, 403, "NotLicensed", "jeff.support.backup")
        assert engine.calls == []
        licensed = decide_calls(http, bearer(token(private, licences=["jeff.support.backup"])), unsure,
                                on_low_confidence=policy)
    assert licensed.status_code == 200, licensed.text
    assert licensed.json()["decisions"][0]["decided_by"] == "jeff.support.backup.urgency@1.3.0"


def test_an_escalation_step_of_a_dispatch_needs_its_licence_before_it_runs(
        keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    private, public = keys
    engine = fake(ROUTED | {"jeff.support.backup"})
    catalogue = proprietary(ROUTERS, tmp_path, "jeff.support.backup", backup=True)
    request = {"dispatch": {"input": "unsure", "interface": "rate", "context": CONTEXT},
               "on_low_confidence": {"escalate": ["backup.urgency"]}}
    with client(engine, catalogue=catalogue, hub_public_key=public) as http:
        refused = http.post("/v1/decide", json=request, headers=bearer(token(private)))
    assert_error(refused, 403, "NotLicensed", "jeff.support.backup")
    assert engine.calls == []  # the policy's steps are checked before routing starts


def test_an_escalation_step_of_a_graph_node_needs_its_licence_before_the_graph_runs(
        keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    private, public = keys
    engine = fake(FIXTURE_ADAPTERS | {"jeff.support.backup"})
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.support.backup", backup=True)
    graph = yaml.safe_load(GRAPH.read_text())
    del graph["nodes"]["ticket"]
    graph["outputs"] = ["kind", "team", "urgency"]
    request = {"graph": {"inline": graph}, "input": "My wardrobe is broken", "params": PARAMS,
               "on_low_confidence_by_node": {"urgency": {"escalate": ["backup.urgency"]}}}
    with client(engine, catalogue=catalogue, hub_public_key=public) as http:
        refused = http.post("/v1/decide", json=request, headers=bearer(token(private)))
        assert_error(refused, 403, "NotLicensed", "jeff.support.backup")
        assert engine.calls == []
        licensed = http.post("/v1/decide", json=request,
                             headers=bearer(token(private, licences=["jeff.support.backup"])))
    assert licensed.status_code == 200, licensed.text


def test_a_graph_node_needs_its_licence(keys: tuple[Ed25519PrivateKey, Path], tmp_path: Path) -> None:
    private, public = keys
    engine = fake()
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.support.triage")
    graph = yaml.safe_load(GRAPH.read_text())
    del graph["nodes"]["ticket"]
    graph["outputs"] = ["kind", "team", "urgency"]
    request = {"graph": {"inline": graph}, "input": "My wardrobe is broken", "params": PARAMS}
    with client(engine, catalogue=catalogue, hub_public_key=public) as http:
        refused = http.post("/v1/decide", json=request, headers=bearer(token(private)))
        assert_error(refused, 403, "NotLicensed", "jeff.support.triage")
        assert engine.calls == []
        licensed = http.post("/v1/decide", json=request,
                             headers=bearer(token(private, licences=["jeff.support.triage"])))
    assert licensed.status_code == 200, licensed.text


# --- rate limits ---------------------------------------------------------------------------------------------------

def test_a_burst_from_one_caller_is_429_while_another_caller_is_served(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    """Review Focus 4, in public mode: one request per second, burst one."""
    private, public = keys
    alice, bob = bearer(token(private, sub="alice")), bearer(token(private, sub="bob"))
    with client(public=True, hub_public_key=public, rate_limit=(1.0, 5)) as http:
        assert http.get("/v1/graphs", headers=alice).status_code == 200
        refused = http.get("/v1/graphs", headers=alice)
        assert_error(refused, 429, "RateLimited", "alice")
        assert refused.headers["Retry-After"] == "1"
        assert http.get("/v1/graphs", headers=bob).status_code == 200


def test_a_caller_over_its_concurrent_limit_is_429_and_the_slot_is_freed_when_its_request_ends(
        keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    alice, bob = bearer(token(private, sub="alice")), bearer(token(private, sub="bob"))
    entered, release = threading.Event(), threading.Event()

    def slow(adapter: str, row: dict[str, Any]) -> list[float]:
        if "slow" in json.dumps(row["state"]):
            entered.set()
            assert release.wait(10)
        return answers(adapter, row)

    with client(fake(answer=slow), hub_public_key=public, rate_limit=(1000.0, 1)) as http:
        first: dict[str, Any] = {}
        worker = threading.Thread(target=lambda: first.setdefault(
            "response", decide_calls(http, alice, {**SPAM_CALL, "input": "slow"})))
        worker.start()
        assert entered.wait(10)
        assert_error(decide_calls(http, alice, SPAM_CALL), 429, "RateLimited", "in flight")
        assert decide_calls(http, bob, SPAM_CALL).status_code == 200
        release.set()
        worker.join(10)
        assert first["response"].status_code == 200
        assert decide_calls(http, alice, SPAM_CALL).status_code == 200


def test_the_in_flight_slot_is_freed_when_a_request_fails(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    alice = bearer(token(private, sub="alice"))

    def broken(adapter: str, row: dict[str, Any]) -> list[float]:
        raise EngineError("the engine broke")

    with client(fake(answer=broken), hub_public_key=public, rate_limit=(1000.0, 1)) as http:
        for _ in range(3):
            assert_error(decide_calls(http, alice, SPAM_CALL), 500, "EngineError")
            assert_error(decide_calls(http, alice, {**SPAM_CALL, "function": "nope.nothing"}), 404,
                         "UnknownFunction")
            assert_error(http.post("/v1/decide", content=b"{", headers=alice), 422, "InvalidRequest")
    assert auth_limiter_idle(http)


def auth_limiter_idle(http: TestClient) -> bool:
    limiter: auth.RateLimiter = http.app.state.limiter  # type: ignore[attr-defined]
    return all(count == 0 for count in limiter.in_flight.values())


def test_the_api_key_and_console_logins_are_separate_subjects() -> None:
    assert auth.Caller("api-key", "api-key", None).rate_key != auth.Caller("hub", "api-key", frozenset()).rate_key


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def test_the_bucket_refills_at_the_rate_and_holds_at_most_a_burst_of_rps() -> None:
    clock = Clock()
    limiter = auth.RateLimiter(2.0, 100, clock=clock)
    for _ in range(2):
        limiter.acquire("a")
        limiter.release("a")
    with pytest.raises(RateLimited) as error:
        limiter.acquire("a")
    assert error.value.retry_after_s == pytest.approx(0.5)
    clock.now += 10  # long idle: the bucket refills only to its burst, 2
    for _ in range(2):
        limiter.acquire("a")
        limiter.release("a")
    with pytest.raises(RateLimited):
        limiter.acquire("a")
    clock.now += 0.5
    limiter.acquire("a")


@pytest.mark.parametrize("rps,concurrent", [(float("nan"), 1), (float("inf"), 1), (0.0, 1), (-1.0, 1), (1.0, 0),
                                            (1.0, -1), (1.0, 1.5), (1.0, True)])
def test_the_limiter_refuses_limits_that_are_not_finite_and_positive(rps: float, concurrent: Any) -> None:
    with pytest.raises(InvalidRequest, match="RateLimiter"):
        auth.RateLimiter(rps, concurrent)


def test_idle_buckets_are_swept_at_most_once_a_sweep_interval() -> None:
    clock = Clock()
    limiter = auth.RateLimiter(1.0, 1, clock=clock)
    for index in range(auth.MAX_IDLE_BUCKETS + 1):
        limiter.acquire(f"hub:{index}")
        limiter.release(f"hub:{index}")
    clock.now += 5  # every bucket refilled: all idle
    limiter.acquire("hub:sweeper")  # over the cap: sweeps once
    limiter.release("hub:sweeper")
    assert len(limiter.buckets) == 1
    for index in range(auth.MAX_IDLE_BUCKETS + 1):
        limiter.acquire(f"hub:again-{index}")
        limiter.release(f"hub:again-{index}")
    clock.now += 5
    limiter.acquire("hub:too-soon")  # within the interval of the last sweep: no scan
    limiter.release("hub:too-soon")
    assert len(limiter.buckets) == auth.MAX_IDLE_BUCKETS + 3
    clock.now += auth.SWEEP_INTERVAL_S
    limiter.acquire("hub:later")
    limiter.release("hub:later")
    assert len(limiter.buckets) == 1


def test_a_rate_below_one_per_second_still_allows_one_request() -> None:
    clock = Clock()
    limiter = auth.RateLimiter(0.5, 1, clock=clock)
    limiter.acquire("a")
    limiter.release("a")
    with pytest.raises(RateLimited) as error:
        limiter.acquire("a")
    assert error.value.retry_after_s == pytest.approx(2.0)
    clock.now += 2
    limiter.acquire("a")


def test_the_limiter_is_thread_safe() -> None:
    limiter = auth.RateLimiter(1_000_000.0, 8, clock=Clock())
    admitted, refused = [0], [0]
    lock = threading.Lock()

    def work() -> None:
        for _ in range(500):
            try:
                limiter.acquire("a")
            except RateLimited:
                with lock:
                    refused[0] += 1
                continue
            with lock:
                admitted[0] += 1
            assert limiter.in_flight["a"] <= 8
            limiter.release("a")

    threads = [threading.Thread(target=work) for _ in range(16)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert admitted[0] + refused[0] == 16 * 500
    assert limiter.in_flight.get("a", 0) == 0


def test_a_public_server_needs_credentials_on_every_v1_request(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    _, public = keys
    env = {"JEFF_ENGINE_SOCKETS": "/tmp/engine-0.sock", "JEFF_MODEL_NAME": "jeff-v1.3", "JEFF_PUBLIC": "1",
           "JEFF_RATE_LIMIT": "5/2", "JEFF_HUB_PUBLIC_KEY": str(public), "JEFF_CATALOGUE": str(CATALOGUE)}
    with TestClient(create_app(ServerSettings.from_env(env), engine=fake())) as http:
        assert_error(http.get("/v1/graphs"), 401, "Unauthorised")
    with pytest.raises(InvalidRequest, match="must not be open"):
        ServerSettings.from_env({key: value for key, value in env.items() if key != "JEFF_HUB_PUBLIC_KEY"})
    with pytest.raises(InvalidRequest, match="must not be unlimited"):
        ServerSettings.from_env({key: value for key, value in env.items() if key != "JEFF_RATE_LIMIT"})


# --- jeff-server token ---------------------------------------------------------------------------------------------

@pytest.fixture
def keypair(tmp_path: Path) -> Iterator[Path]:
    cli(["token", "keygen", "--out", str(tmp_path / "keys")])
    yield tmp_path / "keys"


def test_keygen_writes_an_ed25519_key_pair_with_an_owner_only_private_key(keypair: Path) -> None:
    private = serialization.load_pem_private_key((keypair / "private.pem").read_bytes(), password=None)
    assert isinstance(private, Ed25519PrivateKey)
    assert stat.S_IMODE((keypair / "private.pem").stat().st_mode) == 0o600
    public = serialization.load_pem_public_key((keypair / "public.pem").read_bytes())
    assert public.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw) == \
        private.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def test_keygen_will_not_overwrite_a_key(keypair: Path) -> None:
    with pytest.raises(SystemExit, match="already"):
        cli(["token", "keygen", "--out", str(keypair)])


def test_an_issued_token_opens_the_api_with_its_licences(keypair: Path, tmp_path: Path,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    cli(["token", "issue", "--key", str(keypair / "private.pem"), "--sub", "alice", "--licence",
         "jeff.support.triage", "--licence", "jeff.support.backup", "--ttl-s", "600"])
    issued = capsys.readouterr().out.strip()
    claims = jwt.decode(issued, options={"verify_signature": False})
    assert claims["sub"] == "alice" and claims["licences"] == ["jeff.support.triage", "jeff.support.backup"]
    assert claims["exp"] - claims["iat"] == 600
    engine = fake()
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.support.triage")
    with client(engine, catalogue=catalogue, hub_public_key=keypair / "public.pem") as http:
        assert decide_calls(http, bearer(issued), URGENCY_CALL).status_code == 200


def test_issue_without_a_licence_gives_a_token_for_open_adapters_only(keypair: Path,
                                                                      capsys: pytest.CaptureFixture[str]) -> None:
    cli(["token", "issue", "--key", str(keypair / "private.pem"), "--sub", "bob", "--ttl-s", "60"])
    assert jwt.decode(capsys.readouterr().out.strip(), options={"verify_signature": False})["licences"] == []


@pytest.mark.parametrize("ttl", ["3601", "0", "-5"])
def test_issue_refuses_a_lifetime_outside_one_to_3600_seconds(keypair: Path, ttl: str) -> None:
    with pytest.raises(SystemExit, match="3600"):
        cli(["token", "issue", "--key", str(keypair / "private.pem"), "--sub", "bob", "--ttl-s", ttl])


def test_the_in_flight_slot_is_freed_when_the_request_is_cancelled() -> None:
    """A client that disconnects cancels the route: the cancellation passes through `limited`, whose finally frees
    the slot."""
    import asyncio
    from types import SimpleNamespace

    limiter = auth.RateLimiter(1000.0, 1)
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(limiter=limiter)))
    caller = auth.Caller("hub", "alice", frozenset())

    async def run() -> None:
        dependency = auth.limited(request, caller)  # type: ignore[arg-type]
        assert await anext(dependency) is caller
        assert limiter.in_flight == {"hub:alice": 1}
        with pytest.raises(asyncio.CancelledError):
            await dependency.athrow(asyncio.CancelledError())

    asyncio.run(run())
    assert limiter.in_flight == {}


def test_a_flood_of_new_callers_within_one_interval_keeps_the_buckets_bounded() -> None:
    """Besides the timer, a sweep runs whenever the buckets have doubled since the last one (past the cap), so the
    dict stays within a constant factor of the callers that are not idle."""
    clock = Clock()
    limiter = auth.RateLimiter(1_000_000.0, 1, clock=clock)
    largest = 0
    for index in range(50_000):
        clock.now += 0.000_01  # 10 tokens' worth: a caller seen once is idle (full) by the next request
        limiter.acquire(f"hub:{index}")
        limiter.release(f"hub:{index}")
        largest = max(largest, len(limiter.buckets))
    assert clock.now - 1000.0 < auth.SWEEP_INTERVAL_S  # the whole flood falls inside one sweep interval
    assert largest <= 2 * auth.MAX_IDLE_BUCKETS + 1
