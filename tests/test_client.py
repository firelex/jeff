"""The Python client, against a fake HTTP server (scripted answers) and against jeff.server with a stand-in model."""

import asyncio
import json
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from jeff import AsyncClient, Client
from jeff.client import (Busy, ConnectionFailed, InvalidRequest, NotReady, ProtocolError, ServerError, TooManyOptions,
                         Unauthorised, UnknownModel, choice_question, score_question, yes_no_question)

ROOT = Path(__file__).resolve().parents[1]
HEAVY = {"torch", "torchvision", "transformers", "numpy", "PIL", "fastapi", "starlette", "pydantic", "uvicorn",
         "safetensors", "huggingface_hub", "datasets", "accelerate", "httpx", "matplotlib", "mlx", "mlx_lm"}


class Fake:
    """A scripted server: each request is recorded and answered with the next scripted (status, headers, body)."""

    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.replies: list[tuple[int, dict[str, str], object]] = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def reply(self) -> None:
                length = int(self.headers.get("content-length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                fake.requests.append({"method": self.command, "path": self.path, "body": body,
                                      "authorization": self.headers.get("authorization")})
                status, headers, payload = fake.replies.pop(0)
                data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = reply

            def log_message(self, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def answer(self, answers: dict, status: int = 200, headers: dict[str, str] | None = None) -> None:
        self.replies.append((status, headers or {}, {"model": "jeff-qwen3.5-0.8b", "answers": answers,
                                                     "usage": {"input_tokens": 10, "output_tokens": 0}}))

    def fail(self, status: int, detail: object, headers: dict[str, str] | None = None) -> None:
        self.replies.append((status, headers or {}, {"detail": detail}))


@pytest.fixture
def fake() -> Iterator[Fake]:
    server = Fake()
    yield server
    server.server.shutdown()


def choice_answer(probabilities: dict[str, float]) -> dict:
    return {"type": "choice", "choice": max(probabilities, key=probabilities.__getitem__),
            "probabilities": probabilities, "confidence": 0.5}


def test_importing_the_client_loads_no_model_or_training_package() -> None:
    code = "import sys, jeff, jeff.client; print(sorted({m.split('.')[0] for m in sys.modules}))"
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True, cwd=ROOT)
    loaded = set(json.loads(result.stdout.strip().replace("'", '"')))
    assert not loaded & HEAVY, f"importing jeff.client loads {sorted(loaded & HEAVY)}"


def test_choose_from_a_mapping_sends_the_options_in_order_and_reads_the_choice(fake: Fake) -> None:
    fake.answer({"choice": choice_answer({"refunds": 0.2, "parcels": 0.7, "login": 0.1})})
    jeff = Client(fake.url, model="jeff-latest")
    picked = jeff.choose("My parcel arrived crushed.",
                         {"refunds": "Refunds", "parcels": "Damaged or lost parcels", "login": None}, "Which team?")
    assert (picked.key, picked.index, picked.option, picked.probability) == ("parcels", 1, "Damaged or lost parcels", 0.7)
    assert picked.ranked()[0] == ("parcels", 0.7)
    [sent] = fake.requests
    assert sent["path"] == "/v1/systemone" and sent["authorization"] is None
    assert sent["body"] == {"model": "jeff-latest", "state": "My parcel arrived crushed.", "questions": {"choice": {
        "type": "choice", "instructions": "Which team?",
        "criteria": {"refunds": "Refunds", "parcels": "Damaged or lost parcels", "login": None}}}}
    assert list(sent["body"]["questions"]["choice"]["criteria"]) == ["refunds", "parcels", "login"]


def test_choose_from_a_list_uses_keys_that_are_not_numbers(fake: Fake) -> None:
    fake.answer({"choice": choice_answer({"o1": 0.1, "o2": 0.1, "o3": 0.8})})
    picked = Client(fake.url, model="jeff").choose("Turn the lights off", ["Play music", "Set a timer", "Lights off"])
    assert (picked.key, picked.index, picked.option) == ("o3", 2, "Lights off")
    assert fake.requests[0]["body"]["questions"]["choice"]["criteria"] == {
        "o1": "Play music", "o2": "Set a timer", "o3": "Lights off"}
    assert "instructions" not in fake.requests[0]["body"]["questions"]["choice"]


@pytest.mark.parametrize("key", ["1", "0", "12", " 3", "2.5", "-1", "1e3"])
def test_number_like_option_keys_are_refused(key: str) -> None:
    with pytest.raises(ValueError, match="bare number"):
        choice_question({key: "x", "other": "y"})


