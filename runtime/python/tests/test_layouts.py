"""Each request is prepared with its own adapter's prompt layout, never the layout of whichever adapter ran last
(the 2a-1 must-fix, design 2b-1 Section 5). Runs without weights or a GPU: a fake model, adapter set and chat
template, the real FastPath.prepare/split and LocalEngine._predict.

fast_path imports Triton at the top for its GPU kernels; on a machine without Triton (the Mac) a stand-in module is
installed for the import only, so the CPU-side code (prepare, split) can be tested. No kernel is ever run here."""

import importlib
import itertools
import sys
import threading
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from jeff_runtime import LocalEngine

STATE = {"customer": "Ann", "message": "My parcel arrived crushed."}
ROW = {"state": STATE, "question": {"type": "choice", "instructions": "Which team?",
                                    "criteria": {"refunds": "Refunds", "parcels": "Damaged parcels"}}}


@pytest.fixture
def fast_path() -> Iterator[Any]:
    """The fast_path module, imported with a stand-in Triton when the real one is not installed. The stand-in is in
    sys.modules only while fast_path itself is imported (torch and transformers check for Triton when they load, so
    model.py and everything it pulls in are imported first), and that copy of fast_path is dropped afterwards."""
    if importlib.util.find_spec("triton") is not None:
        yield importlib.import_module("jeff_runtime.engine.fast_path")
        return
    importlib.import_module("jeff_runtime.engine.model")
    triton = types.ModuleType("triton")
    triton.jit = lambda function: function  # type: ignore[attr-defined]
    language = types.ModuleType("triton.language")
    triton.language = language  # type: ignore[attr-defined]
    sys.modules["triton"], sys.modules["triton.language"] = triton, language
    try:
        module = importlib.import_module("jeff_runtime.engine.fast_path")
    finally:
        del sys.modules["triton"], sys.modules["triton.language"]
    try:
        yield module
    finally:
        del sys.modules["jeff_runtime.engine.fast_path"]
        delattr(sys.modules["jeff_runtime.engine"], "fast_path")


class _ChatTemplate:
    """Renders messages the way the Qwen chat template does for these prompts (system turn, user turn, an open
    assistant turn), which is all split() relies on."""

    def apply_chat_template(self, messages: list[dict[str, Any]], *, tokenize: bool, add_generation_prompt: bool,
                            enable_thinking: bool) -> str:
        system, user = messages
        text = "".join(part["text"] for part in user["content"] if part["type"] == "text")
        return (f"<|im_start|>system\n{system['content']}<|im_end|>\n<|im_start|>user\n{text}<|im_end|>\n"
                "<|im_start|>assistant\n<think>\n\n</think>\n\n")


class _Tokenizer:
    def __call__(self, text: str, add_special_tokens: bool = False) -> dict[str, list[int]]:
        return {"input_ids": list(range(len(text.split())))}


class _Model:
    """A DecisionModel stand-in: its prompt_layout is whatever the adapter set selected last."""
    codes = [chr(ord("a") + i) for i in range(26)]
    processor = _ChatTemplate()
    prompt_layout = "live-last"


class _Head:
    def __init__(self, prompt_layout: str | None) -> None:
        self.prompt_layout = prompt_layout


class _AdapterSet:
    """As jeff_runtime.engine.lora.AdapterSet.use: selecting an adapter sets the model's prompt layout."""

    def __init__(self, model: _Model, layouts: dict[str | None, str | None]) -> None:
        self.model = model
        self.heads = {name: _Head(layout) for name, layout in layouts.items()}

    def use(self, name: str | None) -> None:
        layout = self.heads[name].prompt_layout
        if layout is not None:
            self.model.prompt_layout = layout


LAYOUTS: dict[str | None, str | None] = {None: "live-last", "live": "live-last", "first": "state-first",
                                         "inherits": None}


def fast_path_on(module: Any, model: _Model, adapter_set: _AdapterSet) -> Any:
    """A FastPath with only its CPU side (no GPU buffers, no graphs): the model, the tokenizer and the layout table
    filled as FastPath.__init__ and add_adapter fill it."""
    fast = object.__new__(module.FastPath)
    fast.model, fast.tokenizer, fast.adapter_set = model, _Tokenizer(), adapter_set
    fast.layouts = {None: adapter_set.heads[None].prompt_layout}
    for name in adapter_set.heads:
        if name is not None:
            fast.add_layout(name)
    return fast


