"""Time the two local decision loops, so a change to either can be checked for added per-call cost.

1. The engine socket: SocketEngine.predict of one row against the fake engine server (jeff_runtime.testing.Running
   over jeff.testing.FakeEngine) on a temporary Unix socket. The scheduler runs batches of one row, so a call never
   waits out a batching window and the time measured is the socket round trip and its framing.
2. In process: Jeff over jeff.testing.FakeEngine deciding one call, as Jeff.local does over the real engine.

Prints, as JSON, the 50th and 99th percentile of each loop in microseconds over CALLS calls (after WARMUP untimed
calls). Usage: uv run python scripts/bench_local_loop.py > results/step2a5/bench-before.json"""

import json
import platform
import statistics
import sys
import time
from pathlib import Path
from typing import Any

from jeff import Call, Jeff
from jeff.catalogue import Catalogue
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine
from jeff_runtime.scheduler import Settings
from jeff_runtime.testing import Running

ROOT = Path(__file__).resolve().parents[1]
CATALOGUE = ROOT / "packages" / "python" / "tests" / "fixtures" / "catalogue"
ADAPTER = "jeff.support.triage"
CALLS = 5_000
WARMUP = 500
ROW = {"state": {}, "question": {"type": "noul", "instructions": "?"}}
CALL = Call("triage.urgency", "Broken wardrobe", {"company": "A furniture shop.", "channel": "email"})


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return [0.0, 0.0, 0.9, 0.1, 0.0]
    if question["type"] == "noul":
        return [0.2, 0.8]
    return [1.0] + [0.0] * (len(question["criteria"]) - 1)


def percentiles(samples_ns: list[int]) -> dict[str, float]:
    cuts = statistics.quantiles(samples_ns, n=100, method="inclusive")
    return {"p50_us": round(cuts[49] / 1000, 2), "p99_us": round(cuts[98] / 1000, 2)}


def timed(work: Any) -> list[int]:
    for _ in range(WARMUP):
        work()
    samples = []
    for _ in range(CALLS):
        start = time.perf_counter_ns()
        work()
        samples.append(time.perf_counter_ns() - start)
    return samples


def socket_loop() -> dict[str, float]:
    server = Running(FakeEngine(answers, {ADAPTER}), fixed={ADAPTER: Path("/x")},
                     settings=Settings(window_ms=20, max_batch_rows=1, queue_limit_rows=4096))
    try:
        engine = SocketEngine(server.path, timeout_s=5)
        try:
            return percentiles(timed(lambda: engine.predict(ADAPTER, [ROW])))
        finally:
            engine.close()
    finally:
        server.close()


def in_process_loop() -> dict[str, float]:
    jeff = Jeff(FakeEngine(answers, {ADAPTER}), Catalogue.from_dir(CATALOGUE))
    return percentiles(timed(lambda: jeff.decide([CALL])))


def main() -> None:
    report = {"calls": CALLS, "warmup": WARMUP, "python": sys.version.split()[0], "machine": platform.machine(),
              "socket_predict": socket_loop(), "in_process_decide": in_process_loop()}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
