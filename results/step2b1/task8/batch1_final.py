"""The final batch-1 comparison before the single-row graphs are removed (plan 2b-1 Task 8, Ruling B): the batched
path at B = 1 against today's FastPath.decide, in one process, call after call alternating (either first, in turn),
1,000 timed runs each after 100 warm-up runs, a cached 1,000-token prefix, the nav adapter, 32 options, at 32, 64, 128
and 256 new tokens. It is test_batched_path_cuda.test_batch_1_is_no_slower_than_todays_fast_path run on the engine's
own BatchedPath (engine.batched, as served), writing to results/step2b1/batch1_final.json instead of batch1.json.

Run on datigator at ae101bd: .venv/bin/python results/step2b1/task8/batch1_final.py"""

from __future__ import annotations

import glob
import importlib.util
import json
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

REPO = Path(__file__).resolve().parents[3]
OUT = REPO / "results/step2b1/batch1_final.json"


def main() -> None:
    import torch

    from jeff_runtime import LocalEngine

    spec = importlib.util.spec_from_file_location("bp", REPO / "runtime/python/tests/test_batched_path_cuda.py")
    assert spec is not None and spec.loader is not None
    bp = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bp)
    folders = {name: Path(glob.glob(bp.CHECKPOINTS.format(name=name))[0]) for name in bp.ADAPTERS}
    started = time.perf_counter()
    engine = LocalEngine(bp.BASE, folders, backend="fast", prepare_workers=1, prefix_pool_mb=6144,
                         max_adapters=len(bp.ADAPTERS))
    torch.cuda.synchronize()
    recording = {"engine_start_seconds": round(time.perf_counter() - started, 1),
                 "batched_record_seconds": round(engine.batched.record_seconds, 1),
                 "batched_graphs": len(engine.batched.graphs), "fast_path_graphs": len(engine.fast.graphs),
                 "gpu_memory_after_start_gb": {"allocated": round(torch.cuda.memory_allocated() / 2**30, 2),
                                               "reserved": round(torch.cuda.memory_reserved() / 2**30, 2)}}
    bp.BATCH1_RESULTS = OUT
    gpu = SimpleNamespace(engine=engine, fast=engine.fast, batched=engine.batched, recording=recording, torch=torch)
    failed = None
    try:
        bp.test_batch_1_is_no_slower_than_todays_fast_path(gpu)
    except AssertionError as error:
        failed = str(error)
    record = json.loads(OUT.read_text())
    record["script"] = "results/step2b1/task8/batch1_final.py (the test's body on engine.batched)"
    record["commit"] = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short", "HEAD"], capture_output=True,
                                      text=True).stdout.strip() or "synced tree (no .git on datigator): ae101bd"
    OUT.write_text(json.dumps(record, indent=1) + "\n")
    print(json.dumps(record["batch1"], indent=1))
    if failed:
        raise SystemExit(f"batch-1 check failed: {failed}")


if __name__ == "__main__":
    main()