def test_prepare_uses_the_adapters_own_layout_whatever_ran_before(fast_path: Any) -> None:
    model = _Model()
    adapter_set = _AdapterSet(model, LAYOUTS)
    fast = fast_path_on(fast_path, model, adapter_set)
    for first, second in (("live", "first"), ("first", "live"), ("first", "inherits"), ("live", None)):
        adapter_set.use(first)  # the model's prompt layout is now the first adapter's
        prepared = fast.prepare(second, [ROW])
        assert not isinstance(prepared, str)
        prefix_text = prepared[0].prefix_text
        expected = LAYOUTS[second] or LAYOUTS[None]  # an adapter that names no layout has the base's
        if expected == "live-last":
            assert prefix_text.endswith(fast_path.LATEST), (first, second, prefix_text[-60:])
        else:
            assert prefix_text.endswith(fast_path.USER_TURN), (first, second, prefix_text[-60:])
        assert prefix_text == fast.split(ROW, expected)[0]


def test_split_takes_the_layout_as_an_argument(fast_path: Any) -> None:
    model = _Model()
    fast = fast_path_on(fast_path, model, _AdapterSet(model, LAYOUTS))
    live_prefix, live_new = fast.split(ROW, "live-last")
    first_prefix, first_new = fast.split(ROW, "state-first")
    assert live_prefix.endswith(fast_path.LATEST) and "parcel arrived crushed" in live_new
    assert first_prefix.endswith(fast_path.USER_TURN) and first_new.startswith("State:\n")
    model.prompt_layout = "state-first"
    assert fast.split(ROW, "live-last") == (live_prefix, live_new)  # the model's current layout is never read


def test_prepare_refuses_an_adapter_the_fast_path_has_not_loaded(fast_path: Any) -> None:
    model = _Model()
    fast = fast_path_on(fast_path, model, _AdapterSet(model, LAYOUTS))
    with pytest.raises(ValueError, match="'missing'.*not loaded"):
        fast.prepare("missing", [ROW])


class _Processor(_ChatTemplate):
    """A preparation processor: the chat template and a tokenizer (one id per word)."""
    tokenizer = _Tokenizer()


class _Pool:
    """The prefix pool as LocalEngine._fast_many reads it: each slot's prefix length, and pins undone."""

    def __init__(self) -> None:
        self.unpinned: list[list[int]] = []

    def length(self, slot: int) -> int:
        return 5

    def unpin(self, slots: list[int]) -> None:
        self.unpinned.append(list(slots))


class _Batched:
    """BatchedPath as LocalEngine._fast_many calls it: a prefix slot per row (one per distinct prefix), then each
    row's answer [adapter index, number of new tokens], with the per-row timing of passes per bucket (a row's bucket
    here: 32 up to 32 new tokens, else 64)."""

    def __init__(self) -> None:
        self.calls: list[list[tuple[int, int, Any]]] = []

    def ensure_prefixes(self, rows: list[tuple[int, str]], max_tokens: int) -> tuple[list[int], dict[str, Any]]:
        slots = {key: n for n, key in enumerate(dict.fromkeys(rows), start=1)}
        return [slots[key] for key in rows], {"prefix_ms": 0.5, "prefix_misses": len(slots), "prefix_hits": 0,
                                              "prefix_shared": len(rows) - len(slots), "prefix_passes": 1}

    def decide(self, rows: list[tuple[int, int, Any]]) -> tuple[list[list[float]], list[int], dict[str, Any]]:
        self.calls.append(list(rows))
        buckets = [32 if len(row.new_ids) <= 32 else 64 for _, _, row in rows]
        return ([[float(index), float(len(row.new_ids))] for index, _, row in rows],
                [5 + len(row.new_ids) for _, _, row in rows],
                {"model_ms": 1.0, "new_tokens": 0, "passes": len(set(buckets)),
                 "batch_rows": [buckets.count(b) for b in buckets], "bucket": buckets,
                 "adapters_in_pass": [len({rows[i][0] for i in range(len(rows)) if buckets[i] == b}) for b in buckets]})


