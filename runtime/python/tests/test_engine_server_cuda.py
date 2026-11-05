"""The engine process on the fast backend (plan 2b-1 Task 7): an EngineServer with its Scheduler and a real LocalEngine
holding three v1.3 adapters on datigator, three clients on their own connections at once. Skipped, saying why, where
there is no NVIDIA GPU or the weights are absent (as on the Mac).

Run on datigator: .venv/bin/python -m pytest -m cuda runtime/python/tests/test_engine_server_cuda.py -s"""

from __future__ import annotations

import glob
import json
import threading
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.cuda

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
TEST_ROWS = "/mnt/d/jeff-models/v1.3/casdgx01/testsets/{name}/test.jsonl"
ADAPTERS = ("triage", "nav", "spam")
ROWS_PER_CLIENT = 2
# As test_batched_path_cuda.MAX_PROBABILITY_DIFFERENCE (controller ruling, Task 5): a row in a mixed pass against the
# same row alone (another batch bucket's graph) may differ by this much, with the same top option.
MAX_PROBABILITY_DIFFERENCE = 0.03


def top(probabilities: list[float]) -> int:
    return probabilities.index(max(probabilities))


@pytest.fixture(scope="module")
def served() -> Any:
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    folders = {name: glob.glob(CHECKPOINTS.format(name=name)) for name in ADAPTERS}
    missing = [str(BASE)] if not BASE.exists() else []
    missing += [CHECKPOINTS.format(name=name) for name, found in folders.items() if not found]
    missing += [TEST_ROWS.format(name=name) for name in ADAPTERS if not Path(TEST_ROWS.format(name=name)).exists()]
    if missing:
        pytest.skip(f"needs the v1.3 weights and test rows of datigator, missing here: {missing}")
    from jeff_runtime import LocalEngine
    from jeff_runtime.scheduler import Settings
    from jeff_runtime.testing import Running

    adapters = {name: Path(found[0]) for name, found in folders.items()}
    engine = LocalEngine(BASE, adapters, backend="fast", prepare_workers=3, prefix_pool_mb=1024,
                         max_adapters=len(adapters))
    # a long window (the clients send together, released by a barrier once all three are connected), so their
    # requests are gathered into one micro-batch however slowly the threads are scheduled
    running = Running(engine, fixed=adapters, settings=Settings(window_ms=3000, max_batch_rows=64,
                                                                queue_limit_rows=4096), prepare_workers=3)
    try:
        yield running, engine
    finally:
        running.close()


def rows_in_one_bucket(engine: Any) -> tuple[int, dict[str, list[dict[str, Any]]]]:
    """ROWS_PER_CLIENT real test rows of each adapter, all in one new-token bucket (the smallest every adapter has
    enough rows in), so the three clients' rows can share one pass."""
    from jeff_runtime.engine.prepare import NEW_BUCKETS

    by_bucket: dict[int, dict[str, list[dict[str, Any]]]] = {}
    for name in ADAPTERS:
        for line in Path(TEST_ROWS.format(name=name)).read_text().splitlines()[:200]:
            item = json.loads(line)
            row = {"state": item["state"], "question": item["question"]}
            prepared = engine.prepare(name, [row])
            if prepared.route != "fast":
                continue
            n = len(prepared.items[0].new_ids)
            bucket = next(b for b in NEW_BUCKETS if b >= n)
            by_bucket.setdefault(bucket, {}).setdefault(name, []).append(row)
    for bucket in sorted(by_bucket):
        chosen = by_bucket[bucket]
        if all(len(chosen.get(name, [])) >= ROWS_PER_CLIENT for name in ADAPTERS):
            return bucket, {name: chosen[name][:ROWS_PER_CLIENT] for name in ADAPTERS}
    raise AssertionError(f"no new-token bucket holds {ROWS_PER_CLIENT} rows of every adapter: "
                         f"{ {b: {n: len(r) for n, r in v.items()} for b, v in by_bucket.items()} }")


def ask(running: Any, adapter: str, rows: list[dict[str, Any]], number: int,
        together: threading.Barrier | None = None) -> dict[str, Any]:
    client = running.client()
    try:
        if together is not None:
            together.wait(60)  # every client connected and greeted: all send at once
        client.send({"op": "predict", "id": number, "adapter": adapter, "rows": rows})
        reply = client.receive()
    finally:
        client.disconnect()
    assert reply["ok"], reply
    return dict(reply["result"])


def test_three_concurrent_clients_rows_of_three_adapters_share_one_pass(served: Any) -> None:
    """Each client sends two rows of its own adapter, all three at once: they run in one micro-batch, as one pass of
    six rows mixing the three adapters, and each client gets its own rows' answers, equal (top option, and within
    MAX_PROBABILITY_DIFFERENCE) to the same rows sent alone, one client at a time, before."""
    running, engine = served
    bucket, rows = rows_in_one_bucket(engine)
    alone = {name: ask(running, name, rows[name], 1) for name in ADAPTERS}  # also caches every prefix
    for name, result in alone.items():
        assert result["timing"]["adapters_in_pass"] == [1] * ROWS_PER_CLIENT, (name, result["timing"])
    together: dict[str, dict[str, Any]] = {}
    barrier = threading.Barrier(len(ADAPTERS))
    clients = [threading.Thread(target=lambda name=name: together.__setitem__(
                   name, ask(running, name, rows[name], 2, barrier)))
               for name in ADAPTERS]
    for client in clients:
        client.start()
    for client in clients:
        client.join(120)
    assert sorted(together) == sorted(ADAPTERS)
    worst = 0.0
    for name in ADAPTERS:
        timing = together[name]["timing"]
        print(f"\n{name}: {timing}")
        assert timing["path"] == "fast" and timing["batch_rows"] == 3 * ROWS_PER_CLIENT
        assert timing["adapters_in_pass"] == [3] * ROWS_PER_CLIENT
        assert timing["pass_rows"] == [3 * ROWS_PER_CLIENT] * ROWS_PER_CLIENT
        assert timing["bucket"] == [bucket] * ROWS_PER_CLIENT and timing["prefix_misses"] == 0
        assert together[name]["input_tokens"] == alone[name]["input_tokens"]
        assert timing["new_tokens"] == alone[name]["timing"]["new_tokens"]  # its own rows', not the micro-batch's
        assert len(timing["new_tokens"]) == ROWS_PER_CLIENT and all(0 < n <= bucket for n in timing["new_tokens"])
        for got, expected in zip(together[name]["probabilities"], alone[name]["probabilities"], strict=True):
            assert top(got) == top(expected), (name, got, expected)
            worst = max(worst, max(abs(x - y) for x, y in zip(got, expected, strict=True)))
    print(f"bucket {bucket}: largest difference together vs alone {worst:.4f}")
    assert worst <= MAX_PROBABILITY_DIFFERENCE
