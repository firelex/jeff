"""The fast decision path of jeff-serve (JEFF_DECISION_PATH=fast): Qwen3.5-0.8B-layout Jeff checkpoints on an NVIDIA
GPU, text only, answering each question in a few milliseconds of GPU time instead of a full PyTorch forward pass.

This module holds what the fast path is made of; batched_path.BatchedPath runs it (every decision and every prefix
pass is a replay of one of its CUDA graphs, one row or up to 64 rows at a time, each row with its own adapter and
prefix). Since plan 2b-1 there are no single-row graphs here: a lone row runs in BatchedPath's B = 1 graphs.

- FastPath: the base weights fused for the kernels (Lin: matrices that read the same input, concatenated along the
  output dimension, bfloat16, cuBLAS), the adapters stacked on the GPU, the prefix pool, and each adapter's prompt
  layout.
  - Adapters: every adapter's packed LoRA matrices (~34 MB), readout and temperature live in one row each of three
    stacked GPU tensors (the pools), allocated at start for max_adapters + 1 rows; row 0 is the base (zero LoRA
    matrices, which leave every base product unchanged, and the base readout). The graphs read the row named by each
    row's adapter index in GPU memory (the LoRA kernels per tile of tokens), so switching adapter copies no weights.
    Adding an adapter writes a free row; removing one frees its row and drops its cached prefixes.
  - Prefixes (prefix_pool.py, --prefix-pool-mb): uniform pages; a prefix's block table lists its Gated DeltaNet
    recurrent states (48 pages), its last three convolution inputs (2 pages) and its attention keys and values (one
    page per 32 tokens); least recently used first out when the pool is full, never a pinned one.
- The fused kernels its decision and prefix passes share: residual add + RMSNorm, the gated output norm, the attention
  output gate, SiLU x up, and the LoRA update (lora_add: the stacked pool, a per-row adapter index, the partial products
  summed in a fixed order, so results are deterministic; no atomic additions).
- Preparation (prepare.py, re-exported here): the prompt is split where the part that changes per request begins,
  after "Latest:\\n" in the live-last layout (an object state), otherwise after the start of the user turn, each row
  by its own adapter's layout. Only the text after the split is tokenised per request; the split is at a boundary of
  the tokenizer's pre-tokenisation, so the two token lists joined are exactly the whole prompt's tokens.

Requests with images, or with more new tokens than the largest bucket, are answered by the standard path; that is a
routing rule decided before any work, never a reaction to a failure. If the fast path fails, the request fails.

The kernels come from jeff-asic's benchmark/gpu_int4_fused.py (bfloat16 path), with the LoRA update changed to read
the stacked pool and sum its split partial products in a fixed order."""

from __future__ import annotations

import bisect
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import torch
import triton
import triton.language as tl

from jeff_runtime.engine.int4 import KernelInt4, pack_group, read_int4_file
from jeff_runtime.engine.prefix_pool import (FAST_PATH_SHAPES, LONGEST_PREFIX_TOKENS, PrefixPool, PrefixShapes,
                                             check_pool_size, pool_pages, prefixes_held)
# LATEST, USER_TURN and Row are re-exported: callers import them from here
from jeff_runtime.engine.prepare import (LATEST, NEW_BUCKETS, USER_TURN, Row, prepare_rows,  # noqa: F401
                                         split)
from jeff_runtime.wording import loaded

if TYPE_CHECKING:
    from jeff_runtime.engine.int4 import Quantization
    from jeff_runtime.engine.lora import AdapterSet
    from jeff_runtime.engine.model import DecisionModel
    from jeff_runtime.engine.types import DecisionInput

EPS = 1e-6
DT = torch.bfloat16
NK, DK, NV, DV = 16, 128, 16, 128
NQ, NKV, HD, ROT = 8, 2, 256, 64
HIDDEN, INTER, CONV = 1024, 3584, 4
QKV_DIM = NK * DK * 2 + NV * DV  # 6144
READOUT_ROWS = 255
# Prefix lengths a new prefix is padded up to (one recorded graph each)
PREFIX_BUCKETS = (64, 128, 256, 512, 768, 1024, 1536, 2048, 3072, 4096, 6144, LONGEST_PREFIX_TOKENS)
# The ten matrices a Jeff adapter adapts, by the last part of the layer name; the fast path packs exactly these.
LORA_TARGETS = ("in_proj_qkv", "in_proj_z", "out_proj", "q_proj", "k_proj", "v_proj", "o_proj", "gate_proj", "up_proj",
                "down_proj")


