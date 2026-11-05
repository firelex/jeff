"""Batch 1 in process on MLX (plan 2b-1 Task 7, fix round 1): Jeff.local with one shipped adapter (triage), one
decision at a time through Jeff.decide, timing its engine call two ways in one process, call after call alternating
(either first, in turn):
- before: what LocalEngine.predict did before Task 7 on MLX (_validated, then the model's own pass);
- now: LocalEngine.predict as it is (prepared without a second rendering and tokenising pass, then predict_many).
The engine's predict is swapped between the two for each call; everything else (Jeff, the catalogue, the call) is the
same. Prints and writes p50, p99 and mean of the whole Jeff.decide call, in ms.

Run on the Mac, alone (one heavy local model at a time):
    uv run python scripts/bench_mlx_local.py --out results/step2b1/task7_mlx_local.json --warmup 50 --reps 500"""

from __future__ import annotations

import argparse
import json
import platform
import statistics
import time
from pathlib import Path
from typing import Any

from jeff import Call, Jeff
from jeff.engine import EngineResult, Prepared

CALL = Call("triage.urgency", "My wardrobe arrived broken and I need it replaced before the weekend.",
            {"company": "A furniture shop.", "channel": "email"})


def pct(samples: list[float]) -> dict[str, float]:
    cuts = statistics.quantiles(samples, n=100, method="inclusive")
    return {"p50": round(cuts[49], 3), "p99": round(cuts[98], 3), "mean": round(statistics.fmean(samples), 3)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--warmup", type=int, required=True)
    parser.add_argument("--reps", type=int, required=True)
    args = parser.parse_args()

    jeff = Jeff.local(["triage"], backend="mlx")
    engine: Any = jeff.engine
    now_predict = engine.predict

    def before_predict(adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        """LocalEngine.predict on MLX before Task 7: _validated, then the model's pass (as _one_group runs it)."""
        validated = engine._validated(adapter_id, rows, engine._max_options[adapter_id])
        return engine._one_group(adapter_id, Prepared(validated, generation=engine._generations[adapter_id]))

    samples: dict[str, list[float]] = {"before": [], "now": []}
    answers: dict[str, Any] = {}
    paths = (("before", before_predict), ("now", now_predict))
    for i in range(args.warmup + args.reps):
        for label, predict in paths if i % 2 == 0 else paths[::-1]:
            engine.predict = predict
            started = time.perf_counter()
            [decision] = jeff.decide([CALL])
            elapsed = (time.perf_counter() - started) * 1000
            answers[label] = decision
            if i >= args.warmup:
                samples[label].append(elapsed)
    engine.predict = now_predict
    got = {label: pct(values) for label, values in samples.items()}
    same = all(getattr(answers["before"], name) == getattr(answers["now"], name) for name in ("value", "probabilities"))
    record = {"script": "scripts/bench_mlx_local.py", "date": time.strftime("%Y-%m-%d %H:%M"),
              "machine": platform.machine(), "adapter": "triage", "call": "triage.urgency, one row",
              "warmup": args.warmup, "reps": args.reps,
              "order": "alternating call after call, the first of each pair swapped on every iteration",
              "jeff_decide_ms": got, "same_decision": same,
              "now_vs_before_p50": round(got["now"]["p50"] / got["before"]["p50"], 4)}
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(record, indent=1) + "\n")
    print(json.dumps(record, indent=1))


if __name__ == "__main__":
    main()
