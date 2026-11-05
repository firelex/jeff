"""Batch 1 through the real engine: a lone request's end-to-end time through LocalEngine.predict, which prepares the
request (prepare: checked, split and tokenised) and runs it on the batched path (predict_many: the prefix looked up and
pinned, BatchedPath.decide in its B = 1 graph, unpinned).

Plan 2b-1 Task 7 ran this against what LocalEngine.predict did before Task 7 (FastPath.decide, the single-row graphs),
call after call in one process (results/step2b1/task7_engine_batch1.json); Task 8 removed that path, so the script now
measures the engine alone (results/step2b1/task8/engine_batch1.json). Compare any change in one process against the
code before it, never against these stored numbers.

Rows: real test rows of the v1.3 adapters on datigator, one per new-token bucket 32, 64, 128 and 256 (the row with the
most new tokens in that bucket), each with its own adapter and its cached prefix (warm-up runs cache it). Reports p50,
p99 and mean of the wall time of the whole call and of model_ms (the decision only, preparation being done before, on
arrival).

Run on datigator:
  .venv/bin/python scripts/bench_engine_batch1.py --out results/step2b1/task8/engine_batch1.json --warmup 100 --reps 1000"""

from __future__ import annotations

import argparse
import glob
import importlib.metadata
import json
import statistics
import time
from pathlib import Path
from typing import Any

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
TEST_ROWS = "/mnt/d/jeff-models/v1.3/casdgx01/testsets/{name}/test.jsonl"
ADAPTERS = ("triage", "nav", "spam", "emotion", "ground", "guard", "legal-clauses", "support-intents", "tools")
BUCKETS = (32, 64, 128, 256)


def pct(samples: list[float]) -> dict[str, float]:
    cuts = statistics.quantiles(samples, n=100, method="inclusive")
    return {"p50": round(cuts[49], 4), "p99": round(cuts[98], 4), "mean": round(statistics.fmean(samples), 4)}


def pick_rows(engine: Any) -> dict[int, tuple[str, dict[str, Any], int]]:
    """For each bucket, (adapter, row, new tokens): the fast-path row with the most new tokens in that bucket."""
    best: dict[int, tuple[str, dict[str, Any], int]] = {}
    for name in ADAPTERS:
        for line in Path(TEST_ROWS.format(name=name)).read_text().splitlines()[:300]:
            item = json.loads(line)
            row = {"state": item["state"], "question": item["question"]}
            prepared = engine.prepare(name, [row])
            if prepared.route != "fast":
                continue
            n = len(prepared.items[0].new_ids)
            bucket = next((b for b in BUCKETS if b >= n), None)
            if bucket is not None and (bucket not in best or n > best[bucket][2]):
                best[bucket] = (name, row, n)
    missing = [b for b in BUCKETS if b not in best]
    if missing:
        raise SystemExit(f"no test row in new-token buckets {missing}")
    return best


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--warmup", type=int, required=True)
    parser.add_argument("--reps", type=int, required=True)
    args = parser.parse_args()

    import torch

    from jeff_runtime import LocalEngine

    adapters = {name: Path(glob.glob(CHECKPOINTS.format(name=name))[0]) for name in ADAPTERS}
    engine = LocalEngine(BASE, adapters, backend="fast", prepare_workers=1, prefix_pool_mb=2048,
                         max_adapters=len(adapters))
    rows = pick_rows(engine)

    results: dict[str, Any] = {}
    with torch.inference_mode():
        for bucket, (name, row, n) in sorted(rows.items()):
            wall: list[float] = []
            model: list[float] = []
            for i in range(args.warmup + args.reps):
                started = time.perf_counter()
                result = engine.predict(name, [row])
                elapsed = (time.perf_counter() - started) * 1000
                assert result.timing["path"] == "fast", result.timing
                assert result.row_timing is not None and result.row_timing["pass_rows"] == [1], result.row_timing
                if i >= args.warmup:
                    wall.append(elapsed)
                    model.append(float(result.timing["model_ms"]))
            results[str(bucket)] = {"adapter": name, "new_tokens": n, "wall_ms": pct(wall), "model_ms": pct(model)}
            print(f"bucket {bucket} ({name}, {n} new tokens): {results[str(bucket)]}")
    record = {
        "script": "scripts/bench_engine_batch1.py",
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "versions": {"torch": torch.__version__, "cuda": str(torch.version.cuda), "gpu": torch.cuda.get_device_name(),
                     **{d: importlib.metadata.version(d) for d in ("triton", "flash-linear-attention")}},
        "setup": {"adapters_loaded": len(ADAPTERS), "warmup": args.warmup, "reps": args.reps,
                  "call": "LocalEngine.predict: prepare (check, split, tokenise), then predict_many on the batched "
                          "path (model_ms: the decision only)",
                  "other_servers": "the other Jeff servers on datigator stay resident on the GPU"},
        "batch1": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=1) + "\n")


if __name__ == "__main__":
    main()
