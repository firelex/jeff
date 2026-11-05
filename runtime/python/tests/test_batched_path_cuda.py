"""The batched decision graphs (batched_path.py) on a real GPU with the v1.3 weights on datigator (read only, under
/mnt/d): mixed passes against the same rows one at a time, padding rows, the refusals, and the batched prefix passes.
Skipped, saying why, where there is no NVIDIA GPU or those files are absent (as on the Mac).

One engine (the nine v1.3 adapters) and one BatchedPath are built for the whole module; building the BatchedPath
records its graphs, and the time and memory that takes are printed.

The batch-1 latency against the single-row path it replaced (FastPath.decide, removed in plan 2b-1 Task 8) was
checked here until then, in one process, call after call; the last such comparison is results/step2b1/
batch1_final.json (results/step2b1/task8/batch1_final.py).

Run on datigator: .venv/bin/python -m pytest -m cuda runtime/python/tests/test_batched_path_cuda.py -s"""

from __future__ import annotations

import gc
import glob
import json
import random
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.cuda

REPO = Path(__file__).resolve().parents[3]
BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
TEST_ROWS = "/mnt/d/jeff-models/v1.3/casdgx01/testsets/{name}/test.jsonl"
ADAPTERS = ("triage", "nav", "spam", "emotion", "ground", "guard", "legal-clauses", "support-intents", "tools")
# The largest probability difference allowed between a row in a pass and the same row run alone in the B = 1 graph
# (and, until plan 2b-1 Task 8 removed it, through the single-row FastPath.decide); the top option must be equal on
# every row. Controller ruling (Task 5): 0.03, not the brief's 0.02, because 0.02 lies inside the spread between equally legitimate kernel
# choices, measured on datigator (2026-10-24) on the same 64 real rows at N = 32, eagerly with forced settings:
# - two attention splits of the same B = 1 graph (16 against 1): 0.027;
# - fla's chunk kernel against the recurrent kernel in the B = 64 pass: 0.026;
# - a B = 1 setting (chunk, split 1) against the single-row FastPath.decide of the time: 0.022.
# Bfloat16 rounding through 24 layers moves a row that much whatever the setting. The accuracy gate is Task 8's 99%
# per-adapter top-option agreement with the standard path.
MAX_PROBABILITY_DIFFERENCE = 0.03
# The largest probability difference allowed between a decision on a prefix made in a batched prefix pass and the same
# decision on that prefix made alone (one at a time); top options must be equal on the test's rows (not near ties).
# Controller ruling (Task 6): 0.05. A pass of several prefixes is not bit for bit the same as each prefix alone,
# because cuBLAS chooses its bfloat16 matrix product kernels by the number of rows, and PyTorch's attention kernel
# varies with the batch. Measured on datigator (2026-10-24): 0.0287 on this test's 64 rows, and at most 0.047 over the
# design's 1,786 agreement rows (results/step2b1/prefix_batching.json). The accuracy gate is Task 8's 99% per-adapter
# top-option agreement with the standard path.
MAX_PREFIX_BATCH_DIFFERENCE = 0.05


def adapter_folder(name: str) -> Path:
    found = glob.glob(CHECKPOINTS.format(name=name))
    assert len(found) == 1, f"expected one v1.3 {name} checkpoint, found {found}"
    return Path(found[0])


def read_rows(name: str, count: int) -> list[dict[str, Any]]:
    lines = Path(TEST_ROWS.format(name=name)).read_text().splitlines()[:count]
    return [{"state": item["state"], "question": item["question"]} for item in map(json.loads, lines)]


