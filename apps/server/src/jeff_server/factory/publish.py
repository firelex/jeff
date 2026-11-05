"""Publishing a factory adapter (the publish step's work, after the gate in jeff_server.factory.runner): the adapter
folder with its manifest goes into the first adapters dir, the engines serve it, and an example is answered through
Jeff, so the adapter is callable by name through Jeff, /v1/decide and MCP with no restart.

1. An existing <adapter root>/<name>/ (or a .<name>.publishing/ a stopped publish left) is refused.
2. The calibration split is scored as Jeff sends its requests (manifest.served_question), and the manifest is built
   and validated (jeff_server.factory.manifest); nothing is copied before that.
3. The adapter is copied into a hidden <adapter root>/.<name>.publishing/ with manifest.yaml, example.json,
   data_card.json and the run's test and calibration splits (manifest.published_rows); the manifest is validated
   again from disk, with the catalogue the API would build from it.
4. The folder is renamed to <adapter root>/<name>/.
5. Every engine reloads (finding the folder) and serves the adapter beside the ones it serves (a reload finds new
   folders; serving one changes the served file), under the adapter-change lock the API's adapter changes take too
   (jeff_server.adapter_change).
6. The example's first question is decided through a Jeff over the engines, with the API's catalogue, and its answer
   checked to be one of the function's labels.
7. publish.json is written into the run folder.

A failure before 5 removes the hidden folder. A failure after 4 takes the adapter back out (unpublish): each engine
stops serving it, the folder moves to <adapter root>/.<name>.failed-<time>/ (hidden, so never discovered), the
engines reload, and the step fails with the original cause; a clean-up that fails too is added to the error as a
note and recorded in the run's publish.rolled_back event."""

import shutil
import time
from collections import defaultdict
from contextlib import closing
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from jeff.client import Call, Jeff
from jeff.decision import Decision
from jeff.errors import Conflict, EngineError, JeffError, ManifestError
from jeff.manifest import FunctionSpec, load_manifest
from jeff.socket_engine import SocketEngine

from jeff_server.adapter_change import AdapterChangeLock
from jeff_server.catalogue_refresh import catalogue_with_folders
from jeff_server.factory import evaluation, manifest as manifest_module, spec as spec_module
from jeff_server.factory.store import Run, read_json, read_jsonl, write_json, write_jsonl
from jeff_server.router import EngineRouter
from jeff_server.systemone import check_model_name

if TYPE_CHECKING:
    from jeff_server.factory.runner import WorkerHooks

# How long publishing waits for an engine to reload, serve or answer (a reload loads the new adapter).
ENGINE_CHANGE_TIMEOUT_S = 120.0


def publish(run: Run, hooks: "WorkerHooks", metrics: dict[str, Any], adapter: Path,
            card: dict[str, Any]) -> dict[str, Any]:
    """Steps 1 to 7 of the module docstring for `run`, whose gate has passed; `adapter` is the trainer's output and
    `card` the data card. Returns what the step's end event records."""
    spec = run.spec
    destination = hooks.adapter_root / spec["name"]
    staging = destination.with_name(f".{spec['name']}.publishing")
    if destination.exists():
        raise Conflict(f"{destination} already exists; an adapter of that name is already in the folder")
    if staging.exists():
        raise Conflict(f"{staging} is left from a publish that stopped; remove it, then publish again")
    raw, splits = build_manifest(run, hooks, metrics, adapter, destination)
    example = example_request(spec, read_jsonl(run.data / "test.jsonl"))
    try:
        shutil.copytree(adapter, staging)
        (staging / "manifest.yaml").write_text(yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
                                               encoding="utf-8")
        write_json(staging / "example.json", example)
        write_json(staging / "data_card.json", card)
        for name, rows in splits.items():
            write_jsonl(staging / name, rows)
        if not (staging / "adapter_config.json").is_file():
            raise ManifestError(f"{adapter} has no adapter_config.json; an engine would not find the folder")
        published = load_manifest(staging / "manifest.yaml")
        # The checks the API makes when it builds its catalogue, so no API worker refuses the folder once it is in
        # place.
        check_model_name(catalogue_with_folders(hooks.catalogue, hooks.adapter_dirs, [published]), hooks.model_name)
        staging.rename(destination)
    except Exception as error:
        if staging.exists():
            try:
                shutil.rmtree(staging)
            except OSError as problem:
                error.add_note(f"{staging} could not be removed ({problem}); remove it before publishing again")
        raise
    adapter_id = raw["id"]
    try:
        served = serve_published(hooks, adapter_id)
        function = f"{adapter_id}.{manifest_module.function_keys(spec)[next(iter(example['questions']))]}"
        decision, server_ms = decide_example(hooks, spec, function, example)
        write_json(run.folder / "publish.json", {
            "folder": str(destination), "adapter": adapter_id, "served": served, "example": example,
            "function": function, "answer": decision.to_json(), "server_ms": server_ms,
            "warning": None if metrics.get("independent") else
            "Published on the synthetic test only (no independent test set)"})
    except Exception as error:
        unpublish(run, hooks, destination, adapter_id, error)
        raise
    return {"folder": str(destination), "adapter": adapter_id, "server_ms": server_ms}


