"""Kernel-level comparison at batch 1, cached 1,000-token prefix, nav adapter: jeff-asic's bf16 fused decision
(gpu_int4_floor_pkgfla.py, run_fused) against today's FastPath.decide_body, in one process and one environment.
Each body runs eagerly under torch.profiler (CUDA activity only) REPS times; kernel times are summed per kernel
name and divided by REPS. Kernel durations do not depend on eager vs graph launch; graph launch only removes the
gaps between kernels, which the graph-replay GPU time (also printed) includes.
Usage: PYTHONPATH=~/jeff-2b-scratch/gap/asic python p_kernels.py OUT.json"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import torch
from torch.profiler import ProfilerActivity, profile

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
NAV = Path("/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-nav-20261003-0149/final")
REF = Path.home() / "jeff-asic/int4floor/reference.pt"
REPS = 20
NEWS = (32, 128)


def category(name: str) -> str:
    n = name.lower()
    if any(k in n for k in ("_add_rms", "_gdn_prep", "_gated_norm", "_attn_prep", "_gate_mul", "_silu_mul")):
        return "fused_elementwise"
    if "lora" in n:
        return "lora"
    if any(k in n for k in ("chunk", "fwd_h", "fwd_o", "kkt", "solve_tril", "recompute_w_u", "cumsum", "merge_16x16", "wy_")):
        return "gated_deltanet_fla"
    if any(k in n for k in ("fmha", "flash", "attention", "sdpa", "efficient")):
        return "attention"
    if any(k in n for k in ("gemm", "cublas", "nvjet", "cutlass", "sm90", "sm100", "sm120", "matmul")):
        return "gemm"
    return "other_torch"


def prof(fn) -> dict:
    for _ in range(3):
        fn()
    torch.cuda.synchronize()
    with profile(activities=[ProfilerActivity.CUDA]) as p:
        for _ in range(REPS):
            fn()
        torch.cuda.synchronize()
    per = defaultdict(lambda: [0.0, 0])
    for e in p.events():
        if e.device_type == torch.autograd.DeviceType.CUDA and e.device_time > 0:
            per[e.name][0] += e.device_time  # microseconds
            per[e.name][1] += 1
    cats = defaultdict(lambda: [0.0, 0])
    for name, (us, cnt) in per.items():
        c = category(name)
        cats[c][0] += us / REPS
        cats[c][1] += cnt / REPS
    top = sorted(((us / REPS, cnt / REPS, name[:110]) for name, (us, cnt) in per.items()), reverse=True)[:25]
    return {"kernel_ms_total": round(sum(v[0] for v in cats.values()) / 1000, 4),
            "kernels_per_decision": round(sum(v[1] for v in cats.values()), 1),
            "by_category_ms": {k: {"ms": round(v[0] / 1000, 4), "kernels": round(v[1], 1)} for k, v in sorted(cats.items())},
            "top_kernels": [{"us": round(u, 2), "count": c, "name": n} for u, c, n in top]}


def graph_gpu_ms(replay) -> float:
    s = []
    for i in range(300):
        a, b = torch.cuda.Event(enable_timing=True), torch.cuda.Event(enable_timing=True)
        a.record()
        replay()
        b.record()
        b.synchronize()
        if i >= 50:
            s.append(a.elapsed_time(b))
    return round(sorted(s)[len(s) // 2], 4)


def main() -> None:
    out = {"torch": torch.__version__}
    torch.backends.cuda.matmul.allow_tf32 = False
    # jeff-asic
    import argparse
    import gpu_int4_floor_pkgfla as G
    args = argparse.Namespace(base=BASE, adapter="nav", adapter_dir=NAV, weights="bf16", gdn="fla", attn="sdpa", compile=None,
                              fused=True, command="time", quant=None)
    model = G.load(args)
    targs = argparse.Namespace(reference=REF, adapter="nav")
    tokens = torch.tensor(G.real_tokens(targs, 1000 + 256 + 16), device="cuda")
    cache = model.prefix(tokens[:1000], 1000 + 256)
    from jeff_runtime.engine.fast_path import PREFIX_CAPS, Row
    from jeff_runtime.local_engine import LocalEngine
    eng = LocalEngine(BASE, {"nav": NAV}, backend="fast")
    fast = eng.fast
    tok = fast.tokenizer
    prefix_text = tok.decode(tokens[:1000].tolist())
    p_len = len(tok(prefix_text, add_special_tokens=False)["input_ids"])
    cap = next(c for c in PREFIX_CAPS if c >= p_len)
    out["fast_prefix_tokens"] = p_len
    for n in NEWS:
        with torch.inference_mode():
            d = G.Decision(model, cache, 1000, n, 32, graph=True)
            new = tokens[1000:1000 + n].cpu()[None]
            d(new)
            asic = prof(lambda: d._body())
            asic["graph_gpu_ms_p50"] = graph_gpu_ms(d.graph.replay)
            row = Row(prefix_text, tokens[1000:1000 + n].tolist(), 32)
            fast.decide("nav", [row], 8192)
            ours = prof(lambda: fast.decide_body(cap, n))
            graph, _ = fast.graphs[(cap, n)]
            ours["graph_gpu_ms_p50"] = graph_gpu_ms(graph.replay)
        out[str(n)] = {"jeff_asic_fused_bf16": asic, "fast_path": ours}
        print(n, json.dumps({k: {"total": v["kernel_ms_total"], "graph": v["graph_gpu_ms_p50"], "cats": v["by_category_ms"]} for k, v in out[str(n)].items()}), flush=True)
        del d
    Path(sys.argv[1]).write_text(json.dumps(out, indent=1) + "\n")


if __name__ == "__main__":
    main()
