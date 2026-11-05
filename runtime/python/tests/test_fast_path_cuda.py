"""The fast path on a real GPU with the v1.3 weights on datigator (read only, under /mnt/d). Skipped, saying why, where
there is no NVIDIA GPU or those files are absent (as on the Mac).

Run on datigator: .venv/bin/python -m pytest -m cuda runtime/python/tests/test_fast_path_cuda.py"""

import glob
import json
import random
from pathlib import Path
from typing import Any

import pytest

from jeff_runtime import LocalEngine

pytestmark = pytest.mark.cuda

BASE = Path("/mnt/d/jeff-models/v1.3/v13base/ll-v12-final")
TRIAGE_CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-triage-*/final"
TRIAGE_TEST_ROWS = Path("/mnt/d/jeff-models/v1.3/casdgx01/testsets/triage/test.jsonl")


def v13_triage() -> Path:
    """The v1.3 triage adapter's folder; skips the test when the GPU or the files are not on this machine."""
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    found = glob.glob(TRIAGE_CHECKPOINTS)
    missing = [str(path) for path in (BASE, TRIAGE_TEST_ROWS) if not path.exists()] + ([] if found else [TRIAGE_CHECKPOINTS])
    if missing:
        pytest.skip(f"needs the v1.3 weights and test rows of datigator, missing here: {missing}")
    assert len(found) == 1, f"expected one v1.3 triage checkpoint, found {found}"
    return Path(found[0])


def state_first_copy(adapter: Path, folder: Path) -> Path:
    """A copy of `adapter` whose decision_config.json declares the state-first prompt layout, its other files
    symlinked. Every real adapter is live-last, so this is the second layout. Its accuracy does not matter: the test
    is whether the fast path answers as the standard path does for the layout the adapter declares."""
    folder.mkdir()
    for source in adapter.iterdir():
        if source.name == "decision_config.json":
            config = json.loads(source.read_text())
            assert config["prompt_layout"] == "live-last", config.get("prompt_layout")
            config["prompt_layout"] = "state-first"
            (folder / source.name).write_text(json.dumps(config))
        else:
            (folder / source.name).symlink_to(source)
    return folder


# As test_batched_path_cuda.MAX_PREFIX_BATCH_DIFFERENCE (controller ruling, Task 6): a decision on a prefix made in a
# batched prefix pass against one on the same prefix made in another pass.
MAX_PREFIX_BATCH_DIFFERENCE = 0.05


def top(probabilities: list[float]) -> int:
    return probabilities.index(max(probabilities))


