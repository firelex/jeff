"""The real engine behind jeff.Jeff.local(): the base model loaded once, adapters beside it, one backend chosen at
start (fast path on NVIDIA, MLX on Apple silicon, PyTorch otherwise). Nothing switches backend after a failure.

The base itself is served too, with no adapter, under the adapter id "base" (jeff.engine.BASE_ADAPTER): always
installed, never added or removed. Each of the base and the adapters answers choice questions of at most its
decision_config.json's max_options options (TooManyLabels beyond), and every predict reports each row's prompt
length in tokens (EngineResult.input_tokens)."""

import base64
import binascii
import copy
import importlib.util
import io
import itertools
import json
import platform
import queue
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal, cast

import torch
from PIL import Image

from jeff.engine import BASE_ADAPTER, EngineResult, Prepared, capacity_error, check_capacity, check_option_limit
from jeff.errors import BackendUnavailable, EngineError, InputTooLong, InvalidRequest, JeffError, NotInstalled
# Every backend loads model.py (the MLX one through mlx_backend), so these cost nothing extra; the modules that need
# Triton (fast_path, batched_path) or MLX stay imported only where their backend is built.
from jeff_runtime.engine.int4 import BF16, base_format
from jeff_runtime.engine.model import decision_messages
from jeff_runtime.engine.prefix_pool import LONGEST_PREFIX_TOKENS, check_pool_holds, check_pool_size, prefixes_held
from jeff_runtime.engine.prepare import prepare_rows
from jeff_runtime.engine.types import DecisionInput
from jeff_runtime.wording import loaded

type Backend = Literal["auto", "mlx", "torch", "fast"]


# The ported engine (model.py, mlx_backend.py, fast_path.py) refuses an over-long request with a ValueError whose
# message ends with this; LocalEngine re-raises exactly that one as InputTooLong.
TOO_LONG_MESSAGE = "no input was truncated"

# mlx_backend.MlxDecisionModel.prompt_ids refuses a row with images with a ValueError with exactly this message
# (the MLX backend is text only); LocalEngine re-raises exactly that one as InvalidRequest.
IMAGES_NOT_SUPPORTED_MESSAGE = "The MLX backend reads text only; use the PyTorch backend for image decisions"

# As jeff-server: an image may have at most this many pixels.
MAX_IMAGE_PIXELS = 16_000_000


def read_max_options(folder: Path) -> int:
    """The largest number of options a checkpoint (the base or an adapter) can answer: the most its training
    questions had, from its decision_config.json's "max_options" (as jeff-server's max_options()). Answer codes past
    that were never trained, so the model would silently never pick those options; a checkpoint without the value is
    refused (EngineError), never given a default."""
    path = Path(folder) / "decision_config.json"
    try:
        config = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise EngineError(f"Cannot read {path} (needed for its \"max_options\"): {error}") from error
    value = config["max_options"] if isinstance(config, dict) and "max_options" in config else None
    if not isinstance(value, int) or isinstance(value, bool) or value < 2:
        raise EngineError(f"{path} needs \"max_options\": the largest number of options the model was trained on "
                          "(the trainer writes it; the Jeff models released on 2026-09-28 handle 26).")
    return value


IMAGE_FORMATS = ("PNG", "JPEG", "WEBP")


def open_checked_image(value: Any, number: int) -> Any:
    """Image `number` of a row (a data URL, a path or a PIL image), decoded to RGB as the model reads it. Its format
    and size are read from the header first (PIL opens lazily), so an image of more than MAX_IMAGE_PIXELS pixels
    (jeff-server's limit) or not PNG, JPEG or WebP is refused before anything is decoded. Every way it can fail is
    the caller's mistake: InvalidRequest naming the image."""
    if isinstance(value, Image.Image):
        size = value.width * value.height
        if size > MAX_IMAGE_PIXELS:
            raise InvalidRequest(f"image {number} has {size} pixels; each image must have at most 16 million pixels")
        return value.convert("RGB")
    try:
        if isinstance(value, str) and value.startswith("data:image/"):
            source: Any = io.BytesIO(base64.b64decode(value.split(",", 1)[1], validate=True))
        else:
            source = value
        with Image.open(source) as image:  # reads the header only
            kind, size = image.format, image.width * image.height
            if kind not in IMAGE_FORMATS:
                raise InvalidRequest(f"image {number} is a {kind} image; use PNG, JPEG or WebP")
            if size > MAX_IMAGE_PIXELS:
                raise InvalidRequest(f"image {number} has {size} pixels; each image must have at most 16 million "
                                     "pixels")
            return image.convert("RGB")  # the decode, only now
    except InvalidRequest:
        raise
    except (OSError, ValueError, IndexError, binascii.Error, Image.DecompressionBombError, SyntaxError) as error:
        raise InvalidRequest(f"image {number} could not be read as a PNG, JPEG or WebP image: "
                             f"{type(error).__name__}: {error}") from error


