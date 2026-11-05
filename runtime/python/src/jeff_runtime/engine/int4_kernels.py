"""The fast path's INT4 kernel (step 2b-2 design, Sections 3.1 and 3.3; plan 2b-2 Task 4): one fused group's base
product from its packed 4-bit integers, with the group's LoRA update, deterministic.

int4_product(lin, x, ws, adp) computes what Lin.__call__ computes on an INT4 base (cuBLAS on the dequantised bfloat16
copy W = bf16(q * s), then fast_path.lora_add), reading the packed integers and scales (int4.KernelInt4) instead of
W. Two launches:
1. One launch with two kinds of program:
   - the INT4 base product, in tiles of int4_tiles.int4_tiles. Each weight is dequantised to bf16(q * s) (the scale
     mode of design Section 3.1, the same weights as W, so the same model as cuBLAS on W), and each program sums its
     range of the input dimension in float32, in input order. A product cut into several ranges (split > 1) writes
     one float32 partial product per range; an uncut one writes the bfloat16 product directly.
   - the LoRA first factor x A^T, exactly as fast_path.lora_add's first kernel computes it: the same tiles
     (ws.tiles), the same split of the input dimension and the same warps, A read from the stacked adapter pool in
     the row of each tile's adapter (adp, per row of ws.row_len tokens). Its partial products go to ws.QP.
2. One epilogue:
   - split > 1: the base partials summed in split order, plus bf16(the LoRA partials summed in split order) Bt from
     the tile's adapter row, accumulated in float32 and rounded to bfloat16 once;
   - split = 1: fast_path's own LoRA output kernel on the bfloat16 product, as lora_add runs it.

No atomic additions anywhere (the 2b-1 rulings): every sum has a fixed order, so the kernel is deterministic (a shape
always gives the same bits, eager or in a CUDA graph replay). It is judged against exact arithmetic, not against
cuBLAS, whose summation order is not sequential (it splits the input dimension even at small passes and, with
PyTorch's default allow_bf16_reduced_precision_reduction, adds the partial products in bfloat16). Each output lies
within the forward error bound of the operations above: an uncut product rounds twice, as lora_add does (half a
bfloat16 step of the rounded base product, half a step of the value finally rounded, a float32 accumulation slack and the
bfloat16 rounding of the LoRA first factor); a cut product rounds once (one step of the exact result plus the float32
slack). On average it is no less accurate than cuBLAS + lora_add. tests/test_int4_kernels_cuda.py derives and checks
both bounds.

jeff-asic's benchmark/gpu_int4_fused.py is the model for this kernel: the same weight layout, tiles and launch
structure, with its atomic additions replaced by partial products summed in order, and its single adapter replaced by
the stacked pool and the per-tile adapter index of fast_path's LoRA kernels."""

from __future__ import annotations

import torch
import triton
import triton.language as tl

from jeff_runtime.engine.fast_path import DT, Lin, Workspace, _lora_out_kernel
from jeff_runtime.engine.int4_tiles import GROUP, Int4Tiles, int4_tiles


@triton.jit
def _base_tile(X, sx, WQ, S, P, Y, sy, M, N, OUT, pn, ps, pm, GROUPS, DIRECT: tl.constexpr, BM: tl.constexpr,
               BN: tl.constexpr):
    """One program of the INT4 base product: BM tokens by BN of the N kernel columns, over input groups
    ps * GROUPS .. (ps + 1) * GROUPS - 1 of 64 inputs each, summed in float32 in input order."""
    rm = pm * BM + tl.arange(0, BM)
    rn = pn * BN + tl.arange(0, BN)
    shifts = tl.arange(0, 8) * 4
    acc = tl.zeros([BM, BN], dtype=tl.float32)
    for g in range(ps * GROUPS, (ps + 1) * GROUPS):
        rk = g * 64 + tl.arange(0, 64)
        x = tl.load(X + rm[:, None] * sx + rk[None, :], mask=rm[:, None] < M, other=0.0)
        words = tl.load(WQ + (g * 8 + tl.arange(0, 8))[:, None] * N + rn[None, :])  # (8, BN): 64 inputs
        q = (words[:, None, :] >> shifts[None, :, None]) & 0xF  # input 8i + j: nibble j of word i
        s = tl.load(S + g * N + rn).to(tl.float32)
        w = ((tl.reshape(q, (64, BN)) - 8).to(tl.float32) * s[None, :]).to(tl.bfloat16)  # bf16(q * s)
        acc += tl.dot(x, w)
    if DIRECT:
        tl.store(Y + rm[:, None] * sy + rn[None, :], acc.to(tl.bfloat16), mask=(rm[:, None] < M) & (rn[None, :] < OUT))
    else:
        tl.store(P + (ps * M + rm[:, None]) * N + rn[None, :], acc, mask=rm[:, None] < M)


