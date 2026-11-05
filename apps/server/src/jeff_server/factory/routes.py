"""The /v1/factory endpoints: jeff-server's factory router, behind the server's own credentials. Every route is the
owner's (jeff_server.operations.owner: the API key or a console session; a hub token is a customer's, 403
NotLicensed). Build endpoints also need JEFF_FACTORY=owner, and answer 403 FactoryOff without it; the mode and the
replays are also offered with the factory off, and so are description checks and previews when the server has the
teacher settings.

The API never runs a build step: starting a run or a step queues a job for the factory worker
(jeff_server.factory.queue, jeff_server.factory.worker), and GET /v1/factory/worker says whether a worker is
running. Two kinds of teacher work run in the API instead, since they cost no GPU time and the person is waiting:
a description check (one synchronous teacher call) and a preview's interpretation (a background thread, at most
four at once in each API process; a fifth is 409 Conflict, before any run folder is made). A preview still
interpreting when its API process stopped is failed when the API starts again ("the server restarted during
interpret"); one a live sibling API process is interpreting is left alone (Factory.mark_interrupted).

Errors: a spec or request the factory refuses (a ValueError from its checks) is 422 InvalidRequest with the message
unchanged; a request that does not fit the run's state is 409 Conflict; a run, test set, reference or replay that
does not exist is 422 InvalidRequest naming it; a teacher that fails during a description check (unreachable, or its
reply still invalid after the retries) is 500 EngineError."""

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, TypeVar

from fastapi import APIRouter, Depends, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from jeff.errors import EngineError, FactoryOff, InvalidRequest

from jeff_server.auth import V1_DEPENDENCIES
from jeff_server.factory import describe, importing, queue, replay, testset as testset_module
from jeff_server.factory.config import FactoryConfig
from jeff_server.factory.replay import public_state
from jeff_server.factory.runner import Factory
from jeff_server.factory.store import Run, read_json
from jeff_server.factory.teacher import TeacherError
from jeff_server.factory.worker import worker_status
from jeff_server.operations import owner

T = TypeVar("T")


# The request bodies. Their JSON Schemas (with these descriptions) are the request bodies in contract/openapi.yaml
# (scripts/build_openapi.py).

class CheckDescription(BaseModel):
    """A task description for the teacher to check."""
    model_config = ConfigDict(extra="forbid")
    description: str = Field(description="What the adapter should decide, in plain English.")
    examples: str | None = Field(None, description="Example cases with their answers, as free text.")


class StartRun(BaseModel):
    """A run (or preview) started from a description check, with the description the person confirmed."""
    model_config = ConfigDict(extra="forbid")
    check_id: str = Field(description="The id of the description check (POST /v1/factory/description-checks).")
    choice: Literal["original", "improved", "edited"] = Field(
        description="Which description is sent: the person's own, the teacher's improved one, or an edited one.")
    description: str = Field(description="The confirmed description, exactly as checked unless choice is edited.")
    name: str | None = Field(None, description="The adapter's name: lower case words joined by hyphens.")


class SpecBody(BaseModel):
    """An edited task spec."""
    model_config = ConfigDict(extra="forbid")
    spec: dict[str, Any] = Field(description="The whole task spec (name, summary, family, live_field, questions, "
                                             "hard_cases, styles, sizes), as GET returns it.")


class ImportRun(BaseModel):
    """A run (or preview) from an imported question set."""
    model_config = ConfigDict(extra="forbid")
    reference: str = Field(description="The question set's name (GET /v1/factory/references).")
    name: str = Field(description="The adapter's name: lower case words joined by hyphens.")
    description: str = Field(description="The short task description: who writes the requests, the hard patterns.")


class ReviewBody(BaseModel):
    """The owner's verdict on one item of a test set."""
    model_config = ConfigDict(extra="forbid")
    item_id: str = Field(description="The item's id.")
    verdict: Literal["confirm", "correct", "drop"] = Field(description="Keep the item, correct its call, or drop it.")
    call: dict[str, Any] | None = Field(None, description="The corrected call ({function, arguments}), with verdict "
                                                          "correct.")


class FreezeBody(BaseModel):
    """Freezing a test set."""
    model_config = ConfigDict(extra="forbid")
    waiver: str | None = Field(None, description="The owner's instruction to freeze without the owner's checks, "
                                                 "quoted.")


