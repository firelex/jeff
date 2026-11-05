"""The v1.3 INT4 base on the fast backend (plan 2b-2, Task 3): every fused group of matrices (fast_path.Lin) holds its
integers and scales in the INT4 kernel's layout (design Section 3.1) beside its dequantised bfloat16 copy, which
cuBLAS multiplies by; since plan 2b-2 Task 5 each graph chooses between cuBLAS and the INT4 kernel (Task 4).

- The packed integers and scales are the folder's, exactly; the padding columns are zero weights.
- GPU memory: an INT4 engine takes a bfloat16 engine's memory plus the packed copy (about 0.27 GB), within 0.3 GB.
- Agreement: the INT4 fast path makes the INT4 standard path's top choice on at least 99% of 1,000 test rows per
  adapter (all of them when there are fewer), with the rows served as the engine server serves them: one row per
  group, shuffled across adapters, 64 groups per predict_many call. Since plan 2b-2 Task 5 each graph chooses its
  product kernel (cuBLAS on the dequantised copy, or the INT4 kernel) by timing; the agreement is checked with the
  graphs as chosen, then with every graph forced to the INT4 kernel and to cuBLAS, so it holds whichever is chosen.
  The tie rule is the 2b-1 agreement gate's (test_agreement_cuda.py): a row whose top two standard-path options are
  exactly equal agrees when the fast path picks either. The plan asked for 200 rows; on 200, one near tie moves an
  adapter by 0.5 points, and emotion's 200 agreed on 197 (three near ties, standard-path margins 0.02 to 0.06) while
  its 2,000 agreed on 99.65% (the bfloat16 fast path: 99.55% on the same 2,000). The full gate (Task 7) also covers
  the INT4 kernel.

Folders on datigator: the v1.3 base, its INT4 folder ~/jeff-2b-scratch/2b2/int4-base/ll-v12-final-int4-gptq, the nine
v1.3 adapters and their test sets. Results go to results/step2b2/int4-fast-agreement.json (as chosen) and
int4-fast-agreement-int4.json / -cublas.json (forced).

Run on datigator: .venv/bin/python -m pytest -m cuda runtime/python/tests/test_int4_fast_path_cuda.py -s"""

from __future__ import annotations

import gc
import glob
import json
import random
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.cuda

REPO = Path(__file__).resolve().parents[3]
BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
INT4_BASE = Path("~/jeff-2b-scratch/2b2/int4-base/ll-v12-final-int4-gptq").expanduser()
TEST_SETS = Path("/mnt/d/jeff-models/v1.3/casdgx01/testsets")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
ADAPTERS = ("emotion", "ground", "guard", "legal-clauses", "nav", "spam", "support-intents", "tools", "triage")
RESULTS = REPO / "results/step2b2/int4-fast-agreement.json"
ROWS_PER_ADAPTER = 1000
SEED = 7
GATE = 0.99
MAX_BATCH_ROWS = 64
PREFIX_POOL_MB = 2048
GB = 1e9
PACKED_GB = 0.27  # design Section 4.1: 248.8 MB of integers and 15.5 MB of scales, plus the padding columns
MEMORY_TOLERANCE_GB = 0.3


def folders() -> dict[str, Path]:
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    missing = [str(path) for path in (BASE, INT4_BASE, TEST_SETS) if not path.exists()]
    if missing:
        pytest.skip(f"needs datigator's v1.3 base, its INT4 folder and the test sets; missing here: {missing}")
    found = {}
    for name in ADAPTERS:
        matches = glob.glob(CHECKPOINTS.format(name=name))
        assert len(matches) == 1, f"expected one v1.3 {name} checkpoint, found {matches}"
        found[name] = Path(matches[0])
    return found


def fast_engine(base: Path, adapters: dict[str, Path]) -> Any:
    from jeff_runtime import LocalEngine
    return LocalEngine(base, adapters, backend="fast", prepare_workers=1, prefix_pool_mb=PREFIX_POOL_MB,
                       max_adapters=len(adapters))


def top(probabilities: list[float]) -> int:
    return probabilities.index(max(probabilities))