@pytest.fixture(scope="module")
def gpu() -> SimpleNamespace:
    """The engine with the nine v1.3 adapters on the fast backend, and a BatchedPath on its FastPath (recording time
    and the GPU memory it adds measured). (The engine records its own BatchedPath, engine.batched, when it is built;
    the one here is a second, on the same FastPath, recorded where it can be measured. The tests use this one.)"""
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    missing = [str(BASE)] if not BASE.exists() else []
    missing += [CHECKPOINTS.format(name=name) for name in ADAPTERS if not glob.glob(CHECKPOINTS.format(name=name))]
    missing += [TEST_ROWS.format(name=name) for name in ADAPTERS if not Path(TEST_ROWS.format(name=name)).exists()]
    if missing:
        pytest.skip(f"needs the v1.3 weights and test rows of datigator, missing here: {missing}")
    from jeff_runtime import LocalEngine
    from jeff_runtime.engine.batched_path import BatchedPath

    engine = LocalEngine(BASE, {name: adapter_folder(name) for name in ADAPTERS}, backend="fast", prepare_workers=1,
                         prefix_pool_mb=6144, max_adapters=len(ADAPTERS))
    torch.cuda.synchronize()
    before = {"allocated_gb": torch.cuda.memory_allocated() / 2**30, "reserved_gb": torch.cuda.memory_reserved() / 2**30}
    started = time.perf_counter()
    batched = BatchedPath(engine.fast)
    seconds = time.perf_counter() - started
    after = {"allocated_gb": torch.cuda.memory_allocated() / 2**30, "reserved_gb": torch.cuda.memory_reserved() / 2**30}
    recording = {"graphs": len(batched.graphs), "seconds": round(seconds, 1),
                 "gpu_memory_before_gb": {k: round(v, 2) for k, v in before.items()},
                 "gpu_memory_after_gb": {k: round(v, 2) for k, v in after.items()},
                 "added_gb": {k: round(after[k] - before[k], 2) for k in after},
                 "timing_reserved_gb": batched.timing_reserved_gb,
                 "plans": {f"B={b} N={n}": {"gdn": p.gdn, "splits": p.splits, "gdn_ms": p.gdn_ms,
                                            "attention_ms": {str(s): ms for s, ms in p.attention_ms.items()}}
                           for (b, n), p in batched.plans.items()}}
    print(f"\nrecorded {recording['graphs']} batched graphs in {recording['seconds']} s; GPU memory after: "
          f"{recording['gpu_memory_after_gb']}, added {recording['added_gb']}; reserved around the timing (GB): "
          f"{recording['timing_reserved_gb']}")
    for shape, plan in recording["plans"].items():
        print(f"  {shape}: Gated DeltaNet {plan['gdn']} {plan['gdn_ms']}, split {plan['splits']} {plan['attention_ms']}")
    return SimpleNamespace(engine=engine, fast=engine.fast, batched=batched, recording=recording, torch=torch)


@contextmanager
def pinned(gpu: SimpleNamespace, items: list[tuple[str, Any]]) -> Iterator[list[tuple[int, int, Any]]]:
    """(adapter index, prefix slot, row) for each (adapter name, prepared row): each prefix found, or processed alone
    (ensure_prefixes of that row only: the B = 1 prefix graph), and pinned, so no later row's prefix evicts it; all
    unpinned when the block ends."""
    fast = gpu.fast
    slots: list[int] = []
    try:
        out = []
        for name, row in items:
            index = fast.index(name)
            made, _ = gpu.batched.ensure_prefixes([(index, row.prefix_text)], gpu.engine.max_tokens)
            slots.extend(made)
            out.append((index, made[0], row))
        yield out
    finally:
        fast.prefixes.unpin(slots)


def prepared_rows(gpu: SimpleNamespace, per_adapter: int, seed: int) -> list[tuple[str, Any]]:
    """Real test rows of every adapter that the fast path takes, prepared with their adapter's layout, shuffled."""
    out = []
    for name in ADAPTERS:
        taken = 0
        for row in read_rows(name, 40):
            prepared = gpu.fast.prepare(name, [row])
            if isinstance(prepared, str):
                continue
            out.append((name, prepared[0]))
            taken += 1
            if taken == per_adapter:
                break
        assert taken == per_adapter, f"{name}: only {taken} of its first 40 test rows fit the fast path"
    random.Random(seed).shuffle(out)
    return out