def test_bad_questions_are_refused_before_sending() -> None:
    with pytest.raises(ValueError, match="at least one option"):
        choice_question([])
    with pytest.raises(TypeError, match="not one string"):
        choice_question("abc")
    with pytest.raises(ValueError, match="2 to 10 levels"):
        score_question(["only"])
    with pytest.raises(ValueError, match="orders"):
        Client("http://localhost:1", model="jeff", orders=3)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="http"):
        Client("localhost:8765", model="jeff")


def test_yes_no_score_and_several_questions_in_one_request(fake: Fake) -> None:
    jeff = Client(fake.url, model="jeff-latest", api_key="secret", orders=2)
    fake.answer({"noul": {"type": "noul", "noul": 0.83}})
    assert jeff.yes_no("I have asked three times now!", "Is the customer angry?", yes="Angry", no="Calm") == 0.83
    assert fake.requests[-1]["body"]["questions"]["noul"] == {
        "type": "noul", "instructions": "Is the customer angry?", "criteria": {"true": "Angry", "false": "Calm"}}
    assert fake.requests[-1]["body"]["orders"] == 2 and fake.requests[-1]["authorization"] == "Bearer secret"

    fake.answer({"score": {"type": "score", "score": 1.6, "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
                           "legend": {"0": "low", "1": "medium", "2": "high"}, "confidence": 0.4}})
    rated = jeff.score("The server is down for everyone.", ["low", "medium", "high"], "How urgent?", orders=1)
    assert (rated.score, rated.level, rated.probabilities) == (1.6, 2, [0.1, 0.2, 0.7])
    assert fake.requests[-1]["body"]["orders"] == 1

    fake.answer({"route": choice_answer({"billing": 0.9, "tech": 0.1}), "angry": {"type": "noul", "noul": 0.2}})
    answers = jeff.ask("Why was I charged twice?", {"route": choice_question({"billing": "Billing", "tech": "Tech"}),
                                                    "angry": yes_no_question("Is the customer angry?")},
                       model="jeff-support")
    assert answers.choice("route").key == "billing" and answers.yes_no("angry") == 0.2
    assert fake.requests[-1]["body"]["model"] == "jeff-support"
    with pytest.raises(TypeError, match="choice question, not noul"):
        answers.yes_no("route")
    with pytest.raises(KeyError, match="No question"):
        answers.choice("missing")


def test_with_model_and_decide_use_the_client_model_unless_the_request_names_one(fake: Fake) -> None:
    nav = Client(fake.url, model="jeff-latest").with_model("jeff-nav")
    fake.answer({"q": {"type": "noul", "noul": 0.5}})
    fake.answer({"q": {"type": "noul", "noul": 0.5}})
    nav.decide({"state": "s", "questions": {"q": {"type": "noul"}}})
    nav.decide({"model": "jeff-guard", "state": "s", "questions": {"q": {"type": "noul"}}})
    assert [request["body"]["model"] for request in fake.requests] == ["jeff-nav", "jeff-guard"]
    assert "orders" not in fake.requests[0]["body"]


def test_prepare_sends_the_fixed_part_and_needs_the_changing_field_last(fake: Fake) -> None:
    jeff = Client(fake.url, model="jeff-latest")
    fake.answer({"nav": choice_answer({"ask_question": 0.5, "none_of_these": 0.5})})
    state = {"current_screen": "Inbox", "transcript": ""}
    assert jeff.prepare(state, {"nav": choice_question({"ask_question": "A question", "none_of_these": "None"})}) is None
    assert list(fake.requests[0]["body"]["state"]) == ["current_screen", "transcript"]
    with pytest.raises(ValueError, match="last field"):
        jeff.prepare("just text", {"nav": choice_question(["a", "b"])})


@pytest.mark.parametrize(("status", "detail", "headers", "error"), [
    (401, "Missing or invalid API key.", {}, Unauthorised),
    (422, "Question 'choice' has 30 options, but this model handles at most 26. Shortlist the options first.", {},
     TooManyOptions),
    (422, [{"loc": ["body", "questions", "choice", "choice", "criteria"], "msg": "too long", "type": "too_long"}], {},
     TooManyOptions),
    (422, [{"loc": ["body", "model"], "msg": "Value error, Unknown model.", "type": "value_error"}], {}, UnknownModel),
    (422, [{"loc": ["body", "state"], "msg": "Field required", "type": "missing"}], {}, InvalidRequest),
    (503, "The model is not ready.", {}, NotReady),
    (500, "Internal Server Error", {}, ServerError),
])
def test_errors_are_typed(fake: Fake, status: int, detail: object, headers: dict, error: type) -> None:
    fake.fail(status, detail, {"x-request-id": "abc", **headers})
    with pytest.raises(error) as raised:
        Client(fake.url, model="jeff").choose("s", ["a", "b"])
    assert raised.value.status == status and raised.value.detail == detail and raised.value.request_id == "abc"
    assert len(fake.requests) == 1  # never retried


