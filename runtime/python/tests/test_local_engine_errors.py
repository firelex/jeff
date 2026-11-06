"""LocalEngine's typed errors, without loading weights: backend selection, predict's error paths, check()'s
validation, and add_adapter's rollback on a fast-path failure."""

import base64
import importlib.util
import io
import platform
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest
import torch

from jeff.errors import BackendUnavailable, EngineError, InputTooLong, InvalidRequest, NotInstalled, TooManyLabels
from jeff_runtime import LocalEngine, select_backend
from jeff_runtime.local_engine import (Preparers, check_max_adapters, check_prefix_pool, check_prefix_pool_mb,
                                       check_prepare_workers, read_max_options)


def machine(monkeypatch: pytest.MonkeyPatch, *, cuda: bool, apple: bool, mlx: bool) -> None:
    monkeypatch.setattr(torch.cuda, "is_available", lambda: cuda)
    monkeypatch.setattr(platform, "system", lambda: "Darwin" if apple else "Linux")
    monkeypatch.setattr(platform, "machine", lambda: "arm64" if apple else "x86_64")
    real = importlib.util.find_spec
    monkeypatch.setattr(importlib.util, "find_spec", lambda name, *a: (object() if mlx else None) if name == "mlx"
                        else real(name, *a))


def test_auto_on_apple_silicon_without_mlx_is_refused_with_the_install_hint(monkeypatch: pytest.MonkeyPatch) -> None:
    machine(monkeypatch, cuda=False, apple=True, mlx=False)
    with pytest.raises(BackendUnavailable, match=r"jeff-runtime\[mac\]"):
        select_backend("auto")


def test_auto_picks_by_machine(monkeypatch: pytest.MonkeyPatch) -> None:
    machine(monkeypatch, cuda=False, apple=True, mlx=True)
    assert select_backend("auto") == "mlx"
    machine(monkeypatch, cuda=True, apple=False, mlx=False)
    assert select_backend("auto") == "fast"
    machine(monkeypatch, cuda=False, apple=False, mlx=False)
    assert select_backend("auto") == "torch"


def test_requested_backends_that_cannot_run_are_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    machine(monkeypatch, cuda=False, apple=False, mlx=False)
    with pytest.raises(BackendUnavailable, match="NVIDIA"):
        select_backend("fast")
    with pytest.raises(BackendUnavailable, match="mlx"):
        select_backend("mlx")


NOUL_ROW = {"state": "s", "question": {"type": "noul"}}


def engine_raising(error: Exception) -> LocalEngine:
    engine = object.__new__(LocalEngine)
    engine.base_format = {"format": "bf16"}
    engine.backend, engine.fast, engine._adapters = "torch", None, {"jeff.support.triage": "unused"}
    engine._max_options = {"base": 26, "jeff.support.triage": 26}
    engine._layouts = {"base": "state-first", "jeff.support.triage": "state-first"}
    engine._adapters_lock = threading.Lock()
    engine._generations = {"base": 1, "jeff.support.triage": 2}
    engine.model, engine.max_tokens = _FakeModel(), 8192
    engine._preparers = Preparers([_FakeProcessor()])

    class AdapterSet:
        def use(self, adapter_id: str) -> None:
            pass

    def standard(rows: list[dict[str, Any]], layout: str) -> tuple[list[list[float]], list[int]]:
        raise error

    engine.adapter_set = AdapterSet()
    engine._standard = standard  # type: ignore[method-assign]
    return engine


def test_unknown_adapter_is_not_installed() -> None:
    with pytest.raises(NotInstalled, match="jeff.safety.spam.*not loaded"):
        engine_raising(ValueError("unused")).predict("jeff.safety.spam", [])
    with pytest.raises(NotInstalled, match="jeff.safety.spam.*not loaded"):
        engine_checking("torch", _FakeModel()).check("jeff.safety.spam", [{"state": {}, "question": {}}])


def test_installed_accepts_refresh_and_ignores_it() -> None:
    assert engine_raising(ValueError("unused")).installed(refresh=True) == {"base", "jeff.support.triage"}


