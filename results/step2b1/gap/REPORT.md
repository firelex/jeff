# The 1 ms gap between today's fast path and jeff-asic's benchmark (step 2b-1, Task 0)

Measured on datigator (RTX PRO 6000 Blackwell Max-Q, WSL2) on 2026-10-24, with the other Jeff servers (jeff-server,
jeff-public, fastserve, tdd-preview, the legal evaluation) resident on the GPU and left untouched.

**Answer: the gap is in our code, not in the environment.** jeff-asic's own bfloat16 benchmark, run in our
environment (`~/jeff-dev-next/.venv`: torch 2.14.0, Triton 3.8.0, fla 0.5.2, other servers resident), gives
2.27 / 2.46 / 2.77 / 3.54 ms at 32 / 64 / 128 / 256 new tokens, the same as jeff-asic's 2.30 / 2.49 / 2.79 / 3.69 ms
on an idle GPU in vLLM's environment. Today's fast path in the same environment gives 3.29 / 4.02 / 4.01 / 4.62 ms.
A kernel profile of both, in one process, accounts for the whole difference (0.99 ms of 1.0 at 32 tokens):

1. **The LoRA update kernels: +0.56 ms at 32 tokens, +0.86 ms at 128.** Fixed in **Task 2**.
2. **Attention through PyTorch SDPA with a boolean mask: +0.38 ms at 32 tokens, +0.33 ms at 128.** Fixed in
   **Task 5**, with Task 4's paged attention kernel.
3. Small PyTorch operations around the graph: +0.07 ms. Fixed in **Task 5**.

The library versions do not explain it. Torch 2.13 makes today's fast path **slower** (3.43 against 3.29 ms at 32
tokens, 5.18 against 4.62 at 256), and vLLM's copy of flash-linear-attention is no faster than fla 0.5.2. **No pin
changes:** torch 2.14.0 and fla 0.5.2 stay as they are in `runtime/python/pyproject.toml`.

## 1. Batch-1 time for each change

The setup is the same in every row: one decision after a cached 1,000-token prefix, nav adapter, 32 answer options,
1,000 timed runs after 100 warm-up runs. Each cell gives p50 / p99 in milliseconds.

**jeff-asic's benchmark** (`benchmark/gpu_int4_floor.py time --weights bf16 --fused`, wall clock as jeff-asic defines it:
ids copied in, one graph replay, probabilities copied out, synchronise):

| Environment (what changes) | 32 | 64 | 128 | 256 |
|---|---|---|---|---|
| jeff-asic's report (`~/qwen-rtx/.venv`, torch 2.13, vLLM's fla, idle GPU, 2026-10-17) | 2.30 / 2.34 | 2.49 / 2.53 | 2.79 / 2.83 | 3.69 / 3.73 |
| A. `~/jeff-dev-next/.venv`: torch 2.14.0, Triton 3.8.0, fla 0.5.2 | 2.27 / 2.32 | 2.46 / 2.50 | 2.77 / 2.80 | 3.54 / 3.57 |
| B. A with torch 2.13.0 and Triton 3.7.1 (`venv-t213`) | 2.26 / 2.30 | 2.46 / 2.50 | 2.77 / 2.80 | 3.57 / 3.62 |
| C. Replica of jeff-asic's environment (`venv-asic`), with fla 0.5.2 | 2.26 / 2.30 | 2.47 / 2.51 | 2.77 / 2.81 | 3.61 / 3.65 |
| D. C with vLLM 0.30.0's copy of fla, which is exactly jeff-asic's setup | 2.29 / 2.33 | 2.49 / 2.52 | 2.78 / 2.82 | 3.63 / 3.66 |

**Today's fast path** (`scripts/bench_fast_path.py`, the method of `2b1-prototypes/p0_baseline.py`; `FastPath.decide()`
wall clock, with 25 adapters loaded as in p0):

| Environment (what changes) | 32 | 64 | 128 | 256 |
|---|---|---|---|---|
| Design Section 1 (`~/jeff-dev-next/.venv`, 2026-10-24) | 3.30 / 3.35 | 4.02 / 4.07 | 4.01 / 4.06 | 4.63 / 4.68 |
| A. `~/jeff-dev-next/.venv`: torch 2.14.0, Triton 3.8.0, fla 0.5.2 | **3.29 / 3.36** | **4.02 / 4.09** | **4.01 / 4.08** | **4.62 / 4.69** |
| B. A with torch 2.13.0 and Triton 3.7.1 | 3.43 / 3.48 | 4.20 / 4.26 | 4.30 / 4.36 | 5.18 / 5.24 |
| C. Replica of jeff-asic's environment, with fla 0.5.2 | 3.43 / 3.49 | 4.21 / 4.28 | 4.30 / 4.36 | 5.21 / 5.27 |
| D. C with vLLM 0.30.0's fla patched in for `chunk_gated_delta_rule` | 3.47 / 3.55 | 4.23 / 4.29 | 4.32 / 4.37 | 5.23 / 5.30 |

