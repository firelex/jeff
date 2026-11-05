"""The jeff command: test, calibrate, gen, runtime serve, runtime quantize, runtime gate, runtime health, mcp."""

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any
from pathlib import Path

from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.engine import check_capacity, format_id_of
from jeff.errors import JeffError, ManifestError, RuntimeMissing, Unauthorised
from jeff.evaluate import (BATCH, Report, calibration_set, correct, engine_score, mine_rows, mirror_read, published_set,
                           resolve, run, score, template_rows)
from jeff.gate import Int4Identity
from jeff.gen import python_module
from jeff.ids import parse_ref
from jeff.local import adapter_folder, local_for_batches, resolve_base
from jeff.manifest import Manifest

if TYPE_CHECKING:
    from jeff_runtime.engine.types import DecisionInput


def make_jeff(adapters: list[str], backend: str, base: Path | None = None, catalogue: Catalogue | None = None,
              paths: dict[str, Path] | None = None) -> Jeff:
    """The local Jeff jeff test and jeff calibrate run on: they hand the engine at most jeff.evaluate.BATCH rows at
    once, which sizes the fast backend's prefix pool (local_for_batches). base: a base folder to load instead of the
    adapters' base from the catalogue (jeff calibrate --base, an INT4 base made from it). catalogue and paths: the
    catalogue to read (default: the shipped one) and adapter weights folders by ID (jeff calibrate on an adapter
    folder)."""
    return local_for_batches(adapters, rows_per_pass=BATCH, backend=backend, base=base, catalogue=catalogue,
                             paths=paths)


def _print(reports: list[Report], as_json: bool) -> None:
    for report in reports:
        if as_json:
            print(json.dumps(report.__dict__))
        else:
            low, high = report.interval
            print(f"{report.function}: {report.accuracy:.1%} ({report.correct}/{report.n}, 95% {low:.1%}-{high:.1%}), "
                  f"abstained {report.abstained}, p50 {report.p50_ms:.1f} ms")


def _published(jeff: Jeff, refs: list[str], args: argparse.Namespace) -> int:
    for ref in refs:
        manifest = jeff.catalogue.adapter(ref)
        key = parse_ref(ref).key
        if key is not None and args.engine_rows:
            raise JeffError(f"--engine-rows scores whole adapters (the rows as written); name {manifest.id}, not {ref}")
        chosen = published_set(manifest, "test", args.sample)
        if args.engine_rows:
            _print([engine_score(jeff.engine, manifest.id, chosen.raw)], args.json)
            continue
        function = jeff.catalogue.function(ref).id if key is not None else None
        rows = [row for row in chosen.rows if function is None or row.function == function]
        _print(score(jeff, rows), args.json)
        if chosen.skipped:
            skipped = ", ".join(f"{count} {reason}" for reason, count in sorted(chosen.skipped.items()))
            print(f"{manifest.id}: skipped {sum(chosen.skipped.values())} rows the manifest does not describe "
                  f"({skipped})", file=sys.stderr)
    return 0


def cmd_test(args: argparse.Namespace) -> int:
    catalogue = Catalogue.shipped()
    adapters = sorted({catalogue.adapter(ref).id for ref in args.functions})
    jeff = make_jeff(adapters, args.backend)
    if not args.mine:
        return _published(jeff, args.functions, args)
    rows = mine_rows(Path(args.mine))
    results = {row.id: correct(row, decision) for row, decision, _ in run(jeff, rows)}
    versions = {ref: jeff.catalogue.adapter(ref).version for ref in adapters}
    failed = sorted(case for case, ok in results.items() if not ok)
    print(f"{len(results) - len(failed)}/{len(results)} cases pass" + (f"; failing: {', '.join(failed)}" if failed else ""))
    if args.save_baseline:
        Path(args.save_baseline).write_text(json.dumps({"versions": versions, "cases": results}, indent=2))
    if not args.against:
        return 1 if failed else 0
    # Against a baseline the run fails only on regressions and on baseline cases missing from this run; a case that
    # already failed in the baseline and still fails is reported, not counted.
    baseline = json.loads(Path(args.against).read_text())
    changed = {k: (baseline["versions"].get(k), v) for k, v in versions.items() if baseline["versions"].get(k) != v}
    missing = sorted(case for case in baseline["cases"] if case not in results)
    if missing:
        print(f"The baseline {args.against} has cases that {args.mine} does not: {', '.join(missing)}; "
              f"compare the same cases", file=sys.stderr)
        return 1
    if changed:
        print("Version changes: " + ", ".join(f"{k} {a} -> {b}" for k, (a, b) in changed.items()))
    still_failing = sorted(case for case in failed if case in baseline["cases"] and not baseline["cases"][case])
    if still_failing:
        print(f"Still failing (already failed in the baseline, not a regression): {', '.join(still_failing)}")
    new_failures = sorted(case for case in failed if case not in baseline["cases"])
    regressed = sorted(case for case, ok in baseline["cases"].items() if ok and not results[case])
    if regressed:
        print(f"Regressions against {args.against}: {', '.join(regressed)}")
    if new_failures:
        print(f"Failing cases the baseline does not have: {', '.join(new_failures)}")
    return 1 if regressed or new_failures else 0


def _repo_catalogue_root() -> Path:
    """The repository's catalogue/ directory (not the shipped copy), when jeff runs from a checkout: `jeff calibrate`
    writes into it directly. A module-level function, not an inline expression, so tests can monkeypatch it."""
    return Path(__file__).resolve().parents[4] / "catalogue"


def _calibration_catalogue(given: str | None) -> Path:
    from jeff.errors import CalibrationError

    if given is not None:
        return Path(given)
    root = _repo_catalogue_root()
    if not (root / "bases.yaml").is_file():
        raise CalibrationError(f"jeff is not running from a repository checkout ({root} is not a catalogue); pass "
                               "--catalogue PATH, the catalogue folder whose manifests calibration writes to")
    return root


