"""Builds the FastAPI app: settings in, every error answered through the catalogue (jeff_server.errors), every
response stamped with an x-request-id, credentials, licences and rate limits (jeff_server.auth), the engine wired in
for the planner to use, the operations endpoints (jeff_server.operations: adapters, health, metrics), the committed
OpenAPI description served at GET /openapi.json (jeff_server.openapi), every response counted by route and status
for /metrics, every /v1 and /mcp request appended to the access log when JEFF_ACCESS_LOG is set
(jeff_server.access_log), the low-latency channel, a WebSocket at /v1/channel (jeff_server.channel, which logs and
counts its own connections), the MCP
tools at /mcp (jeff_server.mcp_mount), whose session manager runs inside the app's lifespan, and the adapter
factory's routes at /v1/factory (jeff_server.factory.routes), which queue jobs for the factory worker."""

import asyncio
import logging
import secrets
import threading
import time
from collections.abc import AsyncIterator, MutableMapping
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import UTC, datetime
from typing import Any

import anyio.to_thread
import httpx
from fastapi import FastAPI
from starlette.datastructures import Headers
from starlette.types import Receive, Scope, Send

from jeff.engine import Engine
from jeff.errors import InvalidRequest

from jeff_server.access_log import AccessLog, open_geoip
from jeff_server.accounts import HTTP_TIMEOUT_S, TICK_S, Accounts, AccountsError, run_every_minute
from jeff_server.adapter_change import AdapterChangeLock
from jeff_server.auth import Authenticator, Caller, RateLimiter, login_router
from jeff_server.catalogue_refresh import CatalogueRefresher
from jeff_server.channel import Connection
from jeff_server.channel import router as channel_router
from jeff_server.console import html_router as console_html_router
from jeff_server.console import router as console_router
from jeff_server.decide import load_graphs
from jeff_server.decide import router as decide_router
from jeff_server.errors import REQUEST_ID_STATE_KEY, install
from jeff_server.factory.routes import FactoryState
from jeff_server.factory.routes import router as factory_router
from jeff_server.mcp_mount import MCP_PATH, McpRoute, session_manager
from jeff_server.openapi import load_document
from jeff_server.openapi import router as openapi_router
from jeff_server.operations import PublicHealth, RequestCounts, health_router, metrics_router
from jeff_server.operations import router as operations_router
from jeff_server.router import EngineRouter
from jeff_server.settings import ServerSettings
from jeff_server.systemone import router as systemone_router
from jeff_server.usage import UsageBook
from jeff_server.users import SignInGuard

log = logging.getLogger("jeff_server")

REQUEST_ID_HEADER = "x-request-id"
# Threads for the calls that wait on the engine's predictions (/v1/decide, /v1/systemone, /v1/functions,
# /v1/models): anyio's default thread pool, sized here explicitly. 40 is anyio's own default, kept: each thread
# mostly waits on an engine socket, and the engine's queue, not this pool, bounds the work in flight.
DECIDE_THREADS = 40
# Threads of their own for the operations endpoints (/health, /metrics, GET /v1/adapters, PUT /v1/adapters/served,
# POST /v1/adapters/reload), so slow predictions filling DECIDE_THREADS never hold up a health check or a scrape.
OPERATIONS_THREADS = 8


