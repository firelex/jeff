"""The batched fast path (design 2b-1 Sections 2 and 3): one CUDA graph replay answers up to 64 rows, each with its
own adapter and its own cached prefix, on the weights, adapter pools and prefix pool of a loaded FastPath. These are
the fast path's only graphs: a lone row runs in the B = 1 graphs (the single-row graphs of before 2b-1 are gone).

A pass holds B rows of N new tokens each (T = B * N tokens, row after row). Each row is padded to N with pad tokens
after its last real token; every layer is causal, so the padding never reaches a real token, and each row's answer is
read at its last real token.

**Graphs.** One per (B, N): B in BATCH_BUCKETS (1 to 64), N in the fast path's new-token buckets (32 to 1,024), with
B * N <= MAX_PASS_TOKENS (16,384): 39 graphs, all recorded when the BatchedPath is built. The prefix length is not a
graph dimension: the attention kernel reads each row's prefix length and pages itself. Three settings are baked into
each graph, all chosen by timing the candidates on this GPU while recording (and logged):
- the Gated DeltaNet recurrence: the slot-indexed recurrent kernel (each row's state read in place, stopping at the
  row's last real token), or fla's chunk kernel on the rows' states gathered from the pool;
- the attention prefix split: how many programs share a row's walk over its prefix pages (1, 2, 4, 8 or 16; a
  second kernel combines their partial results);
- on an INT4 base only, the product kernel of the fused matrix products (product_choice.py, plan 2b-2 Task 5): cuBLAS
  on the dequantised bfloat16 copy, or the INT4 kernel (int4_kernels.int4_product). Both compute the same weights.
The first two are timed as the 18 Gated DeltaNet layers or the 6 attention layers of that pass alone, each in its own
CUDA graph, on rows with TIMING_PREFIX_TOKENS-token prefixes spread over distinct pages. The product kernel is timed as
the pass's 96 fused products with their LoRA updates, in layer order (each matrix read once, as in the pass, so the
weights come from memory and not the cache), each kernel in its own CUDA graph, the two timed in alternating pairs;
the batched prefix graphs choose their product kernel the same way (choose_prefix). A bfloat16 base has cuBLAS only:
nothing more is timed, and its graphs are what they were before Task 5.

Timing the attention split per graph is a deliberate departure from design Section 3, which fixes it per B (8 ways
for B <= 2, 4 for B = 4, 2 for B = 8 and 16, none above). The fixed table is not the fastest on this GPU. Task 4
measured the best split at B = 1 and 256 new tokens as 4, not 8. At recording on datigator (2026-10-24, 6 attention
layers, 1,000-token prefixes):
- B = 1, N = 32: 16 ways take 0.093 ms against 0.124 ms for 8;
- B = 8, N = 32: 4 ways take 0.202 ms against 0.334 ms for 2.
The design already chooses the Gated DeltaNet kernel by timing per graph; the split is chosen the same way. Accuracy
is guarded by the per-adapter agreement gate with the standard path (Task 8).

**Graph memory.** The batched graphs have a memory pool of their own (`graph_pool`), never shared with any other
graphs of the process; the batched prefix graphs use it too. Within it, the graphs are replayed one at a time and each
result is copied out before the next replay. All timing (the decision graphs' settings and the prefix graphs' product
kernels) happens before the first capture into this pool. The order
matters: when each shape was timed and then recorded in turn, the timing graphs (each with a pool of its own,
destroyed after timing) were destroyed between captures into one shared pool, and a later decision graph hit an
illegal memory access on its first replay, even with the inputs it was captured with (bisected on datigator,
2026-10-24: removing the timing, or giving every graph its own pool, removed the fault). The exact mechanism inside
PyTorch's allocator was not established; the order avoids the case that failed.

**Inputs.** Everything that differs per row is read from GPU memory at fixed addresses: ONE packed int32 buffer per
pass, [slot, adapter, prefix length, last, count] x B, then the B x N token ids. The host fills its pinned twin with
numpy and copies it in with one copy; the probabilities (B x 255) come back with one copy. The graph looks up the
rows' block tables itself (the pool's device copy, by slot). Padding rows (a pass of 5 rows runs in the 8-row graph)
use slot 0 (the empty prefix, zero state), adapter 0 (the base), one token and one option; their outputs are dropped.

**Prefixes.** A row names its prefix slot; the caller found or made it (ensure_prefixes) and pins
it (PrefixPool.pin) until the pass is over. decide() checks that each slot holds the row's own (adapter, prefix text)
and only reads the pool.

**Batched prefix passes** (design Section 10 item 2). ensure_prefixes(rows) finds each row's (adapter, prefix text) in
the pool, or reserves a slot for it (once, however many rows share it), and processes the new prefixes in batched
prefix passes: one graph per (B, prefix bucket) for B up to PREFIX_PASS_ROWS[bucket] (measured: long prefixes fill
the GPU alone, so 4,096 tokens and up run one per pass), each row with its own adapter index, written straight into
its own slot's pages (the page list of each row travels in the pass's input buffer). The new prefixes of a call are
grouped by prefix bucket and cut into the recorded sizes with the least measured time (prefix_passes). The prefix
graphs are recorded after the decision graphs, in the same graph memory pool, and timed afterwards by replaying them.
- A pass of one prefix computes exactly what the single-prefix pass before 2b-1 computed, bit for bit (checked against
  digests recorded before the prefix pool, test_fast_path_cuda), and is faster (the convolution tail is copied by the
  input-side kernel, and the pages come from the host).
- A pass of several prefixes is not bit for bit the same as each prefix alone: cuBLAS picks its matrix product kernels
  by the number of rows, and PyTorch's attention kernel by the batch, so the rounding differs (accepted by the
  controller's ruling; the CUDA tests bound the decisions on such prefixes by MAX_PREFIX_BATCH_DIFFERENCE, and Task
  8's per-adapter agreement with the standard path is the accuracy gate).

All calls are serialised by the caller (the engine's single GPU thread), with adapter changes between passes. decide()
and ensure_prefixes() never run concurrently with each other or with an adapter change: they share the adapter pools,
the prefix pool (whose bookkeeping is not thread safe) and the GPU stream."""

