"""Read-only progress dashboard for generation and training; serves one page and one JSON endpoint."""

import argparse
import json
import subprocess
from collections import Counter, defaultdict
from collections.abc import Callable
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

NVIDIA_SMI = "/usr/lib/wsl/lib/nvidia-smi"


def complete_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    text = path.read_text()
    lines = text.split("\n")[:-1]  # The piece after the last newline is unfinished (or empty).
    result = []
    for number, line in enumerate(lines, start=1):
        try:
            result.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise ValueError(f"{path} line {number} is not valid JSON: {error}") from error
    return result


def generation_status(out: Path) -> dict:
    plan = json.loads((out / "run.json").read_text())
    outcomes = complete_lines(out / "outcomes.jsonl")
    kept = sum(o["kept"] for o in outcomes)
    families: dict[str, Counter[str]] = defaultdict(Counter)
    for outcome in outcomes:
        families[outcome["family"]][outcome["reason"]] += 1
    times = [datetime.fromisoformat(o["time"]) for o in outcomes]
    hours = (max(times) - min(times)).total_seconds() / 3600 if len(times) > 1 else 0.0
    per_hour = len(outcomes) / hours if hours else None
    retries = complete_lines(out / "teacher.log")
    # The planned slot count is a deliberately oversized ceiling (runs are stopped long before), so no total or
    # time left is reported; "updated" (the latest result) shows whether the run is still going.
    return {"started": plan["started"], "done": len(outcomes), "kept": kept,
            "keep_rate": kept / len(outcomes) if outcomes else None,
            "slots_per_hour": per_hour, "kept_per_hour": kept / hours if hours else None,
            "updated": max(times).isoformat() if times else None,
            "by_family": {name: dict(counts) for name, counts in sorted(families.items())},
            "retries": len(retries), "last_retry": retries[-1] if retries else None}


def model_size(base_model: str) -> str:
    """The display name for a base model, e.g. 'Qwen/Qwen3.5-0.8B' -> 'Qwen3.5 0.8B'. Fails loudly for unknown models."""
    if base_model not in MODEL_LABELS:
        raise ValueError(f"No display name for {base_model!r}; add it to MODEL_LABELS")
    return MODEL_LABELS[base_model]


def training_status(run: Path) -> dict:
    events = complete_lines(run / "events.jsonl")
    config = json.loads((run / "config.json").read_text())
    size = model_size(config["base_model"])
    started = [e for e in events if e["kind"] == "training_started"]
    steps = complete_lines(run / "training.jsonl")
    evaluations = complete_lines(run / "evaluations.jsonl")
    total = started[-1]["total_steps"] if started else None
    eta = None
    if total is not None and len(steps) > 1:
        first, last = steps[0], steps[-1]
        seconds = (datetime.fromisoformat(last["timestamp"]) - datetime.fromisoformat(first["timestamp"])).total_seconds()
        eta = seconds / (last["step"] - first["step"]) * (total - last["step"])
    updated = steps[-1]["timestamp"] if steps else (started[-1]["timestamp"] if started else None)
    return {"model_size": size, "started": started[-1]["timestamp"] if started else None, "updated": updated,
            "step": steps[-1]["step"] if steps else 0, "total_steps": total,
            "loss": steps[-1]["loss"] if steps else None, "learning_rate": steps[-1].get("learning_rate") if steps else None,
            "losses": [[row["step"], row["loss"]] for row in steps],
            "evaluations": [{"step": e["step"], "accuracy": e["fitted"]["accuracy"], "nll": e["fitted"]["nll"],
                             "raw_nll": e["raw"]["nll"]} for e in evaluations],
            "selected_step": evaluations[-1]["selected_step"] if evaluations else None, "eta_seconds": eta,
            "finished": any(e["kind"] == "training_finished" for e in events),
            "finished_at": next((e["timestamp"] for e in events if e["kind"] == "training_finished"), None),
            "early_stopped": any(e["kind"] == "training_early_stopped" for e in events),
            "recipe": {"data": training_data(config), "train_rows": config["train_rows"],
                       "epochs": config["epochs"], "peak_learning_rate": config["lr"], "batch": config["effective_batch_size"],
                       "eval_every": config["eval_every"]}}


