"""Throughput of the batched fast path through the real engine (plan 2b-1 Task 8; the table of design Section 7).

One LocalEngine on the fast backend with the nine v1.3 adapters of datigator, called as the engine server calls it for
one micro-batch: LocalEngine.predict_many with one group per adapter, so each call makes sure of every row's prefix in
the pool, pins them, runs the passes and unpins them. For each pass size B in --batches and each new-token count N in
--new, every call holds B rows of exactly N new tokens (one pass of the (B, N) graph):
- row j of call k uses adapter m mod 9 and prefix m mod 16 of 16 real prefixes (test-set prompts' fixed parts of 250
  to 2,000 tokens, taken in turn from the adapters' test sets), m = j + k, so every pass of more than one row mixes
  adapters, a pass of 64 rows has 64 distinct (adapter, prefix) pairs, and the calls cycle through all 144 pairs,
  which the warm-up caches, as in serving after the first requests;
- each row's new tokens are its real row's own, repeated and cut to exactly N, so every row of the call lands in one
  bucket (the comparison is between pass sizes, not texts).
The rows are prepared before timing (in serving, preparation runs on arrival, on the preparation threads, while the
GPU works). Per (B, N): wall time per call (p50, p99, mean), the engine's model_ms, and rows per second from the mean.
B = 1 is the batch-1 row-at-a-time rate, the closest to the single-row path's (removed in Task 8; the design's
"today" column is quoted from Section 7 for reference, not measured here).

The other Jeff servers on datigator stay resident on the GPU and may serve while this runs: nvidia-smi's view of the
GPU (memory used before the engine starts, which is theirs; utilisation and memory sampled during each (B, N)) is
recorded with the results.

Run on datigator:
  .venv/bin/python scripts/bench_batched.py --prefix-pool-mb 8192 --out results/step2b1/throughput.json"""

from __future__ import annotations

import argparse
import dataclasses
import glob
import importlib.metadata
import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any, cast

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
TEST_ROWS = "/mnt/d/jeff-models/v1.3/casdgx01/testsets/{name}/test.jsonl"
ADAPTERS = ("triage", "nav", "spam", "emotion", "ground", "guard", "legal-clauses", "support-intents", "tools")
PREFIXES = 16
PREFIX_TOKENS = (250, 2000)
NVIDIA_SMI = "/usr/lib/wsl/lib/nvidia-smi"
# Design Section 7's "Today" column (the single-row loop before 2b-1, rows/s), quoted for reference.
DESIGN_TODAY_ROWS_PER_S = {32: 294, 64: 242, 128: 249}


def pct(samples: list[float]) -> dict[str, float]:
    cuts = statistics.quantiles(samples, n=100, method="inclusive")
    return {"p50": round(cuts[49], 3), "p99": round(cuts[98], 3), "mean": round(statistics.fmean(samples), 3)}


class GpuWatch:
    """nvidia-smi sampling the whole GPU every 200 ms between start() and stop()."""

    def __init__(self) -> None:
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen([NVIDIA_SMI, "--query-gpu=utilization.gpu,memory.used",
                                      "--format=csv,noheader,nounits", "-lms", "200"], stdout=subprocess.PIPE, text=True)

    def stop(self) -> dict[str, Any]:
        if self.proc is None:
            raise RuntimeError("GpuWatch.stop() before start()")
        self.proc.terminate()
        out = self.proc.communicate()[0]
        self.proc = None
        rows = [[float(v) for v in line.split(",")] for line in out.splitlines() if line.strip()]
        if not rows:
            raise RuntimeError("nvidia-smi gave no samples")
        return {"samples": len(rows), "utilization_pct_mean": round(statistics.fmean(r[0] for r in rows), 1),
                "memory_used_mib_max": max(r[1] for r in rows)}


