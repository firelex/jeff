"""A run's files: runs/<run id>/ holds run.json (state), spec.json (the task spec; frozen and hashed on approval),
events.jsonl (every step start, end and progress line, server clock, UTC), data/, the trainer's files and metrics."""

import fcntl
import hashlib
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STEPS = ["description_check", "confirm", "interpret", "review", "data_prep", "generate", "checks", "train", "calibrate", "evaluate",
         "independent_test", "publish"]
# The steps "Run the rest" runs, in order, after approval; each is one button in the console.
BUILD_STEPS = ["data_prep", "generate", "checks", "train", "calibrate", "evaluate", "publish"]
# Evaluation the customer would not wait for: shown, but left out of the headline time.
EVALUATION_ONLY = {"independent_test"}


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def write_jsonl(path: Path, rows: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as file:
        for row in rows:
            file.write(json.dumps(row, ensure_ascii=False) + "\n")


def read_jsonl(path: Path) -> list[Any]:
    with path.open(encoding="utf-8") as file:
        return [json.loads(line) for line in file if line.strip()]


def spec_hash(spec: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(spec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


class RunLock:
    """A run folder's lock, held across processes: the API's worker processes and the factory worker all read and
    change run.json and append to events.jsonl. A thread lock orders this process's threads (re-entrant, as a Run
    method may call another), and the outermost holder also takes an exclusive flock on the folder's .lock file,
    which orders the processes."""

    def __init__(self, folder: Path) -> None:
        self.path = folder / ".lock"
        self.thread_lock = threading.RLock()
        self.depth = 0
        self.file: Any = None

    def __enter__(self) -> "RunLock":
        self.thread_lock.acquire()
        try:
            if self.depth == 0:
                self.file = self.path.open("a")
                fcntl.flock(self.file, fcntl.LOCK_EX)
            self.depth += 1
        except BaseException:
            if self.depth == 0 and self.file is not None:
                self.file.close()
                self.file = None
            self.thread_lock.release()
            raise
        return self

    def __exit__(self, *exc: object) -> None:
        try:
            self.depth -= 1
            if self.depth == 0:
                fcntl.flock(self.file, fcntl.LOCK_UN)
                self.file.close()
                self.file = None
        finally:
            self.thread_lock.release()


LOCKS: dict[Path, RunLock] = {}
LOCKS_GUARD = threading.Lock()


def lock_for(folder: Path) -> RunLock:
    """One lock per run folder, shared by every Run object for it in this process (each request makes its own Run):
    reads and writes of run.json never overlap, in this process or another, which matters on file systems where a
    rename is not atomic (WSL's /mnt drives)."""
    with LOCKS_GUARD:
        resolved = folder.resolve()
        return LOCKS.setdefault(resolved, RunLock(resolved))


class Run:
    """One factory run on disk. Reading or changing run.json and appending events hold the run's shared lock."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder
        self.lock = lock_for(folder)

    @property
    def id(self) -> str:
        return self.folder.name

    @property
    def data(self) -> Path:
        path = self.folder / "data"
        path.mkdir(exist_ok=True)
        return path

    @property
    def state(self) -> dict[str, Any]:
        with self.lock:
            return read_json(self.folder / "run.json")

    def update(self, **changes: Any) -> dict[str, Any]:
        with self.lock:
            state = self.state
            state.update(changes)
            write_json(self.folder / "run.json", state)
            return state

    def set_step(self, step: str, **changes: Any) -> None:
        with self.lock:
            state = self.state
            state["steps"].setdefault(step, {}).update(changes)
            write_json(self.folder / "run.json", state)

    def event(self, kind: str, at: str | None = None, **fields: Any) -> dict[str, Any]:
        """Append an event stamped with the server clock now, or with `at` for a step that ran before the run existed
        (the description check)."""
        with self.lock:
            line = {"t": at or now(), "kind": kind, **fields}
            with (self.folder / "events.jsonl").open("a", encoding="utf-8") as file:
                file.write(json.dumps(line, ensure_ascii=False) + "\n")
            return line

    def events(self) -> list[dict[str, Any]]:
        path = self.folder / "events.jsonl"
        with self.lock:
            return read_jsonl(path) if path.exists() else []

    @property
    def spec(self) -> dict[str, Any]:
        return read_json(self.folder / "spec.json")


def new_run(runs: Path, name: str, description: str, examples: str | None,
            description_check: dict[str, Any] | None = None, imported: str | None = None,
            spec_from: str | None = None) -> Run:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = runs / f"{name}-{stamp}"
    folder.mkdir(parents=True, exist_ok=False)
    run = Run(folder)
    write_json(folder / "run.json", {
        "id": run.id, "created": now(), "description": description, "examples": examples, "name_hint": name,
        "status": "interpreting", "steps": {}, "spec_sha256": None, "approved": None, "clean": True,
        "unclean_reasons": [], "gate": None, "independent_test": None, "description_check": description_check,
        "mode": "import" if imported else "prompt", "reference": imported, "spec_from": spec_from,
    })
    return run
