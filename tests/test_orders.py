"""Answering twice: the options scored in the given and the reversed order, probabilities put back and averaged."""

import math

import numpy as np
import pytest
import torch
from fastapi.testclient import TestClient

from jeff import server
from jeff.evaluate import evaluate_logits, parse_arguments, predict_local, saved_orders
from jeff.model import MAX_OPTIONS, PreparedBatch, answer, describe, options
from jeff.orders import average_orders, predict_orders, restore_order, reverse_question, reverse_row

CHOICE = {"type": "choice", "instructions": "Where to?", "criteria": {"Paris": None, "Rome": "sunny", "Oslo": {"cold": True}}}
SCORE = {"type": "score", "criteria": ["bad", "fine", "good", "great"]}
NOUL = {"type": "noul", "criteria": {"true": "It rained", "false": None}}
BINARY = {"type": "choice", "criteria": {"Response 1": None, "Response 2": None}}


def logits(row: dict, content: dict[str, float], bias: list[float]) -> list[float]:
    """A stand-in model's scores: a score for each option's text plus a score for its position in the list."""
    _, descriptions = options(row["question"])
    return [content.get(describe(text), 0.0) + bias[position] for position, text in enumerate(descriptions)]


def softmax(values: list[float]) -> list[float]:
    exps = [math.exp(value - max(values)) for value in values]
    return [value / sum(exps) for value in exps]


class Fake:
    """Predicts from option text and position; the position scores (bias) favour the first options listed."""

    def __init__(self, content: dict[str, float] | None = None, bias: tuple[float, ...] = (2.0, 1.0, 0.5, 0.0)) -> None:
        self.content, self.bias, self.temperature = content or {}, list(bias), 1.0
        self.calls: list[list[dict]] = []

    def predict(self, rows, batch_size=8, temperature=None):
        self.calls.append(list(rows))
        return [softmax(logits(row, self.content, self.bias)) for row in rows]


def rows(*questions: dict) -> list[dict]:
    return [{"state": "Plan a trip.", "question": question} for question in questions]


def test_one_order_is_the_single_pass_exactly() -> None:
    model = Fake({"Rome: sunny": 1.0})
    batch = rows(CHOICE, SCORE, NOUL)
    assert predict_orders(model, batch, 1) == Fake({"Rome: sunny": 1.0}).predict(batch)
    assert len(model.calls) == 1


def test_reversing_lists_every_option_in_the_opposite_order() -> None:
    for question in (CHOICE, SCORE, NOUL, BINARY):
        keys, descriptions = options(question)
        reversed_keys, reversed_descriptions = options(reverse_question(question))
        assert reversed_descriptions == descriptions[::-1]
        assert reverse_question(reverse_question(question)) == question or question is NOUL
    assert options(reverse_question(NOUL))[0] == ["true", "false"]
    assert options(reverse_question(CHOICE))[0] == ["Oslo", "Rome", "Paris"]
    assert CHOICE["criteria"] == {"Paris": None, "Rome": "sunny", "Oslo": {"cold": True}}  # the original is untouched


def test_the_reversed_pass_is_put_back_under_the_right_options() -> None:
    assert restore_order(CHOICE, [0.5, 0.3, 0.2]) == [0.2, 0.3, 0.5]  # reversed pass lists Oslo, Rome, Paris
    assert restore_order(NOUL, [0.9, 0.1]) == [0.1, 0.9]  # reversed pass lists true, false
    assert restore_order(SCORE, [0.1, 0.2, 0.3, 0.4]) == [0.4, 0.3, 0.2, 0.1]
    with pytest.raises(ValueError, match="Expected 3 values"):
        restore_order(CHOICE, [0.5, 0.5])


def test_a_position_blind_model_gives_the_same_answer_both_ways() -> None:
    """Scores depend only on the option text, so a correct mapping back makes both orders agree exactly."""
    content = {"Paris": 0.3, "Rome: sunny": 1.2, 'Oslo: {"cold": true}': -0.4, "bad": -1.0, "great": 2.0,
               "It rained": 0.7, "No / false": 0.1}
    model = Fake(content, bias=(0.0, 0.0, 0.0, 0.0))
    batch = rows(CHOICE, SCORE, NOUL)
    single = model.predict(batch)
    for once, twice in zip(single, predict_orders(model, batch, 2), strict=True):
        assert twice == pytest.approx(once, abs=1e-12)
    assert answer(NOUL, predict_orders(model, batch, 2)[2])["noul"] == pytest.approx(softmax([0.1, 0.7])[1])


