"""Task 2: fast_path.lora_tiles (the tiles the fast path uses) against the tiles before Task 2, on every LoRA shape
of a decision: GPU time of the pair of kernels per call (as tune_lora.py times it) and whether the two give bit for
bit the same output on the same random inputs.

Usage (on datigator): .venv/bin/python results/step2b1/task2/eval_tiles.py OUT.json [M ...]"""
import json
import sys

import torch
import triton

from jeff_runtime.engine import fast_path as F
from tune_lora import SHAPES, gpu_us


def before_tiles(m: int, k: int) -> F.LoraTiles:
    bm = 16 if m <= 16 else 32 if m <= 32 else 64
    return F.LoraTiles(bm=bm, split=F._split(k // 64, triton.cdiv(m, bm)), bk=64, mid_warps=4, out_bm=bm, bn=64, nc=1,
                       out_warps=4)


def main() -> None:
    ms = [int(v) for v in sys.argv[2:]] or [32, 64, 128, 256, 512, 1024]
    torch.manual_seed(0)
    out = {}
    for m in ms:
        rows = {}
        for name, (k, n, r, count) in SHAPES.items():
            x = torch.randn(m, k, device="cuda", dtype=torch.bfloat16)
            pool = torch.zeros(2, r * (k + n), device="cuda", dtype=torch.bfloat16)
            pool[1] = torch.randn(r * (k + n), device="cuda", dtype=torch.bfloat16) * 0.02
            adp = torch.ones(1, device="cuda", dtype=torch.long)
            y0 = torch.randn(m, n, device="cuda", dtype=torch.bfloat16)
            y = y0.clone()
            qp = torch.zeros(64 * m * 64, device="cuda", dtype=torch.float32)

            def pair(t: F.LoraTiles) -> None:
                F._lora_mid_kernel[(t.split, triton.cdiv(m, t.bm))](x, x.stride(0), pool, pool.stride(0), 0, adp, m, qp,
                                                                   m, k, k // t.bk // t.split, R=r, BM=t.bm, BK=t.bk,
                                                                   num_warps=t.mid_warps)
                F._lora_out_kernel[(triton.cdiv(n, t.bn * t.nc), triton.cdiv(m, t.out_bm))](
                    y, y.stride(0), qp, pool, pool.stride(0), r * k, adp, m, m, n, t.split, t.nc, R=r, BM=t.out_bm,
                    BN=t.bn, num_warps=t.out_warps)

            old, new = before_tiles(m, k), F.lora_tiles(m, m, k, n)
            results = []
            for t in (old, new):
                y.copy_(y0)
                pair(t)
                results.append(y.clone())
            rows[name] = {"count": count, "tiles": new.__dict__, "us": round(gpu_us(lambda: pair(new)), 3),
                          "before_us": round(gpu_us(lambda: pair(old)), 3),
                          "bit_equal_to_before": bool(torch.equal(*results))}
            print(m, name, rows[name], flush=True)
        total = {key: round(sum(v[key] * v["count"] for v in rows.values()) / 1000, 4) for key in ("us", "before_us")}
        out[str(m)] = {"per_shape": rows, "per_decision_ms": total,
                       "all_bit_equal": all(v["bit_equal_to_before"] for v in rows.values())}
        print(m, json.dumps(total), "all bit equal:", out[str(m)]["all_bit_equal"], flush=True)
    with open(sys.argv[1], "w") as f:
        f.write(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    with torch.inference_mode():
        main()
