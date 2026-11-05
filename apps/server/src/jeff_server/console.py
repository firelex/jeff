"""Serving the built web console (apps/server/ui's Vite build, committed under ui/ beside this module) at /ui, and
its own JSON endpoints: /ui/me, /ui/events, /ui/access-log and /ui/examples.

GET /ui (a relative redirect to /ui/), GET /ui/ and GET /ui/assets/{name} need no credentials at all: the console signs itself in through POST
/ui/login (jeff_server.auth) once it is loaded, and sends its session token on every call after that. The four JSON
endpoints below carry the same credentials and rate limit as every /v1 route (jeff_server.auth.V1_DEPENDENCIES) --
nothing here is open just because it happens to serve the console.

UI_FOLDER is a module-level constant rather than a value baked into each route at import time, so a test can point
it at a temporary folder (monkeypatch.setattr(console, "UI_FOLDER", tmp_path)) and have every route that reads it
see the new value, including the one built by scripts/build_openapi.py's app instance.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from starlette.concurrency import run_in_threadpool

from jeff.client import Jeff
from jeff.contract import shipped_dir
from jeff.errors import InvalidRequest, NotLicensed
from jeff.listing import function_listing

from jeff_server.access import visible_jeff
from jeff_server.access_log import AccessLog
from jeff_server.auth import V1_DEPENDENCIES, Caller, request_caller
from jeff_server.errors import error_response
from jeff_server.settings import ServerSettings

UI_FOLDER = Path(__file__).with_name("ui")  # built by `pnpm --filter jeff-console build`; committed with its source

# The console page's Content-Security-Policy: scripts, styles, fonts, images and API calls from this server only;
# inline styles allowed (React style attributes, CodeMirror's and Mermaid's generated <style> elements); images
# also as data: URLs (small bundled images Vite inlines); never framed by another site.
CONSOLE_CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"
# Sent on the page and every asset, so a browser never guesses a type other than the one served.
NOSNIFF = {"X-Content-Type-Options": "nosniff"}

NOT_BUILT_MESSAGE = "The console is not built: run pnpm --filter jeff-console build"
NO_SUCH_ASSET_MESSAGE = "No such console asset"

# GET /ui/access-log's limit: bounded to this range, 422 outside it (fastapi.Query's ge/le below), never silently
# clamped -- the console never needs more than a few thousand rows on one page.
ACCESS_LOG_MIN, ACCESS_LOG_MAX, ACCESS_LOG_DEFAULT = 1, 5000, 1000

# A flat asset filename: a letter or digit, then up to 127 more letters, digits, '.', '_' or '-' -- never a path
# separator (forward or backward slash), never a leading dot, and nothing a filesystem call could choke on.
# Checked before anything here touches the filesystem: Path.resolve()/is_file() can raise on input this would
# otherwise let through (an embedded NUL byte decoded from %00; a name past the filesystem's own length limit),
# and an uncaught exception from inside a route is answered as 500 EngineError with that exception's own message
# (jeff_server.errors), which can include this process's install path -- unacceptable from an unauthenticated
# route. Matching this pattern first means neither call ever sees that input.
ASSET_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")

# GET /ui, GET /ui/, GET /ui/assets/{name} and GET / (public redirect): no credentials, not JSON operations this
# server's contract describes -- see packages/python/tests/test_openapi.py, which names these same four routes and
# leaves them out of the route-parity check for the same reason /openapi.json, the channel and /mcp are left out.
html_router = APIRouter()
# /ui/me, /ui/events, /ui/access-log, /ui/examples: the same credentials and rate limit as /v1 (V1_DEPENDENCIES).
router = APIRouter(dependencies=V1_DEPENDENCIES)


@html_router.get("/ui", include_in_schema=False)
def console_page_without_slash() -> Response:
    """Sends /ui to /ui/. The page names its assets by relative paths (./assets/...), which resolve under /ui/ only
    when the page's own address ends in a slash. The Location is relative ("ui/", resolved against /ui), so behind a
    proxy that serves this server under a path prefix (the gateway's /jeff/) it lands on <prefix>/ui/ without this
    server knowing or trusting the prefix."""
    return RedirectResponse("ui/", status_code=307)


@html_router.get("/ui/", response_class=HTMLResponse, include_in_schema=False)
def console_page(request: Request) -> Response:
    """index.html is never cached (Cache-Control: no-cache): it names its hashed asset files by content, so a new
    build must always be fetched fresh, while the assets it names are cached forever (console_asset). It carries
    CONSOLE_CSP and nosniff. Each time
    the page is served, a "console" visit is appended to the access log (when JEFF_ACCESS_LOG is set) with no
    user: the page loads before anyone signs in, so these are the visits the access log counts as anonymous."""
    page = UI_FOLDER / "index.html"
    if not page.is_file():
        # 404 with error type InvalidRequest, the same shape an unmatched route already answers with -- but the
        # catalogue fixes InvalidRequest's status at 422 everywhere else, so this passes status=404 explicitly
        # rather than letting a raised InvalidRequest take its catalogued one.
        return error_response(request, InvalidRequest(NOT_BUILT_MESSAGE), status=404)
    response = HTMLResponse(page.read_text(encoding="utf-8"), headers={
        "Cache-Control": "no-cache", "Content-Security-Policy": CONSOLE_CSP, **NOSNIFF})
    access_log: AccessLog | None = request.app.state.access_log
    if access_log is not None:
        access_log.record_request("console", user=None, headers=request.headers,
                                  peer=request.client.host if request.client else None, path=request.url.path,
                                  status=response.status_code)
    return response


@html_router.get("/ui/assets/{name}", include_in_schema=False)
def console_asset(name: str, request: Request) -> Response:
    """A file from ui/assets/ only: `name` must match ASSET_NAME, and its resolved path must still stay inside the
    assets folder once resolved (is_relative_to) -- a name that matches the pattern but happens to be a symlink
    pointing outside the folder is still refused. Anything that fails either check is 404 InvalidRequest, same
    status and type as the "not built" page above, built the same way (error_response's status=)."""
    if not ASSET_NAME.fullmatch(name):
        return error_response(request, InvalidRequest(NO_SUCH_ASSET_MESSAGE), status=404)
    folder = (UI_FOLDER / "assets").resolve()
    path = (folder / name).resolve()
    if not path.is_relative_to(folder) or not path.is_file():
        return error_response(request, InvalidRequest(NO_SUCH_ASSET_MESSAGE), status=404)
    return FileResponse(path, headers={"Cache-Control": "public, max-age=31536000, immutable", **NOSNIFF})


@html_router.get("/", include_in_schema=False)
def root(request: Request) -> Response:
    """A public server sends every visitor to the console; a private server claims no behaviour for "/" here (a
    plain 404, Starlette's own routing-miss shape) -- nothing else in this server answers it. The Location is
    relative ("ui/"), so behind a proxy's path prefix it lands on <prefix>/ui/."""
    settings: ServerSettings = request.app.state.settings
    if settings.public:
        return RedirectResponse("ui/")
    raise HTTPException(404)


def _is_admin(caller: Caller, settings: ServerSettings) -> bool:
    """Whether this caller may read the access log: a signed-in console session (never a hub token or the API
    key, even one whose subject happens to equal a login JEFF_ADMINS lists) under a login JEFF_ADMINS lists.
    JEFF_ADMINS is already lower-cased and stripped once (jeff_server.settings.ServerSettings._admins). A server
    with no credentials configured at all has no console sessions, so it has no admins either, ever."""
    return caller.kind == "console" and caller.subject.strip().lower() in settings.admins


@router.get("/ui/me")
async def me(request: Request) -> dict[str, Any]:
    caller = request_caller(request)
    return {"user": caller.subject, "kind": caller.kind,
            "admin": _is_admin(caller, request.app.state.settings)}


class ConsoleEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")
    view: str = Field(min_length=1, max_length=100, pattern=r"^[a-z0-9-]+$")


@router.post("/ui/events")
async def events(event: ConsoleEvent, request: Request) -> dict[str, bool]:
    """The console reports each page it opens, for the access log, under the same user every other entry for this
    caller has (Caller.log_user)."""
    caller = request_caller(request)
    access_log: AccessLog | None = request.app.state.access_log
    if access_log is not None:
        access_log.record_request("view", user=caller.log_user, headers=request.headers,
                                   peer=request.client.host if request.client else None,
                                   path=request.url.path, status=200, detail=event.view)
    return {"logged": access_log is not None}


@router.get("/ui/access-log")
async def read_access_log(
        request: Request,
        limit: int = Query(ACCESS_LOG_DEFAULT, ge=ACCESS_LOG_MIN, le=ACCESS_LOG_MAX,
                           description="At most this many entries, newest first."),
) -> dict[str, Any]:
    """Only a console session under a JEFF_ADMINS login may read this (403 NotLicensed otherwise -- see
    _is_admin); a server with no credentials configured at all has no console sessions and so no admins, ever,
    whatever JEFF_ADMINS says. 422 InvalidRequest when JEFF_ACCESS_LOG is not set: there is nothing to read."""
    caller = request_caller(request)
    settings: ServerSettings = request.app.state.settings
    if not _is_admin(caller, settings):
        raise NotLicensed(f"Only the server's administrators (JEFF_ADMINS) may read the access log; "
                          f"{caller.subject} is not one")
    access_log: AccessLog | None = request.app.state.access_log
    if access_log is None:
        raise InvalidRequest("No access log on this server: set JEFF_ACCESS_LOG")
    return access_log.read(limit)


@lru_cache(maxsize=1)
def _golden_cases() -> list[dict[str, Any]]:
    path = shipped_dir() / "contract" / "golden" / "step1.json"
    return list(json.loads(path.read_text(encoding="utf-8"))["cases"])


# Written into an adapter folder by scripts/build_playground_examples.py: examples from the adapter's own test set.
EXAMPLES_FILE = "playground-examples.json"


def _folder_examples(jeff: Jeff, served: set[str]) -> list[dict[str, Any]]:
    """The examples in the playground-examples.json of every catalogue adapter whose folder has one (an adapter from
    the shipped catalogue has no such file), for the served functions. A file that names another adapter is an
    error."""
    found = []
    for manifest in jeff.catalogue.manifests():
        path = manifest.path.parent / EXAMPLES_FILE
        if not path.is_file():
            continue
        content = json.loads(path.read_text(encoding="utf-8"))
        if content["adapter"] != manifest.id:
            raise ValueError(f"{path} holds examples for {content['adapter']}, not for {manifest.id}; rebuild it with "
                             "scripts/build_playground_examples.py")
        found.extend({"name": example["name"], "call": example["call"], "expected": example["expected"]}
                     for example in content["examples"] if example["call"]["function"] in served)
    return found


@router.get("/ui/examples")
async def examples(request: Request) -> dict[str, Any]:
    """The playground's examples for the functions of the adapters the server serves right now -- the same "served
    now" jeff.listing.function_listing answers for GET /v1/functions, read fresh (refresh=True) so a just-changed
    served set shows at once: one per committed golden case (contract/golden/step1.json), then those each adapter
    folder holds in playground-examples.json (rows of its test set, each with the test set's answer as
    "expected")."""
    shared: Jeff = await request.app.state.planner.ready()
    jeff = visible_jeff(shared, request_caller(request))  # an adapter the caller may not use is left out
    listing = await run_in_threadpool(function_listing, jeff)
    served = {function["id"] for function in listing["functions"]}
    golden = [{"name": case["name"], "call": case["call"]} for case in _golden_cases()
              if case["call"]["function"] in served]
    return {"examples": golden + await run_in_threadpool(_folder_examples, jeff, served)}
