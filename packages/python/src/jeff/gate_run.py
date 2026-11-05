"""`jeff runtime gate` (step 2b-2 design, Section 6): measure an INT4 base against the bfloat16 base it was quantised
from, for a set of adapters, judge it (jeff.gate.judge) and write gate.json into the INT4 folder. Needs an NVIDIA GPU
and jeff-runtime; run it on the GPU type that will serve the base.

Each base runs in an engine of its own, one after the other (the reference first, then the INT4 base), on the fast
backend as `jeff runtime serve` runs it:
1. Engine mode: every published test row of every adapter, its own state and question sent unchanged. The rows of
   all adapters are shuffled together (seed 7), each its own group, and served 64 groups per predict_many call, as an
   engine server's micro-batches, so passes mix adapters. Each row is prepared alone first; a row refused as too long
   on either base is listed and left out of both.
2. Template mode: every test row the manifest describes, rebuilt as the call a user makes (`jeff test`), decided 16
   at a time without thresholds (both bases alike); rows refused as too long are listed and left out of both.
3. On the INT4 base, the fast path against the standard path: up to 1,000 of each adapter's test rows that took the
   fast path in step 1 (seeded sample), their step-1 probabilities against the standard path's on the same engine.
4. On the INT4 base, every calibrated function's threshold refitted as `jeff calibrate --base` fits it (template
   mode, its calibration set minus the rows the quantiser saw), at its target precision, in memory: the gate records
   the thresholds; writing them into manifests stays `jeff calibrate --base`'s job.
5. Determinism: the first two decoder layers quantised twice from the reference base, with the method and inputs
   the record names (GPTQ: the quantiser's rows of the gated adapters it saw, in the record's order), byte for byte
   equal; and, when every adapter the quantiser saw is gated and this is the GPU type the record names, equal to the
   folder's own integers and scales.

Calibration error (ECE, 15 equal-width bins of confidence, on the engine-mode test rows) is recorded for both bases:
temperatures are not refitted per format (design Section 5), and a threshold refit does not change it."""

import gc
import importlib.metadata
import json
import os
import random
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from jeff.calibrate import MeasuringJeff, excluded_reason, format_block, quantiser_rows_of, read_int4_base
from jeff.catalogue import Catalogue
from jeff.decision import checked_probabilities
from jeff.errors import CalibrationError, InputTooLong, JeffError, ManifestError
from jeff.evaluate import (BATCH, TestRow, correct, mirror_read, predicted, read_rows, resolve, same, set_uri,
                           template_rows)
from jeff.gate import (ADAPTER_LIMIT, AGREEMENT, AGREEMENT_ROWS, GATE_FILE, GATE_FORMAT, GATE_VERSION, MEAN_LIMIT,
                       SEED, AdapterMeasures, Counts, agreement_counts, calibration_error, judge, top)
from jeff.manifest import Manifest

ROWS_PER_CALL = 64  # groups per predict_many call in engine mode: an engine server's largest micro-batch
DETERMINISM_LAYERS = 2


@dataclass
class Measured:
    """One base's measurements. engine: per adapter, row id -> (probabilities, the path that answered);
    template: per adapter, row id -> (function id, correct); too_long: per adapter and mode, the row ids refused as
    too long; standard and calibration: on the INT4 base only."""
    base_format: dict[str, str]
    engine: dict[str, dict[str, tuple[list[float], str]]] = field(default_factory=dict)
    template: dict[str, dict[str, tuple[str, bool]]] = field(default_factory=dict)
    too_long: dict[str, dict[str, list[str]]] = field(default_factory=dict)
    standard: dict[str, dict[str, list[float]]] = field(default_factory=dict)
    calibration: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped: dict[str, dict[str, int]] = field(default_factory=dict)  # template mode: rows not rebuilt, by reason
    seconds: dict[str, float] = field(default_factory=dict)


def _by_id(rows: list[dict[str, Any]], what: str) -> dict[str, dict[str, Any]]:
    ids = [row["id"] for row in rows]
    if len(set(ids)) != len(ids):
        repeated = sorted({row_id for row_id in ids if ids.count(row_id) > 1})
        raise JeffError(f"{what} repeats row ids ({', '.join(map(str, repeated[:3]))}); the gate matches rows by id")
    return {row["id"]: row for row in rows}


