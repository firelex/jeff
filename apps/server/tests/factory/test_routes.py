"""The /v1/factory routes as documented: every route of jeff-server's factory router is here with the same method and
path, plus GET /v1/factory/worker; each one's OpenAPI operation lists the error statuses it can answer, and a route
that takes a body documents the body its own request model accepts, not a bare object."""

from collections.abc import Iterator
from pathlib import Path
from typing import Any, get_args

import yaml
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator
from pydantic import BaseModel

from jeff.testing import FakeEngine

from jeff_server.app import create_app
from jeff_server.factory.config import FactoryConfig
from jeff_server.settings import ServerSettings

ROOT = Path(__file__).resolve().parents[4]
OPENAPI = yaml.safe_load((ROOT / "contract" / "openapi.yaml").read_text(encoding="utf-8"))

# jeff-server's src/jeff/factory/router.py, method and path of every route (its file_path:path parameter written
# as OpenAPI writes it).
JEFF_SERVER_ROUTES = {
    ("get", "/v1/factory/mode"), ("post", "/v1/factory/description-checks"), ("post", "/v1/factory/runs"),
    ("get", "/v1/factory/references"), ("get", "/v1/factory/references/{name}/files/{file_path}"),
    ("post", "/v1/factory/previews"), ("post", "/v1/factory/previews/import"),
    ("get", "/v1/factory/previews/{run_id}"), ("put", "/v1/factory/previews/{run_id}/spec"),
    ("post", "/v1/factory/previews/{run_id}/approve"), ("get", "/v1/factory/previews/{run_id}/events"),
    ("post", "/v1/factory/runs/import"), ("get", "/v1/factory/testsets"), ("get", "/v1/factory/testsets/{name}"),
    ("post", "/v1/factory/testsets/{name}/review"), ("post", "/v1/factory/testsets/{name}/freeze"),
    ("post", "/v1/factory/runs/{run_id}/testset/{name}"), ("post", "/v1/factory/runs/{run_id}/again"),
    ("get", "/v1/factory/runs"), ("get", "/v1/factory/runs/{run_id}"), ("put", "/v1/factory/runs/{run_id}/spec"),
    ("post", "/v1/factory/runs/{run_id}/approve"), ("post", "/v1/factory/runs/{run_id}/steps/{step}"),
    ("post", "/v1/factory/runs/{run_id}/run-rest"), ("put", "/v1/factory/runs/{run_id}/gate"),
    ("post", "/v1/factory/runs/{run_id}/test-set"), ("post", "/v1/factory/runs/{run_id}/replay"),
    ("get", "/v1/factory/runs/{run_id}/events"), ("get", "/v1/factory/replays"),
    ("get", "/v1/factory/replays/{replay_id}"),
}


def owner_app(root: Path) -> TestClient:
    config = FactoryConfig(mode="owner", root=root, teacher_url="u", teacher_key="k", teacher_model="m",
                           trainer=root / "trainer", min_free_gb=16.0)
    settings = ServerSettings(engine_sockets=(root / "e.sock",), model_name="jeff-v1.3", factory=config)
    return TestClient(create_app(settings, engine=FakeEngine(lambda adapter, row: [1.0], set())))


def api_routes(routes: Any) -> Iterator[APIRoute]:
    """Every APIRoute, through included routers (FastAPI keeps an included router as one route holding it)."""
    for route in routes:
        if hasattr(route, "original_router"):
            yield from api_routes(route.original_router.routes)
        elif isinstance(route, APIRoute):
            yield route


def factory_routes(client: TestClient) -> dict[tuple[str, str], APIRoute]:
    routes = {}
    for route in api_routes(client.app.routes):  # type: ignore[attr-defined]
        if route.path.startswith("/v1/factory/"):
            for method in route.methods:
                routes[(method.lower(), route.path.replace("{file_path:path}", "{file_path}"))] = route
    assert routes, "no /v1/factory route found"
    return routes


def model_of(annotation: Any) -> type[BaseModel]:
    """The request model in a body parameter's annotation (`FreezeBody | None` for an optional body)."""
    found = [item for item in (annotation, *get_args(annotation))
             if isinstance(item, type) and issubclass(item, BaseModel)]
    assert len(found) == 1, annotation
    return found[0]


def documented() -> dict[tuple[str, str], dict[str, Any]]:
    return {(method, path): operation for path, item in OPENAPI["paths"].items() if path.startswith("/v1/factory/")
            for method, operation in item.items() if method in ("get", "post", "put", "delete")}


def test_every_jeff_server_factory_route_is_served_and_documented_with_the_worker_route(tmp_path: Path) -> None:
    expected = JEFF_SERVER_ROUTES | {("get", "/v1/factory/worker")}
    with owner_app(tmp_path) as client:
        assert set(factory_routes(client)) == expected
    assert set(documented()) == expected


def test_every_factory_route_lists_its_error_statuses() -> None:
    for (method, path), operation in documented().items():
        statuses = {int(status) for status in operation["responses"] if status != "200"}
        assert {401, 403, 429} <= statuses, (method, path)  # credentials, owner-only or factory off, rate limit
        if "{" in path or "requestBody" in operation:
            assert 422 in statuses, (method, path)  # an unknown run, test set, reference or replay; a refused body


def test_every_body_is_the_routes_own_request_model(tmp_path: Path) -> None:
    operations = documented()
    with owner_app(tmp_path) as client:
        for key, route in factory_routes(client).items():
            body = route.body_field
            if body is None:
                assert "requestBody" not in operations[key], key
                continue
            schema = operations[key]["requestBody"]["content"]["application/json"]["schema"]
            assert schema == model_of(body.field_info.annotation).model_json_schema(), key
            assert schema["additionalProperties"] is False and schema["properties"], key
            assert all(field.get("description") for field in schema["properties"].values()), key
            Draft202012Validator.check_schema(schema)
            assert not Draft202012Validator(schema).is_valid({"unknown": 1}), key


def test_a_body_the_model_refuses_is_a_listed_422(tmp_path: Path) -> None:
    operations = documented()
    with owner_app(tmp_path) as client:
        for (method, path), route in factory_routes(client).items():
            if route.body_field is None:
                continue
            url = path.replace("{run_id}", "missing").replace("{name}", "missing")
            response = client.request(method.upper(), url, json={"unknown": 1})
            assert response.status_code == 422, (method, path, response.text)
            assert response.json()["error"]["type"] == "InvalidRequest"
            assert "422" in operations[(method, path)]["responses"]
