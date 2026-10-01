"""Serve a Jeff checkpoint through the decision API: a situation and questions in, one answer per question out.

With JEFF_ADAPTERS set to a folder of LoRA adapters (jeff.lora), the base checkpoint is loaded once and each request's
"model" field chooses the base or one adapter (the adapter's folder name): JEFF_ADAPTER_MODE=shared, the default.
JEFF_ADAPTER_MODE=merged serves exactly one adapter folded into the base weights, at the base's speed; the plain base
is then not served and adapters cannot be reloaded. JEFF_LORA_PRECISION: model (the default: LoRA weights in the base's
dtype) or float32 (the reference)."""

from __future__ import annotations

import base64
import binascii
import hmac
import json
import os
import threading
import time
import uuid
from _thread import LockType
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Literal, Protocol, cast

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator
from starlette.concurrency import run_in_threadpool
from starlette.middleware.base import RequestResponseEndpoint

from jeff.types import Answer, DecisionInput, DecisionResponse, JSONValue, Question as DecisionQuestion

if TYPE_CHECKING:
    import torch

    from jeff.model import DecisionModel

type Content = str | dict[str, JsonValue] | list[JsonValue]
# Accepted in requests for compatibility with clients written against v1.0 and v1.1, but no longer listed by
# /v1/models: the name wrongly suggested a 27B model. The served model's real name comes from its checkpoint.
LEGACY_MODEL = "jeff-qwen3.8-27b"
DEFAULT_MODEL = LEGACY_MODEL
ALIASES = {"jeff", "jeff-latest", LEGACY_MODEL}


@dataclass
class Service:
    model: DecisionModel | None = None
    name: str = DEFAULT_MODEL
    checkpoint: str = "checkpoints/selected"
    release_date: str = ""
    max_options: int = 0  # the most options the model was trained on; set from decision_config.json
    lock: LockType = field(default_factory=threading.Lock)
    # LoRA adapters (JEFF_ADAPTERS): name -> the most options that adapter was trained on; empty without adapters
    adapters: dict[str, int] = field(default_factory=dict)
    adapter_stamps: dict[str, tuple[tuple[str, int, int], ...]] = field(default_factory=dict)
    adapter_root: Path | None = None
    adapter_set: Adapters | None = None
    merged: str | None = None  # JEFF_ADAPTER_MODE=merged: the one adapter folded into the base; the base is not served


service = Service()


class Question(BaseModel):
    model_config = ConfigDict(extra="forbid")
    instructions: Content | None = None


class Choice(Question):
    type: Literal["choice"]
    criteria: dict[str, Content | None] = Field(min_length=1, max_length=255)


class Score(Question):
    type: Literal["score"]
    criteria: list[Content] = Field(min_length=2, max_length=10)


class Noul(Question):
    type: Literal["noul"]
    criteria: dict[Literal["true", "false"], Content | None] | None = None


class EvaluationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: str
    state: Content
    questions: dict[str, Annotated[Choice | Score | Noul, Field(discriminator="type")]] = Field(min_length=1)
    images: list[str] = Field(default_factory=list, max_length=4)
    # 2: answer each question twice, the second time with its options reversed, and average (twice the cost)
    orders: int = Field(default=1, ge=1, le=2, strict=True)

    @field_validator("model")
    @classmethod
    def known_model(cls, value: str) -> str:
        if service.merged is not None:
            if value != service.merged:
                raise ValueError(f"Unknown model. This server serves only {service.merged} (merged into the base).")
            return value
        if value not in ALIASES | {service.name} | set(service.adapters):
            adapters = f", or an adapter: {', '.join(sorted(service.adapters))}" if service.adapters else ""
            raise ValueError(f"Unknown model. Use {service.name} or jeff-latest{adapters}.")
        return value

    @field_validator("images")
    @classmethod
    def valid_images(cls, values: list[str]) -> list[str]:
        for value in values:
            if len(value) > 12_000_000:
                raise ValueError("Each image must be at most 8 MB before base64 encoding.")
            header, separator, encoded = value.partition(",")
            if not separator or header not in {
                "data:image/png;base64", "data:image/jpeg;base64", "data:image/webp;base64",
            }:
                raise ValueError("Images must be base64 PNG, JPEG, or WebP data URLs.")
            try:
                content = base64.b64decode(encoded, validate=True)
                if len(content) > 8_000_000:
                    raise ValueError("Each image must be at most 8 MB.")
                with Image.open(BytesIO(content)) as image:
                    if image.width * image.height > 16_000_000:
                        raise ValueError("Each image must have at most 16 million pixels.")
                    if image.format not in {"PNG", "JPEG", "WEBP"}:
                        raise ValueError("Unsupported image format.")
                    image.verify()
            except (binascii.Error, OSError, SyntaxError, UnidentifiedImageError, Image.DecompressionBombError) as error:
                raise ValueError("Invalid image data.") from error
        return values