from __future__ import annotations

import logging
import statistics
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import cache, partial
from typing import Any

import torch
import torch.nn.functional as F

from jeff_runtime.engine.batched_kernels import (attn_prep_rows, gather_gdn_states, gdn_prep_rows, gdn_recurrent_slots,
                                                 paged_attention, readout_rows)
from jeff_runtime.engine.fast_path import (DK, DT, DV, HD, NEW_BUCKETS, NKV, NQ, NV, PREFIX_BUCKETS, READOUT_ROWS,
                                           FastPath, Lin, Row, Workspace, add_rms, gate_mul, gated_norm, rms, silu_mul)
from jeff_runtime.engine.int4_kernels import int4_product
from jeff_runtime.engine.prefix_pool import LONGEST_PREFIX_TOKENS, PAGE_TOKENS, SINK_PAGE, ZERO_PAGE
from jeff_runtime.engine.product_choice import CUBLAS, INT4, ProductChoice, choose_product, product_candidates

log = logging.getLogger("jeff.batched_path")

BATCH_BUCKETS = (1, 2, 4, 8, 16, 32, 64)
MAX_PASS_TOKENS = 16384
# The recorded passes: (B, N) for every batch bucket and new-token bucket with B * N <= MAX_PASS_TOKENS.
PASS_SHAPES = tuple((b, n) for n in NEW_BUCKETS for b in BATCH_BUCKETS if b * n <= MAX_PASS_TOKENS)
SPLITS = (1, 2, 4, 8, 16)
# Attention splits keep splits x T x 8 heads x (256 + 2) float32 partial results: at most this many split tokens
# (about 68 MB), so the largest passes, which have programs enough without splitting, never time a split.
MAX_SPLIT_TOKENS = 8192
# The prefix length the attention splits are timed with (the design's batch-1 benchmark point).
TIMING_PREFIX_TOKENS = 1000
GDN_KERNELS = ("recurrent", "chunk")
META = 5  # per-row numbers in the input buffer: slot, adapter, prefix length, last real token, option count
# Batched prefix passes: the most prefixes one pass of each prefix bucket (fast_path.PREFIX_BUCKETS) takes; a graph
# is recorded for every batch bucket up to it. Chosen by measurement (datigator, RTX PRO 6000, 2026-10-24: every
# batch bucket with B x bucket <= 65,536 recorded and replayed, results/step2b1/task6/measure_prefix_caps.py and
# prefix_pass_ms_wide.json): the cap is the first B at which doubling the pass gains less than 5% GPU time per
# prefix. ms per prefix, B = 1 / 2 / 4 / 8 / 16 / 32: 64 tokens 2.39 / 1.32 / 0.84 / 0.63 / 0.54 / 0.54; 256 tokens
# 3.41 / 2.59 / 2.25 / 2.06 / 1.87 / 1.93; 1,024 tokens 9.36 / 8.24 / 7.49 / 7.61; 2,048 tokens 17.1 / 15.3 / 15.4;
# 4,096 tokens 32.4 / 31.8 / 33.4; 8,192 tokens 68.8 / 70.3. Long prefixes fill the GPU alone and gain nothing.
PREFIX_PASS_ROWS = {64: 16, 128: 32, 256: 16, 512: 8, 768: 4, 1024: 4, 1536: 4, 2048: 2, 3072: 2, 4096: 1, 6144: 1,
                    8192: 1}
PREFIX_META = 2  # per-row numbers in the prefix input buffer: adapter, prefix length (then its pages, then the ids)


@dataclass(frozen=True)
class PassPlan:
    """The settings recorded into one pass's graph, and the timings (ms per pass, GPU only) they were chosen by."""
    gdn: str
    splits: int
    gdn_ms: dict[str, float]
    attention_ms: dict[int, float]
    products: ProductChoice


Product = Callable[[Lin, torch.Tensor, Workspace, torch.Tensor], torch.Tensor]


def _cublas_product(lin: Lin, x: torch.Tensor, ws: Workspace, adapters: torch.Tensor) -> torch.Tensor:
    """The group's own call, lin(x, ws, adapters) (Lin.__call__: cuBLAS on W, then lora_add), exactly as the graphs
    called it before plan 2b-2 Task 5."""
    return lin(x, ws, adapters)


def _product(kernel: str) -> Product:
    """The fused product of a kernel (product_choice.PRODUCTS), called as product(lin, x, ws, adapters): the group's
    own call (cuBLAS) or int4_kernels.int4_product. Chosen when a graph's body runs, which is only while recording, so
    the kernel is fixed in the graph."""
    if kernel == CUBLAS:
        return _cublas_product
    if kernel == INT4:
        return int4_product
    raise ValueError(f"Unknown product kernel {kernel!r}; expected {CUBLAS!r} or {INT4!r}")


@dataclass
class _PrefixInputs:
    """Views of the packed int64 prefix input buffer for one (B, prefix bucket nb) layout: per row its adapter index
    and prefix length, its pages (B, fixed pages + nb / 32: the slot's block table, then page 0), then the ids."""
    adapters: torch.Tensor
    lengths: torch.Tensor
    pages: torch.Tensor
    ids: torch.Tensor


@dataclass
class _Inputs:
    """Views of the packed input buffer for one (B, N) layout."""
    slots: torch.Tensor
    adapters: torch.Tensor
    pos: torch.Tensor
    last: torch.Tensor
    counts: torch.Tensor
    ids: torch.Tensor


def max_rows(n: int) -> int:
    """The largest batch bucket recorded for new-token bucket n."""
    return max(b for b in BATCH_BUCKETS if b * n <= MAX_PASS_TOKENS)