def fast_engine(fast_path: Any, model: Any, adapter_set: Any, layouts: dict[str, str]) -> LocalEngine:
    """A LocalEngine on the fast backend with only its CPU side: the real prepare() with a preparation processor, and
    a fake batched path and prefix pool."""
    from jeff_runtime.local_engine import Preparers

    engine = object.__new__(LocalEngine)
    engine.backend, engine.model, engine.adapter_set = "fast", model, adapter_set
    engine.fast = types.SimpleNamespace(index=lambda name: {None: 0, "live": 1, "first": 2}[name], prefixes=_Pool())
    engine.batched = _Batched()
    engine._prefixes_at_once = 64
    engine.max_tokens = 8192
    engine.base_format = {"format": "bf16"}
    engine._adapters = {name: Path("unused") for name in layouts if name != "base"}
    engine._max_options = {name: 26 for name in layouts}
    engine._layouts = dict(layouts)
    engine._adapters_lock = threading.Lock()
    engine._loads = itertools.count(1)
    engine._generations = {name: next(engine._loads) for name in layouts}
    engine._preparers = Preparers([_Processor()])
    return engine


def test_local_engine_prepares_each_call_with_its_adapters_layout(fast_path: Any) -> None:
    """LocalEngine.predict on the fast backend, alternating adapters of different layouts: each call's rows reach the
    batched path split by the called adapter's layout. Before the fix, prepare ran before the adapter was selected, so
    it used the previous call's layout."""
    model = _Model()
    adapter_set = _AdapterSet(model, LAYOUTS)
    engine = fast_engine(fast_path, model, adapter_set, {"base": "live-last", "live": "live-last",
                                                         "first": "state-first"})
    for adapter in ("live", "first", "live", "first", "first"):
        assert engine.predict(adapter, [ROW]).timing["path"] == "fast"
    tails = [(index, row.prefix_text.endswith(fast_path.LATEST)) for call in engine.batched.calls
             for index, _, row in call]
    assert tails == [(1, True), (2, False), (1, True), (2, False), (2, False)]


def wordy(words: int) -> dict[str, Any]:
    return {"state": {"message": " ".join(["word"] * words)}, "question": ROW["question"]}


def test_predict_many_runs_every_adapters_fast_rows_together_and_answers_each_group_in_its_order(
        fast_path: Any) -> None:
    model = _Model()
    engine = fast_engine(fast_path, model, _AdapterSet(model, LAYOUTS), {"base": "live-last", "live": "live-last",
                                                                         "first": "state-first"})
    groups = [("live", engine.prepare("live", [wordy(50), wordy(1), wordy(60)])),
              ("first", engine.prepare("first", [wordy(2)])),
              ("base", engine.prepare("base", [wordy(70), wordy(3)]))]
    assert [prepared.route for _, prepared in groups] == ["fast"] * 3
    results = engine.predict_many(groups)
    assert len(engine.batched.calls) == 1 and len(engine.batched.calls[0]) == 6  # one batched call for all
    assert engine.fast.prefixes.unpinned == [[1, 1, 1, 2, 3, 3]]  # each pin undone once the passes are over
    live, first, base = results
    assert [row[0] for row in live.probabilities] == [1.0, 1.0, 1.0] and [row[0] for row in base.probabilities] == [0.0,
                                                                                                                  0.0]
    new = [[row[1] for row in result.probabilities] for result in results]
    assert new[0][0] > 32 and new[0][1] <= 32 and new[0][2] > 32  # each group's rows in its own order
    assert live.row_timing is not None and live.row_timing["bucket"] == [64, 32, 64]
    assert base.row_timing is not None and base.row_timing["bucket"] == [64, 32]
    assert live.row_timing["adapters_in_pass"] == [2, 3, 2] and first.row_timing["pass_rows"] == [3]
    assert live.input_tokens == [5 + int(n) for n in new[0]]
    assert live.timing["path"] == "fast" and live.timing["prefix_shared"] == 3 and live.timing["passes"] == 2
    assert live.row_timing["new_tokens"] == [int(n) for n in new[0]] and first.row_timing["new_tokens"] == [int(new[1][0])]
    assert "new_tokens" not in live.timing  # per row only, never a group total