def cmd_calibrate(args: argparse.Namespace) -> int:
    """Fits each chosen function's threshold and writes it to the adapter's manifest: its `calibration` block on the
    bfloat16 base, or with --base <INT4 base folder> its calibration_by_format entry for that base's format_id, fitted
    on the calibration set minus the rows the quantiser saw (listed in the folder's quantization.json). Either refuses
    a function that already has what it would write. The adapter is a catalogue ID, alias or function (the manifest
    written is the catalogue's), or an adapter folder (a path with a '/', as JEFF_ADAPTER_DIRS serves; every function,
    written into the folder's own manifest.yaml, the weights loaded from the folder)."""
    import random

    from jeff.calibrate import (MeasuringJeff, block, excluded_reason, format_block, quantiser_rows_of,
                                read_int4_base, write_block, write_format_block)
    from jeff.errors import CalibrationError

    engine_catalogue: Catalogue | None = None  # the catalogue the engine reads (None: the shipped one)
    paths: dict[str, Path] | None = None
    if "/" in args.adapter:
        manifest, folder = _adapter_folder(args.adapter, "jeff calibrate")
        given = Catalogue.from_dir(Path(args.catalogue)) if args.catalogue else Catalogue.shipped()
        catalogue = engine_catalogue = given.replacing([manifest])
        paths = {manifest.id: folder}
        whole_adapter = True
    else:
        catalogue = Catalogue.from_dir(_calibration_catalogue(args.catalogue))
        manifest = catalogue.adapter(args.adapter)
        whole_adapter = parse_ref(args.adapter).key is None
    chosen = list(manifest.functions.values()) if whole_adapter else [catalogue.function(args.adapter)]
    int4 = read_int4_base(Path(args.base)) if args.base is not None else None
    if int4 is None:
        calibrated = [function.key for function in chosen if function.calibration is not None]
        if calibrated:
            raise CalibrationError(f"{manifest.id} is already calibrated ({', '.join(calibrated)}); set its "
                                   "calibration blocks to null in the manifest to refit, or name one uncalibrated "
                                   "function")
    else:
        uncalibrated = [function.key for function in chosen if function.calibration is None]
        if uncalibrated and not whole_adapter:
            raise CalibrationError(f"{chosen[0].id} has calibration null: a function uncalibrated on the bfloat16 "
                                   "base stays uncalibrated in every format; calibrate it without --base first")
        if uncalibrated:
            print(f"{manifest.id}: {', '.join(uncalibrated)} uncalibrated on the bfloat16 base, so uncalibrated on "
                  f"{int4.format_id} too; not calibrated", file=sys.stderr)
        chosen = [function for function in chosen if function.calibration is not None]
        if not chosen:
            raise CalibrationError(f"{manifest.id} has no function calibrated on the bfloat16 base; calibrate it "
                                   "without --base first")
        done = [function.key for function in chosen if int4.format_id in function.calibration_by_format]
        if done:
            raise CalibrationError(f"{manifest.id} is already calibrated on {int4.format_id} ({', '.join(done)}); "
                                   "remove those calibration_by_format entries in the manifest to refit, or name one "
                                   "function not calibrated on it")
        other = [f"{function.key} ({function.calibration.target_precision})" for function in chosen
                 if function.calibration is not None and function.calibration.target_precision != args.target_precision]
        if other:
            raise CalibrationError(f"--target-precision {args.target_precision} differs from the bfloat16 "
                                   f"calibration's target of {', '.join(other)}; a function keeps one target "
                                   "precision in every format")
    chosen_ids = {function.id for function in chosen}

    calibration_uri = calibration_set(manifest)
    split = False
    if args.data:
        uri = args.data
    elif calibration_uri:
        uri = calibration_uri
    elif args.split_test:
        uri = manifest.tests.get("published") or manifest.tests.get("internal")
        if uri is None:
            raise CalibrationError(f"{manifest.id} has no test set to split either: tests.published and "
                                   "tests.internal are both empty")
        split = True
    else:
        raise CalibrationError(f"{manifest.id} has no calibration set; pass --data or --split-test")

    raws = [json.loads(line) for line in resolve(uri).read_text(encoding="utf-8").split("\n") if line.strip()]
    if not raws:
        raise CalibrationError(f"{uri} has no rows")
    read_from = mirror_read(uri)
    source = uri
    if split:
        shuffled = list(raws)
        random.Random(0).shuffle(shuffled)
        raws = shuffled[: len(shuffled) // 2]
        source = f"{uri} (calibration half, seed 0)"
        print(f"Calibrating {manifest.id} on half of its test set (seed 0, {len(raws)} rows); "
              "report accuracy with `jeff test` on the other half only.")

    quantiser_rows: set[str] = set()
    recorded = False  # whether the quantiser saw any rows of this adapter (quantization.json names it)
    if int4 is not None:
        quantiser_rows, recorded = quantiser_rows_of(int4, manifest, raws, uri, published=uri == calibration_uri,
                                                     path=resolve(uri))

    rows, skipped = template_rows(manifest, raws)
    rows = [row for row in rows if row.function in chosen_ids]
    if skipped:
        reasons = ", ".join(f"{count} {reason}" for reason, count in sorted(skipped.items()))
        print(f"{manifest.id}: skipped {sum(skipped.values())} rows the manifest does not describe ({reasons})",
              file=sys.stderr)
    excluded: dict[str, int] = {}
    for row in rows:
        if str(row.id) in quantiser_rows:
            excluded[row.function] = excluded.get(row.function, 0) + 1
    rows = [row for row in rows if str(row.id) not in quantiser_rows]
    if not rows:
        raise CalibrationError(f"{manifest.id}: no rows the manifest describes to calibrate on")

    engine = make_jeff([manifest.id], args.backend, base=int4.folder if int4 is not None else None,
                       catalogue=engine_catalogue, paths=paths).engine
    if int4 is not None and format_id_of(engine.base_format) != int4.format_id:
        raise CalibrationError(f"The engine loaded from {int4.folder} serves {engine.base_format}, not "
                               f"{int4.format_id} as its decision_config.json names")
    jeff = MeasuringJeff(engine, catalogue)
    by_function: dict[str, list[tuple[float, bool]]] = {}
    for row, decision, _ in run(jeff, rows):
        if decision.confidence is None:
            raise CalibrationError(f"{row.function} returned a decision without a confidence for row {row.id}; "
                                   "calibration needs the model's confidence for every row")
        by_function.setdefault(row.function, []).append((decision.confidence, correct(row, decision)))

    failed: list[str] = []
    for function_id, pairs in sorted(by_function.items()):
        key = function_id.rsplit(".", 1)[1]
        try:
            if int4 is None:
                fitted = block(pairs, args.target_precision, source, data_mirror=read_from)
            else:
                reason = excluded_reason(excluded.get(function_id, 0), recorded)
                fitted = format_block(pairs, args.target_precision, source, reason, data_mirror=read_from)
        except CalibrationError as error:
            print(f"{function_id}: {error}", file=sys.stderr)
            failed.append(function_id)
            continue
        if int4 is None:
            write_block(manifest.path, key, fitted)
        else:
            write_format_block(manifest.path, key, int4.format_id, fitted)
        on = "" if int4 is None else f" on {int4.format_id}"
        print(f"{function_id}{on}: threshold {fitted['threshold']:.3f}, precision "
              f"{fitted['precision_at_threshold']:.1%}, coverage {fitted['coverage_at_threshold']:.1%} "
              f"({len(pairs)} rows)")
    return 1 if failed else 0


def cmd_gen(args: argparse.Namespace) -> int:
    source = python_module(Catalogue.shipped(), args.refs)
    Path(args.out).write_text(source)
    print(f"Wrote {args.out}")
    return 0


def _path_pair(value: str) -> tuple[str, str]:
    key, sep, folder = value.partition("=")
    if not sep or not key or not folder:
        raise argparse.ArgumentTypeError(f"--path needs ID=FOLDER, with both sides non-empty, got {value!r}")
    return key, folder


def _rows_pair(value: str) -> tuple[str, str]:
    adapter, sep, path = value.partition("=")
    if not sep or not adapter or not path:
        raise argparse.ArgumentTypeError(f"--calibrate-on-rows needs ID|FOLDER=JSONL, with both sides non-empty, got "
                                         f"{value!r}")
    return adapter, path


def _base_pair(value: str) -> tuple[str, str]:
    base_id, sep, version = value.partition("@")
    if not sep or not base_id or not version:
        raise argparse.ArgumentTypeError(f"--base-version needs ID@VERSION (for example jeff-base@v1.3), got {value!r}")
    return base_id, version


def _bases_text(bases: list[tuple[str, str]]) -> str:
    return ", ".join(f"{base_id}@{version}" for base_id, version in bases)


def _starting_adapters(args: argparse.Namespace, served_file: Path | None) -> list[str]:
    """The adapters the engine starts with: the served file's list when the file exists (it is the source of truth
    once written), else --adapter. An empty list (no adapters from either) is fine only with --base-version: the
    engine then serves only the base. ManifestError otherwise, or for --adapter given with an existing served file."""
    from jeff_runtime.engine_server import read_served

    if served_file is not None and served_file.exists():
        if args.adapter:
            raise ManifestError(f"two sources for the starting adapters: --adapter and the served file {served_file}, "
                            "which already exists; leave out --adapter (the served file is what the engine served "
                            "last), or remove the file to start from --adapter")
        listed = read_served(served_file)
        if not listed and args.base_version is None:
            raise ManifestError(f"the served file {served_file} lists no adapters, so the base to load is unknown; "
                                "give --base-version ID@VERSION (for example jeff-base@v1.3), or --adapter after "
                                "removing the file")
        return listed
    if args.adapter:
        return list(args.adapter)
    if args.base_version is None:
        raise ManifestError("jeff runtime serve needs the adapters to start with: --adapter ID (repeatable), or "
                        "--served-file naming an existing served file, or --base-version ID@VERSION to start with "
                        "no adapters on that base")
    return []


def _int4_base(folder: Path, base: tuple[str, str]) -> Int4Identity:
    """--base: an INT4 base folder made from `base` (id, version), the base the adapters are trained on. ManifestError
    for a folder that is not an INT4 base, or one made from another base."""
    from jeff.gate import int4_identity

    if not (folder / "decision_config.json").is_file():
        raise ManifestError(f"--base {folder} is not a base folder (no decision_config.json); give an INT4 base "
                            "folder, as jeff runtime quantize writes it")
    identity = int4_identity(folder)
    if identity is None:
        raise ManifestError(f"--base {folder} is a bfloat16 base; the bfloat16 base is the catalogue's "
                            f"({base[0]}@{base[1]}, downloaded). --base serves an INT4 base made from it")
    if identity.source != base:
        raise ManifestError(f"--base {folder} is an INT4 base of {identity.source[0]}@{identity.source[1]}, but the "
                            f"adapters are trained on {base[0]}@{base[1]}")
    return identity


def cmd_runtime_serve(args: argparse.Namespace) -> int:
    import asyncio
    import logging
    import signal

    try:
        from jeff_runtime import LocalEngine, select_backend
        from jeff_runtime.engine_server import EngineServer
        from jeff_runtime.local_engine import check_max_adapters, check_prefix_pool, check_prepare_workers
        from jeff_runtime.scheduler import Scheduler, Settings
    except ImportError as error:
        raise RuntimeMissing("jeff runtime serve needs the engine: pip install 'jeff-runtime[mac]' (Apple "
                             "silicon) or 'jeff-runtime[cuda]' (NVIDIA)") from error
    from jeff.adapter_dirs import check_base, scan
    from jeff.gate import GatedBase, check_served

    # checked before anything is downloaded or loaded: a bad setting fails at once, naming the setting
    settings = Settings(window_ms=args.batch_window_ms, max_batch_rows=args.max_batch_rows,
                        queue_limit_rows=args.queue_limit_rows)
    check_prepare_workers(args.prepare_workers)
    if args.backend != "auto":  # checked before select_backend looks for a GPU or mlx
        check_max_adapters(args.backend, args.max_adapters)
        check_prefix_pool(args.backend, args.prefix_pool_mb, args.max_batch_rows)
    backend = select_backend(args.backend)
    if args.backend == "auto":  # now that the backend is known
        check_max_adapters(backend, args.max_adapters)
        check_prefix_pool(backend, args.prefix_pool_mb, args.max_batch_rows)
    catalogue = Catalogue.from_dir(Path(args.catalogue)) if args.catalogue else Catalogue.shipped()
    served_file = Path(args.served_file) if args.served_file else None
    starting = _starting_adapters(args, served_file)
    roots = [Path(folder) for folder in args.adapters_dir]
    found = scan(roots)
    found_names = {name: item.id for item in found.values() for name in (item.id, *item.manifest.aliases)}
    for item in found.values():
        for alias in item.manifest.aliases:
            if alias in catalogue.names() and catalogue.adapter(alias).id != item.id:
                raise ManifestError(f"the alias {alias} of {item.id} ({item.folder}) is already the catalogue's name "
                                    f"for {catalogue.adapter(alias).id}; give the adapter its own aliases")

    # --path folders, by adapter ID: always available to `serve`, whether or not the engine starts with them
    paths: dict[str, Path] = {}
    for key, folder in args.path:
        if key in found_names:
            adapter_id = found_names[key]
            raise ManifestError(f"--path names {adapter_id}, which is also in an adapters dir "
                                f"({found[adapter_id].folder}); serve each adapter from one place")
        adapter_id = catalogue.adapter(key).id
        if adapter_id in paths:
            raise ManifestError(f"--path names {adapter_id} twice")
        paths[adapter_id] = Path(folder)

    # A starting adapter found in an adapters dir is served from its folder there (the server's own copy), even
    # when the catalogue has it too; any other comes from the catalogue, with its --path folder or its open weights.
    starting_ids = [found_names[ref] if ref in found_names else catalogue.adapter(ref).id for ref in starting]
    check_capacity(args.max_adapters, len(set(starting_ids)))  # an adapter named by its ID and an alias is one
    from_catalogue = {adapter_id: catalogue.adapter(adapter_id) for adapter_id in [*starting_ids, *paths]
                      if adapter_id not in found}
    bases = {(manifest.base_id, manifest.base_version) for manifest in from_catalogue.values()}
    bases |= {(found[adapter_id].manifest.base_id, found[adapter_id].manifest.base_version)
              for adapter_id in starting_ids if adapter_id in found}
    if args.base_version is not None:
        given = tuple(args.base_version)
        others = sorted(base for base in bases if base != given)
        if others:
            raise ManifestError(f"--base-version {given[0]}@{given[1]} does not match the base the adapters are "
                                f"trained on ({_bases_text(others)})")
        bases = {given}
    if len(bases) > 1:
        raise ManifestError(f"The adapters are trained on different bases ({_bases_text(sorted(bases))}); load one "
                            "base at a time")
    base_id, base_version = bases.pop()
    check_base(found, (base_id, base_version))  # every found adapter, not only the starting ones

    # An INT4 base (--base) is served only for adapters its gate passed: checked here, before anything is downloaded
    # or loaded, and by the engine server on every serve and reload.
    gated = None
    if args.base is not None:
        identity = _int4_base(Path(args.base).expanduser(), (base_id, base_version))
        fixed_versions = {adapter_id: catalogue.adapter(adapter_id).version
                          for adapter_id in [*starting_ids, *paths] if adapter_id not in found}
        check_served(identity, {adapter_id: found[adapter_id].manifest.version if adapter_id in found
                                else fixed_versions[adapter_id] for adapter_id in starting_ids})
        gated = GatedBase(identity, fixed_versions)
    base = gated.identity.folder if gated is not None else resolve_base(catalogue, base_id, base_version)
    fixed = dict(paths)
    for adapter_id in starting_ids:
        if adapter_id not in found:
            fixed[adapter_id] = adapter_folder(from_catalogue[adapter_id], paths.get(adapter_id))
    folders = {adapter_id: found[adapter_id].folder if adapter_id in found else fixed[adapter_id]
               for adapter_id in starting_ids}
    engine_kwargs: dict[str, int] = {} if args.max_tokens is None else {"max_tokens": args.max_tokens}
    if args.max_adapters is not None:
        engine_kwargs["max_adapters"] = args.max_adapters
    if args.prefix_pool_mb is not None:
        engine_kwargs["prefix_pool_mb"] = args.prefix_pool_mb
    engine = LocalEngine(base, folders, backend=backend, prepare_workers=args.prepare_workers, **engine_kwargs)
    scheduler = Scheduler(engine, settings)
    scheduler.start()
    try:
        server = EngineServer(engine, scheduler, Path(args.socket), roots, fixed, served_file,
                              (base_id, base_version), pinned=set(paths), found=found,
                              prepare_workers=args.prepare_workers, gate=gated)
    except BaseException:
        scheduler.stop()
        raise

    async def serve() -> None:
        # SIGINT/SIGTERM are handled from inside the loop, not by letting a bare KeyboardInterrupt propagate out
        # of asyncio.run(): that would leave the loop closed by the time the scheduler is stopped, and every
        # request still queued would then try to deliver its result through a closed loop, logging one traceback
        # each (Scheduler._deliver catches the delivery failure and logs it). Stopping the scheduler here, before
        # this coroutine returns and asyncio.run() closes the loop, means every pending delivery still has a live
        # loop to call back into.
        loop = asyncio.get_running_loop()
        stopping = asyncio.Event()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stopping.set)
        run_task = asyncio.create_task(server.run())
        stop_wait = asyncio.create_task(stopping.wait())
        broken_wait = asyncio.create_task(server.wait_broken())
        # Whichever finishes first: a stop signal, run_task failing outright (e.g. another engine already serving
        # this socket), or the GPU thread dying. Either way, close and stop before this coroutine returns, so a
        # failure is reported instead of leaving the process hanging for a signal that will never come.
        await asyncio.wait({run_task, stop_wait, broken_wait}, return_when=asyncio.FIRST_COMPLETED)
        stop_wait.cancel()
        broken = broken_wait.result() if broken_wait.done() else None
        broken_wait.cancel()
        if broken is not None:
            logging.getLogger("jeff.engine").error("The GPU thread failed; closing the engine so it can be "
                                                   "restarted: %s", broken)
        await server.close()
        scheduler.stop()
        await run_task
        if broken is not None:
            raise broken  # main() prints it and exits with status 1

    asyncio.run(serve())
    return 0