def confidence_of(question: dict[str, Any], probabilities: list[float]) -> tuple[Any, float]:
    """A row's answer from its raw probabilities, exactly as engine_score (jeff test --engine-rows) scores it, and its
    confidence as make_decision computes it (jeff.decision.checked_probabilities: a reply that breaks the engine's
    contract is refused with EngineError, never rescaled -- rescaling here would hide exactly the broken INT4 output
    the gate exists to catch)."""
    kind = question["type"]
    count = len(question["criteria"]) if kind in ("choice", "score") else 2
    normalised = checked_probabilities(probabilities, count, f"a {kind} question")
    value = predicted(question, probabilities)
    if kind == "noul":
        return value, normalised[1] if value else normalised[0]
    return value, max(normalised)


def machine() -> dict[str, str]:
    """The GPU, its driver (nvidia-smi), CUDA and torch versions. JeffError when nvidia-smi is not on PATH."""
    import torch

    try:
        listed = subprocess.run(["nvidia-smi", "--query-gpu=driver_version", "--format=csv,noheader"],
                                capture_output=True, text=True, check=True).stdout.split()
    except (OSError, subprocess.CalledProcessError) as error:
        raise JeffError(f"jeff runtime gate records the GPU driver from nvidia-smi, which failed ({error}); put it "
                        "on PATH (on WSL: /usr/lib/wsl/lib)") from error
    if len(set(listed)) != 1:
        raise JeffError(f"nvidia-smi listed the drivers {listed}; expected one")
    return {"gpu": torch.cuda.get_device_name(), "driver": listed[0], "cuda": str(torch.version.cuda),
            "torch": torch.__version__}


def _decide_listing_too_long(jeff: Any, rows: list[TestRow]) -> tuple[list[tuple[TestRow, Any]], list[str]]:
    """Each row's decision, BATCH rows per call; a call refused as too long is decided again row by row, and the
    rows refused alone are listed (the gate leaves them out of both bases), not decided."""
    decided: list[tuple[TestRow, Any]] = []
    refused: list[str] = []
    for start in range(0, len(rows), BATCH):
        chunk = rows[start:start + BATCH]
        try:
            decided.extend(zip(chunk, jeff.decide([row.call for row in chunk]), strict=True))
        except InputTooLong:
            for row in chunk:
                try:
                    decided.extend(zip([row], jeff.decide([row.call]), strict=True))
                except InputTooLong:
                    refused.append(row.id)
    return decided, refused


def _engine_mode(engine: Any, tests: Mapping[str, dict[str, dict[str, Any]]], measured: Measured) -> None:
    groups: list[tuple[str, str, Any]] = []
    for adapter_id, rows in tests.items():
        measured.too_long.setdefault(adapter_id, {})["engine"] = []
        for row_id, raw in rows.items():
            try:
                prepared = engine.prepare(adapter_id, [{"state": raw["state"], "question": raw["question"]}])
            except InputTooLong:
                measured.too_long[adapter_id]["engine"].append(row_id)
                continue
            groups.append((adapter_id, row_id, prepared))
    random.Random(SEED).shuffle(groups)
    for start in range(0, len(groups), ROWS_PER_CALL):
        chunk = groups[start:start + ROWS_PER_CALL]
        results = engine.predict_many([(adapter_id, prepared) for adapter_id, _, prepared in chunk])
        for (adapter_id, row_id, _), result in zip(chunk, results, strict=True):
            if isinstance(result, JeffError):
                raise result
            measured.engine.setdefault(adapter_id, {})[row_id] = ([float(p) for p in result.probabilities[0]],
                                                                  str(result.timing["path"]))


def _template_mode(jeff: Any, manifests: Mapping[str, Manifest], tests: Mapping[str, dict[str, dict[str, Any]]],
                   measured: Measured) -> None:
    for adapter_id, rows in tests.items():
        built, measured.skipped[adapter_id] = template_rows(manifests[adapter_id], list(rows.values()))
        decided, refused = _decide_listing_too_long(jeff, built)
        measured.too_long.setdefault(adapter_id, {})["template"] = refused
        measured.template[adapter_id] = {row.id: (row.function, correct(row, decision)) for row, decision in decided}