def fitted(items: list[tuple[str, Any]], n: int) -> list[tuple[str, Any]]:
    """The rows with exactly n new tokens: their own, repeated as often as needed and cut to n (still valid inputs:
    the comparison is between passes), so they all land in new-token bucket n."""
    from jeff_runtime.engine.fast_path import Row

    return [(name, Row(row.prefix_text, (row.new_ids * n)[:n], row.count)) for name, row in items]


def top(probabilities: list[float]) -> int:
    return probabilities.index(max(probabilities))


def largest_difference(a: list[float], b: list[float]) -> float:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def alone(gpu: SimpleNamespace, rows: list[tuple[int, int, Any]]) -> list[list[float]]:
    """Each row decided on its own: the B = 1 graph of its bucket."""
    out = []
    for row in rows:
        probabilities, _, timing = gpu.batched.decide([row])
        assert timing["batch_rows"] == [1] and timing["passes"] == 1
        out.append(probabilities[0])
    return out


# ---------------------------------------------------------------- recording


def test_one_graph_per_batch_and_token_bucket_up_to_16384_tokens(gpu: SimpleNamespace) -> None:
    from jeff_runtime.engine.batched_path import PASS_SHAPES

    assert len(PASS_SHAPES) == 39
    assert set(gpu.batched.graphs) == set(PASS_SHAPES) == set(gpu.batched.plans)
    assert {b for b, n in PASS_SHAPES if n == 512} == {1, 2, 4, 8, 16, 32}
    assert {b for b, n in PASS_SHAPES if n == 1024} == {1, 2, 4, 8, 16}
    for (b, n), plan in gpu.batched.plans.items():
        assert plan.gdn in plan.gdn_ms and plan.gdn_ms[plan.gdn] == min(plan.gdn_ms.values()), (b, n)
        assert plan.attention_ms[plan.splits] == min(plan.attention_ms.values()), (b, n)


def test_a_bfloat16_base_records_cublas_in_every_graph_without_timing_it(gpu: SimpleNamespace) -> None:
    """Plan 2b-2 Task 5: on a bfloat16 base (no INT4 weights) the only product kernel is cuBLAS, so nothing is timed
    for it and every decision and prefix graph calls each fused group as before (Lin.__call__)."""
    from jeff_runtime.engine.product_choice import CUBLAS, ProductChoice

    assert all(lin.int4 is None for lin in gpu.fast.lins)
    for batched in (gpu.batched, gpu.engine.batched):
        assert batched.products == (CUBLAS,)
        assert all(plan.products == ProductChoice(CUBLAS, {}) for plan in batched.plans.values())
        assert set(batched.prefix_plans) == set(batched.prefix_graphs)
        assert all(choice == ProductChoice(CUBLAS, {}) for choice in batched.prefix_plans.values())


def test_every_graph_replays_with_padding_rows_only(gpu: SimpleNamespace) -> None:
    """Each of the 39 graphs, replayed with padding rows only (slot 0, the base, one token, one option), finishes and
    gives probability 1 to the only option. A graph left reading freed memory fails here: timing graphs destroyed
    between captures into the shared pool once made the (2, 64) graph hit an illegal memory access."""
    torch = gpu.torch
    batched = gpu.batched
    for (b, n), (graph, out) in batched.graphs.items():
        v = batched.inputs[(b, n)]
        for field in (v.slots, v.adapters, v.pos, v.last):
            field.fill_(0)
        v.counts.fill_(1)
        v.ids.fill_(gpu.fast.pad_id)
        graph.replay()
        torch.cuda.synchronize()
        assert torch.equal(out[:, 0], torch.ones(b, device="cuda")), (b, n)
        assert not out[:, 1:].any(), (b, n)


# ---------------------------------------------------------------- mixed passes against rows alone


