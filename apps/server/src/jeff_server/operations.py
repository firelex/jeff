"""Operating the server: which adapters every engine serves (GET /v1/adapters, PUT /v1/adapters/served, POST
/v1/adapters/reload), GET /health and GET /metrics.

Every engine process of a deployment serves the same adapters, so a change goes to every engine (concurrently) and
succeeds only if every engine accepts it. A failure on any engine names each engine that failed and says the engines
may now differ; GET /health then shows each engine's list, and repeating the change brings them back in line.

Routers: an installed router must have every target installed (jeff.routing.check_routers, run at startup). A served
set is checked BEFORE any engine is changed -- serve replaces an engine's whole list with exactly the set asked for,
so the check on that set is the check on the result -- and a set that would leave a router with a target not served
is refused (422 InvalidRequest) with the engines untouched. A reload reads each engine's served file, which only the
engine can read, so its result is checked after the engines have changed: a router left without a target is then
reported (424 NotInstalled, saying the engines have already changed and how to repair it). After every change the
check is re-run on what the engines report (installed(refresh=True)), the same check Jeff runs at startup.

The catalogue is JEFF_CATALOGUE plus the adapter folders under JEFF_ADAPTER_DIRS (jeff_server.catalogue_refresh). A
reload first builds it from the folders as they are now, so a broken folder is refused (500 ManifestError) before any
engine changes; after the engines have reloaded, this worker rebuilds its planner at once, and every other worker
does on its next request that sees the engines' new adapters_version. A new folder that no engine serves does not
change adapters_version, so other workers' GET /v1/adapters does not list it until the next change of version.

On a public server (JEFF_PUBLIC=1) the served adapters stay as started: changing them is refused. On a private one,
changing them (and reading /metrics) needs the owner's credentials -- the API key or a console session -- never a hub
token, which is a customer's (403 NotLicensed); with no credentials configured every caller is the owner.

GET /v1/adapters lists only the catalogue adapters trained for the base the engines serve (jeff.adapter_dirs.
trained_for, the rule an engine refuses an adapter by), each with its description and functions, from entries built
with the catalogue (jeff_server.catalogue_refresh.adapter_entries). An adapter for another base is not listed, and PUT
/v1/adapters/served refuses it as it always has: the engines answer 404 UnknownFunction. It is a 500 EngineError
while any engine is down or the engines serve different adapters (or different bases): it reports one served list
for the deployment, which does not exist then. GET /health is the place to look: it reports each engine.

Health checks, metrics scrapes and GET /v1/adapters wait at most HEALTH_TIMEOUT_S for each engine's reply, not the
prediction timeout (JEFF_ENGINE_TIMEOUT_S): an engine that does not answer in time is reported down, by its socket."""

import asyncio
import math
import os
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path, PurePath
from time import monotonic
from typing import Any, TypeVar

import anyio
import anyio.to_thread
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse

from jeff import contract
from jeff.catalogue import Catalogue
from jeff.errors import (EngineError, InvalidRequest, JeffError, ManifestError, NotInstalled, NotLicensed,
                         UnknownFunction)
from jeff.routing import check_routers
from jeff.socket_engine import SocketEngine

from jeff_server.access import hidden_from, private_ids
from jeff_server.adapter_change import AdapterChangeLock
from jeff_server.auth import ACCOUNT_HEADER, API_KEY_CALLER, V1_DEPENDENCIES, Caller, request_caller
from jeff_server.catalogue_refresh import CatalogueRefresher, build_catalogue
from jeff_server.decide import read_body
from jeff_server.redaction import cut, served_lists_error, visible_ids
from jeff_server.router import EngineRouter

PUBLIC_REFUSAL = "This server is public: its adapters stay as started; change them on the private server"
# How long a health check, a metrics scrape or GET /v1/adapters waits for each engine's reply.
HEALTH_TIMEOUT_S = 2.0
# How long a public server reuses the status it gives callers that are not signed in, so a flood of unsigned health
# checks does not become engine traffic.
PUBLIC_HEALTH_CACHE_S = 1.0