def _jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def _jsonl_numbered(path: Path) -> tuple[list[Any], list[int]]:
    """The values of a JSONL file's non-blank lines and their line numbers; JeffError naming the file and line for a
    line that is not JSON."""
    values: list[Any] = []
    numbers: list[int] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            values.append(json.loads(line))
        except json.JSONDecodeError as error:
            raise JeffError(f"{path} line {number} is not valid JSON: {error}") from error
        numbers.append(number)
    return values, numbers


def _adapter_folder(ref: str, flag: str) -> tuple[Manifest, Path]:
    """An adapter folder's manifest and the folder (`flag`: the option or command that named it, for messages): it
    holds manifest.yaml and adapter_config.json, as --adapters-dir and JEFF_ADAPTER_DIRS find adapters."""
    from jeff.manifest import load_manifest

    folder = Path(ref).expanduser()
    if not (folder / "manifest.yaml").is_file() or not (folder / "adapter_config.json").is_file():
        raise ManifestError(f"{flag} {ref}: an adapter folder holds manifest.yaml and adapter_config.json; this one "
                            "does not")
    return load_manifest(folder / "manifest.yaml"), folder


def _named_adapters(refs: list[str], path_pairs: list[tuple[str, str]], catalogue: Catalogue,
                    flag: str) -> list[tuple[Manifest, Path]]:
    """Each named adapter's manifest and weights folder (`flag`: the option that named them, for messages). A value
    with a '/' is an adapter folder (its manifest.yaml and weights, as --adapters-dir finds them); any other value is
    a catalogue ID or alias, whose weights are its --path folder or its open weights, downloaded. Sorted by the name
    an adapter's quantiser rows are drawn with (the last part of its ID)."""
    from jeff_runtime.engine.quantize import short_name

    chosen: dict[str, tuple[Manifest, Path]] = {}
    paths: dict[str, Path] = {}
    for key, given in path_pairs:
        adapter_id = catalogue.adapter(key).id
        if adapter_id in paths:
            raise ManifestError(f"--path names {adapter_id} twice")
        paths[adapter_id] = Path(given)
    for ref in refs:
        if "/" in ref:
            manifest, folder = _adapter_folder(ref, flag)
            if manifest.id in paths:
                raise ManifestError(f"--path names {manifest.id}, which {flag} {ref} already gives a folder")
        else:
            manifest = catalogue.adapter(ref)
            folder = adapter_folder(manifest, paths.get(manifest.id))
        if manifest.id in chosen:
            raise ManifestError(f"{flag} names {manifest.id} twice")
        chosen[manifest.id] = (manifest, folder)
    unused = sorted(set(paths) - set(chosen))
    if unused:
        raise ManifestError(f"--path names {', '.join(unused)}, which {flag} does not")
    bases = sorted({(manifest.base_id, manifest.base_version) for manifest, _ in chosen.values()})
    if len(bases) != 1:
        raise ManifestError(f"The adapters are trained on different bases ({_bases_text(bases)}); an INT4 base is "
                            "made from one source base, and so serves adapters of that base only")
    return sorted(chosen.values(), key=lambda item: (short_name(item[0].id), item[0].id))


