"""Latency and throughput of an INT4 engine against a bfloat16 engine on the fast backend, in one process, back to back
(plan 2b-2 Task 5; step 2b-2 design, Sections 3.2 and 8 item 5). The requirement: for every (B, N) below, the INT4
engine's p50 is at most the bfloat16 engine's plus BOUND_MS (0.05 ms). Since Task 5 each graph of an INT4 engine
uses the faster of cuBLAS (on the dequantised bfloat16 copy) and the INT4 kernel, timed while recording, so it should
never be slower than a bfloat16 engine apart from timing noise. runtime/python/tests/test_int4_latency_cuda.py runs
this as a test.

Method (the design's bench_int4.py, Section 3.2, on two real engines instead of two paths on one):
- Two LocalEngines on the fast backend, both alive for the whole run: the v1.3 bfloat16 base and its INT4 GPTQ folder
  (plan 2b-2 Tasks 1-3), each with the nine v1.3 adapters and a PREFIX_POOL_MB prefix pool (the 144 prefixes below
  take about 4.4 GB). The bfloat16 engine must hold no INT4 weights and record cuBLAS in every graph, untimed.
- Prefixes: 16 texts of 250 to 2,000 tokens from legal-clauses test states (seeded), cached under every adapter in
  each engine's own pool before timing.
- Rows: batch 1 is the nav adapter, the cached 1,000-token prefix, 32 options, N = 32 / 64 / 128 / 256 new tokens;
  batches of B = 4 / 16 / 64 rows cycle the nine adapters and the 16 prefixes, at the same N. Every pass runs in the
  (B, N) graph exactly.
- Timing: BatchedPath.decide wall clock (stage, copy in, replay, copy out, synchronise), as the design's benchmark.
  Per (B, N): WARM calls on each engine, then ROUNDS rounds, each R calls on the bfloat16 engine then R on the INT4
  engine. p50 and p99 are over all of an engine's calls; the GPU time (model_ms) p50 is reported too, and the median
  of the per-round differences in p50 (the figure least moved by other GPU users). Rows per second: B / p50.
- Recorded: each INT4 graph's chosen product kernel with both timings, the recording time of each engine, peak GPU
  memory, each engine's reserved GPU memory before its graphs' timing, after it and after handing the timing's memory
  back (BatchedPath.timing_reserved_gb), and the GPU's other processes and utilisation from nvidia-smi before and
  after (on datigator nvidia-smi is in /usr/lib/wsl/lib).

Usage (datigator; results/step2b2/task5/run_latency.sh runs it as the test, detached with nohup):
  .venv/bin/python scripts/bench_int4.py --conditions "..." --out results/step2b2/latency.json
Another base (v1.4): --base, --int4-base, --test-rows and --adapter NAME=FOLDER (repeated; the first is the batch-1
adapter) replace the v1.3 paths and adapters; all four are given together or not at all.
Writes --out, then exits with an error naming every (B, N) over the bound. A failed precondition (missing checkpoint,
a base of the wrong format, rows not filling one graph) raises RuntimeError before anything is written."""

from __future__ import annotations

import argparse
import glob
import json
import random
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
INT4_BASE = Path("~/jeff-2b-scratch/2b2/int4-base/ll-v12-final-int4-gptq").expanduser()
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
TEST_ROWS = Path("/mnt/d/jeff-models/v1.3/casdgx01/testsets/legal-clauses/test.jsonl")
ADAPTERS = ("nav", "triage", "tools", "spam", "guard", "ground", "emotion", "support-intents", "legal-clauses")
PREFIX_LENGTHS = (1000, 300, 500, 800, 1200, 1500, 2000, 400, 700, 900, 1100, 1300, 1700, 600, 250, 1900)
PREFIX_POOL_MB = 5120
OPTIONS = 32
SHAPES = tuple((b, n) for b in (1, 4, 16, 64) for n in (32, 64, 128, 256))
BOUND_MS = 0.05
WARM = {1: 100, 4: 20, 16: 20, 64: 10}
ROUNDS = 10
R = {1: 100, 4: 30, 16: 30, 64: 10}  # calls per round: 1,000 / 300 / 300 / 100 per engine, as the design's benchmark


def text_of(tok: Any, rows: list[dict[str, Any]], n: int) -> str:
    """A text of exactly n tokens from the test states (the design's bench_int4.text_of)."""
    text = "\n".join(str(r["state"]) for r in rows)
    while len(tok(text, add_special_tokens=False)["input_ids"]) < n:
        text = text + "\n" + text
    return tok.decode(tok(text, add_special_tokens=False)["input_ids"][:n])


def pct(samples: list[float]) -> dict[str, float]:
    cuts = statistics.quantiles(samples, n=100, method="inclusive")
    return {"p50": round(cuts[49], 4), "p99": round(cuts[98], 4)}


