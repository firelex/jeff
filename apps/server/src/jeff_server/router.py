"""EngineRouter: presents several jeff.socket_engine.SocketEngine processes (one per engine) as a single
jeff.engine.Engine.

Every engine in a deployment is expected to serve the exact same set of adapters (changing it goes through every
engine at once -- jeff_server.operations; the factory's publish changes them one at a time, and installed() answers
what every engine serves meanwhile). predict() sends each row to one engine chosen by hashing its *binding*: the adapter, its
question, and the part of its state that stays fixed across every call sharing that binding. Rows are built by
jeff.binding.build_row in the "live-last" layout -- `{**context, input.key: input}` -- so the one field that varies
from call to call (the caller's input) is always the LAST key inserted into `state`; everything before it is the
fixed context that gives every call on this binding the same prompt prefix. Dropping that last field, hashing what
remains, and sending every row with the same binding key to the same engine process lets that engine reuse the
prefix instead of reprocessing it from scratch on a different process each time.

`state` is not always an object, though: jeff_runtime's decision_messages (runtime/python/src/jeff_runtime/engine/
model.py) accepts a plain string or a list there on purpose, and always puts a non-object state "state-first" --
the WHOLE state ahead of the question and options, regardless of the configured layout -- because there is no
single trailing field to carve a fixed part out of a string or a list. /v1/systemone (jeff_server.systemone) passes
such rows through unchanged, so a non-object state is not an error here either: see _binding_key for the binding
key it gets instead.
"""

import hashlib
import json
import threading
from collections.abc import Sequence
from typing import Any

from jeff.engine import EngineResult
from jeff.errors import EngineError, InvalidRequest
from jeff.socket_engine import SocketEngine

from jeff_server.redaction import served_lists_error


def _fixed_state(state: dict[str, Any]) -> dict[str, Any]:
    """The part of an object state that is the same for every call sharing its binding: `state` with its last
    field (the varying input, added last by build_row) dropped. A state with no fields, or exactly one field, has
    no fixed part either -- an empty dict, not a fallback, just what "no state" and "one-field state" both reduce
    to."""
    items = list(state.items())
    return dict(items[:-1])


