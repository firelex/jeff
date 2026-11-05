"""/mcp: Jeff's MCP tools (jeff.mcp: one tool per function the server can call now, jeff_dispatch and
jeff_list_functions) over the MCP SDK's streamable HTTP transport, behind the same credentials, rate limiter and
access log as /v1.

The FastAPI dependencies that guard /v1 (jeff_server.auth.V1_DEPENDENCIES) only run for FastAPI routes, not for an
ASGI app the server routes to, so McpEndpoint does the same checks itself, for every HTTP request to /mcp, before the
MCP SDK reads it:

- the caller is authenticated (Authenticator.caller, as the `authenticated` dependency does); without valid
  credentials the request is answered 401 Unauthorised with WWW-Authenticate: Bearer, exactly as on /v1, and the
  caller is stored in the request's state (where the access log, jeff_server.app, finds it, as for /v1);
- the request takes one slot of the caller's rate limit, exactly as one /v1 request does; over the limit it is
  answered 429 RateLimited with Retry-After. The slot is held while the request is answered and given back just
  before the response starts (as /v1 gives it back before its response is sent), or when the request ends without
  one. Every request counts, whatever MCP message it carries (initialize, tools/list, tools/call, notifications):
  an MCP client opening a connection and calling one tool sends several, and the SDK's own client also lists the
  tools after the first call of a tool it has not seen listed.

Each tools/list and tools/call runs on caller_view(the shared Jeff, its caller), so licences are checked per caller as
on /v1: a proprietary function without its licence is the tool error "NotLicensed: ...". A JeffError in a tool is a
tool error (jeff.mcp); any other exception is logged by the MCP SDK and answered as a JSON-RPC error inside an HTTP
200 response (so /metrics counts it as a 200): in the handshake era its message is the exception's text, in the
2026-07-28 era "Internal server error".

The transport is stateless with JSON responses: each POST carries one message and gets its answer in the response
body, and the server keeps no MCP sessions between requests, so every request is authenticated and limited on its
own, and nothing outlives the request that made it. Requests are read up to the same 50 MiB as a /v1 body. The
session manager runs inside the app's lifespan (jeff_server.app). Building the catalogue checks that no two of its
functions share a tool name (jeff.mcp.check_tool_names, run by jeff_server.catalogue_refresh.build_catalogue), so a
clashing catalogue stops the server starting, and a rebuilt one that clashes is refused."""

from collections.abc import Awaitable, Callable, MutableMapping
from contextvars import ContextVar
from typing import Any

from mcp.server.context import HandlerResult, ServerRequestContext
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
from starlette.requests import Request
from starlette.routing import Route
from starlette.types import Receive, Scope, Send

from jeff.client import Jeff
from jeff.errors import EngineError, JeffError, RateLimited, Unauthorised
from jeff.mcp import build_mcp_server

from jeff_server.auth import ACCOUNT_HEADER, Authenticator, Caller, RateLimiter
from jeff_server.catalogue_refresh import CatalogueRefresher
from jeff_server.decide import MAX_BODY_BYTES, caller_view
from jeff_server.errors import error_response
from jeff_server.redaction import as_seen_by

MCP_PATH = "/mcp"
# The MCP requests answered with the caller's view of the shared Jeff.
ANSWERED = ("tools/call", "tools/list")

# The caller of the MCP request being answered, set by _Caller around the handler, which runs in the same task.
_CALLER: ContextVar[Caller] = ContextVar("jeff_mcp_caller")


