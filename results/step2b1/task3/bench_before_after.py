"""Task 3 batch-1 latency, before and after, in one process, back to back (design Section 7, constraints): today's
FastPath (prefixes in the unified page pool of 8,192 MB, staged from it) against the FastPath of 23fb008 (a copy of its fast_path.py,
given as --before: prefixes in an OrderedDict of `Prefix` tensors), both built on the same loaded model and adapter
set, so they share the base weights and differ only in the fast path's own code.

As results/step2b1/task2/bench_before_after.py: one question after a cached prefix of --prefix-tokens tokens,
--use adapter, 32 answer options; at each new-token count, --rounds rounds, each timing the before path and then
the after path (the after path first with --after-first, to see any effect of the order), --reps runs after --warmup untimed runs each, of:
- decide_ms: decide() on one row whose prefix is cached and staged (the local loop);
- switch_ms: decide() alternating two rows with different cached prefixes, so every call stages its prefix (the
  part this task changed: before, three copies from the `Prefix`; after, from the slot and a gather of its pages);
and once per round, prefix_ms: a new prefix of --prefix-tokens tokens processed (the prefix graph, plus the copies
out of it before; the writes into the slot and pages are inside the graph after).

Usage (on datigator): .venv/bin/python results/step2b1/task3/bench_before_after.py --before FAST_PATH_PRE.py \\
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


def measure(fast: Any, adapter: str, row: Any, other: Any, warmup: int, reps: int) -> dict[str, Any]:
    decide = timed(lambda: fast.decide(adapter, [row], 8192), warmup, reps)
    flip = [row, other]
    state = {"i": 0}

    def switch() -> None:
        state["i"] ^= 1
        fast.decide(adapter, [flip[state["i"]]], 8192)

    return {"decide_ms": pct(decide), "switch_ms": pct(timed(switch, warmup, reps))}


def prefix_pass(fast: Any, ids: list[int], index: int, after: bool, reps: int) -> dict[str, float]:
    samples = []
    if after:
        slot = fast.prefixes.reserve(index, "bench prefix", len(ids))
    for i in range(reps + 3):
        torch.cuda.synchronize()
        t = time.perf_counter()
        if after:
            fast.run_prefix(ids, index, slot)
        else:
            fast.run_prefix(ids, index)
        torch.cuda.synchronize()
        if i >= 3:
            samples.append((time.perf_counter() - t) * 1000)
    if after:
        fast.prefixes.forget_adapter(index)  # drops the bench prefix (and the adapter's others: they come back)
    fast.staged = None
    return pct(samples)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--before", type=Path, required=True, help="fast_path.py as it was before Task 3")
    p.add_argument("--base", type=Path, required=True)
    p.add_argument("--adapter", action="append", required=True, help="NAME=DIR, once per adapter to load")
    p.add_argument("--use", required=True)
    p.add_argument("--text", type=Path, required=True)
    p.add_argument("--prefix-tokens", type=int, default=1000)
    p.add_argument("--new", default="32,64,128,256")
    p.add_argument("--warmup", type=int, default=100)
    p.add_argument("--reps", type=int, default=1000)
    p.add_argument("--rounds", type=int, default=3)
    p.add_argument("--after-first", action="store_true")
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(f"Refusing to overwrite {a.output}")
    adapters = {name: Path(folder) for name, folder in (item.split("=", 1) for item in a.adapter)}

    from jeff_runtime.engine.fast_path import Row
    from jeff_runtime.local_engine import LocalEngine

    before_module = load_module(a.before)
    started = time.time()
    engine = LocalEngine(a.base, adapters, backend="fast", max_adapters=len(adapters), prefix_pool_mb=8192)
    after = engine.fast
    before = before_module.FastPath(engine.model, engine.adapter_set, 8192, len(adapters))
    out: dict[str, Any] = {"versions": versions(), "adapters_loaded": len(adapters), "use": a.use, "warmup": a.warmup,
                           "reps": a.reps, "rounds": a.rounds, "after_first": a.after_first, "start_s": round(time.time() - started, 1),
                           "after_pool": after.describe()}
    tok = after.tokenizer
    states = [str(json.loads(line)["state"]) for line in a.text.read_text().splitlines()[:200] if line.strip()]
    prefix = long_text(tok, states[:10], a.prefix_tokens)
    other_prefix = long_text(tok, states[20:30], a.prefix_tokens)
    new_ids = tok(long_text(tok, states[10:], 300), add_special_tokens=False)["input_ids"]
    prefix_ids = tok(prefix, add_special_tokens=False)["input_ids"]
    out["prefix_tokens"] = len(prefix_ids)
    index = after.index(a.use)
    assert before.index(a.use) == index

    watch = GpuWatch(NVIDIA_SMI)
    results: dict[str, Any] = {}
    with torch.inference_mode():
        for n in (int(x) for x in a.new.split(",")):
            rows = {side: (module.Row(prefix, new_ids[:n], 32), module.Row(other_prefix, new_ids[:n], 32))
                    for side, module in (("before", before_module), ("after", sys.modules[Row.__module__]))}
            same = after.decide(a.use, [rows["after"][0]], 8192)[0][0], before.decide(a.use, [rows["before"][0]], 8192)[0][0]
            entry: dict[str, Any] = {"largest_probability_difference": max(abs(x - y) for x, y in zip(*same)),
                                     "rounds": []}
            watch.start()
            for _ in range(a.rounds):
                order = [("before", before), ("after", after)]
                entry["rounds"].append({side: measure(fast, a.use, *rows[side], a.warmup, a.reps)
                                        for side, fast in (order[::-1] if a.after_first else order)})
            entry["gpu_watch"] = watch.stop()
            for kind in ("decide_ms", "switch_ms"):
                for q in ("p50", "p99"):
                    entry[f"{kind}_{q}"] = {side: [r[side][kind][q] for r in entry["rounds"]]
                                            for side in ("before", "after")}
            results[str(n)] = entry
            print(n, json.dumps({k: v for k, v in entry.items() if k.endswith(("p50", "p99"))}), flush=True)
        passes = []
        for _ in range(a.rounds):
            timed_pass = {"before": lambda: prefix_pass(before, prefix_ids, index, False, 50),
                          "after": lambda: prefix_pass(after, prefix_ids, index, True, 50)}
            sides = ["after", "before"] if a.after_first else ["before", "after"]
            passes.append({side: timed_pass[side]() for side in sides})
        out["prefix_pass_ms_p50"] = {side: [r[side]["p50"] for r in passes] for side in ("before", "after")}
        print("prefix pass", json.dumps(out["prefix_pass_ms_p50"]), flush=True)
    out["batch1"] = results
    out["prefix_pass"] = passes
    a.output.parent.mkdir(parents=True, exist_ok=True)
    a.output.write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