Row A of the fast-path table is **the pre-2b-1 batch-1 reference (controller ruling A)**. The full record, with the bare
decision and GPU-only times, nvidia-smi samples and versions, is in `pre2b1_batch1.json`:

| New tokens | `decide()` p50 / p99 | bare decision p50 / p99 | GPU only p50 |
|---|---|---|---|
| 32 | 3.291 / 3.360 | 3.241 / 3.276 | 3.119 |
| 64 | 4.021 / 4.087 | 3.971 / 4.009 | 3.846 |
| 128 | 4.012 / 4.076 | 3.959 / 3.994 | 3.838 |
| 256 | 4.624 / 4.685 | 4.573 / 4.626 | 4.443 |

**Clock and power state.** All of these runs show the same state while timing: P1 at 2,332–2,340 MHz SM clock. That is
the 2,340 MHz jeff-asic recorded. Board power was 166–300 W, depending on the token count. Between runs the GPU
dropped to P8 at 180 MHz. The 100 warm-up runs bring it back to P1 before timing starts (only the first sample of each
fast-path count shows a lower clock). The clock state does not explain the gap.

**The other servers on the GPU.** nvidia-smi sampled the GPU for 5 s before each fast-path run, before our engine
loaded. Utilisation was 0% (mean and maximum) every time, with 33.8 GB resident. Our runs added about 6.5 GB. With the
servers resident, jeff-asic's benchmark is within 0.03 ms of jeff-asic's idle-GPU numbers at 32–128 tokens, and
0.15 ms faster at 256. At this load, the servers' effect is within run-to-run noise.

**Library versions.** These are the comparisons that change one thing at a time:
- **torch 2.14 against 2.13** (rows A and B): jeff-asic's kernels are unchanged (±0.03 ms). Today's fast path is
  0.14–0.56 ms slower on torch 2.13. Keep torch 2.14.0.
- **fla 0.5.2 against vLLM 0.30.0's copy** (rows C and D): within 0.03 ms for both programs, and vLLM's copy is never
  faster. Keep fla 0.5.2.
- Rows A and C also differ in Python (3.13 against 3.12) and transformers (5.17 against 5.18). They agree for
  jeff-asic's kernels, which is further evidence that the environment is not the cause.

## 2. The cause: a kernel profile of both programs

`scratch/p_kernels.py` loads jeff-asic's model and today's `FastPath`, both with the nav adapter, in one process
(environment A). It runs each decision body eagerly 20 times under `torch.profiler`. It sums kernel time by kind per
decision, and also times each graph replay with CUDA events.

| Kernel time per decision (ms) | 32: fast path | 32: jeff-asic | difference | 128: fast path | 128: jeff-asic | difference |
|---|---|---|---|---|---|---|
| LoRA update (2 kernels × 96 products) | 0.914 | 0.353 | **+0.562** | 1.288 | 0.423 | **+0.864** |
| Attention (6 layers) | 0.458 | 0.081 | **+0.377** | 0.467 | 0.142 | **+0.325** |
| Other PyTorch operations | 0.099 | 0.033 | +0.066 | 0.098 | 0.034 | +0.064 |
| Base matrix products (cuBLAS) | 0.977 | 0.988 | −0.011 | 1.063 | 1.078 | −0.015 |
| Gated DeltaNet (fla, 18 layers) | 0.416 | 0.417 | 0.000 | 0.572 | 0.571 | 0.000 |
| Fused element-wise kernels | 0.202 | 0.202 | 0.000 | 0.305 | 0.310 | −0.005 |
| **Total** | **3.066** | **2.073** | **+0.993** | **3.792** | **2.558** | **+1.234** |
| Graph replay, GPU only (p50) | 3.145 | 2.166 | +0.979 | 3.862 | 2.662 | +1.200 |

jeff-asic's LoRA kernels are `_w4_mid_kernel` and `_epilogue_kernel` (the profiler's raw category puts them under
"other", see `raw/kernels_dev.json`). They are counted as LoRA here.

### 2.1 The LoRA update (owner: Task 2)

Today, `_lora_mid_kernel` splits the input dimension so that about 64 programs run. Each program writes its own
float32 partial product of x·Aᵀ. Then every program of `_lora_out_kernel` (one per 64 output columns per row tile)
re-reads and sums **all** of those partials, in split order, before it multiplies by Bᵀ. This keeps the result
deterministic. But the GDN input projection has 8,224 outputs, so 129 programs each re-read up to 16 partials, and
MLP down (K = 3,584) has 56 splits. `_lora_out_kernel` averages 7.9 µs per launch, against 1.9 µs for jeff-asic's
`_epilogue_kernel`. jeff-asic instead adds the partials into one buffer with atomic additions (the summation order
then varies from run to run), so its epilogue reads one (M, R) tile.

The isolated kernels on the fast path's real shapes reproduce the profile (`scratch/p_lora.py`, `scratch/p_lora2.py`,
`raw/lora_kernels_dev.json`, `raw/lora_variants_dev.json`). Each figure is GPU ms per decision, 96 LoRA updates:

