"""Installs the API's error handling on a FastAPI app:

- every JeffError is answered with the HTTP status from jeff.contract.error_catalogue() and the body
  {"error": error_to_wire(error)}; Busy and RateLimited also carry Retry-After (whole seconds, rounded up from
  retry_after_s), and Unauthorised carries WWW-Authenticate: Bearer;
- a request body that is not valid JSON is answered as InvalidRequest;
- a body that is valid JSON but the wrong shape for a Pydantic-validated route (fastapi.RequestValidationError) is
  answered as 422 InvalidRequest naming the first problem;
- an unmatched route or method (starlette.exceptions.HTTPException, raised by Starlette's own routing for a 404 or
  405) is answered with the same {"error": {"type": "InvalidRequest", ...}} body, but keeps HTTPException's own
  status (404 or 405) rather than InvalidRequest's catalogued 422 -- no catalogue entry is added for this, since
  it is a routing signal, not a JeffError;
- a JeffError whose catalogue status is null is a server bug (the catalogue says it is never sent over HTTP):
  answered as 500 EngineError, and logged, rather than leaking a status outside the HTTP range;
- an exception that is not a JeffError at all is also a server bug: answered as 500 EngineError, and logged, so
  every error response (not just the ones we anticipated) has the same {"error": ...} body.

x-request-id is NOT set here: Starlette hoists a handler registered for the bare Exception class out of its normal
exception-handling middleware and into ServerErrorMiddleware -- the very outermost layer, added by Starlette before
any middleware added with add_middleware() -- so a BaseHTTPMiddleware-based approach never sees that response
(call_next simply raises past it, since the exception propagates all the way up to ServerErrorMiddleware, which
sends its own response using the *original* send it was given, bypassing any inner middleware's wrapped send).
jeff_server.app's FastAPI subclass sets and stamps the id by overriding __call__ itself, the one place that wraps
truly everything. Handlers here only read the id (via request.state.request_id) for logging.
"""

import logging
from collections.abc import Callable
from json import JSONDecodeError
from math import ceil
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from jeff.contract import error_catalogue
from jeff.errors import Busy, EngineError, InvalidRequest, JeffError, RateLimited, Unauthorised
from jeff.wire import error_to_wire

logger = logging.getLogger("jeff_server")

REQUEST_ID_STATE_KEY = "request_id"


def install(app: FastAPI) -> None:
    @app.exception_handler(JeffError)
    async def _jeff_error(request: Request, error: JeffError) -> JSONResponse:
        return _respond(request, error)

    @app.exception_handler(JSONDecodeError)
    async def _bad_json(request: Request, error: JSONDecodeError) -> JSONResponse:
        return _respond(request, InvalidRequest(f"The request body is not valid JSON: {error}"))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
        return _respond(request, InvalidRequest(_first_problem(error)))

    @app.exception_handler(StarletteHTTPException)
    async def _http_exception(request: Request, error: StarletteHTTPException) -> JSONResponse:
        return _respond(request, InvalidRequest(_http_exception_message(request, error)),
                        status=error.status_code)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, error: Exception) -> JSONResponse:
        logger.exception("Unhandled %s answering %s %s (request id %s)", type(error).__name__, request.method,
                         request.url.path, _request_id(request))
        return _respond(request, EngineError(f"{type(error).__name__}: {error}"))


def _first_problem(error: RequestValidationError) -> str:
    problems = error.errors()
    if not problems:
        return str(error)
    first = problems[0]
    where = "/".join(str(part) for part in first.get("loc", ()) if part != "body")
    message = str(first["msg"])
    return f"{message} (at /{where})" if where else message


def _http_exception_message(request: Request, error: StarletteHTTPException) -> str:
    if error.status_code == 404:
        return f"No such endpoint: {request.url.path}"
    if error.status_code == 405:
        return f"Method not allowed: {request.method} {request.url.path}"
    return str(error.detail)


def _request_id(request: Request) -> str:
    return str(getattr(request.state, REQUEST_ID_STATE_KEY, "-"))


def error_response(request: Request, error: JeffError, *, status: int | None = None, challenge: bool = True,
                   legacy_detail: Callable[[JeffError], Any] | None = None) -> JSONResponse:
    """The response the handlers give for `error`; status overrides the catalogue's own lookup, for a route that
    deliberately answers a JeffError at a different HTTP status than its catalogued one -- jeff_server.console
    answers "the console is not built" and "no such asset" as 404 InvalidRequest, even though the catalogue fixes
    InvalidRequest at 422 for every other route, matching how an unmatched route already answers 404. challenge=
    False leaves out an Unauthorised error's WWW-Authenticate header, for a 401 that no bearer token would fix
    (sign-in not configured). legacy_detail: for jeff-server's endpoints (jeff_server.systemone), the body also
    gets {"detail": legacy_detail(error as sent)}."""
    return _respond(request, error, status=status, challenge=challenge, legacy_detail=legacy_detail)


def _respond(request: Request, error: JeffError, *, status: int | None = None, challenge: bool = True,
             legacy_detail: Callable[[JeffError], Any] | None = None) -> JSONResponse:
    # A caller who is not the owner gets an error that names no private adapter (jeff_server.redaction; imported
    # here, as it imports jeff_server.auth, which imports this module).
    from jeff_server.redaction import as_seen_by
    error = as_seen_by(error, getattr(request.state, "caller", None),
                       lambda: request.app.state.planner.built.catalogue,
                       lambda: request.app.state.settings.engine_sockets,
                       f"{request.method} {request.url.path} (request id {_request_id(request)})")
    wire_error: JeffError = error
    if status is None:
        spec = error_catalogue().get(type(error).__name__)
        status = spec.status if spec is not None else None
        if status is None:
            logger.error("%s has status null in the error catalogue (never sent over HTTP) but was raised "
                          "answering a request (request id %s); this is a server bug: %s",
                          type(error).__name__, _request_id(request), error)
            wire_error = EngineError(f"{type(error).__name__}: {error}")
            status = 500
    headers = {}
    if isinstance(error, (Busy, RateLimited)):
        headers["Retry-After"] = str(ceil(error.retry_after_s))
    if isinstance(error, Unauthorised) and challenge:
        headers["WWW-Authenticate"] = "Bearer"
    content: dict[str, Any] = {"error": error_to_wire(wire_error)}
    if legacy_detail is not None:
        content["detail"] = legacy_detail(wire_error)
    return JSONResponse(content, status_code=status, headers=headers)
