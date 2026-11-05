"""Task 2: the fastest LoRA tiles that keep the summation order of the tiles before Task 2, so the results stay bit
for bit the same (compare_outputs.py showed that the shorter splits of tune_lora.py move some probabilities by up to
0.039 on real rows, beyond controller ruling C's 0.005).

The split of the input dimension and its 64-wide blocks stay as before (they fix which partial sums exist and the
order they are added in). Everything else is searched: the first kernel's tile height and warps, the second
kernel's tile height, column block, column blocks per program and warps. A candidate counts only if its output is
bit for bit equal to the earlier tiles' output on the same random inputs (the partials for the first kernel, the
updated y for the second). Times as tune_lora.py.

Usage (on datigator): .venv/bin/python results/step2b1/task2/tune_same_order.py OUT.json [M ...]"""
import itertools
import json
import sys

import torch
import triton

from jeff_runtime.engine import fast_path as F
from tune_lora import SHAPES, gpu_us


def before_tiles(m: int, k: int) -> tuple[int, int]:
    """(tile height, split) of the tiles before Task 2."""
    bm = 16 if m <= 16 else 32 if m <= 32 else 64
    return bm, F._split(k // 64, triton.cdiv(m, bm))


def main() -> None:
    ms = [int(v) for v in sys.argv[2:]] or [32, 64, 128, 256]
    torch.manual_seed(0)
    result = {}
    for m in ms:
        per = {}
        for name, (k, n, r, count) in SHAPES.items():
            x = torch.randn(m, k, device="cuda", dtype=torch.bfloat16)
            pool = torch.zeros(2, r * (k + n), device="cuda", dtype=torch.bfloat16)
            pool[1] = torch.randn(r * (k + n), device="cuda", dtype=torch.bfloat16) * 0.02
            adp = torch.ones(1, device="cuda", dtype=torch.long)
            y0 = torch.randn(m, n, device="cuda", dtype=torch.bfloat16)
            bm0, split = before_tiles(m, k)
            qp = torch.zeros(split * m * r, device="cuda", dtype=torch.float32)
            y = y0.clone()

            def mid(bm, warps):
                F._lora_mid_kernel[(split, triton.cdiv(m, bm))](x, x.stride(0), pool, pool.stride(0), 0, adp, m, qp, m,
                                                               k, k // 64 // split, R=r, BM=bm, BK=64, num_warps=warps)

            def outk(bm, bn, nc, warps):
                F._lora_out_kernel[(triton.cdiv(n, bn * nc), triton.cdiv(m, bm))](
                    y, y.stride(0), qp, pool, pool.stride(0), r * k, adp, m, m, n, split, nc, R=r, BM=bm, BN=bn,
                    num_warps=warps)

            mid(bm0, 4)
            qp_ref = qp.clone()
            outk(bm0, 64, 1, 4)
            y_ref = y.clone()
            before_us = gpu_us(lambda: (mid(bm0, 4), outk(bm0, 64, 1, 4)))
            bms = [b for b in (16, 32, 64) if b <= m]
            best_mid = None
            for bm, warps in itertools.product(bms, (2, 4, 8)):
                qp.zero_()
                mid(bm, warps)
                if not torch.equal(qp, qp_ref):
                    continue
                us = gpu_us(lambda: mid(bm, warps))
                if best_mid is None or us < best_mid[0]:
                    best_mid = (us, {"bm": bm, "mid_warps": warps})
            qp.copy_(qp_ref)
            best_out = None
            for bm, bn, nc, warps in itertools.product(bms, (32, 64, 128, 256), (1, 2, 4, 8), (2, 4, 8)):
                if bn * nc > 2 * n:
                    continue
                y.copy_(y0)
                outk(bm, bn, nc, warps)
                if not torch.equal(y, y_ref):
                    continue
                us = gpu_us(lambda: outk(bm, bn, nc, warps))
                if best_out is None or us < best_out[0]:
                    best_out = (us, {"out_bm": bm, "bn": bn, "nc": nc, "out_warps": warps})
            t = {**best_mid[1], **best_out[1], "split": split, "bk": 64}
            pair = gpu_us(lambda: (mid(t["bm"], t["mid_warps"]), outk(t["out_bm"], t["bn"], t["nc"], t["out_warps"])))
            y.copy_(y0)
            qp.zero_()
            mid(t["bm"], t["mid_warps"])
            outk(t["out_bm"], t["bn"], t["nc"], t["out_warps"])
            per[name] = {"count": count, "tiles": t, "pair_us": round(pair, 3), "before_us": round(before_us, 3),
                         "pair_bit_equal": bool(torch.equal(y, y_ref))}
            print(m, name, per[name], flush=True)
        result[str(m)] = {"per_shape": per, "per_decision_ms": {
            key: round(sum(v[key] * v["count"] for v in per.values()) / 1000, 4) for key in ("before_us", "pair_us")}}
        print(m, json.dumps(result[str(m)]["per_decision_ms"]), flush=True)
    with open(sys.argv[1], "w") as f:
        f.write(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    with torch.inference_mode():
        main()
