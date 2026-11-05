"""The factory's runs: starting them, the review gate, test sets, and the steps themselves, each with a start and
an end event (server clock) and a failure event when it stops. A run stays the clean, timed kind only if every step
ran once, in order, without the process running it stopping in between; anything else is recorded in run.json's
unclean_reasons.

Where steps run (Factory.launch):
- a build run's steps are queued (jeff_server.factory.queue) and the factory worker (jeff_server.factory.worker)
  runs them, one at a time across all runs; the API never runs one;
- a preview's one step, interpret (teacher calls, no GPU), runs in the API process, in a background thread, at most
  PREVIEW_THREADS at once, as in jeff-server."""

import csv
import hashlib
import io
import json
import os
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jeff.errors import Conflict, EngineError, FactoryOff

from jeff_server.factory import checks as checks_module
from jeff_server.factory import describe, evaluation, generate, importing, queue, spec as spec_module, testset as testset_module, training
from jeff_server.factory import publish as publish_module
from jeff_server.factory.config import FactoryConfig
from jeff_server.factory.store import BUILD_STEPS, Run, new_run, now, read_json, read_jsonl, spec_hash, write_json, write_jsonl
from jeff_server.factory.teacher import Teacher

SEED = 20261017


# The steps a preview may run: before any data is generated. Everything after costs teacher time and GPU time.
PREVIEW_STEPS = {"interpret"}
# Previews interpreted at once in one API process; a further one is refused with Conflict.
PREVIEW_THREADS = 4
API_RUNS_NO_STEP = "Build steps run in the factory worker (jeff-server factory worker), never in the API"
# The steps the worker runs; "review" is the person's, between interpret and approval.
WORKER_STEPS = {"interpret", *BUILD_STEPS, "independent_test"}


@dataclass(frozen=True)
class WorkerHooks:
    """What the steps need from the factory worker: the base the engines serve (`base`, the checkpoint folder:
    training starts from it and scoring loads it; `base_id` and `base_version`, as manifests name it), the base's
    public name (JEFF_MODEL_NAME, which a published adapter's names must not take, as the API checks), the adapter
    folders (JEFF_ADAPTER_DIRS: new adapters are published into the first) and the catalogue (JEFF_CATALOGUE, None
    for the one shipped with jeff) the API builds its catalogue from, the engine sockets to reload after a publish,
    and the scorer (jeff_server.factory.evaluation.score; tests pass a fake)."""
    base: str
    base_id: str
    base_version: str
    model_name: str
    adapter_dirs: tuple[Path, ...]
    catalogue: Path | None
    engines: tuple[Path, ...]
    score: evaluation.Scorer

    @property
    def adapter_root(self) -> Path:
        return self.adapter_dirs[0]