def test_an_int4_engine_takes_a_bfloat16_engines_gpu_memory_plus_the_packed_weights() -> None:
    """Each engine is built alone and measured as the allocated GPU memory its construction added (weights, fused
    copies, adapter pools, prefix pool, recorded graphs and their workspaces), then freed."""
    import torch

    adapters = folders()

    def added(base: Path) -> tuple[int, int]:
        gc.collect()
        torch.cuda.empty_cache()
        before = torch.cuda.memory_allocated()
        engine = fast_engine(base, adapters)
        torch.cuda.synchronize()
        grown = torch.cuda.memory_allocated() - before
        packed = engine.fast.int4_bytes
        del engine
        gc.collect()
        torch.cuda.empty_cache()
        return grown, packed

    bf16, bf16_packed = added(BASE)
    int4, int4_packed = added(INT4_BASE)
    print(f"\nbfloat16 engine {bf16 / GB:.3f} GB, INT4 engine {int4 / GB:.3f} GB (+{(int4 - bf16) / GB:.3f} GB), "
          f"packed INT4 {int4_packed / GB:.4f} GB, peak allocated {torch.cuda.max_memory_allocated() / GB:.2f} GB")
    assert bf16_packed == 0
    assert 0.26 * GB < int4_packed < PACKED_GB * GB
    assert abs(int4 - (bf16 + PACKED_GB * GB)) <= MEMORY_TOLERANCE_GB * GB


@pytest.fixture(scope="module")
def int4_engine() -> Iterator[Any]:
    engine = fast_engine(INT4_BASE, folders())
    yield engine
    del engine
    gc.collect()