def select_backend(requested: Backend) -> Backend:
    has_mlx = importlib.util.find_spec("mlx") is not None
    if requested != "auto":
        if requested == "fast" and not torch.cuda.is_available():
            raise BackendUnavailable("backend='fast' needs an NVIDIA GPU (torch.cuda.is_available() is False)")
        if requested == "mlx" and not has_mlx:
            raise BackendUnavailable("backend='mlx' needs mlx-lm: pip install 'jeff-runtime[mac]'")
        return requested
    if torch.cuda.is_available():
        return "fast"
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        if not has_mlx:
            raise BackendUnavailable("This is Apple silicon, where Jeff runs on MLX, but mlx is not installed: "
                                     "pip install 'jeff-runtime[mac]' (or pass backend='torch' to use PyTorch)")
        return "mlx"
    return "torch"


def check_max_adapters(backend: str, max_adapters: int | None) -> None:
    """The fast backend stacks every adapter on the GPU in room allocated at start, so it needs max_adapters (jeff
    runtime serve --max-adapters, JEFF_MAX_ADAPTERS in deploy/): a whole number of at least 1, with no default. The
    other backends have no such limit, so there it is refused rather than silently ignored. EngineError otherwise."""
    if backend == "fast":
        if max_adapters is None:
            raise EngineError("The fast backend needs --max-adapters (max_adapters): the most adapters this engine "
                              "will hold at once, beside the base. Room for that many is allocated on the GPU at "
                              "start (about 34 MB each), and loading more is refused.")
        if isinstance(max_adapters, bool) or not isinstance(max_adapters, int) or max_adapters < 1:
            raise EngineError(f"--max-adapters {max_adapters!r}: give a whole number of at least 1")
    elif max_adapters is not None:
        raise EngineError(f"--max-adapters (max_adapters) only applies to the fast backend; this engine runs on "
                          f"{backend}, which has no such limit, so leave it out")


def check_prefix_pool_mb(backend: str, prefix_pool_mb: int | None) -> None:
    """The fast backend keeps cached prefixes in a pool of GPU memory allocated at start, so it needs prefix_pool_mb
    (jeff runtime serve --prefix-pool-mb, JEFF_PREFIX_POOL_MB in deploy/, Jeff.local(prefix_pool_mb=...)): a whole
    number of MB, at least the smallest pool (prefix_pool.check_pool_size), with no default. The other backends have
    no such pool, so there it is refused rather than silently ignored. EngineError otherwise."""
    if backend == "fast":
        if prefix_pool_mb is None:
            raise EngineError("The fast backend needs --prefix-pool-mb (prefix_pool_mb): the GPU memory, in MB, that "
                              "holds cached prompt prefixes, allocated at start. For example 2048 holds "
                              f"{prefixes_held(2048, 1000)} prefixes of 1,000 tokens.")
        check_pool_size(prefix_pool_mb)
    elif prefix_pool_mb is not None:
        raise EngineError(f"--prefix-pool-mb (prefix_pool_mb) only applies to the fast backend; this engine runs on "
                          f"{backend}, which has no prefix pool, so leave it out")


def check_prefix_pool(backend: str, prefix_pool_mb: int | None, max_batch_rows: int) -> None:
    """check_prefix_pool_mb, and on the fast backend, that the pool holds max_batch_rows prefixes of the longest
    prefix at once (jeff runtime serve --max-batch-rows; design 2b-1 Risk 5)."""
    check_prefix_pool_mb(backend, prefix_pool_mb)
    if backend == "fast":
        check_pool_holds(cast(int, prefix_pool_mb), max_batch_rows)


def check_base_format(backend: str, folder: Path, weights: Mapping[str, str]) -> None:
    """An INT4 base (engine.int4) runs on the torch and fast backends; MLX loads bfloat16 bases only. EngineError for
    an INT4 base on MLX, before any weights load. `weights`: the base's format (base_format)."""
    if weights["format"] == BF16:
        return
    if backend == "mlx":
        raise EngineError(f"{folder} is an INT4 base ({weights['format_id']}); the MLX backend serves bfloat16 bases "
                          "only (use backend='torch' or 'fast')")


