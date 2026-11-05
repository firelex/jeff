"""Task 6: how long a batched prefix pass takes per (B, prefix bucket), to choose batched_path.PREFIX_PASS_ROWS, and a
quick check of batched prefixes against one-at-a-time ones on real prefixes (scratch measurement, kept for the record).

With the argument `wide`, records every batch bucket with B x bucket <= 65,536 (instead of the chosen caps) and writes
the ms per pass and per prefix to ~/jeff-2b-scratch/t6/prefix_ms_wide.json (copied here as prefix_pass_ms_wide.json:
{bucket: {B: [ms per pass, ms per prefix]}}). Then 53 real prefixes of nine adapters: made one at a time
(FastPath.prefix), then all at once (BatchedPath.ensure_prefixes), and the tensors compared.

Run on datigator from ~/jeff-2b-scratch: ~/jeff-dev-next/.venv/bin/python measure_prefix_caps.py wide"""
import glob, json, sys, time, random, logging
from pathlib import Path
import torch
logging.basicConfig(level=logging.INFO)
sys.path.insert(0, str(Path.home() / "jeff-dev-next/runtime/python/src"))
import jeff_runtime.engine.batched_path as bp

WIDE = sys.argv[1] == "wide" if len(sys.argv) > 1 else False
if WIDE:
    bp.PREFIX_PASS_ROWS = {nb: max(b for b in bp.BATCH_BUCKETS if b * nb <= 65536) for nb in bp.PREFIX_BUCKETS}
print("caps", bp.PREFIX_PASS_ROWS, flush=True)
from jeff_runtime import LocalEngine
BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
NAMES = ("triage", "nav", "spam", "emotion", "ground", "guard", "legal-clauses", "support-intents", "tools")
ad = {n: Path(glob.glob(f"/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{n}-*/final")[0]) for n in NAMES}
eng = LocalEngine(BASE, ad, backend="fast", prefix_pool_mb=6144, max_adapters=len(NAMES))
fast = eng.fast
torch.cuda.synchronize()
t = time.time()
b = bp.BatchedPath(fast)
print("record s", round(time.time() - t, 1), "mem", torch.cuda.memory_allocated() / 2**30, torch.cuda.memory_reserved() / 2**30, flush=True)
table = {}
for (B, nb), ms in sorted(b.prefix_pass_ms.items(), key=lambda x: (x[0][1], x[0][0])):
    table.setdefault(nb, {})[B] = (ms, round(ms / B, 3))
for nb, row in table.items():
    print(nb, {B: v for B, v in row.items()}, flush=True)
out = Path.home() / "jeff-2b-scratch/t6"
out.mkdir(exist_ok=True)
(out / ("prefix_ms_wide.json" if WIDE else "prefix_ms.json")).write_text(json.dumps({str(k): v for k, v in table.items()}, indent=1))

# bit equality
items = []
for n in NAMES:
    lines = Path(f"/mnt/d/jeff-models/v1.3/casdgx01/testsets/{n}/test.jsonl").read_text().splitlines()[:6]
    for line in lines:
        r = json.loads(line)
        p = fast.prepare(n, [{"state": r["state"], "question": r["question"]}])
        if isinstance(p, str):
            continue
        items.append((fast.index(n), p[0].prefix_text))
random.Random(1).shuffle(items)
items = list(dict.fromkeys(items))
pool = fast.prefixes
def grab(slot):
    conv, state, kv = pool.gather(slot)
    return conv.cpu(), state.cpu(), kv.cpu()
ref = {}
with torch.inference_mode():
    for idx, text in items:
        timing = {"model_ms": 0.0, "prefix_ms": 0.0, "prefix_hits": 0, "prefix_misses": 0, "new_tokens": 0}
        slot = fast.prefix(idx, text, 8192, timing)
        ref[(idx, text)] = grab(slot)
        pool.release(slot)
    print("one-at-a-time done", len(ref), flush=True)
    lens = [len(fast.tokenizer(t, add_special_tokens=False)["input_ids"]) for _, t in items]
    print("prefix lengths", sorted(lens), flush=True)
    rows = items + items[:3]  # shared prefixes
    slots, timing = b.ensure_prefixes(rows, 8192)
    print("timing", timing, flush=True)
    bad = 0
    worst = {}
    for (idx, text), slot in zip(rows, slots):
        got = grab(slot)
        for name, x, y in zip(("conv", "state", "kv"), got, ref[(idx, text)]):
            if not torch.equal(x, y):
                bad += 1
                d = (x.float() - y.float()).abs().max().item()
                worst[name] = max(worst.get(name, 0), d)
    pool.unpin(slots)
    print("unequal tensors", bad, "of", 3 * len(rows), "worst", worst, flush=True)
    print("zero page clean", not pool.data[1].any().item())
