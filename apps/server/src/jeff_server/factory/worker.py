"""The factory worker: the one process that runs build steps (`jeff-server factory worker`). The API only queues
jobs (jeff_server.factory.queue); this process claims them one at a time, oldest first, across every run, and runs each job's steps through Factory.step, so the API never trains, scores or calls the teacher for a build step,
and restarting the API leaves a running step alone.

- One worker at a time: it holds an exclusive flock on JEFF_FACTORY_DIR/worker.lock for as long as it runs, and a
  second worker is refused at start, naming the first one's process.
- Heartbeat: worker.json holds {pid, started, heartbeat, current_job}, rewritten every HEARTBEAT_S seconds by a
  thread of its own, so it stays fresh during a step that takes hours. The API reports the worker as running only
  while the heartbeat is at most STALE_S seconds old (worker_status). If the heartbeat thread dies, the worker stops
  with an error at its next look at the queue, rather than run on while the API reports it stopped.
- Recovery: at start, the worker holds the lock, so no other worker is running. A train step still marked running
  whose trainer process (its recorded pid) is still alive stops the start: that trainer is still using the GPU and
  writing into the run, and the worker never kills anything itself. Otherwise every build step still marked running
  was left by a worker that stopped: it is failed with "the worker stopped during <step>" and its run is marked
  unclean. A claimed job left behind is cleared; its run, if none of its steps was running and some step of the job
  is not done, gets a job.failed event and the status failed (a job whose steps all finished, left only because the
  worker stopped before deleting the claim, is just cleared).
- The base is the one the engines serve, read from each engine's health at start: training starts from its
  checkpoint, scoring loads it, and a published manifest names its id and version. Every engine must serve the same
  base. The engine sockets, the adapter folders (JEFF_ADAPTER_DIRS: new adapters go into the first) and the
  catalogue (JEFF_CATALOGUE) are kept for publishing, which reloads the engines and decides an example with the
  catalogue the API builds."""

import fcntl
import logging
import os
import threading
import time
from collections.abc import Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jeff.errors import EngineError, InvalidRequest

from jeff_server.factory import evaluation, queue
from jeff_server.factory.config import FactoryConfig
from jeff_server.factory.runner import Factory, WorkerHooks, alive
from jeff_server.factory.store import now, read_json, write_json

log = logging.getLogger("jeff_server.factory.worker")

HEARTBEAT_S = 5.0
STALE_S = 15.0
# How long reading an engine's health at start may take.
ENGINE_TIMEOUT_S = 30.0


