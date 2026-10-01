"""Call a Jeff server (jeff-serve) from Python: a situation and questions in, a probability per option out.

This module uses only the standard library, so it can be imported (or copied) without torch or any training package.

    from jeff import Client

    jeff = Client("http://localhost:8765", model="jeff-latest")
    picked = jeff.choose("My parcel arrived crushed.", {"refunds": "Refunds", "parcels": "Damaged or lost parcels"},
                         "Which team should handle this?")
    picked.key, picked.probability           # ("parcels", 0.91)

Conventions that matter:
- Option keys are never bare numbers ("1", "2"): JavaScript puts number-like keys ahead of all others, so a request
  built or relayed in JavaScript would silently list the options in a different order. Options given as a list get the
  keys o1, o2, ... and the answer says which item was chosen.
- Advance preparation: a server can prepare the unchanging start of a request once and reuse it. Give the state as an
  object whose fields that stay the same come first and whose one changing field (for example a voice transcript)
  comes last, and list the options that never change first, word for word, before the ones that do.
- Nothing is retried and nothing is guessed: every failure raises a JeffError subclass that says what went wrong.
"""

from __future__ import annotations

import asyncio
import json
import re
import urllib.error
import urllib.request
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal, NotRequired, TypedDict, cast

type JSON = str | int | float | bool | None | list[JSON] | dict[str, JSON]
type Content = str | dict[str, JSON] | list[JSON]
type Options = Mapping[str, Content | None] | Sequence[Content]
type Orders = Literal[1, 2]

NUMBER_LIKE = re.compile(r"^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$")


# The wire format, as jeff-serve reads and writes it.

class WireQuestion(TypedDict):
    type: Literal["choice", "noul", "score"]
    instructions: NotRequired[Content]
    criteria: NotRequired[dict[str, Content | None] | list[Content]]


class Request(TypedDict):
    state: Content
    questions: dict[str, WireQuestion]
    model: NotRequired[str]  # the client's model when left out
    images: NotRequired[list[str]]  # base64 PNG, JPEG or WebP data URLs, at most four
    orders: NotRequired[Orders]  # 2: answer twice, the second time with the options reversed, and average


class WireAnswer(TypedDict):
    type: Literal["choice", "noul", "score"]
    choice: NotRequired[str]
    noul: NotRequired[float]
    score: NotRequired[float]
    probabilities: NotRequired[dict[str, float]]
    confidence: NotRequired[float]
    legend: NotRequired[dict[str, Content | None]]