def test_each_adapter_is_prepared_with_its_own_layout_whatever_ran_before(tmp_path: Path) -> None:
    """Design 2b-1 Section 5 (the 2a-1 must-fix): two adapters of different prompt layouts, each called right after
    the other, must each agree with the standard path for their own layout on at least 99% of the rows, and answer
    exactly as when called after themselves. Before the fix the fast path split each call's prompts by the previous
    adapter's layout (on 80 of these rows: 68/80 and 67/80 agreed).

    The rows are every row of the prototype's 400-row sample that the fast path takes in both layouts (388 on
    2026-10-24), not only 80: on 80 rows the 99% gate would allow no miss, and one row is a near tie (fast path
    0.4997 / 0.4997, standard path 0.46 / 0.54) that the live-last adapter misses whatever ran before it.

    Since Task 7 predict runs on the batched path, whose new prefixes are made in batched prefix passes: a prefix the
    small pool evicted between the two calls is made again in another pass, which rounds differently (controller
    ruling, Task 6), so "exactly as after themselves" is within MAX_PREFIX_BATCH_DIFFERENCE. A row split by the wrong
    layout differs far more."""
    triage = v13_triage()
    adapters = {"triage": triage, "triage-sf": state_first_copy(triage, tmp_path / "triage-state-first")}
    fast = LocalEngine(BASE, adapters, backend="fast", prepare_workers=1, prefix_pool_mb=512, max_adapters=2)
    assert fast._layouts == {"base": "live-last", "triage": "live-last", "triage-sf": "state-first"}

    # the test rows that the fast path takes in both layouts (state-first puts the whole state in the new tokens,
    # and a row over 1,024 new tokens would go to the standard path and hide the bug)
    lines = TRIAGE_TEST_ROWS.read_text().splitlines()
    sample = [json.loads(line) for line in random.Random(3).sample(lines, 400)]
    rows: list[dict[str, Any]] = []
    for row in ({"state": line["state"], "question": line["question"]} for line in sample):
        if all(not isinstance(fast.fast.prepare(adapter, [row]), str) for adapter in adapters):
            rows.append(row)
    assert len(rows) >= 300, f"only {len(rows)} of 400 sampled rows fit the fast path in both layouts"

    reference = LocalEngine(BASE, adapters, backend="torch", prepare_workers=1)
    standard = {adapter: reference.predict(adapter, rows).probabilities for adapter in adapters}
    del reference

    agreement = {}
    for before, adapter in (("triage", "triage-sf"), ("triage-sf", "triage")):
        fast.predict(before, rows[:1])
        result = fast.predict(adapter, rows)
        assert result.timing["path"] == "fast"
        agreement[f"{adapter} after {before}"] = sum(
            top(answer) == top(expected) for answer, expected in zip(result.probabilities, standard[adapter]))
        fast.predict(adapter, rows[:1])
        again = fast.predict(adapter, rows).probabilities
        moved = max(abs(x - y) for a, b in zip(again, result.probabilities, strict=True)
                    for x, y in zip(a, b, strict=True))
        assert moved <= MAX_PREFIX_BATCH_DIFFERENCE, \
            f"{adapter} answers differently after itself than after {before} (by {moved})"
    print(f"agreement with the standard path out of {len(rows)}: {agreement}")
    assert all(count >= 0.99 * len(rows) for count in agreement.values()), agreement


# ---------------------------------------------------------------- Task 2: the stacked adapter pool

REPO = Path(__file__).resolve().parents[3]
CHECKPOINTS = "/mnt/d/jeff-models/v1.3/v13adapters/checkpoints/0.8b-{name}-*/final"
TEST_ROWS = "/mnt/d/jeff-models/v1.3/casdgx01/testsets/{name}/test.jsonl"


def v13(name: str) -> Path:
    """A v1.3 adapter's folder; skips the test when the GPU or the files are not on this machine."""
    import torch
    if not torch.cuda.is_available():
        pytest.skip("needs an NVIDIA GPU (torch.cuda.is_available() is False)")
    found = glob.glob(CHECKPOINTS.format(name=name))
    if not BASE.exists() or not found:
        pytest.skip(f"needs the v1.3 base and the {name} adapter of datigator")
    assert len(found) == 1, f"expected one v1.3 {name} checkpoint, found {found}"
    return Path(found[0])


def test_rows(name: str, count: int) -> list[dict[str, Any]]:
    lines = Path(TEST_ROWS.format(name=name)).read_text().splitlines()[:count]
    return [{"state": item["state"], "question": item["question"]} for item in map(json.loads, lines)]


test_rows.__test__ = False  # type: ignore[attr-defined]  # a helper, not a test


class PoolWrites:
    """Every PyTorch operation, seen from Python, that touches the storage of the stacked pools (LoRA, readout,
    temperature): a CUDA graph replay reads them without any such operation, so a switch of adapter shows none."""

    def __init__(self, fast: Any) -> None:
        self.storages = {pool.untyped_storage().data_ptr() for pool in (fast.lora_pool, fast.readout_pool,
                                                                         fast.temp_pool)}
        self.seen: list[str] = []

    def __enter__(self) -> "PoolWrites":
        import torch
        from torch.utils._python_dispatch import TorchDispatchMode

        outer = self

        class Mode(TorchDispatchMode):
            def __torch_dispatch__(self, func: Any, types: Any, args: Any = (), kwargs: Any = None) -> Any:
                tensors = [a for a in torch.utils._pytree.tree_leaves((args, kwargs or {}))
                           if isinstance(a, torch.Tensor)]
                if any(t.untyped_storage().data_ptr() in outer.storages for t in tensors):
                    outer.seen.append(str(func))
                return func(*args, **(kwargs or {}))

        self.mode = Mode()
        self.mode.__enter__()
        return self

    def __exit__(self, *exc: object) -> None:
        self.mode.__exit__(*exc)


