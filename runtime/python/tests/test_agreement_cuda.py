"""The agreement gate of plan 2b-1 (design Section 6): every v1.3 adapter with a test set on datigator must make the
same top choice as the standard path on at least 99% of its own test rows, with the rows served the way the engine
server serves them. Skipped, saying why, where there is no NVIDIA GPU or those folders are absent (as on the Mac).

- Adapters: every folder /mnt/d/jeff-models/v1.3/casdgx01/testsets/<name>/test.jsonl whose adapter
  /mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-<name>-*/final exists.
- Rows: every test row of each adapter, up to ROWS_PER_ADAPTER (2,000; all of them when there are fewer), drawn with
  a fixed seed; never calibration rows.
- Reference: the standard path of the same engine (LocalEngine._standard: the PyTorch forward pass, chunks of 8, each
  adapter's own prompt layout).
- As served: one LocalEngine on the fast backend with every adapter loaded, behind a Scheduler with
  --max-batch-rows 64. Every row is its own request, and all requests are queued, shuffled across adapters, before
  the scheduler starts, so its micro-batches of 64 rows mix adapters and prefixes, and each micro-batch runs as
  passes per new-token bucket through the batched graphs and kernel choices that serve.
- Rows the engine routes to the standard path (more than 1,024 new tokens) are counted per adapter and left out.
- A row agrees when the fast path's top option is the standard path's. A row whose top two options are exactly equal
  in the standard path (at the probabilities it returns) agrees when the fast path picks either tied option: a tie
  has no single correct answer.

Both rules are controller rulings of Task 8 of the batched fast path's plan. With 200 rows
each, emotion agreed on 195 (97.5%): five near ties, two of them exact ties in the reference, and the single-row path
it replaced scored 98.5% on the same rows; at 200 rows one near tie moves the result by 0.5 points. On 2,000 emotion
rows the replaced path scored 98.85% and the batched path 99.05% to 99.35% (43 exact ties in the reference). The raw
agreement, without the tie rule, is recorded beside it for every adapter.

Results (per adapter: rows, agreement, largest probability difference, every disagreeing row; and how the rows were
batched) go to results/step2b1/agreement.json.

Run on datigator: .venv/bin/python -m pytest -m cuda runtime/python/tests/test_agreement_cuda.py -s"""

from __future__ import annotations

import glob
import importlib.metadata
import json
import random
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

pytestmark = pytest.mark.cuda

REPO = Path(__file__).resolve().parents[3]
BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
TEST_SETS = Path("/mnt/d/jeff-models/v1.3/casdgx01/testsets")
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
# The v1.3 adapters with test sets on datigator: the gate must judge all of them, never fewer.
EXPECTED = ("emotion", "ground", "guard", "legal-clauses", "nav", "spam", "support-intents", "tools", "triage")
RESULTS = REPO / "results/step2b1/agreement.json"
ROWS_PER_ADAPTER = 2000
SEED = 7
GATE = 0.99  # the owner's gate: top-option agreement with the standard path, per adapter
MAX_BATCH_ROWS = 64  # jeff runtime serve's --max-batch-rows as deployed
PREFIX_POOL_MB = 8192  # as deployed (JEFF_PREFIX_POOL_MB in deploy/*.env.example)


def top(probabilities: list[float]) -> int:
    return probabilities.index(max(probabilities))


def largest_difference(a: list[float], b: list[float]) -> float:
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def adapters_with_test_sets() -> dict[str, Path]:
    """{name: adapter folder} for each of the EXPECTED adapters. Skips the test, naming what is missing, when the GPU
    or the v1.3 folders are absent (as on the Mac); where the folders exist, a missing test set or checkpoint of any
    EXPECTED adapter fails the test, so the gate never passes on fewer adapters."""
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    missing = [str(path) for path in (BASE, TEST_SETS) if not path.exists()]
    if missing:
        pytest.skip(f"needs the v1.3 weights and test sets of datigator, missing here: {missing}")
    found: dict[str, Path] = {}
    for test_set in sorted(TEST_SETS.glob("*/test.jsonl")):
        name = test_set.parent.name
        folders = glob.glob(CHECKPOINTS.format(name=name))
        assert len(folders) <= 1, f"expected at most one v1.3 {name} checkpoint, found {folders}"
        if folders:
            found[name] = Path(folders[0])
    missing_adapters = sorted(set(EXPECTED) - set(found))
    assert not missing_adapters, (f"v1.3 adapters without a test set under {TEST_SETS} or a checkpoint matching "
                                  f"{CHECKPOINTS}: {missing_adapters}")
    return {name: found[name] for name in EXPECTED}