def test_the_scheduler_gives_each_request_on_one_adapter_its_own_new_tokens(fast_path: Any) -> None:
    """Two requests on one adapter join one group of one micro-batch (and the second is chunked over two); each
    request's new_tokens are its own rows' (a list, one per row, in row order), never the group's total."""
    from jeff_runtime.scheduler import Scheduler, Settings

    model = _Model()
    engine = fast_engine(fast_path, model, _AdapterSet(model, LAYOUTS), {"base": "live-last", "live": "live-last"})
    scheduler = Scheduler(engine, Settings(window_ms=50, max_batch_rows=3, queue_limit_rows=64))
    scheduler.start()
    results: list[Any] = [None, None]
    done = [threading.Event(), threading.Event()]

    def finisher(i: int) -> Any:
        def on_done(result: Any) -> None:
            results[i] = result
            done[i].set()
        return on_done

    scheduler.submit("live", [wordy(10), wordy(40)], None, finisher(0))
    scheduler.submit("live", [wordy(3), wordy(70), wordy(5)], None, finisher(1))
    assert all(event.wait(10) for event in done)
    scheduler.stop()
    first, second = results
    assert not isinstance(first, Exception) and not isinstance(second, Exception)
    new = [[int(row[1]) for row in result.probabilities] for result in results]
    assert new[0][0] < new[0][1] and new[1][1] > new[1][0]  # each row's own answer
    assert first.timing["batch_rows"] == 3 and second.timing["batch_rows"] == 2  # shared, then chunked
    assert first.timing["new_tokens"] == new[0] and second.timing["new_tokens"] == new[1]


def test_fast_rows_run_in_runs_of_as_many_prefixes_as_the_pool_holds_at_once(fast_path: Any) -> None:
    """A direct predict with more distinct prefixes than the prefix pool is sure to hold at once runs in several runs
    (each made sure of, decided and unpinned before the next), answers still in row order."""
    model = _Model()
    engine = fast_engine(fast_path, model, _AdapterSet(model, LAYOUTS), {"base": "live-last", "live": "live-last"})
    engine._prefixes_at_once = 2

    def on(screen: str, words: int) -> dict[str, Any]:
        return {"state": {"screen": screen, "message": " ".join(["word"] * words)}, "question": ROW["question"]}

    rows = [on("a", 1), on("a", 2), on("b", 3), on("c", 4), on("c", 5), on("a", 6)]
    result = engine.predict("live", rows)
    assert [len(call) for call in engine.batched.calls] == [3, 3]
    assert len(engine.fast.prefixes.unpinned) == 2
    new = [int(row[1]) for row in result.probabilities]
    assert new == sorted(new) and len(set(new)) == 6  # each row's own answer, in row order
    assert result.row_timing is not None and len(result.row_timing["bucket"]) == 6


class _LoadingAdapterSet(_AdapterSet):
    """An adapter set whose adapters can be removed and added again, each time with the layout in `next_layout`."""

    next_layout = "live-last"

    def add_adapter(self, name: str, path: Path) -> None:
        self.heads[name] = _Head(self.next_layout)

    def remove_adapter(self, name: str) -> None:
        del self.heads[name]


def reloading_engine(fast_path: Any, tmp_path: Path) -> tuple[LocalEngine, _LoadingAdapterSet, Path]:
    """fast_engine whose add_adapter and remove_adapter run for real (LocalEngine's own), over fakes."""
    model = _Model()
    adapter_set = _LoadingAdapterSet(model, LAYOUTS)
    engine = fast_engine(fast_path, model, adapter_set, {"base": "live-last", "live": "live-last",
                                                         "first": "state-first"})
    engine.fast.add_adapter = lambda name: None
    engine.fast.remove_adapter = lambda name: None
    engine.max_adapters = None
    folder = tmp_path / "first"
    folder.mkdir()
    (folder / "decision_config.json").write_text('{"max_options": 26}')
    return engine, adapter_set, folder


def test_rows_prepared_before_their_adapter_was_loaded_again_are_refused(fast_path: Any, tmp_path: Path) -> None:
    """A reload (remove, then add) between a request's preparation and its micro-batch: its rows were split by the
    adapter's old layout and checked against its old option limit, so they must not run on the new weights. Both a
    fast-route and a standard-route group of that adapter get NotInstalled naming it; another adapter's group runs."""
    from PIL import Image

    from jeff.errors import NotInstalled
    from jeff_runtime.local_engine import Preparers

    class _ImageProcessor(_Processor):
        def __call__(self, *, text: list[str], images: list[Any], padding: bool, return_tensors: str) -> Any:
            return {"input_ids": types.SimpleNamespace(shape=(1, 12))}

    engine, adapter_set, folder = reloading_engine(fast_path, tmp_path)
    engine._preparers = Preparers([_ImageProcessor()])
    image = Image.new("RGB", (4, 4))
    groups = [("live", engine.prepare("live", [ROW])), ("first", engine.prepare("first", [ROW])),
              ("first", engine.prepare("first", [{**ROW, "images": [image]}]))]
    assert [prepared.route for _, prepared in groups] == ["fast", "fast", "standard: images"]
    engine.remove_adapter("first")
    adapter_set.next_layout = "live-last"  # loaded again with another layout
    engine.add_adapter("first", folder)
    assert engine._layouts["first"] == "live-last"
    live, first_fast, first_standard = engine.predict_many(groups)
    for refused in (first_fast, first_standard):
        assert isinstance(refused, NotInstalled) and "first" in str(refused) and "after this request" in str(refused)
    assert not isinstance(live, Exception)
    assert [index for index, _, _ in engine.batched.calls[0]] == [1]  # only the live adapter's row ran
    # prepared again, the request runs, split by the new layout
    [again] = engine.predict_many([("first", engine.prepare("first", [ROW]))])
    assert not isinstance(again, Exception)
    assert engine.batched.calls[-1][0][2].prefix_text.endswith(fast_path.LATEST)


