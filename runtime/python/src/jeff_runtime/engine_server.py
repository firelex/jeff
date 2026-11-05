"""The engine process: serves jeff.engine.ServingEngine decisions over a Unix socket (contract/socket.md), with a
Scheduler batching requests from every connected client on the GPU thread. One EngineServer per socket; `jeff
runtime serve` builds one with a real LocalEngine, tests build one with jeff.testing.FakeEngine."""

import asyncio
import contextlib
import json
import logging
import os
import socket
import stat
import time
from collections.abc import Callable, Collection, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from jeff.adapter_dirs import Found, Stamp, check_base, scan
from jeff.contract import first_problem
from jeff.engine import BASE_ADAPTER, ServingEngine, check_capacity
from jeff.errors import EngineError, InvalidRequest, JeffError, ManifestError, ProtocolMismatch, UnknownFunction
from jeff.gate import GatedBase, check_served
from jeff.protocol import (check_hello, encode, fail, hello, ok, read_body_async, read_frame_async, unpack,
                           validate_message)
from jeff_runtime.scheduler import Scheduler

log = logging.getLogger("jeff.engine")

_REQUEST_OPS = {"predict", "installed", "serve", "reload", "stats", "health"}

SERVED_FORMAT = "jeff-engine-served"
SERVED_VERSION = 1


