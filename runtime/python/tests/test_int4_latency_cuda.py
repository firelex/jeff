"""An INT4 engine is never slower than a bfloat16 engine (plan 2b-2 Task 5): scripts/bench_int4.py as a test. For
every (B, N) of the benchmark (B = 1 / 4 / 16 / 64, N = 32 / 64 / 128 / 256 new tokens), the INT4 engine's p50 wall
time of BatchedPath.decide is at most the bfloat16 engine's plus 0.05 ms, both engines in this process, measured back
to back in alternating rounds (the method is in the script's docstring). The full latency and throughput table goes
to results/step2b2/latency.json.

Needs datigator's v1.3 base, its INT4 folder, the nine v1.3 adapters and the legal-clauses test rows, nvidia-smi on
PATH (/usr/lib/wsl/lib on datigator), and GPU memory for two engines (estimated under 25 GB; the peak is recorded in
latency.json). Estimated at about 10 minutes (not yet run).

Run on datigator (detached: results/step2b2/task5/run_latency.sh):
  PATH=$PATH:/usr/lib/wsl/lib .venv/bin/python -m pytest -m cuda runtime/python/tests/test_int4_latency_cuda.py -s"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.cuda

REPO = Path(__file__).resolve().parents[3]
RESULTS = REPO / "results/step2b2/latency.json"


def test_an_int4_engine_is_at_most_005_ms_slower_than_a_bfloat16_engine_at_every_batch_and_length() -> None:
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    sys.path.insert(0, str(REPO / "scripts"))
    import bench_int4

    missing = [str(path) for path in (bench_int4.BASE, bench_int4.INT4_BASE, bench_int4.TEST_ROWS) if not path.exists()]
    if missing:
        pytest.skip(f"needs datigator's v1.3 base, its INT4 folder and the test rows; missing here: {missing}")
    record = bench_int4.run("runtime/python/tests/test_int4_latency_cuda.py (other GPU users: gpu_state_before and "
                            "gpu_state_after)", RESULTS)
    print(f"\n{'B x N':>8} {'bf16 p50':>9} {'INT4 p50':>9} {'change':>8} {'rows/s bf16 -> INT4':>22}  "
          "INT4 graph's kernel")
    for shape, res in record["results"].items():
        print(f"{shape:>8} {res['bf16']['p50']:9.3f} {res['int4']['p50']:9.3f} {res['int4_minus_bf16_p50_ms']:+8.3f} "
              f"{res['bf16']['rows_per_s']:>10} -> {res['int4']['rows_per_s']:<9}  {res['int4_product_kernel']} "
              f"{res['int4_product_ms']}")
    over = {shape: record["results"][shape] for shape in record["over_bound"]}
    assert not over, f"INT4 p50 over the bfloat16 p50 + {bench_int4.BOUND_MS} ms: {over}"
