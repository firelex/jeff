"""Batch-1 latency of the CUDA fast path: the batched path's B = 1 graphs (jeff_runtime.engine.batched_path.BatchedPath,
the only graphs the fast path has since plan 2b-1 Task 8), measured as the design's prototype 0 measures it
(the design study's p0_baseline.py, part 1). Before Task 8 this script timed the single-row
FastPath.decide; its results under results/step2b1/gap/ are of that path.

One question after a cached prefix of --prefix-tokens tokens, at each of --new new-token counts, with one adapter
and 32 answer options. Each count over --reps runs after --warmup untimed runs:
- decide_ms: wall clock of one row through the batched path as the engine runs it (the prefix looked up and pinned,
  BatchedPath.decide, unpinned), Python included;
- replay_ms: wall clock of the bare decision (the packed inputs copied in, one CUDA graph replay, probabilities copied
  out, synchronise), as jeff-asic's benchmark/gpu_int4_floor.py measures it;
- gpu_ms: CUDA events around the graph replay alone.

While timing, nvidia-smi samples the whole GPU every 100 ms (utilisation, SM clock, board power, performance state),
and it samples it for --idle-seconds before the engine loads, so the load from other processes on the GPU is on
record. The output also records the torch, Triton and flash-linear-attention versions.

The text is real: the "state" fields of the rows in --text (a test set, JSON lines), repeated until long enough.

Usage (on the CUDA machine):
  .venv/bin/python scripts/bench_fast_path.py --base DIR --adapter nav=DIR [--adapter NAME=DIR ...] --use nav \\
      --text TEST.jsonl --prefix-pool-mb 2048 --output results/step2b1/gap/fast_path.json"""

from __future__ import annotations

import argparse
import importlib.metadata
import json
import statistics
import subprocess
import time
from pathlib import Path
from typing import Any

import torch

NVIDIA_SMI = "/usr/lib/wsl/lib/nvidia-smi"
SMI_FIELDS = "utilization.gpu,clocks.sm,power.draw,pstate,memory.used"


def pct(samples: list[float]) -> dict[str, float]:
    cuts = statistics.quantiles(samples, n=100, method="inclusive")
    return {"p50": round(cuts[49], 4), "p99": round(cuts[98], 4), "mean": round(statistics.fmean(samples), 4)}


class GpuWatch:
    """nvidia-smi sampling the whole GPU every 100 ms between start() and stop()."""

    def __init__(self, smi: str) -> None:
        self.smi = smi
        self.proc: subprocess.Popen[str] | None = None

    def start(self) -> None:
        self.proc = subprocess.Popen([self.smi, f"--query-gpu={SMI_FIELDS}", "--format=csv,noheader,nounits", "-lms", "100"],
                                     stdout=subprocess.PIPE, text=True)

    def stop(self) -> dict[str, Any]:
        if self.proc is None:
            raise RuntimeError("GpuWatch.stop() before start()")
        self.proc.terminate()
        out = self.proc.communicate()[0]
        self.proc = None
        rows = [[v.strip() for v in line.split(",")] for line in out.splitlines() if line.strip()]
        if len(rows) < 3:
            raise RuntimeError(f"nvidia-smi gave only {len(rows)} samples")
        util = [float(r[0]) for r in rows]
        clock = [float(r[1]) for r in rows]
        power = [float(r[2]) for r in rows]
        return {"samples": len(rows), "utilization_pct": {"mean": round(statistics.fmean(util), 1), "max": max(util)},
                "sm_clock_mhz": {"median": statistics.median(clock), "min": min(clock), "max": max(clock)},
                "power_w": {"mean": round(statistics.fmean(power), 1), "max": max(power)},
                "pstates": sorted({r[3] for r in rows}), "memory_used_mib_max": max(float(r[4]) for r in rows)}


def versions() -> dict[str, str]:
    out = {"torch": torch.__version__, "cuda": str(torch.version.cuda), "gpu": torch.cuda.get_device_name()}
    for dist in ("triton", "flash-linear-attention", "fla-core", "transformers", "vllm"):
        try:
            out[dist] = importlib.metadata.version(dist)
        except importlib.metadata.PackageNotFoundError:
            out[dist] = "not installed"
    return out


def long_text(tok: Any, states: list[str], n: int) -> str:
    text = "\n".join(states)
    while len(tok(text, add_special_tokens=False)["input_ids"]) < n:
        text = text + "\n" + text
    ids = tok(text, add_special_tokens=False)["input_ids"]
    return str(tok.decode(ids[:n]))


