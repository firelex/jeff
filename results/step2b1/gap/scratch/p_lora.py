"""The LoRA update kernels of today's fast path (fast_path.lora_add: _lora_mid_kernel + _lora_out_kernel) against
jeff-asic's (gpu_int4_fused.w4_linear with y_base: _w4_mid_kernel + _epilogue_kernel), on the fast path's real
shapes, plus today's kernels with smaller input-dimension splits (fewer partial products for _lora_out_kernel to sum).
GPU time per call: 200 calls recorded in one CUDA graph, timed with events, divided by 200 (median of 20 replays).
Shapes per decision: 18 GDN layers (proj 1024->8224 r32, out 2048->1024 r16), 6 attention layers (proj 1024->5120 r64,
out 2048->1024 r16), 24 MLPs (in 1024->7168 r32, down 3584->1024 r16)."""
import json
import sys

import torch
import triton

import gpu_int4_fused as K
from jeff_runtime.engine import fast_path as F

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


def main() -> None:
    out = {}
    torch.manual_seed(0)
    for m in (32, 128):
        per = {}
        totals = {}
        for name, (k, n, r, count) in SHAPES.items():
            x = torch.randn(m, k, device="cuda", dtype=torch.bfloat16)
            a = torch.randn(r, k, device="cuda", dtype=torch.bfloat16) * 0.02
            bt = torch.randn(r, n, device="cuda", dtype=torch.bfloat16) * 0.02
            y0 = torch.randn(m, n, device="cuda", dtype=torch.bfloat16)
            y = y0.clone()
            row = {}
            for target in (64, 16, 8, 4, 2, 1):
                ws = F.Workspace(m)
                ws.splits = {kk: F._split(kk // 64, triton.cdiv(m, ws.bm), target) for kk in ws.splits}
                ws.QP = torch.empty(max(ws.splits.values()) * m * 64, device="cuda", dtype=torch.float32)
                row[f"today_split_target_{target}"] = {"us": round(gpu_us(lambda: F.lora_add(y, x, a, bt, ws)), 2),
                                                       "split": ws.splits[k]}
            aws = K.Workspace(m, max(n for _, n, _, _ in SHAPES.values()) + 64)
            row["jeff_asic"] = {"us": round(gpu_us(lambda: K.w4_linear(x, None, None, n, n, aws, a, bt, y_base=y)), 2)}
            per[name] = row
            for key, v in row.items():
                totals[key] = totals.get(key, 0.0) + v["us"] * count
        out[str(m)] = {"per_shape": per, "per_decision_ms": {k: round(v / 1000, 4) for k, v in totals.items()}}
        print(m, json.dumps(out[str(m)]["per_decision_ms"]), flush=True)
    open(sys.argv[1], "w").write(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    with torch.inference_mode():
        main()