class FromRunBody(BaseModel):
    """A new run from an earlier run's approved spec."""
    model_config = ConfigDict(extra="forbid")
    name: str = Field(description="The new adapter's name: lower case words joined by hyphens.")
    sizes: dict[str, int] = Field(description="Rows per split, training updates and families: train, updates, dev, "
                                              "calibration, test, families.")


class GateBody(BaseModel):
    """The minimum accuracy to publish."""
    model_config = ConfigDict(extra="forbid")
    min_accuracy: float = Field(description="A share between 0 and 1, for example 0.9.")


class TestSetBody(BaseModel):
    """An independent labelled test set."""
    model_config = ConfigDict(extra="forbid")
    format: Literal["csv", "jsonl", "calls"] = Field(description="CSV or JSON Lines of labelled states, or JSON "
                                                                 "Lines of commands with their correct calls.")
    content: str = Field(description="The file's text.")


class FactoryState:
    """The factory as the API sees it, made once when the app is created: the settings, the factory (owner mode),
    whose build steps are queued for the worker, and the preview factory (whenever the server has teacher settings,
    also with JEFF_FACTORY=off), whose interpretations run in this process. Neither has worker hooks. Previews this
    process's previous run left interpreting are failed now."""

    def __init__(self, config: FactoryConfig) -> None:
        self.config = config
        self.factory = Factory(config, None) if config.mode == "owner" else None
        self.preview = Factory(config, None, preview=True) if config.can_check else None
        if self.preview is not None:
            self.preview.mark_interrupted("the server restarted")


router = APIRouter(prefix="/v1/factory", dependencies=[*V1_DEPENDENCIES, Depends(owner)])


def state_of(request: Request) -> FactoryState:
    state: FactoryState = request.app.state.factory
    return state


def built(request: Request) -> Factory:
    """The owner-mode factory, or 403 FactoryOff."""
    factory = state_of(request).factory
    if factory is None:
        raise FactoryOff("Building adapters is switched off on this server (JEFF_FACTORY=off).")
    return factory


def previewer(request: Request) -> Factory:
    preview = state_of(request).preview
    if preview is None:
        raise FactoryOff("Trying the factory is not set up on this server (no teacher settings).")
    return preview


def handle(work: Callable[[], T]) -> T:
    """The factory's own checks raise ValueError naming the problem: 422 InvalidRequest, with the message unchanged.
    Conflict and FactoryOff are JeffErrors already."""
    try:
        return work()
    except ValueError as error:
        raise InvalidRequest(str(error)) from error


def run_of(factory: Factory, run_id: str) -> Run:
    try:
        return factory.run(run_id)
    except KeyError as error:
        raise InvalidRequest(f"No run {run_id!r}") from error


def job_of(factory: Factory, run: Run) -> dict[str, Any] | None:
    """A build run's job, waiting or running; None for a preview, which is never queued."""
    if factory.preview:
        return None
    found = queue.job_of(factory.config.required_root, run.id)
    return None if found is None else {**found[0].public(), "state": found[1]}


def busy(factory: Factory) -> str | None:
    """The run whose job the worker has claimed now, as jeff-server's "busy"; None for previews, which are not
    queued."""
    if factory.preview:
        return None
    claimed = queue.claimed(factory.config.required_root)
    return claimed[0].run if claimed else None


def view(factory: Factory, run: Run) -> dict[str, Any]:
    files = {name: read_json(run.folder / f"{name}.json") for name in ("checks", "metrics", "train", "publish")
             if (run.folder / f"{name}.json").exists()}
    spec = read_json(run.folder / "spec.json") if (run.folder / "spec.json").exists() else None
    return {"run": public_state(run.state) | {"unclean_reasons": run.state["unclean_reasons"]}, "spec": spec,
            "rest": factory.rest(run) if run.state["approved"] else [], "busy": busy(factory),
            "job": job_of(factory, run), **files}


@router.get("/mode")
def mode(request: Request) -> dict[str, Any]:
    state = state_of(request)
    return {"mode": state.config.mode, "replays": state.config.root is not None,
            "description_check": state.config.can_check, "preview": state.preview is not None}


@router.get("/worker")
def worker(request: Request) -> dict[str, Any]:
    """Whether the factory worker is running (a heartbeat at most 15 seconds old), and the job it runs now."""
    return worker_status(built(request).config.required_root)