def _calibration(jeff: Any, int4: Any, manifests: Mapping[str, Manifest], measured: Measured) -> None:
    """Step 4: every calibrated function's INT4 threshold, fitted as `jeff calibrate --base` fits it, in memory."""
    for adapter_id, manifest in manifests.items():
        calibrated = {function.id: function for function in manifest.functions.values()
                      if function.calibration is not None}
        measured.calibration[adapter_id] = {}
        if not calibrated:
            continue
        uri = set_uri(manifest, "calibration")
        read_from = mirror_read(uri)
        raws = list(_by_id(read_rows(manifest, "calibration"), f"{adapter_id}'s calibration set {uri}").values())
        quantiser, recorded = quantiser_rows_of(int4, manifest, raws, uri, published=True, path=resolve(uri))
        rows, _ = template_rows(manifest, raws)
        rows = [row for row in rows if row.function in calibrated]
        excluded = {function_id: sum(str(row.id) in quantiser for row in rows if row.function == function_id)
                    for function_id in calibrated}
        rows = [row for row in rows if str(row.id) not in quantiser]
        decided, refused = _decide_listing_too_long(jeff, rows)
        function_of = {row.id: row.function for row in rows}
        pairs: dict[str, list[tuple[float, bool]]] = {function_id: [] for function_id in calibrated}
        for row, decision in decided:
            if decision.confidence is None:
                raise CalibrationError(f"{row.function} returned a decision without a confidence for row {row.id}")
            pairs[row.function].append((decision.confidence, correct(row, decision)))
        for function_id, function in calibrated.items():
            target = function.calibration.target_precision  # type: ignore[union-attr]
            entry: dict[str, Any] = {"target_precision": target,
                                     "bfloat16_threshold": function.calibration.threshold,  # type: ignore[union-attr]
                                     "too_long": sorted(row_id for row_id in refused
                                                        if function_of[row_id] == function_id)}
            try:
                entry["int4"] = format_block(pairs[function_id], target, uri,
                                             excluded_reason(excluded[function_id], recorded),
                                             data_mirror=read_from)
            except CalibrationError as error:
                entry["error"] = str(error)
            measured.calibration[adapter_id][function_id] = entry


def _agreement_rows(engine_rows: dict[str, tuple[list[float], str]], adapter_id: str) -> list[str]:
    """Up to AGREEMENT_ROWS of an adapter's engine-mode rows that took the fast path, sampled with the seed."""
    fast = sorted(row_id for row_id, (_, path) in engine_rows.items() if path == "fast")
    return sorted(random.Random(f"{SEED}:{adapter_id}").sample(fast, min(AGREEMENT_ROWS, len(fast))))


def _standard(engine: Any, tests: Mapping[str, dict[str, dict[str, Any]]], measured: Measured) -> None:
    for adapter_id, rows in tests.items():
        chosen = _agreement_rows(measured.engine[adapter_id], adapter_id)
        values = engine.standard_probabilities(
            adapter_id, [{"state": rows[row_id]["state"], "question": rows[row_id]["question"]} for row_id in chosen])
        measured.standard[adapter_id] = dict(zip(chosen, values, strict=True))


def _release() -> None:
    import torch

    gc.collect()
    torch.cuda.empty_cache()


def _measure(folder: Path, manifests: Mapping[str, Manifest], folders: Mapping[str, Path], catalogue: Catalogue,
             tests: Mapping[str, dict[str, dict[str, Any]]], *, int4: Any, log: Callable[[str], None]) -> Measured:
    """One base's steps, in one engine (freed before returning). int4: the INT4 base's Int4Base for steps 3 and 4,
    None for the reference."""
    from jeff.local import local
    from jeff_runtime.engine.prefix_pool import prefix_pool_mb_for

    started = time.time()
    jeff = local(list(manifests), catalogue=catalogue, paths=dict(folders), backend="fast",
                 prefix_pool_mb=prefix_pool_mb_for(ROWS_PER_CALL), base=folder)
    measured = Measured(dict(jeff.engine.base_format))
    measured.seconds["engine_start"] = round(time.time() - started, 1)
    log(f"{folder}: engine ready ({measured.base_format['format']}, {measured.seconds['engine_start']} s)")
    measuring = MeasuringJeff(jeff.engine, catalogue)

    def timed(name: str, began: float) -> None:
        measured.seconds[name] = round(time.time() - began, 1)
        log(f"{folder}: {name.replace('_', ' ')} done ({measured.seconds[name]} s)")

    began = time.time()
    _engine_mode(jeff.engine, tests, measured)
    timed("engine_mode", began)
    began = time.time()
    _template_mode(measuring, manifests, tests, measured)
    timed("template_mode", began)
    if int4 is not None:
        began = time.time()
        _standard(jeff.engine, tests, measured)
        timed("agreement", began)
        began = time.time()
        _calibration(measuring, int4, manifests, measured)
        timed("calibration", began)
    del jeff, measuring
    _release()
    return measured