def build_manifest(run: Run, hooks: "WorkerHooks", metrics: dict[str, Any], adapter: Path,
                   destination: Path) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """The manifest for publishing into `destination`, and the files its tests point at (file name -> rows). The
    calibration split is scored again with the requests as Jeff sends them (the canonical instruction, the options
    in the spec's order), at the temperature the calibrate step fitted, so each function's threshold is fitted on
    the confidence Jeff compares with it when serving. The scores are kept in calibration-served-logits.json."""
    spec = run.spec
    decision_config = read_json(adapter / "decision_config.json")
    if "max_options" not in decision_config:
        raise ManifestError(f"{adapter / 'decision_config.json'} has no max_options; the manifest needs it")
    test_rows, calibration_rows = read_jsonl(run.data / "test.jsonl"), read_jsonl(run.data / "calibration.jsonl")
    # The checks that need no scoring first: the requests' fields and the function keys.
    manifest_module.check_named_fields(spec, [*test_rows, *calibration_rows])
    manifest_module.function_keys(spec)
    questions = {question["id"]: manifest_module.served_question(question) for question in spec["questions"]}
    served = [{**row, "question": questions[row["source"]["question_id"]]} for row in calibration_rows]
    scored = evaluation.scored_rows(hooks.score, hooks.base, adapter, served,
                                    run.folder / "calibration-served-logits.json")
    predictions = evaluation.evaluate_logits(served, scored["logits"], decision_config["temperature"])
    base = {"id": hooks.base_id, "version": hooks.base_version, "max_options": decision_config["max_options"]}
    raw = manifest_module.build(spec, metrics, calibration_rows, predictions, base, test_rows=test_rows,
                                folder=destination)
    splits = {manifest_module.TEST_FILE: manifest_module.published_rows(spec, test_rows, raw["id"]),
              manifest_module.CALIBRATION_FILE: manifest_module.published_rows(spec, calibration_rows, raw["id"])}
    return raw, splits


def serve_published(hooks: "WorkerHooks", adapter_id: str) -> dict[str, list[str]]:
    """Each engine reloads, finding the new folder, then serves the adapter beside the ones it serves, under the
    adapter-change lock the API's changes take too (jeff_server.adapter_change), so a served change from the API
    cannot land between an engine's reload and its serve. Returns engine socket -> what it serves now. EngineError
    naming the engine that refused."""
    with AdapterChangeLock(hooks.adapter_dirs).held():
        return _serve_published(hooks, adapter_id)


def _serve_published(hooks: "WorkerHooks", adapter_id: str) -> dict[str, list[str]]:
    served = {}
    for path in hooks.engines:
        action = "connect to"
        engine = None
        try:
            engine = SocketEngine(path, timeout_s=ENGINE_CHANGE_TIMEOUT_S)
            action = "reload"
            installed = engine.reload()
            action = f"serve {adapter_id}"
            served[str(path)] = sorted(engine.serve(sorted(installed | {adapter_id})))
        except JeffError as error:
            raise EngineError(f"The engine at {path} refused to {action}: {type(error).__name__}: {error}") \
                from error
        finally:
            if engine is not None:
                engine.close()
    return served