@router.post("/description-checks")
def check_description(body: CheckDescription, request: Request) -> dict[str, Any]:
    """The teacher checks a description and proposes an improved one; nothing starts until a run is started from
    the check with the confirmed text (owner mode only). Also offered with building switched off, when the server
    has teacher settings."""
    config = state_of(request).config
    if not config.can_check:
        raise FactoryOff("Description checks are not set up on this server (no teacher settings)")
    try:
        # The only ValueError outside the teacher call is the caller's (an empty description): handle() makes it
        # 422. Teacher.ask retries an invalid reply (ValueError, EmptyAnswer) and raises InvalidReply once the
        # retries are spent: a TeacherError, like a teacher that cannot be reached.
        return handle(lambda: describe.run_check(config, body.description, body.examples))
    except TeacherError as error:
        raise EngineError(f"The teacher failed: {type(error).__name__}: {error}") from error


@router.post("/runs")
def start(body: StartRun, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = handle(lambda: factory.start(body.check_id, body.choice, body.description, body.name))
    return view(factory, run)


@router.get("/references")
def references(factory: Factory = Depends(previewer)) -> dict[str, Any]:
    """Question sets that can be imported verbatim (JEFF_FACTORY_DIR/references/)."""
    return {"references": handle(factory.references)}


@router.get("/references/{name}/files/{file_path:path}")
def reference_file(name: str, file_path: str, factory: Factory = Depends(previewer)) -> FileResponse:
    """One file of a reference, to download and inspect (questions, conditions, signatures, the original sources)."""
    folder = factory.config.folder("references") / name
    if "/" in name or name.startswith(".") or not folder.is_dir() or file_path not in importing.bundle_files(folder):
        raise InvalidRequest(f"No file {file_path!r} in reference {name!r}")
    return FileResponse(folder / file_path, filename=Path(file_path).name, media_type="text/plain; charset=utf-8")


# Previews: the cheap steps only (check, interpret or import, edit, approve). There is no endpoint that generates
# data or trains for a preview.
@router.post("/previews")
def preview_start(body: StartRun, factory: Factory = Depends(previewer)) -> dict[str, Any]:
    return view(factory, handle(lambda: factory.start(body.check_id, body.choice, body.description, body.name)))


@router.post("/previews/import")
def preview_import(body: ImportRun, factory: Factory = Depends(previewer)) -> dict[str, Any]:
    return view(factory, handle(lambda: factory.start_import(body.reference, body.name, body.description)))


@router.get("/previews/{run_id}")
def preview_one(run_id: str, factory: Factory = Depends(previewer)) -> dict[str, Any]:
    return view(factory, run_of(factory, run_id))


@router.put("/previews/{run_id}/spec")
def preview_spec(run_id: str, body: SpecBody, factory: Factory = Depends(previewer)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    handle(lambda: factory.save_spec(run, body.spec))
    return view(factory, run)


@router.post("/previews/{run_id}/approve")
def preview_approve(run_id: str, factory: Factory = Depends(previewer)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    handle(lambda: factory.approve(run))
    return view(factory, run)


@router.get("/previews/{run_id}/events")
def preview_events(run_id: str, request: Request, after: int = 0,
                   factory: Factory = Depends(previewer)) -> StreamingResponse:
    return stream_events(run_of(factory, run_id), request, after)


@router.post("/runs/import")
def start_import(body: ImportRun, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = handle(lambda: factory.start_import(body.reference, body.name, body.description))
    return view(factory, run)


@router.get("/testsets")
def testsets(factory: Factory = Depends(built)) -> dict[str, Any]:
    return {"testsets": factory.testsets()}


def testset_of(factory: Factory, name: str) -> Path:
    try:
        return factory.testset_folder(name)
    except KeyError as error:
        raise InvalidRequest(f"No test set {name!r}") from error


@router.get("/testsets/{name}")
def testset(name: str, factory: Factory = Depends(built)) -> dict[str, Any]:
    folder = testset_of(factory, name)
    return {**testset_module.load(folder), "signatures": testset_module.signatures_of(folder),
            "min_owner_checks": testset_module.MIN_OWNER_CHECKS}


@router.post("/testsets/{name}/review")
def testset_review(name: str, body: ReviewBody, factory: Factory = Depends(built)) -> dict[str, Any]:
    folder = testset_of(factory, name)
    try:
        return handle(lambda: testset_module.review(folder, body.item_id, body.verdict, body.call,
                                                    testset_module.signatures_of(folder)))
    except KeyError as error:
        raise InvalidRequest(f"No item {body.item_id!r}") from error


@router.post("/testsets/{name}/freeze")
def testset_freeze(name: str, body: FreezeBody | None = None, factory: Factory = Depends(built)) -> dict[str, Any]:
    """Freeze after the owner's checks, or with `waiver`: the owner's instruction to freeze without them, quoted
    (agreed and cookbook items kept as they are, disputed ones dropped, all of it recorded)."""
    folder = testset_of(factory, name)
    return handle(lambda: testset_module.freeze(folder, testset_module.signatures_of(folder),
                                                body.waiver if body else None))


@router.post("/runs/{run_id}/testset/{name}")
def attach_testset(run_id: str, name: str, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    try:
        handle(lambda: factory.attach_testset(run, name))
    except KeyError as error:
        raise InvalidRequest(f"No test set {name!r}") from error
    return view(factory, run)


@router.post("/runs/{run_id}/again")
def start_again(run_id: str, body: FromRunBody, factory: Factory = Depends(built)) -> dict[str, Any]:
    """A new run with this run's approved spec, changing only the adapter name and the sizes."""
    run_of(factory, run_id)
    return view(factory, handle(lambda: factory.start_from_run(run_id, body.name, body.sizes)))


@router.get("/runs")
def runs(factory: Factory = Depends(built)) -> dict[str, Any]:
    return {"runs": factory.runs(), "busy": busy(factory)}


@router.get("/runs/{run_id}")
def one(run_id: str, factory: Factory = Depends(built)) -> dict[str, Any]:
    return view(factory, run_of(factory, run_id))


@router.put("/runs/{run_id}/spec")
def save_spec(run_id: str, body: SpecBody, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    handle(lambda: factory.save_spec(run, body.spec))
    return view(factory, run)


@router.post("/runs/{run_id}/approve")
def approve(run_id: str, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    handle(lambda: factory.approve(run))
    return view(factory, run)


@router.post("/runs/{run_id}/steps/{step}")
def run_step(run_id: str, step: str, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    handle(lambda: factory.run_steps(run, [step]))
    return view(factory, run)


@router.post("/runs/{run_id}/run-rest")
def run_rest(run_id: str, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    handle(lambda: factory.run_steps(run, factory.rest(run)))
    return view(factory, run)


@router.put("/runs/{run_id}/gate")
def gate(run_id: str, body: GateBody, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    handle(lambda: factory.set_gate(run, body.min_accuracy))
    return view(factory, run)


@router.post("/runs/{run_id}/test-set")
def test_set(run_id: str, body: TestSetBody, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    handle(lambda: factory.upload_test(run, body.content, body.format))
    return view(factory, run)


@router.post("/runs/{run_id}/replay")
def save_replay(run_id: str, factory: Factory = Depends(built)) -> dict[str, Any]:
    run = run_of(factory, run_id)
    return handle(lambda: replay.save(run, factory.config.replays))


@router.get("/runs/{run_id}/events")
def events(run_id: str, request: Request, after: int = 0, factory: Factory = Depends(built)) -> StreamingResponse:
    """Server-sent events: every line of the run's events.jsonl from line `after` on, then new lines as written."""
    return stream_events(run_of(factory, run_id), request, after)


def complete_lines(text: str) -> list[str]:
    """The lines of `text` up to its last newline: another process (the worker) may be halfway through appending
    the next one."""
    return text[:text.rfind("\n") + 1].splitlines()


def stream_events(run: Run, request: Request, after: int) -> StreamingResponse:
    path = run.folder / "events.jsonl"

    async def stream() -> Any:
        sent = after
        while not await request.is_disconnected():
            lines = complete_lines(path.read_text(encoding="utf-8") if path.exists() else "")
            for line in lines[sent:]:
                sent += 1
                yield f"id: {sent}\ndata: {line}\n\n"
            yield ": keep-alive\n\n"
            await asyncio.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@router.get("/replays")
def replays(request: Request) -> dict[str, Any]:
    return {"replays": replay.listing(replays_folder(request))}


@router.get("/replays/{replay_id}")
def replay_detail(replay_id: str, request: Request) -> dict[str, Any]:
    try:
        return replay.detail(replays_folder(request), replay_id)
    except KeyError as error:
        raise InvalidRequest(f"No replay {replay_id!r}") from error


def replays_folder(request: Request) -> Path:
    config = state_of(request).config
    if config.root is None:
        raise FactoryOff("This server keeps no replays: JEFF_FACTORY_DIR is not set.")
    return config.replays