def authenticate(authorization: str | None = Header(default=None)) -> None:
    key = os.getenv("JEFF_API_KEY")
    if key and not hmac.compare_digest((authorization or "").encode(), f"Bearer {key}".encode()):
        raise HTTPException(401, "Missing or invalid API key.", headers={"WWW-Authenticate": "Bearer"})


def max_options(config: dict[str, JSONValue], path: Path) -> int:
    """The largest number of options the checkpoint can answer: the most its training questions had. Answer codes past
    that (for example AA, AB, ... after Z) were never trained, so the model would silently never pick those options."""
    value = config.get("max_options")
    if not isinstance(value, int) or isinstance(value, bool) or value < 2:
        raise ValueError(f"{path} needs \"max_options\": the largest number of options the model was trained on "
                         "(the trainer writes it; the Jeff models released on 2026-09-28 handle 26).")
    return value


def check_option_counts(body: EvaluationRequest) -> None:
    limit = service.adapters.get(body.model, service.max_options)
    for key, question in body.questions.items():
        count = len(question.criteria) if isinstance(question, Choice) else 0
        if count > limit:
            raise HTTPException(422, f"Question {key!r} has {count} options, but this model handles at most "
                                     f"{limit}. Shortlist the options first, or split the question.")


class Adapters(Protocol):
    """The adapters on the loaded base: jeff.lora.AdapterSet (PyTorch) or the MLX model itself."""
    def add_adapter(self, name: str, path: Path) -> None: ...
    def remove_adapter(self, name: str) -> None: ...
    def use(self, name: str | None) -> None: ...


def stamp(folder: Path) -> tuple[tuple[str, int, int], ...]:
    """Name, size and modification time of each file of an adapter folder: a changed adapter is reloaded."""
    return tuple((path.name, path.stat().st_size, path.stat().st_mtime_ns) for path in sorted(folder.iterdir()) if path.is_file())


def sync_adapters() -> dict[str, JSONValue]:
    """Make the loaded adapters match the JEFF_ADAPTERS folder, where every subfolder is one adapter named by the
    folder: load new ones, drop removed ones, reload changed ones. The caller holds the lock."""
    root, loaded = service.adapter_root, service.adapter_set
    if root is None or loaded is None:
        raise ValueError("Adapters are off: start the server with JEFF_ADAPTERS set to a folder of adapters")
    if not root.is_dir():
        raise ValueError(f"JEFF_ADAPTERS={root} is not a folder")
    wanted = {path.name: path for path in sorted(root.iterdir()) if path.is_dir()}
    stamps = {name: stamp(path) for name, path in wanted.items()}
    removed = [name for name in service.adapters if name not in wanted]
    changed = [name for name in service.adapters if name in wanted and service.adapter_stamps[name] != stamps[name]]
    added = [name for name in wanted if name not in service.adapters]
    for name in removed + changed:
        loaded.remove_adapter(name)
        del service.adapters[name], service.adapter_stamps[name]
    for name in changed + added:
        if name in ALIASES | {service.name}:
            raise ValueError(f"Adapter folder {wanted[name]} has the name of the base model; rename it")
        config_path = wanted[name] / "decision_config.json"
        limit = max_options(json.loads(config_path.read_text()), config_path)
        loaded.add_adapter(name, wanted[name])
        service.adapters[name], service.adapter_stamps[name] = limit, stamps[name]
    lists: dict[str, list[str]] = {"added": sorted(added), "removed": sorted(removed), "reloaded": sorted(changed),
                                   "adapters": sorted(service.adapters)}
    return {key: cast(JSONValue, value) for key, value in lists.items()}