@triton.jit
def _lora_mid_tile(X, sx, POOL, pstride, off_a, ADP, ROW_LEN, QP, M, K, ps, pm, BLOCKS, R: tl.constexpr,
                   BM: tl.constexpr, BK: tl.constexpr):
    """One program of the LoRA first factor, the body of fast_path._lora_mid_kernel: QP[ps] (BM tokens, R) = x A^T
    over input blocks ps * BLOCKS .. (ps + 1) * BLOCKS - 1 of BK inputs, A from the pool row of the tile's adapter."""
    adp = tl.load(ADP + (pm * BM) // ROW_LEN).to(tl.int64)
    A = POOL + adp * pstride + off_a
    rm = pm * BM + tl.arange(0, BM)
    rr = tl.arange(0, R)
    acc = tl.zeros([BM, R], dtype=tl.float32)
    for kb in range(ps * BLOCKS, (ps + 1) * BLOCKS):
        rk = kb * BK + tl.arange(0, BK)
        x = tl.load(X + rm[:, None] * sx + rk[None, :], mask=rm[:, None] < M, other=0.0)
        a = tl.load(A + rr[None, :] * K + rk[:, None])
        acc += tl.dot(x, a)
    tl.store(QP + (ps * M + rm[:, None]) * R + rr[None, :], acc, mask=rm[:, None] < M)


@triton.jit
def _int4_mid_kernel(X, sx, WQ, S, P, Y, sy, POOL, pstride, off_a, ADP, ROW_LEN, QP, M, N, OUT, K, NB, SPLIT,
                     GROUPS, MID_SPLIT, MID_BLOCKS, DIRECT: tl.constexpr, R: tl.constexpr, BM: tl.constexpr,
                     BN: tl.constexpr, MID_BM: tl.constexpr, MID_BK: tl.constexpr):
    """Programs [0, NB * SPLIT * ceil(M / BM)): the INT4 base product (_base_tile), into Y (bfloat16, the OUT real
    columns) when DIRECT, else into P[split] (float32, all N columns). The rest: the LoRA first factor
    (_lora_mid_tile: MID_SPLIT ranges of MID_BLOCKS blocks of MID_BK inputs, tiles of MID_BM tokens), into QP[split].
    The two kinds of program are separate functions: Triton requires a name assigned in both branches of an if to
    have the same type in both."""
    pid = tl.program_id(0)
    base_programs = NB * SPLIT * tl.cdiv(M, BM)
    if pid < base_programs:
        _base_tile(X, sx, WQ, S, P, Y, sy, M, N, OUT, pid % NB, (pid // NB) % SPLIT, pid // (NB * SPLIT), GROUPS,
                   DIRECT, BM, BN)
    else:
        j = pid - base_programs
        _lora_mid_tile(X, sx, POOL, pstride, off_a, ADP, ROW_LEN, QP, M, K, j % MID_SPLIT, j // MID_SPLIT, MID_BLOCKS,
                       R, MID_BM, MID_BK)


@triton.jit
def _int4_out_kernel(Y, sy, P, SPLIT, QP, POOL, pstride, off_b, ADP, ROW_LEN, M, N, OUT, MID_SPLIT, NC,
                     R: tl.constexpr, BM: tl.constexpr, BN: tl.constexpr):
    """Y (M, OUT) bf16 = (the SPLIT base partials P, summed in split order) + bf16(the MID_SPLIT LoRA partials QP,
    summed in split order) Bt, accumulated in float32 and rounded once; tiles as fast_path._lora_out_kernel (BM tokens,
    NC blocks of BN columns per program, Bt from the pool row of the tile's adapter)."""
    pn = tl.program_id(0)
    pm = tl.program_id(1)
    adp = tl.load(ADP + (pm * BM) // ROW_LEN).to(tl.int64)
    Bt = POOL + adp * pstride + off_b
    rm = pm * BM + tl.arange(0, BM)
    rr = tl.arange(0, R)
    mid = tl.zeros([BM, R], dtype=tl.float32)
    for s in range(MID_SPLIT):
        mid += tl.load(QP + (s * M + rm[:, None]) * R + rr[None, :], mask=rm[:, None] < M, other=0.0)
    mid16 = mid.to(tl.bfloat16)
    for c in range(NC):
        rn = (pn * NC + c) * BN + tl.arange(0, BN)
        msk = (rm[:, None] < M) & (rn[None, :] < OUT)
        acc = tl.zeros([BM, BN], dtype=tl.float32)
        for s in range(SPLIT):
            acc += tl.load(P + (s * M + rm[:, None]) * N + rn[None, :], mask=msk, other=0.0)
        bt = tl.load(Bt + rr[:, None] * OUT + rn[None, :], mask=rn[None, :] < OUT, other=0.0)
        acc += tl.dot(mid16, bt)
        tl.store(Y + rm[:, None] * sy + rn[None, :], acc.to(tl.bfloat16), mask=msk)


def int4_product(lin: Lin, x: torch.Tensor, ws: Workspace, adp: torch.Tensor,
                 tiles: Int4Tiles | None = None) -> torch.Tensor:
    """The fused product of x (M, in) through `lin`'s INT4 weights plus each token's adapter update (pool row
    adp[t // ws.row_len]): Lin.__call__'s result on an INT4 base, computed from the packed integers (module
    docstring). `tiles`: the base product's tiles, int4_tiles' choice when None (the tests force other splits).
    ValueError when `lin` holds no INT4 weights, x is not a contiguous bfloat16 (M, lin.k) tensor, or the pass is too
    large for the kernel's 32-bit offsets."""
    packed = lin.int4
    if packed is None:
        raise ValueError(f"{', '.join(lin.names)}: no INT4 weights (fast_path.attach_int4 gives them to an INT4 base's "
                         "fused groups only)")
    if x.dtype != DT or x.dim() != 2 or x.shape[1] != lin.k or x.stride(1) != 1:
        raise ValueError(f"{', '.join(lin.names)}: the INT4 product takes a bfloat16 (tokens, {lin.k}) input with "
                         f"contiguous rows; got {x.dtype} {tuple(x.shape)} with strides {x.stride()}")
    m, k = x.shape
    columns = packed.words.shape[1]
    if tiles is None:
        tiles = int4_tiles(columns, k, m)
    if tiles.split * m * columns >= 2**31:
        raise ValueError(f"{', '.join(lin.names)}: {tiles.split} split(s) x {m} tokens x {columns} columns reach 2^31; "
                         "the kernel's offsets are 32-bit")
    if columns % tiles.bn or (k // GROUP) % tiles.split:
        raise ValueError(f"INT4 tiles {tiles} do not fit {columns} columns and {k} inputs")
    lora = ws.tiles[(k, lin.out)]
    if lora.mid_warps != tiles.warps:
        raise ValueError(f"The LoRA first factor runs with {lora.mid_warps} warps in lora_add but would run with "
                         f"{tiles.warps} in the INT4 launch, which could round it differently")
    pool = lin.pool
    y = torch.empty(m, lin.out, device=x.device, dtype=DT)
    direct = tiles.split == 1
    partials = y if direct else torch.empty(tiles.split * m * columns, device=x.device, dtype=torch.float32)
    programs = tiles.programs(columns, m) + lora.split * triton.cdiv(m, lora.bm)
    _int4_mid_kernel[(programs,)](x, x.stride(0), packed.words, packed.scales, partials, y, y.stride(0), pool,
                                  pool.stride(0), lin.off_a, adp, ws.row_len, ws.QP, m, columns, lin.out, k,
                                  columns // tiles.bn, tiles.split, k // GROUP // tiles.split, lora.split,
                                  k // lora.bk // lora.split, DIRECT=direct, R=lin.rank, BM=tiles.bm, BN=tiles.bn,
                                  MID_BM=lora.bm, MID_BK=lora.bk, num_warps=tiles.warps, num_stages=tiles.stages)
    grid = (triton.cdiv(lin.out, lora.bn * lora.nc), triton.cdiv(m, lora.out_bm))
    if direct:
        _lora_out_kernel[grid](y, y.stride(0), ws.QP, pool, pool.stride(0), lin.off_b, adp, ws.row_len, m, lin.out,
                               lora.split, lora.nc, R=lin.rank, BM=lora.out_bm, BN=lora.bn, num_warps=lora.out_warps)
    else:
        _int4_out_kernel[grid](y, y.stride(0), partials, tiles.split, ws.QP, pool, pool.stride(0), lin.off_b, adp,
                               ws.row_len, m, columns, lin.out, lora.split, lora.nc, R=lin.rank, BM=lora.out_bm,
                               BN=lora.bn, num_warps=lora.out_warps)
    return y
