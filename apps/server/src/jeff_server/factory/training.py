"""Training: the pinned trainer (JEFF_TRAINER, the commit that trained the v1.3 adapters and the learning curve) with
the learning-curve settings, started from the server's own base checkpoint so the adapter can be served next to the
others: LoRA rank 16, alpha 32, dropout 0, learning rate 2e-4, readout learning rate 5e-6, weight decay 0.01, batch 16
with no accumulation, a fixed 250 updates (the training rows repeated to 16 x 250 rows, one pass over that file), no
replay, live-last layout, the temperature fitted on the calibration split, an evaluation on the dev split every 25
updates (every update, for more than 250 updates: every 10% of them). The trainer runs as its own process on the
server's GPU (the factory session's CUDA_VISIBLE_DEVICES), with a minimal environment that holds no key
(trainer_environment); its events become progress lines: train.step (update, loss) and train.eval (update, dev
accuracy, dev loss, calibration error)."""

import json
import os
import subprocess
import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jeff_server.factory.spec import BATCH

SEED = 20260920
EVAL_EVERY = 25
REVISION = "2fc06364715b967f1860aea9cf38778875588b17"


def free_gpu_gb() -> tuple[float, float]:
    import torch

    if not torch.cuda.is_available():
        raise ValueError("Training needs a CUDA GPU and this server has none")
    free, total = torch.cuda.mem_get_info()
    return free / 2**30, total / 2**30


def repeated_training_file(train: list[dict[str, Any]], path: Path, updates: int) -> int:
    total = updates * BATCH
    if total % len(train):
        raise ValueError(f"{total} rows is not a whole number of passes over {len(train)} training rows")
    passes = total // len(train)
    with path.open("w", encoding="utf-8") as file:
        for index in range(passes):
            for row in train:
                file.write(json.dumps({**row, "id": f"{row['id']}:p{index}"}, ensure_ascii=False) + "\n")
    return passes


def command(trainer: Path, base: str, data: Path, out: Path, eval_every: int) -> list[str]:
    return [str(trainer / ".venv" / "bin" / "jeff-train"),
            "--train", str(data / "train-repeated.jsonl"), "--development", str(data / "dev.jsonl"),
            "--temperature", str(data / "calibration.jsonl"), "--run", str(out / "run"), "--output", str(out / "checkpoints"),
            "--base-model", "Qwen/Qwen3.5-0.8B", "--revision", REVISION, "--initial-checkpoint", base,
            "--epochs", "1", "--seed", str(SEED), "--weight-decay", "0.01", "--batch-size", str(BATCH),
            "--effective-batch-size", str(BATCH), "--token-budget", "8192", "--max-length", "8192", "--cpu-threads", "16",
            "--eval-every", str(eval_every), "--public-eval-every", "1000000", "--resume-every", "100",
            "--lora-rank", "16", "--lora-alpha", "32", "--lora-dropout", "0", "--lr", "2e-4", "--readout-lr", "5e-6",
            "--prompt-layout", "live-last"]


def progress_line(event: dict[str, Any]) -> tuple[str, dict[str, Any]] | None:
    """The console's progress line for one trainer event, or None for events it does not show."""
    kind = event.get("kind")
    if kind == "training_step":
        return "train.step", {"step": event["step"], "loss": event["loss"], "examples": event["examples_seen"],
                              "step_seconds": round(event["step_seconds"], 3), "gpu_peak_gb": event.get("gpu_peak_gb")}
    if kind == "evaluation":
        raw = event["raw"]
        return "train.eval", {"step": event["step"], "dev_accuracy": raw["accuracy"], "dev_loss": raw["nll"],
                              "dev_ece": raw["ece"], "temperature": event["temperature"],
                              "seconds": round(event["evaluation_seconds"], 1)}
    if kind == "training_finished":
        return "train.finished", {"steps": event["steps"], "complete": event["complete"],
                                  "temperature": event["selected_temperature"], "git_commit": event["git_commit"]}
    return None


# What the trainer gets from the worker's environment: where to find programs and the model caches, the user's home,
# locale and temporary folder, and the GPU (CUDA_VISIBLE_DEVICES, which deploy/run.sh sets from JEFF_FACTORY_GPU).
# Nothing else: the worker's environment holds the API key and the teacher's key, which the trainer must not see.
TRAINER_VARIABLES = ("PATH", "HOME", "USER", "LANG", "LC_ALL", "TMPDIR", "XDG_CACHE_HOME", "TORCH_HOME",
                     "CUDA_VISIBLE_DEVICES", "PYTORCH_CUDA_ALLOC_CONF")


def trainer_environment(parent: Mapping[str, str], events: Path) -> dict[str, str]:
    """The trainer's environment: TRAINER_VARIABLES and the Hugging Face cache and offline settings (HF_*, but never
    a token) from `parent`, the ones it is given, and offline mode (it trains from the local base checkpoint)."""
    kept = {name: value for name, value in parent.items()
            if name in TRAINER_VARIABLES or (name.startswith("HF_") and "TOKEN" not in name)}
    return {**kept, "JEFF_EVENTS": str(events), "PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1",
            "HF_HUB_OFFLINE": "1"}


def train(trainer: Path, base: str, data: Path, out: Path, emit: Callable[[str, dict[str, Any]], None],
          started: Callable[[int], None], updates: int) -> dict[str, Any]:
    """Run the trainer to the end, forwarding its events as they are written. Returns what the end event records."""
    out.mkdir(parents=True, exist_ok=False)
    events = out / "trainer-events.jsonl"
    log = out / "train.log"
    environment = trainer_environment(os.environ, events)
    with log.open("w") as log_file:
        # an evaluation on the dev split every 25 updates (11 points for 250), every tenth of the run for longer runs
        eval_every = max(EVAL_EVERY, updates // 10)
        process = subprocess.Popen(command(trainer, base, data, out, eval_every), cwd=trainer, env=environment,
                                   stdout=log_file, stderr=subprocess.STDOUT, start_new_session=True)
    started(process.pid)
    read = 0
    finished: dict[str, Any] = {}
    while True:
        code = process.poll()
        if events.exists():
            with events.open(encoding="utf-8") as file:
                file.seek(read)
                chunk = file.read()
            complete = chunk[:chunk.rfind("\n") + 1]
            read += len(complete.encode("utf-8"))
            for line in complete.splitlines():
                if not line.strip():
                    continue
                shown = progress_line(json.loads(line))
                if shown is not None:
                    emit(*shown)
                    if shown[0] == "train.finished":
                        finished = shown[1]
        if code is not None:
            break
        time.sleep(1.0)
    if code != 0:
        tail = log.read_text(errors="replace")[-1500:]
        raise RuntimeError(f"The trainer exited with code {code}. End of {log}:\n{tail}")
    adapter = out / "checkpoints" / "final"
    if not (adapter / "adapter_model.safetensors").exists():
        raise RuntimeError(f"The trainer finished but {adapter} has no adapter_model.safetensors")
    config = json.loads((adapter / "decision_config.json").read_text())
    if config.get("prompt_layout") != "live-last":
        raise RuntimeError(f"{adapter}: prompt_layout is {config.get('prompt_layout')!r}, not live-last")
    if not finished.get("complete"):
        raise RuntimeError("The trainer did not report a complete run")
    return {**finished, "adapter": str(adapter), "max_options": config.get("max_options")}
