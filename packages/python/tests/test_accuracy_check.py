"""Each catalogue adapter, run through the new runtime on a fixed sample of its test set, must reach its published
accuracy within the sample's 99.6% interval (95% across all 13 adapters), allowing for the published value's rounding.

Two modes run on the same sample:
- engine mode sends each row's own state and question to the engine unchanged. That is how the published accuracy was
  measured, so this mode is asserted: it proves the runtime reproduces the trained model.
- template mode rebuilds each row as the Call a user would make (the manifest's instructions and label texts). It is
  recorded, not asserted: a gap shows where the manifest's canonical request differs from the rows' wording.

Run alone (one model in memory at a time): uv run pytest -m slow packages/python/tests/test_accuracy_check.py -q"""

import gc
import json
from pathlib import Path

import pytest

from jeff import Jeff
from jeff.catalogue import Catalogue
from jeff.evaluate import engine_score, published_set, score, wilson
from jeff.manifest import Manifest

pytestmark = pytest.mark.slow
SAMPLE = 400
# The gate uses a 99.6% interval (z = 2.97), so that the 13 adapters together have a 95% chance of all passing when
# each reproduces its published accuracy. The results files keep 95% intervals.
GATE_Z = 2.97
ROUNDING = 0.0005  # published accuracies are rounded to 0.1 point
ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "results" / "step1-accuracy.json"
TABLE = ROOT / "results" / "step1-accuracy.md"


def published(manifest: Manifest) -> float | None:
    results = manifest.raw["hub"]["results"]
    value = results[0]["rows"]["adapter"]["value"] if results else None
    return None if value is None else value / 100


def percent(value: float | None) -> str:
    return "-" if value is None else f"{value:.1%}"


def write_table(record: dict) -> None:
    lines = ["# Step 1 accuracy check", "",
             f"A fixed-seed sample of {SAMPLE} test rows per adapter. Engine mode sends each row as written (the "
             "published measurement); template mode rebuilds each row as a user's call through the manifest and skips "
             "rows of shapes the manifest does not describe. Intervals are 95% Wilson intervals.", "",
             "| Adapter | Published | Engine acc [interval] | Template acc [interval] | Skipped |",
             "|---|---|---|---|---|"]
    for adapter, item in sorted(record.items()):
        engine, template = item["engine"], item["template"]
        lines.append(f"| {adapter} | {percent(item['published'])} | {percent(engine['accuracy'])} "
                     f"[{percent(engine['interval'][0])}, {percent(engine['interval'][1])}] | "
                     f"{percent(template['accuracy'])} [{percent(template['interval'][0])}, "
                     f"{percent(template['interval'][1])}] | {template['skipped']} |")
    TABLE.write_text("\n".join(lines) + "\n")


@pytest.mark.parametrize("name", sorted(m.aliases[0] for m in Catalogue.shipped().manifests()))
def test_adapter_matches_published_accuracy(name: str) -> None:
    jeff = Jeff.local([name])
    try:
        manifest = jeff.catalogue.adapter(name)
        chosen = published_set(manifest, "test", SAMPLE)
        engine = engine_score(jeff.engine, manifest.id, chosen.raw)
        reports = score(jeff, chosen.rows)
    finally:
        backend = jeff.engine.backend
        del jeff
        gc.collect()
        if backend == "mlx":
            import mlx.core as mx
            mx.clear_cache()
    right, n = sum(r.correct for r in reports), sum(r.n for r in reports)
    template_interval = wilson(right, n)
    target = published(manifest)
    gate = wilson(engine.correct, engine.n, z=GATE_Z)
    record = json.loads(RESULTS.read_text()) if RESULTS.exists() else {}
    record[manifest.id] = {
        "published": target, "sample": SAMPLE, "seed": 0,
        "engine": {"n": engine.n, "correct": engine.correct, "accuracy": engine.accuracy,
                   "interval": list(engine.interval), "gate_interval_99_6": list(gate), "p50_ms": engine.p50_ms},
        "template": {"n": n, "correct": right, "accuracy": right / n, "interval": list(template_interval),
                     "skipped": sum(chosen.skipped.values()), "skipped_by_reason": chosen.skipped,
                     "functions": {r.function: {"n": r.n, "accuracy": r.accuracy, "interval": list(r.interval)}
                                   for r in reports}}}
    RESULTS.parent.mkdir(exist_ok=True)
    RESULTS.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n")
    write_table(record)
    if target is None:
        pytest.skip(f"{manifest.id} has no published accuracy; engine mode measured {engine.accuracy:.1%}")
    low, high = gate
    assert (target - ROUNDING <= high and target + ROUNDING >= low) or engine.accuracy >= target, \
        f"{manifest.id}: engine mode {engine.accuracy:.1%} [99.6%: {low:.2%}, {high:.2%}] vs published {target:.1%}"
