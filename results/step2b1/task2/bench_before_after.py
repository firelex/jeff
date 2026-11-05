"""Task 2 batch-1 latency, before and after, in one process, back to back (design Section 7, constraints): today's
FastPath (the stacked pool, the new LoRA tiles) against the FastPath of e349d1b (a copy of its fast_path.py, given
as --before), both built on the same loaded model and adapter set, so they share the base weights and differ only in
the fast path's own code.

As scripts/bench_fast_path.py: one question after a cached prefix of --prefix-tokens tokens, --use adapter, 32 answer
options; at each new-token count, --rounds rounds, each timing the before path and then the after path (decide(),
the bare decision, the graph replay alone), --reps runs after --warmup untimed runs each.

Usage (on datigator): .venv/bin/python results/step2b1/task2/bench_before_after.py --before FAST_PATH_PRE.py \\
    --base DIR --adapter nav=DIR [...] --use nav --text TEST.jsonl --output OUT.json"""
import argparse
import importlib.util
import json
import sys
import time
from pathlib import Path
from typing import Any

import torch

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
from bench_fast_path import NVIDIA_SMI, GpuWatch, long_text, pct, timed, versions  # noqa: E402


def load_module(path: Path) -> Any:
    spec = importlib.util.spec_from_file_location("fast_path_before", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["fast_path_before"] = module
    spec.loader.exec_module(module)
    return module


def measure(fast: Any, adapter: str, row: Any, cap: int, n: int, warmup: int, reps: int) -> dict[str, Any]:
    decide = timed(lambda: fast.decide(adapter, [row], 8192), warmup, reps)
    graph, gout = fast.graphs[(cap, n)]

    def bare() -> None:
        fast.ids.copy_(fast.ids_host, non_blocking=True)
        fast.scalars.copy_(fast.scalars_host, non_blocking=True)
        graph.replay()
        fast.out_host.copy_(gout, non_blocking=True)
        torch.cuda.synchronize()

    replay = timed(bare, warmup, reps)
    gpu = []
    for i in range(warmup + reps):
        e0, e1 = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        e0.record()
        graph.replay()
        e1.record()
        e1.synchronize()
        if i >= warmup:
            gpu.append(e0.elapsed_time(e1))
    return {"decide_ms": pct(decide), "replay_ms": pct(replay), "gpu_ms": pct(gpu)}


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--before", type=Path, required=True, help="fast_path.py as it was before Task 2")
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--adapter", action="append", required=True, help="NAME=DIR, once per adapter to load")
    p.add_argument("--use", required=True)
    p.add_argument("--text", type=Path, required=True)
    p.add_argument("--prefix-tokens", type=int, default=1000)
    p.add_argument("--new", default="32,64,128,256")
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--reps", type=int, default=1000)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(f"Refusing to overwrite {a.output}")
    adapters = dict(item.split("=", 1) for item in a.adapter)
    adapters = {name: Path(folder) for name, folder in adapters.items()}

    from jeff_runtime.engine.fast_path import PREFIX_CAPS, Row
    from jeff_runtime.local_engine import LocalEngine

    before_module = load_module(a.before)
    started = time.time()
    engine = LocalEngine(a.base, adapters, backend="fast", max_adapters=len(adapters))
    after = engine.fast
    before = before_module.FastPath(engine.model, engine.adapter_set, 8192)
    out: dict[str, Any] = {"versions": versions(), "adapters_loaded": len(adapters), "use": a.use, "warmup": a.warmup,
                           "reps": a.reps, "rounds": a.rounds, "start_s": round(time.time() - started, 1)}
    tok = after.tokenizer
    states = [str(json.loads(line)["state"]) for line in a.text.read_text().splitlines()[:200] if line.strip()]
    prefix = long_text(tok, states[:10], a.prefix_tokens)
    new_ids = tok(long_text(tok, states[10:], 300), add_special_tokens=False)["input_ids"]
    p_len = len(tok(prefix, add_special_tokens=False)["input_ids"])
    out["prefix_tokens"] = p_len
    cap = next(c for c in PREFIX_CAPS if c >= p_len)

    watch = GpuWatch(NVIDIA_SMI)
    results: dict[str, Any] = {}
    with torch.inference_mode():
        for n in (int(x) for x in a.new.split(",")):
            row_after = Row(prefix, new_ids[:n], 32)
            row_before = before_module.Row(prefix, new_ids[:n], 32)
            same = after.decide(a.use, [row_after], 8192)[0][0], before.decide(a.use, [row_before], 8192)[0][0]
            entry: dict[str, Any] = {"largest_probability_difference": max(abs(x - y) for x, y in zip(*same)),
                                     "same_top": same[0].index(max(same[0])) == same[1].index(max(same[1])),
                                     "rounds": []}
            watch.start()
            for _ in range(a.rounds):
                entry["rounds"].append({"before": measure(before, a.use, row_before, cap, n, a.warmup, a.reps),
                                        "after": measure(after, a.use, row_after, cap, n, a.warmup, a.reps)})
            entry["gpu_watch"] = watch.stop()
            for kind in ("decide_ms", "replay_ms", "gpu_ms"):
                entry[f"{kind}_p50"] = {side: [r[side][kind]["p50"] for r in entry["rounds"]] for side in ("before", "after")}
            results[str(n)] = entry
            print(n, json.dumps({k: v for k, v in entry.items() if k.endswith("p50")}), flush=True)
    out["batch1"] = results
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
