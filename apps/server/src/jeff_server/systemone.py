"""POST /v1/systemone and GET /v1/models: jeff-server's API, kept so existing clients work unchanged.

A request is {model, state, questions, images?, orders?} (contract/schemas/systemone_request): one state and one or
more questions about it, each answered on its own. The response is {model, answers, usage, timing}
(contract/schemas/systemone_response), with jeff-server's answer shapes and confidence formulas (jeff.answers) and,
for orders 2, its two-order averaging (jeff.orders): every question is also asked with its options reversed, both
passes go to the engine in one request, and each question's two distributions are averaged.

`model` names the base -- "jeff", "jeff-latest", "jeff-qwen3.8-27b" (accepted for clients written against v1.0 and
v1.1) or the server's JEFF_MODEL_NAME -- or an adapter in the catalogue, by its ID or an alias. The base runs as the
engine adapter "base" (jeff.engine.BASE_ADAPTER). The response's model is JEFF_MODEL_NAME for the base and the name
the request used for an adapter, as jeff-server answered.

Checked in this order: the body (a choice question with more than 255 options or a score question with more than
10 levels is 422 TooManyLabels, as jeff-server's client expects; anything else not matching the schema is 422
InvalidRequest naming the first problem; also orders that are not a whole number and images that are not base64 or
over 8 MB), the model (422 InvalidRequest for a name that is neither the base nor a catalogue adapter, answered from
the catalogue without asking the engines), the caller's licence for a proprietary adapter (403 NotLicensed, as for
/v1/decide, before the engine sees a row), whether the engines serve it (422 InvalidRequest, as jeff-server answered
a model it did not serve), then, for an account caller, its allowance (402 UsageLimitReached, just before the
engine is asked; the reply's input tokens are counted on its account). The engine then refuses a choice question with
more options than the model was trained on (422 TooManyLabels, re-worded here to name the question and the model) or
a question longer than its token limit (422 InputTooLong), and a full engine queue answers 529 Busy with Retry-After
(jeff-server's JEFF_QUEUE_MS wait is gone: the engine's queue does that job).

Errors from these two endpoints carry, beside the standard {"error": {...}}, jeff-server's "detail" (LegacyRoute):
for a problem with the model the list [{"loc": ["body", "model"], "msg", "type": "value_error"}], otherwise the
message as a string -- the forms jeff-server's client reads to raise UnknownModel, TooManyOptions and the rest.

Differences from jeff-server that a client can see: the error body also has "error"; timing carries the engine's
fields (model_ms, the engines used) and server_ms, not jeff-server's path and prefix fields; release_date in GET
/v1/models is the base's from the catalogue's bases.yaml ("released"), not the checkpoint file's date."""

import base64
import binascii
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from collections.abc import Callable, Coroutine

from fastapi import APIRouter, Request, Response
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool

from jeff import contract
from jeff.answers import answer
from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.engine import BASE_ADAPTER, Engine, serves
from jeff.errors import EngineError, InvalidRequest, JeffError, ManifestError, TooManyLabels
from jeff.manifest import Manifest
from jeff.orders import average_orders, reverse_row

from jeff_server.access import visible_catalogue, visible_jeff
from jeff_server.auth import V1_DEPENDENCIES, Caller, check_adapter_licence, request_caller
from jeff_server.decide import caller_view, read_body
from jeff_server.errors import error_response
from jeff_server.router import EngineRouter
from jeff_server.settings import BASE_NAMES, LEGACY_NAME

if TYPE_CHECKING:
    from jeff_server.catalogue_refresh import CatalogueRefresher

# The end of every error message about the request's model; legacy_detail gives those jeff-server's model detail.
AT_MODEL = "(at /model)"


def legacy_detail(error: JeffError) -> Any:
    """jeff-server's "detail" for an error, in the form its client reads: a problem with the model as a
    Pydantic-style list located at the body's model (UnknownModel), anything else as the message string (which it
    searches for "options, but this model handles at most" to raise TooManyOptions)."""
    message = str(error)
    if isinstance(error, InvalidRequest) and message.endswith(AT_MODEL):
        return [{"loc": ["body", "model"], "msg": message, "type": "value_error"}]
    return message