def test_switching_adapter_copies_no_weights() -> None:
    """Before Task 2 a switch copied the adapter's ~34 MB of packed weights into the graphs' buffer. Now the
    adapter's row index travels with the decision's other numbers: decisions that switch adapter on every call
    touch the pools with no PyTorch operation at all (the graphs read them in place), while add_adapter does."""
    triage, nav = v13("triage"), v13("nav")
    engine = LocalEngine(BASE, {"triage": triage}, backend="fast", prepare_workers=1, prefix_pool_mb=512, max_adapters=2)
    fast = engine.fast
    triage_rows, nav_rows = test_rows("triage", 4), test_rows("nav", 4)
    with PoolWrites(fast) as adding:
        engine.add_adapter("nav", nav)
    assert any("copy_" in op for op in adding.seen), adding.seen  # the detector does see writes to the pools
    for name, row in (("triage", triage_rows[0]), ("nav", nav_rows[0]), ("base", triage_rows[0])):
        engine.predict(name, [row])  # cache each prefix, so the switches below run decisions only
    with PoolWrites(fast) as switching:
        for i in range(4):
            for name, row in (("triage", triage_rows[i]), ("nav", nav_rows[i]), ("base", triage_rows[i])):
                assert engine.predict(name, [row]).timing["path"] == "fast"
    assert switching.seen == [], switching.seen


def test_adapters_removed_and_added_while_serving_never_read_a_freed_row() -> None:
    """Adapters removed and added through the scheduler's change path, between passes, as the engine server does.
    Each freed row of the three pools is poisoned with NaN at once: if any decision read it, its probabilities would
    be NaN. A new adapter takes the lowest free row and overwrites all of it, so it answers exactly as the same
    adapter did from another row."""
    import math
    import threading

    import torch

    from jeff.errors import EngineError, JeffError
    from jeff_runtime.scheduler import Scheduler, Settings

    folders = {name: v13(name) for name in ("triage", "nav", "spam")}
    rows = {name: test_rows(name, 6) for name in folders}
    rows["base"] = rows["triage"]
    engine = LocalEngine(BASE, {"triage": folders["triage"], "nav": folders["nav"]}, backend="fast", prepare_workers=1,
                         prefix_pool_mb=512, max_adapters=2)
    fast = engine.fast
    scheduler = Scheduler(engine, Settings(window_ms=1, max_batch_rows=64, queue_limit_rows=4096))
    scheduler.start()

    def predict(name: str) -> list[list[float]]:
        done = threading.Event()
        box: list[Any] = []
        scheduler.submit(name, rows[name], None, lambda result: (box.append(result), done.set()))
        assert done.wait(120)
        if isinstance(box[0], JeffError):
            raise box[0]
        assert box[0].timing["path"] == "fast"
        return box[0].probabilities

    def change(work: Any) -> JeffError | None:
        done = threading.Event()
        box: list[JeffError | None] = []
        scheduler.change(work, lambda error: (box.append(error), done.set()))
        assert done.wait(120)
        return box[0]

    def poison(index: int) -> None:
        fast.lora_pool[index].fill_(float("nan"))
        fast.readout_pool[index].fill_(float("nan"))
        fast.temp_pool[index] = float("nan")
        torch.cuda.synchronize()

    def finite(values: list[list[float]]) -> bool:
        return all(math.isfinite(v) for row in values for v in row)

    try:
        before = {name: predict(name) for name in ("triage", "nav", "base")}
        assert fast.adapter_index == {None: 0, "triage": 1, "nav": 2}

        assert change(lambda: engine.remove_adapter("nav")) is None
        poison(2)
        for name in ("triage", "base"):
            assert predict(name) == before[name], name

        assert change(lambda: engine.add_adapter("spam", folders["spam"])) is None
        assert fast.adapter_index["spam"] == 2  # the freed row, overwritten
        spam = predict("spam")
        assert finite(spam)
        refused = change(lambda: engine.add_adapter("nav", folders["nav"]))
        assert isinstance(refused, EngineError) and "--max-adapters 2" in str(refused), refused

        assert change(lambda: engine.remove_adapter("triage")) is None
        poison(1)
        assert predict("spam") == spam and predict("base") == before["base"]
        assert change(lambda: engine.add_adapter("nav", folders["nav"])) is None
        assert fast.adapter_index["nav"] == 1  # nav again, now in row 1 instead of row 2
        assert predict("nav") == before["nav"]
    finally:
        scheduler.stop()