def test_too_long_input_becomes_input_too_long_and_other_value_errors_propagate() -> None:
    too_long = ValueError("Question branch exceeds the 8192-token limit; no input was truncated.")
    with pytest.raises(InputTooLong, match="8192-token limit") as caught:
        engine_raising(too_long).predict("jeff.support.triage", [NOUL_ROW])
    assert caught.value.__cause__ is too_long
    with pytest.raises(ValueError, match="something else") as other:
        engine_raising(ValueError("something else")).predict("jeff.support.triage", [NOUL_ROW])
    assert not isinstance(other.value, InputTooLong)


def test_mlx_images_value_error_becomes_invalid_request() -> None:
    """The mlx backend's "images" ValueError (from MlxDecisionModel.prompt_ids, see mlx_backend.py) is a request
    mistake, not an engine failure, so predict() re-raises it as InvalidRequest, not a bare ValueError."""
    images_error = ValueError("The MLX backend reads text only; use the PyTorch backend for image decisions")
    with pytest.raises(InvalidRequest, match="PyTorch backend") as caught:
        engine_raising(images_error).predict("jeff.support.triage", [NOUL_ROW])
    assert caught.value.__cause__ is images_error


# ---------------------------------------------------------------- check()


def engine_checking(backend: str, model: Any = None, processor: Any = None) -> LocalEngine:
    engine = object.__new__(LocalEngine)
    engine.base_format = {"format": "bf16"}
    engine.backend = backend
    engine.max_tokens = 50
    engine._adapters = {"jeff.support.triage": "unused"}
    engine._layouts = {"base": "state-first", "jeff.support.triage": "state-first"}
    engine._max_options = {"base": 26, "jeff.support.triage": 3}
    engine._preparers = Preparers([processor if processor is not None else _FakeProcessor()])
    engine.fast = None
    engine._adapters_lock = threading.Lock()
    engine._generations = {"base": 1, "jeff.support.triage": 2}
    engine.model = model
    return engine


def test_check_rejects_images_on_mlx_without_loading_weights() -> None:
    engine = engine_checking("mlx")  # model is never touched: the images check happens first
    row = {"state": {"a": 1}, "question": {"type": "noul"}, "images": ["unused"]}
    with pytest.raises(InvalidRequest, match="PyTorch backend"):
        engine.check("jeff.support.triage", [row])


