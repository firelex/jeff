"""A bfloat16 fast-path engine of this tree against a reference commit (usually main), in one process, back to back:
the same probabilities, and batch-1 latency within noise. Plan 2b-2 Task 3's check that INT4 weights in FastPath leave
bfloat16 engines unchanged.

Two steps, because the GPU machine's copy of the tree has no .git (scripts/sync_rtx.sh never copies it):

1. On the Mac (a git checkout): write the reference commit's engine modules into a package folder.
     .venv/bin/python scripts/bench_bf16_against_ref.py export --ref main --out /path/refpkg
   It writes fast_path.py, batched_path.py, batched_kernels.py, prefix_pool.py, prepare.py and local_engine.py, as
   `git show <ref>:runtime/python/src/jeff_runtime/...` gives them, with the imports among those six rewritten to the
   package name, plus REF (the commit's full hash). Every other import (model, lora, types, errors) stays this tree's:
   they load the weights, which both engines share.
2. On the GPU machine: copy the folder there, then
     .venv/bin/python scripts/bench_bf16_against_ref.py run --ref-package /path/refpkg --conditions "..." > out.log

Method of `run`:
- One LocalEngine of this tree on the fast backend: the v1.3 bfloat16 base, the nine v1.3 adapters, a 2,048 MB prefix
  pool. It must hold no INT4 weights.
- The reference engine shares that engine's loaded model, adapters and preparation state. It is the reference's
  LocalEngine class with the instance state copied, and the reference's own FastPath, built on the same model.
- The reference engine runs under this tree's BatchedPath, not its own, unless --ref-batched-path is given. The reason:
  main before plan 2b-2 Task 0 has the choose() stream race that Task 0 fixed. Under GPU load, main's BatchedPath
  crashed while recording, with an illegal memory access in _graph_ms (2026-10-25). So by default the comparison
  isolates changes to FastPath and LocalEngine, not to BatchedPath; Task 3 changes neither batched_path.py nor
  batched_kernels.py. This tree's BatchedPath reads each fused group's `int4` (plan 2b-2 Task 5); a reference Lin from
  before Task 3 has none, so it is set to None (no INT4 weights, as on any bfloat16 base).
- --ref-batched-path: the reference engine runs under the reference's own BatchedPath (its batched_path.py and
  batched_kernels.py). Plan 2b-2 Task 5 changes batched_path.py (the per-graph product kernel choice), so its check
  that bfloat16 engines are unchanged uses this, with a reference that has Task 0's fix (45829b8, the commit before
  Task 5).
- That BatchedPath takes this tree's engine's per-pass kernel choices (plans) and prefix-pass timings
  (prefix_pass_ms), instead of timing its own. Both are chosen by timing. Under load, two recordings can choose
  differently and round differently. Without this, 14 of 63 mixed rows differed by up to 0.0027 in probability, and
  the differences came from the two choices alone, not the code under test.
- Rows: scripts/bench_engine_batch1.pick_rows, one real test row per new-token bucket (32, 64, 128, 256).
- Per bucket:
  - each engine gets WARM calls;
  - both engines must give equal probabilities;
  - then ROUNDS alternating rounds of R calls on each engine; the p50 wall time of LocalEngine.predict is reported.
- Then a mixed batch: the first 7 test rows of each adapter, one row per group, in one predict_many (rows over the
  token limit are left out for both engines alike), twice on each engine: the first call makes the prefixes, the
  second finds them cached. Reported and required to be zero: rows differing between the engines on the first call
  and on the second, and between each engine's own first and second call.
- The output is printed lines, then one JSON object."""

from __future__ import annotations

import argparse
import glob
import importlib
import json
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
ENGINE_MODULES = ("fast_path", "batched_path", "batched_kernels", "prefix_pool", "prepare")
TEST_ROWS = "/mnt/d/jeff-models/v1.3/casdgx01/testsets/{name}/test.jsonl"
ROUNDS, R, WARM = 10, 100, 100
PREFIX_POOL_MB = 2048