@pytest.mark.parametrize("cut", [None, 32, 256])
def test_mixed_passes_answer_as_the_same_rows_alone(gpu: SimpleNamespace, cut: int | None) -> None:
    """64 real rows of nine adapters, each with its own prefix, in one call: with their own new tokens (passes per
    new-token bucket, of various sizes), and fitted to 32 and 256 new tokens (one pass of 64 rows, the largest pass of
    each bucket). Every row's top option equals the one it gets alone in the B = 1 graph, and no probability differs by
    more than MAX_PROBABILITY_DIFFERENCE."""
    items = prepared_rows(gpu, per_adapter=8, seed=5)[:64]
    if cut is not None:
        items = fitted(items, cut)
    with pinned(gpu, items) as rows:
        mixed, tokens, timing = gpu.batched.decide(rows)
        single = alone(gpu, rows)
    assert len(mixed) == len(rows) == 64
    if cut is not None:
        assert timing["passes"] == 1 and set(timing["batch_rows"]) == {64}
    assert set(timing["adapters_in_pass"]) <= set(range(1, 10)) and max(timing["adapters_in_pass"]) > 1
    worst = 0.0
    for i, (got, expected) in enumerate(zip(mixed, single, strict=True)):
        assert top(got) == top(expected), (i, items[i][0], got, expected)
        worst = max(worst, largest_difference(got, expected))
    print(f"\ncut {cut}: {timing['passes']} passes, rows per pass {sorted(set(timing['batch_rows']))}, buckets "
          f"{sorted(set(timing['bucket']))}; largest difference mixed vs alone {worst:.4f}")
    assert worst <= MAX_PROBABILITY_DIFFERENCE
    assert tokens == [gpu.fast.prefixes.length(slot) + len(row.new_ids) for _, slot, row in rows]


def test_padding_rows_change_nothing(gpu: SimpleNamespace) -> None:
    """Five real rows run in the 8-row graph, with three padding rows (slot 0, the base, one token, one option). Each
    row's probabilities are bit for bit those it gets in the same graph next to three other real rows instead of the
    padding (the graph computes every row on its own), and agree with the row alone in the B = 1 graph."""
    items = fitted(prepared_rows(gpu, per_adapter=1, seed=11), 64)[:8]
    with pinned(gpu, items) as rows:
        five, _, timing = gpu.batched.decide(rows[3:])
        eight, _, full = gpu.batched.decide(rows)
        single = alone(gpu, rows[3:])
    assert timing["batch_rows"] == [5] * 5 and timing["bucket"] == [64] * 5 and timing["passes"] == 1
    assert timing["adapters_in_pass"] == [len({index for index, _, _ in rows[3:]})] * 5
    assert full["batch_rows"] == [8] * 8
    assert five == eight[3:], "a row's answer depends on what else is in its pass"
    for got, expected in zip(five, single, strict=True):
        assert top(got) == top(expected)
        assert largest_difference(got, expected) <= MAX_PROBABILITY_DIFFERENCE


# ---------------------------------------------------------------- refusals


def test_rows_the_graphs_cannot_answer_as_asked_are_refused(gpu: SimpleNamespace) -> None:
    """A slot holding another adapter's prefix of the same text, an adapter row with no adapter, slot 0, and a row
    over the largest bucket or with no new tokens are refused before any pass runs."""
    from jeff_runtime.engine.fast_path import Row

    (name, row), = prepared_rows(gpu, per_adapter=1, seed=3)[:1]
    other = next(a for a in ADAPTERS if a != name)
    with pinned(gpu, [(name, row), (other, row)]) as rows:
        (index, slot, _), (other_index, other_slot, _) = rows
        with pytest.raises(ValueError, match="holds another prefix"):
            gpu.batched.decide([(index, other_slot, row)])
        with pytest.raises(ValueError, match="holds no prefix"):
            gpu.batched.decide([(index, 0, row)])
        with pytest.raises(ValueError, match="holds no loaded adapter"):
            gpu.batched.decide([(len(ADAPTERS) + 1, slot, row)])
        with pytest.raises(ValueError, match="0 new tokens"):
            gpu.batched.decide([(index, slot, Row(row.prefix_text, [], row.count))])
        with pytest.raises(ValueError, match="1 to 1024"):
            gpu.batched.decide([(index, slot, Row(row.prefix_text, [row.new_ids[0]] * 1025, row.count))])
        with pytest.raises(ValueError, match="answer options"):
            gpu.batched.decide([(index, slot, Row(row.prefix_text, row.new_ids, 256))])