def run_worker(config: FactoryConfig, engines: Sequence[Path], adapter_dirs: Sequence[Path], catalogue: Path | None,
               model_name: str, poll_s: float = 1.0, once: bool = False, *, score: evaluation.Scorer = evaluation.score) -> None:
    """Run jobs until stopped (KeyboardInterrupt), or with once=True until no job is waiting. adapter_dirs and
    catalogue: as the API's JEFF_ADAPTER_DIRS and JEFF_CATALOGUE (None: the catalogue shipped with jeff); model_name:
    the API's JEFF_MODEL_NAME. score: the
    scorer calibration and evaluation use (jeff_server.factory.evaluation.score; tests pass a fake one)."""
    if config.mode != "owner":
        raise InvalidRequest("The factory worker runs build steps, which need JEFF_FACTORY=owner")
    if not engines:
        raise InvalidRequest("The factory worker needs at least one --engine-socket: it trains on the engines' base "
                             "and reloads them after a publish")
    if not adapter_dirs:
        raise InvalidRequest("The factory worker needs JEFF_ADAPTER_DIRS: it publishes new adapters into the first")
    if not adapter_dirs[0].is_dir():
        raise InvalidRequest(f"The adapter folder {adapter_dirs[0]} (the first of JEFF_ADAPTER_DIRS) is not a folder")
    root = config.required_root
    with (root / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise EngineError(f"Another factory worker is running ({describe_other(root)}); one worker runs at a "
                              "time") from error
        base = engine_base(engines)
        hooks = WorkerHooks(base=base["checkpoint"], base_id=base["id"], base_version=base["version"],
                            model_name=model_name, adapter_dirs=tuple(adapter_dirs), catalogue=catalogue, engines=tuple(engines), score=score)
        factory = Factory(config, hooks)
        recover(root, factory)
        heartbeat = Heartbeat(root)
        heartbeat.start()
        try:
            while True:
                heartbeat.check()
                job = queue.claim(root)
                if job is None:
                    if once:
                        return
                    time.sleep(poll_s)
                    continue
                heartbeat.current_job = job.public()
                heartbeat.beat()
                try:
                    run_job(factory, job)
                finally:
                    queue.finish(job)
                    heartbeat.current_job = None
                    heartbeat.beat()
        finally:
            heartbeat.stop()


def run_job(factory: Factory, job: queue.Job) -> None:
    factory.work(factory.run(job.run), job.steps)


def recover(root: Path, factory: Factory) -> None:
    """Fail what a stopped worker left running, and clear its claimed jobs (see the module docstring)."""
    refuse_live_trainers(factory)
    interrupted = factory.mark_interrupted("the worker stopped")
    for job in queue.claimed(root):
        run = factory.run(job.run)
        steps = run.state["steps"]
        unfinished = [step for step in job.steps if steps.get(step, {}).get("status") != "done"]
        if job.run not in interrupted and unfinished:
            error = f"the worker stopped before the job's steps ({', '.join(job.steps)}) had all run"
            run.event("job.failed", job=job.id, steps=job.steps, error=error)
            run.update(status="failed")
        queue.finish(job)


def refuse_live_trainers(factory: Factory) -> None:
    """A train step marked running whose trainer process is still alive: EngineError naming it."""
    for folder in sorted(factory.folder.iterdir()):
        if not (folder / "run.json").exists():
            continue
        train = read_json(folder / "run.json")["steps"].get("train", {})
        pid = train.get("pid")
        if train.get("status") == "running" and pid is not None and alive(pid):
            raise EngineError(f"Run {folder.name}'s trainer (process {pid}) is still running from a worker that "
                              "stopped; stop that trainer first, then start the worker again")


def engine_base(engines: Sequence[Path]) -> dict[str, str]:
    """The base every engine serves, {"id", "version", "checkpoint"}, from each one's health; EngineError if they
    differ or one is down."""
    from jeff.socket_engine import SocketEngine

    bases: dict[str, dict[str, str]] = {}
    for path in engines:
        engine = SocketEngine(path, timeout_s=ENGINE_TIMEOUT_S)
        try:
            health = engine.health(timeout_s=ENGINE_TIMEOUT_S)["base"]
        finally:
            engine.close()
        missing = [key for key in ("id", "version", "checkpoint") if key not in health]
        if missing:
            raise EngineError(f"The engine at {path} reports no base {' or '.join(missing)} in its health; the "
                              "factory needs the base's id, version and checkpoint (update the engine)")
        bases[str(path)] = {key: str(health[key]) for key in ("id", "version", "checkpoint")}
    if len({tuple(base.values()) for base in bases.values()}) != 1:
        raise EngineError(f"The engines serve different bases: {bases}; the factory trains on one base")
    base = next(iter(bases.values()))
    if not Path(base["checkpoint"]).is_dir():
        raise EngineError(f"The engines' base {base['checkpoint']} is not a folder on this machine; the factory worker "
                          "trains on it and must run beside the engines")
    return base


def describe_other(root: Path) -> str:
    path = root / "worker.json"
    if not path.exists():
        return "it has written no worker.json yet"
    info = read_json(path)
    return f"process {info['pid']}, started {info['started']}"


class Heartbeat:
    """Writes worker.json every HEARTBEAT_S seconds from a thread of its own (a step can run for hours)."""

    def __init__(self, root: Path) -> None:
        self.path = root / "worker.json"
        self.started = now()
        self.current_job: dict[str, Any] | None = None
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.thread = threading.Thread(target=self.loop, name="factory-heartbeat", daemon=True)
        self.error: BaseException | None = None

    def start(self) -> None:
        self.beat()
        self.thread.start()

    def loop(self) -> None:
        try:
            while not self.stopping.wait(HEARTBEAT_S):
                self.beat()
        except Exception as error:
            # Logged now; the thread then ends, and check() stops the worker with this error at its next look at
            # the queue (a step already running finishes first).
            log.exception("The factory worker's heartbeat stopped")
            self.error = error

    def check(self) -> None:
        """EngineError when the heartbeat thread has died: the API would report the worker stopped."""
        if not self.thread.is_alive():
            raise EngineError(f"The factory worker's heartbeat stopped ({type(self.error).__name__}: {self.error}); "
                              "the worker stops too")

    def beat(self) -> None:
        with self.lock:
            write_json(self.path, {"pid": os.getpid(), "started": self.started, "heartbeat": now(),
                                   "current_job": self.current_job})

    def stop(self) -> None:
        self.stopping.set()
        self.thread.join()


def worker_status(root: Path) -> dict[str, Any]:
    """GET /v1/factory/worker: whether a worker is running (a heartbeat at most STALE_S seconds old), the heartbeat's
    age (None when no worker has ever run here), and the job claimed now (None when there is none)."""
    path = root / "worker.json"
    age = None
    if path.exists():
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(read_json(path)["heartbeat"])).total_seconds()
    claimed = queue.claimed(root)
    return {"running": age is not None and age <= STALE_S,
            "heartbeat_age_s": None if age is None else round(age, 1),
            "current_job": claimed[0].public() if claimed else None}

