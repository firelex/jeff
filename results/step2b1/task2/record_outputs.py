"""Task 2 reference (controller ruling C): batch-1 fast-path probabilities on fixed real test rows of five adapters,
recorded at the commit Task 2 starts from (e349d1b), before the LoRA kernels change their summation order.

Rows: for each adapter, the first 60 lines of its test set that the fast path takes (no images, at most 1,024 new
tokens). Each row is decided alone (batch 1), all five adapters loaded in one engine, adapters taken in turn so each
call switches adapter. The output names the test-set line of every row, so the comparison test reads the same rows.

Usage (on datigator, from ~/jeff-dev-next): .venv/bin/python results/step2b1/task2/record_outputs.py OUTPUT.json COMMIT"""
import glob
import json
import sys
from pathlib import Path

import torch

from jeff_runtime import LocalEngine

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
ADAPTERS = ("nav", "triage", "legal-clauses", "spam", "emotion")
ROWS_PER_ADAPTER = 60


def checkpoint(name: str) -> Path:
    found = glob.glob(f"/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final")
    if len(found) != 1:
        raise FileNotFoundError(f"expected one v1.3 checkpoint for {name}, found {found}")
    return Path(found[0])


def main() -> None:
    output = Path(sys.argv[1])
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite {output}")
    commit = sys.argv[2]  # the Mac commit the synced tree is at (datigator's copy has no .git)
    engine = LocalEngine(BASE, {name: checkpoint(name) for name in ADAPTERS}, backend="fast", fast_cache_mb=2048)
    rows: dict[str, list[tuple[int, dict]]] = {}
    for name in ADAPTERS:
        lines = Path(f"/mnt/d/jeff-models/v1.3/casdgx01/testsets/{name}/test.jsonl").read_text().splitlines()
        taken: list[tuple[int, dict]] = []
        for number, line in enumerate(lines):
            item = json.loads(line)
            row = {"state": item["state"], "question": item["question"]}
            if not isinstance(engine.fast.prepare(name, [row]), str):
                taken.append((number, row))
            if len(taken) == ROWS_PER_ADAPTER:
                break
        rows[name] = taken
    out: dict[str, dict] = {name: {"lines": [n for n, _ in rows[name]], "probabilities": []} for name in ADAPTERS}
    with torch.inference_mode():
        for i in range(ROWS_PER_ADAPTER):
            for name in ADAPTERS:
                result = engine.predict(name, [rows[name][i][1]])
                if result.timing["path"] != "fast":
                    raise RuntimeError(f"{name} row {rows[name][i][0]} ran on {result.timing['path']}")
                out[name]["probabilities"].append(result.probabilities[0])
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps({"base": str(BASE), "checkpoints": {n: str(checkpoint(n)) for n in ADAPTERS},
                                  "commit": commit,
                                  "adapters": out}) + "\n")
    print("wrote", output, {n: len(v["lines"]) for n, v in out.items()})


if __name__ == "__main__":
    main()
