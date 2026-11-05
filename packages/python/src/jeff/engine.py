"""What the decision logic needs from an inference engine (Engine), and what the scheduler and the server
additionally need to validate a request without running the model and to add or remove adapters while serving
(ServingEngine). jeff_runtime.LocalEngine is the real engine; a remote engine (step 2) and jeff.testing.FakeEngine
implement the same protocols.

Every engine also serves its base model with no adapter, under the adapter id BASE_ADAPTER ("base"): it is always in
installed(), it can be neither added nor removed, and no manifest can have that id (manifest IDs have three dotted
parts). /v1/systemone uses it for requests to the base."""

from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from jeff.errors import EngineError, JeffError, TooManyLabels

BASE_ADAPTER = "base"


@dataclass(frozen=True)
class EngineResult:
    """probabilities: one list per row, over the row's options in order. input_tokens: the prompt length of each
    row, in tokens, when the engine reports it (jeff_runtime.LocalEngine and jeff.testing.FakeEngine do; the
    server's /v1/systemone reports their sum as usage.input_tokens and refuses an engine that does not).
    row_timing: timing that differs per row (for example the pass each row ran in), as lists in row order; the
    scheduler hands each request the part of every list that belongs to its own rows, inside its timing."""
    probabilities: list[list[float]]
    timing: dict[str, Any] = field(default_factory=dict)
    input_tokens: list[int] | None = None
    row_timing: dict[str, list[Any]] | None = None


@dataclass(frozen=True)
class Prepared:
    """A request's rows as ServingEngine.prepare left them on arrival, ready for predict_many: the GPU thread does
    no preparation of its own.

    rows: the rows, checked (images decoded). items: the engine's own preparation, one item per row (on the fast
    backend, each row's prompt split and its new tokens tokenised), or None when the engine needs nothing beyond
    `rows`. route: the engine's name for the way these rows will run (on the fast backend "fast", or "standard: "
    and why the fast path does not take them), or None. generation: the engine's mark of the adapter's load these
    rows were prepared for (LocalEngine: a number that changes whenever the adapter is loaded again), so rows prepared
    before a reload are refused rather than run with the old layout and limits, or None. The scheduler joins
    requests into one group only when their adapter, route and generation are all the same, and never reads route or
    generation otherwise."""
    rows: list[dict[str, Any]]
    items: list[Any] | None = None
    route: str | None = None
    generation: Any = None

    def __post_init__(self) -> None:
        if self.items is not None and len(self.items) != len(self.rows):
            raise EngineError(f"Prepared has {len(self.items)} items for {len(self.rows)} rows")

    def part(self, start: int, stop: int) -> "Prepared":
        """Rows start to stop (a chunk of a large request), with their items; this one itself when that is all of it."""
        if start == 0 and stop == len(self.rows):
            return self
        return Prepared(self.rows[start:stop], None if self.items is None else self.items[start:stop], self.route,
                        self.generation)

    @staticmethod
    def join(parts: Sequence["Prepared"]) -> "Prepared":
        """Several requests' rows in order, as one group; every part must have the same route and generation. One
        part is returned as it is."""
        if len(parts) == 1:
            return parts[0]
        routes = {(part.route, part.generation) for part in parts}
        if len(routes) != 1:
            raise EngineError(f"Prepared.join was given parts of several routes or generations {sorted(map(str, routes))}")
        if all(part.items is None for part in parts):
            items = None
        elif any(part.items is None for part in parts):
            raise EngineError("Prepared.join was given parts with items and parts without")
        else:
            items = [item for part in parts for item in part.items or []]
        route, generation = routes.pop()
        return Prepared([row for part in parts for row in part.rows], items, route, generation)


def check_option_limit(adapter_id: str, rows: Sequence[dict[str, Any]], limit: int) -> None:
    """Refuse a choice question with more options than the adapter (or the base) was trained on, `limit` (the
    max_options in its decision_config.json): answer codes past it were never trained, so the model would silently
    never pick those options. Raises TooManyLabels carrying `limit` (it travels in a wire error's details, so the
    server can name the question that is over it); rows without a choice question pass."""
    for row in rows:
        question = row.get("question")
        if not isinstance(question, dict) or question.get("type") != "choice":
            continue
        criteria = question.get("criteria")
        count = len(criteria) if isinstance(criteria, dict) else 0
        if count > limit:
            error = TooManyLabels(f"A choice question has {count} options, but this model handles at most {limit} "
                                  f"({adapter_id} was trained on questions of up to {limit} options). Shortlist the "
                                  "options first, or split the question.")
            error.limit = limit  # type: ignore[attr-defined]
            raise error


BF16 = "bf16"  # the format of a bfloat16 base, the base as released