| Variant | 32 tokens | 128 tokens | Bit-for-bit equal to today |
|---|---|---|---|
| Today (split target 64, `_lora_out_kernel` 64 columns per program) | 0.876 | 1.254 | yes |
| Today's kernels, `_lora_out_kernel` 256 / 128 columns per program | 0.591 | 0.822 | yes |
| Partials summed once by a third small kernel, in split order | 0.696 | 0.742 | yes |
| Split target 8 with 256 / 128 columns per program | 0.503 | 0.625 | no (another summation order) |
| jeff-asic (atomic additions, so the summation order varies) | 0.330 | 0.412 | no, and not deterministic |

**The fix in Task 2.** Task 2 rewrites both LoRA kernels for the stacked pool anyway. It should also stop every
`_lora_out_kernel` program from re-reading all the partials. It should pick the column width and the split per shape,
by measurement on this GPU. The bit-for-bit variants recover 0.29 ms at 32 tokens and 0.43 ms at 128. Going further
means changing the summation order.

**Controller decision needed.** Task 2's first CUDA test requires batch-1 probabilities that are "bit-for-bit equal to
before this task". Today's split already depends on the row count, so the summation order is a choice, not a fixed
property. A smaller split (0.50 / 0.63 ms) would break that test, though the results stay deterministic. Atomic
additions, as jeff-asic does (0.33 / 0.41 ms), would break determinism. Either needs the controller's decision. The
agreement gate with the standard path would still apply.

### 2.2 Attention (owner: Task 5, with Task 4's kernel)

Today's `decide_body` calls `F.scaled_dot_product_attention` over all `cap + n` key rows (1,024 + n for a 1,000-token
prefix). It passes an explicit boolean mask (prefix rows in use, then causal over the new rows). With an arbitrary
mask, PyTorch cannot use flash attention, so it runs the memory-efficient CUTLASS kernel
(`fmha_cutlassF_bf16_aligned_32x128`): 76 µs per layer at 32 tokens. jeff-asic passes only the `start + n` real key
rows with `causal_lower_right`. That gets PyTorch's flash split-KV kernel plus its combine kernel: 13.5 µs per layer.

**The fix:** Task 5's batched graphs replace this call with Task 4's paged attention kernel, which reads the prefix in
place and splits the prefix walk at small batch (design Section 2.3). That kernel is the main reason the prototype's
batch 1 got faster (2.85 against 3.28 ms). Task 5's batch-1 test then measures the result. Today's single-row graphs
need no separate fix: after Task 7 they no longer serve, and ruling B removes them in Task 8.

### 2.3 Other PyTorch operations (owner: Task 5)

These are about 75 small kernels per decision (0.07 ms more than jeff-asic): building the attention mask in the
graph, a fill of the readout's 255 option rows and the masking, `index_select` on the last token, and similar. Task 5's
graphs read every per-row fact from one input buffer, and Task 4's `readout_rows` computes the readout per row, which
replaces these.

## 3. How it was run

- **Code.** jeff-asic's `benchmark/gpu_int4_floor.py`, `benchmark/gpu_int4_fused.py` and `src/jeffasic/hw_torch.py`
  were copied unchanged from the Mac (the same md5 as `~/jeff-asic/int4floor/` on datigator) to
  `~/jeff-2b-scratch/gap/asic/`. `gpu_int4_floor_pkgfla.py` changes one line of the floor script: it imports
  `chunk_gated_delta_rule` from the fla package instead of `vllm.third_party.flash_linear_attention`, so that it runs
  without vLLM. Prefix tokens come from jeff-asic's `~/jeff-asic/int4floor/reference.pt` (read only).
- **Environments.** All are under `~/jeff-2b-scratch/`. No existing environment was changed.
  - `venv-t213`: our lock's packages with torch 2.13.0, torchvision 0.28.0 and Triton 3.7.1
    (`scratch/build_t213.sh`, `scratch/reqs-t213.txt`).
  - `venv-asic`: Python 3.12, with `~/qwen-rtx/.venv`'s exact package list (`scratch/qwen-rtx-freeze.txt`: torch
    2.13.0, Triton 3.7.1, vLLM 0.30.0, transformers 5.18.0) plus fla 0.5.2 and peft 0.21.1. Our source is on the path
    through a `.pth` file (`scratch/build_asic.sh`).
  - `scratch/vllmfla_wrap.py` swaps vLLM's `chunk_gated_delta_rule` into the fast path.
- **Runs.** `scratch/run_asic.sh` and `scratch/run_fp.sh` ran the benchmarks, sequenced by `scratch/chain_e0.sh` and
  `scratch/chain_e1.sh`, in the background with logs, one at a time. During the jeff-asic runs, nvidia-smi sampled
  every 200 ms (`raw/asic_*.smi.csv`). The fast-path script samples by itself.
  - Row A of the fast path ran while `venv-asic` was being installed (downloads, CPU). It matches the design's numbers
    to 0.01 ms, so the install did not affect it.
- **Raw results:** `raw/asic_*.json`, `raw/fp_*.json`, `raw/kernels_dev.json` and `raw/lora_*.json`.
