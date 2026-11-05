"""One change of the engines' adapters at a time, across processes: the API's PUT /v1/adapters/served and POST
/v1/adapters/reload (jeff_server.operations) and the factory worker's publish and unpublish
(jeff_server.factory.publish) each hold this lock while they change the engines.

Without it, a served change from the API that lands between the worker's reload of an engine and its serve of
(what that engine serves + the new adapter) is overwritten by that serve, which was computed before it.

The lock is an flock on <first JEFF_ADAPTER_DIRS folder>/.jeff-adapter-change.lock: both processes read
JEFF_ADAPTER_DIRS from the same env file (the worker publishes into its first folder, and an API with
JEFF_FACTORY=owner must have it), so they find the same file. It is a file whose name starts with a dot, so adapter
discovery never takes it for an adapter folder (jeff.adapter_dirs.scan looks only at folders, and skips hidden ones).
Inside one process a threading lock is taken first, so two threads of one API worker queue on it rather than on the
file. An API with no adapter folders (the factory off) has no file to share and takes only the threading lock: no
other process publishes into folders it does not read.

Decisions never take this lock: it guards adapter changes only."""

import fcntl
import threading
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path

LOCK_NAME = ".jeff-adapter-change.lock"


class AdapterChangeLock:
    def __init__(self, adapter_dirs: Sequence[Path]) -> None:
        self.path: Path | None = adapter_dirs[0] / LOCK_NAME if adapter_dirs else None
        self._thread_lock = threading.Lock()

    @contextmanager
    def held(self) -> Iterator[None]:
        """Waits for every other holder, in this process and any other, then holds the lock for the block."""
        with self._thread_lock:
            if self.path is None:
                yield
                return
            with self.path.open("a") as file:
                fcntl.flock(file, fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(file, fcntl.LOCK_UN)