def check_prepare_workers(prepare_workers: object) -> int:
    """--prepare-workers (prepare_workers): how many threads prepare requests on arrival at once, each with its own
    processor (chat template and tokenizer); a whole number of at least 1, with no default. EngineError otherwise."""
    if isinstance(prepare_workers, bool) or not isinstance(prepare_workers, int) or prepare_workers < 1:
        raise EngineError(f"--prepare-workers {prepare_workers!r}: give a whole number of at least 1 (how many "
                          "requests are prepared at once on arrival, each by a thread with its own tokenizer)")
    return prepare_workers


PREFIX_LENGTHS_KEPT = 1024  # prefix texts whose token count each preparation processor remembers


class Preparers:
    """The processors (chat template and tokenizer) the preparation threads use, one per thread at a time: a
    tokenizer is not safe to use from two threads at once. borrow() waits for a free one. Each processor also
    remembers the token counts of the last PREFIX_LENGTHS_KEPT prefix texts it tokenised (prefix_length), read and
    written only by the thread that has borrowed it."""

    def __init__(self, processors: list[Any]) -> None:
        self._free: queue.SimpleQueue[Any] = queue.SimpleQueue()
        for processor in processors:
            self._free.put(processor)
        self.count = len(processors)
        self._prefix_lengths: dict[int, OrderedDict[str, int]] = {id(processor): OrderedDict()
                                                                   for processor in processors}

    @contextmanager
    def borrow(self) -> Iterator[Any]:
        processor = self._free.get()
        try:
            yield processor
        finally:
            self._free.put(processor)

    def prefix_length(self, processor: Any, text: str) -> int:
        """The number of tokens of a prefix text, tokenised by `processor` (which the caller has borrowed) the first
        time and remembered: rows of one screen share their prefix, and it is the long part of the prompt."""
        lengths = self._prefix_lengths[id(processor)]
        if text in lengths:
            lengths.move_to_end(text)
            return lengths[text]
        count = len(processor.tokenizer(text, add_special_tokens=False)["input_ids"])
        lengths[text] = count
        if len(lengths) > PREFIX_LENGTHS_KEPT:
            lengths.popitem(last=False)
        return count