def format_id_of(base_format: dict[str, str]) -> str | None:
    """The format_id of an engine's base_format: None for a bfloat16 base ({"format": "bf16"}), whose functions decide
    with their `calibration` block; for any other format (an INT4 base) its format_id, the key of the functions'
    calibration_by_format entries. EngineError for a bfloat16 format carrying a format_id or another format lacking
    one."""
    if base_format["format"] == BF16:
        if "format_id" in base_format:
            raise EngineError(f"The engine's base format {base_format} is bfloat16 but carries a format_id")
        return None
    if "format_id" not in base_format:
        raise EngineError(f"The engine's base format {base_format} names no format_id; every base format other than "
                          f"{BF16} is one exact set of weights, named by its format_id")
    return base_format["format_id"]


class Engine(Protocol):
    base_format: dict[str, str]
    """The base's weight format, read once when the engine is built or connects (never per call), in every predict
    timing and in the engine's health: {"format": "bf16"}, or for an INT4 base {"format": "int4-g64", "format_id":
    <the exact INT4 base's name>} (jeff_runtime.engine.int4). Jeff reads it once to pick each function's
    calibration (format_id_of, jeff.manifest.calibration_for)."""

    def installed(self, refresh: bool = False) -> set[str]:
        """The adapter IDs loaded and ready. An engine that keeps a copy of another process's list (SocketEngine)
        answers from that copy, and with refresh=True asks the other process first; a local engine ignores
        refresh."""
        ...

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        """Probabilities over each row's options, in the row's option order, temperature applied."""
        ...


def serves(engine: Engine, adapter_id: str) -> bool:
    """Whether `engine` serves `adapter_id`. The engine's list may be a cached copy (SocketEngine) that another
    process has since changed, so an adapter missing from it is asked about once more, with refresh=True, before
    it is reported as not served."""
    return adapter_id in engine.installed() or adapter_id in engine.installed(refresh=True)


def capacity_error(max_adapters: int, loaded: int) -> EngineError:
    """Adding an adapter to an engine already holding its most (`--max-adapters`)."""
    return EngineError(f"This engine holds at most {max_adapters} adapters (--max-adapters {max_adapters}) and "
                       f"{loaded} are loaded; remove one first, or restart the engine with a larger --max-adapters")


def check_capacity(max_adapters: int | None, wanted: int) -> None:
    """Refuse a set of `wanted` adapters (beside the base) larger than the engine's `max_adapters` (None: no
    limit), before anything is loaded or unloaded."""
    if max_adapters is not None and wanted > max_adapters:
        raise EngineError(f"{wanted} adapters were asked for, but this engine holds at most {max_adapters} "
                          f"(--max-adapters {max_adapters}); ask for fewer, or restart the engine with a larger "
                          "--max-adapters")


class ServingEngine(Engine, Protocol):
    """An Engine that can also validate a request without running the model, and add or remove adapters while it
    keeps serving. jeff_runtime.LocalEngine and jeff.testing.FakeEngine both implement this; the scheduler and the
    server depend on ServingEngine, not on the smaller Engine protocol."""

    backend: str
    """Which backend is actually running: "mlx", "torch" or "fast" for LocalEngine, "fake" for FakeEngine. The
    engine server reports this in `health` directly, rather than guessing at a default for an engine that has
    none."""

    base_checkpoint: str
    """Where the engine's base model was loaded from (a folder for LocalEngine); reported in the engine's health."""

    max_adapters: int | None
    """The most adapters the engine holds at once beside the base (LocalEngine on the fast backend: its
    --max-adapters), or None for no limit. add_adapter refuses past it (capacity_error), and the engine server
    refuses a `serve` or `reload` asking for more (check_capacity) before changing anything."""

    @property
    def base_max_options(self) -> int:
        """The most options a choice question may have on the base (its decision_config.json's max_options)."""
        ...

    def check(self, adapter_id: str, rows: list[dict[str, Any]]) -> None:
        """Validate a request exactly as predict() would, without touching the GPU. Raise NotInstalled for an
        adapter_id that is not loaded, InvalidRequest for a row missing "state" or "question", or InputTooLong for
        a row whose prompt is longer than the engine's token limit."""
        ...

    def prepare(self, adapter_id: str, rows: list[dict[str, Any]]) -> Prepared:
        """check(), then everything predict_many needs done on the CPU before the model runs (tokenising, on the
        fast backend), without touching the GPU. The engine server calls it on arrival, from any of its preparation
        threads at once; the result travels with the request to predict_many."""
        ...

    def predict_many(self, groups: list[tuple[str, Prepared]]) -> list[EngineResult | JeffError]:
        """One micro-batch: (adapter ID, its prepared rows) groups, one result per group in order. The fast backend
        runs every group's rows together as passes per new-token bucket; the others run the groups one after
        another. Called from one thread only (the scheduler's GPU thread). A group that fails with a typed error
        (its adapter removed since its rows were prepared, say) gets that error in its place and the others still
        run; on the fast backend the groups that run together share one outcome. Any other failure is raised and
        fails the whole call."""
        ...

    def add_adapter(self, adapter_id: str, path: Path) -> None:
        """Load a new adapter beside the ones already serving. Raise EngineError if adapter_id is already loaded."""
        ...

    def remove_adapter(self, adapter_id: str) -> None:
        """Unload an adapter while still serving the others. Raise EngineError if adapter_id is not loaded."""
        ...
