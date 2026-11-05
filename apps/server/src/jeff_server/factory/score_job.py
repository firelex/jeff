"""Score rows with the base plus one adapter, through the one engine (jeff_runtime.LocalEngine, the backend it picks
on this machine). Runs as its own process so the factory worker never holds a model, and its memory is freed when
scoring ends.

Usage: python -m jeff_server.factory.score_job BASE ADAPTER ROWS.jsonl OUT.json
ROWS.jsonl: one {"state", "question"} per line.
OUT.json: {"logits": [[one raw logit per option, in the row's option order], ...], "temperature": the adapter's,
"seconds": wall time of the forward passes}

The engine answers probabilities with the adapter's temperature applied (softmax of logits / temperature), so each
row's raw logits are recovered as temperature * log(probability). They differ from the model's own logits by one
constant per row, which changes neither a softmax at any temperature nor the temperature fitted on them. A
probability that is exactly 0 (a logit gap past what float32 can hold) has no finite logit and stops the job, naming
the row."""

import json
import math
import sys
import time
from pathlib import Path
from typing import Any

ADAPTER_ID = "candidate"
CHUNK = 64  # rows per predict call


def raw_logits(probabilities: list[float], temperature: float, number: int) -> list[float]:
    if any(value <= 0 for value in probabilities):
        raise ValueError(f"Row {number}: an option has probability {min(probabilities)}, which has no finite logit")
    return [temperature * math.log(value) for value in probabilities]


def main() -> None:
    base, adapter, rows_path, out = sys.argv[1:5]
    from jeff_runtime import LocalEngine, select_backend
    from jeff_runtime.engine.prefix_pool import prefix_pool_mb_for

    temperature = float(json.loads((Path(adapter) / "decision_config.json").read_text())["temperature"])
    backend = select_backend("auto")
    # one adapter, never more: the fast backend's room for adapters (required there, refused elsewhere) is 1, and
    # its prefix pool (also required there) holds one predict call's rows, each with its own prefix; this one thread
    # prepares every call, so the engine needs one processor for that (prepare_workers=1)
    engine = LocalEngine(Path(base), {ADAPTER_ID: Path(adapter)}, backend=backend, prepare_workers=1,
                         **({"max_adapters": 1, "prefix_pool_mb": prefix_pool_mb_for(CHUNK)} if backend == "fast"
                            else {}))
    rows: list[dict[str, Any]] = [json.loads(line) for line in Path(rows_path).read_text(encoding="utf-8").splitlines()
                                  if line.strip()]
    logits: list[list[float]] = []
    started = time.perf_counter()
    for start in range(0, len(rows), CHUNK):
        result = engine.predict(ADAPTER_ID, rows[start:start + CHUNK])
        logits.extend(raw_logits(values, temperature, start + index + 1)
                      for index, values in enumerate(result.probabilities))
    Path(out).write_text(json.dumps({"logits": logits, "temperature": temperature,
                                     "seconds": time.perf_counter() - started}))


if __name__ == "__main__":
    main()
