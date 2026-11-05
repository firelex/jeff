"""The teacher: an OpenAI-compatible chat model that writes the training data (GLM-5.3 on the B200 by the owner's
decision). Every call asks for strict JSON against a schema, is cached on disk inside the run (a rerun of a failed
step does not repeat calls; a fresh run starts with an empty cache) and is retried a bounded number of times.
Nothing is filled in: a reply that stays invalid raises TeacherError."""

import hashlib
import re
import json
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

TRANSPORT_ATTEMPTS = 5  # network errors, timeouts, 5xx and 429
INVALID_ATTEMPTS = 5  # replies that are not JSON, do not match the schema or fail the caller's check
TIMEOUT_SECONDS = 180  # one request; a hung request is retried (GLM occasionally stalls)


class TeacherError(RuntimeError):
    pass


class EmptyAnswer(ValueError):
    """The teacher replied with reasoning but no answer: an invalid reply (retried by ask())."""

    def __init__(self, message: str, reasoning: str) -> None:
        super().__init__(message)
        self.reasoning = reasoning


class InvalidReply(TeacherError):
    """The teacher answered, but its reply stayed invalid after the retries: recorded as a failed item."""


@dataclass
class Usage:
    calls: int = 0
    cached: int = 0
    invalid: int = 0
    retries: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def add(self, **amounts: float) -> None:
        with self.lock:
            for name, amount in amounts.items():
                setattr(self, name, getattr(self, name) + amount)

    def snapshot(self) -> dict[str, float]:
        with self.lock:
            return {"calls": self.calls, "cached": self.cached, "invalid": self.invalid, "retries": self.retries,
                    "prompt_tokens": self.prompt_tokens, "completion_tokens": self.completion_tokens,
                    "seconds": round(self.seconds, 1)}


