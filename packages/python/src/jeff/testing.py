"""A fake engine for tests: answers come from a function you supply."""

import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from jeff.engine import (BASE_ADAPTER, BF16, EngineResult, Prepared, capacity_error, check_option_limit,
                         format_id_of)
from jeff.errors import EngineError, InputTooLong, InvalidRequest, JeffError, NotInstalled


MAX_ANSWER_CODES = 255  # a Jeff model's readout has 255 answer codes


def fake_tokens(row: dict[str, Any]) -> int:
    """FakeEngine's stand-in for a row's prompt length: one token per whitespace-separated word of the row's JSON."""
    return len(json.dumps(row).split())


class FakeEngine:
    """installed: the adapter IDs it serves (include "base", jeff.engine.BASE_ADAPTER, to serve the base as a real
    engine does). max_options: adapter ID -> the most options a choice question may have there; when given,
    check() and predict() refuse more with TooManyLabels, as jeff_runtime.LocalEngine does, and every installed
    adapter must have an entry. max_adapters: the most adapters (beside the base) it holds, as the fast backend's
    --max-adapters; None, the default, for no limit. base_format: the base's weight format it reports, as
    LocalEngine does (jeff.engine.Engine.base_format); the default is a bfloat16 base, {"format": "bf16"}; pass
    {"format": "int4-g64", "format_id": "..."} to act as an engine serving an INT4 base."""

    def __init__(self, answers: Callable[[str, dict[str, Any]], list[float]], installed: set[str],
                 max_options: Mapping[str, int] | None = None, *, max_adapters: int | None = None,
                 base_format: Mapping[str, str] | None = None) -> None:
        self.answers = answers
        self._installed = set(installed)
        self.max_options = dict(max_options) if max_options is not None else None
        self.calls: list[tuple[str, list[dict[str, Any]]]] = []
        # one entry per predict_many call (one per micro-batch from the scheduler): its groups' (adapter, rows)
        self.batches: list[list[tuple[str, int]]] = []
        self.backend = "fake"
        self.base_checkpoint = "fake-base"
        self.base_format = dict(base_format) if base_format is not None else {"format": BF16}
        format_id_of(self.base_format)  # refuses a malformed format here, as a real engine's base folder would be
        self.max_adapters = max_adapters

    @property
    def base_max_options(self) -> int:
        """max_options["base"]; without option limits FakeEngine refuses no count, so the answer codes' own
        maximum, 255."""
        if self.max_options is None:
            return MAX_ANSWER_CODES
        if BASE_ADAPTER not in self.max_options:
            raise EngineError("FakeEngine has max_options but no entry for the base ('base')")
        return self.max_options[BASE_ADAPTER]

    def _check_options(self, adapter_id: str, rows: list[dict[str, Any]]) -> None:
        if self.max_options is None:
            return
        if adapter_id not in self.max_options:
            raise EngineError(f"FakeEngine has no max_options entry for {adapter_id}")
        check_option_limit(adapter_id, rows, self.max_options[adapter_id])

    def installed(self, refresh: bool = False) -> set[str]:
        return set(self._installed)

    def predict(self, adapter_id: str, rows: list[dict[str, Any]]) -> EngineResult:
        if adapter_id not in self._installed:
            raise NotInstalled(adapter_id, "FakeEngine has not loaded it")
        self._check_options(adapter_id, rows)
        self.calls.append((adapter_id, rows))
        return EngineResult([self.answers(adapter_id, row) for row in rows],
                            {"path": "fake", "model_ms": 0.0, **self.base_format}, [fake_tokens(row) for row in rows])

    def predict_many(self, groups: list[tuple[str, Prepared]]) -> list[EngineResult | JeffError]:
        """One predict() per group, in order (as LocalEngine on MLX and on the standard path); a group whose
        predict() raises a typed error gets it in its place."""
        self.batches.append([(adapter_id, len(prepared.rows)) for adapter_id, prepared in groups])
        results: list[EngineResult | JeffError] = []
        for adapter_id, prepared in groups:
            try:
                results.append(self.predict(adapter_id, prepared.rows))
            except JeffError as error:
                results.append(error)
        return results

    def prepare(self, adapter_id: str, rows: list[dict[str, Any]]) -> Prepared:
        """check(); FakeEngine has nothing more to prepare."""
        self.check(adapter_id, rows)
        return Prepared(rows)

    def check(self, adapter_id: str, rows: list[dict[str, Any]]) -> None:
        """Validate a request without calling `answers`. A row whose "state" is a mapping containing the key
        "__too_long__" is treated as over the token limit: a test hook, since FakeEngine has no real tokenizer to
        measure a request's length against."""
        if adapter_id not in self._installed:
            raise NotInstalled(adapter_id, "FakeEngine has not loaded it")
        for row in rows:
            if "state" not in row or "question" not in row:
                raise InvalidRequest(f"Each row needs 'state' and 'question'; got {sorted(row)}")
            state = row["state"]
            if isinstance(state, dict) and "__too_long__" in state:
                raise InputTooLong("FakeEngine: row state contains the test hook key '__too_long__'; "
                                   "no input was truncated.")
        self._check_options(adapter_id, rows)

    def add_adapter(self, adapter_id: str, path: Path) -> None:
        if adapter_id in self._installed:
            raise EngineError(f"FakeEngine: {adapter_id} is already installed")
        loaded = len(self._installed - {BASE_ADAPTER})
        if self.max_adapters is not None and loaded >= self.max_adapters:
            raise capacity_error(self.max_adapters, loaded)
        self._installed.add(adapter_id)

    def remove_adapter(self, adapter_id: str) -> None:
        if adapter_id not in self._installed:
            raise EngineError(f"FakeEngine: {adapter_id} is not installed")
        self._installed.discard(adapter_id)