def decide_example(hooks: "WorkerHooks", spec: dict[str, Any], function_id: str,
                   example: dict[str, Any]) -> tuple[Decision, float]:
    """The example's first question decided as `function_id` through a Jeff over every engine, with the catalogue
    the API builds; EngineError when the answer is not one of the function's labels. Returns the decision and its
    wall time in milliseconds. The state is an object of named fields (manifest.check_named_fields)."""
    engines: list[SocketEngine] = []
    try:
        for path in hooks.engines:
            engines.append(SocketEngine(path, timeout_s=ENGINE_CHANGE_TIMEOUT_S))
        jeff = Jeff(EngineRouter(engines), catalogue_with_folders(hooks.catalogue, hooks.adapter_dirs))
        function = jeff.catalogue.function(function_id)
        live, state = spec["live_field"]["name"], example["state"]
        # a caller passes context by code name; the manifest maps each back to the field's exact key for the prompt
        context = {field.code_name: state[field.key] for field in function.context}
        began = time.perf_counter()
        decision = jeff.decide([Call(function_id, state[live], context)])[0]
        server_ms = round((time.perf_counter() - began) * 1000, 1)
    finally:
        for engine in engines:
            engine.close()
    if not is_label(function, decision.value):
        raise EngineError(f"{function_id} answered the example with {decision.value!r}, which is not one of its "
                          "labels")
    return decision, server_ms


def unpublish(run: Run, hooks: "WorkerHooks", destination: Path, adapter_id: str, error: Exception) -> None:
    """Take a published adapter back out after a later step failed with `error`: every engine stops serving it, its
    folder moves to a hidden .<name>.failed-<time>/ beside it, and the engines reload, all under the adapter-change
    lock (as serve_published). A clean-up that fails is added to `error` as a note, never raised in its place."""
    notes: list[str] = []
    failed = destination.with_name(f".{destination.name}.failed-{time.strftime('%Y%m%d-%H%M%S')}")
    with AdapterChangeLock(hooks.adapter_dirs).held():
        for path in hooks.engines:
            try:
                with closing(SocketEngine(path, timeout_s=ENGINE_CHANGE_TIMEOUT_S)) as engine:
                    installed = engine.installed(refresh=True)
                    if adapter_id in installed:
                        engine.serve(sorted(installed - {adapter_id}))
            except Exception as problem:
                notes.append(f"The engine at {path} could not stop serving {adapter_id}: "
                             f"{type(problem).__name__}: {problem}")
        try:
            destination.rename(failed)
        except OSError as problem:
            notes.append(f"{destination} could not be moved to {failed} ({problem}); engines will find it until it "
                         "is removed")
        for path in hooks.engines:
            try:
                with closing(SocketEngine(path, timeout_s=ENGINE_CHANGE_TIMEOUT_S)) as engine:
                    engine.reload()
            except Exception as problem:
                notes.append(f"The engine at {path} could not reload after the folder was moved: "
                             f"{type(problem).__name__}: {problem}")
    for note in notes:
        error.add_note(note)
    run.event("publish.rolled_back", moved_to=failed.name, notes=notes)


def is_label(function: FunctionSpec, value: Any) -> bool:
    """Whether a decision's value is one of the function's labels: true or false, a level's index, or an option."""
    if function.type == "noul":
        return isinstance(value, bool)
    if function.type == "score":
        return isinstance(value, int) and not isinstance(value, bool) and 0 <= value < len(function.labels.levels)
    return isinstance(value, str) and value in function.labels.fixed


def example_request(spec: dict[str, Any], test: list[dict[str, Any]]) -> dict[str, Any]:
    """One held-out test request with every question that applies to it, canonical wording, as /v1/systemone takes
    it."""
    by_scenario: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in test:
        by_scenario[row["source"]["scenario"]].append(row)
    rows = max(by_scenario.values(), key=len)
    asked = {row["source"]["question_id"] for row in rows}
    questions = {question["id"]: spec_module.question_payload(question)
                 for question in spec["questions"] if question["id"] in asked}
    return {"model": spec["name"], "state": rows[0]["state"], "questions": questions}