def test_rows_prepared_before_their_adapter_was_removed_are_refused(fast_path: Any, tmp_path: Path) -> None:
    from jeff.errors import NotInstalled

    engine, _, _ = reloading_engine(fast_path, tmp_path)
    groups = [("live", engine.prepare("live", [ROW])), ("first", engine.prepare("first", [ROW]))]
    engine.remove_adapter("first")
    live, first = engine.predict_many(groups)
    assert isinstance(first, NotInstalled) and not isinstance(live, Exception)
    assert [index for index, _, _ in engine.batched.calls[0]] == [1]


def test_standard_route_requests_keep_their_own_reason(fast_path: Any) -> None:
    """Rows sent to the standard path for different reasons are never joined into one group (the reason is part of
    the route), so each request's timing names its own."""
    from jeff.engine import Prepared

    model = _Model()
    engine = fast_engine(fast_path, model, _AdapterSet(model, LAYOUTS), {"base": "live-last", "live": "live-last"})
    images = Prepared([ROW], ["images"], "standard: images", engine._generations["live"])
    long = Prepared([ROW], ["more than 1024 new tokens"], "standard: more than 1024 new tokens",
                    engine._generations["live"])
    with pytest.raises(Exception, match="several routes"):
        Prepared.join([images, long])
    engine.adapter_set.use = lambda name: None
    engine._standard = lambda rows, layout: ([[0.5, 0.5] for _ in rows], [3 for _ in rows])
    first, second = engine.predict_many([("live", images), ("live", long)])
    assert first.timing["reason"] == "images" and second.timing["reason"] == "more than 1024 new tokens"


def test_a_failed_decision_leaves_no_slot_pinned(fast_path: Any) -> None:
    """When the batched passes fail, every prefix slot the run pinned is unpinned before the error goes on."""
    model = _Model()
    engine = fast_engine(fast_path, model, _AdapterSet(model, LAYOUTS), {"base": "live-last", "live": "live-last",
                                                                         "first": "state-first"})

    def failing(rows: list[Any]) -> Any:
        raise RuntimeError("the pass failed")

    engine.batched.decide = failing
    groups = [("live", engine.prepare("live", [wordy(1), wordy(2)])), ("first", engine.prepare("first", [wordy(3)]))]
    with pytest.raises(RuntimeError, match="the pass failed"):
        engine.predict_many(groups)
    assert engine.fast.prefixes.unpinned == [[1, 1, 2]]


def test_a_fast_row_whose_prefix_is_longer_than_the_pool_holds_is_refused_at_preparation(fast_path: Any) -> None:
    """With a max_tokens above the longest prefix (8,192 tokens), a row whose prefix is longer still fits the request
    limit; it is refused at preparation (InputTooLong, its own request only), not in the micro-batch."""
    from jeff.errors import InputTooLong
    from jeff_runtime.engine.prefix_pool import LONGEST_PREFIX_TOKENS

    model = _Model()
    engine = fast_engine(fast_path, model, _AdapterSet(model, LAYOUTS), {"base": "live-last", "live": "live-last"})
    engine.max_tokens = 3 * LONGEST_PREFIX_TOKENS
    long_screen = {"state": {"screen": " ".join(["word"] * (LONGEST_PREFIX_TOKENS + 10)), "message": "hi"},
                   "question": ROW["question"]}
    with pytest.raises(InputTooLong, match=f"fast path holds at most {LONGEST_PREFIX_TOKENS}"):
        engine.prepare("live", [long_screen])


