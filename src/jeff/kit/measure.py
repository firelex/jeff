"""The three measurements on one test set, run with jeff-evaluate: the untrained base model, Jeff alone, and Jeff with
the adapter. Each runs as its own `python -m jeff.evaluate` process, one after another, so only one model is loaded
at a time.

jeff-evaluate loads full decision checkpoints (a folder with decision_config.json). It cannot load a LoRA adapter
folder on its own: merge the adapter into Jeff first and pass the merged folder as --adapter."""

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

MEASUREMENTS = ("untrained", "jeff", "adapter")
REVISION = re.compile(r"^[0-9a-f]{40}$")


class Result(TypedDict):
    name: str
    accuracy: float
    ece: float
    count: int


def checkpoint_folder(path: Path, role: str) -> Path:
    """A folder jeff-evaluate can load; a LoRA-only folder or a missing one is an error that says what to do."""
    if not path.is_dir():
        raise FileNotFoundError(f"{role} {path} is not a folder")
    if (path / "decision_config.json").is_file():
        return path
    if (path / "adapter_config.json").is_file():
        raise ValueError(f"{role} {path} holds LoRA adapter weights only (adapter_config.json, no decision_config.json). "
                         "jeff-evaluate loads full decision checkpoints: merge the adapter into Jeff, save the merged model "
                         "as a decision checkpoint, and pass that folder.")
    raise ValueError(f"{role} {path} has no decision_config.json, so it is not a Jeff decision checkpoint")


def commands(test: Path, base: Path, adapter: Path, out: Path, *, untrained: str | None, untrained_revision: str | None,
             calibration: Path | None, orders: int, batch_size: int) -> list[tuple[str, list[str]]]:
    """The jeff-evaluate command line for each measurement, in order. Settings that cannot work are errors."""
    if not test.is_file():
        raise FileNotFoundError(f"Test file {test} does not exist")
    checkpoint_folder(base, "--base")
    checkpoint_folder(adapter, "--adapter")
    if (untrained is None) != (untrained_revision is None):
        raise ValueError("--untrained and --untrained-revision go together (jeff-evaluate pins the exact model commit)")
    if untrained_revision is not None and not REVISION.match(untrained_revision):
        raise ValueError(f"--untrained-revision must be a 40-character commit hash, got {untrained_revision!r}")
    if calibration is not None and untrained is None:
        raise ValueError("--calibration fits a temperature for the untrained model only; it needs --untrained")
    if calibration is not None and not calibration.is_file():
        raise FileNotFoundError(f"Calibration file {calibration} does not exist")
    if orders not in (1, 2):
        raise ValueError("--orders must be 1 or 2")
    if batch_size < 1:
        raise ValueError("--batch-size must be positive")
    common = ["--data", str(test), "--local", "--orders", str(orders), "--batch-size", str(batch_size)]
    planned: list[tuple[str, list[str]]] = []
    if untrained is not None and untrained_revision is not None:
        extra = ["--base-model", untrained, "--revision", untrained_revision]
        if calibration is not None:
            extra += ["--calibration", str(calibration)]
        planned.append(("untrained", extra))
    planned += [("jeff", ["--checkpoint", str(base)]), ("adapter", ["--checkpoint", str(adapter)])]
    result = []
    for name, extra in planned:
        output = out / f"{name}.json"
        for path in (output, output.with_suffix(".predictions.jsonl")):
            if path.exists():
                raise FileExistsError(f"Refusing to overwrite {path}; choose another --out folder")
        result.append((name, [sys.executable, "-m", "jeff.evaluate", *common, "--output", str(output), *extra]))
    return result


def read_result(name: str, path: Path) -> Result:
    overall = json.loads(path.read_text())["overall"]
    return {"name": name, "accuracy": float(overall["accuracy"]), "ece": float(overall["ece"]), "count": int(overall["count"])}


def results_block(results: list[Result]) -> str:
    labels = {"untrained": "untrained base model", "jeff": "Jeff alone", "adapter": "Jeff with the adapter"}
    lines = [f"{'model':<24}{'rows':>7}{'accuracy':>10}{'calibration error (ECE)':>25}"]
    lines += [f"{labels[r['name']]:<24}{r['count']:>7}{r['accuracy']:>10.1%}{r['ece']:>25.3f}" for r in results]
    return "\n".join(lines)


def run(planned: list[tuple[str, list[str]]], out: Path) -> list[Result]:
    """Run each measurement in turn; a failed run stops the rest with its error."""
    out.mkdir(parents=True, exist_ok=True)
    for name, command in planned:
        print(f"== {name}: {' '.join(command)}", flush=True)
        subprocess.run(command, check=True)
    return [read_result(name, out / f"{name}.json") for name, _ in planned]
