"""The planner every request runs on -- one jeff.Jeff over the server's engine and its catalogue -- and its rebuild when
the engines' adapters change.

The catalogue is JEFF_CATALOGUE (or the catalogue shipped with jeff) plus every adapter folder under JEFF_ADAPTER_DIRS
(jeff.adapter_dirs.scan, the scan an engine started with --adapters-dir makes). A folder whose adapter ID is also in
the catalogue replaces the catalogue's manifest, because the engine serves that adapter from the folder
(contract/socket.md, "Adapter folders"); a found alias that is another adapter's name is a ManifestError.

Every engine reply carries the engine's adapters_version, which each SocketEngine keeps (SocketEngine.adapters_version).
Their sum is the deployment's version, as GET /v1/adapters reports it; it changes whenever a running engine's
adapters change. A restarted engine counts from 0 again, so its version alone may not move (an engine that never
changed, restarted with another folder or served file, is at 0 before and after), and sums can cancel across
engines; but each SocketEngine also counts its reconnects (SocketEngine.reconnects), which only grow. The planner is
kept with the key it was built at -- (the sum of the versions, the sum of the reconnects) -- and each request compares
that with the key now known: plain attribute reads and one tuple comparison, no file read, no lock and no engine
call while nothing has changed. When they differ the catalogue and the planner are rebuilt over the same engine, on
a worker thread, and swapped in; a request already running keeps the planner it started with. An engine that has
just reconnected and whose version is not known yet (-1) does not by itself cause a rebuild.

A rebuild that a change of key started asks every engine what it serves. While they disagree -- the factory's publish
changes the engines one at a time, so for a moment one serves the new adapter and another does not -- it keeps the
old planner, recorded at the key now known, and the request goes on with it: the next change of key (the last
engine's change, seen in its next reply) tries again. Meanwhile a call's served check is what every engine serves
(jeff_server.router.EngineRouter.installed), so the functions every engine serves keep answering.

The worker that runs POST /v1/adapters/reload rebuilds at once (jeff_server.operations.reload_everywhere). Every other
worker learns the new version from the next engine reply it reads, with no extra engine call, and rebuilds on the
request after that: the request whose reply brought the news, one per worker per change, was planned with the old
manifests. A request refused for a name the catalogue does not have (an unknown function, or an unknown model on
/v1/systemone) never reaches an engine, so it asks the engines' versions itself, at most once a second per worker,
and is planned once more if the catalogue is rebuilt (CatalogueRefresher.retry_unknown)."""

import logging
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any, NamedTuple, TypeVar

import anyio.to_thread

from jeff.adapter_dirs import scan, trained_for
from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.engine import Engine
from jeff.errors import EngineError, JeffError, ManifestError, UnknownFunction
from jeff.manifest import Manifest, has_threshold
from jeff.mcp import check_tool_names

from jeff_server.access import check_access, check_no_wider_access, check_private_adapters, describe_private
from jeff_server.redaction import reword
from jeff_server.router import EngineRouter
from jeff_server.settings import ServerSettings
from jeff_server.systemone import check_model_name

log = logging.getLogger("jeff_server")


def build_catalogue(settings: ServerSettings) -> Catalogue:
    """The catalogue as it is on disk now, checked as at startup: JEFF_MODEL_NAME names none of its adapters, no
    two of its functions share an MCP tool name, and, when JEFF_PRIVATE_ADAPTERS is set, it lists exactly the
    adapters with an access field. ManifestError (or ContractError for the tool names) for any problem
    in a folder or between the folders and the catalogue."""
    catalogue = catalogue_with_folders(settings.catalogue, settings.adapter_dirs)
    check_model_name(catalogue, settings.model_name)
    check_private_adapters(catalogue, settings.private_adapters)
    return catalogue