def fake_adapter_folder(folder: Path, adapter_id: str, *, base_version: str = "v1.3") -> Path:
    """An adapter folder for tests of adapter discovery (jeff.adapter_dirs): the shipped catalogue's triage
    manifest with its ID set to `adapter_id`, no aliases and base jeff-base@`base_version`, plus small dummy weight
    files. Nothing can load these weights; they exist only to be found, stamped and handed to a FakeEngine."""
    import yaml

    from jeff.contract import shipped_dir

    raw = yaml.safe_load((shipped_dir() / "catalogue/jeff.support.triage/manifest.yaml").read_text(encoding="utf-8"))
    raw["id"], raw["aliases"] = adapter_id, []
    raw["base"]["version"] = base_version
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False), encoding="utf-8")
    (folder / "adapter_config.json").write_text("{}")
    (folder / "adapter_model.safetensors").write_bytes(b"adapter")
    (folder / "readout.safetensors").write_bytes(b"readout")
    return folder


FAKE_FORMAT_ID = "jeff-base@v1.3+int4-g64-gptq.0123abcd"
FAKE_INT4_SHA256 = "0123abcd" * 8


def fake_int4_base(folder: Path, *, format_id: str = FAKE_FORMAT_ID, int4_sha256: str = FAKE_INT4_SHA256,
                   source: tuple[str, str] = ("jeff-base", "v1.3")) -> Path:
    """An INT4 base folder for tests of the gate check (jeff.gate.check_served, `jeff runtime serve --base`): only its
    decision_config.json "quantization" object and the record's int4_sha256 and source_base, which is all serving
    reads before an engine loads. Nothing can load it as a model."""
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "decision_config.json").write_text(json.dumps({
        "format_version": 1, "max_options": 26,
        "quantization": {"format": "int4-g64", "format_id": format_id, "method": "gptq",
                         "source_base": {"weights_sha256": {"model.safetensors": "1" * 64}},
                         "record": "quantization.json"}}))
    (folder / "quantization.json").write_text(json.dumps({
        "format": "int4-g64", "method": "gptq", "int4_sha256": int4_sha256,
        "source_base": {"id": source[0], "version": source[1], "weights_sha256": {"model.safetensors": "1" * 64}}}))
    return folder


def fake_gate(folder: Path, adapters: Mapping[str, list[str]], *, overall: list[str] | None = None,
              format_id: str = FAKE_FORMAT_ID, int4_sha256: str = FAKE_INT4_SHA256) -> Path:
    """Writes a gate.json into `folder` (fake_int4_base) as `jeff runtime gate` writes it, with the fields serving
    reads: each adapter (id@version) with its failures (empty: it passed), the set's overall failures, and the base
    it is the gate of. It passed when nothing failed."""
    from jeff.gate import GATE_FILE, GATE_FORMAT, GATE_VERSION

    overall = list(overall or [])
    passed = not overall and not any(adapters.values())
    path = folder / GATE_FILE
    path.write_text(json.dumps({
        "format": GATE_FORMAT, "version": GATE_VERSION, "passed": passed, "overall_failures": overall,
        "base": {"format_id": format_id, "int4_sha256": int4_sha256},
        "adapters": {key: {"passed": not failures, "failures": list(failures)} for key, failures in adapters.items()}}))
    return path
