"""Task 3: ways to stage a cached 1,000-token prefix (32 pages) for today's single-row decision graphs, timed alone
(wall clock per staging, synchronised, median of 2,000 after 200 warm-up), with the pool's real tensor shapes:
- before: the pre-pool `Prefix` copies (conv, state, keys and values from one contiguous tensor);
- list_index: the pages as a new GPU tensor from a Python list (a blocking copy), gather, copy;
- pinned_index: the pages through a pinned host buffer (an asynchronous copy), gather, copy;
- pinned_out: as pinned_index, but each layer's keys and values gathered straight into the staging buffer (12 gathers);
- slice: one slice copy, possible only when the pages are consecutive;
- triton_list / triton_pinned: one Triton kernel copying each page straight to its rows of the staging buffer, the
  pages given as a new GPU tensor from a list, or through the pinned host buffer.
Usage (on datigator): .venv/bin/python results/step2b1/task3/bench_stage.py OUT.json"""
import json
import statistics
import sys
import time

import torch
import triton
import triton.language as tl

P, N = 1000, 32
dev = "cuda"
conv_pool = torch.randn(87, 18, 3, 6144, device=dev).to(torch.bfloat16)
state_pool = torch.randn(87, 18, 16, 128, 128, device=dev)
kv_pool = torch.randn(6, 2, 17476, 32, 2, 256, device=dev).to(torch.bfloat16)
conv = torch.zeros(18, 3, 6144, device=dev, dtype=torch.bfloat16)
state = torch.zeros(18, 16, 128, 128, device=dev)
kv = torch.zeros(6, 2, 9216, 2, 256, device=dev, dtype=torch.bfloat16)
prefix_kv = torch.randn(6, 2, P, 2, 256, device=dev).to(torch.bfloat16)
pages = list(range(4000, 4000 + 2 * N, 2))  # not consecutive
run = list(range(4000, 4000 + N))
host = torch.zeros(256, dtype=torch.long).pin_memory()
host_np = host.numpy()
index = torch.zeros(256, dtype=torch.long, device=dev)
slot = 5


def before() -> None:
    conv.copy_(conv_pool[slot]); state.copy_(state_pool[slot]); kv[:, :, :P].copy_(prefix_kv)


def gather_copy(idx: torch.Tensor) -> None:
    kv[:, :, :N * 32].view(6, 2, N, 32, 2, 256).copy_(kv_pool.index_select(2, idx))


def list_index() -> None:
    conv.copy_(conv_pool[slot]); state.copy_(state_pool[slot])
    gather_copy(torch.tensor(pages, dtype=torch.long, device=dev))


def pinned() -> torch.Tensor:
    host_np[:N] = pages
    index[:N].copy_(host[:N], non_blocking=True)
    return index[:N]


def pinned_index() -> None:
    conv.copy_(conv_pool[slot]); state.copy_(state_pool[slot])
    gather_copy(pinned())


def pinned_out() -> None:
    conv.copy_(conv_pool[slot]); state.copy_(state_pool[slot])
    idx = pinned()
    for layer in range(6):
        for j in range(2):
            torch.index_select(kv_pool[layer, j], 0, idx, out=kv[layer, j, :N * 32].view(N, 32, 2, 256))


def slice_copy() -> None:
    conv.copy_(conv_pool[slot]); state.copy_(state_pool[slot])
    kv[:, :, :N * 32].view(6, 2, N, 32, 2, 256).copy_(kv_pool[:, :, run[0]:run[0] + N])


@triton.jit
def _stage_kernel(SRC, s_src, DST, s_dst, PAGES, PAGE_ELEMS: tl.constexpr, BLOCK: tl.constexpr):
    i = tl.program_id(0)
    lk = tl.program_id(1).to(tl.int64)
    b = tl.program_id(2)
    page = tl.load(PAGES + i).to(tl.int64)
    offs = b * BLOCK + tl.arange(0, BLOCK)
    x = tl.load(SRC + lk * s_src + page * PAGE_ELEMS + offs)
    tl.store(DST + lk * s_dst + i * PAGE_ELEMS + offs, x)


def triton_copy(idx: torch.Tensor) -> None:
    _stage_kernel[(N, 12, 16384 // 4096)](kv_pool, kv_pool.stride(1), kv, kv.stride(1), idx, PAGE_ELEMS=16384,
                                          BLOCK=4096, num_warps=8)


def triton_list() -> None:
    conv.copy_(conv_pool[slot]); state.copy_(state_pool[slot])
    triton_copy(torch.tensor(pages, dtype=torch.long, device=dev))


def triton_pinned() -> None:
    conv.copy_(conv_pool[slot]); state.copy_(state_pool[slot])
    triton_copy(pinned())


triton_list()
torch.cuda.synchronize()
ref = kv_pool[:, :, pages].reshape(6, 2, N * 32, 2, 256)
assert torch.equal(kv[:, :, :N * 32], ref)
out = {}
for name, fn in (("before", before), ("list_index", list_index), ("pinned_index", pinned_index),
                 ("pinned_out", pinned_out), ("slice", slice_copy), ("triton_list", triton_list),
                 ("triton_pinned", triton_pinned)) * 2:
    samples = []
    for i in range(2200):
        torch.cuda.synchronize()
        t = time.perf_counter()
        fn()
        torch.cuda.synchronize()
        if i >= 200:
            samples.append((time.perf_counter() - t) * 1e3)
    out.setdefault(name, []).append(round(statistics.median(samples), 4))
print(json.dumps(out))
open(sys.argv[1], "w").write(json.dumps({"stage_ms_p50_two_runs": out, "gpu": torch.cuda.get_device_name()}, indent=1))