class BatchedPath:
    """The batched decision graphs on a loaded FastPath (see the module docstring). Building one records all graphs.

    products: the product kernels every graph chooses among (product_choice.product_candidates), None for all the
    base supports; benchmarks and tests give one to force it."""

    def __init__(self, fast: FastPath, products: Sequence[str] | None = None) -> None:
        self.fast = fast
        self.products = product_candidates({", ".join(lin.names): lin.int4 is not None for lin in fast.lins}, products)
        self.pool = fast.prefixes
        width = self.pool.tables.shape[1]
        if width < self.pool.fixed_pages + self.pool.max_kv_pages:
            raise ValueError(f"The prefix pool's block tables have {width} entries; the batched path needs "
                             f"{self.pool.fixed_pages + self.pool.max_kv_pages}")
        size = max(META * b + b * n for b, n in PASS_SHAPES)
        self.inp = torch.zeros(size, device="cuda", dtype=torch.int32)
        self.inp_host = torch.zeros(size, dtype=torch.int32).pin_memory()
        self.inp_np = self.inp_host.numpy()
        self.out_host = torch.zeros(max(BATCH_BUCKETS), READOUT_ROWS, dtype=torch.float32).pin_memory()
        self.inputs = {(b, n): self._views(b, n) for b, n in PASS_SHAPES}
        self.row_starts = {(b, n): torch.arange(b, device="cuda") * n for b, n in PASS_SHAPES}
        self.workspaces = {(b, n): Workspace(b * n, n, fast.shapes) for b, n in PASS_SHAPES}
        self.graphs: dict[tuple[int, int], tuple[torch.cuda.CUDAGraph, torch.Tensor]] = {}
        self.plans: dict[tuple[int, int], PassPlan] = {}
        # batched prefix passes: one graph per (B, prefix bucket) with B <= PREFIX_PASS_ROWS[bucket]
        self.prefix_shapes = tuple((b, nb) for nb in PREFIX_BUCKETS for b in BATCH_BUCKETS if b <= PREFIX_PASS_ROWS[nb])
        size = max(self._prefix_size(b, nb) for b, nb in self.prefix_shapes)
        self.prefix_inp = torch.zeros(size, device="cuda", dtype=torch.long)  # int64: index_copy_ takes int64 pages
        self.prefix_inp_host = torch.zeros(size, dtype=torch.long).pin_memory()
        self.prefix_inp_np = self.prefix_inp_host.numpy()
        self.prefix_inputs = {(b, nb): self._prefix_views(b, nb) for b, nb in self.prefix_shapes}
        self.prefix_workspaces = {(b, nb): Workspace(b * nb, nb, fast.shapes) for b, nb in self.prefix_shapes}
        rows = max(b for b, _ in self.prefix_shapes)
        # the convolution tail a prefix starts from: nothing (slot 0's zero page)
        self.zero_tables = torch.full((rows, self.pool.fixed_pages), ZERO_PAGE, device="cuda", dtype=torch.long)
        self.zero_pos = torch.zeros(rows, device="cuda", dtype=torch.int32)
        self.prefix_graphs: dict[tuple[int, int], torch.cuda.CUDAGraph] = {}
        self.prefix_plans: dict[tuple[int, int], ProductChoice] = {}  # each prefix graph's product kernel
        self.prefix_pass_ms: dict[tuple[int, int], float] = {}  # measured after recording; prefix_passes uses it
        self.graph_pool = torch.cuda.graph_pool_handle()  # this path's own (module docstring)
        self.start = torch.cuda.Event(enable_timing=True)
        self.end = torch.cuda.Event(enable_timing=True)
        self.record_seconds = 0.0
        # GPU memory reserved before the timing, after it, and after returning the timing's memory to the GPU (record)
        self.timing_reserved_gb: dict[str, float] = {}
        self.record()

    def _views(self, b: int, n: int) -> _Inputs:
        meta = self.inp[:META * b].view(META, b)
        return _Inputs(meta[0], meta[1], meta[2], meta[3], meta[4], self.inp[META * b:META * b + b * n])

    def _prefix_width(self, nb: int) -> int:
        return self.pool.fixed_pages + nb // PAGE_TOKENS

    def _prefix_size(self, b: int, nb: int) -> int:
        return (PREFIX_META + self._prefix_width(nb) + nb) * b

    def _prefix_views(self, b: int, nb: int) -> _PrefixInputs:
        w = self._prefix_width(nb)
        meta = self.prefix_inp[:PREFIX_META * b].view(PREFIX_META, b)
        pages = self.prefix_inp[PREFIX_META * b:(PREFIX_META + w) * b].view(b, w)
        return _PrefixInputs(meta[0], meta[1], pages, self.prefix_inp[(PREFIX_META + w) * b:self._prefix_size(b, nb)])

    # ------------------------------------------------------------ the pass

    def body(self, b: int, n: int, plan: PassPlan) -> torch.Tensor:
        """The recorded pass: B rows of N tokens from the packed input buffer. Returns the option probabilities (B,
        255), zero past each row's count."""
        fast, pool = self.fast, self.pool
        v = self.inputs[(b, n)]
        ws = self.workspaces[(b, n)]
        t = b * n
        product = _product(plan.products.kernel)
        tables = pool.tables.index_select(0, v.slots)
        x = fast.embed.index_select(0, v.ids)
        kn = torch.empty(t, NKV, HD, device="cuda", dtype=DT)  # the pass's new keys and values, layer after layer
        vn = torch.empty_like(kn)
        d: torch.Tensor | None = None
        gi = ai = 0
        for layer in fast.layers:
            x, h = add_rms(x, d, layer["norm_in"])
            pr = product(layer["proj"], h, ws, v.adapters)
            if layer["kind"] == "linear_attention":
                q, k, vv, g, beta = gdn_prep_rows(pr, pool, tables, gi, layer["conv"], layer["A"], layer["dt_bias"], n)
                o = self._gdn(plan.gdn, q, k, vv, g, beta, tables, gi, v.last, b)
                y = gated_norm(o.reshape(t, NV, DV), pr, layer["gnorm"])
                gi += 1
            else:
                q = attn_prep_rows(pr, layer["q_norm"], layer["k_norm"], fast.inv_freq, v.pos, kn, vn, n)
                o = paged_attention(q, pool, tables, ai, v.pos, kn, vn, n, splits=plan.splits)
                y = gate_mul(o.view(t, NQ, HD).permute(1, 0, 2)[None], pr, t)
                ai += 1
            d = product(layer["out"], y, ws, v.adapters)
            x, h = add_rms(x, d, layer["norm_post"])
            d = product(layer["mlp_down"], silu_mul(product(layer["mlp_in"], h, ws, v.adapters)), ws, v.adapters)
        if d is None:
            raise ValueError("The fast path's model has no layers")
        at = self.row_starts[(b, n)] + v.last  # each row's last real token
        hidden = rms(x.index_select(0, at) + d.index_select(0, at), fast.final_norm).contiguous()
        return readout_rows(hidden, fast.readout_pool, fast.temp_pool, v.adapters, v.counts)

    def _gdn(self, kernel: str, q: torch.Tensor, k: torch.Tensor, v: torch.Tensor, g: torch.Tensor, beta: torch.Tensor,
             tables: torch.Tensor, gdn_layer: int, last: torch.Tensor, b: int) -> torch.Tensor:
        if kernel == "recurrent":
            return gdn_recurrent_slots(q, k, v, g, beta, self.pool, tables, gdn_layer, last=last)
        if kernel == "chunk":
            from fla.ops.gated_delta_rule import chunk_gated_delta_rule

            h0 = gather_gdn_states(self.pool, tables, gdn_layer, b)
            o, _ = chunk_gated_delta_rule(q, k, v, g, beta, scale=DK ** -0.5, initial_state=h0,
                                          output_final_state=False, use_qk_l2norm_in_kernel=False)
            return o
        raise ValueError(f"Unknown Gated DeltaNet kernel {kernel!r}; expected one of {GDN_KERNELS}")

    def prefix_body(self, b: int, nb: int, kernel: str) -> None:
        """The recorded batched prefix pass: B prefixes of at most nb tokens each (row r: ids[r * nb:][:lengths[r]],
        then padding), each from an empty state with its own adapter, written into the pages its row of the input lists
        (the slot's block table, then page 0), the fused products computed by product kernel `kernel`. Row by row the
        same computation as the single-prefix pass before 2b-1: padding tokens get decay 0 and update strength 0, so
        each row's recurrent state after them is its state after its last real token; the convolution inputs kept are
        each row's last three real ones (copied by the input-side kernel); the keys and values of tokens past the row's
        last page go to page 0, which no prefix owns. Padding rows (adapter 0, length 0) list page 0 only."""
        from fla.ops.gated_delta_rule import chunk_gated_delta_rule

        fast, pool = self.fast, self.pool
        v = self.prefix_inputs[(b, nb)]
        ws = self.prefix_workspaces[(b, nb)]
        t = b * nb
        product = _product(kernel)
        shapes = pool.shapes
        s, c, fixed = shapes.state_pages, shapes.conv_pages, pool.fixed_pages
        kv_pages = nb // PAGE_TOKENS
        x = fast.embed.index_select(0, v.ids)
        conv = torch.empty(b, shapes.gdn_layers, *shapes.conv, device="cuda", dtype=DT)  # filled by gdn_prep_rows
        # every attention layer's keys and values, written in place by the attention input kernel
        kv = torch.empty(shapes.attn_layers, 2, t, NKV, HD, device="cuda", dtype=DT)
        states = []
        d: torch.Tensor | None = None
        gi = ai = 0
        for i, layer in enumerate(fast.layers):
            x, h = add_rms(x, d, layer["norm_in"])
            pr = product(layer["proj"], h, ws, v.adapters)
            if layer["kind"] == "linear_attention":
                q, k, vv, g, beta = gdn_prep_rows(pr, pool, self.zero_tables[:b], gi, layer["conv"], layer["A"],
                                                  layer["dt_bias"], nb, real=v.lengths, tail=conv[:, gi])
                o, state = chunk_gated_delta_rule(q, k, vv, g, beta, scale=DK ** -0.5, initial_state=None,
                                                  output_final_state=True, use_qk_l2norm_in_kernel=False)
                states.append(state)
                y = gated_norm(o.reshape(t, NV, DV), pr, layer["gnorm"])
                gi += 1
            else:
                kn, vn = kv[ai, 0], kv[ai, 1]
                q = attn_prep_rows(pr, layer["q_norm"], layer["k_norm"], fast.inv_freq, self.zero_pos[:b], kn, vn, nb)
                ai += 1
                if i == len(fast.layers) - 1:
                    break  # the last layer: only its keys and values are needed
                o = F.scaled_dot_product_attention(q.view(b, nb, NQ, HD).transpose(1, 2),
                                                   kn.view(b, nb, NKV, HD).transpose(1, 2),
                                                   vn.view(b, nb, NKV, HD).transpose(1, 2), is_causal=True,
                                                   scale=HD ** -0.5, enable_gqa=True)
                y = gate_mul(o.transpose(1, 2).reshape(t, NQ, HD).permute(1, 0, 2)[None], pr, t)
            d = product(layer["out"], y, ws, v.adapters)
            x, h = add_rms(x, d, layer["norm_post"])
            d = product(layer["mlp_down"], silu_mul(product(layer["mlp_in"], h, ws, v.adapters)), ws, v.adapters)
        if fast.kinds[-1] != "full_attention":
            raise ValueError("The batched prefix pass expects the last layer to be an attention layer")
        # into the pages (PrefixPool.write's layout, for B prefixes at once)
        pages = v.pages
        state_all = torch.stack(states, 1).float()  # (B, layers, heads, key dim, value dim)
        pool.state.index_copy_(0, pages[:, :s].reshape(-1), state_all.reshape(b * s, *pool.state.shape[1:]))
        pool.conv.index_copy_(0, pages[:, s:s + c].reshape(-1), conv.reshape(b * c, *pool.conv.shape[1:]))
        kv_rows = kv.view(shapes.attn_layers, 2, b, kv_pages, PAGE_TOKENS, *shapes.kv).permute(2, 3, 0, 1, 4, 5, 6)
        pool.kv.index_copy_(0, pages[:, fixed:].reshape(-1), kv_rows.reshape(b * kv_pages, *pool.kv.shape[1:]))

    # ------------------------------------------------------------ recording

    def record(self) -> None:
        """Choose every pass's settings by timing (choose; choose_prefix for the prefix graphs' product kernel), then
        record every pass's graph into this path's own graph memory pool. All timing comes before the first capture
        into that pool (see "Graph memory" in the module docstring for why)."""
        started = time.perf_counter()
        fast = self.fast
        stream = torch.cuda.Stream()
        stream.wait_stream(torch.cuda.current_stream())
        reserved_before = torch.cuda.memory_reserved()
        with torch.inference_mode():
            for b, n in PASS_SHAPES:
                plan = self.choose(b, n, stream)
                self.plans[(b, n)] = plan
                log.info("batched pass B=%d N=%d: Gated DeltaNet %s (%s), attention split %d (%s), products %s (%s)", b,
                         n, plan.gdn, _ms(plan.gdn_ms), plan.splits, _ms(plan.attention_ms), plan.products.kernel,
                         _ms(plan.products.ms))
            for b, nb in self.prefix_shapes:
                choice = self.choose_prefix(b, nb, stream)
                self.prefix_plans[(b, nb)] = choice
                log.info("batched prefix pass B=%d prefix bucket %d: products %s (%s)", b, nb, choice.kernel,
                         _ms(choice.ms))
            torch.cuda.synchronize()
            # The timing graphs and inputs are gone, but PyTorch's allocator still holds their memory: hand it back to
            # the GPU before recording, so other processes on a shared GPU can have it. Still before any capture.
            reserved_after_timing = torch.cuda.memory_reserved()
            torch.cuda.empty_cache()
            self.timing_reserved_gb = {"before_timing": round(reserved_before / 2**30, 3),
                                       "after_timing": round(reserved_after_timing / 2**30, 3),
                                       "after_release": round(torch.cuda.memory_reserved() / 2**30, 3)}
            log.info("batched fast path timing: GPU memory reserved %s GB", self.timing_reserved_gb)
            for b, n in PASS_SHAPES:
                plan = self.plans[(b, n)]
                v = self.inputs[(b, n)]
                for field in (v.slots, v.adapters, v.pos):
                    field.fill_(0)
                v.last.fill_(n - 1)
                v.counts.fill_(2)
                v.ids.fill_(fast.pad_id)
                stream.wait_stream(torch.cuda.current_stream())  # wait for this iteration's fills, not just the last
                with torch.cuda.stream(stream):
                    for _ in range(2):  # compile and warm up outside the capture
                        self.body(b, n, plan)
                stream.synchronize()
                graph = torch.cuda.CUDAGraph()
                with torch.cuda.graph(graph, pool=self.graph_pool, stream=stream):
                    out = self.body(b, n, plan)
                self.graphs[(b, n)] = (graph, out)
            for b, nb in self.prefix_shapes:
                self._prefix_padding(b, nb)
                stream.wait_stream(torch.cuda.current_stream())  # wait for this iteration's padding, not just the last
                with torch.cuda.stream(stream):
                    for _ in range(2):
                        self.prefix_body(b, nb, self.prefix_plans[(b, nb)].kernel)
                stream.synchronize()
                graph = torch.cuda.CUDAGraph()
                with torch.cuda.graph(graph, pool=self.graph_pool, stream=stream):
                    self.prefix_body(b, nb, self.prefix_plans[(b, nb)].kernel)
                self.prefix_graphs[(b, nb)] = graph
        torch.cuda.current_stream().wait_stream(stream)
        torch.cuda.synchronize()
        self.prefix_pass_ms = {shape: self._replay_ms(*shape) for shape in self.prefix_graphs}
        self.record_seconds = time.perf_counter() - started
        log.info("batched fast path: %d decision and %d prefix CUDA graphs recorded in %.1f s; GPU memory allocated "
                 "%.2f GB, reserved %.2f GB", len(self.graphs), len(self.prefix_graphs), self.record_seconds,
                 torch.cuda.memory_allocated() / 2**30, torch.cuda.memory_reserved() / 2**30)

    def _prefix_padding(self, b: int, nb: int) -> None:
        """The (b, nb) prefix pass's inputs as padding rows only (the base, nb // 2 pad tokens, every page 0), so the
        pass writes page 0 only. Every layout shares one packed buffer, so this is set again before each use."""
        pv = self.prefix_inputs[(b, nb)]
        pv.adapters.fill_(0)
        pv.lengths.fill_(nb // 2)
        pv.pages.fill_(SINK_PAGE)
        pv.ids.fill_(self.fast.pad_id)

    def _replay_ms(self, b: int, nb: int, reps: int = 5) -> float:
        """Median GPU time of the recorded (b, nb) prefix graph on padding rows only. Replays of graphs already
        recorded: no graph is created or destroyed (see "Graph memory")."""
        self._prefix_padding(b, nb)
        graph = self.prefix_graphs[(b, nb)]
        samples = []
        for i in range(reps + 1):
            self.start.record()
            graph.replay()
            self.end.record()
            self.end.synchronize()
            if i:
                samples.append(self.start.elapsed_time(self.end))
        return round(statistics.median(samples), 4)

    def choose(self, b: int, n: int, stream: torch.cuda.Stream) -> PassPlan:
        """Time both Gated DeltaNet kernels (the 18 layers of the pass) and every attention split allowed for its size
        (the 6 layers), each as its own CUDA graph on synthetic rows: prefixes of TIMING_PREFIX_TOKENS tokens on
        distinct pages, every row's last token real; and, on an INT4 base, both product kernels (_choose_product). The
        fastest of each is the plan."""
        pool = self.pool
        t = b * n
        gen = torch.Generator(device="cuda").manual_seed(b * 10007 + n)
        tables = torch.zeros(b, pool.tables.shape[1], device="cuda", dtype=torch.long)
        used = pool.fixed_pages + -(-TIMING_PREFIX_TOKENS // pool.page_tokens)
        spread = torch.arange(b * used, device="cuda").view(b, used) % (pool.pages - 2) + 2  # pages 0, 1 reserved
        tables[:, :used] = spread
        pos = torch.full((b,), TIMING_PREFIX_TOKENS, device="cuda", dtype=torch.int32)
        last = torch.full((b,), n - 1, device="cuda", dtype=torch.int32)

        def unit(*shape: int) -> torch.Tensor:
            x = torch.randn(*shape, device="cuda", generator=gen)
            return (x / x.norm(dim=-1, keepdim=True)).to(DT)

        q, k = unit(b, n, NV, DK), unit(b, n, NV, DK)
        v = torch.randn(b, n, NV, DV, device="cuda", generator=gen).to(DT)
        g = -0.5 * torch.nn.functional.softplus(torch.randn(b, n, NV, device="cuda", generator=gen))
        beta = torch.sigmoid(torch.randn(b, n, NV, device="cuda", generator=gen)).to(DT)
        gdn_layers = len(self.fast.gdn_layers)

        def gdn(kernel: str) -> list[torch.Tensor]:
            return [self._gdn(kernel, q, k, v, g, beta, tables, gi, last, b) for gi in range(gdn_layers)]

        gdn_ms = {kernel: _graph_ms(partial(gdn, kernel), stream, t) for kernel in GDN_KERNELS}
        del q, k, v, g, beta
        aq = torch.randn(t, NQ, HD, device="cuda", generator=gen).to(DT)
        kn = torch.randn(t, NKV, HD, device="cuda", generator=gen).to(DT)
        vn = torch.randn(t, NKV, HD, device="cuda", generator=gen).to(DT)
        attn_layers = len(self.fast.attn_layers)

        def attention(splits: int) -> list[torch.Tensor]:
            return [paged_attention(aq, pool, tables, ai, pos, kn, vn, n, splits=splits) for ai in range(attn_layers)]

        attention_ms = {s: _graph_ms(partial(attention, s), stream, t)
                        for s in SPLITS if s == 1 or s * t <= MAX_SPLIT_TOKENS}
        del aq, kn, vn
        products = self._choose_product(b, t, self.workspaces[(b, n)], stream)
        return PassPlan(gdn=min(gdn_ms, key=gdn_ms.__getitem__), splits=min(attention_ms, key=attention_ms.__getitem__),
                        gdn_ms=gdn_ms, attention_ms=attention_ms, products=products)

    def choose_prefix(self, b: int, nb: int, stream: torch.cuda.Stream) -> ProductChoice:
        """The (b, nb) prefix graph's product kernel (_choose_product; on a bfloat16 base cuBLAS, untimed)."""
        return self._choose_product(b, b * nb, self.prefix_workspaces[(b, nb)], stream)

    def _choose_product(self, b: int, t: int, ws: Workspace, stream: torch.cuda.Stream) -> ProductChoice:
        """The product kernel of a pass of b rows and t tokens (product_choice.choose_product among self.products): each
        kernel recorded once as a CUDA graph of the pass's fused products with their LoRA updates, every fused group of
        every layer once, in layer order (as the pass reads them, so the weights come from memory), on random inputs,
        the rows' adapters cycling through every row of the adapter pools; the graphs then timed in alternating pairs
        (choose_product). With one candidate (a bfloat16 base) nothing is built or timed."""
        fast = self.fast

        @cache
        def inputs() -> tuple[dict[int, torch.Tensor], torch.Tensor]:
            """Built on the first timing only: (each input width's (t, width) input, each row's adapter index)."""
            gen = torch.Generator(device="cuda").manual_seed(t * 10007 + b)
            xs = {k: torch.randn(t, k, device="cuda", generator=gen).to(DT) for k in sorted({x.k for x in fast.lins})}
            return xs, torch.arange(b, device="cuda", dtype=torch.int32) % (fast.max_adapters + 1)

        graphs: dict[str, torch.cuda.CUDAGraph] = {}

        def time_ms(kernel: str) -> float:
            """One median replay time of the kernel's timing graph, recorded on its first timing."""
            if kernel not in graphs:
                xs, adapters = inputs()
                product = _product(kernel)

                def products() -> None:
                    for lin in fast.lins:
                        product(lin, xs[lin.k], ws, adapters)  # each result dropped, so its memory is reused

                graphs[kernel] = _timing_graph(products, stream)
            return _replay_ms(graphs[kernel], stream, t)

        choice = choose_product(self.products, time_ms)
        graphs.clear()
        return choice

    # ------------------------------------------------------------ requests

    def decide(self, rows: Sequence[tuple[int, int, Row]]) -> tuple[list[list[float]], list[int], dict[str, Any]]:
        """Each row's option probabilities (temperature applied), each row's input tokens (prefix + new), and timing.

        rows: (adapter index in the pools, prefix slot holding that adapter's prefix of the row, prepared row), the
        slots pinned by the caller for the call. Rows are grouped by new-token bucket and cut into passes of at most
        max_rows(N) rows, each run in the smallest recorded batch bucket that holds it; results come back in the rows'
        order.

        timing: model_ms (GPU time of the passes, copies included), new_tokens (real new tokens), passes, and per row
        (lists in the rows' order): batch_rows (real rows in its pass), bucket (its pass's N) and adapters_in_pass
        (distinct adapters in its pass).

        The request token limit (max_tokens, prefix + new tokens) is not checked here: that is the caller's job, before
        any work. Never call this concurrently with ensure_prefixes or an adapter change (shared pools and stream; see
        the module docstring)."""
        lengths = self._check(rows)
        by_bucket: dict[int, list[int]] = {}
        for i, (_, _, row) in enumerate(rows):
            by_bucket.setdefault(next(b for b in NEW_BUCKETS if b >= len(row.new_ids)), []).append(i)
        results: list[list[float]] = [[] for _ in rows]
        batch_rows, bucket, adapters_in_pass = [0] * len(rows), [0] * len(rows), [0] * len(rows)
        timing: dict[str, Any] = {"model_ms": 0.0, "new_tokens": sum(len(row.new_ids) for _, _, row in rows),
                                  "passes": 0}
        for n, members in sorted(by_bucket.items()):
            cap = max_rows(n)
            for first in range(0, len(members), cap):
                part = members[first:first + cap]
                timing["model_ms"] += self._pass(rows, lengths, part, n, results)
                timing["passes"] += 1
                adapters = len({rows[i][0] for i in part})
                for i in part:
                    batch_rows[i], bucket[i], adapters_in_pass[i] = len(part), n, adapters
        timing.update(batch_rows=batch_rows, bucket=bucket, adapters_in_pass=adapters_in_pass)
        tokens = [lengths[i] + len(row.new_ids) for i, (_, _, row) in enumerate(rows)]
        return results, tokens, timing

    def _check(self, rows: Sequence[tuple[int, int, Row]]) -> list[int]:
        """Each row's prefix length, after refusing (ValueError) a row the graphs cannot answer as asked."""
        loaded = set(self.fast.adapter_index.values())
        lengths = []
        for i, (adapter, slot, row) in enumerate(rows):
            if adapter not in loaded:
                raise ValueError(f"Row {i}: adapter row {adapter} holds no loaded adapter (loaded rows: "
                                 f"{sorted(loaded)})")
            if self.pool.key(slot) != (adapter, row.prefix_text):
                raise ValueError(f"Row {i}: prefix slot {slot} holds another prefix than the row's under adapter row "
                                 f"{adapter} (it holds adapter row {self.pool.key(slot)[0]}'s)")
            if not 1 <= len(row.new_ids) <= max(NEW_BUCKETS):
                raise ValueError(f"Row {i}: {len(row.new_ids)} new tokens; the batched path takes 1 to "
                                 f"{max(NEW_BUCKETS)}")
            if not 1 <= row.count <= READOUT_ROWS:
                raise ValueError(f"Row {i}: {row.count} answer options; the readout has 1 to {READOUT_ROWS}")
            lengths.append(self.pool.length(slot))
        return lengths

    def _pass(self, rows: Sequence[tuple[int, int, Row]], lengths: list[int], part: list[int], n: int,
              results: list[list[float]]) -> float:
        """Run rows[part] (all in new-token bucket n) as one pass; fills their results, returns the pass's GPU ms."""
        b = next(b for b in BATCH_BUCKETS if b >= len(part))
        graph, out = self.graphs[(b, n)]
        size = META * b + b * n
        meta = self.inp_np[:META * b].reshape(META, b)
        ids = self.inp_np[META * b:size]
        meta[:4] = 0  # padding rows: slot 0 (the empty prefix), adapter 0 (the base), prefix length 0, one token
        meta[4] = 1  # and one option
        ids[:] = self.fast.pad_id
        for j, i in enumerate(part):
            adapter, slot, row = rows[i]
            real = len(row.new_ids)
            if real > n:
                raise AssertionError(f"Row {i}: {real} new tokens staged in a pass of {n}")
            ids[j * n:j * n + real] = row.new_ids
            meta[:, j] = (slot, adapter, lengths[i], real - 1, row.count)
        self.start.record()
        self.inp[:size].copy_(self.inp_host[:size], non_blocking=True)
        graph.replay()
        self.out_host[:b].copy_(out, non_blocking=True)
        self.end.record()
        self.end.synchronize()
        for j, i in enumerate(part):
            results[i] = self.out_host[j, :rows[i][2].count].tolist()
        return self.start.elapsed_time(self.end)

    # ------------------------------------------------------------ prefixes

    def ensure_prefixes(self, rows: Sequence[tuple[int, str]], max_tokens: int) -> tuple[list[int], dict[str, Any]]:
        """Each row's prefix slot, every slot pinned once per row (the caller unpins `slots` when its passes are over,
        in a finally), and timing.

        rows: (adapter index in the pools, prefix text). A prefix not in the pool gets a slot and is processed by a
        batched prefix pass: the missing prefixes are grouped by prefix bucket and each group cut into passes of
        recorded sizes (prefix_passes). Rows with the same missing (adapter, prefix text) share one slot, computed once.

        Before any change to the pool, a prefix longer than the longest prefix or than max_tokens - 1 tokens is refused
        (ValueError). Found prefixes are pinned before any new one is reserved, and each new one
        right after its reservation, so no reservation of this call evicts a slot the call returns; when the pool
        cannot make room, PrefixPool.reserve raises EngineError. On any failure, every pin of this call is undone and
        every reserved slot not yet written is released, then the error is raised.

        timing: prefix_ms (GPU time of the prefix passes), prefix_misses (prefixes processed), prefix_hits (rows whose
        prefix was in the pool already), prefix_shared (rows whose new prefix an earlier row of this call brought, so
        it was processed once for both: prefix_hits + prefix_misses + prefix_shared = rows), prefix_passes."""
        fast, pool = self.fast, self.pool
        loaded = set(fast.adapter_index.values())
        keys = [(adapter, text) for adapter, text in rows]
        for i, (adapter, _) in enumerate(keys):
            if adapter not in loaded:
                raise ValueError(f"Row {i}: adapter row {adapter} holds no loaded adapter (loaded rows: "
                                 f"{sorted(loaded)})")
        new_ids: dict[tuple[int, str], list[int]] = {}
        for adapter, text in keys:
            if (adapter, text) in new_ids or pool.lookup(adapter, text) is not None:
                continue
            ids = list(fast.tokenizer(text, add_special_tokens=False)["input_ids"])
            if len(ids) > LONGEST_PREFIX_TOKENS:
                raise ValueError(f"The question's fixed part has {len(ids)} tokens; the fast path holds at most "
                                 f"{LONGEST_PREFIX_TOKENS}")
            if len(ids) >= max_tokens:
                raise ValueError(f"Question branch exceeds the {max_tokens}-token limit; no input was truncated.")
            new_ids[(adapter, text)] = ids
        timing: dict[str, Any] = {"prefix_ms": 0.0, "prefix_misses": len(new_ids), "prefix_hits": 0,
                                  "prefix_shared": sum(1 for key in keys if key in new_ids) - len(new_ids),
                                  "prefix_passes": 0}
        pinned: list[int] = []
        found: dict[tuple[int, str], int] = {}
        reserved: dict[tuple[int, str], int] = {}
        written: set[int] = set()
        try:
            for key in keys:  # found prefixes pinned first, so no reservation below evicts them
                if key in new_ids:
                    continue
                timing["prefix_hits"] += 1
                if key not in found:
                    slot = pool.lookup(*key)
                    if slot is None:
                        raise RuntimeError(f"Prefix of adapter row {key[0]} left the pool before any reservation")
                    pool.pin([slot])
                    pinned.append(slot)
                    found[key] = slot
            for key, ids in new_ids.items():
                slot = pool.reserve(key[0], key[1], len(ids))
                reserved[key] = slot
                pool.pin([slot])
                pinned.append(slot)
            slots = [found[key] if key in found else reserved[key] for key in keys]
            again, seen = [], set()  # a second row of one slot pins it again: one pin per row
            for slot in slots:
                if slot in seen:
                    again.append(slot)
                seen.add(slot)
            pool.pin(again)
            pinned.extend(again)
            by_bucket: dict[int, list[tuple[int, str]]] = {}
            for key, ids in new_ids.items():
                by_bucket.setdefault(next(nb for nb in PREFIX_BUCKETS if nb >= len(ids)), []).append(key)
            with torch.inference_mode():
                for nb, members in sorted(by_bucket.items()):
                    first = 0
                    for b in self.prefix_passes(nb, len(members)):
                        part = members[first:first + b]
                        first += b
                        timing["prefix_ms"] += self._prefix_pass(b, nb, [(reserved[k], k[0], new_ids[k]) for k in part])
                        timing["prefix_passes"] += 1
                        written.update(reserved[k] for k in part)
        except BaseException:
            pool.unpin(pinned)
            for slot in reserved.values():
                if slot not in written:
                    pool.release(slot)  # its pages were not written: the key must not find them
            raise
        return slots, timing

    def prefix_passes(self, nb: int, count: int) -> list[int]:
        """The batch buckets of the passes that process `count` new prefixes of prefix bucket nb: the combination of
        recorded graphs with the smallest total measured time (prefix_pass_ms), largest passes first; the last pass may
        hold padding rows."""
        sizes = [b for b, n in self.prefix_shapes if n == nb]
        best = [0.0] + [float("inf")] * count
        choice = [0] * (count + 1)
        for k in range(1, count + 1):
            for b in sizes:
                cost = best[max(0, k - b)] + self.prefix_pass_ms[(b, nb)]
                if cost < best[k]:
                    best[k], choice[k] = cost, b
        out = []
        while count > 0:
            out.append(choice[count])
            count = max(0, count - choice[count])
        return sorted(out, reverse=True)

    def _prefix_pass(self, b: int, nb: int, part: list[tuple[int, int, list[int]]]) -> float:
        """Process part = [(reserved slot, adapter index, prefix ids)] (each at most nb ids) in the (b, nb) prefix
        graph, the rest of its rows padding (the base, no tokens, page 0 only); returns the pass's GPU ms (copy and
        replay)."""
        graph = self.prefix_graphs[(b, nb)]
        w = self._prefix_width(nb)
        size = self._prefix_size(b, nb)
        meta = self.prefix_inp_np[:PREFIX_META * b].reshape(PREFIX_META, b)
        pages = self.prefix_inp_np[PREFIX_META * b:(PREFIX_META + w) * b].reshape(b, w)
        ids = self.prefix_inp_np[(PREFIX_META + w) * b:size]
        meta[:] = 0  # padding rows: adapter 0 (the base), no tokens, every page 0
        pages[:] = SINK_PAGE
        ids[:] = self.fast.pad_id
        for j, (slot, adapter, toks) in enumerate(part):
            table = self.pool.block_table(slot)
            if len(toks) > nb or len(table) != self.pool.shapes.pages_per_prefix(len(toks)):
                raise AssertionError(f"Slot {slot}: a prefix of {len(toks)} tokens with {len(table)} pages staged in a "
                                     f"pass of {nb}")
            pages[j, :len(table)] = table
            ids[j * nb:j * nb + len(toks)] = toks
            meta[:, j] = (adapter, len(toks))
        self.start.record()
        self.prefix_inp[:size].copy_(self.prefix_inp_host[:size], non_blocking=True)
        graph.replay()
        self.end.record()
        self.end.synchronize()
        return self.start.elapsed_time(self.end)

    def describe(self) -> dict[str, object]:
        return {"graphs": len(self.graphs), "prefix_graphs": len(self.prefix_graphs),
                "record_seconds": round(self.record_seconds, 1),
                "plans": {f"{b}x{n}": {"gdn": p.gdn, "splits": p.splits, "products": p.products.kernel}
                          for (b, n), p in self.plans.items()},
                "prefix_products": {f"{b}x{nb}": c.kernel for (b, nb), c in self.prefix_plans.items()},
                "prefix_pass_rows": dict(PREFIX_PASS_ROWS)}


def _graph_ms(work: Callable[[], object], stream: torch.cuda.Stream, tokens: int) -> float:
    """Median GPU time of one replay of `work` recorded as a CUDA graph (its own memory pool, freed after)."""
    graph = _timing_graph(work, stream)
    ms = _replay_ms(graph, stream, tokens)
    del graph
    return ms


def _timing_graph(work: Callable[[], object], stream: torch.cuda.Stream) -> torch.cuda.CUDAGraph:
    """`work` run twice (compile, warm up), then recorded as a CUDA graph with its own memory pool (freed with the
    graph). `work` reads tensors built on the caller's current stream (choose's timing inputs); the side stream must
    wait for that work before it reads them, or, with the GPU busy, it can read them before they are written (an
    illegal memory access; Risk 5 of the 2b-2 design)."""
    stream.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(stream):
        for _ in range(2):
            work()
    stream.synchronize()
    graph = torch.cuda.CUDAGraph()
    with torch.cuda.graph(graph, stream=stream):
        work()
    return graph


def _replay_ms(graph: torch.cuda.CUDAGraph, stream: torch.cuda.Stream, tokens: int) -> float:
    """Median GPU time of one replay of a timing graph (3 replays first, unmeasured)."""
    reps = 50 if tokens <= 1024 else 10
    start, end = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
    samples = []
    with torch.cuda.stream(stream):
        for i in range(reps + 3):
            start.record()
            graph.replay()
            end.record()
            end.synchronize()
            if i >= 3:
                samples.append(start.elapsed_time(end))
    return round(statistics.median(samples), 4)


def _ms(timings: dict[Any, float]) -> str:
    return ", ".join(f"{k}: {v:.3f} ms" for k, v in timings.items())
