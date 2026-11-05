"""Task 6 throughput: the design's agreement rows (design Section 6: the nine v1.3 adapters' test sets, 200 rows each
drawn with seed 7, every row with its own prefix) end to end, with their prefixes made by batched prefix passes
(BatchedPath.ensure_prefixes) against one at a time (FastPath.prefix), decisions in both cases by BatchedPath.decide.

As served: rows shuffled across all adapters (seed 0), in micro-batches of 64 rows (--max-batch-rows 64); per
micro-batch, every row's prefix made (or found) and pinned, one decide() call, unpinned. The pool is emptied before
each run, so every prefix is new. Runs alternate: one at a time, batched, one at a time, batched.

Run on datigator from ~/jeff-dev-next (results/ is not synced: copy this file there by hand):
    .venv/bin/python results/step2b1/task6/bench_prefix_batching.py
Writes results/step2b1/prefix_batching.json."""

from __future__ import annotations

import glob
import json
import random
import time
from pathlib import Path
from typing import Any

import torch

from jeff_runtime import LocalEngine
from jeff_runtime.engine.batched_path import PREFIX_PASS_ROWS, BatchedPath

REPO = Path(__file__).resolve().parents[3]
BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
TEST_ROWS = Path("/mnt/d/jeff-models/v1.3/casdgx01/testsets")
ADAPTERS = ("emotion", "ground", "guard", "legal-clauses", "nav", "spam", "support-intents", "tools", "triage")
PER_ADAPTER, MICRO_BATCH = 200, 64


def test_rows(name: str) -> list[dict[str, Any]]:
    """As the design's p2_agreement.py (common.test_rows): 200 rows drawn with seed 7."""
    lines = (TEST_ROWS / name / "test.jsonl").read_text().splitlines()
    picked = random.Random(7).sample(lines, min(PER_ADAPTER, len(lines)))
    return [{"state": item["state"], "question": item["question"]} for item in map(json.loads, picked)]