class Usage(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    orders: int


class Response(TypedDict):
    answers: dict[str, WireAnswer]
    usage: Usage
    model: NotRequired[str]


class Health(TypedDict):
    status: Literal["ready", "loading"]
    model: str
    checkpoint: str
    max_options: int
    authentication: bool
    modalities: list[str]


class ModelInfo(TypedDict):
    name: str
    description: str
    release_date: str


# Errors. Every failure is one of these; none is retried.

class JeffError(Exception):
    """A Jeff request failed. `status` is the HTTP status (None when no response arrived)."""

    def __init__(self, message: str, status: int | None = None, detail: JSON = None, request_id: str | None = None):
        super().__init__(message)
        self.status, self.detail, self.request_id = status, detail, request_id


class ConnectionFailed(JeffError):
    """The server could not be reached, or did not answer in time."""


class Unauthorised(JeffError):
    """401: the server has JEFF_API_KEY set and the client sent no key or the wrong one."""


class InvalidRequest(JeffError):
    """422: the server refused the request as malformed. `detail` has the server's list of problems."""


class UnknownModel(InvalidRequest):
    """422: the server does not serve the model (or adapter) named in the request."""


class TooManyOptions(InvalidRequest):
    """422: a question lists more options than the model handles. Shortlist the options first, or split the question."""


class NotReady(JeffError):
    """503: the server is still loading the model."""


class Busy(JeffError):
    """529: the server is answering another request. `retry_after` is the server's Retry-After in seconds (None when
    the server sent none). The client never retries by itself; wait and call again if that suits the application."""

    def __init__(self, message: str, retry_after: float | None, status: int | None = None, detail: JSON = None,
                 request_id: str | None = None):
        super().__init__(message, status, detail, request_id)
        self.retry_after = retry_after


class ServerError(JeffError):
    """Any other error status from the server."""


class ProtocolError(JeffError):
    """The server answered, but not in the shape this client understands."""


# Questions, built by the helpers below and sent together with Client.ask.

@dataclass(frozen=True)
class Question:
    """One question as sent, plus what is needed to read its answer (the options in order)."""
    wire: WireQuestion
    options: tuple[tuple[str, Content | None], ...] = ()  # (key, description) for choice and score questions


def option_pairs(options: Options) -> list[tuple[str, Content | None]]:
    """Options as (key, description) pairs in the order given. A list gets the keys o1, o2, ...; a mapping keeps its
    keys, which must not be bare numbers (see the module docstring)."""
    if isinstance(options, str):
        raise TypeError("options must be a mapping of key to description or a list of descriptions, not one string")
    if isinstance(options, Mapping):
        pairs = list(options.items())
        for key, _ in pairs:
            if not isinstance(key, str) or not key:
                raise ValueError(f"Option keys must be non-empty strings; got {key!r}")
            if NUMBER_LIKE.match(key):
                raise ValueError(f"Option key {key!r} is a bare number. JavaScript reorders number-like keys, which "
                                 "would change the option order; use keys such as 'o1' or a short word.")
    else:
        pairs = [(f"o{index + 1}", description) for index, description in enumerate(options)]
    if not pairs:
        raise ValueError("A question needs at least one option")
    return pairs


def choice_question(options: Options, instructions: Content | None = None) -> Question:
    """Pick one of the options: a mapping of key to description (None: the key says it all) or a list of
    descriptions."""
    pairs = option_pairs(options)
    wire: WireQuestion = {"type": "choice", "criteria": dict(pairs)}
    if instructions is not None:
        wire["instructions"] = instructions
    return Question(wire, tuple(pairs))


def yes_no_question(instructions: Content, yes: Content | None = None, no: Content | None = None) -> Question:
    """A yes/no question, answered as the probability of yes. `yes` and `no` optionally say what each answer means."""
    wire: WireQuestion = {"type": "noul", "instructions": instructions}
    if yes is not None or no is not None:
        wire["criteria"] = {"true": yes, "false": no}
    return Question(wire)


def score_question(levels: Sequence[Content], instructions: Content | None = None) -> Question:
    """A point on a scale: 2 to 10 levels, lowest first. The answer's score runs from 0 (the first level) to
    len(levels) - 1."""
    if isinstance(levels, str) or not 2 <= len(levels) <= 10:
        raise ValueError("A score question needs a list of 2 to 10 levels, lowest first")
    wire: WireQuestion = {"type": "score", "criteria": list(levels)}
    if instructions is not None:
        wire["instructions"] = instructions
    return Question(wire, tuple((str(index), level) for index, level in enumerate(levels)))


# Answers.

@dataclass(frozen=True)
class Choice:
    key: str  # the chosen option's key (o1, o2, ... when the options were a list)
    index: int  # its position among the options as sent
    option: Content | None  # its description as sent
    probability: float
    probabilities: dict[str, float]  # every option's probability, in the order sent
    confidence: float  # 0 when the answer is no better than a uniform guess, 1 when certain

    def ranked(self) -> list[tuple[str, float]]:
        """(key, probability) pairs, most likely first."""
        return sorted(self.probabilities.items(), key=lambda item: -item[1])


@dataclass(frozen=True)
class Score:
    score: float  # the expected level: 0 is the first level, len(levels) - 1 the last
    level: int  # the most likely level
    probabilities: list[float]  # one per level, in the order sent
    confidence: float


class Answers:
    """The answers to several questions asked together; read each with the accessor for its type."""

    def __init__(self, questions: Mapping[str, Question], response: Response):
        self.questions, self.response = dict(questions), response
        missing = [key for key in questions if key not in response["answers"]]
        if missing:
            raise ProtocolError(f"The response has no answer for {missing}")

    def _answer(self, key: str, kind: str) -> tuple[Question, WireAnswer]:
        if key not in self.questions:
            raise KeyError(f"No question {key!r} was asked; asked: {list(self.questions)}")
        question, answer = self.questions[key], self.response["answers"][key]
        if question.wire["type"] != kind:
            raise TypeError(f"Question {key!r} is a {question.wire['type']} question, not {kind}")
        if answer.get("type") != kind:
            raise ProtocolError(f"Question {key!r} was a {kind} question but the answer is {answer!r}")
        return question, answer

    def choice(self, key: str) -> Choice:
        question, answer = self._answer(key, "choice")
        keys = [option for option, _ in question.options]
        probabilities = in_order(answer, keys, key)
        chosen = answer.get("choice")
        if chosen not in keys:
            raise ProtocolError(f"Question {key!r}: the server chose {chosen!r}, which was not one of the options sent")
        index = keys.index(chosen)
        return Choice(keys[index], index, question.options[index][1], probabilities[keys[index]], probabilities,
                      number(answer, "confidence", key))

    def yes_no(self, key: str) -> float:
        """The probability that the answer is yes."""
        _, answer = self._answer(key, "noul")
        return number(answer, "noul", key)

    def score(self, key: str) -> Score:
        question, answer = self._answer(key, "score")
        probabilities = list(in_order(answer, [level for level, _ in question.options], key).values())
        level = max(range(len(probabilities)), key=probabilities.__getitem__)
        return Score(number(answer, "score", key), level, probabilities, number(answer, "confidence", key))


def number(answer: WireAnswer, field: str, key: str) -> float:
    value = answer.get(field)
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProtocolError(f"Question {key!r}: the answer has no number {field!r}: {answer!r}")
    return float(value)


def in_order(answer: WireAnswer, keys: list[str], key: str) -> dict[str, float]:
    """The answer's probabilities under the keys sent, in the order sent."""
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(keys):
        raise ProtocolError(f"Question {key!r}: expected a probability for each of {keys}, got {probabilities!r}")
    return {option: float(probabilities[option]) for option in keys}


# Requests and responses, shared by the synchronous and asyncio clients.

@dataclass(frozen=True)
class Call:
    method: Literal["GET", "POST"]
    path: str
    body: JSON = None


def build_request(state: Content, questions: Mapping[str, Question], model: str, orders: Orders | None,
                  images: Sequence[str] | None) -> Request:
    if not questions:
        raise ValueError("Ask at least one question")
    request: Request = {"model": model, "state": state,
                        "questions": {key: question.wire for key, question in questions.items()}}
    if images:
        request["images"] = list(images)
    if orders is not None:
        request["orders"] = orders
    return request


def check_orders(orders: Orders | None) -> None:
    if orders is not None and (isinstance(orders, bool) or orders not in (1, 2)):
        raise ValueError(f"orders must be 1 (the options as given) or 2 (also reversed, then averaged); got {orders!r}")


def check_prepare_state(state: Content) -> None:
    if not isinstance(state, dict) or not state:
        raise ValueError("prepare needs the state as an object whose last field is the one that changes (sent empty "
                         "or as it stands now); a server can only reuse the fields before it")


def error_for(status: int, text: str, headers: Mapping[str, str]) -> JeffError:
    """The typed error for a failed response."""
    request_id = headers.get("x-request-id")
    try:
        detail: JSON = json.loads(text).get("detail", text)
    except (json.JSONDecodeError, AttributeError):
        detail = text
    shown = detail if isinstance(detail, str) else json.dumps(detail)
    message = f"Jeff answered {status}: {shown[:500]}"
    if status == 401:
        return Unauthorised(message + " (pass the server's JEFF_API_KEY as api_key)", status, detail, request_id)
    if status == 422:
        problems = [problem for problem in detail if isinstance(problem, dict)] if isinstance(detail, list) else []
        locations = [problem["loc"] for problem in problems if isinstance(problem.get("loc"), list)]
        if isinstance(detail, str) and "options, but this model handles at most" in detail or any(
                problem.get("type") == "too_long" and isinstance(problem.get("loc"), list)
                and cast(list[JSON], problem["loc"])[-1:] == ["criteria"] for problem in problems):
            return TooManyOptions(message, status, detail, request_id)
        if any(cast(list[JSON], loc)[-1:] == ["model"] for loc in locations):
            return UnknownModel(message, status, detail, request_id)
        return InvalidRequest(message, status, detail, request_id)
    if status == 503:
        return NotReady(message, status, detail, request_id)
    if status == 529:
        retry = headers.get("retry-after")
        if retry is not None and not NUMBER_LIKE.match(retry):
            raise ProtocolError(f"Jeff answered 529 with a Retry-After that is not a number of seconds: {retry!r}",
                                status, detail, request_id)
        return Busy(message, float(retry) if retry is not None else None, status, detail, request_id)
    return ServerError(message, status, detail, request_id)


def parse_response(value: JSON) -> Response:
    if not isinstance(value, dict) or not isinstance(value.get("answers"), dict):
        raise ProtocolError(f"Expected a JSON object with answers, got {str(value)[:300]}")
    return cast(Response, value)


def parse_health(value: JSON) -> Health:
    fields = {"status", "model", "checkpoint", "max_options", "authentication", "modalities"}
    if not isinstance(value, dict) or not fields <= set(value):
        raise ProtocolError(f"Expected a health report with {sorted(fields)}, got {str(value)[:300]}")
    return cast(Health, value)


def parse_models(value: JSON) -> list[ModelInfo]:
    models = value.get("models") if isinstance(value, dict) else None
    if not isinstance(models, list) or not all(isinstance(model, dict) and "name" in model for model in models):
        raise ProtocolError(f"Expected a list of models, got {str(value)[:300]}")
    return cast(list[ModelInfo], models)


class Transport:
    """Sends one call over HTTP with the standard library and returns the decoded JSON, or raises a JeffError."""

    def __init__(self, url: str, api_key: str | None, timeout: float):
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"The server URL must start with http:// or https://; got {url!r}")
        if timeout <= 0:
            raise ValueError(f"timeout must be positive; got {timeout!r}")
        self.url, self.api_key, self.timeout = url.rstrip("/"), api_key, timeout

    def send(self, call: Call) -> JSON:
        headers = {"accept": "application/json"}
        data = None
        if call.body is not None:
            headers["content-type"] = "application/json"
            data = json.dumps(call.body).encode()
        if self.api_key is not None:
            headers["authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(self.url + call.path, data=data, headers=headers, method=call.method)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                text = response.read().decode()
        except urllib.error.HTTPError as error:
            with error:
                raise error_for(error.code, error.read().decode(errors="replace"),
                                {key.lower(): value for key, value in error.headers.items()}) from None
        except (urllib.error.URLError, TimeoutError, ConnectionError) as error:
            raise ConnectionFailed(f"Could not reach Jeff at {self.url}: {error}") from error
        try:
            return cast(JSON, json.loads(text))
        except json.JSONDecodeError as error:
            raise ProtocolError(f"Jeff answered with something that is not JSON: {text[:300]}") from error


class Client:
    """A synchronous client for one Jeff server.

    `model` names the model every request uses unless a call names another (with several adapters on one server, the
    model name chooses the adapter). `api_key` is the server's JEFF_API_KEY, if it has one. `orders=2` answers every
    question twice, the second time with its options reversed, and averages the two (twice the cost); a call's own
    `orders` overrides it. Left as None, the field is not sent and the server answers once."""

    def __init__(self, url: str, *, model: str, api_key: str | None = None, orders: Orders | None = None,
                 timeout: float = 30.0):
        check_orders(orders)
        if not model:
            raise ValueError("model must name the model to use, for example 'jeff-latest'")
        self.transport = Transport(url, api_key, timeout)
        self.model, self.orders = model, orders

    def with_model(self, model: str) -> Client:
        """The same client, with another default model (for example one application's adapter)."""
        return Client(self.transport.url, model=model, api_key=self.transport.api_key, orders=self.orders,
                      timeout=self.transport.timeout)

    def _request(self, state: Content, questions: Mapping[str, Question], model: str | None, orders: Orders | None,
                 images: Sequence[str] | None) -> Request:
        check_orders(orders)
        return build_request(state, questions, model if model is not None else self.model, orders if orders is not None else self.orders,
                             images)

    def decide(self, request: Request) -> Response:
        """Send one request as it is and return the server's response. A request without "model" or "orders" gets
        the client's."""
        body = dict(request)
        body.setdefault("model", self.model)
        if self.orders is not None:
            body.setdefault("orders", self.orders)
        return parse_response(self.transport.send(Call("POST", "/v1/systemone", cast(JSON, body))))

    def ask(self, state: Content, questions: Mapping[str, Question], *, model: str | None = None,
            orders: Orders | None = None, images: Sequence[str] | None = None) -> Answers:
        """Several independent questions about one state in one request, for example
        ask(state, {"route": choice_question(...), "angry": yes_no_question(...)}).choice("route")."""
        return Answers(questions, self.decide(self._request(state, questions, model, orders, images)))

    def choose(self, state: Content, options: Options, instructions: Content | None = None, *,
               model: str | None = None, orders: Orders | None = None, images: Sequence[str] | None = None) -> Choice:
        """Which option fits the state best."""
        return self.ask(state, {"choice": choice_question(options, instructions)}, model=model, orders=orders,
                        images=images).choice("choice")

    def yes_no(self, state: Content, instructions: Content, *, yes: Content | None = None, no: Content | None = None,
               model: str | None = None, orders: Orders | None = None, images: Sequence[str] | None = None) -> float:
        """The probability that the answer to the yes/no question is yes."""
        return self.ask(state, {"noul": yes_no_question(instructions, yes, no)}, model=model, orders=orders,
                        images=images).yes_no("noul")

    def score(self, state: Content, levels: Sequence[Content], instructions: Content | None = None, *,
              model: str | None = None, orders: Orders | None = None, images: Sequence[str] | None = None) -> Score:
        """Where the state sits on a scale of 2 to 10 levels, lowest first."""
        return self.ask(state, {"score": score_question(levels, instructions)}, model=model, orders=orders,
                        images=images).score("score")

    def prepare(self, state: Content, questions: Mapping[str, Question], *, model: str | None = None,
                orders: Orders | None = None) -> None:
        """Send a request ahead of time so the server can prepare its unchanging start; the answer is discarded.

        Send the state with its changing last field empty (or as it stands now) and the questions with only the
        options that never change. A later request that starts the same way, word for word, can then be answered
        faster by a server that reuses prepared work; a server that does not simply answers."""
        check_prepare_state(state)
        self.ask(state, questions, model=model, orders=orders)

    def health(self) -> Health:
        """The server's state: "ready" or "loading", the model name, the most options it handles and more."""
        return parse_health(self.transport.send(Call("GET", "/health")))

    def models(self) -> list[ModelInfo]:
        """The model names this server answers to."""
        return parse_models(self.transport.send(Call("GET", "/v1/models")))


class AsyncClient:
    """The asyncio version of Client, with the same arguments and methods. Each request runs in a worker thread."""

    def __init__(self, url: str, *, model: str, api_key: str | None = None, orders: Orders | None = None,
                 timeout: float = 30.0):
        self.sync = Client(url, model=model, api_key=api_key, orders=orders, timeout=timeout)

    @property
    def model(self) -> str:
        return self.sync.model

    def with_model(self, model: str) -> AsyncClient:
        other = AsyncClient.__new__(AsyncClient)
        other.sync = self.sync.with_model(model)
        return other

    async def decide(self, request: Request) -> Response:
        return await asyncio.to_thread(self.sync.decide, request)

    async def ask(self, state: Content, questions: Mapping[str, Question], *, model: str | None = None,
                  orders: Orders | None = None, images: Sequence[str] | None = None) -> Answers:
        return await asyncio.to_thread(self.sync.ask, state, questions, model=model, orders=orders, images=images)

    async def choose(self, state: Content, options: Options, instructions: Content | None = None, *,
                     model: str | None = None, orders: Orders | None = None,
                     images: Sequence[str] | None = None) -> Choice:
        return await asyncio.to_thread(self.sync.choose, state, options, instructions, model=model, orders=orders,
                                       images=images)

    async def yes_no(self, state: Content, instructions: Content, *, yes: Content | None = None,
                     no: Content | None = None, model: str | None = None, orders: Orders | None = None,
                     images: Sequence[str] | None = None) -> float:
        return await asyncio.to_thread(self.sync.yes_no, state, instructions, yes=yes, no=no, model=model,
                                       orders=orders, images=images)

    async def score(self, state: Content, levels: Sequence[Content], instructions: Content | None = None, *,
                    model: str | None = None, orders: Orders | None = None,
                    images: Sequence[str] | None = None) -> Score:
        return await asyncio.to_thread(self.sync.score, state, levels, instructions, model=model, orders=orders,
                                       images=images)

    async def prepare(self, state: Content, questions: Mapping[str, Question], *, model: str | None = None,
                      orders: Orders | None = None) -> None:
        await asyncio.to_thread(self.sync.prepare, state, questions, model=model, orders=orders)

    async def health(self) -> Health:
        return await asyncio.to_thread(self.sync.health)

    async def models(self) -> list[ModelInfo]:
        return await asyncio.to_thread(self.sync.models)
