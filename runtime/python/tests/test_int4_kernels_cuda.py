"""The fast path's INT4 kernel (engine/int4_kernels.py, plan 2b-2 Task 4) against what it replaces: cuBLAS on the
dequantised bfloat16 matrix W = bf16(q * s) plus fast_path.lora_add, which is Lin.__call__ on an INT4 base.

Every fused group of the Qwen3.5-0.8B layout (random integers and scales of realistic size), every pass the batched
path records (decision passes B x N and batched prefix passes, from batched_path's own lists) plus one pass whose
token count is not a multiple of the tiles (48 tokens in rows of 16: the tail masks), rows with mixed adapters (rows
cycle through adapters 1, 2, 0 (the base) and 3 of a stacked pool, so neighbouring row tiles read different pool rows;
a lone row reads adapter 1).

The kernel is judged against exact arithmetic, not against cuBLAS (controller's ruling, 2026-10-26, on the first GPU
run): cuBLAS is not a reference with a sequential summation order. On this GPU it splits the input dimension (split-K,
`cublasLt::splitKreduce_kernel`) even at batch-1 passes of the groups with 1,024 outputs, and with PyTorch's default
`torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction = True` it adds those partial products in
bfloat16. Measured: about 40% of outputs one step off the kernel at the batch-1 shapes, cuBLAS's mean error against
exact about 40% above the kernel's, and cuBLAS up to 0.004 from exact where the kernel was within 1e-5 (gdn_proj,
1,024 tokens). So:
- Exact: float64 x @ bf16(q * s)^T plus the float64 LoRA term, from the same bfloat16 weights and LoRA factors.
- At every batch-1 shape jeff-asic tuned (32, 64 and 128 tokens; int4_tiles.TUNED, the kernel forced to split 1) and
  at every pass the batched path records (int4_tiles' own tiles), each output must lie within the forward error bound
  of the arithmetic it performs (exact_and_bounds derives both): an uncut product (split 1) rounds twice by design,
  as lora_add does (the base product to bfloat16, then the LoRA update added and rounded), so its bound is half a
  step of the rounded base, half a step of the value finally rounded (taken at an upper bound of it, which can lie
  one binade above exact), a float32 slack and the bfloat16 rounding of the LoRA first factor; a cut product rounds once, so its bound is one step of exact plus the float32 slack (controller's rulings,
  2026-10-26). The kernel's mean absolute error against exact must be no larger than cuBLAS + lora_add's.
- cuBLAS + lora_add's own numbers against exact (and against the uncut bound) are written beside the kernel's, as a
  report, not asserted:
  results/step2b2/task4/int4-against-exact-batch1.json and int4-against-exact-passes.json.
- Independent of cuBLAS: the LoRA first factor the kernel's launch leaves in ws.QP equals lora_add's bit for bit on
  every pass (same tiles, split and warps; per-tile adapter index).
- Two runs give the same bits, and so does a CUDA graph replay (a cut pass and an uncut one).

Run on datigator (needs the GPU to itself apart from the resident servers):
.venv/bin/python -m pytest -m cuda runtime/python/tests/test_int4_kernels_cuda.py"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import torch

pytestmark = pytest.mark.cuda

REPO = Path(__file__).resolve().parents[3]

# label: (matrix names, their outputs, inputs); the names make Lin give each group its fast-path LoRA rank
FUSED = {"gdn_proj": (["linear_attn.in_proj_qkv", "linear_attn.in_proj_z", "linear_attn.in_proj_b",
                       "linear_attn.in_proj_a"], [6144, 2048, 16, 16], 1024),
         "gdn_out": (["linear_attn.out_proj"], [1024], 2048),
         "attn_proj": (["self_attn.q_proj", "self_attn.k_proj", "self_attn.v_proj"], [4096, 512, 512], 1024),
         "attn_out": (["self_attn.o_proj"], [1024], 2048),
         "mlp_in": (["mlp.gate_proj", "mlp.up_proj"], [3584, 3584], 1024),
         "mlp_down": (["mlp.down_proj"], [1024], 3584)}
ADAPTER_CYCLE = (1, 2, 0, 3)  # pool rows of consecutive request rows; row 0 is the base (zero LoRA matrices)
POOL_ROWS = 4
TAIL_PASS = (48, 16)  # 48 tokens in rows of 16: not a multiple of the base product's 64-token tiles


@pytest.fixture(scope="module")
def gpu() -> Iterator[SimpleNamespace]:
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    from jeff_runtime.engine.batched_path import BATCH_BUCKETS, PASS_SHAPES, PREFIX_PASS_ROWS
    from jeff_runtime.engine.fast_path import DT, PREFIX_BUCKETS, Lin
    from jeff_runtime.engine.int4 import dequantize, pack_kernel

    torch.manual_seed(0)
    lins: dict[str, Any] = {}
    for label, (names, sizes, inputs) in FUSED.items():
        q = torch.randint(-8, 8, (sum(sizes), inputs), device="cuda", dtype=torch.int8)
        scales = (torch.rand(sum(sizes), inputs // 64, device="cuda") * 0.008 + 0.001).to(torch.bfloat16)
        w = dequantize(q, scales)
        lin = Lin([f"layers.0.{name}" for name in names], list(w.split(sizes)))
        pool = torch.zeros(POOL_ROWS, lin.numel(), device="cuda", dtype=DT)
        pool[1:] = (torch.randn(POOL_ROWS - 1, lin.numel(), device="cuda") * 0.02).to(DT)
        lin.bind(pool, 0)
        lin.int4 = pack_kernel(q, scales)
        lins[label] = lin
    prefix_shapes = [(b, nb) for nb in PREFIX_BUCKETS for b in BATCH_BUCKETS if b <= PREFIX_PASS_ROWS[nb]]
    passes = sorted({(b * n, n) for b, n in [*PASS_SHAPES, *prefix_shapes]} | {TAIL_PASS})  # (tokens, row length)
    yield SimpleNamespace(lins=lins, passes=passes, shapes={(lin.k, lin.out) for lin in lins.values()})
    torch.cuda.empty_cache()


def pass_inputs(gpu: SimpleNamespace, lin: Any, tokens: int, row_len: int) -> tuple[Any, torch.Tensor, torch.Tensor]:
    from jeff_runtime.engine.fast_path import DT, Workspace

    ws = Workspace(tokens, row_len, gpu.shapes)
    rows = tokens // row_len
    adp = torch.tensor([ADAPTER_CYCLE[r % len(ADAPTER_CYCLE)] for r in range(rows)], device="cuda", dtype=torch.int32)
    gen = torch.Generator(device="cuda").manual_seed(tokens * 7919 + row_len * 31 + lin.k)
    x = torch.randn(tokens, lin.k, device="cuda", generator=gen).to(DT)
    return ws, adp, x


def reference(lin: Any, x: torch.Tensor, ws: Any, adp: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """(Lin.__call__ on the dequantised copy: cuBLAS then lora_add, the cuBLAS base product alone)."""
    from jeff_runtime.engine.fast_path import lora_add

    base = x @ lin.W.T
    return lora_add(base.clone(), x, lin, adp, ws), base


def step(v: torch.Tensor) -> torch.Tensor:
    """The bfloat16 step (unit in the last place) at |v|: bfloat16 has 8 significant bits; 0 at 0."""
    _, e = torch.frexp(v.abs())
    return torch.where(v == 0, 0.0, torch.ldexp(torch.ones_like(v), e - 8))


def exact_and_bounds(lin: Any, x: torch.Tensor, adp: torch.Tensor,
                     row_len: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """(The exact product in float64, the bound for a cut product, the bound for an uncut product), per output.

    Exact: x @ W^T plus each row's (x @ A^T) @ Bt, summed in float64 from the very bfloat16 values the kernel reads:
    W = bf16(q * s) (lin.W, the weights the kernel dequantises to) and the pool's A and Bt. Every product of two
    bfloat16 values is exact in float64, and the sums carry an error below 2^-40 of the absolute terms, so this is
    the exact result for every purpose here.

    Both bounds are forward error bounds of the operations the kernel performs (int4_kernels' module docstring), with
    these quantities (u32 = 2^-23 is one whole float32 unit, not the half unit of round-to-nearest, because a
    tensor-core accumulation is not guaranteed to round to nearest; products of two bfloat16 values are exact in
    float32; n = inputs + rank):
    - slack = n x u32 x (|x| @ |W|^T + (|x| @ |A|^T) @ |Bt|): the classical bound for float32 sums of products
      (n x u x the sum of the absolute terms), covering the base product's float32 sum (inputs terms), the LoRA first
      factor's float32 sum (inputs terms, carried to the output through |Bt|) and the float32 sum of the LoRA output
      (rank terms plus the base).
    - lora_rounding = 2^-9 x (|x @ A^T| @ |Bt|): both kernels round the LoRA first factor m = x A^T to bfloat16
      before multiplying by Bt (mid16, as lora_add does); rounding to nearest with 8 significant bits moves each
      component by at most 2^-9 |m|, and Bt carries that to the output with weights |Bt|.

    Uncut product (split 1; the operations of cuBLAS + lora_add): b = the float32 base product; B = bf16(b), off by at
    most half a step of B (round to nearest; the step of B is at least that of b); M = bf16(m); r = the float32 sum
    B + M @ Bt; the result bf16(r), off by at most half a step of r, the value actually rounded. r can sit just above
    a binade boundary that the exact result sits just below (seen on the RTX: exact -1.99991, where the step is
    2^-7, but r beyond -2, where it is 2^-6, rounded to -2.01562), so that half step is taken at an upper bound of
    |r|: |r| <= |exact| + step(B) / 2 + slack + lora_rounding. So
        |result - exact| <= step(B) / 2 + step(|exact| + step(B) / 2 + slack + lora_rounding) / 2 + slack
                            + lora_rounding,
    with B taken as bf16(the exact base product) (controller's rulings, 2026-10-26).

    Cut product (split > 1): the base partials summed in float32 and never rounded to bfloat16; the result rounded
    once. Its bound stays one bfloat16 step of the exact value plus slack (the ruling of 2026-10-26; cut products
    peaked at 0.44 x it on the first run)."""
    xd = x.double()
    base = xd @ lin.W.double().T
    exact = base.clone()
    magnitude = xd.abs() @ lin.W.double().abs().T
    lora_rounding = torch.zeros_like(exact)
    rows = adp.repeat_interleave(row_len)
    pool = lin.pool.double()
    for a in rows.unique().tolist():
        sel = rows == a
        a_mat = pool[a, lin.off_a:lin.off_b].view(lin.rank, lin.k)
        bt = pool[a, lin.off_b:lin.off_b + lin.rank * lin.out].view(lin.rank, lin.out)
        mid = xd[sel] @ a_mat.T
        exact[sel] += mid @ bt
        magnitude[sel] += (xd[sel].abs() @ a_mat.abs().T) @ bt.abs()
        lora_rounding[sel] = 2.0 ** -9 * (mid.abs() @ bt.abs())
    slack = (lin.k + lin.rank) * 2.0 ** -23 * magnitude
    rounded_base = base.to(torch.bfloat16).double()
    cut = step(exact) + slack
    before_final = step(rounded_base) / 2 + slack + lora_rounding  # how far r may lie from exact
    uncut = before_final + step(exact.abs() + before_final) / 2
    return exact, cut, uncut


def judge(lin: Any, x: torch.Tensor, ws: Any, adp: torch.Tensor, row_len: int, got: torch.Tensor, uncut: bool,
          record: dict[str, Any]) -> list[str]:
    """Checks `got` (the kernel's product; `uncut` when its base product ran with split 1) against exact arithmetic:
    within its bound (exact_and_bounds) everywhere, and a mean absolute error no larger than cuBLAS + lora_add's (the
    path the kernel replaces) on the same inputs. cuBLAS + lora_add is measured against the uncut bound, the
    arithmetic it performs (reported, not asserted). Fills `record`; returns the failures."""
    ref, _ = reference(lin, x, ws, adp)
    exact, cut_bound, uncut_bound = exact_and_bounds(lin, x, adp, row_len)
    bound = uncut_bound if uncut else cut_bound
    kernel_error = (got.double() - exact).abs()
    cublas_error = (ref.double() - exact).abs()
    record.update({
        "outputs": got.numel(),
        "kernel_bound": "uncut" if uncut else "cut",
        "kernel_mean_abs_error": kernel_error.mean().item(),
        "cublas_mean_abs_error": cublas_error.mean().item(),
        "kernel_worst_error_over_bound": (kernel_error / bound).max().item(),
        "kernel_outputs_beyond_bound": int((kernel_error > bound).sum()),
        "cublas_worst_error_over_uncut_bound": (cublas_error / uncut_bound).max().item(),
        "cublas_outputs_beyond_uncut_bound": int((cublas_error > uncut_bound).sum()),
        "outputs_differing_from_cublas": int((got != ref).sum()),
    })
    failures = []
    if record["kernel_outputs_beyond_bound"]:
        failures.append(f"{record['kernel_outputs_beyond_bound']} outputs beyond the {record['kernel_bound']} "
                        f"product's error bound (worst at {record['kernel_worst_error_over_bound']:.3g} x the bound)")
    if record["kernel_mean_abs_error"] > record["cublas_mean_abs_error"]:
        failures.append(f"mean absolute error {record['kernel_mean_abs_error']:.4g} against exact, above cuBLAS's "
                        f"{record['cublas_mean_abs_error']:.4g}")
    return failures


def write_report(name: str, records: dict[str, Any]) -> None:
    """The numbers against exact, cuBLAS's beside the kernel's, to results/step2b2/task4/<name> (a report only)."""
    out = REPO / "results/step2b2/task4" / name
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"gpu": torch.cuda.get_device_name(), "torch": torch.__version__,
                               "allow_bf16_reduced_precision_reduction":
                                   torch.backends.cuda.matmul.allow_bf16_reduced_precision_reduction,
                               "cases": records}, indent=2) + "\n")


def test_batch_one_products_forced_uncut_are_within_their_error_bound(gpu: SimpleNamespace) -> None:
    """Every batch-1 shape jeff-asic tuned (32, 64 and 128 tokens; int4_tiles.TUNED), the kernel forced to split 1."""
    from jeff_runtime.engine.int4_kernels import int4_product
    from jeff_runtime.engine.int4_tiles import TUNED, Int4Tiles

    failures: list[str] = []
    records: dict[str, Any] = {}
    with torch.inference_mode():
        for label, lin in gpu.lins.items():
            columns = lin.int4.words.shape[1]
            for tokens in (32, 64, 128):
                bm, bn, _, warps, stages = TUNED[(columns, lin.k, tokens)]
                ws, adp, x = pass_inputs(gpu, lin, tokens, tokens)
                got = int4_product(lin, x, ws, adp, tiles=Int4Tiles(bm, bn, 1, warps, stages))
                record = records[f"{label} {tokens} tokens"] = {}
                failures += [f"{label} {tokens} tokens: {f}" for f in judge(lin, x, ws, adp, tokens, got, True, record)]
    write_report("int4-against-exact-batch1.json", records)
    assert len(records) == 3 * len(gpu.lins)
    assert not failures, "batch-1 INT4 products (split 1) against exact arithmetic:\n" + "\n".join(failures)


def test_the_lora_first_factor_equals_lora_adds_bit_for_bit(gpu: SimpleNamespace) -> None:
    """The partial first factors int4_product leaves in ws.QP against those lora_add leaves, on the same inputs."""
    from jeff_runtime.engine.fast_path import lora_add
    from jeff_runtime.engine.int4_kernels import int4_product

    with torch.inference_mode():
        for label, lin in gpu.lins.items():
            for tokens, row_len in gpu.passes:
                ws, adp, x = pass_inputs(gpu, lin, tokens, row_len)
                used = ws.tiles[(lin.k, lin.out)].split * tokens * lin.rank
                int4_product(lin, x, ws, adp)
                ours = ws.QP[:used].clone()
                lora_add(x @ lin.W.T, x, lin, adp, ws)
                assert torch.equal(ours, ws.QP[:used]), f"{label} {tokens} tokens (rows of {row_len})"


def test_every_product_is_within_its_error_bound(gpu: SimpleNamespace) -> None:
    """Every pass the batched path records (and the tail pass), with int4_tiles' own choice of tiles, cut or uncut."""
    from jeff_runtime.engine.int4_kernels import int4_product
    from jeff_runtime.engine.int4_tiles import int4_tiles

    failures: list[str] = []
    records: dict[str, Any] = {}
    with torch.inference_mode():
        for label, lin in gpu.lins.items():
            for tokens, row_len in gpu.passes:
                ws, adp, x = pass_inputs(gpu, lin, tokens, row_len)
                got = int4_product(lin, x, ws, adp)
                uncut = int4_tiles(lin.int4.words.shape[1], lin.k, tokens).split == 1
                assert got.shape == (tokens, lin.out) and got.dtype == torch.bfloat16
                assert bool(torch.isfinite(got).all())
                name = f"{label} {tokens} tokens (rows of {row_len})"
                record = records[name] = {}
                failures += [f"{name}: {f}" for f in judge(lin, x, ws, adp, row_len, got, uncut, record)]
    write_report("int4-against-exact-passes.json", records)
    assert not failures, "INT4 products against exact arithmetic:\n" + "\n".join(failures)


def test_two_runs_and_a_graph_replay_give_the_same_bits(gpu: SimpleNamespace) -> None:
    from jeff_runtime.engine.int4_kernels import int4_product
    from jeff_runtime.engine.int4_tiles import int4_tiles

    with torch.inference_mode():
        for label, lin in gpu.lins.items():
            for tokens, row_len in gpu.passes:
                ws, adp, x = pass_inputs(gpu, lin, tokens, row_len)
                first = int4_product(lin, x, ws, adp)
                second = int4_product(lin, x, ws, adp)
                assert torch.equal(first, second), f"{label} {tokens} tokens: two runs differ"
        # the batched path records the kernel in CUDA graphs: a replay gives the eager bits, for a cut pass of every
        # group (B = 1, 32 tokens) and an uncut one (mlp_down, 1,024 tokens in rows of 64: split 1)
        cases = [(lin, 32, 32) for lin in gpu.lins.values()] + [(gpu.lins["mlp_down"], 1024, 64)]
        for lin, tokens, row_len in cases:
            assert (int4_tiles(lin.int4.words.shape[1], lin.k, tokens).split == 1) == (tokens == 1024)
            ws, adp, x = pass_inputs(gpu, lin, tokens, row_len)
            eager = int4_product(lin, x, ws, adp)
            stream = torch.cuda.Stream()
            stream.wait_stream(torch.cuda.current_stream())
            with torch.cuda.stream(stream):
                int4_product(lin, x, ws, adp)  # warm-up outside the capture
            torch.cuda.current_stream().wait_stream(stream)
            graph = torch.cuda.CUDAGraph()
            with torch.cuda.graph(graph):
                out = int4_product(lin, x, ws, adp)
            graph.replay()
            torch.cuda.synchronize()
            assert torch.equal(out, eager), f"{lin.names[0]} {tokens} tokens: the graph replay differs from the eager run"


def test_a_group_without_int4_weights_or_a_wrong_input_is_refused(gpu: SimpleNamespace) -> None:
    from jeff_runtime.engine.fast_path import DT, Lin, Workspace
    from jeff_runtime.engine.int4_kernels import int4_product

    plain = Lin(["layers.0.mlp.down_proj"], [torch.zeros(1024, 3584, device="cuda", dtype=DT)])
    plain.bind(torch.zeros(1, plain.numel(), device="cuda", dtype=DT), 0)
    ws = Workspace(32, 32, {(3584, 1024)})
    adp = torch.zeros(1, device="cuda", dtype=torch.int32)
    with pytest.raises(ValueError, match="no INT4 weights"):
        int4_product(plain, torch.zeros(32, 3584, device="cuda", dtype=DT), ws, adp)
    lin = gpu.lins["mlp_down"]
    with pytest.raises(ValueError, match="bfloat16"):
        int4_product(lin, torch.zeros(32, 3584, device="cuda", dtype=torch.float32), ws, adp)
    with pytest.raises(ValueError, match="contiguous rows"):
        int4_product(lin, torch.zeros(3584, 32, device="cuda", dtype=DT).T, ws, adp)
    from jeff_runtime.engine.int4_tiles import Int4Tiles
    with pytest.raises(ValueError, match="32-bit"):  # checked before any allocation or launch
        int4_product(lin, torch.zeros(32, 3584, device="cuda", dtype=DT), ws, adp, tiles=Int4Tiles(32, 64, 2**16, 4, 2))