def test_averaging_balances_a_position_only_model() -> None:
    model = Fake()  # no content scores: only the position matters, favouring A
    once = model.predict(rows(BINARY, NOUL, SCORE, CHOICE))
    twice = predict_orders(model, rows(BINARY, NOUL, SCORE, CHOICE), 2)
    assert once[0][0] > 0.7 and once[1][0] > 0.7  # A (the first option) wins by position alone
    assert twice[0] == pytest.approx([0.5, 0.5]) and twice[1] == pytest.approx([0.5, 0.5])
    assert twice[2] == pytest.approx(twice[2][::-1])  # a symmetric spread: level i gets what level n-1-i gets
    assert twice[3] == pytest.approx(twice[3][::-1]) and math.isclose(sum(twice[3]), 1)
    assert answer(NOUL, twice[1])["noul"] == pytest.approx(0.5)


def test_averaging_with_text_and_position_scores() -> None:
    model = Fake({"Response 2": 1.0}, bias=(2.0, 0.0))
    given, reversed_ = softmax([2.0, 1.0]), softmax([3.0, 0.0])  # reversed lists Response 2 first
    assert predict_orders(model, rows(BINARY), 2)[0] == pytest.approx(
        [(given[0] + reversed_[1]) / 2, (given[1] + reversed_[0]) / 2])


def test_orders_other_than_one_or_two_are_refused() -> None:
    for orders in (0, 3, True):
        with pytest.raises(ValueError, match="orders must be 1"):
            predict_orders(Fake(), rows(BINARY), orders)
    with pytest.raises(ValueError, match="both option orders"):
        average_orders([BINARY], [[0.5, 0.5]], [])


def example(identifier: str, question: dict, label: object) -> dict:
    return {"id": identifier, "suite": "s", "family": identifier, "state": "x", "question": question,
            "label": label, "target": label, "source": {"dataset": "d"}}


def infer_with(content: dict[str, float], bias: tuple[float, ...]):
    return lambda batch: [logits(row, content, list(bias)) for row in batch]


def test_evaluate_one_order_matches_the_single_pass_and_two_orders_balance_position() -> None:
    examples = [example("a", BINARY, "Response 2"), example("b", NOUL, True), example("c", CHOICE, "Rome")]
    infer = infer_with({"Response 2": 0.5}, (2.0, 1.0, 0.0))
    assert predict_local(examples, infer, 1.5) == evaluate_logits(examples, infer(examples), 1.5)
    assert saved_orders(predict_local(examples, infer, 1.5)) == 1
    twice = predict_local(examples, infer_with({}, (2.0, 1.0, 0.0)), 1.0, orders=2)
    assert twice[0]["probabilities"] == pytest.approx([0.5, 0.5]) and twice[1]["probabilities"] == pytest.approx([0.5, 0.5])
    assert twice[0]["logits"] == [2.0, 1.0] and twice[0]["reversed_logits"] == [1.0, 2.0]
    assert saved_orders(twice) == 2
    blind = predict_local(examples, infer_with({"Rome: sunny": 1.0, "Yes / true": -0.3}, (0.0, 0.0, 0.0)), 1.0, orders=2)
    for once, averaged in zip(predict_local(examples, infer_with({"Rome: sunny": 1.0, "Yes / true": -0.3}, (0.0, 0.0, 0.0)), 1.0),
                              blind, strict=True):
        assert averaged["probabilities"] == pytest.approx(once["probabilities"]) and averaged["correct"] == once["correct"]
    with pytest.raises(ValueError, match="all or none"):
        saved_orders([twice[0], predict_local(examples, infer, 1.0)[1]])


def test_evaluate_orders_flag() -> None:
    base = ["--data", "d.jsonl", "--output", "o.json", "--local"]
    assert parse_arguments(base).orders == 1
    assert parse_arguments([*base, "--orders", "2"]).orders == 2
    with pytest.raises(SystemExit):
        parse_arguments([*base, "--orders", "3"])
    with pytest.raises(SystemExit):
        parse_arguments(["--data", "d.jsonl", "--output", "o.json", "--predictions", "p.jsonl", "--orders", "2"])


