"""Test helpers for driving an EngineServer over a real Unix socket: `Running` starts one on its own event loop
thread with a blocking test `Client`. Used by runtime/python/tests/test_engine_server.py and by
packages/python/tests/test_socket_engine.py (the `jeff` package's tests may import `jeff_runtime` since the
workspace installs both)."""

import asyncio
import socket
import struct
import tempfile
import threading
import time
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Any

from jeff.adapter_dirs import Found
from jeff.gate import GatedBase
from jeff.protocol import PROTOCOL_VERSION, encode, hello, read_frame
from jeff.testing import FakeEngine
from jeff_runtime.engine_server import EngineServer
from jeff_runtime.scheduler import Scheduler, Settings


class Running:
    """An EngineServer on its own event loop thread, with a blocking test client. close() may be called twice.

    fixed: the adapter folders the engine may serve, as `jeff runtime serve` gives them from --path or the catalogue
    (default: a.b.c at /x and a.b.d at /y); pinned: the fixed IDs given with --path, which a found folder may not
    share (default: every fixed ID; pass a smaller set for ones downloaded from the catalogue); adapter_roots: the
    --adapters-dir folders to find more in (default: none); found: the scan the engine was loaded from, passed
    through to EngineServer (default: scan now); base: the (base id, version) the engine has loaded, which found
    adapters must be trained on (default: jeff-base v1.3, the catalogue's base). path: where to listen (default: a
    new temporary folder's e.sock); given, a test can start a second server where a closed one listened, as a
    restarted engine process does. settings: the scheduler's (default: a 20 ms window, 64-row batches); a benchmark
    passes max_batch_rows=1 so one row runs at once instead of waiting out the window. prepare_workers: the server's
    preparation threads (default 4, so requests are prepared concurrently as in a real deployment). gate: an INT4
    base's gate check (jeff.gate.GatedBase; default none, a bfloat16 base)."""
    def __init__(self, engine: FakeEngine, served_file: Path | None = None,
                 fixed: Mapping[str, Path] | None = None, *, pinned: Collection[str] | None = None,
                 adapter_roots: Sequence[Path] = (), found: Mapping[str, Found] | None = None,
                 base: tuple[str, str] = ("jeff-base", "v1.3"), path: Path | None = None,
                 settings: Settings | None = None, prepare_workers: int = 4, gate: GatedBase | None = None) -> None:
        self.path = path if path is not None else Path(tempfile.mkdtemp(prefix="jeff")) / "e.sock"
        self.closed = False
        self.scheduler = Scheduler(engine, settings if settings is not None
                                   else Settings(window_ms=20, max_batch_rows=64, queue_limit_rows=4096))
        self.scheduler.start()
        folders = fixed if fixed is not None else {"a.b.c": Path("/x"), "a.b.d": Path("/y")}
        try:
            self.server = EngineServer(engine, self.scheduler, self.path, adapter_roots, folders, served_file, base,
                                       pinned=set(folders) if pinned is None else pinned, found=found,
                                       prepare_workers=prepare_workers, gate=gate)
        except BaseException:
            self.scheduler.stop()  # a refused server must not leave the scheduler's GPU thread running
            raise
        self.loop = asyncio.new_event_loop()
        ready = asyncio.Event()
        self.thread = threading.Thread(target=self.loop.run_until_complete, args=(self.server.run(ready),), daemon=True)
        self.thread.start()
        # Wait for `ready` (set once the server listens), not for the socket file: the file exists from bind(), a
        # moment before listen(), and a connection in between is refused.
        for _ in range(500):
            if ready.is_set():
                break
            threading.Event().wait(0.01)
        else:
            raise RuntimeError(f"The test engine server at {self.path} was not listening within 5 s")

    def client(self, version: int = PROTOCOL_VERSION) -> "Client":
        return Client(self.path, version)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        asyncio.run_coroutine_threadsafe(self.server.close(), self.loop).result(5)
        self.scheduler.stop()


class Client:
    def __init__(self, path: Path, version: int) -> None:
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.connect(str(path))
        self.file = self.sock.makefile("rb")
        message = hello()
        message["version"] = version
        self.sock.sendall(encode(message))
        self.greeting = read_frame(self._read)

    def _read(self, n: int) -> bytes:
        data = self.file.read(n)
        if len(data) < n:
            raise EOFError
        return data

    def send(self, message: dict[str, Any]) -> None:
        self.sock.sendall(encode(message))

    def send_raw(self, body: bytes) -> None:
        self.sock.sendall(struct.pack(">I", len(body)) + body)

    def receive(self) -> dict[str, Any]:
        return read_frame(self._read)

    def disconnect(self) -> None:
        """A real disconnect: closing the raw socket alone is not enough while `self.file` (from `makefile()`)
        still holds its own reference to the underlying file descriptor, so the server never sees the other end
        go away. shutdown() first means the server's pending read fails immediately rather than relying only on
        garbage collection of the file object to drop the last reference."""
        self.sock.shutdown(socket.SHUT_RDWR)
        self.file.close()
        self.sock.close()


class SlowAddAdapter(FakeEngine):
    """A fake engine whose add_adapter takes a while, so overlapping `serve` requests and disconnects-in-flight
    can be exercised deterministically."""
    def __init__(self, delay: float, installed: set[str]) -> None:
        super().__init__(lambda adapter, row: [0.4, 0.6], installed)
        self.delay = delay

    def add_adapter(self, adapter_id: str, path: Path) -> None:
        time.sleep(self.delay)
        super().add_adapter(adapter_id, path)


class SlowPredict(FakeEngine):
    """A fake engine whose predict takes a while, so a client can disconnect before its reply is ready."""
    def __init__(self, delay: float, installed: set[str]) -> None:
        super().__init__(lambda adapter, row: [0.4, 0.6], installed)
        self.delay = delay

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> Any:
        time.sleep(self.delay)
        return super().predict(adapter_id, rows)
