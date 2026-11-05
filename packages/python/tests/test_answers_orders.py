"""jeff.answers (each question's answer from its option probabilities, as jeff-server's jeff.model.answer) and
jeff.orders (answering twice: the options in the given and the reversed order, put back and averaged), ported from
jeff-server's tests/test_orders.py without the model."""

import math

import pytest

from jeff.answers import answer, describe, options
from jeff.errors import EngineError
from jeff.orders import average_orders, restore_order, reverse_question, reverse_row

CHOICE = {"type": "choice", "instructions": "Where to?", "criteria": {"Paris": None, "Rome": "sunny", "Oslo": {"cold": True}}}
SCORE = {"type": "score", "criteria": ["bad", "fine", "good", "great"]}
NOUL = {"type": "noul", "criteria": {"true": "It rained", "false": None}}
BINARY = {"type": "choice", "criteria": {"Response 1": None, "Response 2": None}}


def softmax(values: list[float]) -> list[float]:
    exps = [math.exp(value - max(values)) for value in values]
    return [value / sum(exps) for value in exps]


def position_and_text(question: dict, content: dict[str, float], bias: list[float]) -> list[float]:
    """A stand-in model: a score for each option's text plus a score for its position in the list."""
    _, descriptions = options(question)
    return softmax([content.get(describe(text), 0.0) + bias[position] for position, text in enumerate(descriptions)])


def twice(question: dict, content: dict[str, float], bias: list[float]) -> list[float]:
    given = position_and_text(question, content, bias)
    reversed_ = position_and_text(reverse_question(question), content, bias)
    return average_orders([question], [given], [reversed_])[0]


def test_options_are_the_keys_and_descriptions_each_question_type_lists() -> None:
    assert options(CHOICE) == (["Paris", "Rome", "Oslo"], ["Paris", "Rome: sunny", 'Oslo: {"cold": true}'])
    assert options(SCORE) == (["0", "1", "2", "3"], ["bad", "fine", "good", "great"])
    assert options(NOUL) == (["false", "true"], ["No / false", "It rained"])
    assert options({"type": "noul"}) == (["false", "true"], ["No / false", "Yes / true"])


def test_reversing_lists_every_option_in_the_opposite_order() -> None:
    for question in (CHOICE, SCORE, NOUL, BINARY):
        _, descriptions = options(question)
        _, reversed_descriptions = options(reverse_question(question))
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
    content = {"Paris": 0.3, "Rome: sunny": 1.2, 'Oslo: {"cold": true}': -0.4, "bad": -1.0, "great": 2.0,
               "It rained": 0.7, "No / false": 0.1}
    for question in (CHOICE, SCORE, NOUL):
        assert twice(question, content, [0.0] * 4) == pytest.approx(position_and_text(question, content, [0.0] * 4),
                                                                    abs=1e-12)
    assert answer(NOUL, twice(NOUL, content, [0.0] * 4))["noul"] == pytest.approx(softmax([0.1, 0.7])[1])


def test_averaging_balances_a_position_only_model() -> None:
    bias = [2.0, 1.0, 0.5, 0.0]  # no content scores: only the position matters, favouring the first option
    assert position_and_text(BINARY, {}, bias)[0] > 0.7
    assert twice(BINARY, {}, bias) == pytest.approx([0.5, 0.5])
    assert twice(NOUL, {}, bias) == pytest.approx([0.5, 0.5])
    levels = twice(SCORE, {}, bias)
    assert levels == pytest.approx(levels[::-1])  # a symmetric spread: level i gets what level n-1-i gets
    spread = twice(CHOICE, {}, bias)
    assert spread == pytest.approx(spread[::-1]) and math.isclose(sum(spread), 1)


def test_averaging_with_text_and_position_scores() -> None:
    given, reversed_ = softmax([2.0, 1.0]), softmax([3.0, 0.0])  # reversed lists Response 2 first
    assert twice(BINARY, {"Response 2": 1.0}, [2.0, 0.0]) == pytest.approx(
        [(given[0] + reversed_[1]) / 2, (given[1] + reversed_[0]) / 2])


def test_averaging_needs_both_option_orders() -> None:
    with pytest.raises(ValueError, match="both option orders"):
        average_orders([BINARY], [[0.5, 0.5]], [])


def test_the_noul_answer_reads_true_by_key() -> None:
    assert answer(reverse_question(NOUL), [0.8, 0.2])["noul"] == pytest.approx(0.8)  # reversed lists true first
    assert answer(NOUL, [0.8, 0.2])["noul"] == pytest.approx(0.2)
    assert reverse_row({"state": "s", "question": NOUL, "images": []})["question"]["true_first"] is True


def test_average_keeps_probabilities_normalized() -> None:
    averaged = average_orders([CHOICE], [[0.2, 0.3, 0.5]], [[0.6, 0.3, 0.1]])[0]
    assert averaged == pytest.approx([(0.2 + 0.1) / 2, 0.3, (0.5 + 0.6) / 2]) and math.isclose(sum(averaged), 1)


def test_a_choice_answer_normalises_and_scales_confidence_from_a_uniform_guess_to_certainty() -> None:
    result = answer(BINARY, [0.75, 0.25])
    assert result == {"type": "choice", "probabilities": {"Response 1": 0.75, "Response 2": 0.25},
                      "choice": "Response 1", "confidence": pytest.approx(0.5)}
    assert answer(BINARY, [0.5, 0.5])["confidence"] == 0.0
    assert answer({"type": "choice", "criteria": {"only": None}}, [1.0])["confidence"] == 1.0


def test_a_score_answer_is_the_expected_level_with_a_legend_and_a_distance_confidence() -> None:
    question = {"type": "score", "criteria": ["low", "medium", "high"]}
    result = answer(question, [0.1, 0.2, 0.7])
    assert result["type"] == "score" and result["score"] == pytest.approx(1.6)
    assert result["legend"] == {"0": "low", "1": "medium", "2": "high"}
    assert result["probabilities"] == pytest.approx({"0": 0.1, "1": 0.2, "2": 0.7})
    distance = 0.1 * 2 + 0.2 * 1  # from the most likely level, 2
    baseline = (1 + 0 + 1) / 3  # the mean distance from the middle level
    assert result["confidence"] == pytest.approx(1 - distance / baseline)
    assert answer(question, [1 / 3] * 3)["confidence"] == pytest.approx(0.0)


@pytest.mark.parametrize("values", [[0.5], [0.5, 0.5, 0.0], [], [-0.1, 1.1], [0.0, 0.0], [math.nan, 1.0],
                                    [math.inf, 1.0]])
def test_probabilities_that_do_not_fit_the_options_are_refused(values: list[float]) -> None:
    """A reply that breaks the engine's contract is refused with EngineError (jeff.decision.checked_probabilities),
    never rescaled: the wrong count, a value outside 0 to 1, or a sum too far from 1."""
    with pytest.raises(EngineError, match="the engine returned"):
        answer(BINARY, values)
