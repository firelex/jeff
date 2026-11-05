"""Which kernel computes the fused matrix products in each recorded graph of the batched fast path (plan 2b-2 Task 5;
step 2b-2 design, Section 3.3). Kept apart from batched_path.py, which needs Triton and a GPU to import, so the rule
can be checked anywhere.

The candidates:
- "cublas": cuBLAS on the fused group's bfloat16 matrix W, then fast_path.lora_add (Lin.__call__). On a bfloat16
  base this is the only product. On an INT4 base W is the dequantised copy bf16(q * s).
- "int4": int4_kernels.int4_product, the deterministic INT4 kernel reading the packed integers. Only an INT4 base has
  them (fast_path.attach_int4 gives them to every fused group of an INT4 base, and to none of a bfloat16 base).

Both compute the same weights bf16(q * s), so on an INT4 base the choice changes answers by rounding only. Each graph
takes the faster on this GPU, timed while recording (BatchedPath.choose and choose_prefix), as the Gated DeltaNet
kernel and the attention split are. A bfloat16 base has one candidate: nothing is timed and every graph runs exactly
what it ran before Task 5."""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

CUBLAS = "cublas"
INT4 = "int4"
# In this order: on a tie the first wins, so cuBLAS (the standard path's own product on the same weights)
PRODUCTS = (CUBLAS, INT4)
# Each candidate is timed this many times, the candidates alternating (cuBLAS, INT4, cuBLAS, INT4, ...), and its
# median taken, so drift of the GPU's clocks or caches during the timing falls on both alike
PAIRS = 3


@dataclass(frozen=True)
class ProductChoice:
    """The product kernel recorded into one graph, and the timings it was chosen by (ms per graph of the pass's fused
    products, GPU only; empty when there was a single candidate and nothing was timed)."""
    kernel: str
    ms: dict[str, float]


def product_candidates(int4_groups: Mapping[str, bool], requested: Sequence[str] | None = None) -> tuple[str, ...]:
    """The product kernels a BatchedPath may record, in PRODUCTS order.

    int4_groups: for every fused group of the fast path (named by its matrices), whether it holds INT4 weights. All of
    them: an INT4 base, both kernels; none: a bfloat16 base, cuBLAS only. Some but not all is a broken fast path
    (ValueError naming the groups without them).

    requested: the kernels to choose among (benchmarks and tests force one), None for every kernel the base supports.
    ValueError for an unknown kernel, a kernel the base cannot run, a repeated one, or none."""
    if not int4_groups:
        raise ValueError("The fast path has no fused groups of matrices")
    without = [name for name, has in int4_groups.items() if not has]
    if without and len(without) < len(int4_groups):
        raise ValueError(f"{len(without)} of {len(int4_groups)} fused groups hold no INT4 weights (first: "
                         f"{without[0]}); an INT4 base gives them to every group (fast_path.attach_int4)")
    supported = (CUBLAS,) if without else PRODUCTS
    if requested is None:
        return supported
    unknown = [kernel for kernel in requested if kernel not in PRODUCTS]
    if unknown:
        raise ValueError(f"Unknown product kernel(s) {unknown}; expected some of {PRODUCTS}")
    if not requested or len(set(requested)) != len(requested):
        raise ValueError(f"Give each product kernel to choose among once, at least one; got {list(requested)}")
    unsupported = [kernel for kernel in requested if kernel not in supported]
    if unsupported:
        raise ValueError(f"Product kernel(s) {unsupported} need an INT4 base; this fast path's base is bfloat16 "
                         f"(it supports {list(supported)})")
    return tuple(kernel for kernel in PRODUCTS if kernel in requested)


def choose_product(candidates: Sequence[str], time_ms: Callable[[str], float], pairs: int = PAIRS) -> ProductChoice:
    """The fastest of `candidates` by time_ms(kernel), with every candidate's timing: `pairs` rounds, each timing every
    candidate once in order, and each candidate's median over its rounds. The first of equals wins (candidates in
    PRODUCTS order: cuBLAS on a tie). A single candidate is returned without timing."""
    if not candidates:
        raise ValueError("No product kernel to choose among")
    if pairs < 1:
        raise ValueError(f"pairs={pairs}: time each candidate at least once")
    if len(candidates) == 1:
        return ProductChoice(candidates[0], {})
    samples: dict[str, list[float]] = {kernel: [] for kernel in candidates}
    for _ in range(pairs):
        for kernel in candidates:
            samples[kernel].append(time_ms(kernel))
    ms = {kernel: round(statistics.median(values), 4) for kernel, values in samples.items()}
    return ProductChoice(min(candidates, key=ms.__getitem__), ms)
