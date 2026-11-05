"""The batched fast path's kernels (batched_kernels.py) on a real GPU, each against a plain PyTorch reference on random
inputs, in passes of B = 1, 3, 16 and 64 rows whose prefixes have mixed lengths (0, and lengths that are not multiples
of 32), read from a real prefix pool through block tables. At B = 1 the recurrence, the attention and the readout are
also compared with what the single-row path did (fla's chunk kernel, PyTorch's attention, the cuBLAS readout). Until
plan 2b-1 Task 8 removed the single-row path, the two input kernels were also checked bit for bit, row by row,
against its single-row kernels.

Each test states what it compares and the tolerance, with the reason. Two facts recur:
- bfloat16 has 8 significant bits, so one rounding step is at most 2^-7 of the value (rtol 2^-7 allows one step);
- a reference that rounds the same intermediate values can still land one step away when its float32 value differs
  in the last bits (a different summation order, or a fused multiply-add), so references are compared within one or
  two rounding steps.

Skipped where there is no NVIDIA GPU (as on the Mac). Run on datigator:
.venv/bin/python -m pytest -m cuda runtime/python/tests/test_batched_kernels.py"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace
from typing import Any

import pytest
import torch

pytestmark = pytest.mark.cuda

BATCHES = (1, 3, 16, 64)
# Prefix lengths in the pool: empty, short, around a page boundary, long and not multiples of 32.
PREFIX_LENGTHS = (0, 1, 7, 31, 32, 33, 45, 64, 100, 257, 300, 513, 1000, 1100, 2050)
QKV, Z, HEADS, KD, VD = 6144, 2048, 16, 128, 128
BF16_STEP = 2.0 ** -7  # one bfloat16 rounding step, relative to the value, at most


@pytest.fixture(scope="module")
def gpu() -> SimpleNamespace:
    """The kernels and a prefix pool on the GPU holding one random prefix per length of PREFIX_LENGTHS (the padding
    tokens of each prefix's last key/value page hold NaN, which no kernel may read), plus slot 0 (the empty prefix)."""
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    from jeff_runtime.engine import batched_kernels
    from jeff_runtime.engine.prefix_pool import PrefixPool

    torch.manual_seed(0)
    pool = PrefixPool(1200, "cuda")
    shapes = pool.shapes
    slots = {}
    for i, p in enumerate(PREFIX_LENGTHS):
        slot = pool.reserve(1, f"prefix {i}", p)
        padded = math.ceil(p / 32) * 32
        kv = torch.randn(shapes.attn_layers, 2, padded, *shapes.kv, device="cuda").to(torch.bfloat16)
        kv[:, :, p:] = float("nan")
        conv = torch.randn(shapes.gdn_layers, *shapes.conv, device="cuda").to(torch.bfloat16)
        state = 0.5 * torch.randn(shapes.gdn_layers, *shapes.state, device="cuda")
        pool.write(pool.tables[slot], conv, state, kv)
        slots[p] = slot
    slots_by_length = {0: [0, slots[0]], **{p: [s] for p, s in slots.items() if p}}
    gathered = {slot: pool.gather(slot) for slot in [0, *slots.values()]}
    return SimpleNamespace(bk=batched_kernels, pool=pool, slots=slots, slots_by_length=slots_by_length,
                           gathered=gathered)


def pass_slots(gpu: SimpleNamespace, b: int, seed: int) -> list[int]:
    """The prefix slots of a pass of b rows: at B = 1 the 33-token prefix; otherwise the empty prefix (slot 0), a
    zero-length prefix with a state, 33, 32 (exactly one page) and 1,000 tokens first, then random prefixes (rows may
    share one)."""
    if b == 1:
        return [gpu.slots[33]]
    must = [0, gpu.slots[0], gpu.slots[33], gpu.slots[32], gpu.slots[1000]]
    rng = random.Random(seed)
    every = [0, *gpu.slots.values()]
    chosen = (must + [rng.choice(every) for _ in range(b)])[:b]
    rng.shuffle(chosen)
    return chosen


def tables_for(gpu: SimpleNamespace, slots: list[int]) -> tuple[torch.Tensor, torch.Tensor]:
    """The rows' block tables (int32, as the pass's input buffer holds them) and prefix lengths."""
    index = torch.tensor(slots, device="cuda")
    tables = gpu.pool.tables.index_select(0, index).to(torch.int32).contiguous()
    pos = torch.tensor([gpu.pool.length(s) for s in slots], device="cuda", dtype=torch.int32)
    return tables, pos


def bf(x: torch.Tensor) -> torch.Tensor:
    return x.to(torch.bfloat16).float()


def gdn_weights(seed: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    gen = torch.Generator(device="cuda").manual_seed(seed)
    conv_w = bf(0.5 * torch.randn(4, QKV, device="cuda", generator=gen)).contiguous()
    a = -torch.exp(torch.randn(HEADS, device="cuda", generator=gen))
    dt_bias = torch.randn(HEADS, device="cuda", generator=gen)
    return conv_w, a, dt_bias


# ---------------------------------------------------------------- Gated DeltaNet input side


def gdn_prep_reference(pr: torch.Tensor, tails: torch.Tensor, conv_w: torch.Tensor, a: torch.Tensor,
                       dt_bias: torch.Tensor, n: int) -> tuple[torch.Tensor, ...]:
    """Plain PyTorch: per row, the causal convolution over [its 3 cached inputs; its n new inputs], SiLU rounded to
    bfloat16, query and key heads L2-normalised; g = A softplus(a + dt_bias), beta = sigmoid(b)."""
    b = tails.shape[0]
    x = torch.cat([tails.float(), pr[:, :QKV].float().view(b, n, QKV)], 1)
    acc = sum(conv_w[i] * x[:, i:i + n] for i in range(4))
    c = bf(acc * torch.sigmoid(acc)).view(b, n, 3 * HEADS, KD)
    qk = c[:, :, :2 * HEADS]
    y = qk * torch.rsqrt((qk * qk).sum(-1, keepdim=True) + 1e-6)
    bv = pr[:, QKV + Z:QKV + Z + HEADS].float().view(b, n, HEADS)
    av = pr[:, QKV + Z + HEADS:QKV + Z + 2 * HEADS].float().view(b, n, HEADS) + dt_bias
    g = a * torch.where(av > 20, av, torch.log1p(torch.exp(av)))
    return y[:, :, :HEADS], y[:, :, HEADS:], c[:, :, 2 * HEADS:], g, torch.sigmoid(bv)


@pytest.mark.parametrize("b", BATCHES)
@pytest.mark.parametrize("n", [32, 64])
@pytest.mark.parametrize("gdn_layer", [0, 13, 17])
def test_gdn_prep_rows(gpu: SimpleNamespace, b: int, n: int, gdn_layer: int) -> None:
    """Each row's convolution tail comes from its own convolution page (layers 0, 13 and 17: both convolution pages,
    first and last layer of a page). Compared with the PyTorch reference: v and beta within one bfloat16 step (rtol
    2^-7: the float32 convolution sum may differ in its last bits, by a fused multiply-add, before the rounding); q and
    k within two (rtol 2^-6: the L2 norm is summed in another order too, and its last bits can move the rounding of a
    value just past one step, measured 1.07 steps); g float32 within rtol 1e-5 (the GPU's exp and log
    approximations)."""
    slots = pass_slots(gpu, b, seed=b * 100 + n + gdn_layer)
    tables, _ = tables_for(gpu, slots)
    conv_w, a, dt_bias = gdn_weights(gdn_layer)
    pr = torch.randn(b * n, QKV + Z + 2 * HEADS, device="cuda").to(torch.bfloat16)
    got = gpu.bk.gdn_prep_rows(pr, gpu.pool, tables, gdn_layer, conv_w, a, dt_bias, n)
    tails = torch.stack([gpu.gathered[s][0][gdn_layer] for s in slots])
    ref = gdn_prep_reference(pr, tails, conv_w, a, dt_bias, n)
    for name, x, y, rtol in zip("q k v".split(), got[:3], ref[:3], (2 * BF16_STEP, 2 * BF16_STEP, BF16_STEP),
                                strict=True):
        torch.testing.assert_close(x.float(), y, rtol=rtol, atol=1e-6, msg=lambda m, name=name: f"{name}: {m}")
    torch.testing.assert_close(got[3], ref[3], rtol=1e-5, atol=1e-6)
    torch.testing.assert_close(got[4].float(), ref[4], rtol=BF16_STEP, atol=1e-6)


# ---------------------------------------------------------------- Gated DeltaNet recurrence


def gdn_inputs(b: int, n: int, seed: int) -> tuple[torch.Tensor, ...]:
    """Random q, k, v, g, beta as gdn_prep_rows gives them: unit query and key heads, decay g < 0, beta in (0, 1)."""
    gen = torch.Generator(device="cuda").manual_seed(seed)

    def unit() -> torch.Tensor:
        x = torch.randn(b, n, HEADS, KD, device="cuda", generator=gen)
        return (x / x.norm(dim=-1, keepdim=True)).to(torch.bfloat16)

    q, k = unit(), unit()
    v = torch.randn(b, n, HEADS, VD, device="cuda", generator=gen).to(torch.bfloat16)
    g = -0.5 * torch.nn.functional.softplus(torch.randn(b, n, HEADS, device="cuda", generator=gen))
    beta = torch.sigmoid(torch.randn(b, n, HEADS, device="cuda", generator=gen)).to(torch.bfloat16)
    return q, k, v, g, beta


def gdn_reference(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, g: torch.Tensor, beta: torch.Tensor,
                  h0: torch.Tensor) -> torch.Tensor:
    """Plain PyTorch, float32, token by token: S <- S exp(g); v' = beta (v - S^T k); S <- S + k v'^T; o = S^T q / sqrt(128),
    with S (key dim, value dim) per row and head, starting from h0."""
    s = h0.clone()
    out = torch.empty(v.shape, device="cuda", dtype=torch.float32)
    for t in range(q.shape[1]):
        s = s * torch.exp(g[:, t])[..., None, None]
        kt, vt, qt = k[:, t].float(), v[:, t].float(), q[:, t].float() * KD ** -0.5
        vnew = beta[:, t].float()[..., None] * (vt - torch.einsum("bhkv,bhk->bhv", s, kt))
        s = s + kt[..., :, None] * vnew[..., None, :]
        out[:, t] = torch.einsum("bhkv,bhk->bhv", s, qt)
    return out


def initial_states(gpu: SimpleNamespace, slots: list[int], gdn_layer: int) -> torch.Tensor:
    return torch.stack([gpu.gathered[s][1][gdn_layer] for s in slots])


# Gated DeltaNet outputs (|o| < 0.25 here). The recurrent kernel and the reference both work in float32 from the same
# bfloat16 inputs and differ only in summation order before the output's bfloat16 rounding; fla's chunk kernel rounds
# more (bfloat16 operands in its chunked products). Measured on datigator (2026-10-24), largest absolute difference:
# recurrent vs reference 4.9e-4, chunk vs reference 6.9e-4, recurrent vs chunk (today's path) 9.8e-4, i.e. one or two
# bfloat16 steps at the largest outputs (a step is 2^-11 = 4.9e-4 for |o| in [0.06, 0.125), 9.8e-4 up to 0.25).
# Tolerance: atol 2^-9 (two steps at |o| up to 0.25), rtol 2^-6. A wrong state (another layer's, another row's)
# misses it by 100x (measured 0.22).
GDN_RTOL, GDN_ATOL = 2.0 ** -6, 2.0 ** -9


@pytest.mark.parametrize("b", BATCHES)
@pytest.mark.parametrize("n", [32, 64])
@pytest.mark.parametrize("gdn_layer", [0, 5, 17])
def test_gdn_recurrent_slots(gpu: SimpleNamespace, b: int, n: int, gdn_layer: int) -> None:
    """Each row starts from its own prefix's state, read in place from its state pages (layer 5's heads 80..95
    straddle pages 13 to 15). Compared with the PyTorch reference (GDN_RTOL, GDN_ATOL); with the stop at the last
    real token, the real positions are bit for bit those without the stop and the padding positions are zero."""
    slots = pass_slots(gpu, b, seed=b * 7 + n + gdn_layer)
    tables, _ = tables_for(gpu, slots)
    q, k, v, g, beta = gdn_inputs(b, n, seed=b + n + gdn_layer)
    got = gpu.bk.gdn_recurrent_slots(q, k, v, g, beta, gpu.pool, tables, gdn_layer)
    ref = gdn_reference(q, k, v, g, beta, initial_states(gpu, slots, gdn_layer))
    torch.testing.assert_close(got.float(), ref, rtol=GDN_RTOL, atol=GDN_ATOL)

    rng = random.Random(b * n)
    last = torch.tensor([n - 1 if r == 0 else rng.randrange(n) for r in range(b)], device="cuda", dtype=torch.int32)
    stopped = gpu.bk.gdn_recurrent_slots(q, k, v, g, beta, gpu.pool, tables, gdn_layer, last=last)
    for r in range(b):
        end = int(last[r]) + 1
        assert torch.equal(stopped[r, :end], got[r, :end]), f"row {r}: the stop changed a real position"
        assert not stopped[r, end:].any(), f"row {r}: padding positions after the stop are not zero"


@pytest.mark.parametrize("b", BATCHES)
@pytest.mark.parametrize("gdn_layer", [0, 5, 17])
def test_gather_gdn_states(gpu: SimpleNamespace, b: int, gdn_layer: int) -> None:
    """The states gathered for fla's chunk kernel are each row's prefix state of that layer, bit for bit (a copy)."""
    slots = pass_slots(gpu, b, seed=b + gdn_layer)
    tables, _ = tables_for(gpu, slots)
    got = gpu.bk.gather_gdn_states(gpu.pool, tables, gdn_layer, b)
    assert torch.equal(got, initial_states(gpu, slots, gdn_layer))
    out = torch.empty_like(got)
    assert gpu.bk.gather_gdn_states(gpu.pool, tables, gdn_layer, b, out=out) is out
    assert torch.equal(out, got)


@pytest.mark.parametrize("b", BATCHES)
@pytest.mark.parametrize("n", [32, 128])
def test_chunk_kernel_on_gathered_states(gpu: SimpleNamespace, b: int, n: int) -> None:
    """fla's chunk kernel with a batch of gathered initial states (the larger passes' Gated DeltaNet path) against
    the PyTorch reference (GDN_RTOL, GDN_ATOL), so its batched initial_state is used as the design assumes."""
    from fla.ops.gated_delta_rule import chunk_gated_delta_rule

    gdn_layer = 9
    slots = pass_slots(gpu, b, seed=b * 3 + n)
    tables, _ = tables_for(gpu, slots)
    q, k, v, g, beta = gdn_inputs(b, n, seed=b * n)
    h0 = gpu.bk.gather_gdn_states(gpu.pool, tables, gdn_layer, b)
    got, _ = chunk_gated_delta_rule(q, k, v, g, beta, scale=KD ** -0.5, initial_state=h0, output_final_state=False,
                                    use_qk_l2norm_in_kernel=False)
    ref = gdn_reference(q, k, v, g, beta, initial_states(gpu, slots, gdn_layer))
    torch.testing.assert_close(got.float(), ref, rtol=GDN_RTOL, atol=GDN_ATOL)


@pytest.mark.parametrize("n", [32, 64, 128, 256])
def test_gdn_recurrent_slots_against_today_at_batch_1(gpu: SimpleNamespace, n: int) -> None:
    """At B = 1, against today's decision path for one row: fla's chunk kernel from the staged state, for an empty
    prefix (slot 0 and a zero-length prefix with a state), 32, 33 and 1,000 tokens. The two algorithms round
    differently (see GDN_ATOL; measured up to 9.8e-4)."""
    from fla.ops.gated_delta_rule import chunk_gated_delta_rule

    for length in (0, 32, 33, 1000):
        for slot in gpu.slots_by_length[length]:
            tables, _ = tables_for(gpu, [slot])
            q, k, v, g, beta = gdn_inputs(1, n, seed=n + slot)
            got = gpu.bk.gdn_recurrent_slots(q, k, v, g, beta, gpu.pool, tables, 4)
            staged = gpu.gathered[slot][1][4:5].contiguous()
            today, _ = chunk_gated_delta_rule(q, k, v, g, beta, scale=KD ** -0.5, initial_state=staged,
                                              output_final_state=False, use_qk_l2norm_in_kernel=False)
            torch.testing.assert_close(got.float(), today.float(), rtol=GDN_RTOL, atol=GDN_ATOL)


# ---------------------------------------------------------------- attention input side


def attn_weights(seed: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    gen = torch.Generator(device="cuda").manual_seed(seed)
    q_w1 = 1.0 + 0.1 * torch.randn(256, device="cuda", generator=gen)
    k_w1 = 1.0 + 0.1 * torch.randn(256, device="cuda", generator=gen)
    inv_freq = 1.0 / (1e7 ** (torch.arange(0, 64, 2, dtype=torch.float32, device="cuda") / 64))
    return q_w1, k_w1, inv_freq


def attn_prep_reference(pr: torch.Tensor, q_w1: torch.Tensor, k_w1: torch.Tensor, inv_freq: torch.Tensor,
                        positions: torch.Tensor) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    """Plain PyTorch with the same bfloat16 roundings as the kernel: RMSNorm per head times its weight, then the rotary
    position (first 64 dimensions, rotate_half) at each token's own position."""
    t = pr.shape[0]
    x = pr.float()
    queries = x[:, :4096].view(t, 8, 512)[:, :, :256]
    keys = x[:, 4096:4608].view(t, 2, 256)
    values = pr[:, 4608:5120].view(t, 2, 256)

    def norm_rope(h: torch.Tensor, w1: torch.Tensor) -> torch.Tensor:
        y = bf(h * torch.rsqrt((h * h).mean(-1, keepdim=True) + 1e-6) * w1)
        rot = torch.cat([-y[..., 32:64], y[..., :32]], -1)
        f = positions.float()[:, None, None] * torch.cat([inv_freq, inv_freq])
        out = bf(bf(y[..., :64] * bf(torch.cos(f))) + bf(rot * bf(torch.sin(f))))
        return torch.cat([out, y[..., 64:]], -1)

    return norm_rope(queries, q_w1), norm_rope(keys, k_w1), values


@pytest.mark.parametrize("b", BATCHES)
@pytest.mark.parametrize("n", [32, 64])
def test_attn_prep_rows(gpu: SimpleNamespace, b: int, n: int) -> None:
    """Each row's tokens are at positions prefix length + 0..n-1 of its own prefix, and its new keys and values go to
    the per-pass buffer. Compared with the PyTorch reference: within two bfloat16 steps (rtol and atol 2^-6), since
    it takes cos and sin from PyTorch rather than the GPU's approximations, which can move a rounded cos or sin by one
    step (measured: at most one step, 0.031 at |q| = 4.5); values bit for bit."""
    slots = pass_slots(gpu, b, seed=b + n)
    _, pos = tables_for(gpu, slots)
    q_w1, k_w1, inv_freq = attn_weights(b + n)
    pr = torch.randn(b * n, 5120, device="cuda").to(torch.bfloat16)
    kn = torch.empty(b * n, 2, 256, device="cuda", dtype=torch.bfloat16)
    vn = torch.empty_like(kn)
    q = gpu.bk.attn_prep_rows(pr, q_w1, k_w1, inv_freq, pos, kn, vn, n)
    positions = pos.long().repeat_interleave(n) + torch.arange(n, device="cuda").repeat(b)
    rq, rk, rv = attn_prep_reference(pr, q_w1, k_w1, inv_freq, positions)
    torch.testing.assert_close(q.float(), rq, rtol=2.0 ** -6, atol=2.0 ** -6)
    torch.testing.assert_close(kn.float(), rk, rtol=2.0 ** -6, atol=2.0 ** -6)
    assert torch.equal(vn, rv)


# ---------------------------------------------------------------- paged attention


def attention_reference(gpu: SimpleNamespace, slots: list[int], attn_layer: int, q: torch.Tensor, kn: torch.Tensor,
                        vn: torch.Tensor, n: int) -> torch.Tensor:
    """Plain PyTorch, float32 softmax: each row's queries over [its prefix's keys; its new keys], the prefix fully and
    the new keys causally; query head h reads key/value head h // 4."""
    out = torch.empty(q.shape, device="cuda", dtype=torch.float32)
    for r, slot in enumerate(slots):
        kv = gpu.gathered[slot][2][attn_layer]  # (2, p, 2, 256)
        p = kv.shape[1]
        rows = slice(r * n, (r + 1) * n)
        keys = torch.cat([kv[0], kn[rows]]).float().repeat_interleave(4, 1)  # (p + n, 8, 256)
        values = torch.cat([kv[1], vn[rows]]).float().repeat_interleave(4, 1)
        scores = torch.einsum("nhd,mhd->hnm", q[rows].float(), keys) * 256 ** -0.5
        m = torch.arange(p + n, device="cuda")
        allowed = (m[None, :] < p) | (m[None, :] - p <= torch.arange(n, device="cuda")[:, None])
        weights = scores.masked_fill(~allowed, float("-inf")).softmax(-1)
        out[rows] = torch.einsum("hnm,mhd->nhd", weights, values)
    return out


# Attention outputs (values of scale 1, so |out| up to about 4 for short prefixes and about 0.3 after 1,000 tokens).
# The kernel rounds the softmax weights to bfloat16 before the value product (up to 2^-8 relative per weight) and its
# output to bfloat16. Measured on datigator (2026-10-24), largest absolute difference: 9.2e-3 against the float32
# reference (at outputs above 2, under one bfloat16 step there), 3.9e-3 against today's SDPA at short prefixes and
# 9.8e-4 at 1,000 tokens. Tolerance: rtol 2^-6 (two steps), atol 2^-8. A wrong layer or a prefix one token short
# misses it by 50x or more (measured 0.9 to 1.7).
ATTN_RTOL, ATTN_ATOL = 2.0 ** -6, 2.0 ** -8


def attention_inputs(b: int, n: int, seed: int) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor]:
    gen = torch.Generator(device="cuda").manual_seed(seed)
    q = torch.randn(b * n, 8, 256, device="cuda", generator=gen).to(torch.bfloat16)
    kn = torch.randn(b * n, 2, 256, device="cuda", generator=gen).to(torch.bfloat16)
    vn = torch.randn(b * n, 2, 256, device="cuda", generator=gen).to(torch.bfloat16)
    return q, kn, vn


@pytest.mark.parametrize("b", BATCHES)
@pytest.mark.parametrize("n", [32, 64])
@pytest.mark.parametrize("splits", [1, 2, 8])
def test_paged_attention(gpu: SimpleNamespace, b: int, n: int, splits: int) -> None:
    """Each row's prefix keys and values read in place from its pages (the NaN past each prefix's last token in its
    last page must not be read), then its new keys causally, the prefix walk split 1, 2 or 8 ways (8 ways leaves
    some splits with no page for short prefixes). Against the PyTorch reference (ATTN_RTOL, ATTN_ATOL), in attention
    layers 0 and 5."""
    slots = pass_slots(gpu, b, seed=b * 11 + n)
    tables, pos = tables_for(gpu, slots)
    q, kn, vn = attention_inputs(b, n, seed=b + n + splits)
    for attn_layer in (0, 5):
        got = gpu.bk.paged_attention(q, gpu.pool, tables, attn_layer, pos, kn, vn, n, splits=splits)
        assert not got.isnan().any(), "the kernel read a NaN past a prefix's last token"
        ref = attention_reference(gpu, slots, attn_layer, q, kn, vn, n)
        torch.testing.assert_close(got.float(), ref, rtol=ATTN_RTOL, atol=ATTN_ATOL)


@pytest.mark.parametrize("n", [32, 64, 128, 256])
@pytest.mark.parametrize("splits", [1, 8])
def test_paged_attention_against_today_at_batch_1(gpu: SimpleNamespace, n: int, splits: int) -> None:
    """At B = 1, against today's decision attention: the prefix staged into a buffer of capacity rows, the new keys
    after it, PyTorch's scaled_dot_product_attention with today's mask. SDPA keeps the softmax weights in higher
    precision and sums in another order, so the tolerance is the reference's (ATTN_RTOL, ATTN_ATOL)."""
    import torch.nn.functional as F

    for length in (0, 32, 33, 1000):
        for slot in gpu.slots_by_length[length]:
            tables, pos = tables_for(gpu, [slot])
            q, kn, vn = attention_inputs(1, n, seed=n + slot)
            got = gpu.bk.paged_attention(q, gpu.pool, tables, 2, pos, kn, vn, n, splits=splits)
            cap = 1024
            kv = gpu.gathered[slot][2][2]
            kc = torch.zeros(cap + n, 2, 256, device="cuda", dtype=torch.bfloat16)
            vc = torch.zeros_like(kc)
            kc[:length], vc[:length] = kv[0], kv[1]
            kc[cap:], vc[cap:] = kn, vn
            keys = torch.arange(cap + n, device="cuda")
            rows = torch.arange(n, device="cuda")[:, None]
            allowed = (keys < length) | ((keys >= cap) & (keys - cap <= rows))
            today = F.scaled_dot_product_attention(q[None].transpose(1, 2), kc[None].transpose(1, 2),
                                                   vc[None].transpose(1, 2), attn_mask=allowed, scale=256 ** -0.5,
                                                   enable_gqa=True)
            torch.testing.assert_close(got.float(), today[0].transpose(0, 1).float(), rtol=ATTN_RTOL,
                                       atol=ATTN_ATOL)


# ---------------------------------------------------------------- readout


def readout_inputs(b: int, seed: int) -> dict[str, Any]:
    """Six readouts (row 0 the base), temperatures from 0.5 to 2, each row's adapter and option count (1 to 255)."""
    gen = torch.Generator(device="cuda").manual_seed(seed)
    rng = random.Random(seed)
    counts = [1, 2, 255] + [rng.randrange(1, 256) for _ in range(b)]
    return {"hidden": torch.randn(b, 1024, device="cuda", generator=gen).to(torch.bfloat16),
            "readout_pool": (0.05 * torch.randn(6, 255, 1024, device="cuda", generator=gen)).to(torch.bfloat16),
            "temp_pool": 0.5 + 1.5 * torch.rand(6, device="cuda", generator=gen),
            "adapters": torch.tensor([rng.randrange(6) for _ in range(b)], device="cuda", dtype=torch.int32),
            "counts": torch.tensor(counts[:b], device="cuda", dtype=torch.int32)}


def softmax_reference(logits: torch.Tensor, x: dict[str, Any]) -> torch.Tensor:
    """As DecisionModel.forward: options past the count get -1e9, then division by the temperature and softmax."""
    masked = logits.masked_fill(torch.arange(255, device="cuda")[None] >= x["counts"][:, None].long(), -1e9)
    return (masked / x["temp_pool"][x["adapters"].long()][:, None]).softmax(-1)


@pytest.mark.parametrize("b", BATCHES)
def test_readout_rows(gpu: SimpleNamespace, b: int) -> None:
    """Each row reads its own adapter's readout and temperature. Compared with:
    - logits: the exact dot product (float64) rounded to bfloat16; the kernel sums in float32, so a value within a
      float32 rounding of a bfloat16 midpoint can land one step away: every logit within one step (rtol 2^-7), and
      at most 0.1% not equal, or one logit in a pass smaller than 1,000 logits (measured: 1 of 4,080 at B = 16, none
      at the other sizes);
    - probabilities: the PyTorch mask, temperature and softmax on the kernel's own logits, within float32 rounding
      (rtol 1e-5, atol 1e-7), zero past each row's count."""
    x = readout_inputs(b, seed=b)
    logits = gpu.bk.readout_logits(x["hidden"], x["readout_pool"], x["adapters"])
    exact = torch.einsum("bd,bod->bo", x["hidden"].double(), x["readout_pool"][x["adapters"].long()].double())
    expected = bf(exact.float())
    torch.testing.assert_close(logits, expected, rtol=BF16_STEP, atol=1e-6)
    assert int((logits != expected).sum()) <= max(1, 0.001 * logits.numel())
    probabilities = gpu.bk.readout_rows(x["hidden"], x["readout_pool"], x["temp_pool"], x["adapters"], x["counts"])
    torch.testing.assert_close(probabilities, softmax_reference(logits, x), rtol=1e-5, atol=1e-7)
    past = torch.arange(255, device="cuda")[None] >= x["counts"][:, None]
    assert not probabilities[past].any()


def test_readout_rows_against_today_at_batch_1(gpu: SimpleNamespace) -> None:
    """At B = 1, against today's decision readout (the cuBLAS bfloat16 product with the adapter's readout, then the
    mask, temperature and softmax), for every adapter row. Measured identical on all six rows (2026-10-24), but cuBLAS
    sums in its own order before rounding, so a logit may land one bfloat16 step away: logits within one step (rtol
    2^-7), the same top option, and every probability within 0.005 (the largest probability difference the fast path
    allowed for a change of summation order in Task 2)."""
    x = readout_inputs(64, seed=5)
    for adapter in range(6):
        hidden = x["hidden"][adapter:adapter + 1].contiguous()
        index = torch.tensor([adapter], device="cuda", dtype=torch.int32)
        count = torch.tensor([200], device="cuda", dtype=torch.int32)
        logits = gpu.bk.readout_logits(hidden, x["readout_pool"], index)
        today_logits = (hidden @ x["readout_pool"].index_select(0, index.long())[0].T).float()[0]
        torch.testing.assert_close(logits[0], today_logits, rtol=BF16_STEP, atol=1e-6)
        got = gpu.bk.readout_rows(hidden, x["readout_pool"], x["temp_pool"], index, count)[0]
        masked = today_logits.masked_fill(torch.arange(255, device="cuda") >= 200, -1e9)
        today = (masked / x["temp_pool"].index_select(0, index.long())).softmax(-1)
        assert int(got.argmax()) == int(today.argmax())
        torch.testing.assert_close(got, today, rtol=0, atol=0.005)


# ---------------------------------------------------------------- argument checks


def test_wrappers_refuse_narrow_tables_and_wrong_weights(gpu: SimpleNamespace) -> None:
    """A block table narrower than the pages a kernel may read (the fixed pages for the Gated DeltaNet kernels, also
    the longest prefix's key/value pages for attention) is refused before any launch, as are weights of the wrong
    dtype or shape."""
    pool = gpu.pool
    tables, pos = tables_for(gpu, [gpu.slots[33]])
    n = 32
    conv_w, a, dt_bias = gdn_weights(0)
    pr = torch.randn(n, QKV + Z + 2 * HEADS, device="cuda").to(torch.bfloat16)
    narrow = tables[:, :pool.fixed_pages - 1].contiguous()
    with pytest.raises(ValueError, match="width >= 50"):
        gpu.bk.gdn_prep_rows(pr, pool, narrow, 0, conv_w, a, dt_bias, n)
    with pytest.raises(ValueError, match="width >= 50"):
        gpu.bk.gather_gdn_states(pool, narrow, 0, 1)
    q, k, v, g, beta = gdn_inputs(1, n, seed=1)
    with pytest.raises(ValueError, match="width >= 50"):
        gpu.bk.gdn_recurrent_slots(q, k, v, g, beta, pool, narrow, 0)
    aq, kn, vn = attention_inputs(1, n, seed=1)
    fixed_only = tables[:, :pool.fixed_pages].contiguous()
    with pytest.raises(ValueError, match=f"width >= {pool.fixed_pages + pool.max_kv_pages}"):
        gpu.bk.paged_attention(aq, pool, fixed_only, 0, pos, kn, vn, n)
    with pytest.raises(ValueError, match="conv_w"):
        gpu.bk.gdn_prep_rows(pr, pool, tables, 0, conv_w.to(torch.bfloat16), a, dt_bias, n)
    with pytest.raises(ValueError, match="dt_bias"):
        gpu.bk.gdn_prep_rows(pr, pool, tables, 0, conv_w, a, dt_bias[:8].contiguous(), n)
    with pytest.raises(ValueError, match="a: expected"):
        gpu.bk.gdn_prep_rows(pr, pool, tables, 0, conv_w, a.double(), dt_bias, n)
    x = readout_inputs(1, seed=1)
    with pytest.raises(ValueError, match="temp_pool"):
        gpu.bk.readout_rows(x["hidden"], x["readout_pool"], x["temp_pool"].to(torch.bfloat16), x["adapters"],
                            x["counts"])