async def owner(request: Request) -> Caller:
    """The caller, who must be the owner: the API key, a console session, or anyone on a server with no credentials
    configured. A hub token, an account key and the playground's key are customers' credentials: 403."""
    caller = request_caller(request)
    if caller.licences is not None:
        raise NotLicensed(f"{request.method} {request.url.path} needs the server owner's credentials (the API key or "
                          f"a console session); the {caller.kind} credential for {caller.subject} is a customer's "
                          "and cannot change or inspect the server")
    return caller


router = APIRouter(dependencies=V1_DEPENDENCIES)
# /metrics: signed in like /v1/*, and the owner's only.
metrics_router = APIRouter(dependencies=[*V1_DEPENDENCIES, Depends(owner)])
health_router = APIRouter()  # /health needs no credentials; a public server answers only the status without them

T = TypeVar("T")


async def in_operations_thread(limiter: anyio.CapacityLimiter, work: Callable[..., T], *args: Any) -> T:
    """`work(*args)` on one of the operations endpoints' own threads (jeff_server.app.OPERATIONS_THREADS), never on
    the pool the predictions use, so slow predictions cannot hold up a health check, a scrape or an adapter change."""
    return await anyio.to_thread.run_sync(partial(work, *args), limiter=limiter)


def engines_of(engine: object) -> list[SocketEngine]:
    """The engine processes behind the server's engine: the operations here talk to each one."""
    if not isinstance(engine, EngineRouter):
        raise EngineError(f"This server's engine is a {type(engine).__name__}, not an EngineRouter over engine "
                          "sockets; adapter changes, health and metrics need the engine processes")
    return engine.engines


def _each(engines: list[SocketEngine], work: Callable[[SocketEngine], T]) -> list[T | JeffError]:
    """`work` on every engine at once; each engine's result or the JeffError it raised, in engine order. Anything
    that is not a JeffError is a bug and is raised."""
    def run(engine: SocketEngine) -> T | JeffError:
        try:
            return work(engine)
        except JeffError as error:
            return error

    with ThreadPoolExecutor(max_workers=len(engines), thread_name_prefix="jeff-operations") as pool:
        return list(pool.map(run, engines))


# --- GET /v1/adapters -----------------------------------------------------------------------------------------------

def adapters_listing(planner: CatalogueRefresher, engine: EngineRouter, caller: Caller) -> dict[str, Any]:
    """Every catalogue adapter trained for the base the engines serve that the caller may use (jeff_server.access: a
    private adapter that does not admit it is left out), marked served or not, with its description and
    functions (jeff_server.catalogue_refresh.adapter_entries, built with the catalogue: nothing is read from a
    manifest here). An adapter trained for another base is left out: no engine can serve it, and PUT
    /v1/adapters/served refuses it as before (404 UnknownFunction from the engines). adapters_version is a number
    that changes whenever any engine's served list changes: the sum of the engines' own adapters_version counters
    (each engine counts its own changes from 0, so with one engine it is that engine's number). The engines are
    asked first, so an engine down or engines that disagree (on what they serve, or on their base) are reported as
    such; the entries are then the planner's, rebuilt first if what the engines answered shows a change."""
    engines = engine.engines
    answers = _each(engines, lambda engine: engine.installed_version(HEALTH_TIMEOUT_S))
    failed = [(engine, answer) for engine, answer in zip(engines, answers, strict=True)
              if isinstance(answer, JeffError)]
    if failed:
        raise EngineError("Could not ask every engine what it serves: " + "; ".join(
            f"{engine.path}: {type(error).__name__}: {error}" for engine, error in failed)
            + ". GET /health shows each engine")
    pairs = [answer for answer in answers if not isinstance(answer, JeffError)]
    served = pairs[0][1]
    if any(adapters != served for _, adapters in pairs[1:]):
        # a caller who is not the owner sees each list cut to the adapters it may see (jeff_server.redaction)
        raise served_lists_error("The engines serve different adapters: ",
                                 [(str(engine.path), adapters) for engine, (_, adapters) in zip(engines, pairs,
                                                                                                strict=True)],
                                 lambda engine, adapters: f"{engine}: {adapters}", "; ",
                                 ". Bring them back in line with PUT /v1/adapters/served")
    engine.base()  # EngineError naming each engine's base when they differ
    planned = planner.planned
    entries = planned.adapters
    assert entries is not None  # built for the engines' base, which they agree on
    hidden = hidden_from(caller, planned.jeff.catalogue)
    return {"adapters": [{**entry, "served": adapter_id in served} for adapter_id, entry in entries
                         if adapter_id not in hidden],
            "adapters_version": sum(version for version, _ in pairs)}