def quantiser_calibration(manifests: list[Manifest], rows_per_adapter: int,
                          training: Mapping[str, Path] | None = None
                          ) -> "tuple[list[tuple[str, DecisionInput]], dict[str, Any]]":
    """The quantiser's calibration rows ((adapter id, row) in drawn order) and the `calibration` record
    quantization.json keeps of them: rows_per_adapter rows of each adapter's calibration set (RULE), none in its test
    set. The record names each set by its URI as the manifest gives it (data, not_in_test_sets[*].data); a set read
    from a JEFF_DATA_MIRROR copy also gets data_mirror (calibration.data_mirror[adapter], not_in_test_sets[adapter].
    data_mirror): the local file and its SHA-256 (jeff.evaluate.mirror_read). Without a mirror the record has no
    data_mirror at all.

    With `training` (adapter id -> a JSONL file of its training rows, one for every manifest), the rows are drawn
    from those files instead (TRAINING_RULE), and none may be in the adapter's test set or its calibration set. The
    record's data then names each training file, training_rows gives its path and SHA-256, and
    not_in_calibration_sets names each calibration set checked against."""
    import hashlib

    from jeff_runtime.engine.quantize import (ARITHMETIC, RULE, TRAINING_RULE, check_not_calibration_rows,
                                              check_not_test_rows, quantiser_rows, short_name, training_rows)
    from jeff_runtime.engine.types import DecisionInput

    from jeff.errors import EngineError
    from jeff.evaluate import set_uri

    rows: list[tuple[str, DecisionInput]] = []
    row_ids: dict[str, list[str]] = {}
    data: dict[str, str] = {}
    data_mirror: dict[str, dict[str, str]] = {}
    test_sets: dict[str, dict[str, Any]] = {}
    training_files: dict[str, dict[str, str]] = {}
    calibration_sets: dict[str, dict[str, Any]] = {}
    if training is not None:
        named = {manifest.id for manifest in manifests}
        missing = sorted(named - set(training))
        extra = sorted(set(training) - named)
        if missing or extra:
            raise JeffError("Training rows must be named for exactly the adapters calibrated on"
                            + (f"; none named for {', '.join(missing)}" if missing else "")
                            + (f"; named for {', '.join(extra)}, which is not calibrated on" if extra else ""))

    def sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    for manifest in manifests:
        uri, test_uri = set_uri(manifest, "calibration"), set_uri(manifest, "test")
        if uri == test_uri:
            raise JeffError(f"{manifest.id}: its calibration set and its test set are the same file ({uri}); the "
                            "quantiser never uses test rows")
        path, test_path = resolve(uri), resolve(test_uri)
        if path.resolve() == test_path.resolve():
            raise JeffError(f"{manifest.id}: its calibration set {uri} and its test set {test_uri} are the same file; "
                            "the quantiser never uses test rows")
        key = f"{manifest.id}@{manifest.version}"
        read, test_read = mirror_read(uri), mirror_read(test_uri)
        if training is None:
            drawn = quantiser_rows(short_name(manifest.id), _jsonl(path), rows_per_adapter)
            check_not_test_rows(manifest.id, drawn, _jsonl(test_path))
            data[key] = uri
            if read is not None:
                data_mirror[key] = read
        else:
            given = Path(training[manifest.id]).expanduser()
            if not given.is_file():
                raise JeffError(f"{manifest.id}: its training rows file {given} does not exist")
            train_path = given.resolve()
            if train_path in (path.resolve(), test_path.resolve()):
                raise JeffError(f"{manifest.id}: its training rows file {train_path} is its calibration or test set")
            try:
                train_raws, lines = _jsonl_numbered(train_path)
                drawn = training_rows(short_name(manifest.id), train_raws, rows_per_adapter, source=str(train_path),
                                      lines=lines)
            except EngineError as error:
                raise JeffError(f"{manifest.id} ({train_path}): {error}") from error
            check_not_test_rows(manifest.id, drawn, _jsonl(test_path), training=True)
            check_not_calibration_rows(manifest.id, drawn, _jsonl(path))
            data[key] = str(train_path)
            training_files[key] = {"path": str(train_path), "sha256": sha256(train_path)}
            calibration_sets[key] = {"data": uri, "sha256": sha256(path),
                                     **({} if read is None else {"data_mirror": read})}
        row_ids[key] = [row["id"] for row in drawn]
        test_sets[key] = {"data": test_uri, "sha256": sha256(test_path),
                          **({} if test_read is None else {"data_mirror": test_read})}
        rows.extend((manifest.id, {"state": row["state"], "question": row["question"]}) for row in drawn)
    calibration: dict[str, Any] = {"rows_per_adapter": rows_per_adapter,
                                   "rule": RULE if training is None else TRAINING_RULE, "rows": row_ids, "data": data,
                                   **({"data_mirror": data_mirror} if data_mirror else {}),
                                   **({"training_rows": training_files} if training is not None else {}),
                                   "not_in_test_sets": test_sets,
                                   **({"not_in_calibration_sets": calibration_sets} if training is not None else {}),
                                   "arithmetic": ARITHMETIC}
    return rows, calibration