def main() -> None:
    adapters = {}
    for name in ADAPTERS:
        found = glob.glob(CHECKPOINTS.format(name=name))
        assert len(found) == 1, (name, found)
        adapters[name] = Path(found[0])
    engine = LocalEngine(BASE, adapters, backend="fast", prefix_pool_mb=6144, max_adapters=len(ADAPTERS))
    fast = engine.fast
    pool = fast.prefixes
    torch.cuda.synchronize()
    before = torch.cuda.memory_reserved() / 2**30
    started = time.perf_counter()
    batched = BatchedPath(fast)
    record_s = time.perf_counter() - started
    added = torch.cuda.memory_reserved() / 2**30 - before
    items, raw, skipped = [], [], {}
    for name in ADAPTERS:
        for row in test_rows(name):
            prepared = fast.prepare(name, [row])
            if isinstance(prepared, str):
                skipped[name] = skipped.get(name, 0) + 1
                continue
            items.append((fast.index(name), prepared[0]))
            raw.append((name, row))
    order = list(range(len(items)))
    random.Random(0).shuffle(order)
    items, raw = [items[i] for i in order], [raw[i] for i in order]
    tok = fast.tokenizer
    lengths = [len(tok(row.prefix_text, add_special_tokens=False)["input_ids"]) for _, row in items]
    distinct = len({(index, row.prefix_text) for index, row in items})
    print(f"{len(items)} rows ({distinct} distinct prefixes), skipped {skipped}", flush=True)
    chunks = [items[i:i + MICRO_BATCH] for i in range(0, len(items), MICRO_BATCH)]

    def empty() -> None:
        for index in list(fast.adapter_index.values()):
            fast.forget(index)

    def run(mode: str) -> tuple[dict[str, Any], list[list[float]]]:
        empty()
        torch.cuda.synchronize()
        out: list[list[float]] = []
        totals = {"prefix_ms": 0.0, "decide_ms": 0.0, "prefix_misses": 0, "prefix_passes": 0, "decide_passes": 0}
        t0 = time.perf_counter()
        for chunk in chunks:
            keys = [(index, row.prefix_text) for index, row in chunk]
            if mode == "one_at_a_time":
                timing = {"model_ms": 0.0, "prefix_ms": 0.0, "prefix_hits": 0, "prefix_misses": 0, "new_tokens": 0}
                slots = []
                try:
                    for index, text in keys:
                        slot = fast.prefix(index, text, engine.max_tokens, timing)
                        pool.pin([slot])
                        slots.append(slot)
                    probabilities, _, decided = batched.decide([(i, s, r) for (i, r), s in zip(chunk, slots)])
                finally:
                    pool.unpin(slots)
                totals["prefix_passes"] += timing["prefix_misses"]
            else:
                slots, timing = batched.ensure_prefixes(keys, engine.max_tokens)
                try:
                    probabilities, _, decided = batched.decide([(i, s, r) for (i, r), s in zip(chunk, slots)])
                finally:
                    pool.unpin(slots)
                totals["prefix_passes"] += timing["prefix_passes"]
            totals["prefix_ms"] += timing["prefix_ms"]
            totals["prefix_misses"] += timing["prefix_misses"]
            totals["decide_ms"] += decided["model_ms"]
            totals["decide_passes"] += decided["passes"]
            out.extend(probabilities)
        torch.cuda.synchronize()
        wall = time.perf_counter() - t0
        result = {"wall_s": round(wall, 3), "rows_per_s": round(len(items) / wall, 1),
                  "prefix_gpu_s": round(totals["prefix_ms"] / 1000, 3),
                  "decide_gpu_s": round(totals["decide_ms"] / 1000, 3),
                  "prefix_misses": totals["prefix_misses"], "prefix_passes": totals["prefix_passes"],
                  "decide_passes": totals["decide_passes"]}
        print(mode, result, flush=True)
        return result, out

    runs: dict[str, list[dict[str, Any]]] = {"one_at_a_time": [], "batched": []}
    answers = {}
    for mode in ("one_at_a_time", "batched", "one_at_a_time", "batched"):
        result, answers[mode] = run(mode)
        runs[mode].append(result)
    a, b = answers["one_at_a_time"], answers["batched"]

    def top(p: list[float]) -> int:
        return max(range(len(p)), key=p.__getitem__)

    def top2(p: list[float]) -> list[list[float]]:
        ranked = sorted(range(len(p)), key=p.__getitem__, reverse=True)[:2]
        return [[i, round(p[i], 4)] for i in ranked]

    same_top = sum(top(x) == top(y) for x, y in zip(a, b))
    differences = sorted(max(abs(u - v) for u, v in zip(x, y)) for x, y in zip(a, b))
    worst = differences[-1]
    flipped = []
    for i, (x, y) in enumerate(zip(a, b)):
        if top(x) != top(y):  # what the standard path (the reference) answers for these rows
            name, row = raw[i]
            engine.adapter_set.use(name)
            standard = engine._standard([row], engine._layout(name))[0][0]
            matched = [side for side, p in (("one_at_a_time", x), ("batched", y)) if top(p) == top(standard)]
            flipped.append({"adapter": name, "one_at_a_time_top2": top2(x), "batched_top2": top2(y),
                            "standard_top2": top2(standard), "matched_standard_path": matched})
    best = {mode: min(r["wall_s"] for r in rs) for mode, rs in runs.items()}
    record = {
        "script": "results/step2b1/task6/bench_prefix_batching.py",
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "versions": {"torch": torch.__version__, "cuda": str(torch.version.cuda), "gpu": torch.cuda.get_device_name()},
        "setup": {"adapters": list(ADAPTERS), "rows": len(items), "distinct_prefixes": distinct,
                  "skipped_to_standard_path": skipped, "micro_batch_rows": MICRO_BATCH,
                  "prefix_tokens": {"min": min(lengths), "median": sorted(lengths)[len(lengths) // 2],
                                    "max": max(lengths)},
                  "prefix_pass_rows": {str(k): v for k, v in PREFIX_PASS_ROWS.items()},
                  "batched_path_record_s": round(record_s, 1), "batched_path_reserved_gb_added": round(added, 2),
                  "note": "other Jeff servers resident on the GPU during the runs (light load)"},
        "runs": runs,
        "best_wall_s": best,
        "speedup_end_to_end": round(best["one_at_a_time"] / best["batched"], 2),
        "speedup_prefix_gpu": round(min(r["prefix_gpu_s"] for r in runs["one_at_a_time"])
                                    / min(r["prefix_gpu_s"] for r in runs["batched"]), 2),
        "decisions": {"compared": "the same rows in the same decision passes; only how their prefixes were made differs",
                      "same_top_option": same_top, "rows": len(items),
                      "largest_probability_difference": round(worst, 4),
                      "probability_difference_p50_p99": [round(differences[len(differences) // 2], 4),
                                                         round(differences[int(len(differences) * 0.99)], 4)],
                      "rows_with_another_top_option": flipped},
        "design_reference": "design Section 6: 27 s for these rows in the prototype, almost all prefix passes",
    }
    path = REPO / "results/step2b1/prefix_batching.json"
    path.write_text(json.dumps(record, indent=1) + "\n")
    print(json.dumps(record, indent=1))


if __name__ == "__main__":
    with torch.inference_mode():
        main()