@router.get("/v1/adapters")
async def list_adapters(request: Request) -> dict[str, Any]:
    return await in_operations_thread(request.app.state.operations_limiter, adapters_listing, request.app.state.planner,
                                      _router_engine(request), request_caller(request))


# --- PUT /v1/adapters/served and POST /v1/adapters/reload --------------------------------------------------------

def _check_served_body(body: object) -> dict[str, Any]:
    """The request body against contract/schemas/served_request (not a looser hand-written model): each adapter
    string must be non-empty, as the schema requires, so a served_request.schema.json change (a new constraint)
    is enforced here too without a second, driftable copy of it. JSON nested too deeply for the validator is
    InvalidRequest, as for /v1/decide."""
    try:
        valid = contract.is_valid("served_request", body)
        problem = None if valid else contract.first_problem("served_request", body)
    except RecursionError as error:
        raise InvalidRequest("The request body is JSON nested too deeply to check") from error
    if problem is not None:
        raise InvalidRequest(f"The request body does not match PUT /v1/adapters/served: {problem}")
    assert isinstance(body, dict)  # the schema requires a JSON object
    return body


def _refuse_on_public(request: Request) -> None:
    if request.app.state.settings.public:
        raise InvalidRequest(PUBLIC_REFUSAL)


def _change_everywhere(engines: list[SocketEngine], what: str, change: Callable[[SocketEngine], set[str]]) -> None:
    """`change` on every engine; all must succeed. Every engine refusing the change as unknown (each engine checks
    the adapters it may serve before it changes anything) is 404 UnknownFunction; any other failure is an
    EngineError naming each engine that failed and saying the engines may now differ."""
    answers = _each(engines, change)
    failed = [(engine, answer) for engine, answer in zip(engines, answers, strict=True)
              if isinstance(answer, JeffError)]
    if not failed:
        return
    named = "; ".join(f"{engine.path}: {type(error).__name__}: {error}" for engine, error in failed)
    if len(failed) == len(engines) and all(isinstance(error, UnknownFunction) for _, error in failed):
        raise UnknownFunction(f"No engine changed its adapters ({what}): {named}") from failed[0][1]
    changed = [str(engine.path) for engine, answer in zip(engines, answers, strict=True)
               if not isinstance(answer, JeffError)]
    raise EngineError(f"{what} failed on {len(failed)} of {len(engines)} engine(s): {named}. "
                      f"{'Changed: ' + ', '.join(changed) + '. ' if changed else ''}The engines may now serve "
                      "different adapters; GET /health shows each engine, and repeating the change brings them back "
                      "in line") from failed[0][1]