def session_manager(state: Any) -> StreamableHTTPSessionManager:
    """The MCP session manager for an app whose state holds the planner (state.planner,
    jeff_server.catalogue_refresh). Built in the app's lifespan, where it is also run (a manager runs only once).
    Each tools/list and tools/call runs on the planner current when it arrives, so the tools follow a catalogue
    rebuilt after the engines' adapters change."""

    planner: CatalogueRefresher = state.planner

    def jeff_for_request() -> Jeff:
        # On the worker thread the request's work runs on (jeff.mcp), which carries this request's context, so
        # _CALLER is set and a rebuild never runs on the event loop.
        return caller_view(planner.jeff, _CALLER.get())

    def answer_with(work: Callable[[Jeff], dict[str, Any]]) -> dict[str, Any]:
        """A tool call runs on this caller's view of the shared planner, read first, so the retry compares the very
        planner the view was made from; a call naming a tool that catalogue does not have is tried once more on a
        newer catalogue, if the engines' adapters changed (CatalogueRefresher.retry_unknown), on that catalogue's
        view for this caller. A caller whose view leaves private adapters out (jeff_server.access) is retried
        exactly as the owner is."""
        caller = _CALLER.get()
        shared = planner.jeff
        return planner.retry_unknown(shared, lambda planned: work(caller_view(planned, caller)))

    def reword(error: JeffError) -> JeffError:
        """A caller who is not the owner gets an error naming no private adapter (jeff_server.redaction)."""
        return as_seen_by(error, _CALLER.get(), lambda: planner.built.catalogue,
                          lambda: planner.settings.engine_sockets, "an MCP request on /mcp")

    server = build_mcp_server(jeff_for_request, answer_with, reword)
    server.middleware.append(_Caller())
    return StreamableHTTPSessionManager(app=server, stateless=True, json_response=True,
                                        max_request_body_size=MAX_BODY_BYTES)


class _Caller:
    """MCP server middleware: for a tools/list or tools/call, the caller McpEndpoint stored on the HTTP request, for
    jeff_for_request, while the handler runs (in the same task)."""

    async def __call__(self, ctx: ServerRequestContext[Any, Any],
                       call_next: Callable[[ServerRequestContext[Any, Any]], Awaitable[HandlerResult]]
                       ) -> HandlerResult:
        if ctx.method not in ANSWERED:
            return await call_next(ctx)
        request = ctx.request
        if not isinstance(request, Request):
            raise EngineError(f"An MCP {ctx.method} request on /mcp reached the server without its HTTP request "
                              f"(got {type(request).__name__}): a server bug")
        token = _CALLER.set(request.state.caller)
        try:
            return await call_next(ctx)
        finally:
            _CALLER.reset(token)


class McpEndpoint:
    """The ASGI app at /mcp: authenticates the request (401 Unauthorised without valid credentials), stores the
    caller in the request's state, admits it through the rate limiter (429 RateLimited over the limit) and hands it
    to the MCP session manager (app.state.mcp_sessions)."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        request = Request(scope, receive)
        auth: Authenticator = request.app.state.auth
        try:
            caller = auth.caller(request.headers.get("authorization"), request.headers.get(ACCOUNT_HEADER))
        except Unauthorised as error:
            await error_response(request, error)(scope, receive, send)
            return
        request.state.caller = caller
        manager: StreamableHTTPSessionManager = request.app.state.mcp_sessions
        limiter: RateLimiter | None = request.app.state.limiter
        if limiter is None:
            await manager.handle_request(scope, receive, send)
            return
        try:
            limiter.acquire(caller.rate_key)
        except RateLimited as error:
            await error_response(request, error)(scope, receive, send)
            return
        holding = True

        def give_back() -> None:
            nonlocal holding
            if holding:
                holding = False
                limiter.release(caller.rate_key)

        async def send_after_release(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                give_back()  # before the client can see the answer and send its next request
            await send(message)

        try:
            await manager.handle_request(scope, receive, send_after_release)
        finally:
            give_back()


class McpRoute(Route):
    """/mcp exactly (a Mount would match only /mcp/..., and redirect /mcp there), for GET, POST and DELETE, the
    methods of streamable HTTP. It records itself as the request's route, as FastAPI's own routes do, so /metrics
    counts its responses under /mcp."""

    def __init__(self) -> None:
        super().__init__(MCP_PATH, McpEndpoint(), methods=["GET", "POST", "DELETE"])

    async def handle(self, scope: Scope, receive: Receive, send: Send) -> None:
        scope["route"] = self
        await super().handle(scope, receive, send)