def _determinism(quantization: Any, reference: Path, gated: Mapping[str, tuple[Manifest, Path]],
                 gpu: str, *, log: Callable[[str], None]) -> dict[str, Any]:
    """Step 5. Returns the record's determinism entry; its "failures" list is empty when it passes."""
    import torch
    from safetensors import safe_open

    from jeff_runtime.engine.int4 import INT4_FILE, unpack_int4
    from jeff_runtime.engine.quantize import quantize_gptq, quantize_rtn
    from jeff_runtime.engine.types import DecisionInput

    record = quantization.record
    entry: dict[str, Any] = {"method": quantization.method, "layers": DETERMINISM_LAYERS}
    complete = True  # whether the inputs are exactly the record's (every adapter the quantiser saw is gated)
    if quantization.method == "gptq":
        rows: list[tuple[str, DecisionInput]] = []
        used, left_out = [], []
        for key, ids in record["calibration"]["rows"].items():
            adapter_id, version = key.rsplit("@", 1)
            if adapter_id not in gated or gated[adapter_id][0].version != version:
                left_out.append(key)
                continue
            data = _by_id([json.loads(line) for line in resolve(record["calibration"]["data"][key])
                           .read_text(encoding="utf-8").split("\n") if line.strip()], f"{key}'s calibration set")
            missing = [row_id for row_id in ids if row_id not in data]
            if missing:
                raise JeffError(f"{key}: the quantiser's rows {missing[:3]} are not in its calibration set "
                                f"{record['calibration']['data'][key]}")
            rows.extend((adapter_id, {"state": data[row_id]["state"], "question": data[row_id]["question"]})
                        for row_id in ids)
            used.append(key)
        complete = not left_out
        entry.update(rows=len(rows), adapters=used, adapters_not_gated=left_out)
        folders = {adapter_id: folder for adapter_id, (_, folder) in gated.items()
                   if any(key.rsplit("@", 1)[0] == adapter_id for key in used)}

        def run() -> dict[str, tuple[Any, Any]]:
            return quantize_gptq(reference, folders, rows, layers=DETERMINISM_LAYERS, log=log)
    elif quantization.method == "rtn":
        def run() -> dict[str, tuple[Any, Any]]:
            return quantize_rtn(reference, layers=DETERMINISM_LAYERS)
    else:
        raise JeffError(f"The gate checks the determinism of gptq and rtn bases; this one is {quantization.method}")

    began = time.time()
    first, second = run(), run()
    entry["seconds"] = round(time.time() - began, 1)
    _release()
    entry["matrices"] = len(first)
    differ = sorted(name for name in first if name not in second or not all(
        torch.equal(a, b) for a, b in zip(first[name], second[name], strict=True)))
    if set(second) != set(first):
        differ = sorted(set(differ) | (set(first) ^ set(second)))
    entry["identical_runs"] = not differ
    failures = [f"two runs of the quantiser on the first {DETERMINISM_LAYERS} layers differ in {', '.join(differ)}"] \
        if differ else []
    with safe_open(str(quantization.folder / INT4_FILE), "pt") as handle:
        unlike = sorted(name for name, (integers, scales) in first.items()
                        if not torch.equal(unpack_int4(handle.get_tensor(f"{name}.qweight")), integers.cpu())
                        or not torch.equal(handle.get_tensor(f"{name}.scales"), scales.cpu()))
    entry["reproduces_folder"] = not unlike
    recorded_gpu = record["tool"].get("gpu") if isinstance(record.get("tool"), dict) else None
    if quantization.method == "rtn":
        judged, why = True, "round to nearest needs no GPU and no rows"
    elif not complete:
        judged, why = False, "not every adapter the quantiser saw is gated, so the inputs differ from the record's"
    elif recorded_gpu is None:
        judged, why = False, "the record names no GPU"
    elif recorded_gpu != gpu:
        judged, why = False, f"quantised on {recorded_gpu}, gated on {gpu} (determinism holds per GPU type)"
    else:
        judged, why = True, f"same inputs and GPU type ({gpu}) as the record"
    entry["reproduces_folder_judged"] = judged
    entry["reproduces_folder_note"] = why
    if judged and unlike:
        failures.append(f"the first {DETERMINISM_LAYERS} layers quantised here differ from the folder's "
                        f"{INT4_FILE} in {', '.join(unlike[:3])}{', ...' if len(unlike) > 3 else ''} ({why})")
    entry["failures"] = failures
    return entry


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 6)