class Factory:
    def __init__(self, config: FactoryConfig, hooks: WorkerHooks | None, preview: bool = False) -> None:
        """hooks: the worker's, or None in the API, which runs no build step. preview=True: runs live in previews/,
        are offered whenever the server has teacher settings (also with JEFF_FACTORY=off), and stop before data
        generation: the description check, interpretation or import, spec edits and approval only. Their
        interpretation runs here, in this process (launch)."""
        self.config, self.preview = config, preview
        self.hooks = hooks
        self.folder = config.folder("previews" if preview else "runs")
        self.busy = threading.Semaphore(PREVIEW_THREADS)  # previews only: a few interpretations at once
        self.tokenizer: Any = None

    # --- runs -------------------------------------------------------------------------------------------------------

    def run(self, run_id: str) -> Run:
        folder = self.folder / run_id
        if "/" in run_id or run_id.startswith(".") or not (folder / "run.json").exists():
            raise KeyError(run_id)
        return Run(folder)

    def runs(self) -> list[dict[str, Any]]:
        found = []
        for folder in sorted(self.folder.iterdir(), reverse=True):
            if (folder / "run.json").exists():
                state = read_json(folder / "run.json")
                found.append({key: state[key] for key in ("id", "created", "status", "clean", "approved")}
                             | {"name": spec_name(folder)})
        return found

    def mark_interrupted(self, stopped: str) -> set[str]:
        """A step still marked running belongs to a process that stopped: fail it with "<stopped> during <step>" and
        mark the run unclean. Called by the factory worker at start for build runs ("the worker stopped"; only one
        worker runs at a time, so no other is running then) and by the API at start for previews ("the server
        restarted").

        A preview's interpretation records the pid of the API process running it. With `serve --workers N` that may
        be a sibling API process that is still running (uvicorn restarts a worker on its own): such a preview is left
        alone. A preview is failed only when its pid is this process's own (a previous life of it) or no longer
        alive, or when it has no pid (written by older code). Nothing is ever killed. A pid reused by an unrelated
        process since leaves the preview "running"; that edge is accepted. Returns the runs changed."""
        changed = set()
        for folder in self.folder.iterdir():
            if not (folder / "run.json").exists():
                continue
            run = Run(folder)
            for step, info in run.state["steps"].items():
                if step in WORKER_STEPS and info.get("status") == "running":
                    pid = info.get("pid")
                    if self.preview and pid is not None and pid != os.getpid() and alive(pid):
                        continue  # a live sibling API process is interpreting it
                    error = f"{stopped} during {step}"
                    run.event("step.failed", step=step, error=error)
                    run.set_step(step, status="failed", error=error)
                    self.unclean(run, error)
                    run.update(status="failed")
                    changed.add(run.id)
        return changed

    def unclean(self, run: Run, reason: str) -> None:
        state = run.state
        run.update(clean=False, unclean_reasons=[*state["unclean_reasons"], reason])

    @contextmanager
    def preview_slot(self) -> Iterator[None]:
        """For a preview: take one of the PREVIEW_THREADS slots before anything is written (Conflict when all are
        busy, so a refused preview leaves no run folder and does not use up its description check). The slot passes
        to the preview's thread once launched (preview_work frees it); if starting the run fails, it is freed here.
        For a build run: nothing."""
        if not self.preview:
            yield
            return
        if not self.busy.acquire(blocking=False):
            raise Conflict("Several previews are being interpreted; try again in a minute")
        try:
            yield
        except BaseException:
            self.busy.release()
            raise

    def start(self, check_id: str, choice: str, description: str, name: str | None) -> Run:
        with self.preview_slot():
            return self.start_checked(check_id, choice, description, name)

    def start_checked(self, check_id: str, choice: str, description: str, name: str | None) -> Run:
        """Start a run from a checked and confirmed description: the person kept their own text (original), took the
        teacher's improved text (improved) or edited it (edited). The check and the confirmation go into the run's
        timeline with their real times, before run.start."""
        folder = self.config.folder("description-checks")
        try:
            checked = describe.load(folder, check_id)
        except KeyError as error:
            raise ValueError(f"No description check {check_id!r}; check the description first") from error
        if checked["used_by"]:
            raise Conflict(f"That description check already started run {checked['used_by']}; check again for a new run")
        expected = {"original": checked["description"], "improved": checked["result"]["improved_description"]}
        if choice not in ("original", "improved", "edited"):
            raise ValueError("choice must be original, improved or edited")
        if choice in expected and description != expected[choice]:
            raise ValueError(f"The description sent is not the {choice} one from the check")
        if not description.strip():
            raise ValueError("The description is empty")
        if name is not None:
            spec_module.check_name(name)
        record = {"id": check_id, "choice": choice, "original": checked["description"],
                  "improved": checked["result"]["improved_description"], "ready": checked["result"]["ready"],
                  "summary": checked["result"]["summary"], "problems": checked["result"]["problems"],
                  "questions": checked["result"]["questions"], "changes": checked["result"]["changes"],
                  "teacher_model": checked["teacher_model"]}
        run = new_run(self.folder, name or "adapter", description, checked["examples"], description_check=record)
        run.event("description_check.start", at=checked["started"])
        run.event("description_check.end", at=checked["ended"], teacher=checked["teacher"],
                  ready=checked["result"]["ready"], problems=len(checked["result"]["problems"]))
        run.event("confirm.start", at=checked["ended"])
        run.event("confirm.end", choice=choice)
        run.set_step("description_check", status="done", started=checked["started"], ended=checked["ended"])
        run.set_step("confirm", status="done", started=checked["ended"], ended=now())
        checked["used_by"] = run.id
        write_json(folder / f"{check_id}.json", checked)
        run.event("run.start", description_chars=len(description))
        self.launch(run, ["interpret"])
        return run

    def references(self) -> list[dict[str, Any]]:
        return importing.bundles(self.config.folder("references"))

    def start_import(self, reference: str, name: str, description: str) -> Run:
        with self.preview_slot():
            return self.start_imported(reference, name, description)

    def start_imported(self, reference: str, name: str, description: str) -> Run:
        """Start a run from an imported question set (JEFF_FACTORY_DIR/references/<reference>/): the questions are
        taken verbatim; the teacher only plans and writes the training data from the short task description."""
        folder = self.config.folder("references") / reference
        if "/" in reference or reference.startswith(".") or not folder.is_dir():
            raise ValueError(f"No reference {reference!r}; the references are: "
                             f"{', '.join(b['name'] for b in self.references()) or 'none'}")
        spec_module.check_name(name)
        if not description.strip():
            raise ValueError("Give the short task description (who writes the requests, the hard patterns)")
        bundle = importing.load_bundle(folder)
        importing.to_spec_questions(bundle["questions"], bundle["conditions"])  # refuses anything not verbatim
        run = new_run(self.folder, name, description, None, imported=reference)
        reference_copy = run.folder / "reference"
        reference_copy.mkdir()
        for file in folder.iterdir():
            if file.is_file():
                shutil.copy2(file, reference_copy / file.name)
        run.event("run.start", description_chars=len(description), reference=reference,
                  questions=len(bundle["questions"]))
        self.launch(run, ["interpret"])
        return run

    def start_from_run(self, source_id: str, name: str, sizes: dict[str, int]) -> Run:
        with self.preview_slot():
            return self.start_copied(source_id, name, sizes)

    def start_copied(self, source_id: str, name: str, sizes: dict[str, int]) -> Run:
        """A new run with the approved spec of an earlier run, changing only the adapter name and the sizes: same
        questions (and imported reference), same data plan, same description. Everything else is fresh: a new run
        folder, no data or teacher replies from the earlier run."""
        source = self.run(source_id)
        state = source.state
        if not state["approved"]:
            raise Conflict(f"Run {source_id} has no approved spec")
        spec_module.check_name(name)
        spec = {**source.spec, "name": name, "sizes": sizes}
        spec_module.check_spec(spec)
        run = new_run(self.folder, name, state["description"], state["examples"], imported=state["reference"],
                      spec_from=source_id)
        if state["reference"]:
            shutil.copytree(source.folder / "reference", run.folder / "reference")
        write_json(run.folder / "copied-spec.json", spec)
        run.event("run.start", spec_from=source_id, source_spec_sha256=state["spec_sha256"], sizes=sizes)
        self.launch(run, ["interpret"])
        return run

    def reference_bundle(self, run: Run) -> dict[str, Any]:
        return importing.load_bundle(run.folder / "reference")

    # --- the review gate --------------------------------------------------------------------------------------------

    def save_spec(self, run: Run, spec: dict[str, Any]) -> None:
        if run.state["approved"]:
            raise Conflict("The spec is approved and frozen; start a new run to change it")
        if run.state["steps"].get("interpret", {}).get("status") != "done":
            raise Conflict("The description has not been interpreted yet")
        spec_module.check_spec(spec)
        if run.state["mode"] == "import" and spec["questions"] != run.spec["questions"]:
            raise ValueError("The questions of an import run are taken verbatim and cannot be edited")
        write_json(run.folder / "spec.json", spec)

    def approve(self, run: Run) -> None:
        if run.state["approved"]:
            raise Conflict("The spec is already approved")
        if not (run.folder / "spec.json").exists():
            raise Conflict("There is no spec to approve yet")
        spec = run.spec
        spec_module.check_spec(spec)
        digest = spec_hash(spec)
        run.event("review.end", spec_sha256=digest)
        run.set_step("review", status="done", ended=now())
        run.update(approved=now(), spec_sha256=digest, status="approved")

    def set_gate(self, run: Run, minimum: float) -> None:
        if not 0 < minimum <= 1:
            raise ValueError("The minimum accuracy is a share between 0 and 1, for example 0.9")
        run.update(gate={"min_accuracy": minimum})

    def upload_test(self, run: Run, content: str, kind: str) -> dict[str, Any]:
        if not run.state["approved"]:
            raise Conflict("Approve the spec before uploading a test set")
        if kind == "calls":
            bundle = self.reference_bundle(run) if run.state["mode"] == "import" else {}
            if "signatures" not in bundle:
                raise ValueError("A test set of calls needs an import run whose reference has signatures.json")
            rows = parse_calls(run.spec, content, bundle["signatures"])
        else:
            rows = parse_test_set(run.spec, content, kind)
        write_jsonl(run.data / "independent.jsonl", rows)
        info = {"rows": len(rows), "uploaded": now(), "format": kind}
        run.update(independent_test=info)
        return info

    def testsets(self) -> list[dict[str, Any]]:
        found = []
        for folder in sorted(self.config.folder("testsets").iterdir()):
            if (folder / "candidates.jsonl").exists():
                state = testset_module.load(folder)
                found.append({"name": folder.name, "summary": state["summary"], "frozen": state["frozen"],
                              "items": len(state["candidates"]), "reviewed": len(state["reviews"])})
        return found

    def testset_folder(self, name: str) -> Path:
        folder = self.config.folder("testsets") / name
        if "/" in name or name.startswith(".") or not (folder / "candidates.jsonl").exists():
            raise KeyError(name)
        return folder

    def attach_testset(self, run: Run, name: str) -> dict[str, Any]:
        """Use a frozen test set as the run's independent test. Recorded with its hash, and whether it was attached
        before data generation started (the official run requires that: training data never sees the test)."""
        folder = self.testset_folder(name)
        state = testset_module.load(folder)
        if not state["frozen"]:
            raise Conflict(f"Test set {name} is not frozen yet")
        content = (folder / "frozen.jsonl").read_text(encoding="utf-8")
        if hashlib.sha256(content.encode()).hexdigest() != state["frozen"]["sha256"]:
            raise Conflict(f"Test set {name} changed after it was frozen")
        info = self.upload_test(run, content, "calls")
        before = run.state["steps"].get("generate", {}).get("status") is None
        info = {**info, "testset": name, "sha256": state["frozen"]["sha256"], "before_generation": before}
        run.update(independent_test=info)
        run.event("independent_test.attached", testset=name, sha256=state["frozen"]["sha256"], rows=info["rows"],
                  before_generation=before)
        return info

    # --- running steps ----------------------------------------------------------------------------------------------

    def rest(self, run: Run) -> list[str]:
        """The steps still to run, in order (the independent test only when a test set was uploaded)."""
        steps = run.state["steps"]
        order = [step for step in BUILD_STEPS]
        if run.state["independent_test"]:
            order.insert(order.index("publish"), "independent_test")
        return [step for step in order if steps.get(step, {}).get("status") != "done"]

    def launch(self, run: Run, steps: list[str]) -> None:
        """A build run's steps: queued for the factory worker (Conflict when the run already has a job). A
        preview's: run here, in a background thread, holding the slot its start took (preview_slot)."""
        if not self.preview:
            queue.enqueue(self.config.required_root, run.id, steps)
            return
        if not set(steps) <= PREVIEW_STEPS:
            raise FactoryOff("Generating data and training are not available in investor mode")
        threading.Thread(target=self.preview_work, args=(run, steps), name=f"preview-{run.id}", daemon=True).start()

    def preview_work(self, run: Run, steps: list[str]) -> None:
        try:
            self.work(run, steps)
        finally:
            self.busy.release()

    def run_steps(self, run: Run, steps: list[str]) -> None:
        if self.preview:
            raise FactoryOff("Generating data and training are not available in investor mode")
        if not run.state["approved"]:
            raise Conflict("Approve the spec first")
        for step in steps:
            if step not in BUILD_STEPS and step != "independent_test":
                raise ValueError(f"Unknown step {step!r}")
        self.launch(run, steps)

    def work(self, run: Run, steps: list[str]) -> None:
        """Run a job's steps in order (the worker), stopping at the first that fails."""
        for step in steps:
            if not self.step(run, step):
                return

    @property
    def worker(self) -> WorkerHooks:
        if self.hooks is None:
            raise EngineError(API_RUNS_NO_STEP)
        return self.hooks

    def step(self, run: Run, step: str) -> bool:
        if self.hooks is None and not self.preview:
            # Only the factory worker runs a build step; in the API, refused before anything changes.
            raise EngineError(API_RUNS_NO_STEP)
        previous = run.state["steps"].get(step, {}).get("status")
        # A preview records the API process interpreting it (mark_interrupted); a build step's trainer records its own
        # pid (do_train).
        owner = {"pid": os.getpid()} if self.preview else {}
        run.set_step(step, status="running", started=now(), error=None, **owner)
        run.update(status=f"running {step}")
        run.event(f"{step}.start")
        began = time.monotonic()
        try:
            if previous in ("done", "failed"):
                self.unclean(run, f"{step} was run again")
            expected = self.rest(run)
            if step != "interpret" and expected and expected[0] != step:
                self.unclean(run, f"{step} ran before {expected[0]}")
            teacher = self.teacher(run)
            before = teacher.usage.snapshot()
            used = getattr(self, f"do_{step}")(run, teacher) or {}
        except Exception as error:  # every failure stops the step, is shown in its card and is logged in full
            # Notes added on the way out (a publish's clean-up that failed too) are shown after the cause.
            message = " ".join([f"{type(error).__name__}: {error}", *getattr(error, "__notes__", [])])
            (run.folder / f"{step}-error.txt").write_text(traceback.format_exc())
            run.event("step.failed", step=step, error=message)
            run.set_step(step, status="failed", error=message, ended=now())
            run.update(status="failed")
            return False
        after = teacher.usage.snapshot()
        teacher_used = {key: round(after[key] - before[key], 1) for key in after}
        run.event(f"{step}.end", seconds=round(time.monotonic() - began, 1), teacher=teacher_used, **used)
        run.set_step(step, status="done", ended=now())
        if step == "interpret":
            run.event("review.start")
            run.set_step("review", status="running", started=now())
            run.update(status="review")
        elif step == "publish":
            run.event("run.end", adapter=run.spec["name"])
            run.update(status="published")
        else:
            run.update(status=f"{step} done")
        return True

    def teacher(self, run: Run) -> Teacher:
        config = self.config
        assert config.teacher_url and config.teacher_key and config.teacher_model
        return Teacher(config.teacher_url, config.teacher_key, config.teacher_model, run.folder / "teacher-cache")

    # --- the steps --------------------------------------------------------------------------------------------------

    def do_interpret(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        state = run.state
        if state["spec_from"]:  # the earlier run's approved spec, sizes and name changed; no teacher call
            spec = read_json(run.folder / "copied-spec.json")
            write_json(run.folder / "spec.json", spec)
            write_json(run.folder / "draft-spec.json", spec)
            return {"spec_from": state["spec_from"], "teacher_model": None}
        if state["mode"] == "import":
            bundle = self.reference_bundle(run)
            questions = importing.to_spec_questions(bundle["questions"], bundle["conditions"])
            spec, model = spec_module.plan_for_import(teacher, state["description"], questions, state["name_hint"])
            write_json(run.folder / "spec.json", spec)
            write_json(run.folder / "draft-spec.json", spec)
            return {"teacher_model": model, "imported_questions": len(questions)}
        hint = state["name_hint"] if state["name_hint"] != "adapter" else None
        spec, model = spec_module.interpret(teacher, state["description"], state["examples"], hint)
        write_json(run.folder / "spec.json", spec)
        write_json(run.folder / "draft-spec.json", spec)
        return {"teacher_model": model}

    def do_data_prep(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        spec = run.spec
        families = generate.make_families(teacher, spec, spec["sizes"]["families"])
        with ThreadPoolExecutor(generate.IN_FLIGHT) as pool:
            pools = dict(zip([q["id"] for q in spec["questions"]],
                             pool.map(lambda question: generate.make_pool(teacher, question), spec["questions"])))
        plan = generate.plan_splits(spec, families, SEED)
        write_json(run.data / "families.json", families)
        write_json(run.data / "instruction-pools.json", pools)
        write_json(run.data / "plan.json", plan)
        return {"families": len(families), "test_families": len(plan["test"]),
                "pool": {qid: len(pool["variants"]) for qid, pool in pools.items()},
                "pool_rejected": {qid: pool["rejected"] for qid, pool in pools.items()}}

    def do_generate(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        spec = run.spec
        families = read_json(run.data / "families.json")
        plan = read_json(run.data / "plan.json")
        pools = read_json(run.data / "instruction-pools.json")
        last = [0.0]

        def progress(line: dict[str, Any]) -> None:
            if line["phase"] == "rows" and time.monotonic() - last[0] < 2 and line["done"] != line["calls"]:
                return  # at most one progress line every 2 seconds, plus each wave's last call
            last[0] = time.monotonic()
            run.event("generate.progress", **line)

        scenarios, failures = generate.generate_scenarios(teacher, spec, families, plan, SEED, progress)
        rows = generate.make_rows(spec, scenarios, pools, run.state["spec_sha256"], run.id, SEED)
        write_jsonl(run.data / "scenarios.jsonl", scenarios)
        write_jsonl(run.data / "rows.jsonl", rows)
        write_json(run.data / "invalid-teacher-calls.json", failures)
        teachers = sorted({scenario["teacher"] for scenario in scenarios})
        return {"requests": len(scenarios), "rows": len(rows), "invalid_calls": len(failures), "teacher_models": teachers}

    def do_checks(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        spec = run.spec
        rows = read_jsonl(run.data / "rows.jsonl")
        avoid = [row["state"] if isinstance(row["state"], str) else row["state"][spec["live_field"]["name"]]
                 for row in read_jsonl(run.data / "independent.jsonl")] if (run.data / "independent.jsonl").exists() else []
        files, report = checks_module.run_checks(spec, rows, SEED, self.count_tokens, avoid)
        for split, split_rows in files.items():
            write_jsonl(run.data / f"{split}.jsonl", split_rows)
        write_json(run.folder / "checks.json", report)
        return {"rows": report["rows"], "duplicates_dropped": report["duplicates_dropped"],
                "near_copies_dropped": report["near_copies_dropped"]}

    def count_tokens(self, row: dict[str, Any]) -> int:
        from jeff_runtime.engine.model import decision_messages

        if self.tokenizer is None:
            from transformers import AutoTokenizer
            self.tokenizer = AutoTokenizer.from_pretrained(self.worker.base)
            self.codes = read_json(Path(self.worker.base) / "decision_config.json")["codes"]
        messages = decision_messages({"state": row["state"], "question": row["question"]}, self.codes, "live-last")
        # Render the prompt as text, then count its tokens. (apply_chat_template(tokenize=True) returns a dictionary
        # in this transformers version; its length would be the number of keys, not of tokens.)
        text = self.tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        if not isinstance(text, str):
            raise TypeError(f"The chat template rendered {type(text).__name__}, not text")
        return len(self.tokenizer(text, add_special_tokens=False)["input_ids"])

    def do_train(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        free, total = training.free_gpu_gb()
        minimum = self.config.required_min_free_gb
        if free < minimum:
            raise RuntimeError(f"Only {free:.1f} of {total:.1f} GB GPU memory is free; training needs "
                               f"{minimum:g} GB so serving is not starved")
        train_rows = read_jsonl(run.data / "train.jsonl")
        updates = run.spec["sizes"]["updates"]
        passes = training.repeated_training_file(train_rows, run.data / "train-repeated.jsonl", updates)
        trainer = self.config.required_trainer
        run.event("train.launch", trainer_commit=self.config.trainer_commit, base=self.worker.base, passes=passes, updates=updates,
                  gpu_free_gb=round(free, 1), gpu_total_gb=round(total, 1))
        out = run.folder / "training"
        if out.exists():  # a failed attempt: kept for diagnosis, out of the way
            failed = run.folder / f"training-failed-{time.strftime('%Y%m%d-%H%M%S')}"
            out.rename(failed)
            run.event("train.previous_attempt", moved_to=failed.name)
        began = time.monotonic()
        result = training.train(trainer, self.worker.base, run.data, out,
                                emit=lambda kind, fields: run.event(kind, **fields),
                                started=lambda pid: run.set_step("train", pid=pid), updates=updates)
        gpu_seconds = round(time.monotonic() - began, 1)
        (run.data / "train-repeated.jsonl").unlink()  # rebuilt exactly from train.jsonl on demand
        write_json(run.folder / "train.json", {**result, "trainer_commit": self.config.trainer_commit,
                                               "gpu_seconds": gpu_seconds, "passes": passes})
        return {"gpu_seconds": gpu_seconds, "steps": result["steps"], "trainer_commit": self.config.trainer_commit}

    def adapter(self, run: Run) -> Path:
        return Path(read_json(run.folder / "train.json")["adapter"])

    def do_calibrate(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        result = evaluation.calibrate(self.worker.base, self.adapter(run), run.data / "calibration.jsonl", run.folder,
                                      self.worker.score)
        self.save_metric(run, "calibration", result)
        return {"gpu_seconds": result["seconds"], "temperature": result["temperature"]}

    def do_evaluate(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        result = evaluation.evaluate(self.worker.base, self.adapter(run), run.data / "test.jsonl", run.folder,
                                     evaluation.SYNTHETIC, self.worker.score)
        self.save_metric(run, "synthetic", result)
        return {"gpu_seconds": result["seconds"], "accuracy": result["accuracy"], "interval": result["interval"],
                "ece": result["ece"], "label": result["label"]}

    def do_independent_test(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        if not run.state["independent_test"]:
            raise Conflict("Upload an independent test set first")
        result = evaluation.evaluate(self.worker.base, self.adapter(run), run.data / "independent.jsonl", run.folder,
                                     evaluation.INDEPENDENT, self.worker.score)
        self.save_metric(run, "independent", result)
        return {"gpu_seconds": result["seconds"], "accuracy": result["accuracy"], "interval": result["interval"],
                "ece": result["ece"], "label": result["label"]}

    def save_metric(self, run: Run, name: str, value: dict[str, Any]) -> None:
        path = run.folder / "metrics.json"
        metrics = read_json(path) if path.exists() else {}
        metrics[name] = value
        write_json(path, metrics)

    def do_publish(self, run: Run, teacher: Teacher) -> dict[str, Any]:
        """The gate, as in jeff-server (evaluated, a minimum accuracy set and reached), then publishing
        (jeff_server.factory.publish): the adapter folder with its manifest, the engines serving it, an example
        answered through Jeff."""
        state = run.state
        metrics = read_json(run.folder / "metrics.json")
        if "synthetic" not in metrics:
            raise RuntimeError("The held-out synthetic test has not run; an adapter is never published unevaluated")
        if state["independent_test"] and "independent" not in metrics:
            raise RuntimeError("An independent test set was uploaded but not evaluated")
        gate = state["gate"]
        if gate is None:
            raise RuntimeError("Set the minimum accuracy to publish (the gate) first")
        judged = metrics.get("independent") or metrics["synthetic"]
        if judged["accuracy"] < gate["min_accuracy"]:
            raise RuntimeError(f"Accuracy {judged['accuracy']:.1%} on the {judged['label']} is below the gate "
                               f"{gate['min_accuracy']:.1%}; publishing is blocked")
        return publish_module.publish(run, self.worker, metrics, self.adapter(run), data_card(run, run.spec, metrics))


def alive(pid: int) -> bool:
    """Whether process `pid` exists (signal 0 only asks); one under another user exists too."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def spec_name(folder: Path) -> str | None:
    path = folder / "spec.json"
    return read_json(path)["name"] if path.exists() else None


def data_card(run: Run, spec: dict[str, Any], metrics: dict[str, Any]) -> dict[str, Any]:
    state = run.state
    train = read_json(run.folder / "train.json")
    adapter_config = read_json(Path(train["adapter"]) / "decision_config.json")
    checks = read_json(run.folder / "checks.json")
    rows = read_jsonl(run.data / "train.jsonl")
    return {
        "adapter": spec["name"], "run": run.id, "created": state["created"], "spec": spec,
        "spec_sha256": state["spec_sha256"], "description": state["description"],
        "teacher_models": sorted({row["source"]["teacher"] for row in rows}),
        "rows": checks["rows"], "checks": {key: value for key, value in checks.items() if key != "sample"},
        "recipe": {"trainer_commit": train["trainer_commit"], "updates": spec["sizes"]["updates"], "batch": spec_module.BATCH,
                   "lora_rank": 16, "lora_alpha": 32, "lr": 2e-4, "readout_lr": 5e-6, "weight_decay": 0.01,
                   "replay": False, "prompt_layout": "live-last", "training_seed": training.SEED, "data_seed": SEED},
        "base": adapter_config["adapter"]["base_checkpoint"],
        "temperature": adapter_config["temperature"],
        "metrics": {name: {key: value for key, value in result.items() if key not in ("mistakes", "reliability")}
                    for name, result in metrics.items()},
    }


def parse_test_set(spec: dict[str, Any], content: str, kind: str) -> list[dict[str, Any]]:
    """An independent labelled test set: JSON Lines of {"state": {...}, "question_id": ..., "label": option key,
    "call_id": optional}, or CSV with columns question_id, label, optional call_id, and one column per state field
    (fixed fields, then the changing field). Rows with the same call_id are one command: whole-call exact match is
    scored when every row has one."""
    fields = [field["name"] for field in spec["family"]["fields"]] + [spec["live_field"]["name"]]
    if kind == "jsonl":
        items = [json.loads(line) for line in content.splitlines() if line.strip()]
    elif kind == "csv":
        reader = csv.DictReader(io.StringIO(content))
        missing = {"question_id", "label", *fields} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"The CSV lacks the columns {', '.join(sorted(missing))}")
        items = [{"state": {name: row[name] for name in fields}, "question_id": row["question_id"], "label": row["label"],
                  **({"call_id": row["call_id"]} if row.get("call_id") else {})} for row in reader]
    else:
        raise ValueError("Upload csv or jsonl")
    questions = {question["id"]: question for question in spec["questions"]}
    rows = []
    for number, item in enumerate(items, 1):
        question = questions.get(item.get("question_id"))
        if question is None:
            raise ValueError(f"Line {number}: question_id must be one of {', '.join(questions)}")
        keys = [option["key"] for option in question["options"]]
        if item.get("label") not in keys:
            raise ValueError(f"Line {number}: label {item.get('label')!r} is not an option of {question['id']} ({', '.join(keys)})")
        state = item.get("state")
        if not isinstance(state, dict) or list(state) != fields:
            raise ValueError(f"Line {number}: state must have exactly the fields {', '.join(fields)}, in that order")
        label = spec_module.label_of(question, item["label"])
        rows.append({"id": f"independent:{number:05d}:{question['id']}", "suite": spec["name"], "family": "independent",
                     "state": state, "question": spec_module.question_payload(question), "label": label, "target": label,
                     "source": {"dataset": "independent", "question_id": question["id"], "line": number,
                                **({"call_id": str(item["call_id"])} if item.get("call_id") is not None else {})}})
    if not rows:
        raise ValueError("The test set is empty")
    return rows


def parse_calls(spec: dict[str, Any], content: str, signatures: dict[str, Any]) -> list[dict[str, Any]]:
    """An independent test set of commands with their correct calls, JSON Lines of
    {"id": optional, "command": the request text, "function": ..., "arguments": {closed-set argument: value}}.
    Every call is checked against the functions' value lists and defaults (importing.call_answers) before any
    scoring; one row per question the call implies, all rows of a command sharing its call_id."""
    questions = {question["id"]: question for question in spec["questions"]}
    rows: list[dict[str, Any]] = []
    problems: list[str] = []
    seen: set[str] = set()
    for number, line in enumerate((line for line in content.splitlines() if line.strip()), 1):
        item = json.loads(line)
        call_id = str(item.get("id", number))
        if call_id in seen:
            problems.append(f"line {number}: id {call_id} is used twice")
            continue
        seen.add(call_id)
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            problems.append(f"line {number}: no command")
            continue
        try:
            answers = importing.call_answers(item, signatures)
        except ValueError as error:
            problems.append(f"line {number} ({command[:40]!r}): {error}")
            continue
        for qid, key in answers.items():
            question = questions[qid]
            label = spec_module.label_of(question, key)
            rows.append({"id": f"independent:{call_id}:{qid}", "suite": spec["name"], "family": "independent",
                         "state": command, "question": spec_module.question_payload(question), "label": label,
                         "target": label, "source": {"dataset": "independent", "question_id": qid, "call_id": call_id,
                                                     "line": number, "call": {"function": item["function"],
                                                                              "arguments": item.get("arguments") or {}}}})
    if problems:
        raise ValueError(f"{len(problems)} calls do not fit the functions; nothing was uploaded:\n" + "\n".join(problems[:20]))
    if not rows:
        raise ValueError("The test set is empty")
    return rows