class _ShapeOnly:
    """Stands in for a torch.Tensor in a fake processor's output: check() must read only its shape, never move it
    to a device (no .to(), no .cpu())."""

    def __init__(self, length: int) -> None:
        self.shape = (1, length)

    def to(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("check() must not move tensors to a device")


class _FakeTokenizer:
    def __call__(self, text: str, add_special_tokens: bool = False) -> dict[str, list[int]]:
        return {"input_ids": [0] * 3}  # short: only the text-only branch would use this


class _FakeProcessor:
    tokenizer = _FakeTokenizer()

    def apply_chat_template(self, messages: list[dict[str, object]], *, tokenize: bool, add_generation_prompt: bool,
                            enable_thinking: bool) -> str:
        return "<prompt/>"

    def __call__(self, *, text: list[str], images: list[object], padding: bool, return_tensors: str) -> dict[str, Any]:
        assert return_tensors == "pt"
        # each image stands for a lot of placeholder tokens, far more than the bare text the old code counted
        return {"input_ids": _ShapeOnly(10 + 100 * len(images))}


class _FakeModel:
    codes = ["A", "B"]
    prompt_layout = "state-first"
    processor = _FakeProcessor()


class _Untouchable:
    def __getattr__(self, name: str) -> Any:
        raise AssertionError("check() must use its own processor, never the one predict() uses")


class _PredictsOnly:
    """A model whose processor belongs to predict() on the GPU thread (tokenizers are not safe to share between
    threads) and whose current prompt layout is whatever adapter predict() used last."""
    codes = ["A", "B"]
    prompt_layout = "live-last"
    processor = _Untouchable()


def test_check_uses_its_own_processor_and_the_adapters_own_prompt_layout(monkeypatch: pytest.MonkeyPatch) -> None:
    from jeff_runtime import local_engine as engine_module

    layouts: list[str] = []
    real = engine_module.decision_messages
    monkeypatch.setattr(engine_module, "decision_messages",
                        lambda row, codes, layout: layouts.append(layout) or real(row, codes, layout))
    engine = engine_checking("torch", _PredictsOnly())
    engine.check("jeff.support.triage", [{"state": {"a": 1}, "question": {"type": "noul", "instructions": "?"}}])
    assert layouts == ["state-first"]  # the adapter's layout, not the model's current "live-last"
    mlx = engine_checking("mlx", _PredictsOnly())
    mlx.check("jeff.support.triage", [{"state": {"a": 1}, "question": {"type": "noul", "instructions": "?"}}])


def test_check_counts_image_rows_through_the_full_processor_on_the_cpu(monkeypatch: pytest.MonkeyPatch) -> None:
    """check() must count a row with images as DecisionModel.prepare would (text expanded through the processor
    together with the opened images), not just the surrounding text — otherwise a request that is actually over
    max_tokens once its images are accounted for would be undercounted and wrongly accepted."""
    engine = engine_checking("torch", _FakeModel())
    row = {"state": {"a": 1}, "question": {"type": "noul"}, "images": [png(8, 8)]}
    with pytest.raises(InputTooLong, match="50-token limit"):
        engine.check("jeff.support.triage", [row])  # 10 + 100 = 110 tokens > max_tokens=50


def test_check_accepts_a_text_only_row_within_the_limit() -> None:
    engine = engine_checking("torch", _FakeModel())
    row = {"state": {"a": 1}, "question": {"type": "noul"}}
    engine.check("jeff.support.triage", [row])  # the fake tokenizer returns 3 ids, well under max_tokens=50


def test_check_never_sees_an_adapter_half_removed(monkeypatch: pytest.MonkeyPatch) -> None:
    """check()'s membership test (`_adapters`) and its layout lookup (`_layouts`) are two separate dict reads;
    remove_adapter()'s matching deletes are two separate dict writes. Before the fix, a check() landing between
    the two deletes would raise a bare KeyError instead of NotInstalled. Deterministic: remove_adapter is
    monkeypatched to hold `_adapters_lock` open (via two Events) exactly across its own two deletes, and a
    concurrent check() is proven to block on that same lock rather than slip in between them."""
    engine = engine_checking("torch", _FakeModel())
    engine.backend, engine.fast = "torch", None
    engine.adapter_set = type("AdapterSetStub", (), {"remove_adapter": lambda self, adapter_id: None})()
    row = {"state": {"a": 1}, "question": {"type": "noul"}}
    engine.check("jeff.support.triage", [row])  # warm up check()'s own lazy imports first, so their one-time
                                                 # cost on the real race below cannot be mistaken for the checker
                                                 # thread blocking on the lock

    holding_lock = threading.Event()
    release_lock = threading.Event()

    def paced_remove_adapter(self: LocalEngine, adapter_id: str) -> None:
        if adapter_id not in self._adapters:
            raise EngineError(f"{adapter_id} is not loaded in this engine")
        self.adapter_set.remove_adapter(adapter_id)
        with self._adapters_lock:
            holding_lock.set()
            assert release_lock.wait(5), "the checker thread never reached the lock"
            del self._adapters[adapter_id]
            del self._layouts[adapter_id]

    monkeypatch.setattr(LocalEngine, "remove_adapter", paced_remove_adapter)

    remover = threading.Thread(target=engine.remove_adapter, args=("jeff.support.triage",))
    remover.start()
    assert holding_lock.wait(5), "remove_adapter never reached its critical section"

    outcome: dict[str, Any] = {}

    def checking() -> None:
        try:
            engine.check("jeff.support.triage", [row])
            outcome["result"] = "ok"
        except NotInstalled:
            outcome["result"] = "not_installed"
        except KeyError as error:
            outcome["result"] = f"key_error: {error}"

    checker = threading.Thread(target=checking)
    checker.start()
    time.sleep(0.1)  # check() must be blocked on _adapters_lock, not racing the half-finished removal
    assert "result" not in outcome, outcome
    release_lock.set()
    remover.join(5)
    checker.join(5)
    assert outcome["result"] == "not_installed", outcome


def test_remove_adapter_really_takes_the_adapters_lock() -> None:
    """Not monkeypatched this time: the real remove_adapter() must itself acquire `_adapters_lock` for its final
    deletes. Proven by holding the lock open from this thread first, then calling the real remove_adapter on
    another thread and showing it blocks until the lock is released."""
    engine = engine_checking("torch", _FakeModel())
    engine.backend, engine.fast = "torch", None
    removed: list[str] = []
    engine.adapter_set = type("AdapterSetStub", (), {
        "remove_adapter": lambda self, adapter_id: removed.append(adapter_id)})()

    engine._adapters_lock.acquire()
    done = threading.Event()

    def removing() -> None:
        engine.remove_adapter("jeff.support.triage")
        done.set()

    remover = threading.Thread(target=removing)
    remover.start()
    assert not done.wait(0.2), "remove_adapter finished without ever blocking on _adapters_lock"
    # the backend-specific removal runs before the lock is taken, so it has already happened by now
    assert removed == ["jeff.support.triage"]
    assert "jeff.support.triage" in engine._adapters and "jeff.support.triage" in engine._layouts

    engine._adapters_lock.release()
    assert done.wait(5)
    remover.join(5)
    assert "jeff.support.triage" not in engine._adapters
    assert "jeff.support.triage" not in engine._layouts


# ---------------------------------------------------------------- add_adapter rollback


class _AdapterSetStub:
    def __init__(self) -> None:
        self.names: set[str] = set()

    def add_adapter(self, name: str, path: Path) -> None:
        self.names.add(name)

    def remove_adapter(self, name: str) -> None:
        self.names.discard(name)


class _FailingFast:
    def add_adapter(self, name: str) -> None:
        raise ValueError(f"fast path rejects {name}")


def test_add_adapter_rolls_back_the_adapter_set_when_the_fast_path_rejects_it(tmp_path: Path) -> None:
    (tmp_path / "decision_config.json").write_text('{"max_options": 26}')
    engine = object.__new__(LocalEngine)
    engine.base_format = {"format": "bf16"}
    engine.backend, engine._adapters, engine.max_adapters = "fast", {}, 4
    engine._max_options, engine._layouts = {"base": 26}, {"base": "live-last"}
    engine._adapters_lock = threading.Lock()
    engine.adapter_set = _AdapterSetStub()
    engine.fast = _FailingFast()

    with pytest.raises(EngineError, match="jeff.support.triage") as first:
        engine.add_adapter("jeff.support.triage", tmp_path)
    assert isinstance(first.value.__cause__, ValueError)
    assert engine.installed() == {"base"}
    assert engine.adapter_set.names == set()  # rolled back, not left half-added

    # a retry must see exactly the same clean state, not a different error from a half-added adapter
    with pytest.raises(EngineError, match="jeff.support.triage") as second:
        engine.add_adapter("jeff.support.triage", tmp_path)
    assert isinstance(second.value.__cause__, ValueError)
    assert engine.installed() == {"base"}
    assert engine.adapter_set.names == set()


def test_add_adapter_reads_the_adapters_option_limit_before_loading_anything(tmp_path: Path) -> None:
    engine = object.__new__(LocalEngine)
    engine.base_format = {"format": "bf16"}
    engine.backend, engine._adapters, engine.fast, engine.max_adapters = "torch", {}, None, None
    engine._max_options, engine._layouts = {"base": 26}, {"base": "live-last"}
    engine._adapters_lock = threading.Lock()
    engine.adapter_set = _AdapterSetStub()
    (tmp_path / "decision_config.json").write_text('{"temperature": 1.0}')
    with pytest.raises(EngineError, match="max_options"):
        engine.add_adapter("jeff.support.triage", tmp_path)
    assert engine.adapter_set.names == set() and engine.installed() == {"base"}


# ---------------------------------------------------------------- the base and option limits


def test_the_option_limit_is_required_in_the_checkpoint_config(tmp_path: Path) -> None:
    """As jeff-server's max_options(): the most options the checkpoint was trained on; a checkpoint without it
    would silently never pick the options past it, so it is refused at load."""
    config = tmp_path / "decision_config.json"
    config.write_text('{"max_options": 255}')
    assert read_max_options(tmp_path) == 255
    for bad in ("{}", '{"max_options": null}', '{"max_options": 1}', '{"max_options": true}',
                '{"max_options": "26"}', "not json"):
        config.write_text(bad)
        with pytest.raises(EngineError, match="max_options"):
            read_max_options(tmp_path)
    config.unlink()
    with pytest.raises(EngineError, match="decision_config.json"):
        read_max_options(tmp_path)


def choice(count: int) -> dict[str, Any]:
    return {"state": {"a": 1}, "question": {"type": "choice", "criteria": {f"o{i}": None for i in range(count)}}}


class _ManyCodes(_FakeModel):
    codes = [f"C{i}" for i in range(30)]


def test_check_and_predict_refuse_a_choice_over_the_adapters_option_limit() -> None:
    engine = engine_checking("torch", _ManyCodes())
    engine.check("jeff.support.triage", [choice(3)])
    with pytest.raises(TooManyLabels, match="4 options, but this model handles at most 3 .*Shortlist the options"):
        engine.check("jeff.support.triage", [choice(3), choice(4)])
    engine.check("base", [choice(26)])  # the base has its own limit
    with pytest.raises(TooManyLabels, match="27 options, but this model handles at most 26"):
        engine.check("base", [choice(27)])
    with pytest.raises(TooManyLabels, match="at most 26"):
        engine_raising(ValueError("never reached")).predict("base", [choice(27)])


def test_the_base_is_always_installed_and_served_with_no_adapter() -> None:
    engine = engine_raising(ValueError("unused"))
    used: list[str | None] = []
    engine.adapter_set = type("Recording", (), {"use": lambda self, name: used.append(name)})()
    engine._standard = lambda rows, layout: ([[0.5, 0.5] for _ in rows], [7 for _ in rows])  # type: ignore[method-assign]
    result = engine.predict("base", [{"state": "s", "question": {"type": "noul"}}])
    assert used == [None]  # the plain base, no adapter
    assert result.probabilities == [[0.5, 0.5]] and result.input_tokens == [7]
    engine.predict("jeff.support.triage", [{"state": "s", "question": {"type": "noul"}}])
    assert used == [None, "jeff.support.triage"]
    engine_checking("torch", _FakeModel()).check("base", [{"state": "s", "question": {"type": "noul"}}])


def test_the_base_can_be_neither_added_nor_removed() -> None:
    engine = engine_raising(ValueError("unused"))
    with pytest.raises(EngineError, match="base"):
        engine.add_adapter("base", Path("/unused"))
    with pytest.raises(EngineError, match="always served"):
        engine.remove_adapter("base")
    assert engine.installed() == {"base", "jeff.support.triage"}


def png(width: int, height: int, mode: str = "1") -> str:
    """A real PNG of that size as a data URL (1-bit by default, so even a large one stays small)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new(mode, (width, height)).save(buffer, format="PNG")
    return "data:image/png;base64," + base64.b64encode(buffer.getvalue()).decode()


def test_an_image_that_cannot_be_read_is_an_invalid_request() -> None:
    row = {"state": {"a": 1}, "question": {"type": "noul"}, "images": ["data:image/png;base64,AAAA"]}
    with pytest.raises(InvalidRequest, match="image 1 could not be read"):
        engine_checking("torch", _FakeModel()).check("jeff.support.triage", [row])


def test_an_image_over_16_million_pixels_is_refused_from_its_header_without_decoding_it(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import ImageFile

    big = png(5000, 4000)  # 20 million pixels
    decoded: list[object] = []
    real_load = ImageFile.ImageFile.load
    monkeypatch.setattr(ImageFile.ImageFile, "load", lambda self: decoded.append(self) or real_load(self))
    row = {"state": {"a": 1}, "question": {"type": "noul"}, "images": [big]}
    with pytest.raises(InvalidRequest, match="image 1 has 20000000 pixels; .*16 million"):
        engine_checking("torch", _FakeModel()).check("jeff.support.triage", [row])
    assert decoded == []  # refused from the header alone
    with pytest.raises(InvalidRequest, match="16 million"):
        engine_raising(ValueError("never reached")).predict("jeff.support.triage", [row])
    assert decoded == []


def test_an_image_whose_format_is_not_png_jpeg_or_webp_is_refused(tmp_path: Path) -> None:
    from PIL import Image

    gif = tmp_path / "x.gif"
    Image.new("L", (4, 4)).save(gif, format="GIF")
    row = {"state": {"a": 1}, "question": {"type": "noul"}, "images": [str(gif)]}
    with pytest.raises(InvalidRequest, match="image 1 is a GIF image"):
        engine_checking("torch", _FakeModel()).check("jeff.support.triage", [row])


def test_predict_runs_the_same_checks_as_check_so_in_process_callers_get_typed_errors() -> None:
    engine = engine_raising(ValueError("never reached"))
    for row, error, named in (
            ({"state": "s"}, InvalidRequest, "question"),
            ({"state": "s", "question": {"type": "noul"}, "images": ["data:image/png;base64,AAAA"]}, InvalidRequest,
             "image 1 could not be read"),
            ({"state": "s", "question": {"type": "choice", "criteria": {f"o{i}": None for i in range(27)}}},
             TooManyLabels, "at most 26")):
        with pytest.raises(error, match=named):
            engine.predict("jeff.support.triage", [row])


def test_predict_hands_the_model_the_decoded_images() -> None:
    from PIL import Image

    engine = engine_raising(ValueError("unused"))
    seen: list[Any] = []
    engine._standard = lambda rows, layout: (seen.extend(rows) or [[0.5, 0.5] for _ in rows],  # type: ignore[method-assign]
                                             [1 for _ in rows])
    engine.predict("jeff.support.triage", [{"state": "s", "question": {"type": "noul"}, "images": [png(3, 2, "RGB")]}])
    [image] = seen[0]["images"]
    assert isinstance(image, Image.Image) and image.size == (3, 2) and image.mode == "RGB"


def test_the_constructor_refuses_an_adapter_named_base(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="'base' is the base model's id"):
        LocalEngine(tmp_path, {"base": tmp_path}, backend="torch", prepare_workers=1)


def test_too_many_labels_carries_the_limit_for_the_server_to_name_the_question() -> None:
    with pytest.raises(TooManyLabels) as refused:
        engine_checking("torch", _ManyCodes()).check("base", [choice(27)])
    assert refused.value.limit == 26  # type: ignore[attr-defined]  # travels in the wire error's details


def test_the_bases_option_limit_is_reported() -> None:
    assert engine_raising(ValueError("unused")).base_max_options == 26


# ---------------------------------------------------------------- max_adapters (the fast backend's stacked adapters)


def test_the_fast_backend_needs_max_adapters_and_the_others_refuse_it() -> None:
    with pytest.raises(EngineError, match="fast backend needs --max-adapters"):
        check_max_adapters("fast", None)
    for bad in (0, -1, True, 2.5):
        with pytest.raises(EngineError, match="whole number of at least 1"):
            check_max_adapters("fast", bad)  # type: ignore[arg-type]
    check_max_adapters("fast", 1)
    for backend in ("mlx", "torch"):
        with pytest.raises(EngineError, match=f"only applies to the fast backend; this engine runs on {backend}"):
            check_max_adapters(backend, 4)
        check_max_adapters(backend, None)


def test_local_engine_checks_max_adapters_before_loading_anything(monkeypatch: pytest.MonkeyPatch,
                                                                   tmp_path: Path) -> None:
    """Refused before any checkpoint is read: tmp_path holds no decision_config.json, which would fail otherwise."""
    machine(monkeypatch, cuda=True, apple=False, mlx=False)
    with pytest.raises(EngineError, match="needs --max-adapters"):
        LocalEngine(tmp_path, {}, backend="fast", prepare_workers=1)
    with pytest.raises(EngineError, match=r"2 adapters were asked for, but this engine holds at most 1 \(--max-adapters 1\)"):
        LocalEngine(tmp_path, {"a": tmp_path, "b": tmp_path}, backend="fast", prepare_workers=1, max_adapters=1, prefix_pool_mb=2048)
    with pytest.raises(EngineError, match="runs on torch"):
        LocalEngine(tmp_path, {}, backend="torch", prepare_workers=1, max_adapters=2)


# ---------------------------------------------------------------- prefix_pool_mb (the fast backend's prefix pool)


def test_the_fast_backend_needs_prefix_pool_mb_and_the_others_refuse_it() -> None:
    with pytest.raises(EngineError, match=r"needs --prefix-pool-mb \(prefix_pool_mb\).*2048 holds 66 prefixes"):
        check_prefix_pool_mb("fast", None)
    for bad in (115, 0, -1, True, 2048.0):
        with pytest.raises(EngineError, match="at least 116"):
            check_prefix_pool_mb("fast", bad)  # type: ignore[arg-type]
    check_prefix_pool_mb("fast", 116)
    for backend in ("mlx", "torch"):
        with pytest.raises(EngineError, match=f"only applies to the fast backend; this engine runs on {backend}"):
            check_prefix_pool_mb(backend, 2048)
        check_prefix_pool_mb(backend, None)
        check_prefix_pool(backend, None, 64)  # no pool, nothing to hold


def test_the_prefix_pool_must_hold_max_batch_rows_of_the_longest_prefix() -> None:
    check_prefix_pool("fast", 7345, 64)
    with pytest.raises(EngineError, match=r"--prefix-pool-mb 7344 cannot hold --max-batch-rows 64"):
        check_prefix_pool("fast", 7344, 64)


def test_local_engine_checks_prefix_pool_mb_before_loading_anything(monkeypatch: pytest.MonkeyPatch,
                                                                     tmp_path: Path) -> None:
    """Refused before any checkpoint is read: tmp_path holds no decision_config.json, which would fail otherwise."""
    machine(monkeypatch, cuda=True, apple=False, mlx=False)
    with pytest.raises(EngineError, match="needs --prefix-pool-mb"):
        LocalEngine(tmp_path, {}, backend="fast", prepare_workers=1, max_adapters=1)
    with pytest.raises(EngineError, match="at least 116"):
        LocalEngine(tmp_path, {}, backend="fast", prepare_workers=1, max_adapters=1, prefix_pool_mb=100)
    with pytest.raises(EngineError, match="runs on torch"):
        LocalEngine(tmp_path, {}, backend="torch", prepare_workers=1, prefix_pool_mb=2048)


def test_add_adapter_beyond_max_adapters_is_refused_before_loading(tmp_path: Path) -> None:
    (tmp_path / "decision_config.json").write_text('{"max_options": 26}')
    engine = object.__new__(LocalEngine)
    engine.base_format = {"format": "bf16"}
    engine.backend, engine.max_adapters = "fast", 1
    engine._adapters = {"jeff.support.triage": tmp_path}
    engine._max_options = {"base": 26, "jeff.support.triage": 26}
    engine._layouts = {"base": "live-last", "jeff.support.triage": "live-last"}
    engine._adapters_lock = threading.Lock()
    engine.adapter_set = _AdapterSetStub()
    engine.fast = _FailingFast()  # never reached

    with pytest.raises(EngineError) as refused:
        engine.add_adapter("jeff.safety.spam", tmp_path)
    assert "at most 1 adapters (--max-adapters 1) and 1 are loaded" in str(refused.value)
    assert engine.adapter_set.names == set()
    assert engine.installed() == {"base", "jeff.support.triage"}


@pytest.mark.parametrize("value", [0, -1, True, 2.0, "4", None])
def test_prepare_workers_must_be_a_whole_number_of_at_least_one(value: object) -> None:
    with pytest.raises(EngineError, match="--prepare-workers"):
        check_prepare_workers(value)


def test_local_engine_refuses_bad_prepare_workers_before_loading_anything(tmp_path: Path) -> None:
    with pytest.raises(EngineError, match="--prepare-workers 0"):
        LocalEngine(tmp_path, {}, backend="torch", prepare_workers=0)  # tmp_path holds no model: nothing loaded


def test_predict_in_process_does_not_render_or_tokenise_before_the_models_own_pass() -> None:
    """On mlx and torch the model's own pass renders, tokenises (and opens images) and refuses an over-long row; an
    in-process predict() does not add a second such pass beforehand (check(), for the engine server, still does)."""

    class Untouchable:
        def __getattr__(self, name: str) -> Any:
            raise AssertionError(f"predict() used the preparation processor ({name})")

    engine = engine_raising(ValueError("unused"))
    engine._standard = lambda rows, layout: ([[0.5, 0.5] for _ in rows], [7 for _ in rows])  # type: ignore[method-assign]
    engine._preparers = Preparers([Untouchable()])
    assert engine.predict("jeff.support.triage", [NOUL_ROW]).probabilities == [[0.5, 0.5]]
    with pytest.raises(AssertionError, match="preparation processor"):
        engine.check("jeff.support.triage", [NOUL_ROW])


@pytest.mark.parametrize("backend", ["pytorch", "mlx"])
def test_an_unknown_adapter_names_the_loaded_ones_in_the_shared_wording(backend: str) -> None:
    """Both backends name the loaded adapters with jeff_runtime.wording.loaded, the wording the server cuts for a
    caller who may not see some of them; built without weights, as use() refuses before it reads any."""
    from jeff_runtime.engine.lora import AdapterSet
    from jeff_runtime.engine.mlx_backend import MlxDecisionModel
    from jeff_runtime.wording import loaded

    holder: Any = object.__new__(AdapterSet if backend == "pytorch" else MlxDecisionModel)
    holder.heads = {None: object(), "jeff.b.two": object(), "jeff.a.one": object()}
    refusals = [lambda: holder.use("jeff.c.missing")]
    if backend == "pytorch":  # the MLX backend's remove_adapter names no list
        refusals.append(lambda: holder.remove_adapter("jeff.c.missing"))
    for refused in refusals:
        with pytest.raises(ValueError) as raised:
            refused()
        assert str(raised.value) == f"Unknown adapter 'jeff.c.missing' {loaded(['jeff.a.one', 'jeff.b.two'])}"


def _apng_with_background_disposal(width: int, height: int) -> str:
    """A tiny animated PNG (under 200 bytes) whose canvas is `width` x `height` but whose two frames are one pixel
    each, the first disposed to the background: GHSA-5xm2-6676-qcp9, where Pillow before 13.0.0 allocated the whole
    canvas for that disposal before its decompression-bomb check."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    def frame_control(sequence: int) -> bytes:  # a 1x1 frame at 0,0, disposed to the background
        return chunk(b"fcTL", struct.pack(">IIIIIHHBB", sequence, 1, 1, 0, 0, 1, 1, 1, 0))

    pixel = zlib.compress(b"\x00\x00\x00\x00")  # one RGB row: the filter byte, then the pixel
    data = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"acTL", struct.pack(">II", 2, 0)) + frame_control(0) + chunk(b"IDAT", pixel)
            + frame_control(1) + chunk(b"fdAT", struct.pack(">I", 2) + pixel) + chunk(b"IEND", b""))
    return "data:image/png;base64," + base64.b64encode(data).decode()