def export(ref: str, out: Path) -> None:
    if out.exists():
        raise SystemExit(f"{out} exists; give a new folder")
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", ref], check=True, capture_output=True,
                            text=True).stdout.strip()
    out.mkdir(parents=True)
    package = out.name
    sources = {f"{name}.py": f"runtime/python/src/jeff_runtime/engine/{name}.py" for name in ENGINE_MODULES}
    sources["local_engine.py"] = "runtime/python/src/jeff_runtime/local_engine.py"
    pattern = re.compile(r"jeff_runtime\.engine\.(" + "|".join(ENGINE_MODULES) + r")\b")
    for target, source in sources.items():
        text = subprocess.run(["git", "-C", str(REPO), "show", f"{commit}:{source}"], check=True,
                              capture_output=True, text=True).stdout
        (out / target).write_text(pattern.sub(lambda match: f"{package}.{match.group(1)}", text))
    (out / "__init__.py").write_text("")
    (out / "REF").write_text(commit + "\n")
    print(f"{ref} = {commit} written to {out}")


def differing(a: list[Any], b: list[Any]) -> dict[str, float]:
    """How many rows of two predict_many results differ in any probability, and the largest difference."""
    gaps = [max(abs(x - y) for x, y in zip(p.probabilities[0], q.probabilities[0], strict=True))
            for p, q in zip(a, b, strict=True)]
    return {"rows_differing": sum(gap > 0 for gap in gaps), "largest_difference": max(gaps)}


