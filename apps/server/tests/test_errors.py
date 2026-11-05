"""jeff_server.errors.install: every catalogued error maps to its HTTP status and the wire body, Busy carries
Retry-After, a non-JSON body is InvalidRequest, every response carries x-request-id (even one Starlette's
ServerErrorMiddleware sends for a totally unexpected exception, outside any BaseHTTPMiddleware-style handling --
see jeff_server.app._Application), and a null-status error (a server bug, per the catalogue) is answered as 500
EngineError and logged."""

import logging
import re
from collections.abc import Callable

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel

import jeff.errors as errors
from jeff.contract import error_catalogue
from jeff.decision import Decision
from jeff.testing import FakeEngine

from jeff_server.app import create_app
from jeff_server.errors import error_response
from jeff_server.settings import ServerSettings

MINIMAL_ENV = {"JEFF_ENGINE_SOCKETS": "/tmp/engine-0.sock", "JEFF_MODEL_NAME": "jeff-v1.3"}

_DECISION = Decision(value=2, confidence=0.4, abstained=True, calibrated=True, interface="rate",
                     function="jeff.support.triage.urgency", version="1.3.0",
                     probabilities={"0": 0.1, "1": 0.2, "2": 0.4, "3": 0.2, "4": 0.1}, score=2.0, route=(),
                     decided_by="jeff.support.triage.urgency@1.3.0", escalations=(), timing={"path": "mlx"})

# One way to build each catalogued error with valid arguments, so the test route can raise any of them by name.
CONSTRUCTORS: dict[str, Callable[[], errors.JeffError]] = {
    "JeffError": lambda: errors.JeffError("boom"),
    "ContractError": lambda: errors.ContractError("boom"),
    "ManifestError": lambda: errors.ManifestError("boom"),
    "InvalidReference": lambda: errors.InvalidReference("boom"),
    "UnknownFunction": lambda: errors.UnknownFunction("boom"),
    "NotInstalled": lambda: errors.NotInstalled("jeff.test.adapter.fn"),
    "MissingContext": lambda: errors.MissingContext("jeff.test.adapter.fn", ["company"]),
    "MissingLabels": lambda: errors.MissingLabels("jeff.test.adapter.fn"),
    "InvalidLabels": lambda: errors.InvalidLabels("boom"),
    "TooManyLabels": lambda: errors.TooManyLabels("boom"),
    "TooManyHandles": lambda: errors.TooManyHandles("boom"),
    "UnknownContext": lambda: errors.UnknownContext("boom"),
    "InterfaceMismatch": lambda: errors.InterfaceMismatch("boom"),
    "LowConfidence": lambda: errors.LowConfidence(_DECISION),
    "EscalationFailed": lambda: errors.EscalationFailed("human", "timeout"),
    "EscalationBudgetExceeded": lambda: errors.EscalationBudgetExceeded("boom"),
    "InvalidPolicy": lambda: errors.InvalidPolicy("boom"),
    "GraphError": lambda: errors.GraphError("boom"),
    "RuntimeMissing": lambda: errors.RuntimeMissing("boom"),
    "ProprietaryWeights": lambda: errors.ProprietaryWeights("jeff.test.adapter"),
    "CalibrationError": lambda: errors.CalibrationError("boom"),
    "EngineError": lambda: errors.EngineError("boom"),
    "InputTooLong": lambda: errors.InputTooLong("boom"),
    "BackendUnavailable": lambda: errors.BackendUnavailable("boom"),
    "Busy": lambda: errors.Busy("boom", 2.3),
    "DeadlineExceeded": lambda: errors.DeadlineExceeded("boom"),
    "ProtocolMismatch": lambda: errors.ProtocolMismatch("boom"),
    "NotReady": lambda: errors.NotReady("boom"),
    "Unauthorised": lambda: errors.Unauthorised("boom"),
    "NotLicensed": lambda: errors.NotLicensed("boom"),
    "RateLimited": lambda: errors.RateLimited("boom", 0.2),
    "UsageLimitReached": lambda: errors.UsageLimitReached("boom", "u_1", 1, 2, "2026-12-03T10:00:00Z"),
    "InvalidRequest": lambda: errors.InvalidRequest("boom"),
    "Conflict": lambda: errors.Conflict("boom"),
    "FactoryOff": lambda: errors.FactoryOff("boom"),
}

STATUSED = sorted(name for name, spec in error_catalogue().items() if spec.status is not None)
NULL_STATUS = sorted(name for name, spec in error_catalogue().items() if spec.status is None)


def test_constructors_cover_the_whole_catalogue() -> None:
    assert set(CONSTRUCTORS) == set(error_catalogue())


class _ValidatedBody(BaseModel):
    n: int


def build_app() -> FastAPI:
    settings = ServerSettings.from_env(MINIMAL_ENV)
    engine = FakeEngine(answers=lambda adapter, row: [1.0], installed=set())
    app = create_app(settings, engine=engine)

    @app.post("/test/raise")
    async def _raise(request: Request) -> None:
        payload = await request.json()
        raise CONSTRUCTORS[payload["error"]]()

    @app.get("/test/ok")
    async def _ok() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/test/bug")
    async def _bug() -> None:
        raise KeyError("missing-key")  # a plain, non-JeffError bug, not caught anywhere in our own code

    @app.post("/test/validated")
    async def _validated(body: _ValidatedBody) -> dict[str, int]:
        return {"n": body.n}

    return app


@pytest.mark.parametrize("name", STATUSED)
def test_catalogued_error_maps_to_its_status_and_wire_body(name: str) -> None:
    with TestClient(build_app()) as client:
        response = client.post("/test/raise", json={"error": name})
    spec = error_catalogue()[name]
    assert response.status_code == spec.status
    body = response.json()
    assert body["error"]["type"] == name
    assert isinstance(body["error"]["message"], str) and body["error"]["message"]
    assert isinstance(body["error"]["details"], dict)
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


