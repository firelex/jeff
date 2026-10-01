"""Score questions (ordered levels) in training and evaluation: the same options, order and answer codes as the served
prompt (jeff.model.options), hard labels as level indices and soft targets as one probability per level."""

import random

import pytest
import torch

from jeff.evaluate import evaluate_logits, hard_label, label_index, options
from jeff.model import decision_messages, options as served_options
from jeff.train import augment, targets

LEVELS = ["Not urgent", "Low", "Medium", "High", "Critical"]


def row(target: object, label: int = 3) -> dict:
    return {"id": "t-1", "suite": "triage", "family": "f-1", "source": {"dataset": "triage"},
            "state": {"ticket": "The site is down for every customer."},
            "question": {"type": "score", "instructions": "How urgent is the ticket?", "criteria": list(LEVELS)},
            "label": label, "target": target}


def test_score_options_are_the_levels_in_order() -> None:
    question = row(3)["question"]
    assert options(question) == [0, 1, 2, 3, 4]  # type: ignore[arg-type]
    keys, descriptions = served_options(question)  # type: ignore[arg-type]
    assert keys == ["0", "1", "2", "3", "4"] and descriptions == LEVELS
    assert label_index(options(question), hard_label(row(3))) == 3  # type: ignore[arg-type]


def test_score_targets_hard_and_soft() -> None:
    soft = [0.0, 0.1, 0.2, 0.6, 0.1]
    values = targets([row(3), row(soft)], torch.device("cpu"))  # type: ignore[list-item]
    assert values[0, :5].tolist() == [0.0, 0.0, 0.0, 1.0, 0.0] and values[0, 5:].abs().sum() == 0
    assert values[1, :5].tolist() == pytest.approx(soft)
    with pytest.raises(ValueError, match="Invalid training target"):
        targets([row([0.5, 0.5])], torch.device("cpu"))  # type: ignore[list-item]


def test_score_levels_are_never_shuffled_and_the_prompt_is_the_served_one() -> None:
    [shuffled] = augment([row([0.0, 0.1, 0.2, 0.6, 0.1])], random.Random(1))  # type: ignore[list-item]
    assert shuffled["question"]["criteria"] == LEVELS
    text = decision_messages(row(3), ["A", "B", "C", "D", "E"])[1]["content"][-1]["text"]  # type: ignore[arg-type, index]
    assert "Options:\nA: Not urgent\nB: Low\nC: Medium\nD: High\nE: Critical" in text


def test_score_predictions_and_metrics() -> None:
    [prediction] = evaluate_logits([row(3)], [[0.0, 0.0, 1.0, 3.0, 0.0]])  # type: ignore[list-item]
    assert prediction["prediction"] == 3 and prediction["correct"] and prediction["options"] == [0, 1, 2, 3, 4]