def run(package_dir: Path, conditions: str, ref_batched_path: bool) -> None:
    import torch

    sys.path.insert(0, str(REPO / "scripts"))
    sys.path.insert(0, str(package_dir.parent))
    from bench_engine_batch1 import ADAPTERS, BASE, CHECKPOINTS, pick_rows

    from jeff.errors import InputTooLong
    from jeff_runtime import LocalEngine
    from jeff_runtime.engine.batched_path import BatchedPath

    commit = (package_dir / "REF").read_text().strip()
    ref_local = importlib.import_module(f"{package_dir.name}.local_engine")
    ref_fast = importlib.import_module(f"{package_dir.name}.fast_path")
    batched_class = importlib.import_module(f"{package_dir.name}.batched_path").BatchedPath if ref_batched_path \
        else BatchedPath

    adapters = {name: Path(glob.glob(CHECKPOINTS.format(name=name))[0]) for name in ADAPTERS}
    engine = LocalEngine(BASE, adapters, backend="fast", prepare_workers=1, prefix_pool_mb=PREFIX_POOL_MB,
                         max_adapters=len(adapters))
    assert engine.fast.int4_bytes == 0 and all(lin.int4 is None for lin in engine.fast.lins)
    ref = object.__new__(ref_local.LocalEngine)
    ref.__dict__ = dict(engine.__dict__)
    ref.fast = ref_fast.FastPath(engine.model, engine.adapter_set, PREFIX_POOL_MB, len(adapters))
    if not ref_batched_path:
        for lin in ref.fast.lins:
            if not hasattr(lin, "int4"):  # a Lin before plan 2b-2 Task 3: no INT4 weights, as on any bfloat16 base
                lin.int4 = None

    class SamePlans(batched_class):  # type: ignore[misc, valid-type]
        """A BatchedPath (this tree's, or the reference's with --ref-batched-path) with this tree's engine's per-pass
        kernel choices (see the module docstring)."""

        def choose(self, b: int, n: int, stream: Any) -> Any:
            return engine.batched.plans[(b, n)]

    ref.batched = SamePlans(ref.fast)
    ref.batched.prefix_pass_ms = dict(engine.batched.prefix_pass_ms)

    rows = pick_rows(engine)
    out: dict[int, dict[str, float]] = {}
    with torch.inference_mode():
        for bucket, (name, row, n) in sorted(rows.items()):
            for e in (engine, ref):
                for _ in range(WARM):
                    e.predict(name, [row])
            a, b = engine.predict(name, [row]), ref.predict(name, [row])
            assert a.probabilities == b.probabilities, bucket
            assert a.timing["path"] == b.timing["path"] == "fast" and a.timing["format"] == "bf16"
            samples: dict[str, list[float]] = {"branch": [], "ref": []}
            for _ in range(ROUNDS):
                for label, e in (("branch", engine), ("ref", ref)):
                    for _ in range(R):
                        started = time.perf_counter()
                        e.predict(name, [row])
                        samples[label].append((time.perf_counter() - started) * 1000)
            out[bucket] = {label: round(statistics.median(v), 4) for label, v in samples.items()}
            out[bucket]["change_pct"] = round(100 * (out[bucket]["branch"] / out[bucket]["ref"] - 1), 2)
            print(bucket, name, n, out[bucket], flush=True)

        mixed = []
        for name in ADAPTERS:
            for line in Path(TEST_ROWS.format(name=name)).read_text().splitlines()[:7]:
                item = json.loads(line)
                row = {"state": item["state"], "question": item["question"]}
                try:
                    engine.prepare(name, [row])
                except InputTooLong:  # left out for both engines alike
                    continue
                mixed.append((name, row))
        mine: list[Any] = []
        theirs: list[Any] = []
        for _ in range(2):  # first: prefixes made; second: prefixes cached
            mine.append(engine.predict_many([(name, engine.prepare(name, [row])) for name, row in mixed]))
            theirs.append(ref.predict_many([(name, ref.prepare(name, [row])) for name, row in mixed]))
        paths = sorted({r.timing["path"] for call in mine + theirs for r in call})
        mixed_record = {
            "rows": len(mixed), "paths": paths, "passes": mine[0][0].timing["passes"],
            "prefix_passes": mine[0][0].timing["prefix_passes"],
            "branch_vs_ref_first_call": differing(mine[0], theirs[0]),
            "branch_vs_ref_second_call": differing(mine[1], theirs[1]),
            "branch_first_vs_second": differing(mine[0], mine[1]),
            "ref_first_vs_second": differing(theirs[0], theirs[1])}
        print(json.dumps(mixed_record), flush=True)
        for key in ("branch_vs_ref_first_call", "branch_vs_ref_second_call", "branch_first_vs_second",
                    "ref_first_vs_second"):
            assert mixed_record[key]["rows_differing"] == 0, (key, mixed_record[key])
        print(f"{len(mixed)} mixed rows in one predict_many, twice: equal probabilities", flush=True)
    print(json.dumps({"ref": commit, "ref_batched_path": ref_batched_path, "conditions": conditions,
                      "date": time.strftime("%Y-%m-%d %H:%M"),
                      "p50_wall_ms": out, "rounds": ROUNDS, "calls_per_round": R, "warmup": WARM,
                      "mixed": mixed_record, "gpu": torch.cuda.get_device_name()}, indent=1))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    exporting = commands.add_parser("export", help="write the reference commit's engine modules (needs git)")
    exporting.add_argument("--ref", required=True, help="the reference commit, for example main")
    exporting.add_argument("--out", type=Path, required=True, help="a new folder; its name is the package name")
    running = commands.add_parser("run", help="measure on the GPU machine")
    running.add_argument("--ref-package", type=Path, required=True, help="the folder export wrote")
    running.add_argument("--conditions", required=True,
                         help="the machine's state during the run (for example other jobs on the GPU), recorded")
    running.add_argument("--ref-batched-path", action="store_true",
                         help="run the reference engine under the reference's own BatchedPath (module docstring)")
    args = parser.parse_args()
    if args.command == "export":
        export(args.ref, args.out)
    else:
        run(args.ref_package.resolve(), args.conditions, args.ref_batched_path)


if __name__ == "__main__":
    main()
