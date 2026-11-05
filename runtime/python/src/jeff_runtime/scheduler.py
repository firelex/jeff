"""The engine's scheduler: requests from every client are prepared on arrival (checked, and on the fast backend
tokenised), queued, gathered into micro-batches and run as one engine call per micro-batch on the GPU thread (spec 2a,
Section 4; design 2b-1 Section 3). Within the call, rows are grouped by adapter (and by the route the engine prepared
them for); the fast backend runs every group's rows together as passes per new-token bucket, the other backends run
the groups one after another."""

import logging
import math
import statistics
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from jeff.engine import EngineResult, Prepared, ServingEngine
from jeff.errors import Busy, DeadlineExceeded, EngineError, InvalidRequest, JeffError

log = logging.getLogger("jeff.engine")
type Done = Callable[[EngineResult | JeffError], None]


@dataclass(frozen=True)
class Settings:
    """window_ms: how long the first queued request waits for others to join its micro-batch (> 0).
    max_batch_rows: the most rows one micro-batch (one engine call) may hold, every adapter's together (>= 1).
    queue_limit_rows: the most rows that may wait in the queue (>= max_batch_rows, so a full batch always fits)."""
    window_ms: float
    max_batch_rows: int
    queue_limit_rows: int

    def __post_init__(self) -> None:
        if not self.window_ms > 0:
            raise InvalidRequest(f"window_ms must be greater than 0, got {self.window_ms}")
        if self.max_batch_rows < 1:
            raise InvalidRequest(f"max_batch_rows must be at least 1, got {self.max_batch_rows}")
        if self.queue_limit_rows < self.max_batch_rows:
            raise InvalidRequest(f"queue_limit_rows ({self.queue_limit_rows}) must be at least max_batch_rows "
                                 f"({self.max_batch_rows})")


@dataclass(eq=False)  # identity, not field equality: deque.remove must remove this exact request
class _Request:
    adapter: str
    prepared: Prepared                               # its rows as the engine prepared them on arrival
    deadline: float | None
    on_done: Done
    arrived: float
    claimed: int = 0                                 # rows handed to a batch so far (running or already run)
    taken: int = 0                                   # rows already run (chunked requests)
    queue_ms: float | None = None                    # arrival until the batch with its first rows was formed
    results: list[list[float]] = field(default_factory=list)
    # Each row's prompt length, sliced from its batches' input_tokens; None once any batch came without them.
    input_tokens: list[int] | None = field(default_factory=list)
    row_timing: dict[str, list[Any]] = field(default_factory=dict)  # per-row timing, sliced from its batches'
    timing: dict[str, Any] = field(default_factory=dict)

    @property
    def size(self) -> int:
        return len(self.prepared.rows)

    @property
    def unclaimed(self) -> int:
        """Rows still waiting in the queue: not yet in any batch."""
        return self.size - self.claimed

    @property
    def remaining(self) -> int:
        return self.size - self.taken


type _Part = tuple[_Request, int, int]                        # a request's rows start to stop in one micro-batch
type _Key = tuple[str, str | None, Any]                      # (adapter, route, generation): one group each
type _Batch = list[tuple[_Key, list[_Part]]]                  # (its key, its parts), one per group