class LegacyRoute(APIRoute):
    """A route whose JeffErrors -- its own, and its dependencies' (credentials, rate limits) -- are answered with
    jeff-server's "detail" added to the standard error body."""

    def get_route_handler(self) -> Callable[[Request], Coroutine[Any, Any, Response]]:
        handler = super().get_route_handler()

        async def legacy(request: Request) -> Response:
            try:
                return await handler(request)
            except JeffError as error:
                return error_response(request, error, legacy_detail=legacy_detail)

        return legacy


router = APIRouter(dependencies=V1_DEPENDENCIES, route_class=LegacyRoute)

BASE_DESCRIPTION = "Local Jeff text and image decisions."
ADAPTER_DESCRIPTION = "A LoRA adapter on the base model."
MAX_IMAGE_BYTES = 8_000_000


@dataclass(frozen=True)
class Target:
    engine_id: str  # the engine adapter id: BASE_ADAPTER or the adapter's manifest ID
    name: str  # the response's model
    manifest: Manifest | None  # None for the base


# --- the request -----------------------------------------------------------------------------------------------------

def _image_matches(kind: str, content: bytes) -> bool:
    if kind == "png":
        return content.startswith(b"\x89PNG\r\n\x1a\n")
    if kind == "jpeg":
        return content.startswith(b"\xff\xd8\xff")
    return content[:4] == b"RIFF" and content[8:12] == b"WEBP"


def _check_image(value: str, number: int) -> None:
    """The schema has checked the data URL's header; here the data: base64, at most 8 MB, and the image kind the
    header says (the engine opens it and refuses one it cannot read or over 16 million pixels)."""
    header, _, encoded = value.partition(",")
    kind = header.removeprefix("data:image/").removesuffix(";base64")
    try:
        content = base64.b64decode(encoded, validate=True)
    except binascii.Error as error:
        raise InvalidRequest(f"image {number} is not valid base64: {error} (at /images/{number - 1})") from error
    if len(content) > MAX_IMAGE_BYTES:
        raise InvalidRequest(f"image {number} is {len(content)} bytes; each image must be at most 8 MB "
                             f"(at /images/{number - 1})")
    if not _image_matches(kind, content):
        raise InvalidRequest(f"image {number} is not a {kind.upper()} image, as its data URL says "
                             f"(at /images/{number - 1})")


# jeff-server's schema: at most 255 choice options and 10 score levels; its client raises more as TooManyOptions.
MOST_OPTIONS = {"choice": (dict, 255), "score": (list, 10)}


def too_many(key: str, count: int, limit: int, why: str) -> TooManyLabels:
    """jeff-server's wording; its client looks for "options, but this model handles at most"."""
    return TooManyLabels(f"Question {key!r} has {count} options, but this model handles at most {limit}{why}. "
                         "Shortlist the options first, or split the question.")


def _refuse_too_many_options(body: object) -> None:
    if not isinstance(body, dict) or not isinstance(body.get("questions"), dict):
        return  # the schema check names what is wrong
    for key, question in body["questions"].items():
        if not isinstance(question, dict) or question.get("type") not in MOST_OPTIONS:
            continue
        kind, limit = MOST_OPTIONS[question["type"]]
        criteria = question.get("criteria")
        if isinstance(criteria, kind) and len(criteria) > limit:
            why = " (a score question has 2 to 10 levels)" if question["type"] == "score" else ""
            raise too_many(key, len(criteria), limit, why)


def check_body(body: object) -> dict[str, Any]:
    _refuse_too_many_options(body)
    try:
        problem = None if contract.is_valid("systemone_request", body) else \
            contract.first_problem("systemone_request", body)
    except RecursionError as error:
        raise InvalidRequest("The request body is JSON nested too deeply to check") from error
    if problem is not None:
        raise InvalidRequest(f"The request body does not match POST /v1/systemone: {problem}")
    if not isinstance(body, dict):
        raise InvalidRequest(f"The request body must be a JSON object; got {type(body).__name__}")
    # JSON Schema counts 2.0 as an integer; jeff-server took orders as a strict whole number.
    if "orders" in body and type(body["orders"]) is not int:
        raise InvalidRequest(f"orders must be the whole number 1 or 2, not {body['orders']!r} (at /orders)")
    for number, image in enumerate(body.get("images", []), start=1):
        _check_image(image, number)
    return body


