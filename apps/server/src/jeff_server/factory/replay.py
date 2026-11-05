"""Recorded runs for investor mode. "Save as replay" copies a finished, clean timed run's spec, events (every
timestamp and progress line), metrics, check report (with its 20-row sample), training summary, publish result and
data card into replays/<run id>/. Never the full data. A replay is read-only and never recomputed."""

import shutil
from pathlib import Path
from typing import Any

from jeff_server.factory.store import Run, read_json, read_jsonl

FILES = ["run.json", "spec.json", "events.jsonl", "checks.json", "metrics.json", "train.json", "publish.json"]


def save(run: Run, replays: Path) -> dict[str, Any]:
    state = run.state
    if not state["clean"]:
        raise ValueError("Only a clean timed run can be saved as a replay; this one is not: "
                         + "; ".join(state["unclean_reasons"]))
    kinds = {event["kind"] for event in run.events()}
    if "run.end" not in kinds:
        raise ValueError("The run has not finished (no run.end event)")
    destination = replays / run.id
    if destination.exists():
        raise ValueError(f"A replay of {run.id} already exists")
    staging = replays / f".{run.id}.saving"
    staging.mkdir()
    for name in FILES:
        shutil.copy2(run.folder / name, staging / name)
    staging.rename(destination)
    return summary(destination)


def summary(folder: Path) -> dict[str, Any]:
    state = read_json(folder / "run.json")
    spec = read_json(folder / "spec.json")
    metrics = read_json(folder / "metrics.json")
    return {"id": folder.name, "name": spec["name"], "summary": spec["summary"], "created": state["created"],
            "accuracy": {name: metrics[name]["accuracy"] for name in ("synthetic", "independent") if name in metrics}}


def listing(replays: Path) -> list[dict[str, Any]]:
    return [summary(folder) for folder in sorted(replays.iterdir(), reverse=True)
            if folder.is_dir() and not folder.name.startswith(".")]


def detail(replays: Path, replay_id: str) -> dict[str, Any]:
    folder = replays / replay_id
    if "/" in replay_id or replay_id.startswith(".") or not folder.is_dir():
        raise KeyError(replay_id)
    state = read_json(folder / "run.json")
    return {"run": public_state(state), "spec": read_json(folder / "spec.json"), "events": read_jsonl(folder / "events.jsonl"),
            "checks": read_json(folder / "checks.json"), "metrics": read_json(folder / "metrics.json"),
            "train": read_json(folder / "train.json"), "publish": read_json(folder / "publish.json")}


def public_state(state: dict[str, Any]) -> dict[str, Any]:
    """The run fields a replay shows. spec_from (the run a spec was copied from) is None for replays recorded before
    spec_from existed (runs from before 17 October 2026, 18:00, which never record it): the replay then shows no
    source run. This is for those old replays only; every other field is required (owner's ruling, plan 2a-5)."""
    state = {"spec_from": None, **state}
    return {key: state[key] for key in ("id", "created", "description", "status", "clean", "approved", "spec_sha256",
                                        "gate", "independent_test", "steps", "description_check", "mode", "reference", "spec_from")}
