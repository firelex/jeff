from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jeff import server


class Uniform:
    """A stand-in model that gives every option the same probability."""
    backend = "mlx"
    base_model = "Qwen/Qwen3.5-0.8B"

    def decide(self, rows):
        return [([1 / len(row["question"]["criteria"])] * len(row["question"]["criteria"]), 10) for row in rows]


def request(count: int) -> dict:
    return {"model": "jeff", "state": "Book me a flight.",
            "questions": {"q": {"type": "choice", "instructions": "Which destination?",
                                "criteria": {f"City {i}": None for i in range(count)}}}}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server.service, "model", Uniform())
    monkeypatch.setattr(server.service, "max_options", 26)
    return TestClient(server.app)


def test_questions_up_to_the_trained_limit_are_answered(client: TestClient) -> None:
    response = client.post("/v1/systemone", json=request(26))
    assert response.status_code == 200
    assert len(response.json()["answers"]["q"]["probabilities"]) == 26


def test_questions_over_the_trained_limit_are_refused_clearly(client: TestClient) -> None:
    response = client.post("/v1/systemone", json=request(27))
    assert response.status_code == 422
    assert "27 options" in response.text and "at most 26" in response.text


def test_the_limit_is_required_in_the_checkpoint_config() -> None:
    assert server.max_options({"max_options": 255}, Path("c")) == 255
    for config in ({}, {"max_options": None}, {"max_options": 1}, {"max_options": True}, {"max_options": "26"}):
        with pytest.raises(ValueError, match="max_options"):
            server.max_options(config, Path("c"))


def test_overlapping_request_gets_529_without_a_queue(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("JEFF_QUEUE_MS", raising=False)
    assert server.service.lock.acquire(blocking=False)
    try:
        response = client.post("/v1/systemone", json=request(2))
        assert response.status_code == 529
        assert response.headers.get("retry-after") == "1"
    finally:
        server.service.lock.release()


def test_overlapping_request_waits_when_queue_ms_is_set(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JEFF_QUEUE_MS", "500")
    assert server.service.lock.acquire(blocking=False)

    def release_soon() -> None:
        import time
        time.sleep(0.05)
        server.service.lock.release()

    import threading
    threading.Thread(target=release_soon, daemon=True).start()
    response = client.post("/v1/systemone", json=request(2))
    assert response.status_code == 200
    assert "q" in response.json()["answers"]