# --- the model -------------------------------------------------------------------------------------------------------

def _public_name(manifest: Manifest) -> str:
    return manifest.aliases[0] if manifest.aliases else manifest.id


def _served(catalogue: Catalogue, installed: set[str]) -> list[Manifest]:
    return sorted((manifest for manifest in catalogue.manifests() if manifest.id in installed), key=_public_name)


def check_model_name(catalogue: Catalogue, model_name: str) -> None:
    """At startup: JEFF_MODEL_NAME must not also name a catalogue adapter, or /v1/systemone could not tell the base
    from it."""
    for manifest in catalogue.manifests():
        if model_name == manifest.id or model_name in manifest.aliases:
            raise InvalidRequest(f"JEFF_MODEL_NAME={model_name} is also the name of the adapter {manifest.id}; "
                                 "/v1/systemone could not tell the base from it. Choose another name for the base")


def resolve(catalogue: Catalogue, model_name: str, model: str) -> Target:
    """From the names alone: the engines are not asked, so an unknown model is the caller's 422 even while the
    engines are down or disagree."""
    if model in BASE_NAMES or model == model_name:
        return Target(BASE_ADAPTER, model_name, None)
    for manifest in catalogue.manifests():
        if model == manifest.id or model in manifest.aliases:
            return Target(manifest.id, model, manifest)
    names = sorted(_public_name(manifest) for manifest in catalogue.manifests())
    adapters = f", or an adapter: {', '.join(names)}" if names else ""
    raise InvalidRequest(f"Unknown model {model!r}. Use {model_name} or jeff-latest{adapters} {AT_MODEL}")


def _check_served(engine: Engine, target: Target, model: str) -> None:
    """Not served (jeff.engine.serves, which asks the engine afresh when its cached list says no) is an unknown
    model, as jeff-server answered. The base is always served by a jeff-runtime engine; the engine itself answers
    otherwise."""
    if target.manifest is None:
        return
    if not serves(engine, target.engine_id):
        raise InvalidRequest(f"Unknown model {model!r}: the adapter {target.engine_id} is in the catalogue but not "
                             f"served by this server; serve it with PUT /v1/adapters/served {AT_MODEL}")


def _named_too_many(error: TooManyLabels, questions: dict[str, dict[str, Any]], target: Target) -> TooManyLabels:
    """The engine's TooManyLabels (it knows the limit, not the question keys) re-worded with the question and the
    model, keeping jeff-server's wording. An error without the limit is the engine's own and is passed on."""
    details = vars(error)
    if "limit" not in details:
        return error
    limit = int(details["limit"])
    for key, question in questions.items():
        if question["type"] == "choice" and len(question["criteria"]) > limit:
            return too_many(key, len(question["criteria"]), limit,
                            f" ({target.name} was trained on questions of up to {limit} options)")
    return error


# --- the answer ------------------------------------------------------------------------------------------------------