def test_the_33rd_adapter_at_max_adapters_32_is_refused() -> None:
    """Room for --max-adapters adapters is allocated at start; one more is refused with an error naming the setting
    and the count, and the 32 loaded adapters keep serving. 32 names over the nine v1.3 adapters' folders."""
    names = ("triage", "nav", "spam", "emotion", "ground", "guard", "legal-clauses", "support-intents", "tools")
    folders = [v13(name) for name in names]
    adapters = {f"a{i:02d}": folders[i % len(folders)] for i in range(32)}
    engine = LocalEngine(BASE, adapters, backend="fast", prepare_workers=1, prefix_pool_mb=512, max_adapters=32)
    assert sorted(engine.fast.adapter_index.values()) == list(range(33)) and engine.fast.free_rows == []
    from jeff.errors import EngineError

    with pytest.raises(EngineError) as refused:
        engine.add_adapter("a32", folders[0])
    assert "at most 32 adapters (--max-adapters 32) and 32 are loaded" in str(refused.value)
    assert "a32" not in engine.installed() and "a32" not in engine.adapter_set.names
    with pytest.raises(ValueError, match="--max-adapters 32"):  # the fast path refuses on its own too
        engine.fast.add_adapter("a32")
    row = test_rows("triage", 1)
    assert engine.predict("a00", row).probabilities == engine.predict("a09", row).probabilities  # both triage


# ---------------------------------------------------------------- Task 3: the prefix pool

PRE_TASK3_PREFIXES = REPO / "results/step2b1/task3/pre_task3_prefixes.json"


def digest(tensor: Any) -> str:
    import hashlib

    import torch

    data = tensor.detach().contiguous().cpu()
    raw = data.view(torch.int16) if data.dtype == torch.bfloat16 else data
    return hashlib.sha256(raw.numpy().tobytes()).hexdigest()