# Request settings for GLM-5.3 behind LiteLLM (the factory's teacher): low reasoning with the template's thinking on,
# and the reply decoded against the JSON schema. See the comment in ask().
GLM_SETTINGS: dict[str, Any] = {
    "reasoning_effort": "low", "chat_template_kwargs": {"enable_thinking": True},
    "allowed_openai_params": ["reasoning_effort", "chat_template_kwargs"],
}
THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class Teacher:
    """structured=True: the reply is decoded against the schema (response_format). structured=False: the model writes
    freely (for models whose thinking arrives inside the content, such as Qwen3 on a vLLM server without a reasoning
    parser, where a schema would cut the thinking off); the JSON after the thinking is taken and checked here."""

    def __init__(self, url: str, key: str, model: str, cache: Path, settings: dict[str, Any] | None = None,
                 structured: bool = True) -> None:
        self.url, self.key, self.model = url, key, model
        self.settings = GLM_SETTINGS if settings is None else settings
        self.structured = structured
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)
        self.usage = Usage()

    def ask(self, prompt: str, schema: dict[str, Any], name: str,
            check: Callable[[Any], None] | None = None, max_tokens: int = 16000, attempts: int = INVALID_ATTEMPTS) -> tuple[Any, str]:
        """The teacher's JSON reply, and the exact model name the server reported for it,
        to one plain-English prompt. `check` raises ValueError for a reply that is valid
        JSON but wrong (for example an option key that does not exist); such replies are retried, then fail.
        The schema is enforced while decoding, and checked again here."""
        # GLM_SETTINGS: low reasoning is the owner's default for data generation. GLM-5.3 behind LiteLLM reasons only
        # when the chat template's thinking is switched on: without enable_thinking the reasoning is not split from the
        # answer, and under a JSON schema the model then has no room to think and often answers with placeholders
        # ("...", empty lists). With it, the reasoning comes back separately and the schema applies to the answer
        # alone (tested 17 October 2026). LiteLLM passes both settings on only when they are listed as allowed.
        body: dict[str, Any] = {"model": self.model, "max_tokens": max_tokens, **self.settings}
        if self.structured:
            body["messages"] = [{"role": "user", "content": prompt}]
            body["response_format"] = {"type": "json_schema", "json_schema": {"name": name, "strict": True, "schema": schema}}
        else:
            body["messages"] = [{"role": "user", "content": prompt + "\n\nAfter thinking it through, end your reply with "
                                 "only the JSON object, matching this JSON schema:\n" + json.dumps(schema)}]
        problems: list[str] = []
        for attempt in range(attempts):
            request = {**body, "seed": attempt} if attempt else body
            try:
                text, model = self.complete(request)
            except EmptyAnswer as error:
                problems.append(str(error))
                self.usage.add(invalid=1)
                self.keep_empty(request, name, error)
                continue
            try:
                value = json.loads(text if self.structured else final_json(text))
                conforms(value, schema, "reply")
                if check is not None:
                    check(value)
                return value, model
            except (json.JSONDecodeError, ValueError) as error:
                problems.append(str(error)[:300])
                self.usage.add(invalid=1)
                self.keep_invalid(request, name, str(error))
                self.forget(request)
        raise InvalidReply(f"The teacher's reply to {name!r} stayed invalid after {attempts} attempts: "
                           + " | ".join(problems))

    def key_of(self, request: dict[str, Any]) -> Path:
        digest = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        return self.cache / f"{digest}.json"

    def keep_invalid(self, request: dict[str, Any], name: str, error: str) -> None:
        """Save an invalid reply (content, finish reason, any reasoning text) next to the cache, to look at later."""
        saved = json.loads(self.key_of(request).read_text())
        folder = self.cache / "invalid"
        folder.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (folder / f"{stamp}-{name}-{self.key_of(request).stem[:12]}.json").write_text(json.dumps(
            {"name": name, "error": error, **saved, "prompt": request["messages"][0]["content"]}, ensure_ascii=False, indent=1))

    def keep_empty(self, request: dict[str, Any], name: str, error: EmptyAnswer) -> None:
        folder = self.cache / "invalid"
        folder.mkdir(exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        (folder / f"{stamp}-{name}-empty-{self.key_of(request).stem[:12]}.json").write_text(json.dumps(
            {"name": name, "error": str(error), "reasoning": error.reasoning, "prompt": request["messages"][0]["content"]},
            ensure_ascii=False, indent=1))

    def forget(self, request: dict[str, Any]) -> None:
        self.key_of(request).unlink(missing_ok=True)

    def complete(self, request: dict[str, Any]) -> tuple[str, str]:
        cached = self.key_of(request)
        if cached.exists():
            self.usage.add(cached=1)
            saved = json.loads(cached.read_text())
            return saved["content"], saved["model"]
        failures: list[str] = []
        for attempt in range(TRANSPORT_ATTEMPTS):
            started = time.monotonic()
            try:
                reply = self.post(request)
            except TransientError as error:
                failures.append(str(error))
                self.usage.add(retries=1)
                time.sleep(min(60, 2 ** attempt * 3))
                continue
            choice = reply["choices"][0]
            content = choice["message"].get("content")
            if not isinstance(content, str) or not content.strip():
                # Seen 17 October 2026: GLM-5.3 put its whole reply in the reasoning field and left the answer empty.
                # An empty answer is an invalid reply: ask() retries it within its bounded attempts and keeps the
                # reasoning for diagnosis; it is not cached.
                message = choice["message"]
                reasoning = message.get("reasoning_content") or (message.get("provider_specific_fields") or {}).get("reasoning")
                raise EmptyAnswer(f"The teacher's answer was empty (finish_reason {choice.get('finish_reason')!r}, "
                                  f"{len(reasoning or '')} characters of reasoning)", reasoning or "")
            usage = reply.get("usage") or {}
            self.usage.add(calls=1, seconds=time.monotonic() - started,
                           prompt_tokens=int(usage.get("prompt_tokens", 0)),
                           completion_tokens=int(usage.get("completion_tokens", 0)))
            model = reply.get("model")
            if not isinstance(model, str) or not model:
                raise TeacherError("The teacher's reply does not name the model that wrote it")
            message = choice["message"]
            reasoning = message.get("reasoning_content") or (message.get("provider_specific_fields") or {}).get("reasoning")
            cached.write_text(json.dumps({"model": model, "content": content, "finish_reason": choice.get("finish_reason"),
                                          "reasoning": reasoning, "usage": usage}))
            return content, model
        raise TeacherError(f"The teacher at {self.url} failed {TRANSPORT_ATTEMPTS} times: " + " | ".join(failures))

    def post(self, request: dict[str, Any]) -> dict[str, Any]:
        data = json.dumps(request).encode()
        http = urllib.request.Request(f"{self.url}/chat/completions", data=data, method="POST", headers={
            "Content-Type": "application/json", "Authorization": f"Bearer {self.key}"})
        try:
            with urllib.request.urlopen(http, timeout=TIMEOUT_SECONDS) as response:
                return json.loads(response.read())
        except urllib.error.HTTPError as error:
            detail = error.read().decode(errors="replace")[:500]
            if error.code == 429 or error.code >= 500:
                raise TransientError(f"HTTP {error.code}: {detail}") from error
            raise TeacherError(f"The teacher refused the request (HTTP {error.code}): {detail}") from error
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise TransientError(f"{type(error).__name__}: {error}") from error


class TransientError(Exception):
    pass



def conforms(value: Any, schema: dict[str, Any], where: str) -> None:
    """Raise ValueError unless `value` matches `schema` (the subset of JSON Schema the factory uses: type, enum,
    properties, required, additionalProperties false, items)."""
    kinds = schema.get("type")
    if kinds is not None:
        allowed = kinds if isinstance(kinds, list) else [kinds]
        checks = {"object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list),
                  "string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool),
                  "null": lambda v: v is None, "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                  "number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool)}
        if not any(checks[kind](value) for kind in allowed):
            raise ValueError(f"{where}: expected {' or '.join(allowed)}, got {type(value).__name__}")
    if "enum" in schema and value not in schema["enum"]:
        raise ValueError(f"{where}: {value!r} is not one of {schema['enum']}")
    if isinstance(value, dict):
        properties = schema.get("properties", {})
        missing = [key for key in schema.get("required", []) if key not in value]
        if missing:
            raise ValueError(f"{where}: missing {', '.join(missing)}")
        if schema.get("additionalProperties") is False:
            extra = [key for key in value if key not in properties]
            if extra:
                raise ValueError(f"{where}: unexpected {', '.join(extra)}")
        for key, item in value.items():
            if key in properties:
                conforms(item, properties[key], f"{where}.{key}")
    if isinstance(value, list) and "items" in schema:
        for index, item in enumerate(value):
            conforms(item, schema["items"], f"{where}[{index}]")


def final_json(text: str) -> str:
    """The JSON object a free-writing model ends with: thinking removed, then from the first "{" to the last "}"."""
    answer = THINK.sub("", text)
    if "<think>" in answer:
        raise ValueError("the reply stopped inside its thinking")
    start, end = answer.find("{"), answer.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object after the thinking")
    return answer[start:end + 1]