def _check_routers_after(catalogue: Catalogue, engine: EngineRouter, what: str) -> None:
    """The startup router check, on what the engines now report."""
    try:
        installed = engine.installed(refresh=True)
    except EngineError as error:
        # Every engine accepted the change, but they no longer agree (another API process changed one of them in
        # between, say).
        raise EngineError(f"The {what} was applied on every engine, but afterwards: {error}. The engines may now "
                          "serve different adapters; GET /health shows each engine, and repeating PUT "
                          "/v1/adapters/served brings them back in line") from error
    try:
        check_routers(catalogue, installed)
    except NotInstalled as error:
        raise NotInstalled(error.function_id, f"{what} has already changed the engines, which now serve "
                           f"{', '.join(sorted(installed))}, and a router there routes to it; serve it too, or leave "
                           "the router out, with PUT /v1/adapters/served") from error


def serve_everywhere(planner: CatalogueRefresher, engine: EngineRouter, lock: AdapterChangeLock,
                     refs: list[str]) -> dict[str, Any]:
    """The names are looked up in the catalogue as it is on disk now (build_catalogue: never a rebuild that needs
    the engines to agree, so this change can bring engines that differ back in line); after the change this
    worker's planner is rebuilt at once."""
    catalogue = build_catalogue(planner.settings)
    wanted = list(dict.fromkeys(catalogue.adapter(ref).id for ref in refs))  # aliases to IDs, once each
    try:
        check_routers(catalogue, set(wanted))
    except NotInstalled as error:
        raise InvalidRequest(f"Serving {', '.join(wanted) or 'nothing'} would leave {error.function_id} not "
                             f"served while a router routes to it ({error}); add it, or leave the router out. No "
                             "engine was changed") from error
    with lock.held():
        _change_everywhere(engine.engines, "serve", lambda each: each.serve(wanted))
        _check_routers_after(catalogue, engine, "serve")
        _rebuild_after(planner, "serve")
        # Listed under the lock, so another change (from this process or the factory worker) cannot slip in before
        # this answer.
        return adapters_listing(planner, engine, API_KEY_CALLER)  # an owner-only route: the whole list


def reload_everywhere(planner: CatalogueRefresher, engine: EngineRouter, lock: AdapterChangeLock) -> dict[str, Any]:
    """The catalogue is built from the adapter folders first (ManifestError with no engine changed), then every
    engine reloads, then this worker's planner is rebuilt at once from the folders as they are then."""
    with lock.held():
        catalogue = build_catalogue(planner.settings)
        _change_everywhere(engine.engines, "reload", lambda each: each.reload())
        _check_routers_after(catalogue, engine, "reload")
        _rebuild_after(planner, "reload")
        return adapters_listing(planner, engine, API_KEY_CALLER)  # an owner-only route: the whole list


def _rebuild_after(planner: CatalogueRefresher, what: str) -> None:
    """This worker's planner, rebuilt once every engine has changed. A failure keeps the old planner and says the
    engines have already changed: EngineError when the engines could not be asked, otherwise ManifestError."""
    try:
        planner.refresh()
    except JeffError as error:
        failure = EngineError if isinstance(error, EngineError) else ManifestError
        raise failure(f"The {what} was applied on every engine, but this server could not rebuild its catalogue "
                      f"afterwards: {type(error).__name__}: {error}. It keeps answering from the catalogue it had; "
                      f"repeating the {what} tries again") from error


def _router_engine(request: Request) -> EngineRouter:
    engine = request.app.state.engine
    engines_of(engine)
    assert isinstance(engine, EngineRouter)
    return engine


@router.put("/v1/adapters/served", dependencies=[Depends(owner)])
async def put_served(request: Request) -> dict[str, Any]:
    _refuse_on_public(request)
    body = _check_served_body(await read_body(request))
    return await in_operations_thread(request.app.state.operations_limiter, serve_everywhere,
                                      request.app.state.planner, _router_engine(request),
                                      request.app.state.adapter_change_lock, body["adapters"])


@router.post("/v1/adapters/reload", dependencies=[Depends(owner)])
async def post_reload(request: Request) -> dict[str, Any]:
    _refuse_on_public(request)
    return await in_operations_thread(request.app.state.operations_limiter, reload_everywhere,
                                      request.app.state.planner, _router_engine(request),
                                      request.app.state.adapter_change_lock)


