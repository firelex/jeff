"""The kernels of the batched fast path (design 2b-1 Sections 2.1 to 2.3): one pass for B rows, each with its own
cached prefix and its own adapter, every per-row fact read from GPU memory so one CUDA graph serves any mix.

A pass holds B rows of N new tokens each (T = B * N tokens, row after row; N a multiple of 32). Each row is padded to
N with pad tokens after its last real token; every layer is causal, so the padding never reaches a real token.

Per-row facts the kernels read (int32 or int64 tensors on the GPU, one entry per row):
- `tables` (B, width): the row's block table in the prefix pool (prefix_pool.py): its Gated DeltaNet state pages,
  its convolution-tail pages, then its key/value pages in token order. Padding rows use slot 0's table (the zero page
  for the state and convolution pages, no key/value pages) and prefix length 0.
- `pos` (B,): the row's prefix length in tokens (0 for an empty prefix).
- `last` (B,): the index of the row's last real new token.
- `adapters` (B,): the row's adapter index in the stacked pools (0: the base).
- `counts` (B,): the row's number of answer options.

How a row's prefix is read from the pool's pages (the mapping PrefixPool.write and PrefixPool.gather use):
- head h of Gated DeltaNet layer l: flat head f = heads * l + h, page tables[row, f // heads_per_page], slot
  f % heads_per_page of that page (pool.state);
- the convolution tail of Gated DeltaNet layer l: page tables[row, state_pages + l // layers_per_conv_page], layer
  l % layers_per_conv_page of that page (pool.conv);
- token j of the prefix in attention layer a: page tables[row, fixed_pages + j // 32], token j % 32, layer a
  (pool.kv).

The kernels never write the pool: a cached prefix is shared by every row that names it.

Kernels:
- gdn_prep_rows: the Gated DeltaNet input side (causal convolution over the cached tail and the new inputs, SiLU,
  query/key L2 norm, decay and update strength), each row reading its own convolution tail;
- gdn_recurrent_slots: the gated delta rule token by token (fla's fused-recurrent algorithm), each program loading its
  slice of the row's initial state straight from the pool's pages, optionally stopping at the row's last real token;
- gather_gdn_states: the rows' initial states of one layer copied into a (B, heads, key dim, value dim) tensor, for
  fla's chunk kernel (which takes a batch of initial states);
- attn_prep_rows: the attention input side (query/key RMSNorm, rotary position from the row's prefix length), new
  keys and values written to a per-pass buffer, never into the shared prefix pages;
- paged_attention: each row's queries over its prefix pages (online softmax) then its own new keys causally, with the
  prefix walk optionally split across programs and combined by a second kernel (flash-decoding);
- readout_rows: each row's logits from its own adapter's readout, rounded to bfloat16 as the cuBLAS product of
  the single-row path did, then the option mask, the adapter's temperature and the softmax."""

from __future__ import annotations

import torch
import triton
import triton.language as tl

from jeff_runtime.engine.fast_path import DK, DT, DV, EPS, HD, NKV, NQ, NV, QKV_DIM, READOUT_ROWS, ROT, _bf
from jeff_runtime.engine.prefix_pool import PrefixPool

# Paged attention: 16 new tokens of one row and one key/value head per program, new keys in blocks of 32.
ATTN_BQ, ATTN_BKN = 16, 32
# Readout: answer options per program of the logits kernel (8: 3.4 us at B = 1, against 4.7 at 16 and 14.7 for
# the single-row path's readout, measured in a CUDA graph on the RTX PRO 6000).
READOUT_BO = 8


def _check_rows(name: str, tensor: torch.Tensor, rows: int) -> None:
    if tensor.dim() != 1 or tensor.shape[0] < rows or tensor.stride(0) != 1:
        raise ValueError(f"{name}: expected a contiguous vector of at least {rows} entries, got shape "
                         f"{tuple(tensor.shape)}, stride {tensor.stride()}")


def _check_tables(tables: torch.Tensor, rows: int, width: int) -> None:
    """Block tables of at least `rows` rows and `width` entries each: the Gated DeltaNet kernels read only the fixed
    pages (pool.fixed_pages), attention also the key/value pages of the longest prefix (pool.fixed_pages +
    pool.max_kv_pages). A narrower table would let a kernel read past a row into the next one."""
    if tables.dim() != 2 or tables.shape[0] < rows or tables.shape[1] < width or tables.stride(1) != 1:
        raise ValueError(f"tables: expected (rows >= {rows}, width >= {width}) with contiguous rows, got shape "
                         f"{tuple(tables.shape)}, stride {tables.stride()}")
    if tables.dtype not in (torch.int32, torch.int64):
        raise ValueError(f"tables: expected int32 or int64 page numbers, got {tables.dtype}")