@pytest.mark.parametrize(("width", "height"), [(22000, 22000), (12000, 12000)])
def test_an_apng_declaring_a_huge_canvas_is_refused_without_allocating_it(width: int, height: int) -> None:
    """GHSA-5xm2-6676-qcp9. 22000 x 22000 is past Pillow's own bomb limit; 12000 x 12000 (144 million pixels, about
    430 MB as RGB) is under it, so only the engine's 16-million-pixel check stands between it and a full-canvas
    allocation. Measured in a fresh process: the peak resident memory may grow by at most 100 MB across the check."""
    script = f"""
import resource, sys
from jeff.errors import InvalidRequest
from jeff_runtime.local_engine import open_checked_image
image = sys.stdin.read()
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
try:
    open_checked_image(image, 1)
    print("accepted")
except InvalidRequest as error:
    print("refused:", error)
after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print((after - before) / (1 if sys.platform == "darwin" else 1 / 1024) / 2**20)
"""
    result = subprocess.run([sys.executable, "-c", script], input=_apng_with_background_disposal(width, height),
                            capture_output=True, text=True, check=True)
    verdict, growth_mb = result.stdout.strip().split("\n")
    assert verdict.startswith("refused: image 1 ")
    assert float(growth_mb) < 100, f"the check grew the process by {growth_mb} MB"