def merge_one(root: Path) -> None:
    """JEFF_ADAPTER_MODE=merged: the folder must hold exactly one adapter; it is folded into the base weights."""
    from jeff.lora import merge_adapter

    if not root.is_dir():
        raise ValueError(f"JEFF_ADAPTERS={root} is not a folder")
    folders = [path for path in sorted(root.iterdir()) if path.is_dir()]
    if len(folders) != 1:
        raise ValueError(f"JEFF_ADAPTER_MODE=merged needs exactly one adapter in {root}, found {[f.name for f in folders]}")
    folder = folders[0]
    if folder.name in ALIASES | {service.name}:
        raise ValueError(f"Adapter folder {folder} has the name of the base model; rename it")
    config_path = folder / "decision_config.json"
    limit = max_options(json.loads(config_path.read_text()), config_path)
    merge_adapter(cast("torch.nn.Module", service.model), service.checkpoint, folder.name, folder)
    service.adapters, service.merged = {folder.name: limit}, folder.name


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    from jeff.model import DecisionModel

    service.checkpoint = os.getenv("JEFF_CHECKPOINT", "checkpoints/selected")
    from jeff.models import device_from_environment, load_decision_model

    backend = os.getenv("JEFF_BACKEND", "pytorch")
    adapters_root = os.getenv("JEFF_ADAPTERS")
    mode = os.getenv("JEFF_ADAPTER_MODE", "shared")
    precision = os.getenv("JEFF_LORA_PRECISION", "model")
    if mode not in ("shared", "merged"):
        raise ValueError(f"JEFF_ADAPTER_MODE={mode!r}; use shared or merged")
    if precision not in ("model", "float32"):
        raise ValueError(f"JEFF_LORA_PRECISION={precision!r}; use model or float32")
    if mode == "merged" and (not adapters_root or backend != "pytorch"):
        raise ValueError("JEFF_ADAPTER_MODE=merged needs JEFF_ADAPTERS (a folder with exactly one adapter) and the pytorch backend")
    loaded: Adapters | None = None
    if backend == "mlx":  # Apple GPUs: fast Metal kernels for Qwen3.5 (text only)
        from jeff.mlx_backend import MlxDecisionModel
        mlx_model = await run_in_threadpool(lambda: MlxDecisionModel(service.checkpoint, lora_precision=precision))
        service.model = cast("DecisionModel", mlx_model)  # predict() calls its decide() instead of a forward pass
        loaded = mlx_model if adapters_root else None
    elif backend == "pytorch":
        model = await run_in_threadpool(load_decision_model, checkpoint=service.checkpoint, device=device_from_environment())
        service.model = cast("DecisionModel", model)
        if adapters_root and mode == "shared":
            from jeff.lora import AdapterSet
            loaded = await run_in_threadpool(lambda: AdapterSet(model, service.checkpoint, precision=precision))
    else:
        raise ValueError(f"JEFF_BACKEND={backend!r}; use pytorch or mlx")
    service.name = f"jeff-{service.model.base_model.rsplit('/', 1)[-1].lower()}"
    config_path = Path(service.checkpoint) / "decision_config.json"
    service.max_options = max_options(json.loads(config_path.read_text()), config_path)
    if adapters_root and mode == "merged":
        await run_in_threadpool(merge_one, Path(adapters_root))
    elif adapters_root:
        service.adapter_root, service.adapter_set = Path(adapters_root), loaded
        await run_in_threadpool(sync_adapters)
    modified = config_path.stat().st_mtime
    service.release_date = datetime.fromtimestamp(modified, timezone.utc).date().isoformat()
    try:
        yield
    finally:
        service.model = None
        service.adapter_set = service.adapter_root = service.merged = None
        service.adapters, service.adapter_stamps = {}, {}


app = FastAPI(title="Jeff", version="0.2.0", lifespan=lifespan)


@app.middleware("http")
async def request_metadata(request: Request, call_next: RequestResponseEndpoint) -> Response:
    started, identifier = time.perf_counter(), uuid.uuid4().hex
    response = await call_next(request)
    response.headers["x-request-id"] = identifier
    response.headers["server-timing"] = f"total;dur={(time.perf_counter() - started) * 1000:.1f}"
    return response


@app.exception_handler(RequestValidationError)
async def validation_error(request: Request, error: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=422, content={"detail": [
        {"loc": item["loc"], "msg": item["msg"], "type": item["type"]}
        for item in error.errors()
    ]})


@app.get("/", response_class=HTMLResponse, include_in_schema=False)
def playground() -> str:
    return Path(__file__).with_name("playground.html").read_text()


@app.get("/health", response_model=None)
def health() -> dict[str, JSONValue]:
    return {"status": "ready" if service.model is not None else "loading", "model": service.name,
            "checkpoint": service.checkpoint, "max_options": service.max_options,
            "adapters": {name: cast(JSONValue, {"max_options": limit}) for name, limit in service.adapters.items()},
            "merged_adapter": service.merged,
            "authentication": bool(os.getenv("JEFF_API_KEY")),
            "modalities": ["text"] if getattr(service.model, "backend", None) == "mlx" else ["text", "image"]}