class Scheduler:
    def __init__(self, engine: ServingEngine, settings: Settings) -> None:
        self.engine, self.settings = engine, settings
        self._queue: deque[_Request] = deque()
        self._changes: deque[tuple[Callable[[], None], Callable[[JeffError | None], None]]] = deque()
        self._lock = threading.Condition()
        self._stopping = False
        self._thread: threading.Thread | None = None
        self._counts = {"batches": 0, "rows": 0, "busy": 0, "deadline_exceeded": 0}
        self._model_ms: deque[float] = deque(maxlen=1000)
        self._batch_s: deque[float] = deque(maxlen=100)  # wall time of recent predict calls, for Busy.retry_after_s
        self._broken: JeffError | None = None  # set once the GPU thread has died unexpectedly; permanent
        self._broken_listeners: list[Callable[[JeffError], None]] = []

    @property
    def broken(self) -> JeffError | None:
        """The error the GPU thread died with, or None while it is healthy. Once set it never clears: every later
        request and change is refused with it, and the engine process should exit so a supervisor restarts it."""
        with self._lock:
            return self._broken

    def when_broken(self, listener: Callable[[JeffError], None]) -> None:
        """Calls `listener(error)` once if the GPU thread dies: from the GPU thread when it happens, or at once
        (from this thread) if it already has."""
        with self._lock:
            broken = self._broken
            if broken is None:
                self._broken_listeners.append(listener)
                return
        listener(broken)

    # ---- called from client threads
    def start(self) -> None:
        if self._thread is not None:
            raise EngineError("The scheduler is already started; create a new Scheduler to start again")
        self._thread = threading.Thread(target=self._loop, name="jeff-gpu", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if threading.current_thread() is self._thread:
            raise EngineError("stop() was called from the GPU thread itself (e.g. from an on_done or change "
                               "callback); call it from another thread")
        with self._lock:
            self._stopping = True
            self._lock.notify_all()
        if self._thread is not None:
            self._thread.join()
        with self._lock:
            pending_requests, self._queue = list(self._queue), deque()
            pending_changes, self._changes = list(self._changes), deque()
        for request in pending_requests:
            self._deliver(request.on_done, EngineError("The engine is stopping"))
        for _, change_done in pending_changes:
            self._deliver(change_done, EngineError("The engine is stopping"))

    def submit(self, adapter: str, rows: list[dict[str, Any]], deadline: float | None, on_done: Done) -> None:
        """Prepares the request (engine.prepare, on the calling thread: the engine server calls this from one of its
        --prepare-workers preparation threads, never from its event loop or the GPU thread) and queues it with its
        prepared rows, or delivers the error. Several threads may call this at once."""
        arrived = time.monotonic()
        if len(rows) > self.settings.queue_limit_rows:
            self._deliver(on_done, InvalidRequest(
                f"The request has {len(rows)} rows, more than this engine's queue holds "
                f"(queue_limit_rows {self.settings.queue_limit_rows}); split it into smaller requests"))
            return
        try:
            prepared = self.engine.prepare(adapter, rows)
        except JeffError as error:
            self._deliver(on_done, error)
            return
        with self._lock:
            if self._broken is not None:
                rejection: JeffError = self._broken
            elif self._stopping:
                rejection = EngineError("The engine is stopping")
            else:
                queued = sum(request.unclaimed for request in self._queue)
                if queued + len(rows) > self.settings.queue_limit_rows:
                    self._counts["busy"] += 1
                    rejection = Busy(f"The engine queue is full ({queued} of {self.settings.queue_limit_rows} "
                                     "rows); retry shortly", self._retry_after_s(queued + len(rows)))
                else:
                    self._queue.append(_Request(adapter, prepared, deadline, on_done, arrived))
                    self._lock.notify_all()
                    return
        self._deliver(on_done, rejection)

    def _retry_after_s(self, wanted_rows: int) -> float:
        """How long until `wanted_rows` queued rows would fit under queue_limit_rows: the rows over the limit,
        divided into micro-batches of max_batch_rows, times the median wall time of the last 100 engine calls. With
        no engine call finished yet there is nothing to estimate from, so the answer is 1 second. Called with the
        lock held."""
        if not self._batch_s:
            return 1.0
        batches = math.ceil((wanted_rows - self.settings.queue_limit_rows) / self.settings.max_batch_rows)
        return round(batches * statistics.median(self._batch_s), 3)

    def change(self, work: Callable[[], None], on_done: Callable[[JeffError | None], None]) -> None:
        with self._lock:
            if self._broken is not None:
                rejection: JeffError = self._broken
            elif self._stopping:
                rejection = EngineError("The engine is stopping")
            else:
                self._changes.append((work, on_done))
                self._lock.notify_all()
                return
        self._deliver(on_done, rejection)

    def stats(self) -> dict[str, Any]:
        with self._lock:
            ms = sorted(self._model_ms)
            queued = sum(request.unclaimed for request in self._queue)
            counts = dict(self._counts)
        pick = (lambda q: ms[min(len(ms) - 1, int(q * len(ms)))]) if ms else (lambda q: None)
        return {"queued_rows": queued, **counts, "model_ms_p50": pick(0.5), "model_ms_p99": pick(0.99),
                "mean_batch_rows": counts["rows"] / counts["batches"] if counts["batches"] else None}

    # ---- the GPU thread
    def _loop(self) -> None:
        try:
            while True:
                self._run_changes()
                batch = self._gather()
                if batch is None:
                    return
                self._run(batch)
        except Exception as error:  # noqa: BLE001 - the GPU thread died; every client must be told, not left
                                     # hanging, and the scheduler must never queue into a dead thread again
            broken = EngineError(f"The GPU thread failed: {type(error).__name__}: {error}")
            broken.__cause__ = error
            log.exception("The GPU thread failed unexpectedly; the scheduler is now broken")
            with self._lock:
                self._broken = broken
                pending_requests, self._queue = list(self._queue), deque()
                pending_changes, self._changes = list(self._changes), deque()
                listeners, self._broken_listeners = self._broken_listeners, []
            for request in pending_requests:
                self._deliver(request.on_done, broken)
            for _, change_done in pending_changes:
                self._deliver(change_done, broken)
            for listener in listeners:
                self._deliver(listener, broken)

    def _run_changes(self) -> None:
        while True:
            with self._lock:
                if not self._changes:
                    return
                work, on_done = self._changes.popleft()
            try:
                work()
            except JeffError as error:
                self._deliver(on_done, error)
                continue
            except Exception as error:  # noqa: BLE001 - reported to the caller as a typed error
                wrapped = EngineError(f"{type(error).__name__}: {error}")
                wrapped.__cause__ = error
                self._deliver(on_done, wrapped)
                continue
            self._deliver(on_done, None)

    def _gather(self) -> _Batch | None:
        with self._lock:
            while not self._queue and not self._changes and not self._stopping:
                self._lock.wait()
            if self._stopping:
                return None
            if not self._queue:
                return []
            end = self._queue[0].arrived + self.settings.window_ms / 1000
            while sum(r.unclaimed for r in self._queue) < self.settings.max_batch_rows and not self._stopping:
                left = end - time.monotonic()
                if left <= 0:
                    break
                self._lock.wait(left)
            if self._stopping:
                return None
            now = time.monotonic()
            expired = [r for r in self._queue if r.deadline is not None and r.deadline <= now]
            for request in expired:
                self._queue.remove(request)
                self._counts["deadline_exceeded"] += 1
            groups: dict[_Key, list[_Part]] = {}
            part_index: dict[int, int] = {}  # id(request) -> its index in its group: a started request can be given
                                              # room in both the reserved pass and the leftover pass below; it must
                                              # end up with exactly one part in the batch (extended), never two
                                              # (which would run it, and deliver its result or failure, twice).
            room = self.settings.max_batch_rows

            def take(request: _Request, limit: int) -> int:
                taken = min(limit, request.unclaimed)
                if taken <= 0:
                    return 0
                group = groups.setdefault((request.adapter, request.prepared.route, request.prepared.generation), [])
                index = part_index.get(id(request))
                if index is None:
                    part_index[id(request)] = len(group)
                    group.append((request, request.claimed, request.claimed + taken))
                else:
                    existing_request, start, stop = group[index]
                    group[index] = (existing_request, start, stop + taken)
                if request.claimed == 0:
                    request.queue_ms = round((now - request.arrived) * 1000, 3)
                request.claimed += taken
                return taken

            # One micro-batch holds at most max_batch_rows rows, every adapter's together. Requests already part-way
            # through their chunks, of any adapter, keep a guaranteed share of half of it (max_batch_rows // 2, at
            # least 1 row) before requests that have not started yet get a turn, so a started request keeps making
            # progress instead of being starved by a steady stream of fresh requests, on its own adapter or any
            # other. Fresh requests then fill the room in arrival order, and whatever room is left -- because there
            # was no fresh request to fill it, or a started request still wants more -- goes to the started requests
            # too. With no started requests, fresh ones take everything; with no fresh ones, started ones take
            # everything.
            fresh = [request for request in self._queue if request.claimed == 0]
            started = [request for request in self._queue if request.claimed > 0]
            reserved = min(room, max(self.settings.max_batch_rows // 2, 1))
            for request in started:
                taken = take(request, reserved)
                reserved -= taken
                room -= taken
            for request in fresh:
                room -= take(request, room)
            for request in started:
                room -= take(request, room)
        for request in expired:
            self._deliver(request.on_done, DeadlineExceeded(
                f"The request waited {round((now - request.arrived) * 1000)} ms and its deadline passed"))
        return list(groups.items())

    def _run(self, batch: _Batch) -> None:
        """One engine call (predict_many) for the whole micro-batch, one group per (adapter, route, generation)."""
        if not batch:
            return
        groups = [(adapter, Prepared.join([request.prepared.part(start, stop) for request, start, stop in parts]))
                  for (adapter, _, _), parts in batch]
        rows = 0
        for _, prepared in groups:
            rows += len(prepared.rows)
        started = time.monotonic()
        try:
            results = self.engine.predict_many(groups)
        except JeffError as error:
            self._finish_failed(self._every_part(batch), error)
            return
        except Exception as error:  # noqa: BLE001 - every client of this micro-batch gets a typed error
            wrapped = EngineError(f"{type(error).__name__}: {error}")
            wrapped.__cause__ = error
            self._finish_failed(self._every_part(batch), wrapped)
            return
        if len(results) != len(groups):
            self._finish_failed(self._every_part(batch), EngineError(
                f"The engine returned {len(results)} results for {len(groups)} groups"))
            return
        elapsed = time.monotonic() - started
        with self._lock:
            self._batch_s.append(elapsed)
            self._counts["batches"] += 1
            self._counts["rows"] += rows
            self._model_ms.append(round(elapsed * 1000, 3))
        for ((adapter, _, _), parts), (_, prepared), result in zip(batch, groups, results, strict=True):
            if isinstance(result, JeffError):
                self._finish_failed(parts, result)
                continue
            problem = self._mismatch(adapter, len(prepared.rows), result)
            if problem is not None:
                self._finish_failed(parts, EngineError(problem))
                continue
            self._finish(parts, result, rows)

    @staticmethod
    def _every_part(batch: _Batch) -> list[_Part]:
        return [part for _, parts in batch for part in parts]

    @staticmethod
    def _mismatch(adapter: str, rows: int, result: EngineResult) -> str | None:
        """Why the engine's result for a group of `rows` rows cannot be handed out, or None."""
        if len(result.probabilities) != rows:
            return f"The engine returned {len(result.probabilities)} probabilities for {rows} rows sent to {adapter}"
        if result.input_tokens is not None and len(result.input_tokens) != rows:
            return (f"The engine returned {len(result.input_tokens)} input token counts for {rows} rows sent to "
                    f"{adapter}")
        if result.row_timing:
            for name, values in result.row_timing.items():
                if len(values) != rows:
                    return f"The engine returned {len(values)} values of {name} for {rows} rows sent to {adapter}"
        return None

    def _finish(self, parts: list[_Part], result: EngineResult, batch_rows: int) -> None:
        """Hands each part of a group its rows' share of the group's result; delivers the requests now complete."""
        offset = 0
        for request, start, stop in parts:
            count = stop - start
            request.results.extend(result.probabilities[offset:offset + count])
            if result.input_tokens is None:
                request.input_tokens = None
            elif request.input_tokens is not None:
                request.input_tokens.extend(result.input_tokens[offset:offset + count])
            if result.row_timing:
                for name, values in result.row_timing.items():
                    request.row_timing.setdefault(name, []).extend(values[offset:offset + count])
            offset += count
            request.timing = {**result.timing, **request.row_timing, "queue_ms": request.queue_ms,
                              "batch_rows": batch_rows}
            with self._lock:
                request.taken = stop
                done = request.remaining == 0
                if done:
                    self._queue.remove(request)
            if done:
                self._deliver(request.on_done, EngineResult(request.results, request.timing, request.input_tokens))

    def _finish_failed(self, parts: list[_Part], error: JeffError) -> None:
        for request, _, _ in parts:
            with self._lock:
                if request in self._queue:
                    self._queue.remove(request)
            self._deliver(request.on_done, error)

    @staticmethod
    def _deliver(on_done: Callable[[Any], None], value: Any) -> None:
        try:
            on_done(value)
        except Exception:  # noqa: BLE001 - one broken client must not stop the GPU thread; logged, not hidden
            log.exception("Delivering a result to a client failed")