def _training_files(pairs: list[tuple[str, str]], catalogue: Catalogue) -> dict[str, Path] | None:
    """--calibrate-on-rows ADAPTER=FILE pairs -> adapter id -> training rows file (None when none is given). ADAPTER
    is an adapter folder (a path with a '/') or a catalogue ID or alias. Each adapter is named once."""
    if not pairs:
        return None
    files: dict[str, Path] = {}
    for ref, given in pairs:
        adapter_id = _adapter_folder(ref, "--calibrate-on-rows")[0].id if "/" in ref else catalogue.adapter(ref).id
        if adapter_id in files:
            raise ManifestError(f"--calibrate-on-rows names {adapter_id} twice")
        files[adapter_id] = Path(given).expanduser().resolve()
    return files


def cmd_runtime_quantize(args: argparse.Namespace) -> int:
    """Writes the INT4 base of a bfloat16 base (step 2b-2 design, Section 4.2), quantised with GPTQ on the named
    adapters' calibration rows. Every check that needs no GPU runs before the model loads."""
    import hashlib
    import importlib.metadata
    import time

    try:
        import torch

        from jeff_runtime.engine.int4 import read_quantization, write_int4_base
        from jeff_runtime.engine.quantize import GPTQ_SETTINGS, quantize_gptq
    except ImportError as error:
        raise RuntimeMissing("jeff runtime quantize needs the engine: pip install 'jeff-runtime[cuda]' (NVIDIA)") \
            from error

    out, base = Path(args.out).expanduser(), Path(args.base).expanduser()
    if out.exists():
        raise JeffError(f"--out {out} already exists; the INT4 base is written into a new folder")
    if not out.parent.is_dir():
        raise JeffError(f"--out {out}: its parent folder {out.parent} does not exist")
    if not (base / "decision_config.json").is_file():
        raise JeffError(f"--base {base} is not a base folder (no decision_config.json)")
    if out.resolve().is_relative_to(base.resolve()):
        raise JeffError(f"--out {out} is inside --base {base}; write the INT4 base beside its source, not into it")
    if read_quantization(base) is not None:
        raise JeffError(f"--base {base} is already an INT4 base; quantise its bfloat16 source base")
    if args.rows_per_adapter < 1:
        raise JeffError(f"--rows-per-adapter must be at least 1; got {args.rows_per_adapter}")
    if not torch.cuda.is_available():  # before any adapter or data is downloaded
        raise JeffError("jeff runtime quantize needs an NVIDIA GPU: it calibrates in the CUDA standard path's "
                        "bfloat16 arithmetic (torch.cuda.is_available() is False)")
    catalogue = Catalogue.from_dir(Path(args.catalogue)) if args.catalogue else Catalogue.shipped()
    adapters = _named_adapters(args.calibrate_on, args.path, catalogue, "--calibrate-on")

    training = _training_files(args.calibrate_on_rows, catalogue)
    rows, calibration = quantiser_calibration([manifest for manifest, _ in adapters], args.rows_per_adapter,
                                              training)
    kind = "calibration rows" if training is None else "training rows"
    kept = "their test sets" if training is None else "their test or calibration sets"
    print(f"Quantising {base} with GPTQ on {len(rows)} {kind} ({args.rows_per_adapter} each from "
          f"{', '.join(manifest.id for manifest, _ in adapters)}; none in {kept})", flush=True)

    started = time.time()
    matrices = quantize_gptq(base, {manifest.id: folder for manifest, folder in adapters}, rows,
                             log=lambda line: print(line, flush=True))
    peak = torch.cuda.max_memory_allocated()
    tool = {"jeff_runtime": importlib.metadata.version("jeff-runtime"), "torch": torch.__version__,
            "gpu": torch.cuda.get_device_name(), "seconds": round(time.time() - started, 1),
            "peak_gpu_memory_bytes": peak}
    base_id, base_version = adapters[0][0].base_id, adapters[0][0].base_version
    written = write_int4_base(base, out, matrices, method=args.method, source_id=base_id, source_version=base_version,
                              calibration=calibration, gptq=GPTQ_SETTINGS, tool=tool)
    print(f"Wrote {out}: {written.format_id}, {len(matrices)} matrices, {tool['seconds']} s, peak GPU memory "
          f"{peak / 2**30:.2f} GiB. Nothing serves it until it passes its gate.")
    return 0