@app.get("/v1/models", dependencies=[Depends(authenticate)], response_model=None)
def models() -> dict[str, JSONValue]:
    served = [] if service.merged else [(name, "Local Jeff text and image decisions.")
                                        for name in sorted((ALIASES - {LEGACY_MODEL}) | {service.name})]
    served += [(name, "A LoRA adapter merged into the base model." if service.merged else "A LoRA adapter on the base model.")
               for name in sorted(service.adapters)]
    return {"models": [
        {"name": name, "description": description, "release_date": service.release_date}
        for name, description in served
    ]}


def distributions(model: DecisionModel, rows: list[DecisionInput]) -> tuple[list[list[float]], int]:
    """Each row's option probabilities (the checkpoint temperature applied) and the input tokens read."""
    import torch

    if getattr(model, "backend", None) == "mlx":
        results: list[tuple[list[float], int]] = model.decide(rows)  # type: ignore[attr-defined]
        return [values for values, _ in results], sum(tokens for _, tokens in results)
    output: list[list[float]] = []
    input_tokens = 0
    with torch.inference_mode():
        for start in range(0, len(rows), 8):
            batch = model.prepare(rows[start:start + 8])
            probabilities: list[list[float]] = (model(batch) / model.temperature).softmax(-1).cpu().tolist()
            output.extend(values[:count] for values, count in zip(probabilities, batch.counts, strict=True))
            input_tokens += batch.input_tokens
    return output, input_tokens


def predict(model: DecisionModel, body: EvaluationRequest) -> DecisionResponse:
    from jeff.model import answer
    from jeff.orders import average_orders, reverse_row

    questions = {key: cast(DecisionQuestion, question.model_dump(exclude_none=True))
                 for key, question in body.questions.items()}
    rows: list[DecisionInput] = [{"state": body.state, "question": question, "images": list(body.images)}
                                 for question in questions.values()]
    name = body.model if body.model in service.adapters else service.name
    if service.adapter_set is not None:  # switch to the requested adapter (or the base); the caller holds the lock
        service.adapter_set.use(body.model if body.model in service.adapters else None)
    values, input_tokens = distributions(model, rows)
    if body.orders == 2:
        reversed_values, reversed_tokens = distributions(model, [reverse_row(row) for row in rows])
        values = average_orders(list(questions.values()), values, reversed_values)
        input_tokens += reversed_tokens
    answers: dict[str, Answer] = {identifier: answer(question, probabilities)
                                  for (identifier, question), probabilities in zip(questions.items(), values, strict=True)}
    return {"model": name, "answers": answers,
            "usage": {"input_tokens": input_tokens, "output_tokens": 0, "orders": body.orders}}


@app.post("/v1/systemone", dependencies=[Depends(authenticate)], response_model=None)
async def system_one(body: EvaluationRequest) -> DecisionResponse:
    model = service.model
    if model is None:
        raise HTTPException(503, "The model is not ready.")
    check_option_counts(body)
    if not service.lock.acquire(blocking=False):
        raise HTTPException(529, "The model is busy. Retry shortly.", headers={"Retry-After": "1"})
    try:
        return await run_in_threadpool(predict, model, body)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        service.lock.release()


@app.post("/v1/adapters/reload", dependencies=[Depends(authenticate)], response_model=None)
async def reload_adapters() -> dict[str, JSONValue]:
    """Load adapters added to the JEFF_ADAPTERS folder, drop removed ones and reload changed ones, without restarting.
    Requests wait (get 529) while it runs."""
    if service.merged is not None:
        raise HTTPException(409, f"This server has {service.merged} merged into the base (JEFF_ADAPTER_MODE=merged); "
                                 "restart it to change adapters.")
    if service.adapter_set is None:
        raise HTTPException(409, "Adapters are off: start the server with JEFF_ADAPTERS set to a folder of adapters.")
    await run_in_threadpool(service.lock.acquire)
    try:
        return await run_in_threadpool(sync_adapters)
    except ValueError as error:
        raise HTTPException(422, str(error)) from error
    finally:
        service.lock.release()


def main() -> None:
    import uvicorn

    uvicorn.run("jeff.server:app", host=os.getenv("JEFF_HOST", "127.0.0.1"), port=int(os.getenv("PORT", "8000")))


if __name__ == "__main__":
    main()