def _binding_key(adapter_id: str, row: dict[str, Any]) -> str:
    """sha256(adapter_id + json(question) + json(state without its last field)) for an object state (the part
    that fixes the live-last prompt prefix); for a non-object state (a plain string or a list, both accepted on
    purpose -- see the module docstring), there is no trailing field to drop and nothing fixed to fold in from
    state at all, so the key is sha256(adapter_id + json(question)): the part of a state-first prompt that does
    not depend on state."""
    missing = [key for key in ("state", "question") if key not in row]
    if missing:
        raise InvalidRequest(f"Each row needs 'state' and 'question'; missing {missing}; got {sorted(row)}")
    state, question = row["state"], row["question"]
    payload = adapter_id + json.dumps(question) + (json.dumps(_fixed_state(state)) if isinstance(state, dict) else "")
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class EngineRouter:
    """One jeff.engine.Engine over `engines`. `engines` is kept as a public attribute, in call order, so health
    and metrics code can report on each engine by its index."""

    def __init__(self, engines: Sequence[SocketEngine]) -> None:
        if not engines:
            raise EngineError("EngineRouter needs at least one engine")
        self.engines: list[SocketEngine] = list(engines)
        # One base format for the deployment: a row may go to any engine, and a function's calibration is chosen
        # once, for one format (jeff.engine.Engine.base_format).
        formats = [engine.base_format for engine in self.engines]
        if any(found != formats[0] for found in formats[1:]):
            named = ", ".join(f"{engine.path}={found}" for engine, found in zip(self.engines, formats, strict=True))
            raise EngineError(f"The engines serve bases of different formats: {named}; every engine of a deployment "
                              "must serve the same base")
        self.base_format: dict[str, str] = dict(formats[0])
        # The base every engine serves (id, version, as adapter manifests name it), each read when its SocketEngine
        # connected and fixed for this process's life (a reconnect to another base is refused). None when the
        # engines differ, which a deployment never should: base() then raises, so GET /v1/adapters and GET
        # /v1/models answer 500 naming each engine's base.
        self._bases = [engine.base for engine in self.engines]
        self.agreed_base: tuple[str, str] | None = \
            self._bases[0] if all(found == self._bases[0] for found in self._bases[1:]) else None

    def base(self) -> tuple[str, str]:
        """The base every engine serves; EngineError naming each engine's base when they differ."""
        if self.agreed_base is None:
            named = ", ".join(f"{engine.path}={found[0]}@{found[1]}"
                              for engine, found in zip(self.engines, self._bases, strict=True))
            raise EngineError(f"The engines serve different bases: {named}; every engine of a deployment must serve "
                              "the same base")
        return self.agreed_base

    def close(self) -> None:
        """Closes every engine's connection."""
        for engine in self.engines:
            engine.close()

    def installed(self, refresh: bool = False) -> set[str]:
        """What the engines serve.

        refresh=True (GET /v1/adapters' callers, adapter changes, a catalogue rebuild): every engine is asked first,
        and engines that answer with different lists are an EngineError naming each one's list -- a deployment has
        one served set, and those callers need it.

        refresh=False (every decision's served check, jeff.engine.serves): the engines' cached lists, as each one's
        last reply left them. While they agree that is all. While they differ -- the engines are being changed one at
        a time (a factory publish), or a change made everywhere has reached this process only through some engines'
        replies -- every engine is asked once, which brings a stale cache up to date; if they still differ, the
        answer is the intersection, what every engine serves. A call for an adapter every engine serves is then
        answered while a change goes round, and one for an adapter some engine does not serve yet is not served.
        Nothing here asks an engine while the cached lists agree."""
        served = [engine.installed(refresh=refresh) for engine in self.engines]
        if self._agree(served):
            return set(served[0])
        if refresh:
            raise self.disagreement_error(served)
        served = [engine.installed(refresh=True) for engine in self.engines]
        if self._agree(served):
            return set(served[0])
        return set.intersection(*served)

    def disagreement(self) -> str | None:
        """Asks every engine what it serves: None when they all serve the same adapters, otherwise the message
        naming each engine's list (what installed(refresh=True) raises). For a catalogue rebuild that a change of
        version started, which waits for the engines to agree rather than failing a request
        (jeff_server.catalogue_refresh)."""
        served = [engine.installed(refresh=True) for engine in self.engines]
        return None if self._agree(served) else str(self.disagreement_error(served))

    @staticmethod
    def _agree(served: list[set[str]]) -> bool:
        first = served[0]
        return all(other == first for other in served[1:])

    def disagreement_error(self, served: list[set[str]]) -> EngineError:
        """The EngineError naming each engine's served list; a caller who is not the owner sees each list cut to the
        adapters it may see (jeff_server.redaction)."""
        return served_lists_error("The engines disagree on what is installed: ",
                                  [(str(engine.path), adapters) for engine, adapters in zip(self.engines, served,
                                                                                            strict=True)],
                                  lambda engine, adapters: f"{engine}={adapters}", ", ")

    def predict(self, adapter_id: str, rows: list[dict[str, Any]], deadline_ms: float | None = None) -> EngineResult:
        """Groups `rows` by binding key, sends each group to its engine (`int(key, 16) % len(self.engines)`)
        concurrently -- one thread per engine actually involved -- and reassembles the answers in row order.

        `deadline_ms` is passed straight through to every SocketEngine.predict() call, exactly as given here; the
        jeff.engine.Engine protocol's predict() does not declare this parameter (only SocketEngine's does), so it
        is kept as an optional keyword here too, for compatibility with callers that only know the plain protocol.

        If any group's engine call fails, that is the error this call raises -- the SocketEngine's own EngineError
        (or whatever typed error it raised), never wrapped or replaced. Every other group's thread still runs to
        completion first (so a dying engine does not leave another engine's call half-done), but this call still
        raises as soon as any group failed; it is other, separate predict() calls routed to a healthy engine that
        keep succeeding, not other groups *within* this same call."""
        if not rows:
            raise InvalidRequest("EngineRouter.predict was given no rows")

        groups: dict[int, list[tuple[int, dict[str, Any]]]] = {}
        for position, row in enumerate(rows):
            engine_index = int(_binding_key(adapter_id, row), 16) % len(self.engines)
            groups.setdefault(engine_index, []).append((position, row))

        outcomes: dict[int, EngineResult | Exception] = {}

        def run(engine_index: int, members: list[tuple[int, dict[str, Any]]]) -> None:
            engine = self.engines[engine_index]
            try:
                outcomes[engine_index] = engine.predict(adapter_id, [row for _, row in members],
                                                         deadline_ms=deadline_ms)
            except Exception as error:  # noqa: BLE001 - recorded in `outcomes`, raised below, never swallowed
                outcomes[engine_index] = error

        threads = [threading.Thread(target=run, args=(engine_index, members), name=f"jeff-router {engine_index}")
                   for engine_index, members in groups.items()]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        for engine_index in sorted(groups):
            if engine_index not in outcomes:
                # The worker thread for this engine exited without recording anything in `outcomes`: it must
                # have died from something other than Exception (e.g. SystemExit or KeyboardInterrupt raised
                # inside SocketEngine.predict), since `run` above records every Exception before returning.
                raise EngineError(f"Engine index {engine_index} ({self.engines[engine_index].path}) never "
                                   "answered: its worker thread exited without a result")
            outcome = outcomes[engine_index]
            if isinstance(outcome, Exception):
                raise outcome

        by_position: dict[int, list[float]] = {}
        tokens_by_position: dict[int, int] = {}
        untokenised: list[int] = []  # engine indexes that answered without input_tokens
        model_ms_values: list[float] = []
        for engine_index, members in groups.items():
            result = outcomes[engine_index]
            if not isinstance(result, EngineResult):
                raise EngineError(f"Engine index {engine_index} ({self.engines[engine_index].path}) answered "
                                   f"predict with {result!r}, neither an EngineResult nor an exception")
            if len(result.probabilities) != len(members):
                raise EngineError(f"Engine index {engine_index} ({self.engines[engine_index].path}) answered "
                                   f"predict for {len(members)} row(s) with {len(result.probabilities)} "
                                   "probability list(s)")
            for (position, _), probabilities in zip(members, result.probabilities, strict=True):
                by_position[position] = probabilities
            if result.input_tokens is None:
                untokenised.append(engine_index)
            elif len(result.input_tokens) != len(members):
                raise EngineError(f"Engine index {engine_index} ({self.engines[engine_index].path}) answered "
                                   f"predict for {len(members)} row(s) with {len(result.input_tokens)} input token "
                                   "counts")
            else:
                for (position, _), count in zip(members, result.input_tokens, strict=True):
                    tokens_by_position[position] = count
            if "model_ms" not in result.timing:
                raise EngineError(f"Engine index {engine_index} ({self.engines[engine_index].path}) answered "
                                   f"predict without 'model_ms' in its timing: {result.timing}")
            model_ms_values.append(float(result.timing["model_ms"]))

        if untokenised and len(untokenised) != len(groups):
            raise EngineError(f"Some engines answered with input token counts and some without (without: engine "
                              f"index(es) {sorted(untokenised)}); every engine of a deployment must run the same "
                              "engine version")
        merged_probabilities = [by_position[position] for position in range(len(rows))]
        merged_tokens = None if untokenised else [tokens_by_position[position] for position in range(len(rows))]
        # the deployment's one base format (checked at construction), as each engine's own timing names it
        merged_timing = {"model_ms": max(model_ms_values), "engines": sorted(groups), **self.base_format}
        return EngineResult(merged_probabilities, merged_timing, merged_tokens)