@pytest.mark.parametrize("name", NULL_STATUS)
def test_null_status_error_is_a_500_engine_error_and_is_logged(name: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.ERROR, logger="jeff_server"):
        with TestClient(build_app()) as client:
            response = client.post("/test/raise", json={"error": name})
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "EngineError"
    assert name in body["error"]["message"]
    assert any(name in record.getMessage() for record in caplog.records)


def test_busy_carries_retry_after_rounded_up_from_retry_after_s() -> None:
    with TestClient(build_app()) as client:
        response = client.post("/test/raise", json={"error": "Busy"})
    assert response.status_code == 529
    assert response.headers["Retry-After"] == "3"  # ceil(2.3)


def test_rate_limited_carries_retry_after_rounded_up_from_retry_after_s() -> None:
    with TestClient(build_app()) as client:
        response = client.post("/test/raise", json={"error": "RateLimited"})
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "1"  # ceil(0.2)
    assert response.json()["error"]["details"] == {"retry_after_s": 0.2}


def test_unauthorised_asks_for_a_bearer_token() -> None:
    with TestClient(build_app()) as client:
        response = client.post("/test/raise", json={"error": "Unauthorised"})
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_non_json_body_is_422_invalid_request() -> None:
    with TestClient(build_app()) as client:
        response = client.post("/test/raise", content=b"not json {", headers={"content-type": "application/json"})
    assert response.status_code == 422
    assert response.json()["error"]["type"] == "InvalidRequest"


def test_every_response_has_a_request_id_including_success() -> None:
    with TestClient(build_app()) as client:
        response = client.get("/test/ok")
    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


def test_a_totally_unexpected_exception_still_gets_x_request_id_and_is_logged_with_it(
        caplog: pytest.LogCaptureFixture) -> None:
    # A plain KeyError isn't a JeffError, so it is answered by the handler registered for the bare Exception
    # class -- which Starlette hoists into ServerErrorMiddleware, the very outermost layer, outside anything
    # added with add_middleware(). raise_server_exceptions=False makes TestClient behave like a real deployed
    # server here: it returns the response actually sent over the wire instead of re-raising the KeyError into
    # the test (ServerErrorMiddleware always re-raises after sending its response, for a real server's own
    # logging -- see starlette/middleware/errors.py).
    with caplog.at_level(logging.ERROR, logger="jeff_server"):
        with TestClient(build_app(), raise_server_exceptions=False) as client:
            response = client.post("/test/bug")
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "EngineError"
    assert "KeyError" in body["error"]["message"]
    request_id = response.headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", request_id)
    assert any(request_id in record.getMessage() for record in caplog.records)


def test_request_validation_error_is_422_invalid_request_naming_the_first_problem() -> None:
    with TestClient(build_app()) as client:
        response = client.post("/test/validated", json={"n": "not-a-number"})
    assert response.status_code == 422
    body = response.json()
    assert body["error"]["type"] == "InvalidRequest"
    assert "n" in body["error"]["message"]
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


def test_unmatched_route_is_404_invalid_request_naming_the_path() -> None:
    with TestClient(build_app()) as client:
        response = client.get("/no/such/endpoint")
    assert response.status_code == 404
    body = response.json()
    assert body["error"]["type"] == "InvalidRequest"
    assert "/no/such/endpoint" in body["error"]["message"]
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


def test_wrong_method_is_405_invalid_request() -> None:
    with TestClient(build_app()) as client:
        response = client.delete("/test/ok")  # /test/ok only answers GET
    assert response.status_code == 405
    body = response.json()
    assert body["error"]["type"] == "InvalidRequest"
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


def test_a_catalogued_error_does_not_lose_its_status_to_the_http_exception_handler() -> None:
    # Sanity check that registering a handler for StarletteHTTPException doesn't shadow JeffError handling (a
    # fastapi.HTTPException is a StarletteHTTPException subclass, but our JeffErrors never raise one).
    with TestClient(build_app()) as client:
        response = client.post("/test/raise", json={"error": "UnknownFunction"})
    assert response.status_code == 404
    assert response.json()["error"]["type"] == "UnknownFunction"


def test_error_response_can_answer_at_a_status_other_than_the_catalogued_one() -> None:
    """jeff_server.console answers "the page is not built" and "no such asset" as 404 with error type
    InvalidRequest, even though the catalogue fixes InvalidRequest at 422 for every other route; error_response's
    own status= parameter is how a route does that deliberately, rather than building the envelope by hand."""
    app = build_app()

    @app.get("/test/custom-status")
    async def _custom_status(request: Request) -> None:
        return error_response(request, errors.InvalidRequest("boom"), status=404)

    with TestClient(app) as client:
        response = client.get("/test/custom-status")
    assert response.status_code == 404
    assert response.json()["error"] == {"type": "InvalidRequest", "message": "boom", "details": {}}
    assert re.fullmatch(r"[0-9a-f]{32}", response.headers["x-request-id"])


def test_fastapi_own_docs_are_disabled() -> None:
    """/docs and /redoc stay disabled; /openapi.json is no longer a 404 -- Task 8 serves the committed contract
    description there instead of FastAPI's own generated one (jeff_server.openapi)."""
    with TestClient(build_app()) as client:
        assert client.get("/docs").status_code == 404
        assert client.get("/redoc").status_code == 404
        response = client.get("/openapi.json")
    assert response.status_code == 200
    assert response.json()["openapi"] == "3.1.0"