# --- GET /health ---------------------------------------------------------------------------------------------------

def _engine_health(engine: SocketEngine, answer: dict[str, Any] | JeffError) -> dict[str, Any]:
    if isinstance(answer, JeffError):
        # The engine could not be reached: its process died (the connection closed) or did not answer within
        # HEALTH_TIMEOUT_S.
        return {"socket": str(engine.path), "status": "down", "backend": None, "adapters_version": None,
                "adapters": None, "cause": f"{type(answer).__name__}: {answer}"}
    entry = {"socket": str(engine.path), "status": answer["status"], "backend": answer["backend"],
             "adapters_version": answer["adapters_version"], "adapters": sorted(answer["adapters"])}
    if "cause" in answer:
        entry["cause"] = answer["cause"]
    return entry


def _legacy_fields(answers: list[dict[str, Any] | JeffError], authentication: bool) -> dict[str, Any]:
    """jeff-server's health fields, which its client's health() requires, with jeff-server's meanings: checkpoint,
    where the base was loaded from, and max_options, the most options a choice question may have on the base (from
    the first ready engine, in JEFF_ENGINE_SOCKETS order: one deployment serves one base, and an adapter's own
    limit is in its manifest); authentication, whether /v1 needs credentials; modalities, what every ready engine
    reads ("text" on MLX, "text" and "image" otherwise). With no engine ready the three engine fields are null."""
    ready = [answer for answer in answers if not isinstance(answer, JeffError) and answer["status"] == "ready"]
    fields: dict[str, Any] = {"checkpoint": None, "max_options": None, "authentication": authentication,
                              "modalities": None}
    if ready:
        fields["checkpoint"] = ready[0]["base"]["checkpoint"]
        fields["max_options"] = ready[0]["base"]["max_options"]
        fields["modalities"] = ["text"] if any(answer["backend"] == "mlx" for answer in ready) else ["text", "image"]
    return fields


def health_report(engines: list[SocketEngine], model_name: str, authentication: bool) -> dict[str, Any]:
    """ready: every engine is ready and they serve the same adapters; degraded: some engine is down or broken while
    the ready ones serve the same adapters (requests routed to the ready ones are answered); broken: no engine is
    ready, or the ready engines serve different adapters -- then the deployment has no one served list: only
    requests for adapters every engine serves are answered, GET /v1/adapters fails, and it is broken, with a cause
    saying so (a factory publish changes the engines one at a time, so this can show for a moment)."""
    answers = _each(engines, lambda engine: engine.health(HEALTH_TIMEOUT_S))
    entries = [_engine_health(engine, answer) for engine, answer in zip(engines, answers, strict=True)]
    ready = [entry for entry in entries if entry["status"] == "ready"]
    served = {tuple(entry["adapters"]) for entry in ready}
    report: dict[str, Any] = {"status": "ready", "engines": entries, "model": model_name,
                              **_legacy_fields(answers, authentication)}
    if not ready:
        report.update(status="broken", cause="No engine is ready")
    elif len(served) > 1:
        report.update(status="broken", cause="The engines disagree on which adapters they serve, so only requests "
                      "for adapters every engine serves are answered; repeating PUT /v1/adapters/served brings them "
                      "back in line")
    elif len(ready) < len(entries):
        report["status"] = "degraded"
    return report


class PublicHealth:
    """The status a public server gives callers that are not signed in, reused for PUBLIC_HEALTH_CACHE_S. The lock
    makes concurrent unsigned checks wait for one report rather than each asking the engines."""

    def __init__(self) -> None:
        self.lock = asyncio.Lock()
        self.status: str | None = None
        self.until = 0.0

    async def get(self, limiter: anyio.CapacityLimiter, engines: list[SocketEngine], model_name: str) -> str:
        async with self.lock:
            if self.status is None or monotonic() >= self.until:
                # A public server always needs credentials (ServerSettings refuses one without).
                report = await in_operations_thread(limiter, health_report, engines, model_name, True)
                self.status, self.until = report["status"], monotonic() + PUBLIC_HEALTH_CACHE_S
            return self.status