def _check_vector(name: str, tensor: torch.Tensor, size: int, dtype: torch.dtype) -> None:
    if tensor.shape != (size,) or tensor.dtype != dtype or not tensor.is_contiguous():
        raise ValueError(f"{name}: expected a contiguous {dtype} vector of {size} entries, got shape "
                         f"{tuple(tensor.shape)}, {tensor.dtype}")


def _check_pass(t: int, n: int) -> int:
    if n % 32 or t % n:
        raise ValueError(f"A pass of {t} tokens in rows of {n}: rows must be a multiple of 32 tokens, and the pass "
                         "whole rows")
    return t // n


# ---------------------------------------------------------------- Gated DeltaNet input side


@triton.jit
def _gdn_prep_rows_kernel(PR, spr, CP, s_page, conv_off, BT, sbt, conv_entry, CW, A, DTB, Q, K, V, G, BETA, REAL,
                          TAIL, s_tail, N, eps, QKV: tl.constexpr, OFF_B: tl.constexpr, OFF_A: tl.constexpr,
                          NH: tl.constexpr, HDIM: tl.constexpr, MASKED: tl.constexpr):
    """The Gated DeltaNet input side for T = B * N tokens, one program per (token, block of 128 channels): the causal
    convolution over the row's last three cached inputs and its new ones (SiLU, rounded to bfloat16), then the query
    and key heads L2-normalised and the value heads stored; the last block computes the decay g and the update
    strength beta per head. Token t belongs to row t // N, whose last three cached convolution inputs are read from its
    convolution page tables[row, conv_entry], at conv_off in that page.
    MASKED (a prefix pass): tokens from REAL[row] on are padding and get decay 0 and update strength 0 (so the
    recurrent state is unchanged by them), and the row's last three real convolution inputs are copied to TAIL[row]
    (3, QKV) (row stride s_tail), zeros first when the row has fewer than three real tokens."""
    t = tl.program_id(0)
    blk = tl.program_id(1)
    row = t // N
    tt = t - row * N
    if blk < QKV // HDIM:
        ch = blk * HDIM + tl.arange(0, HDIM)
        if MASKED:
            real = tl.load(REAL + row).to(tl.int32)
            at = tt - (real - 3)  # this token's place among the row's last three real tokens
            if (at >= 0) & (at < 3):
                tl.store(TAIL + row * s_tail + at * QKV + ch, tl.load(PR + t * spr + ch))
            if tt == 0:
                for j in tl.static_range(3):
                    if j < 3 - real:
                        tl.store(TAIL + row * s_tail + j * QKV + ch, tl.zeros([HDIM], dtype=tl.bfloat16))
        page = tl.load(BT + row * sbt + conv_entry).to(tl.int64)
        tail = CP + page * s_page + conv_off
        acc = tl.zeros([HDIM], dtype=tl.float32)
        for i in tl.static_range(4):
            s = tt + i  # index in [the row's 3 cached inputs; its new inputs]
            if s < 3:
                xv = tl.load(tail + s * QKV + ch).to(tl.float32)
            else:
                xv = tl.load(PR + (t + i - 3) * spr + ch).to(tl.float32)
            acc += tl.load(CW + i * QKV + ch) * xv
        c = _bf(acc / (1.0 + tl.exp(-acc)))  # SiLU, rounded to bfloat16 as the convolution's output
        head = blk % NH
        r = t * NH + head
        if blk < 2 * NH:  # query or key head: L2-normalised
            y = c * tl.rsqrt(tl.sum(c * c, 0) + eps)
            if blk < NH:
                tl.store(Q + r * HDIM + tl.arange(0, HDIM), y.to(tl.bfloat16))
            else:
                tl.store(K + r * HDIM + tl.arange(0, HDIM), y.to(tl.bfloat16))
        else:
            tl.store(V + r * HDIM + tl.arange(0, HDIM), c.to(tl.bfloat16))
    else:
        hh = tl.arange(0, NH)
        bv = tl.load(PR + t * spr + OFF_B + hh).to(tl.float32)
        av = tl.load(PR + t * spr + OFF_A + hh).to(tl.float32) + tl.load(DTB + hh)
        sp = tl.where(av > 20.0, av, tl.log(1.0 + tl.exp(av)))
        gv = tl.load(A + hh) * sp
        bt = 1.0 / (1.0 + tl.exp(-bv))
        if MASKED:  # tokens past the row's real length: decay 0 and update strength 0, the state stays as it was
            keep = tt < tl.load(REAL + row)
            gv = tl.where(keep, gv, 0.0)
            bt = tl.where(keep, bt, 0.0)
        tl.store(G + t * NH + hh, gv)
        tl.store(BETA + t * NH + hh, bt.to(tl.bfloat16))