class LocalEngine:
    def __init__(self, base: Path, adapters: Mapping[str, Path], *, backend: Backend = "auto",
                 prepare_workers: int, max_tokens: int = 8192, max_adapters: int | None = None,
                 prefix_pool_mb: int | None = None) -> None:
        """prepare_workers: how many requests may be prepared at once (jeff runtime serve --prepare-workers, the size
        of the engine server's preparation pool; 1 for an engine called from one thread), each with its own copy of
        the processor; required, with no default. max_adapters and prefix_pool_mb: required on the fast backend,
        refused on the others (check_max_adapters, check_prefix_pool_mb)."""
        self.backend: str = select_backend(backend)
        self.prepare_workers = check_prepare_workers(prepare_workers)
        check_max_adapters(self.backend, max_adapters)
        check_prefix_pool_mb(self.backend, prefix_pool_mb)
        check_capacity(max_adapters, len(adapters))
        self.max_adapters = max_adapters
        self.max_tokens = max_tokens
        self.base_checkpoint = str(base)
        self._adapters = dict(adapters)
        if BASE_ADAPTER in self._adapters:
            raise EngineError(f"{BASE_ADAPTER!r} is the base model's id; an adapter cannot have it")
        # Read before any weights load, so a checkpoint without max_options fails at once.
        self._max_options = {BASE_ADAPTER: read_max_options(base),
                             **{adapter_id: read_max_options(path) for adapter_id, path in self._adapters.items()}}
        # The base's weight format, in every predict timing and in health.base; an INT4 base's records are checked
        # here, before any weights load.
        self.base_format: dict[str, str] = base_format(Path(base))
        check_base_format(self.backend, Path(base), self.base_format)
        self.fast: Any = None
        self.batched: Any = None
        if self.backend == "mlx":
            from jeff_runtime.engine.mlx_backend import MlxDecisionModel
            self.model: Any = MlxDecisionModel(base, lora_precision="model")
            for adapter_id, path in self._adapters.items():
                self.model.add_adapter(adapter_id, Path(path))
            self.adapter_set: Any = self.model
        else:
            from jeff_runtime.engine.lora import AdapterSet
            from jeff_runtime.engine.model import DecisionModel
            self.model = DecisionModel(checkpoint=base)
            self.adapter_set = AdapterSet(self.model, base, precision="model")
            for adapter_id, path in self._adapters.items():
                self.adapter_set.add_adapter(adapter_id, Path(path))
            if self.backend == "fast":
                from jeff_runtime.engine.fast_path import FastPath
                from jeff_runtime.engine.batched_path import BatchedPath
                # packs every loaded adapter itself
                self.fast = FastPath(self.model, self.adapter_set, cast(int, prefix_pool_mb), cast(int, max_adapters))
                self.batched = BatchedPath(self.fast)  # records the batched graphs
                # distinct prefixes one run of the batched path may pin at once (_fast_many)
                self._prefixes_at_once = prefixes_held(cast(int, prefix_pool_mb), LONGEST_PREFIX_TOKENS)
        # prepare() and check() run on other threads than predict_many() (the engine server's preparation threads,
        # several at once), so each preparation thread borrows a processor of its own, never the one the model
        # uses: loaded once here from the same base folder and copied, one per worker. Both prepare() and the
        # standard path read each adapter's prompt layout from this table rather than the model's current one,
        # which adapter_set.use() switches to whichever adapter ran last.
        from transformers import AutoProcessor

        from jeff_runtime.engine.model import configure_processor
        processor: Any = AutoProcessor.from_pretrained(str(base))
        if self.backend != "mlx":
            configure_processor(processor)  # the image settings predict's processor uses
        self._preparers = Preparers([processor, *(copy.deepcopy(processor) for _ in range(prepare_workers - 1))])
        self._layouts = {adapter_id: self._prompt_layout(adapter_id)
                         for adapter_id in [BASE_ADAPTER, *self._adapters]}
        # Guards `_adapters`, `_layouts` and `_max_options` together: check()'s membership test and its layout and
        # limit lookups are separate dict reads, and add_adapter/remove_adapter's matching writes are separate dict
        # writes; held across all of each, so a concurrent check() for an adapter being removed (or added) never
        # catches it half-done (a KeyError on `_layouts` instead of the NotInstalled that an atomic view would give).
        # Also guards `_generations`: each loaded adapter's (and the base's) load number, new on every add_adapter, so
        # predict_many can refuse rows prepared for an earlier load of the adapter (split by its old layout, checked
        # against its old option limit).
        self._adapters_lock = threading.Lock()
        self._loads = itertools.count(1)
        self._generations = {adapter_id: next(self._loads) for adapter_id in [BASE_ADAPTER, *self._adapters]}

    def installed(self, refresh: bool = False) -> set[str]:
        return set(self._adapters) | {BASE_ADAPTER}

    @property
    def base_max_options(self) -> int:
        return self._max_options[BASE_ADAPTER]

    def _prompt_layout(self, adapter_id: str) -> str:
        """The prompt layout predict() switches to for this adapter (set when the adapter is loaded)."""
        head_name = None if adapter_id == BASE_ADAPTER else adapter_id
        if self.backend == "mlx":
            return str(self.model.heads[head_name][2])
        head = self.adapter_set.heads[head_name]
        layout = head.prompt_layout if head.prompt_layout is not None else self.adapter_set.heads[None].prompt_layout
        if layout is None:
            raise EngineError(f"{adapter_id}: neither the adapter nor the base names a prompt layout")
        return str(layout)

    def _not_loaded(self, adapter_id: str) -> NotInstalled:
        return NotInstalled(adapter_id, f"not loaded in this engine {loaded(self.installed())}")

    def _limit(self, adapter_id: str) -> int:
        """The adapter's (or the base's) option limit; NotInstalled when it is not loaded. Called with
        `_adapters_lock` held."""
        if adapter_id != BASE_ADAPTER and adapter_id not in self._adapters:
            raise self._not_loaded(adapter_id)
        return self._max_options[adapter_id]

    def check(self, adapter_id: str, rows: list[dict[str, Any]]) -> None:
        """prepare(), keeping nothing: validates a request exactly as predict() would, without running the model."""
        self.prepare(adapter_id, rows)

    def prepare(self, adapter_id: str, rows: list[dict[str, Any]]) -> Prepared:
        """_prepare with the length pass: what the engine server's preparation threads call on arrival."""
        return self._prepare(adapter_id, rows, length_pass=True)

    def _prepare(self, adapter_id: str, rows: list[dict[str, Any]], *, length_pass: bool) -> Prepared:
        """Validate a request exactly as predict() would, without running the model, and prepare it for
        predict_many: raises NotInstalled for an adapter that is not loaded, InvalidRequest for a row without
        "state" or "question" (or, on the mlx backend, a row with images, which that backend cannot read; or an image
        that cannot be read or has more than 16 million pixels), TooManyLabels for a choice question with more options
        than the adapter's max_options, and InputTooLong for a row whose prompt is longer than max_tokens. A row with
        images on torch/fast is counted through the full processor exactly as DecisionModel.prepare does (text and
        opened images), kept on the CPU: nothing is moved to a device, and the model itself is never called.

        On the fast backend the request is also routed (prepare.prepare_rows: images, or more new tokens than the
        largest bucket, go to the standard path) and, for the fast path, each row's prompt is split by the adapter's
        own layout and its new text tokenised, so the GPU thread never tokenises a row. The Prepared's route is
        "fast" or "standard", and its items each row's prepare.Row or the reason it runs on the standard path.

        Uses a processor of the preparation pool (never the one the model uses) and the adapter's own prompt layout;
        safe to call from up to prepare_workers threads at once (more wait for a free processor) while
        predict_many() runs on another. The Prepared carries the adapter's load number (generation), read with its
        layout and option limit.

        length_pass=False (predict(), in process) leaves out the length check of rows that do not take the fast path
        (mlx, torch, and the fast backend's standard route): the model's own pass renders and tokenises them anyway and
        refuses an over-long one with the same InputTooLong, so they are not rendered and tokenised twice. The
        engine server keeps the pass (prepare()), so an over-long request is refused alone, before it joins a
        micro-batch."""
        with self._adapters_lock:
            limit = self._limit(adapter_id)
            layout, generation = self._layouts[adapter_id], self._generations[adapter_id]
        validated = self._validated(adapter_id, rows, limit)
        too_long = InputTooLong(f"Question branch exceeds the {self.max_tokens}-token limit; {TOO_LONG_MESSAGE}.")
        if self.fast is None and not length_pass:
            return Prepared(validated, generation=generation)
        with self._preparers.borrow() as processor:
            if self.fast is not None:
                routed = prepare_rows(processor, processor.tokenizer, self.model.codes, layout,
                                      cast(list[DecisionInput], validated))
                if not isinstance(routed, str):
                    # The prompt is split where the prefix's tokens and the new text's tokens join into exactly the
                    # whole prompt's (fast_path.py), so its length is theirs added: the prefix's, often remembered
                    # (rows of one screen share it), and the new text's, just tokenised. A prefix longer than the
                    # prefix pool holds is refused here too (it could pass with a --max-tokens above it), so the
                    # refusal stays this request's own instead of failing the fast path's micro-batch.
                    for item in routed:
                        prefix = self._preparers.prefix_length(processor, item.prefix_text)
                        if prefix > LONGEST_PREFIX_TOKENS:
                            raise InputTooLong(f"The question's fixed part has {prefix} tokens; the fast path holds "
                                               f"at most {LONGEST_PREFIX_TOKENS}; {TOO_LONG_MESSAGE}.")
                        if prefix + len(item.new_ids) > self.max_tokens:
                            raise too_long
                    return Prepared(validated, routed, "fast", generation)
                if not length_pass:
                    return Prepared(validated, [routed] * len(validated), f"standard: {routed}", generation)
            for row in validated:
                messages = decision_messages(cast(DecisionInput, row), self.model.codes, layout)
                images = row.get("images")
                text = processor.apply_chat_template(messages, tokenize=False, add_generation_prompt=True,
                                                     enable_thinking=False)
                if images:
                    encoded = processor(text=[text], images=images, padding=True, return_tensors="pt")
                    token_count = int(encoded["input_ids"].shape[1])
                else:
                    token_count = len(processor.tokenizer(text, add_special_tokens=False)["input_ids"])
                if token_count > self.max_tokens:
                    raise too_long
        if self.fast is None:
            return Prepared(validated, generation=generation)
        return Prepared(validated, [routed] * len(validated), f"standard: {routed}", generation)

    def _validated(self, adapter_id: str, rows: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
        """The checks check() and predict() share, so an in-process caller gets the same typed errors as one behind
        the engine server: NotInstalled, InvalidRequest (a row without "state" or "question", images on mlx, an
        image that cannot be read, is not PNG, JPEG or WebP, or has more than 16 million pixels) and TooManyLabels.
        Returns the rows with their images decoded (PIL images, which DecisionModel.prepare takes as they are), so
        nothing is decoded twice. `limit`: the adapter's option limit, read by the caller with its layout."""
        for row in rows:
            if "state" not in row or "question" not in row:
                raise InvalidRequest(f"Each row needs 'state' and 'question'; got {sorted(row)}")
            if self.backend == "mlx" and row.get("images"):
                raise InvalidRequest(IMAGES_NOT_SUPPORTED_MESSAGE)
        check_option_limit(adapter_id, rows, limit)
        return [{**row, "images": [open_checked_image(value, number)
                                   for number, value in enumerate(row["images"], start=1)]}
                if row.get("images") else row for row in rows]

    def add_adapter(self, adapter_id: str, path: Path) -> None:
        """Load a new adapter beside the ones already serving. If the fast path rejects an adapter the shared-base
        AdapterSet already accepted, the AdapterSet load is undone too, so a failed add_adapter leaves nothing
        behind and a retry sees the same clean state."""
        if adapter_id == BASE_ADAPTER:
            raise EngineError(f"{BASE_ADAPTER!r} is the base model, always served; it cannot be added as an adapter")
        if adapter_id in self._adapters:
            raise EngineError(f"{adapter_id} is already loaded in this engine")
        if self.max_adapters is not None and len(self._adapters) >= self.max_adapters:
            raise capacity_error(self.max_adapters, len(self._adapters))
        path = Path(path)
        limit = read_max_options(path)  # before anything loads: a checkpoint without it changes nothing
        if self.backend == "mlx":
            self.model.add_adapter(adapter_id, path)
        else:
            self.adapter_set.add_adapter(adapter_id, path)
            if self.fast is not None:
                try:
                    self.fast.add_adapter(adapter_id)
                except Exception as error:
                    self.adapter_set.remove_adapter(adapter_id)
                    raise EngineError(f"{adapter_id}: the fast path rejected this adapter: {error}") from error
        with self._adapters_lock:
            self._layouts[adapter_id] = self._prompt_layout(adapter_id)
            self._max_options[adapter_id] = limit
            self._generations[adapter_id] = next(self._loads)
            self._adapters[adapter_id] = path  # the adapter shows as loaded; all set together, atomically
                                                # with check()'s own matched read of them

    def remove_adapter(self, adapter_id: str) -> None:
        """Unload an adapter while still serving the others. The base cannot be removed."""
        if adapter_id == BASE_ADAPTER:
            raise EngineError(f"{BASE_ADAPTER!r} is the base model, always served; it cannot be removed")
        if adapter_id not in self._adapters:
            raise EngineError(f"{adapter_id} is not loaded in this engine {loaded(self._adapters)}")
        if self.backend == "mlx":
            self.model.remove_adapter(adapter_id)
        else:
            if self.fast is not None:
                self.fast.remove_adapter(adapter_id)
            self.adapter_set.remove_adapter(adapter_id)
        with self._adapters_lock:
            del self._adapters[adapter_id]
            del self._layouts[adapter_id]
            del self._max_options[adapter_id]
            del self._generations[adapter_id]

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        """Prepared on this thread (without the length pass the model's own pass makes; see _prepare), then
        predict_many() with that one group."""
        [result] = self.predict_many([(adapter_id, self._prepare(adapter_id, rows, length_pass=False))])
        if isinstance(result, JeffError):
            raise result
        return result

    def predict_many(self, groups: list[tuple[str, Prepared]]) -> list[EngineResult | JeffError]:
        """One result per (adapter ID, rows from prepare()) group, in order; each row's probabilities, its prompt
        length and the timing. On the fast backend every group prepared for the fast path runs together, as passes per
        new-token bucket (BatchedPath), and each group prepared for the standard path runs on its own; on mlx and
        torch the groups run one after another. Call from one thread at a time: the model's thread.

        A group whose adapter is no longer loaded, or was loaded again, since its rows were prepared (its generation
        differs) gets NotInstalled naming it, and does not run: its rows were split and checked for the adapter as it
        was. A typed
        failure of a group run on its own (InputTooLong, InvalidRequest) is that group's alone, and one of the fast
        path is every fast group's. Any other failure is raised.

        timing per group: path ("fast", "standard" (with reason, why the fast path does not take it), "mlx"),
        format (the base's weight format, "bf16" or "int4-g64", and for INT4 its format_id: base_format), and
        model_ms, the wall time of the work its rows were part of (on the fast path, all of the call's fast rows
        together: prefixes, passes and copies). On the fast path also prefix_ms, prefix_misses (new prefixes
        processed), prefix_hits (rows whose prefix was cached), prefix_shared (rows sharing a new prefix with an
        earlier row of the call), prefix_passes and passes, all for the call's fast rows together (every group of
        the call gets the same values; they are never a group's or a request's own); row_timing per row:
        new_tokens (the row's new-text tokens, before padding to its bucket), pass_rows (rows in its pass), bucket
        (its pass's new-token bucket) and adapters_in_pass (distinct adapters in its pass). A group can hold several
        requests' rows (the scheduler joins requests on one adapter), so anything a request needs for itself alone is
        per row, and the scheduler hands each request its own rows' slice."""
        results: list[EngineResult | JeffError | None] = [None] * len(groups)
        with self._adapters_lock:
            generations = dict(self._generations)
        for i, (adapter_id, prepared) in enumerate(groups):
            if prepared.generation is None:
                raise EngineError(f"{adapter_id}: predict_many was given rows this engine's prepare() did not make")
            if generations.get(adapter_id) != prepared.generation:
                results[i] = NotInstalled(adapter_id, "was unloaded or loaded again after this request arrived, so "
                                                      "it was prepared for the adapter as it was then; send it again")
        fast = [i for i, (_, prepared) in enumerate(groups) if prepared.route == "fast" and results[i] is None]
        if fast:
            try:
                outcome: list[EngineResult] | JeffError = self._typed(lambda: self._fast_many([groups[i] for i in fast]))
            except JeffError as error:
                outcome = error
            for n, i in enumerate(fast):
                results[i] = outcome if isinstance(outcome, JeffError) else outcome[n]
        for i, (adapter_id, prepared) in enumerate(groups):
            if results[i] is None:
                try:
                    results[i] = self._typed(lambda: self._one_group(adapter_id, prepared))
                except JeffError as error:
                    results[i] = error
        return cast(list[EngineResult | JeffError], results)

    @staticmethod
    def _typed[T](work: Callable[[], T]) -> T:
        """work(), with the ported engine's ValueErrors for an over-long request or images on MLX raised as the typed
        errors they are (InputTooLong, InvalidRequest); any other error as it is."""
        try:
            return work()
        except ValueError as error:
            text = str(error)
            if IMAGES_NOT_SUPPORTED_MESSAGE in text:
                raise InvalidRequest(text) from error
            if TOO_LONG_MESSAGE not in text:
                raise
            raise InputTooLong(text) from error

    def _one_group(self, adapter_id: str, prepared: Prepared) -> EngineResult:
        """One group on mlx, torch, or the standard path of the fast backend."""
        adapter = None if adapter_id == BASE_ADAPTER else adapter_id
        rows = prepared.rows
        started = time.perf_counter()
        timing: dict[str, Any]
        if self.backend == "mlx":
            self.model.use(adapter)
            decided = self.model.decide(rows, self.max_tokens)
            values = [probabilities for probabilities, _ in decided]
            tokens = [count for _, count in decided]
            timing = {"path": "mlx"}
        else:
            self.adapter_set.use(adapter)
            values, tokens = self._standard(rows, self._layout(adapter))
            timing = {"path": "standard"}
            if prepared.route is not None:  # the fast backend's standard route: "standard: <why>"
                timing["reason"] = prepared.route.removeprefix("standard: ")
        timing["model_ms"] = round((time.perf_counter() - started) * 1000, 3)
        timing.update(self.base_format)
        return EngineResult([list(map(float, row)) for row in values], timing, [int(count) for count in tokens])

    def _fast_many(self, groups: list[tuple[str, Prepared]]) -> list[EngineResult]:
        """Every group's rows on the batched fast path together, results split back per group.

        The rows go in runs of as many distinct prefixes as the prefix pool is sure to hold at once (prefixes of the
        longest length, prefix_pool.prefixes_held): for each run, each row's prefix made sure of in the pool (pinned
        until its passes are over), the request token limit checked, then its passes. The engine server's
        micro-batches always fit in one run (jeff runtime serve refuses a pool that cannot hold --max-batch-rows
        longest prefixes); a direct predict() with more distinct prefixes than that (Jeff.local, the batch tools) runs
        in several."""
        started = time.perf_counter()
        rows = [(self.fast.index(None if adapter_id == BASE_ADAPTER else adapter_id), item)
                for adapter_id, prepared in groups for item in cast(list[Any], prepared.items)]
        values: list[list[float]] = []
        tokens: list[int] = []
        per_row: dict[str, list[Any]] = {"pass_rows": [], "bucket": [], "adapters_in_pass": []}
        totals: dict[str, Any] = {name: 0 for name in ("passes", "prefix_ms", "prefix_misses", "prefix_hits",
                                                       "prefix_shared", "prefix_passes")}
        for run in self._runs(rows):
            run_values, run_tokens, run_timing = self._fast_run(run)
            values.extend(run_values)
            tokens.extend(run_tokens)
            for name in per_row:
                per_row[name].extend(run_timing[name])
            for name in totals:
                totals[name] += run_timing[name]
        shared = {"path": "fast", "model_ms": round((time.perf_counter() - started) * 1000, 3), **totals,
                  **self.base_format}
        results = []
        offset = 0
        for _, prepared in groups:
            mine = slice(offset, offset + len(prepared.rows))
            offset += len(prepared.rows)
            results.append(EngineResult(
                [list(map(float, row)) for row in values[mine]],
                dict(shared),
                [int(n) for n in tokens[mine]],
                {**{name: per_row[name][mine] for name in per_row},
                 "new_tokens": [len(row.new_ids) for row in cast(list[Any], prepared.items)]}))
        return results

    def _runs(self, rows: list[tuple[int, Any]]) -> list[list[tuple[int, Any]]]:
        """The rows cut, in order, into runs of at most self._prefixes_at_once distinct (adapter, prefix text)."""
        runs: list[list[tuple[int, Any]]] = []
        keys: set[tuple[int, str]] = set()
        for index, row in rows:
            key = (index, row.prefix_text)
            if not runs or (key not in keys and len(keys) == self._prefixes_at_once):
                runs.append([])
                keys = set()
            keys.add(key)
            runs[-1].append((index, row))
        return runs

    def _fast_run(self, rows: list[tuple[int, Any]]) -> tuple[list[list[float]], list[int], dict[str, Any]]:
        """One run of _fast_many: prefixes made sure of and pinned, the token limit checked, the passes, unpinned."""
        batched, pool = self.batched, self.fast.prefixes
        slots, prefix_timing = batched.ensure_prefixes([(index, row.prefix_text) for index, row in rows],
                                                       self.max_tokens)
        try:
            for (_, row), slot in zip(rows, slots, strict=True):
                if pool.length(slot) + len(row.new_ids) > self.max_tokens:
                    raise ValueError(f"Question branch exceeds the {self.max_tokens}-token limit; {TOO_LONG_MESSAGE}.")
            values, tokens, pass_timing = batched.decide([(index, slot, row)
                                                          for (index, row), slot in zip(rows, slots, strict=True)])
        finally:
            pool.unpin(slots)
        return values, tokens, {**prefix_timing, "passes": pass_timing["passes"],
                                "pass_rows": pass_timing["batch_rows"], "bucket": pass_timing["bucket"],
                                "adapters_in_pass": pass_timing["adapters_in_pass"]}

    def standard_probabilities(self, adapter_id: str, rows: list[dict[str, Any]]) -> list[list[float]]:
        """The standard path's probabilities for rows of one loaded adapter (or the base), whatever route the fast
        backend would give them: the reference the fast path is held to (jeff runtime gate's agreement step). Torch
        and fast backends only; call from the model's thread, as predict_many."""
        if self.backend == "mlx":
            raise EngineError("The MLX backend has no standard path; standard_probabilities needs torch or fast")
        with self._adapters_lock:
            self._limit(adapter_id)  # NotInstalled for an adapter that is not loaded
        adapter = None if adapter_id == BASE_ADAPTER else adapter_id
        self.adapter_set.use(adapter)
        values, _ = self._typed(lambda: self._standard(rows, self._layout(adapter)))
        return [list(map(float, row)) for row in values]

    def _layout(self, adapter: str | None) -> str:
        """The prompt layout of `adapter` (None: the base), from the table filled when it was loaded."""
        with self._adapters_lock:
            return self._layouts[BASE_ADAPTER if adapter is None else adapter]

    def _standard(self, rows: list[dict[str, Any]], layout: str) -> tuple[list[list[float]], list[int]]:
        """As jeff-server's server.distributions() (the non-mlx branch): chunks of 8 rows, one prepare per chunk
        (which refuses a chunk over max_tokens, never truncates), temperature applied, trimmed to each row's option
        count. Not DecisionModel.predict, which calls prepare without max_length and so never refuses long input.
        Each row's prompt length is its count of real (unpadded) tokens, as jeff-server's input_tokens. `layout` is
        the adapter's own prompt layout, passed rather than read from the model, whose current one is set by
        adapter_set.use()."""
        was_training = self.model.training
        self.model.eval()
        values: list[list[float]] = []
        tokens: list[int] = []
        try:
            with torch.inference_mode():
                for start in range(0, len(rows), 8):
                    batch = self.model.prepare(rows[start:start + 8], max_length=self.max_tokens, layout=layout)
                    probabilities: list[list[float]] = (self.model(batch) / self.model.temperature).softmax(-1).cpu().tolist()
                    values.extend(row[:count] for row, count in zip(probabilities, batch.counts))
                    tokens.extend(int(count) for count in batch.inputs["attention_mask"].sum(-1).tolist())
        finally:
            self.model.train(was_training)
        return values, tokens