def test_rows_with_images_are_routed_to_the_standard_path_at_preparation(fast_path: Any) -> None:
    from PIL import Image

    from jeff_runtime.local_engine import Preparers

    model = _Model()
    engine = fast_engine(fast_path, model, _AdapterSet(model, LAYOUTS), {"base": "live-last", "live": "live-last"})
    image = Image.new("RGB", (4, 4))

    class _ImageProcessor(_Processor):
        def __call__(self, *, text: list[str], images: list[Any], padding: bool, return_tensors: str) -> Any:
            return {"input_ids": types.SimpleNamespace(shape=(1, 12))}

    engine._preparers = Preparers([_ImageProcessor()])
    prepared = engine.prepare("live", [ROW, {**ROW, "images": [image]}])
    assert prepared.route == "standard: images" and prepared.items == ["images", "images"]
    assert engine.prepare("live", [ROW]).route == "fast"


def test_a_fast_row_longer_than_max_tokens_is_refused_at_preparation_and_prefix_lengths_are_remembered(
        fast_path: Any) -> None:
    """On the fast route a row's length is its prefix's tokens plus its new tokens (the split is exact), the prefix's
    tokenised once per prefix text and remembered: the second request on the same screen tokenises only its new
    text."""
    from jeff.errors import InputTooLong

    class _Counting(_Tokenizer):
        def __init__(self) -> None:
            self.texts: list[str] = []

        def __call__(self, text: str, add_special_tokens: bool = False) -> dict[str, list[int]]:
            self.texts.append(text)
            return super().__call__(text, add_special_tokens)

    model = _Model()
    engine = fast_engine(fast_path, model, _AdapterSet(model, LAYOUTS), {"base": "live-last", "live": "live-last"})
    counting = _Counting()
    with engine._preparers.borrow() as processor:
        processor.tokenizer = counting  # this instance only
    first = engine.prepare("live", [wordy(10)])
    prefix_text = first.items[0].prefix_text
    assert counting.texts.count(prefix_text) == 1
    engine.prepare("live", [wordy(20), wordy(30)])
    assert counting.texts.count(prefix_text) == 1  # remembered
    prefix_tokens = len(prefix_text.split())
    engine.max_tokens = prefix_tokens + len(first.items[0].new_ids)
    engine.prepare("live", [wordy(10)])  # exactly at the limit
    with pytest.raises(InputTooLong, match="no input was truncated"):
        engine.prepare("live", [wordy(11)])


def test_preparers_lend_each_processor_to_one_thread_at_a_time() -> None:
    from jeff_runtime.local_engine import Preparers

    in_use: set[int] = set()
    clashes: list[int] = []
    lock = threading.Lock()
    preparers = Preparers([object(), object(), object()])

    def work() -> None:
        for _ in range(200):
            with preparers.borrow() as processor:
                with lock:
                    if id(processor) in in_use:
                        clashes.append(id(processor))
                    in_use.add(id(processor))
                with lock:
                    in_use.discard(id(processor))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(10)
    assert clashes == [] and preparers.count == 3


class _RecordingModel:
    """A DecisionModel stand-in for the standard path: records the layout each prepare call is given."""
    training = False
    temperature = 1.0
    prompt_layout = "live-last"  # stale: what the adapter used last selected
    codes = _Model.codes

    def __init__(self) -> None:
        self.layouts: list[str] = []

    def eval(self) -> None:
        pass

    def train(self, mode: bool) -> None:
        pass

    def prepare(self, rows: list[Any], max_length: int, *, layout: str) -> Any:
        import torch
        self.layouts.append(layout)
        return types.SimpleNamespace(counts=(2,) * len(rows),
                                     inputs={"attention_mask": torch.ones(len(rows), 4, dtype=torch.long)})

    def __call__(self, batch: Any) -> Any:
        import torch
        return torch.zeros(len(batch.counts), 2)


def test_standard_path_passes_the_adapters_own_layout() -> None:
    from jeff_runtime.local_engine import Preparers

    model = _RecordingModel()
    engine = object.__new__(LocalEngine)
    engine.backend, engine.fast, engine.model = "torch", None, model
    engine.base_format = {"format": "bf16"}
    engine.adapter_set = types.SimpleNamespace(use=lambda name: None)  # selects nothing: the layout is passed
    engine.max_tokens = 8192
    engine._adapters = {"first": Path("unused")}
    engine._max_options = {"base": 26, "first": 26}
    engine._layouts = {"base": "live-last", "first": "state-first"}
    engine._adapters_lock = threading.Lock()
    engine._generations = {"base": 1, "first": 2}
    engine._preparers = Preparers([_Processor()])
    engine.predict("first", [ROW])
    engine.predict("base", [ROW])
    assert model.layouts == ["state-first", "live-last"]