def timed(fn: Any, warmup: int, reps: int) -> list[float]:
    for _ in range(warmup):
        fn()
    samples = []
    for _ in range(reps):
        t = time.perf_counter()
        fn()
        samples.append((time.perf_counter() - t) * 1000)
    return samples


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--adapter", action="append", required=True, help="NAME=DIR, once per adapter to load")
    p.add_argument("--use", required=True, help="the adapter active while timing")
    p.add_argument("--text", type=Path, required=True, help="test rows (JSON lines with a 'state' field) for real text")
    p.add_argument("--prefix-tokens", type=int, default=1000)
    p.add_argument("--prefix-pool-mb", type=int, required=True, help="the engine's prefix pool (e.g. 2048)")
    p.add_argument("--new", default="32,64,128,256")
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--reps", type=int, default=1000)
    p.add_argument("--idle-seconds", type=float, default=5.0, help="nvidia-smi samples before the engine loads")
    p.add_argument("--smi", default=NVIDIA_SMI)
    p.add_argument("--label", default="", help="free text stored in the output (what this run changes)")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(f"Refusing to overwrite {a.output}")
    adapters = {}
    for item in a.adapter:
        name, sep, folder = item.partition("=")
        if not sep:
            raise ValueError(f"--adapter {item!r}: use NAME=DIR")
        adapters[name] = Path(folder)
    if a.use not in adapters:
        raise ValueError(f"--use {a.use!r} is not one of the loaded adapters {sorted(adapters)}")

    from jeff_runtime.engine.batched_path import META
    from jeff_runtime.engine.fast_path import Row
    from jeff_runtime.local_engine import LocalEngine

    watch = GpuWatch(a.smi)
    out: dict[str, Any] = {"label": a.label, "versions": versions(), "adapters_loaded": len(adapters), "use": a.use,
                           "warmup": a.warmup, "reps": a.reps}
    watch.start()
    time.sleep(a.idle_seconds)
    out["gpu_before_load"] = watch.stop()

    started = time.time()
    engine = LocalEngine(a.base, adapters, backend="fast", prepare_workers=1, max_adapters=len(adapters), prefix_pool_mb=a.prefix_pool_mb)
    fast, batched = engine.fast, engine.batched
    pool = fast.prefixes
    out["engine_start_s"] = round(time.time() - started, 1)
    tok = fast.tokenizer
    states = [str(json.loads(line)["state"]) for line in a.text.read_text().splitlines()[:200] if line.strip()]
    prefix = long_text(tok, states[:10], a.prefix_tokens)
    new_ids = tok(long_text(tok, states[10:], 300), add_special_tokens=False)["input_ids"]
    out["prefix_tokens"] = len(tok(prefix, add_special_tokens=False)["input_ids"])
    index = fast.index(a.use)
    slots, _ = batched.ensure_prefixes([(index, prefix)], 8192)  # cached for every run below
    pool.unpin(slots)

    def decide(row: Row) -> None:
        slot = pool.lookup(index, row.prefix_text)
        pool.pin([slot])
        try:
            batched.decide([(index, slot, row)])
        finally:
            pool.unpin([slot])

    results: dict[str, Any] = {}
    with torch.inference_mode():
        for n in (int(x) for x in a.new.split(",")):
            row = Row(prefix, new_ids[:n], 32)
            watch.start()
            decided = timed(lambda row=row: decide(row), a.warmup, a.reps)
            graph, gout = batched.graphs[(1, n)]
            size = META + n  # the B = 1 layout of the packed input buffer, as the last decide left it

            def bare() -> None:
                batched.inp[:size].copy_(batched.inp_host[:size], non_blocking=True)
                graph.replay()
                batched.out_host[:1].copy_(gout, non_blocking=True)
                torch.cuda.synchronize()

            replay = timed(bare, a.warmup, a.reps)
            gpu = []
            for i in range(a.warmup + a.reps):
                e0, e1 = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
                e0.record()
                graph.replay()
                e1.record()
                e1.synchronize()
                if i >= a.warmup:
                    gpu.append(e0.elapsed_time(e1))
            results[str(n)] = {"decide_ms": pct(decided), "replay_ms": pct(replay), "gpu_ms": pct(gpu),
                               "plan": {"gdn": batched.plans[(1, n)].gdn, "splits": batched.plans[(1, n)].splits},
                               "gpu_watch": watch.stop()}
            print(n, json.dumps(results[str(n)]), flush=True)
    out["batch1"] = results
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