@health_router.get("/health", response_model=None)
async def health(request: Request) -> dict[str, Any] | JSONResponse:
    settings = request.app.state.settings
    engines = engines_of(request.app.state.engine)
    authorization = request.headers.get("authorization")
    if settings.public:
        if authorization is None:
            public_health: PublicHealth = request.app.state.public_health
            return {"status": await public_health.get(request.app.state.operations_limiter, engines,
                                                      settings.model_name)}
    owner, caller = _health_caller(request, authorization)
    report = await in_operations_thread(request.app.state.operations_limiter, health_report, engines,
                                        settings.model_name, request.app.state.auth.configured)
    if not owner:
        catalogue = (await request.app.state.planner.ready()).catalogue
        _as_seen_by(report, caller, catalogue, settings.engine_sockets)
    return report


def _health_caller(request: Request, authorization: str | None) -> tuple[bool, Caller | None]:
    """Whether this request is the owner's, and its caller (None: not signed in). On a server with no credentials
    every caller is the owner; with credentials configured, a request without them is not, and credentials given
    must be valid: 401 otherwise, on every server (a server that is not public used to ignore them here)."""
    auth = request.app.state.auth
    if not auth.configured:
        return True, None
    if authorization is None:
        return False, None
    caller = auth.caller(authorization, request.headers.get(ACCOUNT_HEADER))
    return caller.licences is None, caller


def _as_seen_by(report: dict[str, Any], caller: Caller | None, catalogue: Catalogue,
                sockets: Sequence[Path]) -> None:
    """The health report as a caller who is not the owner may see it, changed in place, worded as the errors such a
    caller gets are (jeff_server.redaction): each engine's served list without the private adapters (jeff_server.access)
    it may not use (every private one for a request not signed in), each engine's socket path replaced by
    "engine <n>", its place in JEFF_ENGINE_SOCKETS, and in each cause the socket paths replaced and the lists of
    loaded adapters cut the same way; the checkpoint is cut to its last part (the base's name, without where the
    server keeps it)."""
    hidden = private_ids(catalogue) if caller is None else hidden_from(caller, catalogue)
    visible = visible_ids(caller, catalogue)
    for place, entry in enumerate(report["engines"]):
        entry["socket"] = f"engine {place}"
        if entry["adapters"] is not None:
            entry["adapters"] = [adapter_id for adapter_id in entry["adapters"] if adapter_id not in hidden]
        if "cause" in entry:
            entry["cause"] = cut(entry["cause"], lambda: visible, lambda: sockets)
    if "cause" in report:
        report["cause"] = cut(report["cause"], lambda: visible, lambda: sockets)
    if report["checkpoint"] is not None:
        report["checkpoint"] = PurePath(report["checkpoint"]).name


# --- GET /metrics --------------------------------------------------------------------------------------------------

