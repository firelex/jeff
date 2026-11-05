"""Investigation of the agreement gate's emotion result (plan 2b-1 Task 8): the same 200 emotion test rows as
test_agreement_cuda (same seed), answered in this process by the standard path and by the fast path, one row at a time (and, on the current tree, also all rows in one engine call: batched passes of up to 64 rows and
batched prefix passes).
Run with the code at ae101bd on PYTHONPATH, the fast path is the old single-row FastPath.decide; with the current tree,
it is the batched path at B = 1 (LocalEngine.predict of one row). Writes every row's probabilities, so the two runs and
the served (mixed) run can be compared row by row.

Run on datigator: .venv/bin/python results/step2b1/task8/emotion_rows.py --out results/step2b1/task8/emotion_<label>.json"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--adapter", default="emotion")
    parser.add_argument("--rows", type=int, default=200, help="rows drawn (200: exactly the gate's rows)")
    a = parser.parse_args()
    spec = importlib.util.spec_from_file_location("agr", REPO / "runtime/python/tests/test_agreement_cuda.py")
    assert spec is not None and spec.loader is not None
    agr = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(agr)
    agr.ROWS_PER_ADAPTER = a.rows
    import glob

    from jeff_runtime import LocalEngine

    folder = Path(glob.glob(agr.CHECKPOINTS.format(name=a.adapter))[0])
    engine = LocalEngine(agr.BASE, {a.adapter: folder}, backend="fast", prepare_workers=1, prefix_pool_mb=8192,
                         max_adapters=1)
    items = agr.sample(a.adapter)
    rows = [row for _, row in items]
    engine.adapter_set.use(a.adapter)
    standard = engine._standard(rows, engine._layout(a.adapter))[0]
    old = hasattr(engine.fast, "decide")
    fast = []
    for row in rows:
        if old:
            prepared = engine.fast.prepare(a.adapter, [row])
            fast.append(engine.fast.decide(a.adapter, prepared, engine.max_tokens)[0][0])
        else:
            fast.append(engine.predict(a.adapter, [row]).probabilities[0])
    together = None if old else engine.predict(a.adapter, rows).probabilities  # passes of up to 64 rows
    import jeff_runtime
    record = {"together": together, "code": jeff_runtime.__file__, "fast_path": "FastPath.decide (single row)" if old else
              "batched path, B = 1 (LocalEngine.predict of one row)",
              "lines": [n for n, _ in items], "standard": standard, "fast": fast}
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(record) + "\n")
    top = lambda p: p.index(max(p))  # noqa: E731
    miss = [n for (n, _), s, f in zip(items, standard, fast) if top(s) != top(f)]
    print(record["fast_path"], len(rows), "rows; disagreements with standard:", len(miss), miss)
    if together is not None:
        miss = [n for (n, _), s, f in zip(items, standard, together) if top(s) != top(f)]
        print("batched path, all rows in one call (passes of up to 64):", len(miss), miss)


if __name__ == "__main__":
    main()