class MlxFake:
    """The MLX backend's interface: probabilities and input tokens per row."""
    backend = "mlx"
    base_model = "Qwen/Qwen3.5-0.8B"

    def decide(self, rows):
        return [(softmax(logits(row, {"Response 2": 0.5}, [2.0, 1.0, 0.5, 0.0])), 10) for row in rows]


class TorchFake:
    """The PyTorch backend's interface: prepare a batch, then logits padded to every answer code."""
    base_model = "Qwen/Qwen3.5-0.8B"
    temperature = 2.0

    def prepare(self, rows):
        self.rows = list(rows)
        return PreparedBatch({}, tuple(len(options(row["question"])[0]) for row in rows), 7 * len(rows))

    def __call__(self, batch):
        output = torch.full((len(self.rows), MAX_OPTIONS), -1e9)
        for index, row in enumerate(self.rows):
            values = logits(row, {"Response 2": 0.5}, [2.0, 1.0, 0.5, 0.0])
            output[index, :len(values)] = torch.tensor(values)
        return output


def body(**extra: object) -> dict:
    return {"model": "jeff", "state": "Which response is better?", **extra,
            "questions": {"a": BINARY, "b": NOUL, "c": {"type": "score", "criteria": ["bad", "fine", "good"]}}}


@pytest.fixture(params=[MlxFake, TorchFake])
def client(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setattr(server.service, "model", request.param())
    monkeypatch.setattr(server.service, "max_options", 26)
    return TestClient(server.app)


def test_server_one_order_is_the_default_and_unchanged(client: TestClient) -> None:
    default, once = client.post("/v1/systemone", json=body()).json(), client.post("/v1/systemone", json=body(orders=1)).json()
    assert default == once and default["usage"]["orders"] == 1
    model = server.service.model
    temperature = getattr(model, "temperature", 1.0)
    expected = softmax([value / temperature for value in logits({"question": BINARY}, {"Response 2": 0.5}, [2.0, 1.0])])
    assert [default["answers"]["a"]["probabilities"][key] for key in ("Response 1", "Response 2")] == pytest.approx(expected)


def test_server_two_orders_average_and_report_it(client: TestClient) -> None:
    once, twice = client.post("/v1/systemone", json=body()).json(), client.post("/v1/systemone", json=body(orders=2)).json()
    assert twice["usage"]["orders"] == 2
    assert twice["usage"]["input_tokens"] == 2 * once["usage"]["input_tokens"]
    # Response 2 has the better text; the given order's first-position lean made Response 1 look better
    assert once["answers"]["a"]["choice"] == "Response 1" and twice["answers"]["a"]["choice"] == "Response 2"
    assert twice["answers"]["b"]["noul"] == pytest.approx(0.5)  # noul texts score the same; position alone decided
    levels = twice["answers"]["c"]["probabilities"]
    assert levels["0"] == pytest.approx(levels["2"])


@pytest.mark.parametrize("orders", [0, 3, "2", 2.0, True, None, [2]])
def test_server_orders_must_be_one_or_two(client: TestClient, orders: object) -> None:
    response = client.post("/v1/systemone", json=body(orders=orders))
    assert response.status_code == 422
    assert response.json()["detail"][0]["loc"] == ["body", "orders"]


def test_the_noul_answer_reads_true_by_key() -> None:
    assert answer(reverse_question(NOUL), [0.8, 0.2])["noul"] == pytest.approx(0.8)  # reversed lists true first
    assert answer(NOUL, [0.8, 0.2])["noul"] == pytest.approx(0.2)
    assert reverse_row({"state": "s", "question": NOUL, "images": []})["question"]["true_first"] is True


def test_average_keeps_probabilities_normalized() -> None:
    averaged = average_orders([CHOICE], [[0.2, 0.3, 0.5]], [[0.6, 0.3, 0.1]])[0]
    assert averaged == pytest.approx([(0.2 + 0.1) / 2, 0.3, (0.5 + 0.6) / 2]) and np.isclose(sum(averaged), 1)
