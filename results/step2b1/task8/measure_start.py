"""Start-up cost of a fast engine (plan 2b-1 Task 8, Ruling B): the time LocalEngine takes to build with the nine v1.3
adapters on the fast backend (weights loaded, FastPath built, BatchedPath recorded) and the GPU memory this process
holds afterwards, with the graph-recording log lines. Run once before and once after the single-row graphs were removed,
each in a fresh process.

Run on datigator: .venv/bin/python results/step2b1/task8/measure_start.py --label before --out results/step2b1/task8/start_before.json"""

from __future__ import annotations

import argparse
import glob
import json
import logging
import time
from pathlib import Path

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
ADAPTERS = ("triage", "nav", "spam", "emotion", "ground", "guard", "legal-clauses", "support-intents", "tools")


class Lines(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if "recorded" in record.getMessage():
            self.lines.append(f"{record.name}: {record.getMessage()}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", type=Path, required=True)
    a = parser.parse_args()
    import torch

    from jeff_runtime import LocalEngine

    lines = Lines()
    for name in ("jeff.fast_path", "jeff.batched_path"):
        logging.getLogger(name).addHandler(lines)
        logging.getLogger(name).setLevel(logging.INFO)
    folders = {name: Path(glob.glob(CHECKPOINTS.format(name=name))[0]) for name in ADAPTERS}
    started = time.perf_counter()
    engine = LocalEngine(BASE, folders, backend="fast", prepare_workers=1, prefix_pool_mb=8192,
                         max_adapters=len(ADAPTERS))
    torch.cuda.synchronize()
    seconds = time.perf_counter() - started
    fast = engine.fast
    record = {"label": a.label, "date": time.strftime("%Y-%m-%d %H:%M"),
              "setup": {"adapters": len(ADAPTERS), "prefix_pool_mb": 8192, "prepare_workers": 1},
              "engine_start_seconds": round(seconds, 1),
              "gpu_memory_gb": {"allocated": round(torch.cuda.memory_allocated() / 2**30, 2),
                                "reserved": round(torch.cuda.memory_reserved() / 2**30, 2),
                                "max_reserved": round(torch.cuda.max_memory_reserved() / 2**30, 2)},
              "fast_path_graphs": len(getattr(fast, "graphs", {})) + len(getattr(fast, "prefix_graphs", {})),
              "batched_graphs": len(engine.batched.graphs) + len(engine.batched.prefix_graphs),
              "recording_log": lines.lines}
    print(json.dumps(record, indent=1))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(record, indent=1) + "\n")


if __name__ == "__main__":
    main()