# ---------------------------------------------------------------- fused kernels


@triton.jit
def _bf(x):
    return x.to(tl.bfloat16).to(tl.float32)


@triton.jit
def _add_rms_kernel(X, D, W, XO, HO, sx, sd, eps, N: tl.constexpr, HAS_D: tl.constexpr):
    row = tl.program_id(0)
    offs = tl.arange(0, N)
    x = tl.load(X + row * sx + offs).to(tl.float32)
    if HAS_D:
        x = _bf(x + tl.load(D + row * sd + offs).to(tl.float32))
        tl.store(XO + row * N + offs, x.to(tl.bfloat16))
    var = tl.sum(x * x, 0) / N
    h = x * tl.rsqrt(var + eps) * tl.load(W + offs)
    tl.store(HO + row * N + offs, h.to(tl.bfloat16))


def add_rms(x: torch.Tensor, d: torch.Tensor | None, w1: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor]:
    """x (M, N) bf16; d (M, N) bf16 or None; w1 = 1 + weight (N,) float32. Returns (x + d, rms(x + d) * w1)."""
    m, n = x.shape
    h = torch.empty(m, n, device=x.device, dtype=DT)
    xo = torch.empty(m, n, device=x.device, dtype=DT) if d is not None else x
    _add_rms_kernel[(m,)](x, d if d is not None else x, w1, xo, h, x.stride(0), d.stride(0) if d is not None else 0, EPS,
                          N=n, HAS_D=d is not None, num_warps=4)
    return xo, h


@triton.jit
def _gated_norm_kernel(O, PR, spr, W, Y, eps, OFF_Z: tl.constexpr, NH: tl.constexpr, HDIM: tl.constexpr):
    tok = tl.program_id(0)
    head = tl.program_id(1)
    d = tl.arange(0, HDIM)
    o = tl.load(O + (tok * NH + head) * HDIM + d).to(tl.float32)
    y = _bf(o * tl.rsqrt(tl.sum(o * o, 0) / HDIM + eps))
    y = _bf(tl.load(W + d).to(tl.float32) * y)
    z = tl.load(PR + tok * spr + OFF_Z + head * HDIM + d).to(tl.float32)
    tl.store(Y + tok * NH * HDIM + head * HDIM + d, (y * (z / (1.0 + tl.exp(-z)))).to(tl.bfloat16))


def gated_norm(o: torch.Tensor, pr: torch.Tensor, w: torch.Tensor) -> torch.Tensor:
    """o (rows, 16, 128) bf16 contiguous; z read from pr[:, QKV:QKV + 2048]; w (128,) bf16. Returns (rows, 2048)."""
    rows = o.shape[0]
    y = torch.empty(rows, NV * DV, device=o.device, dtype=DT)
    _gated_norm_kernel[(rows, NV)](o, pr, pr.stride(0), w, y, EPS, OFF_Z=QKV_DIM, NH=NV, HDIM=DV, num_warps=4)
    return y


@triton.jit
def _gate_mul_kernel(O, so_h, so_t, PR, spr, Y, NQH: tl.constexpr, HDIM: tl.constexpr):
    t = tl.program_id(0)
    h = tl.program_id(1)
    d = tl.arange(0, HDIM)
    o = tl.load(O + h * so_h + t * so_t + d).to(tl.float32)
    gt = tl.load(PR + t * spr + h * 2 * HDIM + HDIM + d).to(tl.float32)
    s = _bf(1.0 / (1.0 + tl.exp(-gt)))
    tl.store(Y + (t * NQH + h) * HDIM + d, (o * s).to(tl.bfloat16))


def gate_mul(o: torch.Tensor, pr: torch.Tensor, n: int) -> torch.Tensor:
    """o: attention output (1, 8, n, 256) (any strides); gate from pr. Returns (n, 2048)."""
    y = torch.empty(n, NQ * HD, device=o.device, dtype=DT)
    _gate_mul_kernel[(n, NQ)](o, o.stride(1), o.stride(2), pr, pr.stride(0), y, NQH=NQ, HDIM=HD, num_warps=4)
    return y


