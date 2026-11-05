"""Task 1 cost check, one process, back to back: LocalEngine.predict batch-1 (fast path) as now, against the same call
with the removed adapter_set.use(adapter) put back in front of it (what the code before Task 1 did per call)."""
import glob, json, statistics, time
from pathlib import Path
import torch
from jeff_runtime import LocalEngine
BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
triage = Path(glob.glob("/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-triage-*/final")[0])
row = json.loads(Path("/mnt/d/jeff-models/v1.3/casdgx01/testsets/triage/test.jsonl").read_text().splitlines()[0])
row = {"state": row["state"], "question": row["question"]}
with torch.inference_mode():
    eng = LocalEngine(BASE, {"triage": triage}, backend="fast", fast_cache_mb=512)
    def now(): eng.predict("triage", [row])
    def before(): eng.adapter_set.use("triage"); eng.predict("triage", [row])
    def use_only(): eng.adapter_set.use("triage")
    for f in (now, before): [f() for _ in range(100)]
    res = {}
    for rnd in range(3):
        for name, f in (("after_task1", now), ("with_use_added_back", before), ("adapter_set_use_alone", use_only)):
            ts = []
            for _ in range(1000):
                t = time.perf_counter(); f(); ts.append((time.perf_counter() - t) * 1000)
            res.setdefault(name, []).append(round(statistics.median(ts), 4))
    print(json.dumps({"p50_ms_per_round": res}, indent=1))