def test_the_packed_weights_round_trip_to_the_folders_integers(int4_engine: Any) -> None:
    import torch
    from safetensors.torch import load_file

    from jeff_runtime.engine.int4 import KERNEL_COLUMNS, dequantize, int4_matrices, unpack_int4, unpack_kernel

    stored = load_file(str(INT4_BASE / "int4.safetensors"))
    lins = int4_engine.fast.lins
    names = [name for lin in lins for name in lin.names]
    assert len(names) == len(set(names)) == 186
    assert {f"{name}.qweight" for name in names} | {f"{name}.scales" for name in names} == set(stored)
    total = 0
    for lin in lins:
        packed = lin.int4
        columns = -(-lin.out // KERNEL_COLUMNS) * KERNEL_COLUMNS
        assert packed.out == lin.out
        assert packed.words.dtype == torch.int32 and tuple(packed.words.shape) == (lin.k // 8, columns)
        assert packed.scales.dtype == torch.bfloat16 and tuple(packed.scales.shape) == (lin.k // 64, columns)
        assert packed.words.is_contiguous() and packed.scales.is_contiguous() and packed.words.is_cuda
        q, scales = unpack_kernel(packed)
        expected_q = torch.cat([unpack_int4(stored[f"{name}.qweight"]) for name in lin.names]).cuda()
        expected_scales = torch.cat([stored[f"{name}.scales"] for name in lin.names]).cuda()
        assert torch.equal(q, expected_q), lin.names
        assert torch.equal(scales, expected_scales), lin.names
        # the padding columns are zero weights: every nibble q + 8 = 8, every scale 1
        assert bool((packed.words[:, lin.out:] == 0x88888888 - (1 << 32)).all()), lin.names
        assert bool((packed.scales[:, lin.out:] == 1).all()), lin.names
        # cuBLAS's copy is the standard path's weights, bf16(q * s)
        assert torch.equal(lin.W, dequantize(expected_q, expected_scales)), lin.names
        total += packed.nbytes()
    assert total == int4_engine.fast.int4_bytes
    assert len(int4_matrices(int4_engine.model.backbone)) == 186


def test_each_graph_records_the_faster_product_kernel_with_both_timings(int4_engine: Any) -> None:
    """Plan 2b-2 Task 5: on an INT4 base every decision graph and every prefix graph timed both product kernels
    (cuBLAS on the dequantised copy, the INT4 kernel) while recording and records the faster, cuBLAS on a tie."""
    from jeff_runtime.engine.product_choice import CUBLAS, INT4, PRODUCTS

    batched = int4_engine.batched
    assert batched.products == PRODUCTS
    reserved = batched.timing_reserved_gb
    print(f"\nGPU memory reserved around the timing (GB): {reserved}", end="")
    assert reserved["after_release"] <= reserved["after_timing"], reserved  # the timing's memory went back to the GPU
    choices = {f"decision {b}x{n}": plan.products for (b, n), plan in batched.plans.items()}
    choices.update({f"prefix {b}x{nb}": choice for (b, nb), choice in batched.prefix_plans.items()})
    assert len(choices) == len(batched.graphs) + len(batched.prefix_graphs)
    for shape, choice in choices.items():
        assert set(choice.ms) == {CUBLAS, INT4} and all(ms > 0 for ms in choice.ms.values()), (shape, choice)
        faster = INT4 if choice.ms[INT4] < choice.ms[CUBLAS] else CUBLAS
        assert choice.kernel == faster, (shape, choice)
        print(f"\n{shape}: {choice.kernel} (cuBLAS {choice.ms[CUBLAS]:.3f} ms, INT4 {choice.ms[INT4]:.3f} ms)", end="")
    print()


@pytest.fixture(scope="module")
def agreement_set(int4_engine: Any) -> dict[str, Any]:
    """The agreement rows, served as the engine server serves them, and the INT4 standard path's probabilities on
    them, computed once for every product kernel variant: up to ROWS_PER_ADAPTER test rows per adapter (seeded), rows
    over the token limit counted and left out, one row per group, the groups shuffled across adapters."""
    from jeff.errors import InputTooLong

    engine = int4_engine
    assert engine.base_format["format"] == "int4-g64"
    rows: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    over_limit: dict[str, int] = {}
    groups: list[tuple[str, int, Any]] = []
    for name in ADAPTERS:
        lines = (TEST_SETS / name / "test.jsonl").read_text().splitlines()
        numbers = sorted(random.Random(f"{SEED}:{name}").sample(range(len(lines)), min(ROWS_PER_ADAPTER, len(lines))))
        rows[name], over_limit[name] = [], 0
        for number in numbers:
            item = json.loads(lines[number])
            row = {"state": item["state"], "question": item["question"]}
            try:
                prepared = engine.prepare(name, [row])
            except InputTooLong:
                over_limit[name] += 1
                continue
            groups.append((name, len(rows[name]), prepared))
            rows[name].append((number, row))
    random.Random(SEED).shuffle(groups)
    standard: dict[str, list[list[float]]] = {}
    for name, items in rows.items():
        engine.adapter_set.use(name)
        standard[name] = engine._standard([row for _, row in items], engine._layout(name))[0]
    return {"rows": rows, "over_limit": over_limit, "groups": groups, "standard": standard}


def agreement(engine: Any, agreement_set: dict[str, Any], results: Path, product: str) -> None:
    """Serve the agreement rows on the engine's batched path as it is (64 groups per predict_many call), compare each
    with the INT4 standard path (the 2b-1 gate's tie rule: a row whose top two standard-path options are exactly equal
    agrees when the fast path picks either), write `results`, and require GATE per adapter."""
    rows, groups, standard = agreement_set["rows"], agreement_set["groups"], agreement_set["standard"]
    answers: dict[tuple[str, int], Any] = {}
    started = time.perf_counter()
    for start in range(0, len(groups), MAX_BATCH_ROWS):
        chunk = groups[start:start + MAX_BATCH_ROWS]
        served = engine.predict_many([(name, prepared) for name, _, prepared in chunk])
        for (name, i, _), result in zip(chunk, served, strict=True):
            assert not isinstance(result, Exception), (name, i, result)
            answers[(name, i)] = result
    served_seconds = time.perf_counter() - started

    per_adapter: dict[str, dict[str, Any]] = {}
    for name, items in rows.items():
        agree = raw = routed = compared = 0
        worst = 0.0
        disagreements = []
        for i, (number, _) in enumerate(items):
            result = answers[(name, i)]
            assert result.timing["format"] == "int4-g64"
            if result.timing["path"] != "fast":
                routed += 1
                continue
            got, expected = result.probabilities[0], standard[name][i]
            compared += 1
            worst = max(worst, max(abs(a - b) for a, b in zip(got, expected, strict=True)))
            tied = [k for k, p in enumerate(expected) if p == max(expected)]
            if top(got) == top(expected):
                agree += 1
                raw += 1
            elif len(tied) > 1 and top(got) in tied:
                agree += 1
            else:
                ranked = sorted(expected, reverse=True)
                disagreements.append({"line": number, "fast_top": top(got), "standard_top": top(expected),
                                      "standard_margin": round(ranked[0] - ranked[1], 4)})
        per_adapter[name] = {"rows": len(items), "over_limit": agreement_set["over_limit"][name],
                             "routed_to_standard": routed, "compared": compared, "agreeing": agree,
                             "agreement": round(agree / compared, 4), "raw_agreeing": raw,
                             "largest_probability_difference": round(worst, 4), "disagreements": disagreements}
    batched = engine.batched
    kernels = {"decision_graphs": {f"{b}x{n}": plan.products.kernel for (b, n), plan in batched.plans.items()},
               "prefix_graphs": {f"{b}x{nb}": choice.kernel for (b, nb), choice in batched.prefix_plans.items()}}
    results.parent.mkdir(parents=True, exist_ok=True)
    results.write_text(json.dumps({
        "test": f"runtime/python/tests/test_int4_fast_path_cuda.py ({product})",
        "date": time.strftime("%Y-%m-%d %H:%M"), "base": str(INT4_BASE), "format_id": engine.base_format["format_id"],
        "product": product, "product_kernels": kernels,
        "rows_per_adapter": ROWS_PER_ADAPTER, "seed": SEED, "gate": GATE, "max_batch_rows": MAX_BATCH_ROWS,
        "served_seconds": round(served_seconds, 2), "adapters": per_adapter}, indent=1) + "\n")
    decision, prefix = kernels["decision_graphs"].values(), kernels["prefix_graphs"].values()
    print(f"\n{product}: INT4 kernel in {sum(k == 'int4' for k in decision)} of {len(decision)} decision graphs and "
          f"{sum(k == 'int4' for k in prefix)} of {len(prefix)} prefix graphs", end="")
    for name, record in per_adapter.items():
        print(f"\n{name}: {record['agreeing']}/{record['compared']} ({record['agreement']:.2%}), largest difference "
              f"{record['largest_probability_difference']}, {record['routed_to_standard']} routed to the standard "
              f"path, {record['over_limit']} over the token limit", end="")
    print()
    failures = [f"{name}: {r['agreeing']}/{r['compared']}" for name, r in per_adapter.items()
                if r["agreeing"] < GATE * r["compared"]]
    assert not failures, failures
    assert all(r["compared"] >= 0.9 * r["rows"] for r in per_adapter.values()), per_adapter


def test_the_int4_fast_path_agrees_with_the_int4_standard_path(int4_engine: Any, agreement_set: dict[str, Any]) -> None:
    """The engine as served: each graph's product kernel chosen by timing (plan 2b-2 Task 5)."""
    agreement(int4_engine, agreement_set, RESULTS, "each graph's product kernel chosen by timing at recording")


@pytest.mark.parametrize("kernel", ["int4", "cublas"])
def test_the_int4_fast_path_agrees_with_the_int4_standard_path_whichever_product_kernel(
        int4_engine: Any, agreement_set: dict[str, Any], kernel: str) -> None:
    """Every graph forced to one product kernel (BatchedPath(products=(kernel,))), so the agreement holds whichever
    kernel the timing picks for any graph: the INT4 kernel everywhere, then cuBLAS on the dequantised copy everywhere.
    The cached prefixes are dropped before and after, so no prefix made by another variant's prefix graphs is read."""
    from jeff_runtime.engine.batched_path import BatchedPath

    engine = int4_engine
    fast = engine.fast

    def forget_prefixes() -> None:
        for index in fast.adapter_index.values():
            fast.forget(index)

    served = engine.batched
    forget_prefixes()
    engine.batched = BatchedPath(fast, products=(kernel,))
    try:
        assert {plan.products.kernel for plan in engine.batched.plans.values()} == {kernel}
        assert {choice.kernel for choice in engine.batched.prefix_plans.values()} == {kernel}
        agreement(engine, agreement_set, RESULTS.with_name(f"int4-fast-agreement-{kernel}.json"),
                  f"{kernel} in every graph")
    finally:
        engine.batched = served
        forget_prefixes()