def cmd_runtime_gate(args: argparse.Namespace) -> int:
    """Gates an INT4 base (step 2b-2 design, Section 6) against the bfloat16 base it was quantised from, for the
    named adapters, and writes gate.json into the INT4 folder (jeff.gate_run). Exit 0 only when the gate passes;
    `jeff runtime serve` refuses the base, and any adapter, that the gate does not record as passing."""
    try:
        import jeff_runtime  # noqa: F401  (the gate runs engines)

        from jeff.gate_run import run_gate, summary
    except ImportError as error:
        raise RuntimeMissing("jeff runtime gate needs the engine: pip install 'jeff-runtime[cuda]' (NVIDIA)") \
            from error

    catalogue = Catalogue.from_dir(Path(args.catalogue)) if args.catalogue else Catalogue.shipped()
    adapters = _named_adapters(args.adapters, args.path, catalogue, "--adapters")
    gate = run_gate(Path(args.base).expanduser(), Path(args.reference).expanduser(), adapters, catalogue,
                    log=lambda line: print(line, flush=True))
    for line in summary(gate):
        print(line)
    return 0 if gate["passed"] else 1


def cmd_runtime_health(args: argparse.Namespace) -> int:
    """Asks the engine at --socket for its health once and prints the reply. Exit 0 only when it answers "ready":
    deploy/run.sh polls this until each engine it started answers, before starting the API in front of them."""
    from jeff import socket_engine
    from jeff.errors import EngineError

    engine = socket_engine.SocketEngine(args.socket, timeout_s=args.timeout_s)
    try:
        health = engine.health()
    finally:
        engine.close()
    if health["status"] == "broken":
        raise EngineError(f"The engine at {args.socket} is broken: {health['cause']}")
    if health["status"] != "ready":
        raise EngineError(f"The engine at {args.socket} answered status {health['status']!r}, not 'ready'")
    print(json.dumps(health))
    return 0