def catalogue_with_folders(catalogue_dir: Path | None, adapter_dirs: Sequence[Path],
                           extra: Iterable[Manifest] = ()) -> Catalogue:
    """`catalogue_dir` (None: the catalogue shipped with jeff) with every adapter folder under `adapter_dirs`, then
    `extra`, replacing or adding manifests, no two functions sharing an MCP tool name, and every adapter's access
    valid, with no router routing to an adapter of narrower access (jeff_server.access.check_access): the catalogue the API
    builds, also built by the factory worker when it publishes (`extra`: the manifest it is about to move into
    place, so a clash is found before any engine can see the folder; one whose ID a folder already has is a
    ManifestError naming that folder)."""
    shipped = Catalogue.from_dir(catalogue_dir) if catalogue_dir is not None else Catalogue.shipped()
    found = scan(adapter_dirs)
    extra = list(extra)
    taken = [f"{manifest.id} ({found[manifest.id].folder})" for manifest in extra if manifest.id in found]
    if taken:
        raise ManifestError(f"An adapter folder already holds {', '.join(taken)}")
    replacing = [*(item.manifest for item in found.values()), *extra]
    check_no_wider_access(shipped, replacing)
    catalogue = shipped.replacing(replacing)
    check_tool_names(catalogue)
    check_descriptions(catalogue)
    check_access(catalogue)
    return catalogue


def description_of(manifest: Manifest) -> str | None:
    """The adapter's one-line description as its manifest writes it (hub.summary), or None when it has none."""
    hub = manifest.raw.get("hub")
    summary = hub.get("summary") if isinstance(hub, dict) else None
    return summary if isinstance(summary, str) and summary.strip() else None


def check_descriptions(catalogue: Catalogue) -> None:
    """Every adapter the server loads has a one-line description (its manifest's hub.summary, a non-empty string):
    GET /v1/adapters lists it as written. ManifestError naming each manifest without one -- the description is never
    made up or left empty."""
    missing = [f"{manifest.id} ({manifest.path})" for manifest in catalogue.manifests()
               if description_of(manifest) is None]
    if missing:
        raise ManifestError(f"No one-line description (hub.summary, a non-empty string) in the manifest of "
                            f"{', '.join(missing)}; GET /v1/adapters lists each adapter with it. Add hub: {{summary: "
                            "<one sentence saying what the adapter decides>}")


def adapter_entries(catalogue: Catalogue, base: tuple[str, str], format_id: str | None
                    ) -> tuple[tuple[str, dict[str, Any]], ...]:
    """GET /v1/adapters' entries, by adapter ID, each without its "served" flag: every catalogue adapter trained for
    `base` (jeff.adapter_dirs.trained_for, the rule an engine refuses an adapter by, so an adapter an engine could
    serve is always listed and one it never could is not), in catalogue order, with its ID, aliases, version,
    one-line description (hub.summary, as written) and functions -- each one's ID, interface and whether it has a
    threshold for a base of `format_id` (jeff.manifest.has_threshold). Built once per catalogue build, so a listing
    reads no manifest."""
    return tuple(
        (manifest.id, {"id": manifest.id, "aliases": list(manifest.aliases), "version": manifest.version,
                       "description": description_of(manifest),
                       "functions": [{"id": function.id, "interface": function.interface,
                                      "calibrated": has_threshold(function, format_id)}
                                     for function in manifest.functions.values()]})
        for manifest in catalogue.manifests() if trained_for(manifest, base))


# The fewest seconds between two checks of the engines' versions after an unknown function (after_unknown), per
# worker: a caller sending names nobody serves never turns them into more than one engine call a second.
UNKNOWN_CHECK_INTERVAL_S = 1.0

T = TypeVar("T")


class Planned(NamedTuple):
    """What a catalogue build resolves: the key it was built at ((adapters_version sum, reconnects sum)), the
    planner, and GET /v1/adapters' entries (adapter_entries) for the base the engines serve -- None when the engine
    is not an EngineRouter or its engines serve different bases (GET /v1/adapters then refuses, naming them)."""
    key: tuple[int, int]
    jeff: Jeff
    adapters: tuple[tuple[str, dict[str, Any]], ...] | None