class RequestCounts:
    """jeff_http_requests_total: responses by route (the matched route's path template) and status, and each
    /v1/channel connection, once it has ended, by its WebSocket close code (jeff_server.channel)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counts: dict[tuple[str, int], int] = {}

    def add(self, route: str, status: int) -> None:
        with self._lock:
            self._counts[(route, status)] = self._counts.get((route, status), 0) + 1

    def snapshot(self) -> dict[tuple[str, int], int]:
        with self._lock:
            return dict(self._counts)


def _label(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\"", "\\\"").replace("\n", "\\n")


def _number(value: float | int | None) -> str:
    if value is None:
        return "NaN"  # no batch has run yet, so there is no model time to report
    if isinstance(value, float) and math.isnan(value):
        return "NaN"
    return repr(float(value)) if isinstance(value, float) else str(value)


# (metric name, Prometheus type, help, key in the engine's stats reply)
ENGINE_METRICS = [
    ("jeff_queue_rows", "gauge", "Rows waiting in the engine's queue.", "queued_rows"),
    ("jeff_batches_total", "counter", "Batches the engine has run.", "batches"),
    ("jeff_rows_total", "counter", "Rows the engine has answered.", "rows"),
    ("jeff_busy_total", "counter", "Requests the engine refused because its queue was full.", "busy"),
    ("jeff_deadline_exceeded_total", "counter", "Requests whose deadline passed before they ran.",
     "deadline_exceeded"),
]


def metrics_text(engines: list[SocketEngine], counts: RequestCounts) -> str:
    """Prometheus text format. Per engine (label socket): whether the API reached it (jeff_engine_up), and the
    counters the engine's scheduler keeps (the socket's stats reply: queue, batches, rows, busy, deadlines, and the
    model time per batch at the median and 99th percentile over its last 1000 batches). An engine that cannot be
    reached has jeff_engine_up 0 and no other samples from its stats reply. jeff_engine_reconnects_total, counted by
    this API process's SocketEngine (so present for an engine that is down too), is how many times the API has
    reconnected to that engine after its connection closed: a restarted engine process. Per route: HTTP responses by status (and, under
    /v1/channel, each channel connection once it has ended, by its WebSocket close code), counted by this API
    worker process alone, so each of those series carries worker=<this process id>: with `serve --workers N`, a
    scrape reaches one worker, and each worker's counts are distinct series (sum them over worker)."""
    answers = _each(engines, lambda engine: engine.stats(HEALTH_TIMEOUT_S))
    lines: list[str] = []

    def family(name: str, kind: str, help_text: str, samples: list[tuple[str, str]]) -> None:
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        lines.extend(f"{name}{{{labels}}} {value}" for labels, value in samples)

    socket_label = {engine: f"socket=\"{_label(str(engine.path))}\"" for engine in engines}
    reached = [(engine, answer) for engine, answer in zip(engines, answers, strict=True)
               if not isinstance(answer, JeffError)]
    family("jeff_engine_up", "gauge", "1 when the API reached the engine, 0 when it could not.",
           [(socket_label[engine], "0" if isinstance(answer, JeffError) else "1")
            for engine, answer in zip(engines, answers, strict=True)])
    family("jeff_engine_reconnects_total", "counter", "Times the API reconnected to the engine after its "
           "connection closed (the engine process restarted).",
           [(socket_label[engine], str(engine.reconnects)) for engine in engines])
    for name, kind, help_text, key in ENGINE_METRICS:
        family(name, kind, help_text, [(socket_label[engine], _number(stats[key])) for engine, stats in reached])
    family("jeff_model_ms", "gauge", "Model time per batch in milliseconds, at a quantile of recent batches.",
           [(f"{socket_label[engine]},quantile=\"{quantile}\"", _number(stats[key]))
            for engine, stats in reached for quantile, key in (("0.5", "model_ms_p50"), ("0.99", "model_ms_p99"))])
    worker = os.getpid()
    family("jeff_http_requests_total", "counter", "HTTP responses by route and status, and /v1/channel connections "
           "by WebSocket close code, counted by one API worker.",
           [(f"route=\"{_label(route)}\",status=\"{status}\",worker=\"{worker}\"", str(count))
            for (route, status), count in sorted(counts.snapshot().items())])
    return "\n".join(lines) + "\n"


@metrics_router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request) -> PlainTextResponse:
    text = await in_operations_thread(request.app.state.operations_limiter, metrics_text,
                                      engines_of(request.app.state.engine), request.app.state.request_counts)
    return PlainTextResponse(text, media_type="text/plain; version=0.0.4; charset=utf-8")