class _Application(FastAPI):
    """A FastAPI app whose ASGI __call__ sets a request id in the ASGI scope's state and stamps x-request-id on
    every response it sends over the wire -- including the ones Starlette's ServerErrorMiddleware sends directly
    for an exception that has no other registered handler. That path sits outside anything added with
    add_middleware(): Starlette special-cases a handler registered for the bare Exception class and hoists it
    into ServerErrorMiddleware, the very outermost layer (see build_middleware_stack in starlette/applications.py,
    which always puts ServerErrorMiddleware first and treats {500, Exception} handlers as its `handler` kwarg
    rather than as an entry in the inner ExceptionMiddleware's table). When that middleware catches an exception,
    it builds the error response and sends it using the exact `send` callable *it* was given -- which, since
    ServerErrorMiddleware is the first thing Starlette's own __call__ invokes, is only ever wrapped by code that
    runs before Starlette's own __call__ even starts. Overriding __call__ here is that one place."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await super().__call__(scope, receive, send)
            return

        request_id = secrets.token_hex(16)
        scope.setdefault("state", {})[REQUEST_ID_STATE_KEY] = request_id
        sent: dict[str, int] = {}

        async def send_with_request_id(message: MutableMapping[str, Any]) -> None:
            if message["type"] == "http.response.start":
                sent["status"] = int(message["status"])
                headers = [*message.get("headers", []),
                           (REQUEST_ID_HEADER.encode("latin-1"), request_id.encode("latin-1"))]
                message = {**message, "headers": headers}
            await send(message)

        try:
            await super().__call__(scope, receive, send_with_request_id)
        finally:
            # Counted and logged here, after the response, so every response is: the ones handlers send and the
            # ones ServerErrorMiddleware sends for an unexpected exception (see above). A request that ended
            # before any response started (the client went away) has no status and is neither counted nor logged.
            if "status" in sent:
                self._after(scope, sent["status"])

    def _after(self, scope: Scope, status: int) -> None:
        route = scope.get("route")
        # The matched route's path template (never the raw path, which would make a label per URL).
        self.state.request_counts.add(route.path if route is not None else "unmatched", status)
        access_log: AccessLog | None = self.state.access_log
        path: str = scope["path"]
        if access_log is None or not (path.startswith("/v1/") or path == MCP_PATH):
            return
        caller: Caller | None = scope["state"].get("caller")  # set by jeff_server.auth.authenticated
        client = scope.get("client")
        access_log.record_request(
            "api", user=caller.log_user if caller is not None else None,
            headers=Headers(scope=scope), peer=client[0] if client else None, path=path, status=status,
            detail=f"{scope['method']} {caller.kind}" if caller is not None else str(scope["method"]))


def create_app(settings: ServerSettings, *, engine: Engine | None = None,
               accounts_http: httpx.Client | None = None) -> FastAPI:
    """accounts_http lets tests answer for the hub with an httpx.MockTransport; without it, accounts mode builds its
    own client (_hub_client). engine lets tests pass a FakeEngine (or a hand-built router), which stays the caller's
    to close; without
    it, the app builds an EngineRouter from settings.engine_sockets at startup, one SocketEngine per socket, and
    closes those connections at shutdown, or at once if startup fails after opening any. Startup also builds the
    planner (jeff_server.catalogue_refresh: one jeff.Jeff over that engine, settings.catalogue and the adapter
    folders under settings.adapter_dirs, rebuilt when the engines' adapters change), registers the graphs in
    settings.graphs and sizes the thread pools (DECIDE_THREADS, OPERATIONS_THREADS) and runs the MCP session
    manager until shutdown. In accounts mode, startup also starts the minute loop that fetches the key list and
    reports usage (jeff_server.accounts). Shutdown runs in a fixed order, each step run even when an earlier one
    fails: the channel's running requests finish (at most the engine timeout; contextlib.AsyncExitStack, with
    callbacks pushed in startup order so they unwind in the opposite one), then the engines close, then -- last, so
    no channel call or engine close ever races it -- accounts mode's minute loop is stopped and awaited (an
    anyio.CancelScope, not a bare asyncio.Task.cancel(): cancelling a task directly cancels whatever it is
    awaiting, including the future inside an in-flight anyio.to_thread.run_sync, so it would not actually wait for
    that step to finish; an anyio CancelScope's cancellation is the one that respects to_thread.run_sync's shield),
    a last usage report is sent, and the hub client and the usage book are closed. The accounts step runs whenever
    accounts mode is on, even if nothing after it in startup was reached (a broken catalogue or graphs file), so
    the usage book and its lock are never left open on a failed start."""

    @asynccontextmanager
    async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
        anyio.to_thread.current_default_thread_limiter().total_tokens = DECIDE_THREADS
        app.state.operations_limiter = anyio.CapacityLimiter(OPERATIONS_THREADS)

        @asynccontextmanager
        async def _serve() -> AsyncIterator[None]:
            """Everything but accounts mode's minute loop: the engines, the planner, the graphs, the MCP session
            manager, and -- at shutdown, before the engines close -- waiting for the channel's running requests."""
            async with AsyncExitStack() as stack:
                owned = _build_engine_router(settings) if engine is None else None
                # Pushed first, so it is the last of this pair to run: a channel call still running needs its
                # engine.
                if owned is not None:
                    stack.callback(owned.close)
                app.state.engine = owned if owned is not None else engine
                app.state.planner = CatalogueRefresher(settings, app.state.engine)
                app.state.graphs = load_graphs(settings.graphs)
                app.state.mcp_sessions = session_manager(app.state)
                # Pushed last, so it is the first to run.
                stack.push_async_callback(_finish_channels, app.state.channels, settings.engine_timeout_s)
                async with app.state.mcp_sessions.run():
                    yield

        if app.state.accounts is None:
            async with _serve():
                yield
            return

        # The loop runs inside its own CancelScope, in a task group that spans _serve(): stopping it this way (as
        # opposed to a bare asyncio.Task.cancel(), see create_app's docstring) actually waits for a refresh or
        # report already running on its worker thread to finish, rather than abandoning it mid-flight. Whatever
        # _serve() does (including failing before it is ever reached, a startup failure) the outermost `finally`
        # below still sends the last report and closes the hub client and the usage book.
        loop_scope = anyio.CancelScope()
        serve_error: BaseException | None = None
        loop_error: BaseException | None = None

        async def _run_loop() -> None:
            nonlocal loop_error
            try:
                with loop_scope:
                    await run_every_minute(app.state.accounts, TICK_S, app.state.operations_limiter)
            except BaseException as error:  # noqa: BLE001 - stored, never left to reach the task group; see below
                # A bug in run_every_minute (already logged there by log.exception) must never escape into the
                # task group: a task group cancels its whole cancel_scope -- which also holds the host task, here
                # suspended at _serve()'s yield -- when ANY child task raises. That would tear the server down
                # (finish the channels, close the engines, close the book and the client) while the ASGI server
                # keeps accepting and serving requests on this same, now half-closed app. The loop simply dies
                # here instead: the key list goes stale on its own schedule and account keys are refused by name.
                # Re-raised, bare, only once _serve() has shut down for real (see below).
                loop_error = error

        try:
            async with anyio.create_task_group() as loop_group:
                loop_group.start_soon(_run_loop)
                try:
                    async with _serve():
                        yield
                except BaseException as error:  # noqa: BLE001 - re-raised below, never swallowed; see the comment
                    # Caught here, not let through the task group's own body, so a startup failure (or any other
                    # error from _serve()) is re-raised in its own right below, never wrapped in the
                    # BaseExceptionGroup a task group would otherwise raise for anything that crosses its body.
                    serve_error = error
                finally:
                    loop_scope.cancel()
            # loop_group has exited: neither _run_loop nor the body above ever let an exception reach it, so it
            # never cancelled the host task, and this exits cleanly, with _run_loop's own bug (if any) in
            # loop_error.
        finally:
            try:
                await _last_report(app.state.accounts, app.state.operations_limiter)
            except BaseException as report_error:  # noqa: BLE001 - chained onto whichever error is reported below
                if serve_error is not None:
                    raise serve_error from report_error
                if loop_error is not None:
                    raise loop_error from report_error
                raise
        if serve_error is not None:
            if loop_error is not None:
                raise serve_error from loop_error
            raise serve_error
        if loop_error is not None:
            raise loop_error

    # FastAPI's own interactive docs describe themselves, not the contract; /openapi.json serves
    # contract/openapi.yaml instead (jeff_server.openapi).
    app = _Application(docs_url=None, redoc_url=None, openapi_url=None, lifespan=_lifespan)
    app.state.settings = settings
    app.state.limiter = RateLimiter(*settings.rate_limit) if settings.rate_limit is not None else None
    app.state.accounts = _accounts(settings, app.state.limiter, accounts_http)
    # Credentials are loaded now, so a missing or malformed key or users file stops the server before it starts.
    app.state.auth = Authenticator.from_settings(settings, app.state.accounts)
    app.state.sign_in_guard = SignInGuard()
    app.state.access_log = _access_log(settings)
    app.state.request_counts = RequestCounts()
    app.state.public_health = PublicHealth()
    # One adapter change at a time, from this API process and the factory worker (jeff_server.adapter_change).
    app.state.adapter_change_lock = AdapterChangeLock(settings.adapter_dirs)
    app.state.channels = set()  # the open /v1/channel connections (jeff_server.channel.Connection)
    # The factory's settings and its runs on disk (jeff_server.factory.routes); the API only queues build steps.
    app.state.factory = FactoryState(settings.factory)
    # Parsed from YAML to JSON once, now, like every other piece of state above: a malformed committed file (run
    # scripts/build_openapi.py) stops the server before it starts, rather than on the first request to /openapi.json.
    app.state.openapi_document = load_document()
    install(app)
    app.include_router(login_router)
    app.include_router(decide_router)
    app.include_router(channel_router)
    app.include_router(systemone_router)
    app.include_router(operations_router)
    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(openapi_router)
    app.include_router(console_router)
    app.include_router(console_html_router)
    app.include_router(factory_router)
    # A plain Starlette route to the MCP SDK's ASGI app, which checks credentials and the rate limiter itself
    # (jeff_server.mcp_mount): FastAPI dependencies do not run for it.
    app.router.routes.append(McpRoute())
    return app


