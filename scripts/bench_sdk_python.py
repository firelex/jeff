"""Latency of one Jeff function through the Python SDK in-process (Jeff.local: the model loaded in this process).
Shares packages/node/bench/latency.ts's function/input/context/labels/calls/concurrency/warmup options and its
statistics (round_trip_ms, model_ms, overhead_ms, queue_ms, batch_rows, throughput_per_s), so the numbers compare --
but not its --rounds interleaving: this is a separate process with its own in-process model, not another path onto
the engine latency.ts talks to, so there is no shared engine to interleave against. Needs the weights; run on the
Mac or the RTX, with no other model process running.

Honesty: --warmup calls are discarded first (a cold model run is not the number to publish). round_trip_ms,
model_ms, overhead_ms, queue_ms and batch_rows are always measured one call at a time (concurrency 1); model_ms is
the engine's own timing, overhead_ms is round trip minus model_ms (everything this path adds). Jeff.local's
in-process engine has no scheduler (one request prepared at a time, prepare_workers=1, no micro-batching), so it
never reports queue_ms or batch_rows -- both print as null here, not zero. throughput_per_s is measured separately,
at --concurrency concurrent callers (Python threads: the GIL still serialises anything that does not release it)."""

import argparse
import importlib.metadata
import json
import math
import platform
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from jeff import Jeff


def percentile(sorted_ms: list[float], q: float) -> float:
    if not sorted_ms:
        raise ValueError("percentile of no measurements")
    return sorted_ms[min(len(sorted_ms) - 1, max(0, math.ceil(q * len(sorted_ms)) - 1))]


def summary(ms: list[float]) -> dict[str, float]:
    ordered = sorted(ms)
    return {"p50": percentile(ordered, 0.5), "p95": percentile(ordered, 0.95), "p99": percentile(ordered, 0.99),
            "mean": sum(ordered) / len(ordered)}


def print_environment(jeff: Jeff) -> None:
    """The machine, the backend (read from the in-process engine itself: Jeff.local always knows it) and versions,
    to stderr, once, before any result line. batch_window_ms and max_batch_rows are not printed: Jeff.local has no
    scheduler, so neither setting exists here (see the module docstring)."""
    print(json.dumps({
        "machine": {"hostname": platform.node(), "platform": platform.system(), "arch": platform.machine()},
        "python_version": sys.version.split()[0],
        "versions": {name: importlib.metadata.version(name) for name in ("jeff-sdk", "jeff-runtime")},
        "engine": {"backend": jeff.engine.backend, "batch_window_ms": None, "max_batch_rows": None,
                   "prepare_workers": 1},
    }), file=sys.stderr)


def print_note(throughput_concurrency: int) -> None:
    print("round_trip_ms, model_ms, overhead_ms, queue_ms and batch_rows are measured at concurrency 1 (one "
          f"caller, one call at a time); throughput_per_s is measured at concurrency {throughput_concurrency}.",
          file=sys.stderr)


def number_field(timing: dict[str, object], key: str) -> float | None:
    value = timing.get(key)
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--adapter", action="append", required=True, help="an adapter to load (repeatable)")
    parser.add_argument("--function", required=True)
    parser.add_argument("--input", required=True)
    parser.add_argument("--context", help="JSON object of context values, by code name")
    parser.add_argument("--labels", help="JSON labels")
    parser.add_argument("--calls", type=int, default=300)
    parser.add_argument("--concurrency", type=int, required=True)
    parser.add_argument("--warmup", type=int, default=10, help="discarded calls run before any measurement")
    parser.add_argument("--prefix-pool-mb", type=int, help="required on an NVIDIA GPU (the fast backend)")
    args = parser.parse_args()
    options = {} if args.prefix_pool_mb is None else {"prefix_pool_mb": args.prefix_pool_mb}
    jeff = Jeff.local(args.adapter, **options)
    bound = jeff.function(args.function, context=json.loads(args.context) if args.context else None,
                          labels=json.loads(args.labels) if args.labels else None)

    print_environment(jeff)

    for _ in range(args.warmup):
        bound(args.input)

    round_trips: list[float] = []
    model_ms: list[float] = []
    overhead_ms: list[float] = []
    queue_ms: list[float] = []
    batch_rows: list[float] = []
    for _ in range(args.calls):
        started = time.perf_counter()
        decision = bound(args.input)
        elapsed = (time.perf_counter() - started) * 1000
        round_trips.append(elapsed)
        model = number_field(decision.timing, "model_ms")
        if model is not None:
            model_ms.append(model)
            overhead_ms.append(elapsed - model)
        queue = number_field(decision.timing, "queue_ms")
        if queue is not None:
            queue_ms.append(queue)
        rows = number_field(decision.timing, "batch_rows")
        if rows is not None:
            batch_rows.append(rows)

    started = time.perf_counter()
    with ThreadPoolExecutor(args.concurrency) as pool:
        list(pool.map(lambda _: bound(args.input), range(args.calls)))
    throughput_per_s = args.calls / (time.perf_counter() - started)

    print_note(args.concurrency)
    print(json.dumps({
        "path": "python-local", "function": args.function, "calls": args.calls, "warmup": args.warmup,
        "round_trip_ms": summary(round_trips),
        "model_ms": {"p50": percentile(sorted(model_ms), 0.5), "p95": percentile(sorted(model_ms), 0.95)}
            if model_ms else None,
        "overhead_ms": summary(overhead_ms) if overhead_ms else None,
        "queue_ms": {"p50": percentile(sorted(queue_ms), 0.5)} if queue_ms else None,
        "batch_rows": {"p50": percentile(sorted(batch_rows), 0.5), "max": max(batch_rows)} if batch_rows else None,
        "throughput_per_s": throughput_per_s,
        "round_trip_concurrency": 1,
        "throughput_concurrency": args.concurrency,
    }))


if __name__ == "__main__":
    main()