class CatalogueRefresher:
    """Holds a Planned: ((adapters_version sum, reconnects sum), Jeff, the adapter listing's entries). An engine that
    is not an EngineRouter (a test's in-process FakeEngine) has no adapters_version: its catalogue is built once and
    never rebuilt.

    On the event loop, `await ready()`: the comparison runs there, and a rebuild (folder reads, engine calls, waiting
    for another rebuild or a reload to finish) runs on a worker thread. Code already on a worker thread reads `jeff`.
    """

    def __init__(self, settings: ServerSettings, engine: Engine) -> None:
        self.settings, self.engine = settings, engine
        self._engines = tuple(engine.engines) if isinstance(engine, EngineRouter) else ()
        self._lock = threading.Lock()  # one rebuild at a time; never taken while nothing has changed
        self._check_lock = threading.Lock()  # guards _last_check only
        self._last_check: float | None = None  # when after_unknown last asked the engines (time.monotonic)
        # At startup the engines' lists are the ones each SocketEngine read when it connected: no engine call.
        self._current = self._build(ask_engines=False)

    def seen_version(self) -> tuple[int, int] | None:
        """(the sum of the engines' adapters_version as of the last reply read from each, the sum of their
        reconnects): no engine call. None while an engine has just reconnected and its version is not known yet
        (-1): that alone is no change, and the key moves once the new connection's first reply is read."""
        total = reconnects = 0
        for engine in self._engines:
            version = engine.adapters_version
            if version < 0:
                return None
            total += version
            reconnects += engine.reconnects
        return total, reconnects

    def current(self) -> Jeff | None:
        """The planner when nothing has changed (one comparison: no lock, no file read, no engine call); None when
        it must be rebuilt first (catch_up, on a worker thread)."""
        planned = self.planned_now()
        return planned.jeff if planned is not None else None

    def planned_now(self) -> Planned | None:
        """As current(), the whole Planned."""
        planned = self._current
        seen = self.seen_version()
        return planned if seen is None or seen == planned.key else None

    async def ready(self) -> Jeff:
        """The planner to run a request on, for code on the event loop: a rebuild runs on a worker thread."""
        jeff = self.current()
        if jeff is not None:
            return jeff
        return await anyio.to_thread.run_sync(self.catch_up)

    @property
    def jeff(self) -> Jeff:
        """As ready(), for code on a worker thread."""
        jeff = self.current()
        return jeff if jeff is not None else self.catch_up()

    @property
    def built(self) -> Jeff:
        """The planner last built, never rebuilt here (no engine call, no file read): for wording an error
        (jeff_server.redaction), which must not itself fail on a rebuild."""
        return self._current.jeff

    @property
    def planned(self) -> Planned:
        """As `jeff`, the whole Planned (the planner and the adapter listing's entries, built together)."""
        planned = self.planned_now()
        if planned is not None:
            return planned
        self.catch_up()
        return self._current

    def catch_up(self) -> Jeff:
        seen = self.seen_version()
        return self._current.jeff if seen is None else self.refresh_if_changed(seen)

    def refresh_if_changed(self, seen_version: tuple[int, int]) -> Jeff:
        """Rebuilds the planner if `seen_version` (seen_version()'s key) differs from the one it was built at (another
        request may have rebuilt it meanwhile). While the engines disagree on what they serve (being changed one at a
        time), the old planner stays, recorded at the key now known, and is returned: no error, and the next change
        of key tries again. When the rebuild fails -- a folder or the catalogue is invalid, or an engine is down --
        only the rebuild fails: the old planner stays, recorded at the key now known, so the following requests run
        on it rather than each trying again (the next change of key, or a POST /v1/adapters/reload, tries again). The
        failure is logged and raised to the one request that tried: an EngineError when the engines could not be
        asked what they serve, otherwise a ManifestError."""
        with self._lock:
            version, jeff = self._current.key, self._current.jeff
            if seen_version == version:
                return jeff
            try:
                disagreement = self.engine.disagreement() if isinstance(self.engine, EngineRouter) else None
                if disagreement is not None:
                    now = self.seen_version()
                    self._current = self._current._replace(key=seen_version if now is None else now)
                    log.warning("The engines' adapters changed (key %s -> %s), but the engines do not serve the same "
                                "adapters yet (%s); still answering from the catalogue built at %s until the next "
                                "change", version, seen_version, disagreement, version)
                    return jeff
                # An EngineRouter's engines were asked just now; any other engine is asked by _build.
                self._current = self._build(ask_engines=not isinstance(self.engine, EngineRouter))
            except JeffError as error:
                # The rebuild's own engine calls may have moved the version past seen_version.
                now = self.seen_version()
                self._current = self._current._replace(key=seen_version if now is None else now)
                log.error("The engines' adapters changed (adapters_version, reconnects %s -> %s), but the catalogue "
                          "could not be rebuilt: %s: %s. Still answering from the catalogue built at %s", version,
                          seen_version, type(error).__name__, error, version)
                failure = EngineError if isinstance(error, EngineError) else ManifestError
                # Its cause may name private manifests or adapters: a caller who is not the owner gets it without
                # (jeff_server.redaction); the full text is in the log line above.
                raise reword(failure(f"The engines' adapters changed (adapters_version {version[0]} -> "
                                     f"{seen_version[0]}, reconnects {version[1]} -> {seen_version[1]}), but "
                                     f"this server could not rebuild its catalogue: {type(error).__name__}: {error}. "
                                     "It keeps answering from the catalogue it had until the engines' adapters "
                                     "change again or POST /v1/adapters/reload rebuilds it"),
                             lambda visible: failure("The engines' adapters changed, but this server could not "
                                                     "rebuild its catalogue; it keeps answering from the catalogue "
                                                     "it had. The server's log names the cause")) from error
            log.info("The engines' adapters changed (adapters_version, reconnects %s -> %s); catalogue rebuilt",
                     version, self._current.key)
            return self._current.jeff

    def refresh(self) -> Jeff:
        """Rebuilds the planner now, whatever the version (POST /v1/adapters/reload or PUT /v1/adapters/served, in
        the worker that ran it). A failure is raised as it is, and the old planner stays, recorded at the version now
        known (as refresh_if_changed records it), so the following requests run on it."""
        with self._lock:
            try:
                self._current = self._build(ask_engines=True)
            except JeffError:
                now = self.seen_version()
                if now is not None:
                    self._current = self._current._replace(key=now)
                raise
            return self._current.jeff

    def retry_unknown(self, used: Jeff, plan: Callable[[Jeff], T],
                      unknown: tuple[type[JeffError], ...] = (UnknownFunction,)) -> T:
        """`plan(used)`; when it raises one of `unknown` (a name the catalogue does not have), plans once more on a
        newer planner if after_unknown finds one, and that second attempt's error is final. On a worker thread:
        after_unknown may ask the engines and rebuild. If asking the engines fails, the original error is raised
        with that failure as a note. For an account caller, `plan` builds a fresh metered view (caller_view) each
        time, so the second attempt is checked against the allowance at its own first engine request; that is right
        only while an unknown name is always raised before any engine request (true today: functions, escalation
        steps and router targets are resolved first), else the retry could count that work twice and answer 402."""
        try:
            return plan(used)
        except unknown as error:
            try:
                fresh = self.after_unknown(used)
            except JeffError as check_error:
                error.add_note(f"Also failed to check whether the engines' adapters changed: "
                               f"{type(check_error).__name__}: {check_error}")
                raise error from None
            if fresh is None:
                raise
        return plan(fresh)

    def after_unknown(self, used: Jeff) -> Jeff | None:
        """After `used` (the planner a request ran on, or a view of it) did not know a name: a newer planner to plan
        again on, or None. A worker learns the engines' versions only from their replies, and a request refused for
        an unknown name never reaches an engine, so here the engines are asked (installed, each) -- at most once per
        UNKNOWN_CHECK_INTERVAL_S per worker; within it, None at once."""
        version, jeff = self._current.key, self._current.jeff
        if jeff.catalogue is not used.catalogue:
            return jeff  # rebuilt since that request started
        if not self._engines:
            return None
        with self._check_lock:
            now = time.monotonic()
            if self._last_check is not None and now - self._last_check < UNKNOWN_CHECK_INTERVAL_S:
                return None
            self._last_check = now
        for engine in self._engines:
            engine.installed_version()
        seen = self.seen_version()
        if seen is None or seen == version:
            return None
        fresh = self.refresh_if_changed(seen)
        return fresh if fresh.catalogue is not used.catalogue else None

    def _build(self, *, ask_engines: bool) -> Planned:
        """(the key, the planner, the adapter listing's entries), the key read before the catalogue so a change made
        meanwhile is seen by the next request. ask_engines: ask every engine for its served list first. A rebuild does, because the reply
        that showed one engine's change says nothing of the others', and the planner checks its routers against the
        list every engine agrees on (EngineRouter.installed)."""
        if ask_engines:
            self.engine.installed(refresh=True)
        version = self.seen_version()
        if version is None:
            raise EngineError("An engine reconnected while the catalogue was being rebuilt, so its adapters_version "
                              "is not known yet")
        catalogue = build_catalogue(self.settings)
        jeff = Jeff(self.engine, catalogue)
        log.info("Private adapters in the catalogue: %s", describe_private(catalogue))
        base = self.engine.agreed_base if isinstance(self.engine, EngineRouter) else None
        return Planned(version, jeff, adapter_entries(catalogue, base, jeff.format_id) if base is not None else None)