def gpu_state() -> dict[str, str]:
    """The GPU's processes and utilisation, as nvidia-smi reports them (it must be on PATH)."""
    def query(*args: str) -> str:
        return subprocess.run(["nvidia-smi", *args, "--format=csv,noheader"], check=True, capture_output=True,
                              text=True).stdout.strip()

    return {"utilization_and_memory": query("--query-gpu=utilization.gpu,memory.used,memory.total"),
            "processes": query("--query-compute-apps=pid,process_name,used_memory")}


def v13_adapters() -> dict[str, Path]:
    """The nine v1.3 adapters' checkpoint folders (nav first: the batch-1 adapter)."""
    adapters = {}
    for name in ADAPTERS:
        found = glob.glob(CHECKPOINTS.format(name=name))
        if len(found) != 1:
            raise RuntimeError(f"expected one v1.3 {name} checkpoint, found {found}")
        adapters[name] = Path(found[0])
    return adapters


def run(conditions: str, out: Path, *, base: Path = BASE, int4_base: Path = INT4_BASE,
        adapters: dict[str, Path] | None = None, test_rows: Path = TEST_ROWS) -> dict[str, Any]:
    """Measure (module docstring), write `out` (JSON) and return what was written. `adapters` maps each adapter's
    name to its folder, the first being the batch-1 adapter; None means the nine v1.3 adapters."""
    import torch

    from jeff_runtime import LocalEngine
    from jeff_runtime.engine.fast_path import Row
    from jeff_runtime.engine.product_choice import CUBLAS, PRODUCTS, ProductChoice

    before = gpu_state()
    if adapters is None:
        adapters = v13_adapters()
    names = tuple(adapters)
    engines = {}
    for label, folder in (("bf16", base), ("int4", int4_base)):
        engines[label] = LocalEngine(folder, adapters, backend="fast", prepare_workers=1, prefix_pool_mb=PREFIX_POOL_MB,
                                     max_adapters=len(adapters))
    bf16, int4 = engines["bf16"], engines["int4"]
    if bf16.fast.int4_bytes != 0 or bf16.batched.products != (CUBLAS,):
        raise RuntimeError(f"{base} is not a bfloat16 base on the fast backend (INT4 bytes {bf16.fast.int4_bytes}, "
                         f"product kernels {bf16.batched.products})")
    untimed = ProductChoice(CUBLAS, {})
    if any(plan.products != untimed for plan in bf16.batched.plans.values()) or \
            any(choice != untimed for choice in bf16.batched.prefix_plans.values()):
        raise RuntimeError("The bfloat16 engine recorded a product kernel other than untimed cuBLAS")
    if int4.fast.int4_bytes == 0 or int4.batched.products != PRODUCTS:
        raise RuntimeError(f"{int4_base} loaded without INT4 weights in the fast path (product kernels "
                         f"{int4.batched.products})")

    tok = bf16.fast.tokenizer
    src = random.Random(0).sample([json.loads(line) for line in test_rows.read_text().split("\n") if line.strip()], 400)
    prefixes = [text_of(tok, src[i * 20:(i + 1) * 20], n) for i, n in enumerate(PREFIX_LENGTHS)]
    new_ids = tok(text_of(tok, src[350:], 300), add_special_tokens=False)["input_ids"]

    def rows_for(engine: Any, b: int, n: int, slot: dict[tuple[int, str], int]) -> list[tuple[int, int, Any]]:
        if b == 1:
            key = (engine.fast.index(names[0]), prefixes[0])
            return [(key[0], slot[key], Row(prefixes[0], new_ids[:n], OPTIONS))]
        rows = []
        for i in range(b):
            key = (engine.fast.index(names[i % len(names)]), prefixes[(i * 7) % len(prefixes)])
            rows.append((key[0], slot[key], Row(key[1], new_ids[:n], OPTIONS)))
        return rows

    results: dict[str, Any] = {}
    with torch.inference_mode():
        slots = {}
        for label, engine in engines.items():
            keys = [(engine.fast.index(name), text) for name in names for text in prefixes]
            made, _ = engine.batched.ensure_prefixes(keys, 8192)  # pinned until the end: nothing evicts them
            slots[label] = dict(zip(keys, made, strict=True))
        for b, n in SHAPES:
            rows = {label: rows_for(engine, b, n, slots[label]) for label, engine in engines.items()}
            for label, engine in engines.items():
                for _ in range(WARM[b]):
                    engine.batched.decide(rows[label])
            wall: dict[str, list[float]] = {label: [] for label in engines}
            gpu: dict[str, list[float]] = {label: [] for label in engines}
            round_p50: dict[str, list[float]] = {label: [] for label in engines}
            for _ in range(ROUNDS):
                for label, engine in engines.items():
                    this_round = []
                    for _ in range(R[b]):
                        started = time.perf_counter()
                        _, _, timing = engine.batched.decide(rows[label])
                        this_round.append((time.perf_counter() - started) * 1000)
                        gpu[label].append(timing["model_ms"])
                        if timing["passes"] != 1:
                            raise RuntimeError(f"B={b} N={n}: {timing['passes']} passes; the rows must fill one graph")
                    wall[label] += this_round
                    round_p50[label].append(statistics.median(this_round))
            res: dict[str, Any] = {}
            for label in engines:
                res[label] = {**pct(wall[label]), "gpu_p50": round(statistics.median(gpu[label]), 4)}
                res[label]["rows_per_s"] = round(b / res[label]["p50"] * 1000, 1)
            plan = int4.batched.plans[(b, n)]
            res["int4_product_kernel"] = plan.products.kernel
            res["int4_product_ms"] = plan.products.ms
            res["int4_minus_bf16_p50_ms"] = round(res["int4"]["p50"] - res["bf16"]["p50"], 4)
            res["int4_minus_bf16_median_of_rounds_ms"] = round(statistics.median(
                [i - f for i, f in zip(round_p50["int4"], round_p50["bf16"], strict=True)]), 4)
            res["int4_over_bf16"] = round(res["int4"]["p50"] / res["bf16"]["p50"], 3)
            res["within_bound"] = res["int4"]["p50"] <= res["bf16"]["p50"] + BOUND_MS
            results[f"{b}x{n}"] = res
            print(b, n, json.dumps(res), flush=True)
        for label, engine in engines.items():
            engine.fast.prefixes.unpin(list(slots[label].values()))  # one pin per key (ensure_prefixes)

    record = {
        "script": "scripts/bench_int4.py", "date": time.strftime("%Y-%m-%d %H:%M"), "conditions": conditions,
        "gpu": torch.cuda.get_device_name(), "gpu_state_before": before, "gpu_state_after": gpu_state(),
        "bases": {"bf16": str(base), "int4": str(int4_base)}, "format_id": int4.base_format["format_id"],
        "adapters": {name: str(folder) for name, folder in adapters.items()}, "test_rows": str(test_rows), "prefix_pool_mb": PREFIX_POOL_MB, "bound_ms": BOUND_MS, "warm": WARM,
        "rounds": ROUNDS, "calls_per_round": R,
        "record_seconds": {label: round(engine.batched.record_seconds, 1) for label, engine in engines.items()},
        "peak_allocated_gb": round(torch.cuda.max_memory_allocated() / 2**30, 2),
        "timing_reserved_gb": {label: engine.batched.timing_reserved_gb for label, engine in engines.items()},
        "int4_product_kernels": {
            "decision_graphs": {f"{b}x{n}": {"kernel": p.products.kernel, "ms": p.products.ms}
                                for (b, n), p in int4.batched.plans.items()},
            "prefix_graphs": {f"{b}x{nb}": {"kernel": c.kernel, "ms": c.ms}
                              for (b, nb), c in int4.batched.prefix_plans.items()}},
        "results": results,
        "over_bound": [shape for shape, res in results.items() if not res["within_bound"]]}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1) + "\n")
    return record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--conditions", required=True, help="what else ran on the GPU (recorded in the output)")
    parser.add_argument("--out", type=Path, required=True, help="the JSON to write (results/step2b2/latency.json)")
    parser.add_argument("--base", type=Path, help="the bfloat16 base folder (default: the v1.3 base)")
    parser.add_argument("--int4-base", type=Path, help="its INT4 folder (default: the v1.3 INT4 folder)")
    parser.add_argument("--test-rows", type=Path, help="the JSONL whose states make the prefixes (default: v1.3 "
                        "legal-clauses test rows)")
    parser.add_argument("--adapter", action="append", default=[], metavar="NAME=FOLDER",
                        help="an adapter to load (repeatable; the first is the batch-1 adapter; default: the nine "
                             "v1.3 adapters)")
    args = parser.parse_args()
    given = [args.base is not None, args.int4_base is not None, args.test_rows is not None, bool(args.adapter)]
    if any(given) and not all(given):
        parser.error("--base, --int4-base, --test-rows and --adapter are given together or not at all")
    if all(given):
        adapters = {}
        for pair in args.adapter:
            name, sep, folder = pair.partition("=")
            if not sep or not name or not folder:
                parser.error(f"--adapter takes NAME=FOLDER; got {pair!r}")
            if name in adapters:
                parser.error(f"--adapter names {name} twice")
            adapters[name] = Path(folder).expanduser()
        record = run(args.conditions, args.out, base=args.base.expanduser(), int4_base=args.int4_base.expanduser(),
                     adapters=adapters, test_rows=args.test_rows.expanduser())
    else:
        record = run(args.conditions, args.out)
    print(json.dumps(record), flush=True)
    if record["over_bound"]:
        raise SystemExit(f"INT4 p50 over the bfloat16 p50 + {BOUND_MS} ms at (B x N) {record['over_bound']}; see "
                         f"{args.out}")


if __name__ == "__main__":
    main()
