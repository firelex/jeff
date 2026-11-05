"""The factory's job queue, shared by the API and the factory worker through files in JEFF_FACTORY_DIR/queue/.

The API never runs a build step itself: it writes a job (a run and the steps to run, in order) and the worker
(jeff_server.factory.worker) claims and runs one job at a time, across all runs. Previews are not queued: their one
step (interpret: teacher calls, no GPU) runs in the API process (jeff_server.factory.runner.Factory.launch).

- A job waiting is queue/<20-digit nanosecond timestamp>-<run id>.json, written to a .tmp file first and then renamed,
  so the worker never reads half a job. The oldest name is the next job.
- A job running is queue/claimed/<the same name>: claiming is a rename, and the worker deletes the claim when the
  job ends.
- A run has at most one job, waiting or running: queueing a second one is refused with Conflict.

Queueing and claiming hold an exclusive flock on queue/.lock, so two API processes and the worker never interleave
a check and a write."""

import fcntl
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jeff.errors import Conflict

from jeff_server.factory.store import now


@dataclass(frozen=True)
class Job:
    id: str
    run: str
    steps: list[str]
    queued: str
    path: Path

    def public(self) -> dict[str, Any]:
        return {"id": self.id, "run": self.run, "steps": self.steps, "queued": self.queued}


def folder(root: Path) -> Path:
    path = root / "queue" / "claimed"
    path.mkdir(parents=True, exist_ok=True)
    return path.parent


@contextmanager
def locked(root: Path) -> Iterator[Path]:
    queue = folder(root)
    with (queue / ".lock").open("a") as file:
        fcntl.flock(file, fcntl.LOCK_EX)
        try:
            yield queue
        finally:
            fcntl.flock(file, fcntl.LOCK_UN)


def read(path: Path) -> Job:
    value = json.loads(path.read_text(encoding="utf-8"))
    return Job(id=path.stem, run=value["run"], steps=list(value["steps"]), queued=value["queued"],
               path=path)


def waiting(root: Path) -> list[Job]:
    """The jobs not yet claimed, oldest first."""
    return [read(path) for path in sorted(folder(root).glob("*.json"))]


def claimed(root: Path) -> list[Job]:
    """The jobs claimed by a worker: at most one while a worker runs; one left behind by a worker that stopped is
    cleared when the next worker starts (jeff_server.factory.worker.recover)."""
    return [read(path) for path in sorted((folder(root) / "claimed").glob("*.json"))]


def job_of(root: Path, run_id: str) -> tuple[Job, str] | None:
    """The run's job and whether it is "waiting" or "running", or None when it has none."""
    for state, jobs in (("running", claimed(root)), ("waiting", waiting(root))):
        for job in jobs:
            if job.run == run_id:
                return job, state
    return None


def enqueue(root: Path, run_id: str, steps: list[str]) -> str:
    """Queue `steps` of the run; returns the job's id. Conflict when the run already has a job waiting or running."""
    with locked(root) as queue:
        found = job_of(root, run_id)
        if found is not None:
            job, state = found
            raise Conflict(f"Run {run_id} already has a job {state} ({', '.join(job.steps)}); wait for it to end")
        job_id = f"{time.time_ns():020d}-{run_id}"
        temporary = queue / f".{job_id}.json.tmp"
        temporary.write_text(json.dumps({"run": run_id, "steps": steps, "queued": now()}) + "\n",
                             encoding="utf-8")
        temporary.replace(queue / f"{job_id}.json")
        return job_id


def claim(root: Path) -> Job | None:
    """Move the oldest waiting job to claimed/ and return it, or None when nothing is waiting."""
    with locked(root) as queue:
        jobs = sorted(queue.glob("*.json"))
        if not jobs:
            return None
        target = queue / "claimed" / jobs[0].name
        jobs[0].replace(target)
        return read(target)


def finish(job: Job) -> None:
    """The job has ended (its steps ran, or one failed): delete its claim."""
    job.path.unlink()