def gpu_status() -> dict:
    output = subprocess.run([NVIDIA_SMI, "--query-gpu=name,utilization.gpu,memory.used,memory.total",
                             "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True).stdout
    name, utilization, used, total = (part.strip() for part in output.strip().split(","))
    return {"name": name, "utilization_percent": int(utilization), "memory_used_mib": int(used), "memory_total_mib": int(total)}


SUITES = ("BBH", "Financial PhraseBank", "JudgeBench", "RAGTruth", "WinoGrande")


def training_data(config: dict) -> str:
    return Path(config["train"]).parent.name + "/" + Path(config["train"]).name


def panel_result(path: Path) -> dict:
    """A panel score, with the training data of the run it scores (runs/<run>/config.json); untrained models have no run."""
    result = json.loads(path.read_text())
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    config = path.parent.parent / path.stem.removesuffix("-calibrated").removesuffix("-raw") / "config.json"
    return {**result["overall"], "model_size": model_size(manifest["model"]),
            "data": training_data(json.loads(config.read_text())) if config.exists() else None,
            "by_suite": {suite: result["by_suite"][suite]["accuracy"] for suite in SUITES}}


JEVBENCH = "JevBench public hard"


def jevbench_result(path: Path) -> dict:
    """A model's score on JevBench's public hard tier: a sixth benchmark, kept out of the five-benchmark overall."""
    result = json.loads(path.read_text())
    manifest = json.loads(path.with_suffix(".manifest.json").read_text())
    return {"accuracy": result["by_suite"][JEVBENCH]["accuracy"], "count": result["by_suite"][JEVBENCH]["count"],
            "ece": result["overall"]["ece"], "model_size": model_size(manifest["model"])}


def published_reference(path: Path) -> dict:
    """AutoJev's and Jev's published numbers restricted to our five benchmarks (they were measured on a different sample).
    AutoJev's untrained starting model (Qwen3.8-27B) is left out: it is not a competitor."""
    results = json.loads(path.read_text())
    blocks = {"AutoJev-27B (published)": results["checkpoints"]["200"]["fitted"],
              "Jev (published)": results["jev"]["metrics"]}
    rows = {}
    for name, block in blocks.items():
        by = {suite: block["by_benchmark"][suite] for suite in SUITES}
        count = sum(value["count"] for value in by.values())
        rows[name] = {"accuracy": sum(value["accuracy"] * value["count"] for value in by.values()) / count,
                      "by_suite": {suite: value["accuracy"] for suite, value in by.items()},
                      "jevbench": block["by_benchmark"][JEVBENCH]["accuracy"]}
    return rows


def latency_result(path: Path) -> dict:
    """One speed measurement (jeff.latency): time per decision on one device, at each batch size."""
    result = json.loads(path.read_text())
    return {"model_size": model_size(result["base_model"]), "device": result["device"], "device_name": result["device_name"],
            "cpu_threads": result["cpu_threads"], "dtype": result["dtype"], "rows": result["rows"],
            "batches": {size: {"median_ms": b["median_ms"], "p95_ms": b["p95_ms"], "decisions_per_second": b["decisions_per_second"]}
                        for size, b in result["batches"].items()}}


RELEASE = {"0.8b": "Jeff-Qwen3.5-0.8B", "2b": "Jeff-Qwen3.5-2B", "g4": "Jeff-Gemma4-E2B"}
BASE = {"0.8b": "Qwen3.5 0.8B", "2b": "Qwen3.5 2B", "g4": "Gemma 4 E2B"}


def model_name(checkpoint: str) -> str:
    """A readable name for a checkpoint path: the release name and run time, or the base model for untrained ones.
    Paths may end in the run folder or in its selected/final pointer."""
    parts = Path(checkpoint).parts
    run = parts[-2] if parts[-1] in ("selected", "final") else parts[-1]
    if run.startswith("untrained-"):
        return f"{BASE[run.removeprefix('untrained-')]} untrained"
    tag, _, when = run.partition("-")
    if tag not in RELEASE:
        raise ValueError(f"Unknown model tag in checkpoint {checkpoint!r}")
    return f"{RELEASE[tag]} · {when}"


def game_result(path: Path) -> dict:
    """One game experiment (jeff.games): which game, wording and player, and the score over its episodes."""
    result = json.loads(path.read_text())
    player = (model_name(result["server"]["checkpoint"]) if result["player"] == "jeff" else
              {"random": "random", "rule": "rule bot"}[result["player"]])
    return {"game": result["game"], "player": player, "wording": result.get("criteria"), "episodes": len(result["episodes"]),
            "mean": result["mean_score"], "stdev": result["stdev_score"], "ms_per_decision": result["median_ms_per_decision"],
            "created": result.get("created") or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat(),
            "note": result.get("note"), "video": result.get("video"), "featured": result.get("featured", False)}


def section(read: Callable[[], dict]) -> dict:
    """One unreadable section is shown as its error on the page; other sections still render."""
    try:
        return read()
    except Exception as error:  # Displayed to the user verbatim, never swallowed.
        return {"error": f"{type(error).__name__}: {error}"}


def status(root: Path, gpu: Callable[[], dict] = gpu_status) -> dict:
    generation = {path.parent.name: section(lambda path=path: generation_status(path.parent))
                  for path in sorted((root / "gen").glob("*/run.json"))}
    training = {path.parent.name: section(lambda path=path: training_status(path.parent))
                for path in sorted((root / "runs").glob("*/training.jsonl"))}
    panel = {path.stem: section(lambda path=path: panel_result(path))
             for path in sorted((root / "runs" / "eval").glob("*.json"))
             if not path.name.endswith((".manifest.json",))}
    published = section(lambda: published_reference(root / "assets" / "results.json"))
    speed = {path.stem: section(lambda path=path: latency_result(path)) for path in sorted((root / "runs" / "latency").glob("*.json"))}
    games = {path.stem: section(lambda path=path: game_result(path)) for path in sorted((root / "runs" / "games").glob("*.json"))}
    jevbench = {path.stem: section(lambda path=path: jevbench_result(path))
                for path in sorted((root / "runs" / "eval" / "jevbench").glob("*.json")) if not path.name.endswith(".manifest.json")}
    return {"generation": generation, "training": training, "panel": panel, "published": published, "jevbench": jevbench, "speed": speed, "games": games, "gpu": section(gpu),
            "time": datetime.now().astimezone().isoformat()}


ACTIVE_WINDOW_SECONDS = 15 * 60
# Display names for every model in the comparison: family, then size (parameters in brackets when the name has none).
MODEL_LABELS = {"Qwen/Qwen3.5-0.8B": "Qwen3.5 0.8B", "Qwen/Qwen3.5-2B": "Qwen3.5 2B",
                "google/gemma-3-270m-it": "Gemma 3 270M (0.27B)", "google/gemma-4-E2B-it": "Gemma 4 E2B (2B)",
                "microsoft/Phi-4-mini-instruct": "Phi-4-mini (3.8B)",
                "answerdotai/ModernBERT-base": "ModernBERT-base (0.15B)", "answerdotai/ModernBERT-large": "ModernBERT-large (0.4B)"}


def archive_runs(root: Path, names: list[str]) -> list[str]:
    """Hide runs: move their small records (logs, panel scores) into _archive folders and delete their checkpoints,
    which are the only large part and are not used once a run is off the page. Deleting is irreversible.
    Refuses unknown names and runs that are still training."""
    if not names:
        raise ValueError("No runs selected")
    runs, checkpoints = root / "runs", root / "checkpoints"
    for name in names:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith(("_", ".")) or name == "eval":
            raise ValueError(f"Not a run name: {name!r}")
        if not (runs / name / "training.jsonl").is_file():
            raise ValueError(f"No such run: {name}")
        state = training_status(runs / name)
        if not state["finished"] and state["updated"] is not None:
            age = (datetime.now(timezone.utc) - datetime.fromisoformat(state["updated"])).total_seconds()
            if age < ACTIVE_WINDOW_SECONDS:
                raise ValueError(f"{name} is still training; stop it before archiving")
    for name in names:
        (runs / "_archive").mkdir(exist_ok=True)
        shutil.move(runs / name, runs / "_archive" / name)
        if (checkpoints / name).exists():
            shutil.rmtree(checkpoints / name)
        # Panel scores for the run (name-calibrated.*, name-raw.*) go too, so it disappears from the panel table.
        for path in (runs / "eval").glob(f"{name}-*"):
            if path.name.removeprefix(f"{name}-").split(".")[0] in ("calibrated", "raw"):
                (runs / "eval" / "_archive").mkdir(parents=True, exist_ok=True)
                shutil.move(path, runs / "eval" / "_archive" / path.name)
    return names


class ArchiveRequest(BaseModel):
    names: list[str]


def create_app(root: Path, gpu: Callable[[], dict] = gpu_status) -> FastAPI:
    app = FastAPI()
    page = (Path(__file__).parent / "dashboard.html").read_text(encoding="utf-8")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return page

    @app.get("/api/status")
    def api() -> dict:
        return status(root, gpu)

    @app.get("/videos/{name}")
    def game_video(name: str) -> FileResponse:
        """A recorded game episode (runs/games/videos/<name>.mp4)."""
        path = root / "runs" / "games" / "videos" / name
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.mp4", name) or not path.is_file():
            raise HTTPException(status_code=404, detail=f"No video {name!r}")
        return FileResponse(path, media_type="video/mp4")

    @app.post("/api/runs/archive")
    def archive(request: ArchiveRequest) -> dict:
        try:
            return {"archived": archive_runs(root, request.names)}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    uvicorn.run(create_app(args.root.resolve()), host="0.0.0.0", port=args.port)


if __name__ == "__main__":
    main()