class EngineServer:
    """The adapters this engine may be asked to `serve` are `fixed` (adapter ID -> weights folder: the ones named on
    the command line, with --path or downloaded from the catalogue) plus every adapter folder found under
    `adapter_roots` (jeff.adapter_dirs.scan), which `reload` scans again. `pinned` are the fixed IDs given with
    --path: a found folder with one of those IDs is refused (two folders for one adapter, both named by the
    operator), while for any other fixed ID (a catalogue download) the found folder wins, as the server's own copy,
    and `reload` loads the adapter again from it. `found` is the scan the engine's starting adapters were loaded
    from (so the first `reload` compares against what was actually loaded); None scans now. Every adapter the engine
    has installed when the server starts must be available. `base` is the (base id, version) the engine has loaded;
    a found adapter trained on another base is refused. The base
    (jeff.engine.BASE_ADAPTER, "base") is not an adapter: a `serve` may name it but never adds or removes it, and
    the served file lists only adapters.

    `gate`: when the engine serves an INT4 base, that base and the versions of the fixed adapters. Every `serve` and
    `reload` then checks the adapters it would serve against the base's gate.json (jeff.gate.check_served: a passing
    gate of this exact base, with a passing entry for each adapter at its version) before changing anything, and so
    does the server at start for the adapters the engine has installed; None for a bfloat16 base.

    Raises ManifestError for any problem scan finds, EngineError for an installed adapter with no folder or a
    served file in another format. A served file that does not exist yet is written with the installed adapters."""

    def __init__(self, engine: ServingEngine, scheduler: Scheduler, socket_path: Path, adapter_roots: Sequence[Path],
                 fixed: Mapping[str, Path], served_file: Path | None, base: tuple[str, str], *,
                 pinned: Collection[str], prepare_workers: int, found: Mapping[str, Found] | None = None,
                 gate: GatedBase | None = None) -> None:
        self.engine = engine
        self.scheduler = scheduler
        self.socket_path = Path(socket_path)
        self._roots = [Path(root) for root in adapter_roots]
        self._fixed = dict(fixed)
        self._pinned = set(pinned)
        unknown_pins = sorted(self._pinned - set(self._fixed))
        if unknown_pins:
            raise EngineError(f"pinned adapters {', '.join(unknown_pins)} have no fixed folder")
        self._base = base
        self._served_file = served_file
        self._gate = gate
        if gate is not None:
            unversioned = sorted(set(self._fixed) - set(gate.fixed_versions))
            if unversioned:
                raise EngineError(f"serving an INT4 base, but no version was given for the fixed adapters "
                                  f"{', '.join(unversioned)}; the gate is checked per adapter version")
        # The adapters `serve` may name (ID -> folder), the stamps of the found ones, and every one's version (read
        # by the gate check). Only ever read or replaced on the GPU thread (inside scheduler.change) once the server
        # runs, never on the predict path.
        self._available, self._stamps, self._versions = self._discover(
            scan(self._roots) if found is None else dict(found))
        adapters = sorted(engine.installed() - {BASE_ADAPTER})
        missing = [adapter_id for adapter_id in adapters if adapter_id not in self._available]
        if missing:
            raise EngineError(f"the engine has {', '.join(missing)} installed, but no folder was given for "
                              f"{'it' if len(missing) == 1 else 'them'} (--path, the catalogue or --adapters-dir)")
        self._check_gate(adapters, self._versions)
        # Where each installed adapter was loaded from, (folder, stamp or None for a fixed one): `reload` loads an
        # adapter again when this differs from what the latest scan found.
        self._loaded = {adapter_id: self._source(adapter_id) for adapter_id in adapters}
        if served_file is not None:
            if served_file.exists():
                self._load_served()  # refuse at once a file in any other format (jeff-server's served.json, say)
            else:
                self._write_served(adapters)
        # Requests are prepared here on arrival (Scheduler.submit: engine.prepare checks them and, on the fast
        # backend, tokenises them), up to prepare_workers at once (jeff runtime serve --prepare-workers; the engine
        # gives each thread a processor of its own): never on the event loop (a slow preparation would hold up every
        # other connection's replies) and never on the GPU thread, so the GPU thread never tokenises (design 2b-1
        # Risk 4). With several workers, requests that arrive together may be queued in another order than they
        # arrived.
        if isinstance(prepare_workers, bool) or not isinstance(prepare_workers, int) or prepare_workers < 1:
            raise EngineError(f"prepare_workers {prepare_workers!r}: give a whole number of at least 1")
        self._prepare = ThreadPoolExecutor(max_workers=prepare_workers, thread_name_prefix="jeff-prepare")
        self._stop = asyncio.Event()
        self._server: asyncio.Server | None = None
        self._connections: set[asyncio.StreamWriter] = set()
        self._reply_tasks: set[asyncio.Task[None]] = set()
        # (adapters_version, sorted installed list), read once here (before any client can connect, so nothing
        # else can be touching the engine concurrently yet) and from then on only ever rewritten by the GPU
        # thread, as the very last step of a `serve` change's work() -- always as this one tuple, in one
        # attribute assignment, so a reader on the event-loop thread can never see a new version paired with the
        # old list or vice versa. installed/health/stats/hello answer from this snapshot rather than calling
        # engine.installed() themselves, so they never read the engine's adapter set while the GPU thread is
        # partway through adding or removing one. The version is bumped whenever the installed set actually
        # changed, even if the change that caused it failed partway through or failed to write the served file:
        # the engine's real state is the only thing that matters here, not whether the request that caused it
        # was reported as a success.
        self._snapshot: tuple[int, list[str]] = (0, sorted(engine.installed()))

    async def run(self, ready: asyncio.Event | None = None) -> None:
        if self.socket_path.exists():
            mode = self.socket_path.stat().st_mode
            if not stat.S_ISSOCK(mode):
                raise EngineError(f"{self.socket_path} exists and is not a socket; refusing to remove it")
            if await self._answers():
                raise EngineError(f"another engine is serving at {self.socket_path}")
            self.socket_path.unlink()
        # Bound here, not by asyncio.start_unix_server(path=...), which silently removes a socket file already at
        # the path: if another engine binds between the check above and this bind, this bind fails instead.
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        old_umask = os.umask(0o177)  # close the window between bind() and chmod(): nothing but the owner can
        try:                         # ever connect, even for an instant
            listener.bind(str(self.socket_path))
        except OSError as error:
            listener.close()
            raise EngineError(f"cannot listen at {self.socket_path}: {error} (another engine may have started "
                              "there at the same moment)") from error
        finally:
            os.umask(old_umask)
        self._server = await asyncio.start_unix_server(self._handle_client, sock=listener)
        self.socket_path.chmod(0o600)  # belt and braces: some platforms ignore umask for Unix socket files
        if ready is not None:
            ready.set()
        await self._stop.wait()

    async def close(self) -> None:
        """Stops the listening socket and lets `run()` return. This closes the server directly, rather than
        signalling `run()` to do it and waiting on that, because `run()` is normally driven by
        `loop.run_until_complete(server.run())`: the instant that coroutine returns, the loop stops, which would
        race a reply still being chained back to whoever is awaiting `close()` from another thread. Closing here
        means the listening socket is already down by the time this coroutine returns, no matter which of the two
        coroutines the loop gets to first.

        Server.close() stops the listening socket synchronously (and the socket file is removed); wait_closed() is not used because, from Python
        3.13, it also waits for every already-accepted connection to finish, and a client that is simply slow (or
        gone without closing its socket) must not block shutdown."""
        if self._server is not None:
            self._server.close()
            self.socket_path.unlink(missing_ok=True)
        for writer in list(self._connections):  # force-disconnect every open connection so its handler task ends
            writer.close()                       # (instead of being garbage-collected later, mid-read, when the
                                                  # loop may already be closed)
        # Waits for preparations already running (they are short) so none can submit to a scheduler being stopped
        # after this returns; preparations not yet started are cancelled.
        await asyncio.to_thread(self._prepare.shutdown, wait=True, cancel_futures=True)
        # Last, with no await after it: run() returns (and a loop driven by run_until_complete(run()) stops) only
        # once everything above is done and this coroutine's own result is already on its way to its caller.
        self._stop.set()

    async def wait_broken(self) -> JeffError:
        """Returns the scheduler's error once its GPU thread has died (at once if it already has). `jeff runtime
        serve` waits on this to close the server and exit non-zero, so a supervisor restarts the engine."""
        loop = asyncio.get_running_loop()
        seen = asyncio.Event()

        def heard(error: JeffError) -> None:
            loop.call_soon_threadsafe(seen.set)

        self.scheduler.when_broken(heard)
        await seen.wait()
        broken = self.scheduler.broken
        assert broken is not None
        return broken

    def _fail(self, id_: int, error: JeffError) -> dict[str, Any]:
        """A failure reply carrying, in its error details, the adapters version current as it is built, so the
        client can tell whether its list of installed adapters is out of date."""
        reply = fail(id_, error)
        reply["error"]["details"]["adapters_version"] = self._snapshot[0]
        return reply

    async def _answers(self) -> bool:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_unix_connection(str(self.socket_path)), timeout=0.5)
        except (OSError, TimeoutError):
            return False
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        return True

    def _spawn_send(self, loop: asyncio.AbstractEventLoop, writer: asyncio.StreamWriter, lock: asyncio.Lock,
                    message: dict[str, Any]) -> None:
        """Called via loop.call_soon_threadsafe from the GPU thread, so it runs on the loop thread: creates the
        task that actually writes the reply, and keeps a reference to it so it is not garbage-collected before
        it runs."""
        task = loop.create_task(self._send(writer, lock, message))
        self._reply_tasks.add(task)
        task.add_done_callback(self._reply_tasks.discard)

    # ---- per connection

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        lock = asyncio.Lock()
        loop = asyncio.get_running_loop()
        tasks: set[asyncio.Task[None]] = set()
        self._connections.add(writer)
        try:
            try:
                greeting = await read_frame_async(reader)
            except InvalidRequest as error:
                await self._send(writer, lock, self._fail(0, error))
                return
            if greeting is None:
                return
            try:
                check_hello(greeting)
            except ProtocolMismatch as error:
                await self._send(writer, lock, self._fail(0, error))
                return
            await self._send(writer, lock, hello(self._snapshot[0]))
            while True:
                try:
                    body = await read_body_async(reader)
                    if body is None:
                        break
                    raw = unpack(body)
                except InvalidRequest as error:  # the framing itself is broken: nothing after it can be trusted
                    await self._send(writer, lock, self._fail(0, error))
                    break
                try:
                    message = validate_message(raw, "incoming socket message")
                except InvalidRequest:
                    id_ = raw.get("id") if isinstance(raw, dict) else None
                    op = raw.get("op") if isinstance(raw, dict) else None
                    rejection = InvalidRequest(f"{op!r} request does not match the socket schema: "
                                               f"{first_problem('socket', raw)}")
                    if type(id_) is int and id_ >= 1:  # the request can be answered by its id: only it fails
                        await self._send(writer, lock, self._fail(id_, rejection))
                        continue
                    await self._send(writer, lock, self._fail(0, rejection))  # no id to answer: the connection
                    break                                                     # fails as a whole
                task = asyncio.create_task(self._dispatch(message, writer, lock, loop))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:
            self._connections.discard(writer)
            for task in tasks:
                task.cancel()
            writer.close()

    async def _send(self, writer: asyncio.StreamWriter, lock: asyncio.Lock, message: dict[str, Any]) -> None:
        """Writes to one connection are serialised; a reply to a connection that has already closed is dropped,
        not raised (the client is gone, there is nobody to tell). A message this side cannot even encode (too
        big, or otherwise invalid) is replaced with a typed failure reply for the same id, so the caller gets an
        answer instead of waiting forever for one that was silently dropped; only if even that cannot be encoded
        (which nothing here should be able to trigger) is the reply actually given up on, and logged."""
        async with lock:
            if writer.is_closing():
                return
            try:
                payload = encode(message)
            except InvalidRequest as error:
                log.error("Could not encode a reply to id %s (%s); sending a failure reply instead",
                          message.get("id"), error)
                try:
                    payload = encode(self._fail(message.get("id", 0),
                                                EngineError(f"the engine could not encode its reply: {error}")))
                except InvalidRequest:
                    log.error("Could not encode even the failure reply to id %s; dropping it", message.get("id"))
                    return
            try:
                writer.write(payload)
                await writer.drain()
            except (ConnectionError, OSError):
                pass

    async def _dispatch(self, message: dict[str, Any], writer: asyncio.StreamWriter, lock: asyncio.Lock,
                         loop: asyncio.AbstractEventLoop) -> None:
        op = message.get("op")
        id_ = message.get("id", 0)
        if op not in _REQUEST_OPS:
            await self._send(writer, lock, self._fail(id_, InvalidRequest(
                f"{op!r} is not a request this connection can send now; expected one of "
                f"{', '.join(sorted(_REQUEST_OPS))}")))
            return
        try:
            if op == "predict":
                await self._predict(message, writer, lock, loop)
            elif op == "installed":
                version, installed = self._snapshot
                await self._send(writer, lock, ok(id_, {"adapters": installed, "adapters_version": version}))
            elif op == "serve":
                await self._serve(message, writer, lock, loop)
            elif op == "reload":
                await self._reload(message, writer, lock, loop)
            elif op == "stats":
                await self._send(writer, lock, ok(id_, {**self.scheduler.stats(),
                                                          "adapters_version": self._snapshot[0]}))
            elif op == "health":
                version, installed = self._snapshot
                health: dict[str, Any] = {"status": "ready", "backend": self.engine.backend, "adapters": installed,
                                          "adapters_version": version,
                                          "base": {"id": self._base[0], "version": self._base[1],
                                                   "checkpoint": self.engine.base_checkpoint,
                                                   "max_options": self.engine.base_max_options,
                                                   **self.engine.base_format}}
                broken = self.scheduler.broken
                if broken is not None:
                    health["status"], health["cause"] = "broken", str(broken)
                await self._send(writer, lock, ok(id_, health))
        except JeffError as error:
            await self._send(writer, lock, self._fail(id_, error))
        except Exception as error:  # noqa: BLE001 - every error reaching a client must be a typed reply; never
                                     # an exception left for asyncio to report as "Task exception was never
                                     # retrieved" (nobody awaits this task but its own done callback)
            log.exception("Handling %r (id %s) failed", op, id_)
            wrapped = EngineError(f"{type(error).__name__}: {error}")
            wrapped.__cause__ = error
            await self._send(writer, lock, self._fail(id_, wrapped))

    async def _predict(self, message: dict[str, Any], writer: asyncio.StreamWriter, lock: asyncio.Lock,
                 loop: asyncio.AbstractEventLoop) -> None:
        id_, adapter, rows = message["id"], message["adapter"], message["rows"]
        deadline_ms = message.get("deadline_ms")
        deadline = time.monotonic() + deadline_ms / 1000 if deadline_ms is not None else None

        def on_done(result: Any) -> None:
            # Runs on the GPU thread right after the batch (or on the check thread, for a refused request). Changes
            # run only between batches on that same thread, so the snapshot read here is the adapters version the
            # batch actually ran with, not the one current when the request arrived.
            if isinstance(result, JeffError):
                reply = self._fail(id_, result)
            else:
                body: dict[str, Any] = {"probabilities": result.probabilities, "timing": result.timing,
                                        "adapters_version": self._snapshot[0]}
                if result.input_tokens is not None:
                    body["input_tokens"] = result.input_tokens
                reply = ok(id_, body)
            # A RuntimeError here (the loop, and so the server, is already closed) is not swallowed: it is
            # exactly the kind of delivery failure Scheduler._deliver already catches and logs, and the ordering
            # in jeff.cli's shutdown (stop the scheduler before the loop closes) means it should not happen in
            # practice.
            loop.call_soon_threadsafe(lambda: self._spawn_send(loop, writer, lock, reply))

        await loop.run_in_executor(self._prepare, self.scheduler.submit, adapter, rows, deadline, on_done)

    async def _serve(self, message: dict[str, Any], writer: asyncio.StreamWriter, lock: asyncio.Lock,
                      loop: asyncio.AbstractEventLoop) -> None:
        wanted = [adapter_id for adapter_id in message["adapters"] if adapter_id != BASE_ADAPTER]

        def serve(outcome: dict[str, Any]) -> None:
            if self._served_file is not None and self._served_file.exists():
                self._load_served()  # a file in another format is refused before anything changes
            self._refuse_unknown(wanted)
            check_capacity(self.engine.max_adapters, len(set(wanted)))  # before the first change
            self._check_gate(wanted, self._versions)
            self._install(wanted, outcome)

        await self._change(message["id"], serve, writer, lock, loop)

    async def _reload(self, message: dict[str, Any], writer: asyncio.StreamWriter, lock: asyncio.Lock,
                       loop: asyncio.AbstractEventLoop) -> None:
        id_ = message["id"]
        if self._served_file is None:
            await self._send(writer, lock, self._fail(id_, InvalidRequest("this engine has no served file")))
            return

        def reload(outcome: dict[str, Any]) -> None:
            # Every check comes before the first change, so a refused reload leaves the engine, the available set
            # and the served file exactly as they were.
            wanted = [adapter_id for adapter_id in self._load_served() if adapter_id != BASE_ADAPTER]
            available, stamps, versions = self._discover(scan(self._roots))
            gone = sorted(adapter_id for adapter_id in self.engine.installed() - {BASE_ADAPTER}
                          if adapter_id not in available)
            if gone:
                raise ManifestError(f"{', '.join(gone)} is served, but its folder is no longer in any adapters dir; "
                                    "put the folder back, or serve without it first; nothing changed")
            unknown = sorted(adapter_id for adapter_id in wanted if adapter_id not in available)
            if unknown:
                raise UnknownFunction(f"not available to serve from this engine: {', '.join(unknown)}")
            check_capacity(self.engine.max_adapters, len(set(wanted)))
            self._check_gate(wanted, versions)  # at the versions this scan found
            self._available, self._stamps, self._versions = available, stamps, versions
            for adapter_id in sorted(self.engine.installed() - {BASE_ADAPTER}):
                if adapter_id in wanted and self._loaded[adapter_id] != self._source(adapter_id):
                    self.engine.remove_adapter(adapter_id)
                    del self._loaded[adapter_id]
                    outcome["reloaded"] = True
                    self.engine.add_adapter(adapter_id, self._available[adapter_id])
                    self._loaded[adapter_id] = self._source(adapter_id)
            self._install(wanted, outcome)

        await self._change(id_, reload, writer, lock, loop)

    async def _change(self, id_: int, body: Callable[[dict[str, Any]], None], writer: asyncio.StreamWriter,
                      lock: asyncio.Lock, loop: asyncio.AbstractEventLoop) -> None:
        """Runs `body` on the GPU thread, serialised with every other change by the scheduler, and replies with the
        installed list and adapters version. `body` sets outcome["installed"] on success, and
        outcome["reloaded"] once it has removed an adapter to load it again."""
        outcome: dict[str, Any] = {}

        def work() -> None:
            # Everything here runs on the GPU thread, serialised with every other change by the scheduler: the
            # diff is computed against the engine's state as it is right now, never against a snapshot taken
            # earlier on the event-loop thread, which an overlapping `serve` could have made stale by the time
            # this one actually runs. A client disconnecting can only cancel the *reply*, never an already
            # enqueued change, since cancelling the task awaiting `done` cannot retract a change already handed
            # to the scheduler.
            #
            # The `finally` always refreshes the published snapshot from the engine's real state and bumps the
            # version if that state actually changed, or if an adapter was loaded again from changed files (the
            # list is the same, the answers may not be) -- even if add_adapter/remove_adapter raised partway
            # through (the engine mutation up to that point already happened and is not rolled back) or the
            # served-file write failed after a fully successful mutation. A reader must never see a stale
            # installed list just because the request that caused the change was itself reported as a failure.
            try:
                body(outcome)
            finally:
                version, published = self._snapshot
                installed_now = sorted(self.engine.installed())
                if installed_now != published or outcome.get("reloaded"):
                    version += 1
                self._snapshot = (version, installed_now)
                outcome["adapters_version"] = version

        done = asyncio.Event()

        def on_done(error: JeffError | None) -> None:
            outcome["error"] = error
            loop.call_soon_threadsafe(done.set)

        self.scheduler.change(work, on_done)
        await done.wait()
        error = outcome.get("error")
        if error is not None:
            await self._send(writer, lock, self._fail(id_, error))
            return
        await self._send(writer, lock, ok(id_, {"adapters": outcome["installed"],
                                                  "adapters_version": outcome["adapters_version"]}))

    # ---- on the GPU thread (inside scheduler.change), or in __init__ before any client can connect

    def _discover(self, found: dict[str, Found]) -> tuple[dict[str, Path], dict[str, Stamp], dict[str, str]]:
        """From a scan of the adapter roots: the adapters `serve` may name (fixed + found, a found folder winning
        over a catalogue download), the found ones' stamps, and each one's version (a found adapter's from its own
        manifest; fixed ones' from `gate`, only known when serving an INT4 base). ManifestError for a found adapter
        on another base, or a found ID also given with --path."""
        check_base(found, self._base)
        clash = sorted(set(found) & self._pinned)
        if clash:
            raise ManifestError("; ".join(f"{adapter_id} is both in an adapters dir ({found[adapter_id].folder}) and "
                                          f"given with --path ({self._fixed[adapter_id]})"
                                          for adapter_id in clash) + "; serve each adapter from one place")
        available = {**self._fixed, **{adapter_id: item.folder for adapter_id, item in found.items()}}
        versions = {**(self._gate.fixed_versions if self._gate is not None else {}),
                    **{adapter_id: item.manifest.version for adapter_id, item in found.items()}}
        return available, {adapter_id: item.stamp for adapter_id, item in found.items()}, versions

    def _check_gate(self, wanted: Sequence[str], versions: Mapping[str, str]) -> None:
        """On an INT4 base, refuses (EngineError naming each adapter and the command) to serve `wanted` unless the
        base's gate.json passes for each at its version (jeff.gate.check_served); nothing on a bfloat16 base."""
        if self._gate is None:
            return
        check_served(self._gate.identity, {adapter_id: versions[adapter_id] for adapter_id in wanted})

    def _source(self, adapter_id: str) -> tuple[Path, Stamp | None]:
        return self._available[adapter_id], self._stamps.get(adapter_id)

    def _refuse_unknown(self, wanted: list[str]) -> None:
        unknown = sorted(adapter_id for adapter_id in wanted if adapter_id not in self._available)
        if unknown:
            raise UnknownFunction(f"not available to serve from this engine: {', '.join(unknown)}")

    def _install(self, wanted: list[str], outcome: dict[str, Any]) -> None:
        """Makes the installed adapters exactly `wanted` (plus the base), then writes the served file. The file is
        only ever written after every adapter in `wanted` is confirmed installed -- writing it from a partial
        state would make a future `reload` reproduce that same partial, broken state."""
        current = self.engine.installed()
        for adapter_id in current:
            if adapter_id not in wanted and adapter_id != BASE_ADAPTER:
                self.engine.remove_adapter(adapter_id)
                del self._loaded[adapter_id]
        for adapter_id in wanted:
            if adapter_id not in current:
                self.engine.add_adapter(adapter_id, self._available[adapter_id])
                self._loaded[adapter_id] = self._source(adapter_id)
        installed = sorted(self.engine.installed())
        if self._served_file is not None:
            self._write_served([adapter_id for adapter_id in installed if adapter_id != BASE_ADAPTER])
        outcome["installed"] = installed

    def _load_served(self) -> list[str]:
        assert self._served_file is not None
        return read_served(self._served_file)

    def _write_served(self, adapters: list[str]) -> None:
        assert self._served_file is not None
        try:
            self._served_file.write_text(json.dumps({"format": SERVED_FORMAT, "version": SERVED_VERSION,
                                                     "adapters": adapters}))
        except OSError as error:
            raise EngineError(f"{self._served_file}: cannot write the served file ({error})") from error


def read_served(path: Path) -> list[str]:
    """The adapter list from a served file, which must be exactly {"format": "jeff-engine-served", "version": 1,
    "adapters": [<adapter id>, ...]}. Anything else (including jeff-server's own served.json) is refused with an
    EngineError naming the file, so it is never read or overwritten as this engine's."""
    try:
        raw = path.read_text()
    except OSError as error:
        raise EngineError(f"{path}: cannot read the served file ({error})") from error
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise EngineError(f"{path}: not valid JSON ({error})") from error
    if not (isinstance(data, dict) and set(data) == {"format", "version", "adapters"}
            and data["format"] == SERVED_FORMAT and data["version"] == SERVED_VERSION
            and isinstance(data["adapters"], list) and all(isinstance(a, str) for a in data["adapters"])):
        raise EngineError(f"{path}: not a {SERVED_FORMAT} file (expected {{\"format\": \"{SERVED_FORMAT}\", "
                          f"\"version\": {SERVED_VERSION}, \"adapters\": [...]}}); refusing to read or overwrite it")
    adapters: list[str] = data["adapters"]
    return adapters
