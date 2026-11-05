"""GET /openapi.json: the OpenAPI 3.1 description scripts/build_openapi.py writes to contract/openapi.yaml,
committed and shipped with jeff (jeff.contract.shipped_dir() carries the whole contract/ folder, including
openapi.yaml, via scripts/sync_shipped.py). Parsed from YAML to JSON once, at startup -- jeff_server.app's
create_app() calls load_document() and stores the result on app.state.openapi_document, so a request never
re-reads the file.

Signed in exactly like a /v1 route (jeff_server.auth.authenticated): open when this server has no credentials
configured, otherwise needs one. On a public server (JEFF_PUBLIC=1) ServerSettings refuses to start without a
credential configured, so only a signed-in caller gets this route there; on a private server it is open exactly
when /v1 is, since it is the same dependency.

FastAPI's own interactive-docs generator stays disabled (docs_url, redoc_url and openapi_url are all None on the
app in jeff_server.app): this is the one and only /openapi.json the server answers, and it describes the real
contract, not FastAPI's guess at it from the route functions' type hints."""

from pathlib import Path
from typing import Any

import yaml
from fastapi import APIRouter, Depends, Request

from jeff.contract import shipped_dir

from jeff_server.auth import authenticated

router = APIRouter()


def load_document() -> dict[str, Any]:
    path = shipped_dir() / "contract" / "openapi.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise TypeError(f"{path} does not hold a YAML mapping (an OpenAPI document); run "
                        "scripts/build_openapi.py")
    return document


@router.get("/openapi.json", dependencies=[Depends(authenticated)])
async def openapi_json(request: Request) -> dict[str, Any]:
    document: dict[str, Any] = request.app.state.openapi_document
    return document