def test_the_pool_prefix_pass_writes_the_same_bits_as_the_cache_before_it() -> None:
    """The prefix pass now writes straight into the pages of its block table (48 state pages, 2 convolution pages,
    one key/value page per 32 tokens). For three prefix lengths of real text under the triage adapter (77, 1,001 and
    2,601 tokens: three prefix buckets, none a whole number of pages) and one under the base, the state gathered from
    the pages is bit for bit the `Prefix` tensors of the commit before the pool (recorded by
    results/step2b1/task3/record_prefixes.py at 23fb008, as SHA-256 digests, committed), and the zero page (the
    empty prefix's state) stays all zeros. Since plan 2b-1 Task 8 the prefix pass is the batched path's (a prefix
    alone: the B = 1 prefix graph), so this also holds it to the single-prefix pass it replaced, bit for bit."""
    import torch

    assert PRE_TASK3_PREFIXES.exists(), f"{PRE_TASK3_PREFIXES} is committed; copy it with the synced tree"
    recorded = json.loads(PRE_TASK3_PREFIXES.read_text())
    triage = v13("triage")
    assert str(triage) == recorded["triage"]
    engine = LocalEngine(BASE, {"triage": triage}, backend="fast", prepare_workers=1, prefix_pool_mb=2048, max_adapters=1)
    fast, batched = engine.fast, engine.batched
    pool = fast.prefixes
    with torch.inference_mode():
        for case in recorded["prefixes"]:
            index = fast.index(case["adapter"])
            [slot], timing = batched.ensure_prefixes([(index, case["text"])], 8192)
            pool.unpin([slot])
            p = case["tokens"]
            assert timing["prefix_misses"] == 1 and timing["prefix_passes"] == 1 and pool.length(slot) == p
            pages = pool.block_table(slot)
            assert len(pages) == 50 + -(-p // 32) and not {0, 1} & set(pages)
            conv, state, kv = pool.gather(slot)
            found = {"conv": digest(conv), "state": digest(state), "kv": digest(kv)}
            assert found == {kind: case[kind]["sha256"] for kind in found}, (case["adapter"], p)
            again, timing = batched.ensure_prefixes([(index, case["text"])], 8192)
            pool.unpin(again)
            assert again == [slot] and timing["prefix_hits"] == 1 and timing["prefix_misses"] == 0
    assert not pool.data[1].any()
    conv, state, _ = pool.gather(0)
    assert not conv.any() and not state.any()


def test_evicted_prefixes_answer_exactly_as_cached_ones() -> None:
    """The smallest pool (prefix_pool_mb_for(1): one prefix of 8,192 tokens) holds fewer than six of these rows'
    prefixes, so cycling through six rows with six different prefixes twice evicts the least recently used prefix
    before every decision and processes its own again. The answers are bit for bit those of an engine whose pool
    holds them all."""
    from jeff_runtime.engine.prefix_pool import prefix_pool_mb_for

    triage = v13("triage")
    rows = test_rows("triage", 6)
    roomy = LocalEngine(BASE, {"triage": triage}, backend="fast", prepare_workers=1, prefix_pool_mb=512, max_adapters=1)
    expected = [roomy.predict("triage", [row]).probabilities for row in rows]
    assert len({roomy.fast.prepare("triage", [row])[0].prefix_text for row in rows}) == 6
    assert len(roomy.fast.prefixes) == 6
    del roomy
    tight = LocalEngine(BASE, {"triage": triage}, backend="fast", prepare_workers=1, prefix_pool_mb=prefix_pool_mb_for(1), max_adapters=1)
    misses = 0
    for _ in range(2):
        for row, answer in zip(rows, expected, strict=True):
            result = tight.predict("triage", [row])
            misses += result.timing["prefix_misses"]
            assert result.probabilities == answer
            assert len(tight.fast.prefixes) < 6
    assert misses == 12


def test_a_reused_adapter_row_never_serves_the_earlier_adapters_prefixes() -> None:
    """Prefixes are found by (adapter row, prefix text). Remove triage, add nav into the same row, and decide the
    same row (the same prefix text): nav processes the prefix itself and answers as an engine that only ever had
    nav, never with triage's cached state. The same holds for an adapter replaced under its own name (as
    `reload` does with changed files: remove, then add): its prefixes are processed again."""
    triage, nav = v13("triage"), v13("nav")
    row = test_rows("triage", 1)
    engine = LocalEngine(BASE, {"triage": triage}, backend="fast", prepare_workers=1, prefix_pool_mb=512, max_adapters=1)
    engine.predict("triage", row)
    engine.predict("triage", row)  # cached
    assert engine.fast.adapter_index["triage"] == 1
    engine.remove_adapter("triage")
    assert len(engine.fast.prefixes) == 0
    engine.add_adapter("nav", nav)
    assert engine.fast.adapter_index["nav"] == 1
    result = engine.predict("nav", row)
    assert result.timing["prefix_misses"] == 1
    engine.remove_adapter("nav")
    engine.add_adapter("nav", triage)  # "nav" replaced by other weights under the same name
    replaced = engine.predict("nav", row)
    assert replaced.timing["prefix_misses"] == 1
    del engine
    assert result.probabilities == LocalEngine(BASE, {"nav": nav}, backend="fast", prepare_workers=1, prefix_pool_mb=512,
                                               max_adapters=1).predict("nav", row).probabilities
    assert replaced.probabilities == LocalEngine(BASE, {"triage": triage}, backend="fast", prepare_workers=1, prefix_pool_mb=512,
                                                 max_adapters=1).predict("triage", row).probabilities