def _answer(planner: "CatalogueRefresher", jeff: Jeff, model_name: str, caller: Caller, body: dict[str, Any],
            started: float) -> dict[str, Any]:
    """An unknown model may be an adapter just added on the engines: resolve is tried once more on a rebuilt
    catalogue if the engines' adapters changed (CatalogueRefresher.retry_unknown; resolve's only error is the
    unknown model)."""
    # On the caller's view of the catalogue (jeff_server.access): an adapter it may not use is an unknown model, and
    # the unknown model's list of adapters leaves it out.
    target = planner.retry_unknown(jeff, lambda planned: resolve(visible_catalogue(caller, planned.catalogue),
                                                                 model_name, body["model"]),
                                   unknown=(InvalidRequest,))
    # The caller's view: for an account, its engine checks the allowance at the predict and counts its tokens.
    engine = caller_view(jeff, caller).engine
    if target.manifest is not None:
        check_adapter_licence(caller, target.manifest, f"model {body['model']!r}")
    _check_served(engine, target, body["model"])
    # A question's null instructions or criteria are the same as leaving them out (jeff-server's exclude_none).
    questions = {key: {field: value for field, value in question.items() if value is not None}
                 for key, question in body["questions"].items()}
    images = body.get("images", [])
    rows = [{"state": body["state"], "question": question, **({"images": images} if images else {})}
            for question in questions.values()]
    orders = body.get("orders", 1)
    sent = rows + [reverse_row(row) for row in rows] if orders == 2 else rows
    try:
        result = engine.predict(target.engine_id, sent)
    except TooManyLabels as error:
        named = _named_too_many(error, questions, target)
        if named is error:
            raise
        raise named from error
    if len(result.probabilities) != len(sent):
        raise EngineError(f"The engine answered {len(result.probabilities)} of {len(sent)} rows")
    if result.input_tokens is None:
        raise EngineError("The engine answered without input token counts, which /v1/systemone reports as "
                          "usage.input_tokens; run engines from a jeff-runtime that reports them")
    values = result.probabilities
    try:
        if orders == 2:
            values = average_orders(list(questions.values()), values[:len(rows)], values[len(rows):])
        answers = {key: answer(question, probabilities)
                   for (key, question), probabilities in zip(questions.items(), values, strict=True)}
    except ValueError as error:
        raise EngineError(f"The engine's probabilities do not fit the questions: {error}") from error
    timing = {**result.timing, "server_ms": round((time.perf_counter() - started) * 1000, 3)}
    return {"model": target.name, "answers": answers,
            "usage": {"input_tokens": sum(result.input_tokens), "output_tokens": 0, "orders": orders},
            "timing": timing}


@router.post("/v1/systemone")
async def systemone(request: Request) -> dict[str, Any]:
    started = time.perf_counter()
    body = check_body(await read_body(request))
    planner: "CatalogueRefresher" = request.app.state.planner
    # The engine is called synchronously (and asked what it serves); run it off the event loop.
    return await run_in_threadpool(_answer, planner, await planner.ready(), request.app.state.settings.model_name,
                                   request_caller(request), body, started)


# --- GET /v1/models --------------------------------------------------------------------------------------------------

def release_date(catalogue: Catalogue, served_base: tuple[str, str]) -> str:
    """The release date of the base the engines serve (`served_base`, id and version): bases.yaml's "released" for
    it, whatever other bases the catalogue's adapters were trained on. ManifestError when bases.yaml does not list it
    or gives it no date."""
    base_id, version = served_base
    base = catalogue.base(base_id, version)
    if "released" not in base:
        raise ManifestError(f"bases.yaml has no \"released\" date for {base_id} {version}; add released: "
                            "YYYY-MM-DD (the date that version was published)")
    return str(base["released"])


def _models(jeff: Jeff, model_name: str) -> dict[str, Any]:
    """As jeff-server: the base under each name it lists (jeff, jeff-latest and the model name, sorted; the legacy
    jeff-qwen3.8-27b is accepted but not listed), then each served adapter. The release date is that of the base the
    engines serve (EngineRouter.base: EngineError when they serve different bases, or when the engine is not the
    engine processes, whose base is not known)."""
    if not isinstance(jeff.engine, EngineRouter):
        raise EngineError(f"This server's engine is a {type(jeff.engine).__name__}, not an EngineRouter over engine "
                          "sockets; GET /v1/models reports the base the engine processes serve")
    released = release_date(jeff.catalogue, jeff.engine.base())
    served = _served(jeff.catalogue, jeff.engine.installed(refresh=True))
    base_names = sorted({name for name in BASE_NAMES if name != LEGACY_NAME} | {model_name})
    return {"models": [*({"name": name, "description": BASE_DESCRIPTION, "release_date": released}
                         for name in base_names),
                       *({"name": _public_name(manifest), "description": ADAPTER_DESCRIPTION,
                          "release_date": released} for manifest in served)]}


@router.get("/v1/models")
async def models(request: Request) -> dict[str, Any]:
    """The models on the caller's view of the catalogue: an adapter it may not use is not listed."""
    shared = await request.app.state.planner.ready()
    return await run_in_threadpool(_models, visible_jeff(shared, request_caller(request)),
                                   request.app.state.settings.model_name)