def is_mcp_sdk(module: str | None) -> bool:
    """Whether a missing module is the MCP SDK (mcp, mcp_types or a submodule of either)."""
    return module is not None and module.split(".")[0] in ("mcp", "mcp_types")


def env_token(name: str) -> Callable[[], str]:
    """A token callable reading the environment variable `name` each time it is called (remote mode calls it before
    every request), so a token refreshed in the variable is picked up; Unauthorised naming the variable when it is
    unset at that moment."""
    import os

    def token() -> str:
        if name not in os.environ:
            raise Unauthorised(f"--token-env {name}: the environment variable {name} is not set; set it to the "
                               "server's bearer token (an API key or a hub-signed token)")
        return os.environ[name]

    return token


def cmd_mcp(args: argparse.Namespace) -> int:
    """Serve one MCP tool per function, plus jeff_dispatch and jeff_list_functions (jeff.mcp), over stdio, for the
    engine at --socket or the Jeff server at --server. The server's bearer token is read from the environment
    variable --token-env names (env_token, before every request), so it never appears in the process list."""

    try:
        from jeff.mcp import serve_stdio
    except ModuleNotFoundError as error:
        # Only the SDK itself being absent means the extra is not installed; any other missing module is a fault.
        if not is_mcp_sdk(error.name):
            raise
        raise RuntimeMissing("jeff mcp needs the MCP SDK: pip install 'jeff[mcp]'") from error

    catalogue = Catalogue.from_dir(Path(args.catalogue)) if args.catalogue else Catalogue.shipped()
    if args.socket is not None:
        from jeff.socket_engine import SocketEngine

        engine = SocketEngine(args.socket, timeout_s=args.timeout_s)  # what Jeff.connect builds, kept to close it
        try:
            serve_stdio(Jeff(engine, catalogue))  # refuses clashing tool names before serving
        finally:
            engine.close()
        return 0
    token = env_token(args.token_env) if args.token_env is not None else None
    with Jeff.remote(args.server, token, timeout_s=args.timeout_s, catalogue=catalogue) as remote:
        serve_stdio(remote)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="jeff")
    commands = parser.add_subparsers(dest="command", required=True)
    test = commands.add_parser("test", help="Run published test sets, or your own cases")
    test.add_argument("functions", nargs="+", help="adapters or functions, e.g. triage or triage.route")
    test.add_argument("--sample", type=int, help="score a fixed-seed random sample of this many rows per adapter")
    test.add_argument("--mine", help="your own cases: JSON lines {id, function, input, context?, labels?, expected}")
    test.add_argument("--save-baseline", help="with --mine: write the versions and each case's result to this file")
    test.add_argument("--against", help="with --mine: fail on any case that passed in this baseline and fails now")
    test.add_argument("--engine-rows", action="store_true",
                      help="send each published row to the engine as written, instead of rebuilding it as your call")
    test.add_argument("--backend", default="auto", choices=["auto", "mlx", "torch", "fast"])
    test.add_argument("--json", action="store_true")
    test.set_defaults(handler=cmd_test)
    calibrate = commands.add_parser("calibrate", help="Fit per-function thresholds and write them to the catalogue")
    calibrate.add_argument("adapter", help="an adapter (every function) or one function, e.g. triage or "
                           "triage.urgency; or an adapter folder (a path with a '/', holding manifest.yaml and adapter_config.json, "
                           "as JEFF_ADAPTER_DIRS serves): every function, written into the folder's manifest.yaml")
    calibrate.add_argument("--catalogue", help="the catalogue folder to read and write (default: the repository's "
                           "catalogue/, when running from a checkout); for an adapter folder, the catalogue read for "
                           "its base (default: the catalogue shipped with jeff), never written")
    calibrate.add_argument("--target-precision", type=float, required=True)
    calibrate.add_argument("--data")
    calibrate.add_argument("--split-test", action="store_true")
    calibrate.add_argument("--backend", default="auto", choices=["auto", "mlx", "torch", "fast"])
    calibrate.add_argument("--base", help="an INT4 base folder (jeff runtime quantize): fit the functions' thresholds "
                           "on it, leaving out the calibration rows its quantiser saw, and write them as their "
                           "calibration_by_format entries for its format_id")
    calibrate.set_defaults(handler=cmd_calibrate)
    gen = commands.add_parser("gen", help="Generate typed code for functions or adapters")
    gen.add_argument("refs", nargs="+", help="adapters or functions, e.g. triage or triage.route")
    gen.add_argument("--lang", default="python", help="target language (only python exists in this version)")
    gen.add_argument("--out", required=True, help="file to write the generated code to")
    gen.set_defaults(handler=cmd_gen)
    runtime = commands.add_parser("runtime", help="The engine process")
    runtime_commands = runtime.add_subparsers(dest="runtime_command", required=True)
    serve = runtime_commands.add_parser("serve", help="Serve decisions over a Unix socket")
    serve.add_argument("--socket", required=True, help="Unix socket path to listen on")
    serve.add_argument("--adapter", action="append", default=[], metavar="ID",
                       help="an adapter to load and serve at startup (repeatable); only when the served file does "
                            "not exist yet (once it does, the engine starts from its list)")
    serve.add_argument("--adapters-dir", action="append", default=[], metavar="DIR",
                       help="a folder to find adapter folders in, at any depth (repeatable): each one found may be "
                            "served, and 'reload' looks again")
    serve.add_argument("--base-version", type=_base_pair, metavar="ID@VERSION",
                       help="the base to load, e.g. jeff-base@v1.3: needed to start with no adapters; otherwise it "
                            "must match the starting adapters' base")
    serve.add_argument("--path", action="append", type=_path_pair, default=[], metavar="ID=FOLDER",
                       help="load this adapter's weights from FOLDER instead of downloading them (repeatable)")
    serve.add_argument("--catalogue", help="the catalogue folder to read (default: the catalogue shipped with jeff)")
    serve.add_argument("--served-file", help="read and write the served adapter list here, for 'serve' and "
                       "'reload'; when it exists, the engine starts from its list")
    serve.add_argument("--batch-window-ms", type=float, required=True)
    serve.add_argument("--max-batch-rows", type=int, required=True,
                       help="the most rows in one batch; on the fast backend the prefix pool (--prefix-pool-mb) must "
                            "hold this many prefixes of 8,192 tokens at once, or the engine refuses to start")
    serve.add_argument("--queue-limit-rows", type=int, required=True)
    serve.add_argument("--backend", default="auto", choices=["auto", "mlx", "torch", "fast"])
    serve.add_argument("--max-adapters", type=int, metavar="N",
                       help="the most adapters the engine holds at once (beside the base); required on the fast "
                            "backend (NVIDIA), which allocates room for them at start, and refused on the others")
    serve.add_argument("--prefix-pool-mb", type=int, metavar="MB",
                       help="GPU memory, in MB, for cached prompt prefixes, allocated at start; required on the fast "
                            "backend (NVIDIA) and refused on the others. It must hold --max-batch-rows prefixes of "
                            "8,192 tokens: 7345 for 64 rows (8192 holds 266 prefixes of 1,000 tokens)")
    serve.add_argument("--prepare-workers", type=int, required=True, metavar="N",
                       help="how many requests are prepared at once on arrival (checked, and on the fast backend "
                            "tokenised), each by a thread with its own copy of the tokenizer; required, on every "
                            "backend")
    serve.add_argument("--base", metavar="FOLDER",
                       help="serve this INT4 base folder (jeff runtime quantize) of the adapters' base instead of the "
                            "bfloat16 base; refused unless its gate.json (jeff runtime gate) passes for every adapter "
                            "served, at start and on every serve and reload")
    serve.add_argument("--max-tokens", type=int)
    serve.set_defaults(handler=cmd_runtime_serve)
    quantize = runtime_commands.add_parser("quantize", help="Write the INT4 base of a bfloat16 base (NVIDIA)")
    quantize.add_argument("--base", required=True, metavar="FOLDER", help="the bfloat16 base folder to quantise")
    quantize.add_argument("--method", required=True, choices=["gptq"],
                          help="the quantisation method (only GPTQ is built)")
    quantize.add_argument("--calibrate-on", action="append", required=True, metavar="ID|FOLDER",
                          help="an adapter whose calibration rows the quantiser uses (repeatable): a catalogue ID or "
                               "alias, or an adapter folder (a path with a '/', holding manifest.yaml); the rows are "
                               "read from the calibration data its manifest names, never its test rows")
    quantize.add_argument("--calibrate-on-rows", action="append", type=_rows_pair, default=[],
                          metavar="ID|FOLDER=JSONL",
                          help="draw this adapter's quantiser rows from JSONL (its training rows: id, state and "
                               "question per line) instead of its calibration set (repeatable); when given, it must "
                               "name every --calibrate-on adapter. The rows are a seeded sample, and none may be in "
                               "the adapter's test or calibration set")
    quantize.add_argument("--rows-per-adapter", type=int, required=True, metavar="N",
                          help="calibration rows per adapter (the v1.3 INT4 base used 64)")
    quantize.add_argument("--path", action="append", type=_path_pair, default=[], metavar="ID=FOLDER",
                          help="load this catalogue adapter's weights from FOLDER instead of downloading them "
                               "(repeatable)")
    quantize.add_argument("--catalogue", help="the catalogue folder to read (default: the catalogue shipped with jeff)")
    quantize.add_argument("--out", required=True, metavar="FOLDER", help="the new INT4 base folder (must not exist)")
    quantize.set_defaults(handler=cmd_runtime_quantize)
    gate = runtime_commands.add_parser("gate", help="Gate an INT4 base for serving: accuracy, agreement, "
                                       "calibration and determinism against its bfloat16 base (NVIDIA)")
    gate.add_argument("--base", required=True, metavar="FOLDER", help="the INT4 base folder (jeff runtime quantize); "
                      "gate.json is written into it")
    gate.add_argument("--reference", required=True, metavar="FOLDER",
                      help="the bfloat16 base folder it was quantised from")
    gate.add_argument("--adapters", nargs="+", required=True, metavar="ID|FOLDER",
                      help="the adapters to gate (every adapter this base will serve): catalogue IDs or aliases, or "
                           "adapter folders (holding manifest.yaml)")
    gate.add_argument("--path", action="append", type=_path_pair, default=[], metavar="ID=FOLDER",
                      help="a catalogue adapter's weights folder, instead of downloading its open weights "
                           "(repeatable)")
    gate.add_argument("--catalogue", help="the catalogue folder to read (default: the catalogue shipped with jeff)")
    gate.set_defaults(handler=cmd_runtime_gate)
    health = runtime_commands.add_parser("health", help="Ask a running engine for its health once")
    health.add_argument("--socket", required=True, help="the engine's Unix socket (jeff runtime serve --socket)")
    health.add_argument("--timeout-s", type=float, required=True,
                        help="how long to wait for the engine to connect and answer, in seconds")
    health.set_defaults(handler=cmd_runtime_health)
    mcp = commands.add_parser("mcp", help="Serve Jeff's functions as MCP tools over stdio")
    target = mcp.add_mutually_exclusive_group(required=True)
    target.add_argument("--socket", help="the engine's Unix socket (jeff runtime serve --socket)")
    target.add_argument("--server", help="a Jeff server's URL, e.g. http://127.0.0.1:8000")
    mcp.add_argument("--token-env", metavar="NAME",
                     help="with --server: the environment variable holding the server's bearer token (leave out "
                          "for a server with no credentials)")
    mcp.add_argument("--timeout-s", type=float, required=True,
                     help="how long to wait for each answer from the engine or server, in seconds")
    mcp.add_argument("--catalogue", help="the catalogue folder to read (default: the catalogue shipped with jeff)")
    mcp.set_defaults(handler=cmd_mcp)
    args = parser.parse_args(argv)
    if args.command == "mcp" and args.socket is not None and args.token_env is not None:
        parser.error("--token-env goes with --server; an engine socket takes no token")
    if args.command == "test":
        if args.mine is None and (args.save_baseline or args.against):
            parser.error("--save-baseline and --against need --mine")
        if args.mine is not None and args.sample is not None:
            parser.error("--sample samples published test sets; it does not take --mine (your cases all run)")
        if args.mine is not None and args.engine_rows:
            parser.error("--engine-rows scores published test sets; it does not take --mine")
    if args.command == "gen" and args.lang != "python":
        parser.error(f"--lang {args.lang!r} is not supported; only python exists in this version "
                     "(TypeScript arrives with the Node SDK, npm package jeff-sdk)")
    try:
        return int(args.handler(args))
    except JeffError as error:
        print(f"jeff: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