def sample(name: str) -> list[tuple[int, dict[str, Any]]]:
    """(line number, row) for up to ROWS_PER_ADAPTER test rows of the adapter (all when fewer), drawn with the fixed
    seed."""
    lines = (TEST_SETS / name / "test.jsonl").read_text().splitlines()
    numbers = sorted(random.Random(f"{SEED}:{name}").sample(range(len(lines)), min(ROWS_PER_ADAPTER, len(lines))))
    return [(number, {"state": item["state"], "question": item["question"]})
            for number, item in ((number, json.loads(lines[number])) for number in numbers)]


def test_every_adapter_agrees_with_the_standard_path_as_served() -> None:
    import torch

    from jeff.errors import JeffError
    from jeff_runtime import LocalEngine
    from jeff_runtime.scheduler import Scheduler, Settings

    folders = adapters_with_test_sets()
    engine = LocalEngine(BASE, folders, backend="fast", prepare_workers=1, prefix_pool_mb=PREFIX_POOL_MB,
                         max_adapters=len(folders))
    rows = {name: sample(name) for name in folders}

    # the reference: the standard path of this engine, adapter by adapter
    started = time.perf_counter()
    standard: dict[str, list[list[float]]] = {}
    for name, items in rows.items():
        engine.adapter_set.use(name)
        standard[name] = engine._standard([row for _, row in items], engine._layout(name))[0]
    standard_seconds = time.perf_counter() - started

    # as served: every row its own request, all queued shuffled before the scheduler starts
    requests = [(name, i) for name, items in rows.items() for i in range(len(items))]
    random.Random(SEED).shuffle(requests)
    scheduler = Scheduler(engine, Settings(window_ms=1, max_batch_rows=MAX_BATCH_ROWS,
                                           queue_limit_rows=len(requests)))
    answers: dict[tuple[str, int], Any] = {}
    finished = threading.Event()
    lock = threading.Lock()

    def deliver(key: tuple[str, int]) -> Any:
        def on_done(result: Any) -> None:
            with lock:
                answers[key] = result
                if len(answers) == len(requests):
                    finished.set()
        return on_done

    for name, i in requests:
        scheduler.submit(name, [rows[name][i][1]], None, deliver((name, i)))
    started = time.perf_counter()
    scheduler.start()
    try:
        assert finished.wait(1800), f"{len(answers)} of {len(requests)} requests answered in 30 minutes"
    finally:
        scheduler.stop()
    served_seconds = time.perf_counter() - started
    failed = {key: str(result) for key, result in answers.items() if isinstance(result, JeffError)}
    assert not failed, failed

    per_adapter: dict[str, dict[str, Any]] = {}
    pass_rows: Counter[int] = Counter()
    adapters_in_pass: Counter[int] = Counter()
    buckets: Counter[int] = Counter()
    batch_rows: Counter[int] = Counter()
    for name, items in rows.items():
        agree, raw, ties, tie_rescued, worst, compared, disagreements, routed = 0, 0, 0, 0, 0.0, 0, [], 0
        for i, (number, _) in enumerate(items):
            result = answers[(name, i)]
            if result.timing["path"] != "fast":
                assert result.timing["path"] == "standard" and "reason" in result.timing, result.timing
                routed += 1
                continue
            got, expected = result.probabilities[0], standard[name][i]
            compared += 1
            worst = max(worst, largest_difference(got, expected))
            tied = [k for k, p in enumerate(expected) if p == max(expected)]
            ties += len(tied) > 1
            if top(got) == top(expected):
                agree += 1
                raw += 1
            elif len(tied) > 1 and top(got) in tied:
                agree += 1
                tie_rescued += 1
            else:
                ranked = sorted(expected, reverse=True)
                disagreements.append({"line": number, "fast_top": top(got), "standard_top": top(expected),
                                      "fast": [round(p, 4) for p in got],
                                      "standard": [round(p, 4) for p in expected],
                                      "standard_margin": round(ranked[0] - ranked[1], 4),
                                      "pass_rows": result.timing["pass_rows"][0],
                                      "bucket": result.timing["bucket"][0]})
            pass_rows[result.timing["pass_rows"][0]] += 1
            adapters_in_pass[result.timing["adapters_in_pass"][0]] += 1
            buckets[result.timing["bucket"][0]] += 1
            batch_rows[result.timing["batch_rows"]] += 1
        per_adapter[name] = {"rows": len(items), "routed_to_standard": routed, "compared": compared,
                             "agreement": round(agree / compared, 4), "agreeing": agree,
                             "raw_agreement": round(raw / compared, 4), "raw_agreeing": raw,
                             "exact_ties_in_reference": ties, "agreeing_by_the_tie_rule": tie_rescued,
                             "largest_probability_difference": round(worst, 4), "disagreements": disagreements}
    failures = [f"{name}: {r['agreeing']}/{r['compared']} = {r['agreement']:.2%} < {GATE:.0%}"
                for name, r in per_adapter.items() if r["agreeing"] < GATE * r["compared"]]
    record = {
        "test": "runtime/python/tests/test_agreement_cuda.py::test_every_adapter_agrees_with_the_standard_path_as_served",
        "date": time.strftime("%Y-%m-%d %H:%M"),
        "versions": {"torch": torch.__version__, "cuda": str(torch.version.cuda), "gpu": torch.cuda.get_device_name(),
                     **{d: importlib.metadata.version(d) for d in ("triton", "flash-linear-attention")}},
        "setup": {"adapters": {name: str(folder) for name, folder in folders.items()},
                  "rows_per_adapter": f"all test rows, at most {ROWS_PER_ADAPTER}", "seed": SEED, "gate": GATE,
                  "tie_rule": "a row whose top two standard-path probabilities are exactly equal agrees when the fast "
                              "path picks either tied option",
                  "reference": "LocalEngine._standard (PyTorch forward pass, chunks of 8, each adapter's layout)",
                  "served": f"Scheduler(window_ms=1, max_batch_rows={MAX_BATCH_ROWS}) over LocalEngine(backend=fast, "
                            f"prefix_pool_mb={PREFIX_POOL_MB}); one request per row, all queued shuffled across "
                            "adapters before the scheduler started"},
        "seconds": {"standard": round(standard_seconds, 1), "served": round(served_seconds, 1)},
        "batching": {"micro_batch_rows": dict(sorted(batch_rows.items())),
                     "pass_rows": dict(sorted(pass_rows.items())),
                     "adapters_in_pass": dict(sorted(adapters_in_pass.items())),
                     "new_token_bucket": dict(sorted(buckets.items()))},
        "per_adapter": per_adapter,
        "failures": failures,
    }
    RESULTS.parent.mkdir(parents=True, exist_ok=True)
    RESULTS.write_text(json.dumps(record, indent=1) + "\n")
    for name, r in per_adapter.items():
        print(f"{name}: {r['agreeing']}/{r['compared']} ({r['agreement']:.2%}; raw {r['raw_agreeing']}, "
              f"{r['raw_agreement']:.2%}; {r['exact_ties_in_reference']} exact ties), largest difference "
              f"{r['largest_probability_difference']}, routed to standard {r['routed_to_standard']}")
    print(f"batching: {record['batching']}")
    assert max(adapters_in_pass) > 1, "no pass mixed adapters"
    assert not failures, failures