def _hub_client() -> httpx.Client:
    """The client for the hub's internal API: certificates checked (the shared secret travels in every request),
    redirects never followed (a redirect would send that request somewhere other than JEFF_ACCOUNTS_URL; one is
    refused as an unexpected answer), and a timeout so a hung hub cannot hold the minute's work."""
    return httpx.Client(timeout=HTTP_TIMEOUT_S, verify=True, follow_redirects=False)


def _accounts(settings: ServerSettings, limiter: RateLimiter | None,
              http: httpx.Client | None) -> Accounts | None:
    """Accounts mode's state; the usage file is read (and compacted) now, so a broken one stops the server before
    it starts."""
    if settings.accounts is None:
        return None
    if limiter is None:
        raise InvalidRequest("Accounts mode needs JEFF_RATE_LIMIT")  # settings refuse this first; kept for tests
    return Accounts(settings.accounts, UsageBook(settings.accounts.usage_file), limiter,
                    http if http is not None else _hub_client(), time.monotonic, lambda: datetime.now(UTC))


async def _last_report(accounts: Accounts, limiter: anyio.CapacityLimiter) -> None:
    """Accounts mode's last shutdown step, called once the minute loop (jeff_server.app._lifespan's loop_scope and
    task group) has been stopped and fully awaited: one more usage report, so the hub has the totals up to the
    end. The one failure Accounts.report raises on its own, AccountsError, is logged (the totals stay in
    JEFF_USAGE_FILE and go with the next start's first report). Anything else is a server bug and is left to
    propagate: what keeps the rest of shutdown running regardless -- closing the engines, finishing the channels --
    is the structure around this call (the outer try/finally in _lifespan), not a catch here. Then the client and
    the book are closed, whether or not accounts mode ever got as far as starting the loop (a startup failure)."""
    try:
        await anyio.to_thread.run_sync(accounts.report, limiter=limiter)
    except AccountsError as error:
        log.error("Accounts mode: the last usage report failed: %s; the totals stay in %s", error,
                  accounts.settings.usage_file)
    finally:
        accounts.close()