def run_gate(base: Path, reference: Path, adapters: list[tuple[Manifest, Path]], catalogue: Catalogue, *,
             log: Callable[[str], None] = print) -> dict[str, Any]:
    """Gate the INT4 base folder `base` against the bfloat16 base folder `reference` it was quantised from, for
    `adapters` ((manifest, weights folder) each), and write gate.json into `base`. Returns the record (its "passed"
    says whether the gate passed). Every check that needs no GPU runs first."""
    import torch

    from jeff_runtime.engine.int4 import read_quantization
    from jeff_runtime.engine.lora import weights_sha256

    started = time.time()
    path = base / GATE_FILE
    if path.exists():
        raise JeffError(f"{path} already exists: a gate is recorded once per INT4 folder; move it aside to gate again")
    quantization = read_quantization(base)
    if quantization is None:
        raise JeffError(f"--base {base} is a bfloat16 base (no quantization in its decision_config.json); the gate "
                        "measures an INT4 base (jeff runtime quantize) against it, given as --reference")
    if read_quantization(reference) is not None:
        raise JeffError(f"--reference {reference} is an INT4 base; give the bfloat16 base {base} was quantised from")
    if weights_sha256(reference) != quantization.source_hashes:
        raise JeffError(f"--reference {reference} is not the base {base} was quantised from: its weights' sha256 "
                        f"differ from the record's source_base ({quantization.source_hashes})")
    source = quantization.record["source_base"]
    if not adapters:
        raise ManifestError("The gate needs at least one adapter (--adapters)")
    other_base = sorted(f"{m.id} ({m.base_id}@{m.base_version})" for m, _ in adapters
                        if (m.base_id, m.base_version) != (source["id"], source["version"]))
    if other_base:
        raise ManifestError(f"{', '.join(other_base)} trained on another base than {source['id']}@{source['version']}, "
                            f"the source of {quantization.format_id}")
    gated = {manifest.id: (manifest, folder) for manifest, folder in adapters}
    if quantization.method == "gptq" and not any(
            key.rsplit("@", 1)[0] in gated and gated[key.rsplit("@", 1)[0]][0].version == key.rsplit("@", 1)[1]
            for key in quantization.record["calibration"]["rows"]):
        saw = ", ".join(quantization.record["calibration"]["rows"])
        raise ManifestError(f"None of the adapters the quantiser saw ({saw}) "
                            "is among --adapters; the determinism step quantises with their rows, so gate at least one "
                            "of them with the others")
    if not torch.cuda.is_available():
        raise JeffError("jeff runtime gate needs an NVIDIA GPU: it measures the fast path the base is served on "
                        "(torch.cuda.is_available() is False)")
    hardware = machine()
    int4 = read_int4_base(base)
    manifests = {manifest.id: manifest for manifest, _ in adapters}
    folders = {manifest.id: folder for manifest, folder in adapters}
    tests = {adapter_id: _by_id(read_rows(manifest, "test"), f"{adapter_id}'s test set {set_uri(manifest, 'test')}")
             for adapter_id, manifest in manifests.items()}
    measuring = catalogue.replacing(manifests.values())
    log(f"Gating {quantization.format_id} against {reference} for {len(adapters)} adapters "
        f"({sum(len(rows) for rows in tests.values())} test rows) on {hardware['gpu']}")

    reference_measured = _measure(reference, manifests, folders, measuring, tests, int4=None, log=log)
    int4_measured = _measure(base, manifests, folders, measuring, tests, int4=int4, log=log)
    determinism = _determinism(quantization, reference, gated, hardware["gpu"], log=log)

    measures: dict[str, AdapterMeasures] = {}
    entries: dict[str, dict[str, Any]] = {}
    for adapter_id, manifest in manifests.items():
        key = f"{adapter_id}@{manifest.version}"
        raws = tests[adapter_id]
        # engine mode, on the rows both bases answered
        too_long = sorted(set(reference_measured.too_long[adapter_id]["engine"])
                          | set(int4_measured.too_long[adapter_id]["engine"]))
        scored = [row_id for row_id in raws if row_id not in too_long]
        outcomes: dict[str, dict[str, bool]] = {}
        ece: dict[str, float] = {}
        for name, measured in (("reference", reference_measured), ("int4", int4_measured)):
            pairs = []
            outcomes[name] = {}
            for row_id in scored:
                value, confidence = confidence_of(raws[row_id]["question"], measured.engine[adapter_id][row_id][0])
                ok = same(value, raws[row_id]["label"])
                outcomes[name][row_id] = ok
                pairs.append((confidence, ok))
            ece[name] = round(calibration_error(pairs), 6)
        engine_counts = Counts(len(scored), sum(outcomes["reference"].values()), sum(outcomes["int4"].values()))
        # template mode, on the rows both bases decided
        template_too_long = sorted(set(reference_measured.too_long[adapter_id]["template"])
                                   | set(int4_measured.too_long[adapter_id]["template"]))
        decided = [row_id for row_id in reference_measured.template[adapter_id]
                   if row_id in int4_measured.template[adapter_id]]
        functions: dict[str, dict[str, Any]] = {}
        for row_id in decided:
            function_id, ref_ok = reference_measured.template[adapter_id][row_id]
            tally = functions.setdefault(function_id, {"rows": 0, "reference_correct": 0, "int4_correct": 0})
            tally["rows"] += 1
            tally["reference_correct"] += ref_ok
            tally["int4_correct"] += int4_measured.template[adapter_id][row_id][1]
        template_counts = Counts(len(decided), sum(t["reference_correct"] for t in functions.values()),
                                 sum(t["int4_correct"] for t in functions.values()))
        for tally in functions.values():
            counts = Counts(tally["rows"], tally["reference_correct"], tally["int4_correct"])
            tally.update(reference_accuracy=_ratio(counts.reference, counts.rows),
                         int4_accuracy=_ratio(counts.int4, counts.rows), change_points=round(float(counts.change()), 4))
        # agreement: the INT4 fast path against the INT4 standard path
        engine_rows = int4_measured.engine[adapter_id]
        standard = int4_measured.standard[adapter_id]
        agreement = agreement_counts((standard[row_id], engine_rows[row_id][0]) for row_id in standard)
        disagreements = []
        for row_id, expected in standard.items():
            got = engine_rows[row_id][0]
            tied = [option for option, p in enumerate(expected) if p == max(expected)]
            if top(got) not in tied:
                ranked = sorted(expected, reverse=True)
                disagreements.append({"id": row_id, "fast_top": top(got), "standard_top": top(expected),
                                      "standard_margin": round(ranked[0] - ranked[1], 4)})
        calibration = int4_measured.calibration[adapter_id]
        misses = {function_id: entry["error"] for function_id, entry in calibration.items() if "error" in entry}
        measures[key] = AdapterMeasures(engine_counts, template_counts, agreement, misses)
        entries[key] = {
            "folder": str(folders[adapter_id]),
            "engine_mode": {"rows": engine_counts.rows, "too_long": too_long,
                            "reference_correct": engine_counts.reference, "int4_correct": engine_counts.int4,
                            "reference_accuracy": _ratio(engine_counts.reference, engine_counts.rows),
                            "int4_accuracy": _ratio(engine_counts.int4, engine_counts.rows),
                            "change_points": round(float(engine_counts.change()), 4), "ece": ece},
            "template_mode": {"rows": template_counts.rows, "too_long": template_too_long,
                              "skipped": reference_measured.skipped[adapter_id],
                              "reference_correct": template_counts.reference, "int4_correct": template_counts.int4,
                              "reference_accuracy": _ratio(template_counts.reference, template_counts.rows),
                              "int4_accuracy": _ratio(template_counts.int4, template_counts.rows),
                              "change_points": round(float(template_counts.change()), 4), "functions": functions},
            "agreement": {"fast_rows": sum(path == "fast" for _, path in engine_rows.values()),
                          "routed_to_standard": sum(path != "fast" for _, path in engine_rows.values()),
                          "compared": agreement.compared, "agreeing": agreement.agreeing,
                          "raw_agreeing": agreement.raw_agreeing,
                          "agreement": _ratio(agreement.agreeing, agreement.compared) if agreement.compared else None,
                          "exact_ties": agreement.exact_ties,
                          "largest_probability_difference": round(agreement.largest_difference, 4),
                          "disagreements": disagreements},
            "calibration": calibration}
    verdict = judge(measures, determinism["failures"])
    for key, entry in entries.items():
        entry["passed"] = not verdict.adapters[key]
        entry["failures"] = verdict.adapters[key]
    gate = {
        "format": GATE_FORMAT, "version": GATE_VERSION, "passed": verdict.passed,
        "overall_failures": verdict.overall,
        "base": {"folder": str(base), "format_id": quantization.format_id,
                 "int4_sha256": quantization.record["int4_sha256"], "method": quantization.method,
                 "source": f"{source['id']}@{source['version']}"},
        "reference": {"folder": str(reference), "weights_sha256": quantization.source_hashes},
        "limits": {"mean_change_points": float(MEAN_LIMIT), "adapter_change_points": float(ADAPTER_LIMIT),
                   "agreement": float(AGREEMENT), "agreement_rows": AGREEMENT_ROWS, "seed": SEED,
                   "rows_per_call": ROWS_PER_CALL},
        "mean_change_points": {mode: round(float(value), 4) for mode, value in verdict.mean_change.items()},
        "adapters": {key: {"passed": entries[key].pop("passed"), "failures": entries[key].pop("failures"),
                           **entries[key]} for key in entries},
        "determinism": determinism,
        "machine": hardware,
        "code": {"jeff": importlib.metadata.version("jeff-sdk"),
                 "jeff_runtime": importlib.metadata.version("jeff-runtime")},
        "date": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "seconds": {"reference": reference_measured.seconds, "int4": int4_measured.seconds,
                    "total": round(time.time() - started, 1)},
    }
    # Written whole to a temporary file, then linked into place: os.link fails if gate.json exists, so a gate is never
    # overwritten, even by another run finishing at the same time.
    descriptor, temporary = tempfile.mkstemp(dir=base, prefix=f".{GATE_FILE}.", suffix=".tmp")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(gate, indent=1) + "\n")
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise JeffError(f"{path} appeared while this gate ran (another run?); it is never overwritten. This run's "
                            f"record was not kept: move that gate.json aside and gate again") from error
    finally:
        Path(temporary).unlink(missing_ok=True)
    return gate