@triton.jit
def _silu_mul_kernel(GU, sgu, Y, INTERD: tl.constexpr, BLOCK: tl.constexpr):
    row = tl.program_id(0)
    blk = tl.program_id(1)
    c = blk * BLOCK + tl.arange(0, BLOCK)
    g = tl.load(GU + row * sgu + c).to(tl.float32)
    u = tl.load(GU + row * sgu + INTERD + c).to(tl.float32)
    s = _bf(g / (1.0 + tl.exp(-g)))
    tl.store(Y + row * INTERD + c, (s * u).to(tl.bfloat16))


def silu_mul(gu: torch.Tensor) -> torch.Tensor:
    rows = gu.shape[0]
    y = torch.empty(rows, INTER, device=gu.device, dtype=DT)
    _silu_mul_kernel[(rows, INTER // 512)](gu, gu.stride(0), y, INTERD=INTER, BLOCK=512, num_warps=4)
    return y


@triton.jit
def _lora_mid_kernel(X, sx, POOL, pstride, off_a, ADP, ROW_LEN, QP, M, K, KSPLIT, R: tl.constexpr, BM: tl.constexpr,
                     BK: tl.constexpr):
    """QP[split] (M, R) float32 = x A^T over this program's range of the input dimension (LoRA first factor). A is read
    from the stacked pool, in the row of the adapter whose index ADP holds for the tile's tokens: a tile of BM tokens
    lies inside one request row of ROW_LEN tokens (BM divides ROW_LEN), so it reads ADP[(first token) // ROW_LEN]."""
    ps = tl.program_id(0)
    pm = tl.program_id(1)
    adp = tl.load(ADP + (pm * BM) // ROW_LEN).to(tl.int64)
    A = POOL + adp * pstride + off_a
    rm = pm * BM + tl.arange(0, BM)
    rr = tl.arange(0, R)
    acc = tl.zeros([BM, R], dtype=tl.float32)
    for kb in range(ps * KSPLIT, (ps + 1) * KSPLIT):
        rk = kb * BK + tl.arange(0, BK)
        x = tl.load(X + rm[:, None] * sx + rk[None, :], mask=rm[:, None] < M, other=0.0)
        a = tl.load(A + rr[None, :] * K + rk[:, None])
        acc += tl.dot(x, a)
    tl.store(QP + (ps * M + rm[:, None]) * R + rr[None, :], acc, mask=rm[:, None] < M)


@triton.jit
def _lora_out_kernel(Y, sy, QP, POOL, pstride, off_b, ADP, ROW_LEN, M, OUT, NSPLIT, NC, R: tl.constexpr,
                     BM: tl.constexpr, BN: tl.constexpr):
    """Y (M, OUT) bf16 = Y + bf16(sum of the partial first factors, in split order) Bt, accumulated in float32 and
    rounded once. Each program sums the partials of its BM tokens once, then covers NC blocks of BN output columns, so
    the partials are re-read by OUT / (NC * BN) programs per tile, not one per BN columns. Bt is read from the pool
    row of the tile's adapter, as in _lora_mid_kernel."""
    pn = tl.program_id(0)
    pm = tl.program_id(1)
    adp = tl.load(ADP + (pm * BM) // ROW_LEN).to(tl.int64)
    Bt = POOL + adp * pstride + off_b
    rm = pm * BM + tl.arange(0, BM)
    rr = tl.arange(0, R)
    mid = tl.zeros([BM, R], dtype=tl.float32)
    for s in range(NSPLIT):
        mid += tl.load(QP + (s * M + rm[:, None]) * R + rr[None, :], mask=rm[:, None] < M, other=0.0)
    mid16 = mid.to(tl.bfloat16)
    for c in range(NC):
        rn = (pn * NC + c) * BN + tl.arange(0, BN)
        msk = (rm[:, None] < M) & (rn[None, :] < OUT)
        acc = tl.load(Y + rm[:, None] * sy + rn[None, :], mask=msk, other=0.0).to(tl.float32)
        bt = tl.load(Bt + rr[:, None] * OUT + rn[None, :], mask=rn[None, :] < OUT, other=0.0)
        acc += tl.dot(mid16, bt)
        tl.store(Y + rm[:, None] * sy + rn[None, :], acc.to(tl.bfloat16), mask=msk)


@dataclass(frozen=True)
class LoraTiles:
    """How one LoRA update of a given shape runs: the first factor in `split` programs per tile of `bm` tokens (each
    summing `bk`-wide blocks of the input dimension), then the second in programs of `out_bm` tokens by `nc` blocks of
    `bn` output columns. The split fixes the summation order (partials summed in split order), so a shape always gives
    the same bits."""
    bm: int
    split: int
    bk: int
    mid_warps: int
    out_bm: int
    bn: int
    nc: int
    out_warps: int


TUNED_PASS_TOKENS = 1024  # passes of at most this many tokens use the tiles measured for them (lora_tiles)


def lora_tiles(tokens: int, row_len: int, k: int, out: int) -> LoraTiles:
    """The tiles for a LoRA update of `tokens` tokens (rows of `row_len` tokens each) from k inputs to `out` outputs.
    Tiles never span two request rows (Workspace checks that their heights divide row_len).

    The split of the input dimension (into 64-wide blocks) fixes the summation order, and it is the one the fast path
    has always used: about 64 programs of 16-, 32- or 64-token tiles. Results are therefore bit for bit the same as
    before Task 2. Shorter splits were measured too (results/step2b1/task2/tune_lora.py): at most 0.08 ms faster
    per decision, but they move some probabilities on real rows by up to 0.039, so they are not used.

    Up to TUNED_PASS_TOKENS tokens, the rest of the tiles was chosen by measurement on the RTX PRO 6000 among those
    that give bit-for-bit equal results (tune_same_order.py, checked by eval_tiles.py): lower first-kernel tiles,
    and a second kernel with 8 warps and wider column blocks, so fewer programs re-read the partials. LoRA time per
    decision: 0.47 against 0.59 ms at 32 tokens, 0.54 against 0.85 at 128, 0.60 against 1.02 at 256, 0.89 against
    1.24 at 1,024. Longer passes (long prefixes) keep the earlier tiles."""
    before_bm = 16 if tokens <= 16 else 32 if tokens <= 32 else 64
    split = _split(k // 64, triton.cdiv(tokens, before_bm))
    if tokens > TUNED_PASS_TOKENS:
        bm = min(before_bm, row_len)
        return LoraTiles(bm=bm, split=split, bk=64, mid_warps=4, out_bm=bm, bn=64, nc=1, out_warps=4)
    bm = 16 if tokens <= 128 else 32
    if out >= 4096:  # the wide input products: 5,120 to 8,224 outputs
        return LoraTiles(bm=bm, split=split, bk=64, mid_warps=4, out_bm=16 if tokens <= 256 else 32, bn=128,
                         nc=1 if tokens <= 32 else 2 if tokens <= 128 else 4, out_warps=8)
    return LoraTiles(bm=bm, split=split, bk=64, mid_warps=4, out_bm=16,
                     bn=32 if tokens <= 64 else 64 if tokens <= 128 else 128, nc=1, out_warps=8)


def _split(kblocks: int, programs: int, target: int = 64) -> int:
    """Largest split of the input dimension (a divisor of its 64-wide blocks) keeping programs * split near the target."""
    best = 1
    for s in range(1, kblocks + 1):
        if kblocks % s == 0 and programs * s <= target:
            best = s
    return best


class Workspace:
    """The tiles of every LoRA update shape for `rows` tokens in request rows of `row_len` tokens, and the float32
    partial first factors (each product overwrites what it reads)."""

    def __init__(self, rows: int, row_len: int, shapes: set[tuple[int, int]]) -> None:
        if rows % row_len:
            raise ValueError(f"A workspace of {rows} tokens cannot hold whole rows of {row_len} tokens")
        self.rows = rows
        self.row_len = row_len
        self.tiles = {(k, out): lora_tiles(rows, row_len, k, out) for k, out in shapes}
        for (k, out), t in self.tiles.items():
            if row_len % t.bm or row_len % t.out_bm or k % (t.bk * t.split):
                raise ValueError(f"LoRA tiles {t} do not fit {rows} tokens in rows of {row_len}, {k} inputs")
        self.QP = torch.empty(max(t.split for t in self.tiles.values()) * rows * 64, device="cuda", dtype=torch.float32)


def lora_add(y: torch.Tensor, x: torch.Tensor, lin: Lin, adp: torch.Tensor, ws: Workspace) -> torch.Tensor:
    """In place: y (M, N) += bf16(x A^T) Bt, with A and Bt from the pool row adp[t // ws.row_len] for token t."""
    m, k = x.shape
    out = y.shape[1]
    t = ws.tiles[(k, out)]
    pool = lin.pool
    _lora_mid_kernel[(t.split, triton.cdiv(m, t.bm))](x, x.stride(0), pool, pool.stride(0), lin.off_a, adp, ws.row_len,
                                                      ws.QP, m, k, k // t.bk // t.split, R=lin.rank, BM=t.bm, BK=t.bk,
                                                      num_warps=t.mid_warps)
    _lora_out_kernel[(triton.cdiv(out, t.bn * t.nc), triton.cdiv(m, t.out_bm))](
        y, y.stride(0), ws.QP, pool, pool.stride(0), lin.off_b, adp, ws.row_len, m, out, t.split, t.nc, R=lin.rank,
        BM=t.out_bm, BN=t.bn, num_warps=t.out_warps)
    return y


# ---------------------------------------------------------------- weights


def rms(x: torch.Tensor, w1: torch.Tensor) -> torch.Tensor:
    """Qwen3.5 RMSNorm: normalise in float32, multiply by (1 + weight) (given as w1, float32), back to the input dtype."""
    xf = x.float()
    return (xf * torch.rsqrt(xf.pow(2).mean(-1, keepdim=True) + EPS) * w1).to(x.dtype)


class Lin:
    """Matrices that read the same input, fused along the output dimension (bfloat16, cuBLAS), with a LoRA update for
    the fused group: in each adapter's row of the stacked LoRA pool, A (R, in) at off_a and Bt (R, out) at off_b,
    block-diagonal per matrix, with zero rows up to a power of two (exact: they add zeros).

    On an INT4 base, int4 also holds the group's integers and scales in the INT4 kernel's layout (attach_int4), and W
    is their dequantised bfloat16 copy bf16(q * s), which cuBLAS multiplies by."""

    def __init__(self, names: list[str], weights: list[torch.Tensor]) -> None:
        self.names = names
        self.sizes = [w.shape[0] for w in weights]
        self.out = sum(self.sizes)
        self.k = weights[0].shape[1]
        self.W = torch.cat(weights).to(DT).contiguous()
        adapted = [name for name in names if name.rsplit(".", 1)[-1] in LORA_TARGETS]
        self.rank = max(16, 1 << (16 * len(adapted) - 1).bit_length()) if adapted else 16
        self.pool: torch.Tensor
        self.off_a = self.off_b = 0
        self.int4: KernelInt4 | None = None

    def numel(self) -> int:
        return self.rank * (self.k + self.out)

    def bind(self, pool: torch.Tensor, offset: int) -> None:
        """Read this group's LoRA matrices at `offset` in every row of the stacked pool."""
        self.pool = pool
        self.off_a = offset
        self.off_b = offset + self.rank * self.k

    def pack(self, flat: torch.Tensor, updates: dict[str, tuple[torch.Tensor, torch.Tensor]]) -> None:
        """Write one adapter's updates {matrix name: (A (r, in), s*B (out, r))} into this group's slice of `flat`
        (all zeros before)."""
        a_view = flat[:self.rank * self.k].view(self.rank, self.k)
        bt_view = flat[self.rank * self.k:].view(self.rank, self.out)
        r0 = o0 = 0
        for name, size in zip(self.names, self.sizes, strict=True):
            if name in updates:
                a, b = updates[name]
                r = a.shape[0]
                if r0 + r > self.rank:
                    raise ValueError(f"{name}: LoRA rank {r} does not fit the fast path's rank-{self.rank} buffer")
                a_view[r0:r0 + r] = a
                bt_view[r0:r0 + r, o0:o0 + size] = b.T
                r0 += r
            o0 += size

    def __call__(self, x: torch.Tensor, ws: Workspace, adp: torch.Tensor) -> torch.Tensor:
        """The fused product of x (M, in) plus each token's adapter update (pool row adp[t // ws.row_len])."""
        return lora_add(x @ self.W.T, x, self, adp, ws)


def attach_int4(lins: Sequence[Lin], quantization: Quantization) -> int:
    """Give every Lin of an INT4 base its matrices' integers and scales from the folder's int4.safetensors, in the
    INT4 kernel's layout on the device of its W (int4.pack_group, which also checks that W is their bf16(q * s), bit
    for bit). Every INT4 matrix must be in exactly one Lin (read_int4_file refuses a missing or an extra one).
    Returns the packed bytes."""
    matrices = {name: (size, lin.k) for lin in lins for name, size in zip(lin.names, lin.sizes, strict=True)}
    stored = read_int4_file(quantization, matrices)
    total = 0
    for lin in lins:
        lin.int4 = pack_group(stored, lin.names, lin.W)
        total += lin.int4.nbytes()
    return total


class FastPath:
    """The fast path's weights, stacked adapters, prefix pool and prompt layouts on a loaded DecisionModel (and its
    AdapterSet); batched_path.BatchedPath records and runs the graphs on it. All calls are serialised by the caller
    (the engine's GPU thread). See the module docstring."""

    def __init__(self, model: DecisionModel, adapter_set: AdapterSet | None, prefix_pool_mb: int,
                 max_adapters: int) -> None:
        from jeff_runtime.engine.lora import LoraLinear

        if max_adapters < 1:
            raise ValueError(f"max_adapters={max_adapters}: the fast path needs room for at least one adapter")
        loaded = len(adapter_set.names) if adapter_set is not None else 0
        if loaded > max_adapters:
            raise ValueError(f"{loaded} adapters were asked for, but the fast path holds at most {max_adapters} "
                             f"(--max-adapters {max_adapters})")
        if not torch.cuda.is_available() or not str(model.device_name).startswith("cuda"):
            raise ValueError("JEFF_DECISION_PATH=fast needs the model on an NVIDIA GPU (JEFF_DEVICE=cuda)")
        if adapter_set is not None and adapter_set.precision != "model":
            raise ValueError("JEFF_DECISION_PATH=fast serves LoRA in the model's dtype; set JEFF_LORA_PRECISION=model")
        backbone = cast(Any, model).backbone
        config = backbone.config.text_config
        expected = {"hidden_size": HIDDEN, "intermediate_size": INTER, "num_hidden_layers": 24, "num_attention_heads": NQ,
                    "num_key_value_heads": NKV, "head_dim": HD, "linear_num_key_heads": NK, "linear_num_value_heads": NV,
                    "linear_key_head_dim": DK, "linear_value_head_dim": DV, "linear_conv_kernel_dim": CONV}
        wrong = {k: getattr(config, k, None) for k, v in expected.items() if getattr(config, k, None) != v}
        if wrong:
            raise ValueError(f"JEFF_DECISION_PATH=fast is built for the Qwen3.5-0.8B layout; this checkpoint differs: {wrong}")
        rope = config.rope_parameters
        if float(rope.get("partial_rotary_factor", 0)) * HD != ROT:
            raise ValueError(f"JEFF_DECISION_PATH=fast expects 64 rotary dimensions per head; rope settings: {rope}")
        torch.backends.cuda.matmul.allow_tf32 = False
        self.model = model
        self.tokenizer = model.processor.tokenizer
        self.pad_id = int(self.tokenizer.pad_token_id)

        def weight(name: str) -> torch.Tensor:
            module = backbone.get_submodule(name)
            linear = module.base if isinstance(module, LoraLinear) else module
            return linear.weight

        def tensor(name: str) -> torch.Tensor:
            return cast(torch.Tensor, backbone.get_parameter(name)).detach()

        lm = "language_model."
        self.embed = tensor(lm + "embed_tokens.weight")
        self.final_norm = 1.0 + tensor(lm + "norm.weight").float()
        self.kinds: list[str] = list(config.layer_types)
        self.layers: list[dict[str, Any]] = []
        lins: list[Lin] = []
        for i, kind in enumerate(self.kinds):
            p = f"{lm}layers.{i}."

            def lin(*names: str, p: str = p) -> Lin:
                full = [p + name for name in names]
                made = Lin(full, [weight(name) for name in full])
                lins.append(made)
                return made

            layer: dict[str, Any] = {"kind": kind, "norm_in": 1.0 + tensor(p + "input_layernorm.weight").float(),
                                     "norm_post": 1.0 + tensor(p + "post_attention_layernorm.weight").float(),
                                     "mlp_in": lin("mlp.gate_proj", "mlp.up_proj"), "mlp_down": lin("mlp.down_proj")}
            if kind == "linear_attention":
                la = "linear_attn."
                layer["proj"] = lin(la + "in_proj_qkv", la + "in_proj_z", la + "in_proj_b", la + "in_proj_a")
                layer["out"] = lin(la + "out_proj")
                layer["conv"] = tensor(p + la + "conv1d.weight")[:, 0, :].to(DT).float().T.contiguous()  # (4, 6144)
                layer["A"] = -torch.exp(tensor(p + la + "A_log").float())
                layer["dt_bias"] = tensor(p + la + "dt_bias").float()
                layer["gnorm"] = tensor(p + la + "norm.weight").to(DT)
            elif kind == "full_attention":
                sa = "self_attn."
                layer["proj"] = lin(sa + "q_proj", sa + "k_proj", sa + "v_proj")
                layer["out"] = lin(sa + "o_proj")
                layer["q_norm"] = 1.0 + tensor(p + sa + "q_norm.weight").float()
                layer["k_norm"] = 1.0 + tensor(p + sa + "k_norm.weight").float()
            else:
                raise ValueError(f"Layer {i}: unknown layer type {kind!r}")
            self.layers.append(layer)
        self.gdn_layers = [i for i, k in enumerate(self.kinds) if k == "linear_attention"]
        self.attn_layers = [i for i, k in enumerate(self.kinds) if k == "full_attention"]
        shapes = PrefixShapes(len(self.gdn_layers), (CONV - 1, QKV_DIM), (NV, DK, DV), len(self.attn_layers), (NKV, HD))
        if shapes != FAST_PATH_SHAPES:
            raise ValueError(f"JEFF_DECISION_PATH=fast expects {FAST_PATH_SHAPES.gdn_layers} Gated DeltaNet and "
                             f"{FAST_PATH_SHAPES.attn_layers} attention layers; this checkpoint has {shapes.gdn_layers} "
                             f"and {shapes.attn_layers}")
        self.prefix_pool_mb = check_pool_size(prefix_pool_mb)
        self.prefixes = PrefixPool(pool_pages(prefix_pool_mb), "cuda")
        theta = float(rope["rope_theta"])
        self.inv_freq = 1.0 / (theta ** (torch.arange(0, ROT, 2, dtype=torch.float32, device="cuda") / ROT))

        # The adapters, stacked: row 0 of each pool is the base (zero LoRA matrices, the base readout and temperature),
        # rows 1..max_adapters hold loaded adapters. Allocated once for max_adapters, so the graphs (which read the
        # pools at fixed addresses, in the row a GPU index names) are never re-recorded for capacity.
        self.lins = lins
        self.lora_numel = sum(x.numel() for x in lins)
        self.max_adapters = max_adapters
        self.lora_pool = torch.zeros(max_adapters + 1, self.lora_numel, device="cuda", dtype=DT)
        offset = 0
        for x in lins:
            x.bind(self.lora_pool, offset)
            offset += x.numel()
        self.lin_by_name = {name: x for x in lins for name in x.names}
        # An INT4 base (int4.py): each Lin also holds its integers and scales for the INT4 kernel, beside W (the
        # dequantised bfloat16 copy); a bfloat16 base has none.
        self.int4_bytes = attach_int4(lins, model.quantization) if model.quantization is not None else 0
        self.shapes = {(x.k, x.out) for x in lins}
        self.adapter_set = adapter_set
        head = adapter_set.heads[None] if adapter_set is not None else model
        self.readout_pool = torch.zeros(max_adapters + 1, READOUT_ROWS, HIDDEN, device="cuda", dtype=DT)
        self.readout_pool[0].copy_(head.readout.weight.detach())
        self.temp_pool = torch.ones(max_adapters + 1, device="cuda", dtype=torch.float32)
        self.temp_pool[0] = float(head.temperature)
        self.adapter_index: dict[str | None, int] = {None: 0}
        self.free_rows = list(range(1, max_adapters + 1))  # kept sorted: the lowest free row is used first
        # each adapter's prompt layout, fixed when it is loaded: a request is always split by its own adapter's layout,
        # never by the model's current one (which AdapterSet.use sets for the standard path)
        base_layout = adapter_set.heads[None].prompt_layout if adapter_set is not None else model.prompt_layout
        if base_layout is None:
            raise ValueError("JEFF_DECISION_PATH=fast needs the base model to name its prompt layout")
        self.layouts: dict[str | None, str] = {None: base_layout}

        for name in adapter_set.names if adapter_set is not None else []:
            self.add_adapter(name)

    # ------------------------------------------------------------ adapters

    def add_adapter(self, name: str) -> None:
        """Pack a loaded adapter's LoRA matrices (as jeff.lora.LoraLinear holds them: A, and B with alpha / rank
        folded in, both in the model's dtype), readout and temperature into the lowest free row of the pools. Runs
        only between passes (the caller serialises it with them), so no graph is reading the row."""
        if name in self.adapter_index:
            raise ValueError(f"Adapter {name} is already in the fast path")
        if not self.free_rows:
            raise ValueError(f"The fast path holds at most {self.max_adapters} adapters (--max-adapters "
                             f"{self.max_adapters}) and all {len(self.adapter_index) - 1} are loaded; remove one first, "
                             "or restart the engine with a larger --max-adapters")
        adapter_set = cast("AdapterSet", self.adapter_set)
        updates: dict[str, tuple[torch.Tensor, torch.Tensor]] = {}
        for layer, wrapper in adapter_set.layers.items():
            if name in wrapper.updates:
                if layer not in self.lin_by_name or layer.rsplit(".", 1)[-1] not in LORA_TARGETS:
                    raise ValueError(f"Adapter {name} adapts {layer}, which the fast decision path does not support")
                updates[layer] = wrapper.updates[name]
        index = self.free_rows[0]
        row = self.lora_pool[index]
        row.zero_()
        offset = 0
        for x in self.lins:
            x.pack(row[offset:offset + x.numel()], {k: v for k, v in updates.items() if k in x.names})
            offset += x.numel()
        head = adapter_set.heads[name]
        self.readout_pool[index].copy_(head.readout.weight.detach())
        self.temp_pool[index] = float(head.temperature)
        self.free_rows.pop(0)
        self.adapter_index[name] = index
        self.add_layout(name)
        self.forget(index)

    def add_layout(self, name: str) -> None:
        """Record a loaded adapter's prompt layout: its own, or the base's when its decision_config.json names none
        (the same rule as LocalEngine._prompt_layout)."""
        layout = cast("AdapterSet", self.adapter_set).heads[name].prompt_layout
        self.layouts[name] = layout if layout is not None else self.layouts[None]

    def remove_adapter(self, name: str) -> None:
        """Free the adapter's row of the pools (left as it is: nothing reads a row no adapter index names)."""
        index = self.adapter_index.pop(name)
        bisect.insort(self.free_rows, index)
        del self.layouts[name]
        self.forget(index)

    def forget(self, index: int) -> None:
        """Drop the cached prefixes of an adapter row (its adapter was added or removed), so a row filled again never
        serves an earlier adapter's prefixes."""
        self.prefixes.forget_adapter(index)

    def index(self, adapter: str | None) -> int:
        """The adapter's row in the pools (0: the base)."""
        if adapter not in self.adapter_index:
            raise ValueError(f"Adapter {adapter!r} is not loaded in the fast path "
                             f"{loaded(map(str, self.adapter_index))}")
        return self.adapter_index[adapter]

    # ------------------------------------------------------------ requests

    def split(self, row: DecisionInput, layout: str) -> tuple[str, str]:
        """The prompt of one question in the given prompt layout, split into the reusable prefix text and the new text
        (prepare.split, with the model's own processor)."""
        return split(self.model.processor, self.model.codes, row, layout)

    def prepare(self, adapter: str | None, rows: Sequence[DecisionInput]) -> list[Row] | str:
        """The rows ready for the fast path under `adapter` (None: the base), each split by that adapter's own prompt
        layout, or why the request belongs on the standard path (prepare.prepare_rows, with the model's own processor:
        call it only from the thread that runs the model; the engine prepares on its own threads, each with its own
        processor, and the tests call this)."""
        if adapter not in self.layouts:
            raise ValueError(f"Adapter {adapter!r} is not loaded in the fast path {loaded(map(str, self.layouts))}")
        return prepare_rows(self.model.processor, self.tokenizer, self.model.codes, self.layouts[adapter], rows)

    def describe(self) -> dict[str, object]:
        return {"new_token_buckets": list(NEW_BUCKETS), "prefix_buckets": list(PREFIX_BUCKETS),
                "cached_prefixes": len(self.prefixes), "prefix_pool_mb": self.prefix_pool_mb,
                "prefix_pages": self.prefixes.pages,
                "prefixes_of_1000_tokens": prefixes_held(self.prefix_pool_mb, 1000),
                "max_adapters": self.max_adapters}
