"""Task 2: choose the LoRA update's tiles per shape by measurement on datigator's GPU (gap report Section 2.1).

For every LoRA shape of a decision (k inputs, `out` outputs, rank r, how many per decision) and token count M, it
times the two kernels of fast_path.lora_add separately over a grid of tiles:
- _lora_mid_kernel: tile height bm, block width bk, split of the input dimension, warps;
- _lora_out_kernel: tile height, column block bn, column blocks per program nc, warps, for each split (the number of
  partials it sums).
Each time is the GPU time of one call: 200 calls recorded in one CUDA graph, the median of 20 replays, divided by
200. Then for each split the best mid plus the best out, the best split overall, and that pair timed together (the
pair is what a decision pays). Also the tiles fast_path.lora_tiles gives ("today"), timed the same way: when
tune_lora.json and tune_lora_512_1024.json were made, those were still the tiles before Task 2.

The split fixes the summation order. Any choice here is deterministic: a shape and token count always use the same
tiles. Writes JSON: per M and shape, the chosen tiles and times, and the per-decision totals.

Usage (on datigator): .venv/bin/python results/step2b1/task2/tune_lora.py OUT.json [M ...]"""
import itertools
import json
import sys

import torch
import triton

from jeff_runtime.engine import fast_path as F

# (k, out, rank, count per decision): 18 GDN layers, 6 attention layers, 24 MLPs
SHAPES = {"gdn_proj": (1024, 8224, 32, 18), "gdn_out": (2048, 1024, 16, 18), "attn_proj": (1024, 5120, 64, 6),
          "attn_out": (2048, 1024, 16, 6), "mlp_in": (1024, 7168, 32, 24), "mlp_down": (3584, 1024, 16, 24)}


def gpu_us(fn) -> float:
    for _ in range(3):
        fn()
    torch.cuda.synchronize()
    g = torch.cuda.CUDAGraph()
    s = torch.cuda.Stream()
    s.wait_stream(torch.cuda.current_stream())
    with torch.cuda.stream(s):
        fn()
    torch.cuda.current_stream().wait_stream(s)
    with torch.cuda.graph(g):
        for _ in range(200):
            fn()
    t = []
    for _ in range(20):
        a, b = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        a.record()
        g.replay()
        b.record()
        b.synchronize()
        t.append(a.elapsed_time(b) * 1000 / 200)
    return sorted(t)[10]


class FakeLin:
    def __init__(self, pool, k, out, r):
        self.pool, self.off_a, self.off_b, self.rank = pool, 0, r * k, r


def main() -> None:
    out_path = sys.argv[1]
    ms = [int(v) for v in sys.argv[2:]] or [32, 64, 128, 256]
    torch.manual_seed(0)
    result = {}
    for m in ms:
        per = {}
        for name, (k, n, r, count) in SHAPES.items():
            x = torch.randn(m, k, device="cuda", dtype=torch.bfloat16)
            pool = torch.zeros(2, r * (k + n), device="cuda", dtype=torch.bfloat16)
            pool[1, :r * k] = torch.randn(r * k, device="cuda", dtype=torch.bfloat16) * 0.02
            pool[1, r * k:] = torch.randn(r * n, device="cuda", dtype=torch.bfloat16) * 0.02
            adp = torch.ones(1, device="cuda", dtype=torch.long)
            y = torch.randn(m, n, device="cuda", dtype=torch.bfloat16)
            qp = torch.zeros(64 * m * 64, device="cuda", dtype=torch.float32)
            bms = [b for b in (16, 32, 64) if b <= m]

            def mid(bm, bk, split, warps):
                F._lora_mid_kernel[(split, triton.cdiv(m, bm))](x, x.stride(0), pool, pool.stride(0), 0, adp, m, qp, m,
                                                               k, k // bk // split, R=r, BM=bm, BK=bk, num_warps=warps)

            def outk(bm, bn, nc, warps, split):
                F._lora_out_kernel[(triton.cdiv(n, bn * nc), triton.cdiv(m, bm))](
                    y, y.stride(0), qp, pool, pool.stride(0), r * k, adp, m, m, n, split, nc, R=r, BM=bm, BN=bn,
                    num_warps=warps)

            splits = {}
            for bk in (64, 128):
                for split in [s for s in range(1, k // bk + 1) if (k // bk) % s == 0 and s <= 64]:
                    best = None
                    for bm, warps in itertools.product(bms, (2, 4, 8)):
                        us = gpu_us(lambda: mid(bm, bk, split, warps))
                        if best is None or us < best[0]:
                            best = (us, {"bm": bm, "bk": bk, "split": split, "mid_warps": warps})
                    key = split
                    if key not in splits or best[0] < splits[key]["mid"][0]:
                        splits[key] = {"mid": best}
            for split, entry in splits.items():
                best = None
                for bm, bn, nc, warps in itertools.product(bms, (32, 64, 128, 256), (1, 2, 4, 8), (2, 4, 8)):
                    if bn * nc > 2 * n:
                        continue
                    us = gpu_us(lambda: outk(bm, bn, nc, warps, split))
                    if best is None or us < best[0]:
                        best = (us, {"out_bm": bm, "bn": bn, "nc": nc, "out_warps": warps})
                entry["out"] = best
                entry["sum_us"] = entry["mid"][0] + best[0]
            ranked = sorted(splits.items(), key=lambda kv: kv[1]["sum_us"])
            top = []
            for split, entry in ranked[:4]:  # the best few, timed as the pair a decision runs
                t = {**entry["mid"][1], **entry["out"][1]}
                pair = gpu_us(lambda: (mid(t["bm"], t["bk"], t["split"], t["mid_warps"]),
                                       outk(t["out_bm"], t["bn"], t["nc"], t["out_warps"], t["split"])))
                top.append({"tiles": t, "pair_us": round(pair, 3), "sum_us": round(entry["sum_us"], 3)})
            top.sort(key=lambda e: e["pair_us"])
            today = F.lora_tiles(m, m, k, n)
            today_us = gpu_us(lambda: (mid(today.bm, today.bk, today.split, today.mid_warps),
                                       outk(today.out_bm, today.bn, today.nc, today.out_warps, today.split)))
            per[name] = {"count": count, "today": {"tiles": today.__dict__, "pair_us": round(today_us, 3)},
                         "best": top[0], "runners_up": top[1:],
                         "per_split_sum_us": {str(s): round(e["sum_us"], 3) for s, e in ranked}}
            print(m, name, "today", round(today_us, 2), "best", top[0], flush=True)
        result[str(m)] = {"per_shape": per,
                          "per_decision_ms": {
                              "today": round(sum(v["today"]["pair_us"] * v["count"] for v in per.values()) / 1000, 4),
                              "best": round(sum(v["best"]["pair_us"] * v["count"] for v in per.values()) / 1000, 4)}}
        print(m, json.dumps(result[str(m)]["per_decision_ms"]), flush=True)
    with open(out_path, "w") as f:
        f.write(json.dumps(result, indent=1) + "\n")


if __name__ == "__main__":
    with torch.inference_mode():
        main()