# ---------------------------------------------------------------- batched prefix passes (Task 6)

def empty_pool(gpu: SimpleNamespace) -> None:
    """Drop every cached prefix (none is pinned between tests), so the next prefixes are all new."""
    for index in list(gpu.fast.adapter_index.values()):
        gpu.fast.forget(index)
    assert len(gpu.fast.prefixes) == 0


def prefix_keys(gpu: SimpleNamespace, items: list[tuple[str, Any]]) -> list[tuple[int, str]]:
    return [(gpu.fast.index(name), row.prefix_text) for name, row in items]


def test_one_prefix_graph_per_batch_bucket_up_to_the_measured_cap(gpu: SimpleNamespace) -> None:
    """A batched prefix graph for every (B, prefix bucket) with B up to PREFIX_PASS_ROWS[bucket], each timed after
    recording; recording them (padding rows only) wrote nothing to the zero page or to slot 0's table."""
    from jeff_runtime.engine.batched_path import BATCH_BUCKETS, PREFIX_PASS_ROWS
    from jeff_runtime.engine.fast_path import PREFIX_BUCKETS
    from jeff_runtime.engine.prefix_pool import ZERO_PAGE

    batched, pool = gpu.batched, gpu.fast.prefixes
    expected = {(b, nb) for nb in PREFIX_BUCKETS for b in BATCH_BUCKETS if b <= PREFIX_PASS_ROWS[nb]}
    assert set(batched.prefix_graphs) == set(batched.prefix_pass_ms) == expected
    assert all(ms > 0 for ms in batched.prefix_pass_ms.values())
    assert not pool.data[1].any()
    assert (pool.tables[0, :pool.fixed_pages] == ZERO_PAGE).all() and not pool.tables[0, pool.fixed_pages:].any()