def test_busy_carries_retry_after_and_is_not_retried(fake: Fake) -> None:
    fake.fail(529, "The model is busy. Retry shortly.", {"Retry-After": "1"})
    with pytest.raises(Busy) as raised:
        Client(fake.url, model="jeff").yes_no("s", "Is it?")
    assert raised.value.retry_after == 1.0 and len(fake.requests) == 1


def test_malformed_answers_are_protocol_errors(fake: Fake) -> None:
    jeff = Client(fake.url, model="jeff")
    fake.answer({})
    with pytest.raises(ProtocolError, match="no answer"):
        jeff.choose("s", ["a", "b"])
    fake.answer({"choice": choice_answer({"o1": 0.5, "o9": 0.5})})
    with pytest.raises(ProtocolError, match="expected a probability for each"):
        jeff.choose("s", ["a", "b"])
    fake.replies.append((200, {}, "not json"))
    with pytest.raises(ProtocolError, match="not JSON"):
        jeff.choose("s", ["a", "b"])


def test_an_unreachable_server_raises_connection_failed() -> None:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(ConnectionFailed, match="Could not reach"):
        Client(f"http://127.0.0.1:{port}", model="jeff", timeout=2).health()


def test_health_and_models(fake: Fake) -> None:
    health = {"status": "ready", "model": "jeff-qwen3.5-0.8b", "checkpoint": "c", "max_options": 26,
              "authentication": False, "modalities": ["text"]}
    fake.replies.append((200, {}, health))
    fake.replies.append((200, {}, {"models": [{"name": "jeff", "description": "d", "release_date": "2026-09-28"}]}))
    jeff = Client(fake.url, model="jeff")
    assert jeff.health() == health
    assert [model["name"] for model in jeff.models()] == ["jeff"]
    assert [(request["method"], request["path"]) for request in fake.requests] == [("GET", "/health"),
                                                                                  ("GET", "/v1/models")]


def test_the_asyncio_client_runs_requests_concurrently(fake: Fake) -> None:
    for _ in range(3):
        fake.answer({"choice": choice_answer({"o1": 0.9, "o2": 0.1})})

    async def run() -> list[str]:
        jeff = AsyncClient(fake.url, model="jeff")
        picks = await asyncio.gather(*(jeff.choose(f"s{n}", ["a", "b"]) for n in range(3)))
        return [pick.option for pick in picks]  # type: ignore[misc]

    assert asyncio.run(run()) == ["a", "a", "a"]


class Uniform:
    """A stand-in model for jeff.server that gives every option the same probability."""
    backend = "mlx"
    base_model = "Qwen/Qwen3.5-0.8B"

    def decide(self, rows: list[dict]) -> list[tuple[list[float], int]]:
        counts = [len(row["question"].get("criteria") or [0, 0]) for row in rows]
        return [([1 / count] * count, 10) for count in counts]


def test_the_client_matches_the_real_server(monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from jeff import server

    monkeypatch.setattr(server.service, "model", Uniform())
    monkeypatch.setattr(server.service, "max_options", 3)
    monkeypatch.setenv("JEFF_API_KEY", "secret")
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    running = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, lifespan="off", log_level="error"))
    thread = threading.Thread(target=running.run, daemon=True)
    thread.start()
    try:
        for _ in range(200):
            if running.started:
                break
            time.sleep(0.02)
        jeff = Client(f"http://127.0.0.1:{port}", model="jeff-latest", api_key="secret", orders=2)
        answers = jeff.ask({"screen": "Inbox", "transcript": "open the last email"}, {
            "route": choice_question(["Open email", "Archive"], "What does the user want?"),
            "question": yes_no_question("Is the user asking a question?"),
            "urgency": score_question(["low", "medium", "high"])})
        assert answers.choice("route").probabilities == {"o1": 0.5, "o2": 0.5}
        assert answers.yes_no("question") == 0.5
        assert answers.score("urgency").probabilities == pytest.approx([1 / 3] * 3)
        assert answers.response["usage"]["orders"] == 2
        assert jeff.health()["max_options"] == 3
        assert "jeff-latest" in [model["name"] for model in jeff.models()]
        with pytest.raises(TooManyOptions, match="at most 3"):
            jeff.choose("s", ["a", "b", "c", "d"])
        with pytest.raises(UnknownModel):
            jeff.choose("s", ["a", "b"], model="jeff-nowhere")
        with pytest.raises(Unauthorised):
            Client(f"http://127.0.0.1:{port}", model="jeff").choose("s", ["a", "b"])
        server.service.lock.acquire()
        try:
            with pytest.raises(Busy) as raised:
                jeff.choose("s", ["a", "b"])
            assert raised.value.retry_after == 1.0
        finally:
            server.service.lock.release()
    finally:
        running.should_exit = True
        thread.join(timeout=5)
