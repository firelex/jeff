"""Shared decision schema and dataset records."""

from pathlib import Path
from typing import Literal, NotRequired, TypedDict

from PIL import Image

type JSONValue = str | int | float | bool | None | list[JSONValue] | dict[str, JSONValue]
type Content = str | dict[str, JSONValue] | list[JSONValue]
type ImageInput = str | Path | Image.Image
type Label = str | int | bool
type Target = Label | float | list[float]


class QuestionBase(TypedDict):
    instructions: NotRequired[Content | None]


class ChoiceQuestion(QuestionBase):
    type: Literal["choice"]
    criteria: dict[str, Content | None]


class NoulQuestion(QuestionBase):
    type: Literal["noul"]
    criteria: NotRequired[dict[Literal["true", "false"], Content | None] | None]
    true_first: NotRequired[bool]  # list "true" before "false"; set only by jeff.orders for the reversed pass


class ScoreQuestion(QuestionBase):
    type: Literal["score"]
    criteria: list[Content]


type Question = ChoiceQuestion | NoulQuestion | ScoreQuestion


class DecisionInput(TypedDict):
    state: Content
    question: Question
    images: NotRequired[list[ImageInput]]


class Example(DecisionInput):
    id: str
    suite: str
    family: str
    label: Label
    target: Target
    source: dict[str, JSONValue]


class ChoiceAnswer(TypedDict):
    type: Literal["choice"]
    choice: str
    probabilities: dict[str, float]
    confidence: float


class NoulAnswer(TypedDict):
    type: Literal["noul"]
    noul: float


class ScoreAnswer(TypedDict):
    type: Literal["score"]
    score: float
    legend: dict[str, Content | None]
    probabilities: dict[str, float]
    confidence: float


type Answer = ChoiceAnswer | NoulAnswer | ScoreAnswer


class Usage(TypedDict, total=False):
    input_tokens: int
    output_tokens: int
    orders: int  # 2 when each question was answered twice, the second time with its options reversed


class DecisionResponse(TypedDict):
    answers: dict[str, Answer]
    usage: Usage
    model: NotRequired[str]
    id: NotRequired[str]
    provider: NotRequired[str]