def test_prefix_passes_cover_the_new_prefixes_at_the_least_measured_cost(gpu: SimpleNamespace) -> None:
    """For every bucket and up to three times its cap of new prefixes, the passes are recorded sizes that hold them
    all, cost no more than the smallest recorded size holding each run of `cap` prefixes, and one prefix runs alone."""
    from jeff_runtime.engine.batched_path import BATCH_BUCKETS, PREFIX_PASS_ROWS

    batched = gpu.batched
    for nb, cap in PREFIX_PASS_ROWS.items():
        for count in range(1, 3 * cap + 1):
            passes = batched.prefix_passes(nb, count)
            assert all((b, nb) in batched.prefix_graphs for b in passes) and sum(passes) >= count
            assert sum(passes) - max(passes) < count, (nb, count, passes)  # no pass of padding rows only
            rest = [next(b for b in BATCH_BUCKETS if b >= count % cap)] if count % cap else []
            naive = [cap] * (count // cap) + rest
            cost = sum(batched.prefix_pass_ms[(b, nb)] for b in passes)
            assert cost <= sum(batched.prefix_pass_ms[(b, nb)] for b in naive) + 1e-9, (nb, count, passes, naive)
        assert batched.prefix_passes(nb, 1) == [1]


def test_batched_prefix_passes_answer_as_prefixes_made_one_at_a_time(gpu: SimpleNamespace) -> None:
    """64 real rows of nine adapters, each with its own new prefix (a row sharing an earlier row's prefix is left
    out). Their prefixes made by batched prefix passes (ensure_prefixes of all 64) and made one at a time
    (ensure_prefixes of each alone: the B = 1 prefix graph, bit for bit the single-prefix pass of before 2b-1) give, row by row in the same B = 1 decision graph, the same top option, and no probability differs by more than
    MAX_PREFIX_BATCH_DIFFERENCE.

    Not bit for bit: cuBLAS chooses its bfloat16 matrix product kernels by the number of rows, and PyTorch's attention
    kernel by the batch, so a prefix computed beside others rounds differently from the same prefix alone (measured on
    datigator, 2026-10-24: the K = 3,584 down projection differs at every B > 1; the per-row product of strided
    batched GEMMs differs too). Running every product per row would keep the bits but cost most of the gain on short
    prefixes (64 products of 64 rows instead of one of 4,096). A pass of one prefix is bit for bit the single-prefix
    pass of before 2b-1 (test_fast_path_cuda.test_the_pool_prefix_pass_writes_the_same_bits_as_the_cache_before_it)."""
    torch = gpu.torch
    pool = gpu.fast.prefixes
    candidates = prepared_rows(gpu, per_adapter=9, seed=5)
    distinct: dict[tuple[int, str], tuple[str, Any]] = {}  # rows sharing a prefix: the first one only
    for item, key in zip(candidates, prefix_keys(gpu, candidates), strict=True):
        distinct.setdefault(key, item)
    keys = list(distinct)[:64]
    items = [distinct[key] for key in keys]
    assert len(keys) == 64
    empty_pool(gpu)
    with torch.inference_mode():
        with pinned(gpu, items) as rows:
            single = alone(gpu, rows)
        empty_pool(gpu)
        slots, timing = gpu.batched.ensure_prefixes(keys, gpu.engine.max_tokens)
        try:
            rows = [(index, slot, row) for (index, _), slot, (_, row) in zip(keys, slots, items, strict=True)]
            got = alone(gpu, rows)
            mixed = gpu.batched.decide(rows)[0]
        finally:
            pool.unpin(slots)
    assert timing["prefix_misses"] == 64 and timing["prefix_hits"] == 0
    assert timing["prefix_passes"] < 64, timing
    worst = 0.0
    for i, (x, y) in enumerate(zip(got, single, strict=True)):
        assert top(x) == top(y), (i, items[i][0], x, y)
        worst = max(worst, largest_difference(x, y))
    print(f"\n64 new prefixes in {timing['prefix_passes']} batched prefix passes ({timing['prefix_ms']:.1f} ms); "
          f"largest difference against prefixes made one at a time {worst:.4f}")
    assert worst <= MAX_PREFIX_BATCH_DIFFERENCE
    assert all(top(x) == top(y) for x, y in zip(mixed, got, strict=True))
    empty_pool(gpu)


def test_rows_sharing_a_new_prefix_make_one_slot(gpu: SimpleNamespace) -> None:
    """Two rows with the same new (adapter, prefix text) get one slot, processed once and pinned once per row; the
    same text under another adapter is another prefix. Asked again, every row is a hit and nothing runs."""
    pool = gpu.fast.prefixes
    (name, row), (other_name, other_row) = prepared_rows(gpu, per_adapter=1, seed=29)[:2]
    third = next(a for a in ADAPTERS if a not in (name, other_name))
    index, other_index, third_index = gpu.fast.index(name), gpu.fast.index(other_name), gpu.fast.index(third)
    keys = [(index, row.prefix_text), (other_index, other_row.prefix_text), (index, row.prefix_text),
            (third_index, row.prefix_text)]
    empty_pool(gpu)
    slots, timing = gpu.batched.ensure_prefixes(keys, gpu.engine.max_tokens)
    try:
        assert slots[0] == slots[2] and len(set(slots)) == 3
        assert timing["prefix_misses"] == 3 and timing["prefix_hits"] == 0 and timing["prefix_shared"] == 1
        assert len(pool) == 3
        assert pool._pinned == {slots[0]: 2, slots[1]: 1, slots[3]: 1}
        assert [pool.key(slot) for slot in slots] == keys
    finally:
        pool.unpin(slots)
    assert not pool._pinned
    again, timing = gpu.batched.ensure_prefixes(keys, gpu.engine.max_tokens)
    pool.unpin(again)
    assert again == slots
    assert timing == {"prefix_ms": 0.0, "prefix_misses": 0, "prefix_hits": 4, "prefix_shared": 0,
                      "prefix_passes": 0}
    empty_pool(gpu)


def test_a_failed_prefix_pass_leaves_no_pin_and_no_unwritten_prefix(gpu: SimpleNamespace,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    """When a prefix pass fails, ensure_prefixes undoes every pin it made and releases every new slot not yet
    written; prefixes written by earlier passes of the call stay, and so does the prefix that was already cached."""
    from jeff_runtime.engine.fast_path import PREFIX_BUCKETS

    pool, batched = gpu.fast.prefixes, gpu.batched
    items = prepared_rows(gpu, per_adapter=2, seed=31)
    keys = prefix_keys(gpu, items)
    tok = gpu.fast.tokenizer

    def bucket(key: tuple[int, str]) -> int:
        return next(nb for nb in PREFIX_BUCKETS if nb >= len(tok(key[1], add_special_tokens=False)["input_ids"]))

    buckets = sorted({bucket(key) for key in keys})
    assert len(buckets) >= 2
    first = [key for key in keys if bucket(key) == buckets[0]][:1]  # one pass, in the smallest bucket
    later = [key for key in keys if bucket(key) == buckets[1]][:1]
    cached = [key for key in keys if key not in first + later][:1]
    empty_pool(gpu)
    slots, _ = batched.ensure_prefixes(cached, gpu.engine.max_tokens)
    pool.unpin(slots)
    real = batched._prefix_pass
    calls = []

    def failing(b: int, nb: int, part: list[tuple[int, int, list[int]]]) -> float:
        calls.append(nb)
        if len(calls) == 2:
            raise RuntimeError("simulated failure of the second prefix pass")
        return real(b, nb, part)

    monkeypatch.setattr(batched, "_prefix_pass", failing)
    with pytest.raises(RuntimeError, match="simulated failure"):
        batched.ensure_prefixes(cached + later + first, gpu.engine.max_tokens)
    assert calls == [buckets[0], buckets[1]]
    assert not pool._pinned
    assert pool.lookup(*first[0]) is not None and pool.lookup(*cached[0]) is not None
    assert pool.lookup(*later[0]) is None
    assert len(pool) == 2
    empty_pool(gpu)


def test_prefixes_the_fast_path_cannot_take_are_refused_before_any_change(gpu: SimpleNamespace) -> None:
    """A prefix at or over the request token limit, or over the longest prefix, and an adapter row with no adapter,
    are refused (ValueError) before the pool changes: nothing reserved, nothing pinned."""
    pool = gpu.fast.prefixes
    (name, row), = prepared_rows(gpu, per_adapter=1, seed=37)[:1]
    key = (gpu.fast.index(name), row.prefix_text)
    empty_pool(gpu)
    with pytest.raises(ValueError, match="exceeds the 10-token limit"):
        gpu.batched.ensure_prefixes([key], 10)
    with pytest.raises(ValueError, match="holds at most 8192"):
        gpu.batched.ensure_prefixes([key, (key[0], "word " * 9000)], 10**6)
    with pytest.raises(ValueError, match="holds no loaded adapter"):
        gpu.batched.ensure_prefixes([key, (len(ADAPTERS) + 1, row.prefix_text)], gpu.engine.max_tokens)
    assert len(pool) == 0 and not pool._pinned


# ---------------------------------------------------------------- the choose() stream race under GPU load (Task 0)

# A second process's whole job: two 8192 x 8192 float32 matrices (about 0.5 GB) multiplied back to back, forever,
# with no synchronisation between launches, so its kernels queue up on the GPU the way another resident engine's
# would. Passed to a fresh Python interpreter with -c, so nothing beyond this test file is added to the repository.
BUSY_GPU_SCRIPT = """
import torch
torch.cuda.set_device(0)
size = 8192
a = torch.randn(size, size, device="cuda", dtype=torch.float32)
b = torch.randn(size, size, device="cuda", dtype=torch.float32)
while True:
    a @ b
"""


@contextmanager
def busy_gpu() -> Iterator[None]:
    """A second process matrix-multiplying on the GPU (BUSY_GPU_SCRIPT), started here and stopped here: its PID is
    terminated (SIGTERM, then SIGKILL if it has not exited) when the block ends, and no other process is touched."""
    proc = subprocess.Popen([sys.executable, "-c", BUSY_GPU_SCRIPT])
    try:
        time.sleep(2)  # let it reach a steady loop of queued matmuls before the builds below start
        yield
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


def test_choose_builds_ten_batched_paths_while_the_gpu_is_busy(gpu: SimpleNamespace) -> None:
    """Ten BatchedPath builds on the same FastPath, one after another, while a second process keeps the GPU busy
    (busy_gpu): the whole of record() (choose()'s per-shape timing, the decision-graph loop, the prefix-graph loop),
    not just choose(). Each of those three places builds some input (choose's q, k, v, g, beta and the block tables;
    the decision loop's v.slots/adapters/pos/last/counts/ids; the prefix loop's padding) on the current CUDA stream
    and then reads it from a side stream (_graph_ms's capture/replay, or the loops' own `with torch.cuda.stream`
    warm-up and capture); without a wait between the two, a busy GPU can let the side stream read before the current
    stream has written, failing the build with an illegal memory access (Risk 5 of the 2b-2 design: main 4249546
    crashed engine start twice this way, 2b2-prototypes/logs/eval_rtn_crash1.log and ..._crash2.log; the first-round
    fix only covered choose(), and review found the same unguarded pattern still in record()'s two loops).

    Every one of the ten builds must succeed, and (not just "not crash") every one of its 39 graphs must answer
    exactly as the same real rows do in its own B = 1 graph: one real row per adapter (nine), fitted to each
    new-token bucket and pinned once, replayed as one pass per (B, N) shape (decide() on exactly B of them selects
    that shape's graph, see _pass's bucket choice), compared against each distinct adapter's row decided alone."""
    from jeff_runtime.engine.batched_path import PASS_SHAPES, BatchedPath, max_rows

    torch = gpu.torch
    ns = sorted({n for _, n in PASS_SHAPES})
    base = prepared_rows(gpu, per_adapter=1, seed=101)  # one real row per adapter (nine), distinct prefixes
    by_n_items = {n: fitted([base[i % len(base)] for i in range(max_rows(n))], n) for n in ns}
    flat_items = [item for n in ns for item in by_n_items[n]]
    with pinned(gpu, flat_items) as flat_rows:
        rows_by_n: dict[int, list[tuple[int, int, Any]]] = {}
        start = 0
        for n in ns:
            count = len(by_n_items[n])
            rows_by_n[n] = flat_rows[start:start + count]
            start += count
        with busy_gpu():
            for build in range(10):
                batched = BatchedPath(gpu.fast)
                assert set(batched.graphs) == set(PASS_SHAPES) and set(batched.plans) == set(PASS_SHAPES), build
                for b, n in PASS_SHAPES:
                    part = rows_by_n[n][:b]
                    mixed, _, timing = batched.decide(part)
                    assert timing["batch_rows"] == [b] * len(part) and timing["bucket"] == [n] * len(part), (
                        build, b, n, timing)
                    for row, got in zip(part[:len(base)], mixed[:len(base)], strict=False):
                        single, _, single_timing = batched.decide([row])
                        assert single_timing["batch_rows"] == [1], (build, b, n, row)
                        assert top(got) == top(single[0]), (build, b, n, row)
                        assert largest_difference(got, single[0]) <= MAX_PROBABILITY_DIFFERENCE, (build, b, n, row)
                del batched
                gc.collect()
                torch.cuda.synchronize()