async def _finish_channels(channels: set[Connection], timeout_s: float) -> None:
    """At shutdown, before the engines close: waits for the requests still running on any /v1/channel connection
    (jeff_server.channel.Connection.tasks; a connection whose client has gone keeps a task whose engine call is
    running until that call returns), at most timeout_s (the engine timeout, which bounds each engine call). Then
    cancels the ones left and logs how many there were."""
    tasks = {task for connection in list(channels) for task in connection.tasks}
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout_s)
    if pending:
        log.error("Shutting down with %d channel request(s) still running after %gs (the engine timeout); "
                  "cancelling them", len(pending), timeout_s)
        for task in pending:
            task.cancel()


def _access_log(settings: ServerSettings) -> AccessLog | None:
    """JEFF_ACCESS_LOG unset: nothing is logged. The GeoIP database is opened now, so a missing one stops the
    server before it starts."""
    if settings.access_log is None:
        if settings.geoip_db is not None:
            raise InvalidRequest("JEFF_GEOIP_DB is set but JEFF_ACCESS_LOG is not; the location database is only "
                                 "used by the access log, so set JEFF_ACCESS_LOG too, or unset JEFF_GEOIP_DB")
        return None
    geoip = open_geoip(settings.geoip_db) if settings.geoip_db is not None else None
    return AccessLog(settings.access_log, admins=settings.admins, geoip=geoip,
                     server="public" if settings.public else "private")


def _build_engine_router(settings: ServerSettings) -> EngineRouter:
    """One SocketEngine per socket, in order. If one cannot connect, the ones already opened are closed and the
    error is raised."""
    from jeff.socket_engine import SocketEngine

    engines: list[SocketEngine] = []
    try:
        for path in settings.engine_sockets:
            engines.append(SocketEngine(path, timeout_s=settings.engine_timeout_s))
    except BaseException:
        for opened in engines:
            opened.close()
        raise
    return EngineRouter(engines)