def gpu_now() -> dict[str, Any]:
    out = subprocess.run([NVIDIA_SMI, "--query-gpu=memory.used,memory.total,utilization.gpu",
                          "--format=csv,noheader,nounits"], capture_output=True, text=True, check=True).stdout
    used, total, util = (float(v) for v in out.strip().split(","))
    apps = subprocess.run([NVIDIA_SMI, "--query-compute-apps=pid", "--format=csv,noheader"], capture_output=True,
                          text=True, check=True).stdout.split()
    return {"memory_used_mib": used, "memory_total_mib": total, "utilization_pct": util, "compute_processes": len(apps)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--batches", default="1,4,16,64")
    parser.add_argument("--new", default="32,64,128")
    parser.add_argument("--warmup", type=int, default=150)
    parser.add_argument("--reps", type=int, default=300)
    parser.add_argument("--prefix-pool-mb", type=int, required=True, help="the engine's prefix pool (8192 as deployed)")
    args = parser.parse_args()
    batches = [int(b) for b in args.batches.split(",")]
    news = [int(n) for n in args.new.split(",")]

    import torch

    from jeff.engine import EngineResult, Prepared
    from jeff_runtime import LocalEngine
    from jeff_runtime.engine.prepare import Row

    before_engine = gpu_now()  # the other servers' share of the GPU
    adapters = {name: Path(glob.glob(CHECKPOINTS.format(name=name))[0]) for name in ADAPTERS}
    started = time.perf_counter()
    engine = LocalEngine(BASE, adapters, backend="fast", prepare_workers=1, prefix_pool_mb=args.prefix_pool_mb,
                         max_adapters=len(adapters))
    engine_start_s = round(time.perf_counter() - started, 1)
    tok = engine.fast.tokenizer

    # 16 real prefixes of 250 to 2,000 tokens, taken in turn from the adapters' test sets that have such prefixes, and
    # every adapter's own prepared rows for their new tokens
    rows: dict[str, list[tuple[Prepared, Row]]] = {name: [] for name in ADAPTERS}
    candidates: dict[str, list[str]] = {name: [] for name in ADAPTERS}
    for name in ADAPTERS:
        for line in Path(TEST_ROWS.format(name=name)).read_text().splitlines()[:400]:
            item = json.loads(line)
            prepared = engine.prepare(name, [{"state": item["state"], "question": item["question"]}])
            if prepared.route != "fast" or prepared.items is None:
                continue
            row = prepared.items[0]
            if len(rows[name]) < 8:
                rows[name].append((prepared, row))
            p = len(tok(row.prefix_text, add_special_tokens=False)["input_ids"])
            if PREFIX_TOKENS[0] <= p <= PREFIX_TOKENS[1] and row.prefix_text not in candidates[name]:
                candidates[name].append(row.prefix_text)
    prefixes: list[str] = []
    for turn in range(max(len(found) for found in candidates.values())):
        for name in ADAPTERS:
            if turn < len(candidates[name]) and len(prefixes) < PREFIXES:
                prefixes.append(candidates[name][turn])
    if len(prefixes) < PREFIXES:
        raise SystemExit(f"only {len(prefixes)} prefixes of {PREFIX_TOKENS[0]} to {PREFIX_TOKENS[1]} tokens found")
    prefix_tokens = sorted(len(tok(p, add_special_tokens=False)["input_ids"]) for p in prefixes)

    def call_groups(b: int, n: int, k: int) -> list[tuple[str, Prepared]]:
        """Call k of the (b, n) series: one group per adapter, rows in pass order."""
        groups: dict[str, list[Row]] = {}
        sources: dict[str, Prepared] = {}
        for j in range(b):
            m = j + k
            name = ADAPTERS[m % len(ADAPTERS)]
            prepared, row = rows[name][(m // len(ADAPTERS)) % len(rows[name])]
            groups.setdefault(name, []).append(Row(prefixes[m % PREFIXES], (row.new_ids * n)[:n], row.count))
            sources[name] = prepared
        return [(name, dataclasses.replace(sources[name], rows=sources[name].rows[:1] * len(items), items=items))
                for name, items in groups.items()]

    results: dict[str, Any] = {}
    watch = GpuWatch()
    with torch.inference_mode():
        for n in news:
            for b in batches:
                cycle = len(ADAPTERS) * PREFIXES  # call k and call k + 144 hold the same rows
                calls = [call_groups(b, n, k) for k in range(cycle)]
                watch.start()
                wall: list[float] = []
                model: list[float] = []
                for i in range(args.warmup + args.reps):
                    groups = calls[i % cycle]
                    t = time.perf_counter()
                    out = engine.predict_many(groups)
                    elapsed = (time.perf_counter() - t) * 1000
                    for (name, _), result in zip(groups, out, strict=True):
                        if not isinstance(result, EngineResult):
                            raise SystemExit(f"B={b} N={n}, call {i}: the engine refused the {name} group: "
                                             f"{type(result).__name__}: {result}")
                    timing = cast(EngineResult, out[0]).timing
                    if i >= args.warmup:
                        if timing["passes"] != 1 or timing["prefix_misses"] != 0:
                            raise SystemExit(f"B={b} N={n}: expected one pass on cached prefixes, got {timing}")
                        wall.append(elapsed)
                        model.append(float(timing["model_ms"]))
                gpu = watch.stop()
                got = {"wall_ms": pct(wall), "model_ms": pct(model),
                       "rows_per_s": round(b / (statistics.fmean(wall) / 1000), 1),
                       "adapters_per_pass": min(b, len(ADAPTERS)), "gpu_during": gpu}
                results[f"B={b} N={n}"] = got
                print(f"B={b:>2} N={n:>3}: {got['wall_ms']} ms per call, {got['rows_per_s']} rows/s, GPU {gpu}",
                      flush=True)
    table = {str(n): {str(b): results[f"B={b} N={n}"]["rows_per_s"] for b in batches} for n in news}
    record = {
        "script": "scripts/bench_batched.py",
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "versions": {"torch": torch.__version__, "cuda": str(torch.version.cuda), "gpu": torch.cuda.get_device_name(),
                     **{d: importlib.metadata.version(d) for d in ("triton", "flash-linear-attention")}},
        "setup": {"adapters_loaded": len(ADAPTERS), "prefixes": PREFIXES, "prefix_tokens": prefix_tokens,
                  "prefix_pool_mb": args.prefix_pool_mb, "warmup": args.warmup, "reps": args.reps,
                  "call": "LocalEngine.predict_many, one group per adapter: prefixes looked up and pinned, one "
                          "batched pass, unpinned; rows prepared before timing",
                  "engine_start_s": engine_start_s},
        "other_servers": {"gpu_before_this_engine": before_engine,
                          "note": "the other Jeff servers on datigator (jeff-server, jeff-public, fastserve, "
                                  "tdd-preview, legal-eval) stay resident and may serve during the run; "
                                  "gpu_during per (B, N) samples the whole GPU"},
        "rows_per_s": table,
        "design_today_rows_per_s": DESIGN_TODAY_ROWS_PER_S,
        "results": results,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=1) + "\n")
    print(json.dumps(table, indent=1))


if __name__ == "__main__":
    main()
