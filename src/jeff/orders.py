"""Answer twice: score a question with its options in the given order and again in reverse, then average.

Small models lean towards options by position (for example towards A on two-option judgement questions). Scoring the
reversed list as well, putting its probabilities back under the original options and averaging the two distributions
cancels most of that lean, at twice the cost. One order (the default) is the plain single pass.

Every option is reversed, for every question type: the model was trained on shuffled options, so no position is
special. A noul question's fixed pair is listed "true" first in the reversed pass (the "true_first" setting read by
jeff.model.options). A question with a single option reads the same both ways, so its average is its single pass."""

from collections.abc import Sequence
from typing import Protocol

from jeff.types import DecisionInput, Question

ORDERS = (1, 2)


class Predictor(Protocol):
    def predict(self, rows: Sequence[DecisionInput], batch_size: int = 8,
                temperature: float | None = None) -> list[list[float]]: ...


def check_orders(orders: int) -> None:
    if isinstance(orders, bool) or orders not in ORDERS:
        raise ValueError(f"orders must be 1 (the given option order) or 2 (also reversed, then averaged); got {orders!r}")


def reverse_question(question: Question) -> Question:
    """The same question with its options listed in the opposite order."""
    if question["type"] == "choice":
        return {**question, "criteria": dict(reversed(question["criteria"].items()))}
    if question["type"] == "score":
        return {**question, "criteria": question["criteria"][::-1]}
    if question["type"] == "noul":
        return {**question, "true_first": not question.get("true_first", False)}
    raise ValueError(f"Unknown question type {question['type']!r}")


def reverse_row[Row: DecisionInput](row: Row) -> Row:
    reversed_row = dict(row)
    reversed_row["question"] = reverse_question(row["question"])
    return reversed_row  # type: ignore[return-value]  # the same TypedDict with one field replaced


def restore_order(question: Question, values: Sequence[float]) -> list[float]:
    """Values scored on reverse_question(question), put back under the original question's options.

    Choice and noul options are matched by key; score levels are numbered by position, so level i of the original is
    level n-1-i of the reversed list."""
    if question["type"] == "score":
        if len(values) != len(question["criteria"]):
            raise ValueError(f"Expected {len(question['criteria'])} values for the score levels, got {len(values)}")
        return list(values)[::-1]
    keys: list[str]
    reversed_question = reverse_question(question)
    if question["type"] == "choice" and reversed_question["type"] == "choice":
        keys, reversed_keys = list(question["criteria"]), list(reversed_question["criteria"])
    else:
        keys = ["true", "false"] if question.get("true_first") else ["false", "true"]
        reversed_keys = keys[::-1]
    if len(values) != len(keys):
        raise ValueError(f"Expected {len(keys)} values for the options, got {len(values)}")
    by_key = dict(zip(reversed_keys, values, strict=True))
    return [by_key[key] for key in keys]


def average_orders(questions: Sequence[Question], given: Sequence[Sequence[float]],
                   reversed_: Sequence[Sequence[float]]) -> list[list[float]]:
    """Average each question's probabilities from the given order with those from the reversed order."""
    if not len(questions) == len(given) == len(reversed_):
        raise ValueError("Each question needs probabilities from both option orders")
    result: list[list[float]] = []
    for question, first, second in zip(questions, given, reversed_, strict=True):
        restored = restore_order(question, second)
        if len(first) != len(restored):
            raise ValueError(f"The two orders gave {len(first)} and {len(restored)} probabilities")
        result.append([(a + b) / 2 for a, b in zip(first, restored, strict=True)])
    return result


def predict_orders(model: Predictor, rows: Sequence[DecisionInput], orders: int = 1, batch_size: int = 8,
                   temperature: float | None = None) -> list[list[float]]:
    """Probabilities per row: model.predict itself for one order, the order average for two."""
    check_orders(orders)
    given = model.predict(rows, batch_size, temperature)
    if orders == 1:
        return given
    reversed_ = model.predict([reverse_row(row) for row in rows], batch_size, temperature)
    return average_orders([row["question"] for row in rows], given, reversed_)
