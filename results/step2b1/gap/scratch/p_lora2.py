"""Deterministic variants of today's LoRA update (fast_path.lora_add), same shapes and timing as p_lora.py:
- reduce: _lora_mid_kernel writes the split partials as today; a small kernel sums them in split order into one
  (M, R) float32 buffer; _lora_out_kernel then reads that buffer once (NSPLIT = 1). Bit-for-bit equal to today.
- wide_bn: today's two kernels, _lora_out_kernel with 128 or 256 output columns per program (fewer programs
  re-reading the partials). Bit-for-bit equal to today (same summation order per element).
Each with split targets 64 (today), 16, 8."""
import json
import sys

import torch
import triton
import triton.language as tl

from jeff_runtime.engine import fast_path as F
from p_lora import SHAPES, gpu_us


@triton.jit
def _reduce_kernel(QP, Q, MR, NSPLIT, BLOCK: tl.constexpr):
    i = tl.program_id(0) * BLOCK + tl.arange(0, BLOCK)
    acc = tl.zeros([BLOCK], dtype=tl.float32)
    for s in range(NSPLIT):
        acc += tl.load(QP + s * MR + i, mask=i < MR, other=0.0)
    tl.store(Q + i, acc, mask=i < MR)


def lora_reduce(y, x, a, bt, ws, q):
    m, k = x.shape
    r = a.shape[0]
    split = ws.splits[k]
    F._lora_mid_kernel[(split, triton.cdiv(m, ws.bm))](x, x.stride(0), a, ws.QP, m, k, k // 64 // split, R=r, BM=ws.bm, BK=64, num_warps=4)
    _reduce_kernel[(triton.cdiv(m * r, 1024),)](ws.QP, q, m * r, split, BLOCK=1024, num_warps=4)
    ebm = min(max(ws.bm, 16), 64)
    F._lora_out_kernel[(triton.cdiv(y.shape[1], 64), triton.cdiv(m, ebm))](y, y.stride(0), q, bt, m, y.shape[1], 1, R=r, BM=ebm, BN=64, num_warps=4)


def lora_wide(y, x, a, bt, ws, bn):
    m, k = x.shape
    r = a.shape[0]
    split = ws.splits[k]
    F._lora_mid_kernel[(split, triton.cdiv(m, ws.bm))](x, x.stride(0), a, ws.QP, m, k, k // 64 // split, R=r, BM=ws.bm, BK=64, num_warps=4)
    ebm = min(max(ws.bm, 16), 64)
    F._lora_out_kernel[(triton.cdiv(y.shape[1], bn), triton.cdiv(m, ebm))](y, y.stride(0), ws.QP, bt, m, y.shape[1], split, R=r, BM=ebm, BN=bn, num_warps=4)


def main() -> None:
    out = {}
    torch.manual_seed(0)
    for m in (32, 128):
        totals = {}
        exact = True
        for name, (k, n, r, count) in SHAPES.items():
            x = torch.randn(m, k, device="cuda", dtype=torch.bfloat16)
            a = torch.randn(r, k, device="cuda", dtype=torch.bfloat16) * 0.02
            bt = torch.randn(r, n, device="cuda", dtype=torch.bfloat16) * 0.02
            y0 = torch.randn(m, n, device="cuda", dtype=torch.bfloat16)
            for target in (64, 16, 8):
                ws = F.Workspace(m)
                ws.splits = {kk: F._split(kk // 64, triton.cdiv(m, ws.bm), target) for kk in ws.splits}
                ws.QP = torch.empty(max(ws.splits.values()) * m * 64, device="cuda", dtype=torch.float32)
                q = torch.empty(m * 64, device="cuda", dtype=torch.float32)
                ref = y0.clone()
                F.lora_add(ref, x, a, bt, ws)
                variants = {"today": lambda y: F.lora_add(y, x, a, bt, ws), "reduce": lambda y: lora_reduce(y, x, a, bt, ws, q),
                            "wide_bn128": lambda y: lora_wide(y, x, a, bt, ws, 128), "wide_bn256": lambda y: lora_wide(y, x, a, bt, ws, 256)}
                for vname, fn in variants.items():
                    y = y0.clone()
                    fn(y)
                    exact &= bool(torch.equal(y, ref))
                    y = y0.clone()
                    key = f"{vname}_target_{target}"
                    totals[key] = totals.get(key, 0.0) + gpu_us(lambda: fn(y)) * count
        out[str(m)] = {"per_decision_ms": {k: round(v / 1000, 4) for k, v in sorted(totals.items(), key=lambda kv: kv[1])},
                       "bit_for_bit_equal_to_today_same_split": exact}
        print(m, json.dumps(out[str(m)]), flush=True)
    open(sys.argv[1], "w").write(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    with torch.inference_mode():
        main()
