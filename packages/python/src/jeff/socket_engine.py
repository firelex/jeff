"""An Engine over the engine socket (contract/socket.md): thread-safe, multiplexed by request id. After the engine
closes the connection (its process restarted, say), the next call makes one fresh connection; a call that was in
flight when it closed fails and is never resent."""

import io
import itertools
import logging
import socket
import threading
import time
from collections.abc import Callable
from concurrent.futures import Future, TimeoutError as FutureTimeout
from pathlib import Path
from typing import Any

from jeff.engine import EngineResult
from jeff.errors import EngineError, JeffError
from jeff.protocol import check_hello, encode, hello, read_frame
from jeff.wire import error_from_wire

# Replies to these ops carry the engine's whole installed list beside its version, so the cache is set from them
# directly; any other reply carries only the version, and a version different from the cached one means asking
# for the list with `installed`.
_CARRY_ADAPTERS = {"installed", "serve", "reload"}

# The fewest seconds between two reconnect attempts. A call made sooner after a failed attempt fails at once, naming
# that attempt, rather than sleeping or retrying: while the engine is down, every caller learns so immediately and
# the engine's socket is not hammered.
RECONNECT_INTERVAL_S = 1.0

log = logging.getLogger("jeff.socket")


class SocketEngine:
    """reconnects: how many times a call has reconnected to the engine after its connection closed (reported by
    jeff-server's /metrics as jeff_engine_reconnects_total)."""

    def __init__(self, path: Path | str, *, timeout_s: float) -> None:
        self.path, self.timeout_s = Path(path), timeout_s
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._snapshot_lock = threading.Lock()
        # Re-entrant: the reconnecting thread asks the new connection for `installed` through _call, which, should
        # that connection already have closed too, comes back here (and fails at once, the interval not being up).
        self._reconnect_lock = threading.RLock()
        self._pending: dict[int, Future[dict[str, Any]]] = {}
        self._ids = itertools.count(1)
        self._closed: JeffError | None = None
        self._closed_by_caller = False
        self._snapshot: tuple[int, set[str]] = (-1, set())
        self._reader: threading.Thread | None = None
        # (when, and the error it failed with, or None when it succeeded) of the last reconnect attempt.
        self._last_attempt: tuple[float, EngineError | None] | None = None
        self.reconnects = 0
        try:
            self._connect()
        except OSError as error:
            raise EngineError(f"No engine is listening at {self.path}: {error}") from error
        try:
            self._merge_snapshot(*self._installed_now())
            # read once, here: a Jeff picks each function's calibration from the format when binding
            # (jeff.engine.Engine), and jeff-server lists the adapters trained for the base (id, version)
            base_format, base = self._base_now()
            self.base_format: dict[str, str] = base_format
            self.base: tuple[str, str] = base
        except Exception:
            self.close()
            raise

    def _connect(self) -> None:
        """Opens a connection, exchanges `hello`s, makes it the connection every call uses and starts its reader
        thread. Used by __init__ and by _reconnect, which each word their own error for an OSError from connect()
        (nothing listening at the path); any failure closes the new socket and leaves the current state as it
        was. Request ids are not restarted: a caller may have drawn its id from the counter just before the
        old connection closed and register it just after this one opens, and a restarted counter would hand that
        same id out again. The cached adapters_version is set below every real one (the list is kept): a restarted
        engine counts from 0 again, so the next reply's version must replace the old engine's, not lose to it."""
        sock = socket.socket(socket.AF_UNIX)
        try:
            sock.connect(str(self.path))
        except OSError:
            sock.close()
            raise
        file = sock.makefile("rb")
        try:
            self._handshake(sock, file)
        except Exception:
            self._close_socket(sock, file)
            raise
        with self._pending_lock:
            if self._closed_by_caller:
                self._close_socket(sock, file)
                raise self._dead()
            self._sock, self._file = sock, file
            with self._snapshot_lock:
                self._snapshot = (-1, self._snapshot[1])
            self._closed = None
        self._reader = threading.Thread(target=self._reader_loop, args=(file,), name=f"jeff-socket {self.path}",
                                        daemon=True)
        self._reader.start()

    def _reconnect(self) -> None:
        """Called by a call that found the connection closed. One thread at a time: it makes one fresh connection
        (connect, hello, installed); a thread that waited meanwhile finds the connection open again and returns,
        to use it. At most one attempt per RECONNECT_INTERVAL_S; never after the caller's close()."""
        with self._reconnect_lock:
            with self._pending_lock:
                if self._closed_by_caller:
                    raise self._dead()
                if self._closed is None:
                    return
            now = time.monotonic()
            if self._last_attempt is not None and now - self._last_attempt[0] < RECONNECT_INTERVAL_S:
                raise self._too_soon(now)
            self._last_attempt = (now, None)
            try:
                self._connect()
                version, installed = self._installed_now()
                served_format, served_base = self._base_now()
            except Exception as cause:  # noqa: BLE001 - every way the attempt can fail is raised as one typed
                                         # error naming the socket, and remembered for calls within the interval
                error = EngineError(f"No engine is listening at {self.path}: {cause} (reconnect attempt)")
                self._last_attempt = (now, error)
                self._shutdown(error)  # the new connection, if one was opened before `installed` failed
                raise error from cause
            if served_format != self.base_format or served_base != self.base:
                # Functions bound through this engine chose their calibration for the format read at connect, and
                # jeff-server lists the adapters trained for the base read then; the restarted engine serves other
                # weights, so nothing more is sent to it from this process.
                error = EngineError(f"The engine at {self.path} restarted serving a base of format {served_format} "
                                    f"({served_base[0]}@{served_base[1]}), not {self.base_format} ({self.base[0]}@"
                                    f"{self.base[1]}) as when this process connected; the "
                                    "functions bound here use that format's calibration, and the adapters listed "
                                    "are the ones trained for that base. Restart this process to serve the new base")
                self._last_attempt = (now, error)
                self._shutdown(error)
                raise error
            self._merge_snapshot(version, installed)
            self.reconnects += 1
            log.warning("Reconnected to the engine at %s, which now serves adapters_version %d", self.path, version)

    def _too_soon(self, now: float) -> EngineError:
        """The error for a call made within RECONNECT_INTERVAL_S of the last attempt: a fresh one each time (see
        _dead), naming what that attempt found and how long ago it was."""
        assert self._last_attempt is not None
        at, failure = self._last_attempt
        if failure is not None:
            error = EngineError(f"No engine is listening at {self.path}: the last reconnect attempt, "
                                f"{now - at:.2f}s ago, failed: {failure}. The next attempt may be made "
                                f"{RECONNECT_INTERVAL_S}s after it")
            error.__cause__ = failure
            return error
        cause = self._closed
        assert cause is not None
        error = EngineError(f"The engine at {self.path} closed the connection {now - at:.2f}s ago, just after this "
                            f"side reconnected: {type(cause).__name__}: {cause}. The next attempt may be made "
                            f"{RECONNECT_INTERVAL_S}s after the last")
        error.__cause__ = cause
        return error

    def _handshake(self, sock: socket.socket, file: io.BufferedReader) -> None:
        """Exchanges `hello`s on a new connection. Bounded by `timeout_s`, unlike every read after it (which the
        reader thread does, each call bounded instead by `Future.result(timeout_s)`)."""
        sock.settimeout(self.timeout_s)
        try:
            try:
                sock.sendall(encode(hello()))
                greeting = read_frame(self._exact_reader(file))
            except TimeoutError as error:
                raise EngineError(f"No hello from the engine at {self.path} within {self.timeout_s}s") from error
            except EngineError as error:
                # jeff.protocol.read_frame raises exactly this EngineError (wrapping an EOFError) when the socket
                # closes before a full frame arrives; re-raised here so the message names which socket closed.
                raise EngineError(
                    f"The engine at {self.path} closed the connection during the handshake") from error
            except OSError as error:
                # e.g. a BrokenPipeError from sendall(), if the other end had already closed the connection
                # before this side even finished sending its hello.
                raise EngineError(
                    f"The engine at {self.path} closed the connection during the handshake: {error}") from error
        finally:
            sock.settimeout(None)
        if greeting.get("ok") is False:
            raise self._rebuild(greeting["error"])
        check_hello(greeting)

    @staticmethod
    def _exact_reader(file: io.BufferedReader) -> Callable[[int], bytes]:
        """read(n) for read_frame on one connection's file: exactly n bytes, or EOFError when it closes first."""
        def read(n: int) -> bytes:
            data = file.read(n)
            if len(data) < n:
                raise EOFError
            return data
        return read

    def _reader_loop(self, file: io.BufferedReader) -> None:
        """Reads one connection's replies until it closes. Given that connection's file, never self._file: after a
        reconnect, self._file is the new connection's."""
        error: JeffError
        read = self._exact_reader(file)
        try:
            while True:
                message = read_frame(read)
                id_ = int(message["id"])
                if id_ == 0 and message.get("ok") is False:
                    # Not a reply to any call this side made (every id this side sends starts at 1): a
                    # connection-level failure the other side is reporting, e.g. a malformed frame it could not
                    # even attribute to one pending request. Every call still waiting fails with this same
                    # rebuilt error, since there is no single request id to blame it on.
                    error = self._rebuild(message["error"])
                    break
                with self._pending_lock:
                    future = self._pending.pop(id_, None)
                if future is not None:
                    future.set_result(message)
        except EngineError as cause:
            # The one EngineError read_frame raises itself: the socket closed (EOF) before a full frame arrived.
            error = EngineError(f"The engine at {self.path} closed the connection")
            error.__cause__ = cause
        except Exception as cause:  # noqa: BLE001 - every way the reader can die must end the connection with a
                                     # typed error naming what went wrong, not an unobserved thread crash
            error = EngineError(f"The engine socket to {self.path} failed: {type(cause).__name__}: {cause}")
            error.__cause__ = cause
        self._shutdown(error)

    def _shutdown(self, error: JeffError) -> None:
        """The one place that really closes the connection: the first caller's error wins (a later one, whether
        from close() or from the reader noticing the socket die, changes nothing); every pending call fails with
        it at once, and the socket is shut down and closed so the reader thread (if it is the one still running)
        unblocks and exits on its own. The socket closed is the one current when the error was recorded: a
        reconnect replaces self._sock only once self._closed is set, so a reader still finishing its own
        _shutdown never closes the next connection."""
        with self._pending_lock:
            if self._closed is None:
                self._closed = error
            closed = self._closed
            pending, self._pending = list(self._pending.values()), {}
            sock, file = self._sock, self._file
        for future in pending:
            future.set_exception(closed)
        self._close_socket(sock, file)

    @staticmethod
    def _close_socket(sock: socket.socket, file: io.BufferedReader) -> None:
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        file.close()
        sock.close()

    def _dead(self) -> EngineError:
        """A call made after the connection has already died raises this: a fresh `EngineError` every time, never
        the same shared `self._closed` instance handed out to every other thread that calls after death (raising
        one shared exception instance repeatedly, from many threads at once, would pile tracebacks onto it
        unsafely). Chained from the original failure, so the real cause is still visible."""
        cause = self._closed
        assert cause is not None
        error = EngineError(f"The engine at {self.path} closed the connection after: {type(cause).__name__}: {cause}")
        error.__cause__ = cause
        return error

    @staticmethod
    def _rebuild(body: dict[str, Any]) -> JeffError:
        """The error a failure reply describes. The engine puts its adapters_version in every failure's details
        (contract/socket.md); that is protocol information, not an attribute of the error, so it is left out."""
        details = {key: value for key, value in dict(body.get("details") or {}).items() if key != "adapters_version"}
        return error_from_wire({**body, "details": details})

    def _version(self, body: dict[str, Any], op: str) -> int:
        if "adapters_version" not in body:
            raise EngineError(f"The engine at {self.path} answered {op!r} without an adapters_version; every reply "
                              "must carry one (contract/socket.md)")
        return int(body["adapters_version"])

    def _call(self, message: dict[str, Any], timeout_s: float | None = None) -> dict[str, Any]:
        """Sends one request and waits for its reply. Every reply carries the engine's adapters_version: a reply
        to installed/serve/reload sets the cached (version, installed) pair from its own list; any other reply
        whose version differs from the cached one makes this side ask for the list (except a failed `installed`,
        which would only ask again). A failure is raised as the error it describes, after that."""
        op = message["op"]
        wait = self.timeout_s if timeout_s is None else timeout_s
        id_ = next(self._ids)
        message["id"] = id_
        payload = encode(message)  # built and encoded before the future is registered: if this raises (an
                                    # oversized or otherwise invalid message), nothing was ever registered, so
                                    # there is nothing to leak.
        future: Future[dict[str, Any]] = Future()
        with self._pending_lock:
            if self._closed is None:
                self._pending[id_] = future
                sock = self._sock  # the connection this id is registered on, even if a reconnect replaces it
            else:                  # before the send below: the request must never go out on another connection
                sock = None
        if sock is None:
            # The connection has closed: reconnect (or fail, naming why), then make this call on the new one.
            self._reconnect()
            return self._call(message, timeout_s)
        try:
            with self._send_lock:
                sock.sendall(payload)
        except OSError as error:
            with self._pending_lock:
                self._pending.pop(id_, None)
            raise EngineError(f"The engine at {self.path} closed the connection") from error
        try:
            reply = future.result(wait)
        except FutureTimeout as error:
            with self._pending_lock:
                self._pending.pop(id_, None)
            raise EngineError(f"No reply from the engine at {self.path} within {wait}s") from error
        if not reply["ok"]:
            # The server's own error always wins: built first, and raised even if reading its adapters_version or
            # refreshing the cache afterwards goes wrong too. Either of those failures is recorded as a note on
            # the original error (never raised in its place), so it is never lost but also never hides what the
            # server actually reported.
            original = self._rebuild(reply["error"])
            try:
                version = self._version(reply["error"]["details"], op)
            except Exception as version_error:  # noqa: BLE001 - see comment above
                original.add_note(f"Also failed to read adapters_version from this failure reply: "
                                  f"{type(version_error).__name__}: {version_error}")
                raise original from None
            if op != "installed":
                try:
                    self._follow(version, timeout_s)
                except Exception as refresh_error:  # noqa: BLE001 - see comment above
                    original.add_note(f"Also failed to refresh the adapter list after this error: "
                                      f"{type(refresh_error).__name__}: {refresh_error}")
            raise original
        result: dict[str, Any] = reply["result"]
        version = self._version(result, op)
        if op in _CARRY_ADAPTERS:
            self._merge_snapshot(version, set(result["adapters"]))
        else:
            self._follow(version, timeout_s)
        return result

    def _follow(self, version: int, timeout_s: float | None = None) -> None:
        with self._snapshot_lock:
            stale = version != self._snapshot[0]
        if stale:
            self._merge_snapshot(*self._installed_now(timeout_s))

    def _installed_now(self, timeout_s: float | None = None) -> tuple[int, set[str]]:
        """Fetches the engine's current (version, installed) pair. Does not touch the cache: every caller routes
        the result through `_merge_snapshot`, the one place the cache is written."""
        result = self._call({"op": "installed"}, timeout_s)
        return int(result["adapters_version"]), set(result["adapters"])

    def _merge_snapshot(self, version: int, installed: set[str]) -> None:
        """The one place the cached (version, installed) pair is written. Replies can arrive out of order (two
        concurrent calls, or a slow reply overtaken by a faster later one), so a version older than the one
        already cached is dropped rather than rolling the cache backwards."""
        with self._snapshot_lock:
            if version >= self._snapshot[0]:
                self._snapshot = (version, set(installed))

    @property
    def adapters_version(self) -> int:
        """The engine's adapters_version as of the last reply read from it (-1 from a reconnect until the new
        connection's first reply). Read without a lock: the cache is one tuple, replaced whole, so this is one
        attribute read, cheap enough for jeff-server to compare on every request (jeff_server.catalogue_refresh)."""
        return self._snapshot[0]

    def _base_now(self) -> tuple[dict[str, str], tuple[str, str]]:
        """From the engine's health: its base format (base.format, and base.format_id for an INT4 base) and the base
        it serves as adapter manifests name it (base.id, base.version)."""
        health = self._call({"op": "health"})
        base = health.get("base")
        if not isinstance(base, dict) or not isinstance(base.get("format"), str):
            raise EngineError(f"The engine at {self.path} answered 'health' without base.format (got base {base!r}); "
                              "every engine reports its base's weight format (contract/socket.md)")
        if not isinstance(base.get("id"), str) or not isinstance(base.get("version"), str):
            raise EngineError(f"The engine at {self.path} answered 'health' without base.id and base.version (got "
                              f"base {base!r}); every engine reports the base it serves as adapter manifests name it "
                              "(contract/socket.md)")
        return {key: base[key] for key in ("format", "format_id") if key in base}, (base["id"], base["version"])

    def installed(self, refresh: bool = False) -> set[str]:
        """The cached list of adapters the engine has loaded; with refresh=True, asked of the engine first."""
        if refresh:
            self._merge_snapshot(*self._installed_now())
        with self._snapshot_lock:
            return set(self._snapshot[1])

    def installed_version(self, timeout_s: float | None = None) -> tuple[int, set[str]]:
        """Asks the engine for its (adapters_version, installed list) pair, from one reply, and updates the cache
        with it. timeout_s: how long to wait for the reply (default: this SocketEngine's timeout_s)."""
        version, installed = self._installed_now(timeout_s)
        self._merge_snapshot(version, installed)
        return version, set(installed)

    def predict(self, adapter_id: str, rows: list[dict[str, Any]], deadline_ms: float | None = None) -> EngineResult:
        message: dict[str, Any] = {"op": "predict", "adapter": adapter_id, "rows": rows}
        if deadline_ms is not None:
            message["deadline_ms"] = deadline_ms
        result = self._call(message)
        # input_tokens is optional in the reply (contract/socket.md): an engine that does not count tokens leaves it
        # out, and the result says so with None.
        tokens = [int(count) for count in result["input_tokens"]] if "input_tokens" in result else None
        return EngineResult([list(map(float, row)) for row in result["probabilities"]], dict(result["timing"]),
                            tokens)

    def serve(self, adapters: list[str]) -> set[str]:
        return set(self._call({"op": "serve", "adapters": list(adapters)})["adapters"])

    def reload(self) -> set[str]:
        return set(self._call({"op": "reload"})["adapters"])

    def stats(self, timeout_s: float | None = None) -> dict[str, Any]:
        """timeout_s: how long to wait for the reply (default: this SocketEngine's timeout_s), so a health check
        or a metrics scrape need not wait as long as a prediction may."""
        return self._call({"op": "stats"}, timeout_s)

    def health(self, timeout_s: float | None = None) -> dict[str, Any]:
        """timeout_s: as for stats()."""
        return self._call({"op": "health"}, timeout_s)

    def close(self) -> None:
        """Final: later calls fail, and there is no reconnect."""
        with self._pending_lock:
            self._closed_by_caller = True
        self._shutdown(EngineError(f"The SocketEngine for {self.path} was closed by its caller"))
        if self._reader is not None and self._reader is not threading.current_thread():
            self._reader.join(timeout=5)
