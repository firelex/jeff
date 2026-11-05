"""Task 2: today's fast path against the outputs recorded before Task 2 (record_outputs.py), every row: the largest
probability difference per row, whether the top option is the same, and the recorded margin between the top two
options. Prints a summary and the 12 rows that moved most; writes every row to OUT.json.

Usage (on datigator): .venv/bin/python results/step2b1/task2/compare_outputs.py RECORDED.json OUT.json"""
import json
import sys
from pathlib import Path

import torch

from jeff_runtime import LocalEngine

recorded = json.loads(Path(sys.argv[1]).read_text())
names = list(recorded["adapters"])
engine = LocalEngine(Path(recorded["base"]), {n: Path(recorded["checkpoints"][n]) for n in names}, backend="fast",
                     fast_cache_mb=2048, max_adapters=len(names))
rows = {}
for name in names:
    lines = Path(f"/mnt/d/jeff-models/v1.3/casdgx01/testsets/{name}/test.jsonl").read_text().splitlines()
    rows[name] = [{"state": i["state"], "question": i["question"]}
                  for i in (json.loads(lines[n]) for n in recorded["adapters"][name]["lines"])]
out = []
with torch.inference_mode():
    for i in range(len(rows[names[0]])):
        for name in names:
            now = engine.predict(name, [rows[name][i]]).probabilities[0]
            before = recorded["adapters"][name]["probabilities"][i]
            ranked = sorted(before, reverse=True)
            out.append({"adapter": name, "row": i, "line": recorded["adapters"][name]["lines"][i],
                        "diff": max(abs(a - b) for a, b in zip(now, before)),
                        "same_top": now.index(max(now)) == before.index(max(before)),
                        "recorded_margin": ranked[0] - ranked[1], "bit_equal": now == before,
                        "new_ids": len(engine.fast.prepare(name, [rows[name][i]])[0].new_ids)})
out.sort(key=lambda r: -r["diff"])
print("rows", len(out), "top changed", sum(not r["same_top"] for r in out), "max diff", out[0]["diff"],
      "bit equal", sum(r["bit_equal"] for r in out))
for name in names:
    mine = [r["diff"] for r in out if r["adapter"] == name]
    print(name, "max", round(max(mine), 5), "median", round(sorted(mine)[len(mine) // 2], 6))
for r in out[:12]:
    print(r)
Path(sys.argv[2]).write_text(json.dumps(out, indent=1) + "\n")