def gdn_prep_rows(pr: torch.Tensor, pool: PrefixPool, tables: torch.Tensor, gdn_layer: int, conv_w: torch.Tensor,
                  a: torch.Tensor, dt_bias: torch.Tensor, n: int, real: torch.Tensor | None = None,
                  tail: torch.Tensor | None = None) -> tuple[torch.Tensor, ...]:
    """The Gated DeltaNet input side of Gated DeltaNet layer `gdn_layer` for a pass of rows of n tokens.

    pr (T, >= QKV + value dim + 2 heads) the projection output; conv_w (4, QKV) float32; a, dt_bias (16,) float32.
    Returns q, k, v (B, n, 16, 128) bf16, g (B, n, 16) float32, beta (B, n, 16) bf16: row by row, bit for bit what the
    single-row kernel of the fast path before plan 2b-1 returned (checked by Task 4's tests, before it was removed).

    The batched prefix pass gives real (B,) and tail (B, 3, QKV) bf16 together: tokens from real[row] on are padding
    and get g = 0 and beta = 0 (no change to the recurrent state), and tail[row] receives the row's last three
    real convolution inputs (zeros first when it has fewer than three), the convolution tail its prefix keeps."""
    t = pr.shape[0]
    b = _check_pass(t, n)
    _check_tables(tables, b, pool.fixed_pages)
    if (real is None) != (tail is None):
        raise ValueError("gdn_prep_rows: real and tail go together (the batched prefix pass)")
    if real is not None and tail is not None:
        _check_rows("real", real, b)
        if tail.shape != (b, 3, QKV_DIM) or tail.dtype != DT or tail.stride()[1:] != (QKV_DIM, 1):
            raise ValueError(f"gdn_prep_rows: tail must be a bfloat16 ({b}, 3, {QKV_DIM}) tensor with contiguous rows, "
                             f"got {tuple(tail.shape)}, {tail.dtype}, strides {tail.stride()}")
    if conv_w.shape != (4, QKV_DIM) or conv_w.dtype != torch.float32 or not conv_w.is_contiguous():
        raise ValueError(f"gdn_prep_rows: conv_w must be a contiguous float32 (4, {QKV_DIM}) tensor, got "
                         f"{tuple(conv_w.shape)}, {conv_w.dtype}")
    _check_vector("a", a, NV, torch.float32)
    _check_vector("dt_bias", dt_bias, NV, torch.float32)
    shapes = pool.shapes
    per_page = shapes.layers_per_conv_page
    conv = pool.conv
    dev = pr.device
    q = torch.empty(b, n, NV, DV, device=dev, dtype=DT)
    k = torch.empty_like(q)
    v = torch.empty_like(q)
    g = torch.empty(b, n, NV, device=dev, dtype=torch.float32)
    beta = torch.empty(b, n, NV, device=dev, dtype=DT)
    _gdn_prep_rows_kernel[(t, QKV_DIM // DV + 1)](
        pr, pr.stride(0), conv, conv.stride(0), (gdn_layer % per_page) * conv.stride(1), tables, tables.stride(0),
        shapes.state_pages + gdn_layer // per_page, conv_w, a, dt_bias, q, k, v, g, beta,
        real if real is not None else g, tail if tail is not None else q, tail.stride(0) if tail is not None else 0,
        n, EPS, QKV=QKV_DIM, OFF_B=QKV_DIM + NV * DV, OFF_A=QKV_DIM + NV * DV + NV, NH=NV, HDIM=DV,
        MASKED=real is not None, num_warps=4)
    return q, k, v, g, beta


# ---------------------------------------------------------------- Gated DeltaNet recurrence


@triton.jit
def _gdn_recurrent_kernel(Q, K, V, G, BETA, O, SP, s_page, BT, sbt, first_head, LAST, N, scale,
                          NH: tl.constexpr, KD: tl.constexpr, VD: tl.constexpr, BV: tl.constexpr,
                          PER_PAGE: tl.constexpr, STOP_AT_LAST: tl.constexpr):
    """Gated delta rule, one token at a time (fla's fused-recurrent algorithm), for BV value columns of one head of
    one row. The row's initial state of that head, (KD, VD) float32, is read in place from its state page (flat head
    first_head + h, PER_PAGE heads per page). Never writes the state back (the prefix is shared).
    STOP_AT_LAST: stop after the row's last real token (LAST[row]) and write zeros for the padding positions."""
    iv = tl.program_id(0)
    inh = tl.program_id(1)
    row = inh // NH
    h = inh % NH
    flat = first_head + h
    page = tl.load(BT + row * sbt + flat // PER_PAGE).to(tl.int64)
    ok = tl.arange(0, KD)
    ov = iv * BV + tl.arange(0, BV)
    b_h = tl.load(SP + page * s_page + (flat % PER_PAGE) * KD * VD + ok[:, None] * VD + ov[None, :])
    base = row * N
    if STOP_AT_LAST:
        stop = tl.load(LAST + row).to(tl.int32) + 1
    else:
        stop = N
    for t in range(0, stop):
        tok = base + t
        b_q = tl.load(Q + (tok * NH + h) * KD + ok).to(tl.float32) * scale
        b_k = tl.load(K + (tok * NH + h) * KD + ok).to(tl.float32)
        b_v = tl.load(V + (tok * NH + h) * VD + ov).to(tl.float32)
        b_beta = tl.load(BETA + tok * NH + h).to(tl.float32)
        b_g = tl.load(G + tok * NH + h)
        b_h *= tl.exp(b_g)
        b_v = b_beta * (b_v - tl.sum(b_h * b_k[:, None], 0))
        b_h += b_k[:, None] * b_v[None, :]
        b_o = tl.sum(b_h * b_q[:, None], 0)
        tl.store(O + (tok * NH + h) * VD + ov, b_o.to(tl.bfloat16))
    if STOP_AT_LAST:
        for t in range(stop, N):
            tl.store(O + ((base + t) * NH + h) * VD + ov, tl.zeros([BV], dtype=tl.bfloat16))


def gdn_recurrent_slots(q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, g: torch.Tensor, beta: torch.Tensor,
                        pool: PrefixPool, tables: torch.Tensor, gdn_layer: int, last: torch.Tensor | None = None,
                        bv: int = 8, warps: int = 1) -> torch.Tensor:
    """The gated delta rule for Gated DeltaNet layer `gdn_layer`, each row starting from its prefix's state read in
    place from the pool through its block table. q, k, v (B, N, 16, 128) bf16, g (B, N, 16) float32, beta (B, N, 16)
    bf16, contiguous, as gdn_prep_rows returns them. Returns o (B, N, 16, 128) bf16 (the scale 1 / sqrt(128) applied
    to the queries, as the fast path calls fla). With `last` (B,), each row stops after token last[row] and its
    padding positions are zero."""
    b, n = q.shape[:2]
    _check_tables(tables, b, pool.fixed_pages)
    for name, x in (("q", q), ("k", k), ("v", v), ("g", g), ("beta", beta)):
        if not x.is_contiguous():
            raise ValueError(f"gdn_recurrent_slots: {name} must be contiguous")
    if last is not None:
        _check_rows("last", last, b)
    shapes = pool.shapes
    state = pool.state
    o = torch.empty_like(v)
    _gdn_recurrent_kernel[(DV // bv, b * NV)](
        q, k, v, g, beta, o, state, state.stride(0), tables, tables.stride(0), shapes.state[0] * gdn_layer,
        last if last is not None else tables, n, DK ** -0.5, NH=NV, KD=DK, VD=DV, BV=bv,
        PER_PAGE=shapes.heads_per_page, STOP_AT_LAST=last is not None, num_warps=warps)
    return o


@triton.jit
def _gather_states_kernel(SP, s_page, BT, sbt, first_head, OUT, NH: tl.constexpr, PER_PAGE: tl.constexpr,
                          WORDS: tl.constexpr, BLOCK: tl.constexpr):
    """OUT[row, h] = the state of flat head first_head + h of the row, from its state page."""
    inh = tl.program_id(0)
    blk = tl.program_id(1)
    row = inh // NH
    flat = first_head + inh % NH
    page = tl.load(BT + row * sbt + flat // PER_PAGE).to(tl.int64)
    offs = blk * BLOCK + tl.arange(0, BLOCK)
    x = tl.load(SP + page * s_page + (flat % PER_PAGE) * WORDS + offs)
    tl.store(OUT + inh.to(tl.int64) * WORDS + offs, x)


def gather_gdn_states(pool: PrefixPool, tables: torch.Tensor, gdn_layer: int, rows: int,
                      out: torch.Tensor | None = None) -> torch.Tensor:
    """The first `rows` rows' initial states of Gated DeltaNet layer `gdn_layer`, copied from their state pages into
    `out` (rows, 16, 128, 128) float32 (a new tensor when None): fla's chunk kernel takes them as initial_state."""
    _check_tables(tables, rows, pool.fixed_pages)
    shapes = pool.shapes
    heads, kd, vd = shapes.state
    if out is None:
        out = torch.empty(rows, heads, kd, vd, device=pool.data.device, dtype=torch.float32)
    elif out.shape != (rows, heads, kd, vd) or out.dtype != torch.float32 or not out.is_contiguous():
        raise ValueError(f"gather_gdn_states: out must be a contiguous float32 ({rows}, {heads}, {kd}, {vd}) tensor")
    words = kd * vd
    block = 4096
    state = pool.state
    _gather_states_kernel[(rows * heads, words // block)](state, state.stride(0), tables, tables.stride(0),
                                                          heads * gdn_layer, out, NH=heads,
                                                          PER_PAGE=shapes.heads_per_page, WORDS=words, BLOCK=block,
                                                          num_warps=4)
    return out


# ---------------------------------------------------------------- attention input side


@triton.jit
def _attn_prep_rows_kernel(PR, spr, QW, KW, inv, QO, KN, VN, POS, N, eps,
                           NQH: tl.constexpr, NKVH: tl.constexpr, HDIM: tl.constexpr, ROTD: tl.constexpr):
    """The attention input side for T = B * N tokens, one program per (token, query, key or value head): query and
    key heads RMSNorm-ed
    and given their rotary position (the row's prefix length + the token's index in the row, on the first ROTD
    dimensions), queries written to QO; new keys and values go to KN, VN (T, NKVH, HDIM), never into the cached
    prefix."""
    t = tl.program_id(0)
    j = tl.program_id(1)
    row_i = t // N
    d = tl.arange(0, HDIM)
    row = PR + t * spr
    pos = (tl.load(POS + row_i) + (t - row_i * N)).to(tl.float32)
    half: tl.constexpr = ROTD // 2
    if j < NQH + NKVH:
        if j < NQH:
            base = j * 2 * HDIM
            W = QW
        else:
            base = NQH * 2 * HDIM + (j - NQH) * HDIM
            W = KW
        x = tl.load(row + base + d).to(tl.float32)
        rs = tl.rsqrt(tl.sum(x * x, 0) / HDIM + eps)
        y = _bf(x * rs * tl.load(W + d))
        # the partner element of rotate_half, recomputed from the input (same normalisation)
        pidx = tl.where(d < half, d + half, d - half)
        pidx = tl.where(d < ROTD, pidx, d)
        xp = tl.load(row + base + pidx).to(tl.float32)
        yp = _bf(xp * rs * tl.load(W + pidx))
        f = pos * tl.load(inv + (d % half))
        cos = _bf(tl.cos(f))
        sin = _bf(tl.sin(f))
        rot = tl.where(d < half, -yp, yp)
        out = _bf(_bf(y * cos) + _bf(rot * sin))
        out = tl.where(d < ROTD, out, y)
        if j < NQH:
            tl.store(QO + (t * NQH + j) * HDIM + d, out.to(tl.bfloat16))
        else:
            tl.store(KN + (t * NKVH + (j - NQH)) * HDIM + d, out.to(tl.bfloat16))
    else:
        h = j - NQH - NKVH
        val = tl.load(row + NQH * 2 * HDIM + NKVH * HDIM + h * HDIM + d)
        tl.store(VN + (t * NKVH + h) * HDIM + d, val)


def attn_prep_rows(pr: torch.Tensor, q_w1: torch.Tensor, k_w1: torch.Tensor, inv_freq: torch.Tensor,
                   pos: torch.Tensor, kn: torch.Tensor, vn: torch.Tensor, n: int) -> torch.Tensor:
    """The attention input side for a pass of rows of n tokens. pr (T, 2 * 8 * 256 + 2 * 2 * 256): [queries with
    their gates interleaved per head | keys | values]; pos (B,) each row's prefix length. Writes the new keys and
    values into kn, vn (T, 2, 256) bf16 (the per-pass buffer); returns the queries (T, 8, 256): row by row, bit for bit
    what the single-row kernel of the fast path before plan 2b-1 returned (checked by Task 4's tests, before it was
    removed)."""
    t = pr.shape[0]
    b = _check_pass(t, n)
    _check_rows("pos", pos, b)
    for name, x in (("kn", kn), ("vn", vn)):
        if x.shape != (t, NKV, HD) or not x.is_contiguous():
            raise ValueError(f"attn_prep_rows: {name} must be a contiguous ({t}, {NKV}, {HD}) tensor")
    q = torch.empty(t, NQ, HD, device=pr.device, dtype=DT)
    _attn_prep_rows_kernel[(t, NQ + 2 * NKV)](pr, pr.stride(0), q_w1, k_w1, inv_freq, q, kn, vn, pos, n, EPS,
                                              NQH=NQ, NKVH=NKV, HDIM=HD, ROTD=ROT, num_warps=4)
    return q


# ---------------------------------------------------------------- paged attention


@triton.jit
def _paged_attention_kernel(QO, KV, s_page, layer_off, s_kv, BT, sbt, kv_entry, POS, KN, VN, OUT, PM, PL, PACC, N, T,
                            scale_log2, NQH: tl.constexpr, NKVH: tl.constexpr, HDIM: tl.constexpr, BQ: tl.constexpr,
                            PAGE: tl.constexpr, BKN: tl.constexpr, NSPLIT: tl.constexpr):
    """Attention for BQ new tokens of one row and one key/value head (its NQH // NKVH query heads together): first
    the row's cached prefix (POS[row] tokens, in the key/value pages its block table lists from entry kv_entry on,
    PAGE tokens each; this split's share of them), then the row's own new tokens causally (last split only). NSPLIT
    > 1 writes partial results (running max, sum and accumulator) for _attn_combine_kernel."""
    pid0 = tl.program_id(0)
    qb = tl.program_id(1)
    sp = tl.program_id(2)
    row = pid0 // NKVH
    g = pid0 % NKVH
    GQ: tl.constexpr = NQH // NKVH
    M: tl.constexpr = BQ * GQ
    mi = tl.arange(0, M)
    tt = qb * BQ + mi // GQ
    head = g * GQ + mi % GQ
    tok = (row * N + tt).to(tl.int64)
    d = tl.arange(0, HDIM)
    q = tl.load(QO + ((tok * NQH + head) * HDIM)[:, None] + d[None, :])
    m_i = tl.full([M], -1e30, dtype=tl.float32)
    l_i = tl.zeros([M], dtype=tl.float32)
    acc = tl.zeros([M, HDIM], dtype=tl.float32)
    P = tl.load(POS + row).to(tl.int32)
    npages = tl.cdiv(P, PAGE)
    per = tl.cdiv(npages, NSPLIT)
    p0 = sp * per
    p1 = tl.minimum(p0 + per, npages)
    j = tl.arange(0, PAGE)
    for pg in range(p0, p1):
        page = tl.load(BT + row * sbt + kv_entry + pg).to(tl.int64)
        inside = pg * PAGE + j < P  # the prefix's tokens; the rest of its last page is never read
        off = page * s_page + layer_off + (j * NKVH + g) * HDIM
        k = tl.load(KV + off[:, None] + d[None, :], mask=inside[:, None], other=0.0)
        s = tl.dot(q, tl.trans(k)) * scale_log2
        s = tl.where(inside[None, :], s, -1e30)
        m_new = tl.maximum(m_i, tl.max(s, 1))
        alpha = tl.exp2(m_i - m_new)
        p = tl.exp2(s - m_new[:, None])
        l_i = l_i * alpha + tl.sum(p, 1)
        v = tl.load(KV + s_kv + off[:, None] + d[None, :], mask=inside[:, None], other=0.0)
        acc = acc * alpha[:, None] + tl.dot(p.to(tl.bfloat16), v)
        m_i = m_new
    if sp == NSPLIT - 1:
        hi = (qb + 1) * BQ
        for st in range(0, hi, BKN):
            kk = st + tl.arange(0, BKN)
            off = ((row * N + kk).to(tl.int64) * NKVH + g) * HDIM
            k = tl.load(KN + off[:, None] + d[None, :])
            s = tl.dot(q, tl.trans(k)) * scale_log2
            s = tl.where(kk[None, :] <= tt[:, None], s, -1e30)
            m_new = tl.maximum(m_i, tl.max(s, 1))
            alpha = tl.exp2(m_i - m_new)
            p = tl.exp2(s - m_new[:, None])
            l_i = l_i * alpha + tl.sum(p, 1)
            v = tl.load(VN + off[:, None] + d[None, :])
            acc = acc * alpha[:, None] + tl.dot(p.to(tl.bfloat16), v)
            m_i = m_new
    if NSPLIT == 1:
        out = acc / l_i[:, None]
        tl.store(OUT + ((tok * NQH + head) * HDIM)[:, None] + d[None, :], out.to(tl.bfloat16))
    else:
        idx = sp * T * NQH + tok * NQH + head
        tl.store(PM + idx, m_i)
        tl.store(PL + idx, l_i)
        tl.store(PACC + (idx * HDIM)[:, None] + d[None, :], acc)


@triton.jit
def _attn_combine_kernel(PM, PL, PACC, OUT, TH, NSPLIT: tl.constexpr, HDIM: tl.constexpr):
    """OUT[i] = the attention output of (token, query head) i from its NSPLIT partial results."""
    i = tl.program_id(0).to(tl.int64)
    d = tl.arange(0, HDIM)
    mx = tl.load(PM + i)
    for s in tl.static_range(1, NSPLIT):
        mx = tl.maximum(mx, tl.load(PM + s * TH + i))
    total = 0.0
    acc = tl.zeros([HDIM], dtype=tl.float32)
    for s in tl.static_range(NSPLIT):
        w = tl.exp2(tl.load(PM + s * TH + i) - mx)
        total += w * tl.load(PL + s * TH + i)
        acc += w * tl.load(PACC + (s * TH + i) * HDIM + d)
    tl.store(OUT + i * HDIM + d, (acc / total).to(tl.bfloat16))


def paged_attention(q: torch.Tensor, pool: PrefixPool, tables: torch.Tensor, attn_layer: int, pos: torch.Tensor,
                    kn: torch.Tensor, vn: torch.Tensor, n: int, splits: int = 1, warps: int = 8,
                    stages: int = 1) -> torch.Tensor:
    """Attention of attention layer `attn_layer` for a pass of rows of n tokens: each row's queries q (T, 8, 256)
    over its prefix (pos[row] tokens, read in place from the key/value pages of its block table) and then its own
    new keys and values kn, vn (T, 2, 256) causally. `splits` > 1 divides each row's prefix pages among that many
    programs and combines their partial results with a second kernel (for small passes). Returns (T, 8, 256) bf16."""
    t = q.shape[0]
    b = _check_pass(t, n)
    _check_tables(tables, b, pool.fixed_pages + pool.max_kv_pages)
    _check_rows("pos", pos, b)
    if splits < 1:
        raise ValueError(f"paged_attention: splits={splits}, expected 1 or more")
    for name, x in (("q", q), ("kn", kn), ("vn", vn)):
        if not x.is_contiguous():
            raise ValueError(f"paged_attention: {name} must be contiguous")
    kv = pool.kv
    out = torch.empty(t, NQ, HD, device=q.device, dtype=DT)
    if splits > 1:
        pm = torch.empty(splits * t * NQ, device=q.device, dtype=torch.float32)
        pl = torch.empty_like(pm)
        pacc = torch.empty(splits * t * NQ * HD, device=q.device, dtype=torch.float32)
    else:
        pm = pl = pacc = out  # not read
    _paged_attention_kernel[(b * NKV, n // ATTN_BQ, splits)](
        q, kv, kv.stride(0), attn_layer * kv.stride(1), kv.stride(2), tables, tables.stride(0), pool.fixed_pages, pos,
        kn, vn, out, pm, pl, pacc, n, t, HD ** -0.5 * 1.4426950408889634, NQH=NQ, NKVH=NKV, HDIM=HD, BQ=ATTN_BQ,
        PAGE=pool.page_tokens, BKN=ATTN_BKN, NSPLIT=splits, num_warps=warps, num_stages=stages)
    if splits > 1:
        _attn_combine_kernel[(t * NQ,)](pm, pl, pacc, out, t * NQ, NSPLIT=splits, HDIM=HD, num_warps=2)
    return out


# ---------------------------------------------------------------- readout


@triton.jit
def _readout_logits_kernel(H, W, s_adapter, ADP, OUT, D: tl.constexpr, BO: tl.constexpr, NOUT: tl.constexpr):
    """OUT[row, o] = bf16(sum_d W[ADP[row], o, d] * H[row, d]) as float32, for BO options o of one row."""
    b = tl.program_id(0)
    ob = tl.program_id(1)
    adp = tl.load(ADP + b).to(tl.int64)
    o = ob * BO + tl.arange(0, BO)
    d = tl.arange(0, D)
    h = tl.load(H + b * D + d).to(tl.float32)
    w = tl.load(W + adp * s_adapter + o[:, None] * D + d[None, :], mask=o[:, None] < NOUT, other=0.0).to(tl.float32)
    acc = tl.sum(w * h[None, :], 1)
    tl.store(OUT + b * NOUT + o, _bf(acc), mask=o < NOUT)


@triton.jit
def _option_softmax_kernel(LOGITS, ADP, COUNT, TEMP, OUT, NOUT: tl.constexpr, BLOCK: tl.constexpr):
    """As DecisionModel.forward: options from COUNT[row] on get the logit -1e9, then division by the adapter's
    temperature and the softmax over all NOUT options."""
    b = tl.program_id(0)
    o = tl.arange(0, BLOCK)
    x = tl.load(LOGITS + b * NOUT + o, mask=o < NOUT, other=0.0)
    count = tl.load(COUNT + b).to(tl.int32)
    x = tl.where(o < count, x, -1e9)
    x = x / tl.load(TEMP + tl.load(ADP + b).to(tl.int64))
    x = tl.where(o < NOUT, x, float("-inf"))
    e = tl.exp(x - tl.max(x, 0))
    tl.store(OUT + b * NOUT + o, e / tl.sum(e, 0), mask=o < NOUT)


def readout_logits(hidden: torch.Tensor, readout_pool: torch.Tensor, adapters: torch.Tensor) -> torch.Tensor:
    """Each row's 255 logits from its own adapter's readout: hidden (B, 1024) bf16, readout_pool (adapters + 1, 255,
    1024) bf16. Summed in float32 and rounded to bfloat16, as a cuBLAS bfloat16 product rounds its output.
    Returns (B, 255) float32."""
    b, dim = hidden.shape
    _check_rows("adapters", adapters, b)
    if not hidden.is_contiguous() or readout_pool.shape[1:] != (READOUT_ROWS, dim) or not readout_pool.is_contiguous():
        raise ValueError(f"readout_logits: expected contiguous hidden (B, {dim}) and readout_pool (adapters + 1, "
                         f"{READOUT_ROWS}, {dim}), got {tuple(hidden.shape)} and {tuple(readout_pool.shape)}")
    logits = torch.empty(b, READOUT_ROWS, device=hidden.device, dtype=torch.float32)
    _readout_logits_kernel[(b, triton.cdiv(READOUT_ROWS, READOUT_BO))](
        hidden, readout_pool, readout_pool.stride(0), adapters, logits, D=dim, BO=READOUT_BO, NOUT=READOUT_ROWS,
        num_warps=4)
    return logits


def option_softmax(logits: torch.Tensor, adapters: torch.Tensor, counts: torch.Tensor,
                   temp_pool: torch.Tensor) -> torch.Tensor:
    """Each row's option probabilities from its logits (B, 255) float32: options past counts[row] masked, divided by
    temp_pool[adapters[row]], softmax. Returns (B, 255) float32 (zero past the row's count)."""
    b = logits.shape[0]
    _check_rows("adapters", adapters, b)
    _check_rows("counts", counts, b)
    if temp_pool.dim() != 1 or temp_pool.dtype != torch.float32 or not temp_pool.is_contiguous():
        raise ValueError(f"option_softmax: temp_pool must be a contiguous float32 vector (one temperature per adapter "
                         f"row), got shape {tuple(temp_pool.shape)}, {temp_pool.dtype}")
    if logits.shape != (b, READOUT_ROWS) or not logits.is_contiguous():
        raise ValueError(f"option_softmax: expected contiguous logits (B, {READOUT_ROWS}), got {tuple(logits.shape)}")
    out = torch.empty_like(logits)
    _option_softmax_kernel[(b,)](logits, adapters, counts, temp_pool, out, NOUT=READOUT_ROWS,
                                 BLOCK=triton.next_power_of_2(READOUT_ROWS), num_warps=4)
    return out


def readout_rows(hidden: torch.Tensor, readout_pool: torch.Tensor, temp_pool: torch.Tensor, adapters: torch.Tensor,
                 counts: torch.Tensor) -> torch.Tensor:
    """Each row's option probabilities (B, 255) float32: its own adapter's readout (readout_logits), its option mask,
    its adapter's temperature and the softmax (option_softmax). Two kernels; no copy of any readout."""
    return option_softmax(readout_logits(hidden, readout_pool, adapters), adapters, counts, temp_pool)