def summary(gate: dict[str, Any]) -> list[str]:
    """The lines `jeff runtime gate` prints: one per adapter, then the means and the verdict."""
    lines = []
    for key, entry in gate["adapters"].items():
        engine, template, agreement = entry["engine_mode"], entry["template_mode"], entry["agreement"]
        lines.append(f"{key}: engine mode {engine['reference_accuracy']:.2%} -> {engine['int4_accuracy']:.2%} "
                     f"({engine['change_points']:+.2f}), template mode {template['change_points']:+.2f}, agreement "
                     f"{agreement['agreeing']}/{agreement['compared']}"
                     + ("" if entry["passed"] else f"; FAILS: {'; '.join(entry['failures'])}"))
    means = gate["mean_change_points"]
    lines.append(f"mean change: engine mode {means['engine']:+.3f}, template mode {means['template']:+.3f} points; "
                 f"determinism {'ok' if not gate['determinism']['failures'] else 'FAILS'}")
    if gate["passed"]:
        lines.append(f"PASSED: {gate['base']['format_id']} may serve {', '.join(gate['adapters'])}")
    else:
        failing = [key for key, entry in gate["adapters"].items() if not entry["passed"]]
        lines.append(f"FAILED: {'; '.join(gate['overall_failures'] + [f'{k} fails' for k in failing])}")
    return lines
