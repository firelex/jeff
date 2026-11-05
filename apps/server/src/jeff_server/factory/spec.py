"""The task spec: what the adapter decides, written by the teacher from the owner's description, then edited and
approved by the owner. Every later step reads only the approved spec.

Shape:
  name           lower case words joined by hyphens: the adapter's folder and model name
  summary        one sentence
  family         what one deployment is (a trading desk, a support team): its noun, a description and the fields
                 that stay the same for that deployment (they come first in the state). With no fields, the state is
                 just the request text, and families only vary who writes the requests (and group them for splits)
  live_field     the state field that changes on every request (it comes last in the state)
  questions      one or more questions; each training row asks one. The first question is asked of every request and
                 its answers are balanced; later ones may apply only when conditions hold: `requires` lists them
                 (another question's answer must equal a given option key), `applies_when` says it in words
  hard_cases     kinds of difficult requests to include
  styles         ways requests are phrased
  sizes          rows per split and number of families
"""

import re
from typing import Any

from jeff_server.factory.teacher import Teacher
from jeff_server.settings import BASE_NAMES

NAME = re.compile(r"^[a-z][a-z0-9]*(-[a-z0-9]+)*$")
KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_ .\-]{0,59}$")
FIELD = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
QUESTION_ID = re.compile(r"^[A-Za-z_][A-Za-z0-9_.?\-]{0,79}$")
BATCH = 16  # the learning-curve recipe: batch 16, no accumulation
# sizes["updates"]: training updates; updates x 16 rows must be a whole number of passes over the training rows. The
# learning curve used 250 updates (2,000 rows, two passes); larger runs keep about one pass (owner, 17 October 2026).
DEFAULT_SIZES = {"train": 2000, "updates": 250, "dev": 300, "calibration": 300, "test": 300, "families": 40}
MAX_OPTIONS = 254

text = {"type": "string"}
nullable_text = {"type": ["string", "null"]}
option = {"type": "object", "properties": {"key": text, "description": text}, "required": ["key", "description"],
          "additionalProperties": False}
SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["name", "summary", "family", "live_field", "questions", "hard_cases", "styles"],
    "properties": {
        "name": text, "summary": text,
        "family": {"type": "object", "additionalProperties": False, "required": ["noun", "description", "fields"],
                   "properties": {"noun": text, "description": text, "fields": {"type": "array", "items": {
                       "type": "object", "additionalProperties": False, "required": ["name", "description"],
                       "properties": {"name": text, "description": text}}}}},
        "live_field": {"type": "object", "additionalProperties": False, "required": ["name", "description"],
                       "properties": {"name": text, "description": text}},
        "questions": {"type": "array", "items": {
            "type": "object", "additionalProperties": False,
            "required": ["id", "type", "instruction", "options", "applies_when", "requires"],
            "properties": {"id": text, "type": {"type": "string", "enum": ["choice", "noul", "score"]},
                           "instruction": text, "options": {"type": "array", "items": option},
                           "applies_when": nullable_text,
                           "requires": {"type": "array", "items": {
                               "type": "object", "additionalProperties": False, "required": ["question", "equals"],
                               "properties": {"question": text, "equals": text}}}}}},
        "hard_cases": {"type": "array", "items": {"type": "object", "additionalProperties": False,
                                                  "required": ["kind", "description"],
                                                  "properties": {"kind": text, "description": text}}},
        "styles": {"type": "array", "items": text},
    },
}

INTERPRET_PROMPT = """You are helping to build a small classifier. A person has described, in plain English, a decision \
they want a model to make. Turn the description into a precise task specification, as JSON.

How the classifier works: it reads a "state" (a small JSON object describing the situation) and answers one fixed \
question by giving a probability to each of a closed list of options. There are three kinds of question:
- "choice": pick one option from a list. Each option has a short key (a few words, letters, digits or underscores, \
never a bare number) and a one-line description of when it is the right answer.
- "noul": a yes or no question. Its options are exactly two, with keys "true" and "false", each with a one-line \
description of what it means.
- "score": a point on a scale. Its options are the levels from lowest to highest, each with a short key and a \
one-line description.

The state has two parts. First, the fields that stay the same for one deployment of the classifier (call one \
deployment a "family": for example one trading desk, one support team, one shop). Second, exactly one field that \
changes on every request (for example the message a user typed). That changing field always comes last. If the \
description says the classifier reads only the request text and nothing else, use no fixed fields: then the state \
is just the text, and a family is only a kind of user who writes requests (for example "a day trader").

Write:
- "name": a short name for the classifier, lower case words joined by hyphens.
- "summary": one sentence saying what it decides.
- "family": "noun" (what one deployment is, singular, for example "trading desk"), "description" (one sentence), \
and "fields": the fields that stay the same for one deployment, each with a "name" (lower case with underscores) and \
a "description". Use zero to four fields (zero when the state is only the request text).
- "live_field": the one field that changes per request, with "name" and "description".
- "questions": the questions to ask. Usually one. If the description asks for several decisions about the same \
request (for example first which action, then the value of each of that action's settings), write one question for \
each, in that order. Each has an "id" (lower case with underscores), a "type", an "instruction" (the question as one \
or two plain sentences, including any rule the description states), its "options", and "applies_when": null if the \
question is asked of every request, or one sentence naming the condition under which it is asked (for example "only \
when the answer to function is place_order"), and "requires": the same condition as a list of \
{{"question": <an earlier question's id>, "equals": <one of that question's option keys>}}, all of which must hold \
(an empty list for a question asked of every request; for a yes/no question the keys are "true" and "false"). The \
first question must apply to every request.
- When the description gives question ids, question wording, option keys or option descriptions, copy them exactly, \
character for character, including ids with dots or question marks and keys that are numbers. A yes/no question \
lists only the options the description describes: both "true" and "false", only "true", or none.
- "hard_cases": kinds of difficult requests the training examples must include, each with a short "kind" (lower case \
with underscores) and a one-sentence "description". Include the ones the description names, plus near misses and \
cases that look ambiguous but have one right answer.
- "styles": three to six ways the changing field is phrased (for example "formal", "terse", "with typos").

Keep every option, rule and name the description gives, word for word where it gives them. Do not invent options \
the description does not support, except an option for requests that match none of the others when the description \
asks for one.

A short example. Description: "Route support emails to billing, technical or sales." Specification (abridged): \
{{"name": "support-routing", "family": {{"noun": "support team", "fields": [{{"name": "company", ...}}]}}, \
"live_field": {{"name": "email", ...}}, "questions": [{{"id": "team", "type": "choice", "instruction": "Which team \
should handle this email?", "options": [{{"key": "billing", "description": "Payments, invoices and refunds."}}, ...], \
"applies_when": null, "requires": []}}], ...}}

The description:
<<<
{description}
>>>
{examples}"""


def interpret(teacher: Teacher, description: str, examples: str | None, name_hint: str | None) -> tuple[dict[str, Any], str]:
    """The teacher's draft spec for this description, with the default sizes; checked like an edited spec."""
    extra = ""
    if examples:
        extra = f"\nThe person also gave these example cases with their answers:\n<<<\n{examples}\n>>>\n"
    if name_hint:
        extra += f"\nUse this name for the classifier: {name_hint}\n"
    draft, model = teacher.ask(INTERPRET_PROMPT.format(description=description, examples=extra), SCHEMA, "task_spec",
                               check=lambda value: check_spec({**value, "sizes": DEFAULT_SIZES}))
    return {**draft, "sizes": dict(DEFAULT_SIZES)}, model


def check_name(name: str) -> None:
    """The adapter's name, which becomes its alias (jeff.factory.<name>): lower case words joined by hyphens, and none
    of the names /v1/systemone keeps for the base (BASE_NAMES), which it would resolve to the base instead."""
    if not NAME.fullmatch(name):
        raise ValueError(f"name {name!r}: use lower case words joined by hyphens")
    if name in BASE_NAMES:
        raise ValueError(f"name {name!r} is kept for the base model ({', '.join(BASE_NAMES)}); choose another")


def check_spec(spec: dict[str, Any]) -> None:
    """Raise ValueError naming the first problem; the console shows it next to the form."""
    for key in ("name", "summary", "family", "live_field", "questions", "hard_cases", "styles", "sizes"):
        if key not in spec:
            raise ValueError(f"The spec has no {key!r}")
    check_name(spec["name"])
    fields = [field["name"] for field in spec["family"]["fields"]]
    if len(fields) > 6:
        raise ValueError("The family has at most six fixed fields (none: the state is just the request text)")
    names = fields + [spec["live_field"]["name"]]
    for name in names:
        if not FIELD.fullmatch(name):
            raise ValueError(f"Field name {name!r}: use lower case letters, digits and underscores")
    if len(set(names)) != len(names):
        raise ValueError("Field names must be different from each other and from the changing field")
    questions = spec["questions"]
    if not questions:
        raise ValueError("Add at least one question")
    ids = [question["id"] for question in questions]
    if len(set(ids)) != len(ids):
        raise ValueError("Question ids must be different")
    for index, question in enumerate(questions):
        check_question(question, first=index == 0)
        check_requires(question, {q["id"]: q for q in questions[:index]})
    if not spec["styles"]:
        raise ValueError("Add at least one phrasing style")
    for case in spec["hard_cases"]:
        if not FIELD.fullmatch(case["kind"]):
            raise ValueError(f"Hard case kind {case['kind']!r}: use lower case letters, digits and underscores")
    sizes = spec["sizes"]
    if set(sizes) != set(DEFAULT_SIZES) or not all(isinstance(v, int) and not isinstance(v, bool) for v in sizes.values()):
        raise ValueError(f"sizes must give whole numbers for {', '.join(DEFAULT_SIZES)}")
    if not 50 <= sizes["updates"] <= 5000:
        raise ValueError("updates must be between 50 and 5,000")
    if not 25 <= sizes["train"] <= sizes["updates"] * BATCH or (sizes["updates"] * BATCH) % sizes["train"]:
        raise ValueError(f"{sizes['updates']} updates at batch {BATCH} are {sizes['updates'] * BATCH} rows, which must be a "
                         f"whole number of passes over the {sizes['train']} training rows (for example 2,000 rows with "
                         "250 updates, or 6,400 rows with 400)")
    for split in ("dev", "calibration", "test"):
        if not 50 <= sizes[split] <= 2000:
            raise ValueError(f"{split} rows must be between 50 and 2,000")
    if not 10 <= sizes["families"] <= 200:
        raise ValueError("families must be between 10 and 200 (about 10% are held out for the test)")


def check_question(question: dict[str, Any], first: bool) -> None:
    """Imported questions (question["imported"]) are taken verbatim, so the option-key rules for written questions
    (never a bare number, true and false both described) do not apply to them."""
    qid = question["id"]
    if not QUESTION_ID.fullmatch(qid):
        raise ValueError(f"Question id {qid!r}: use letters, digits, underscores, dots, hyphens or question marks")
    if not question["instruction"].strip():
        raise ValueError(f"Question {qid}: the instruction is empty")
    if first and (question["applies_when"] is not None or question.get("requires")):
        raise ValueError(f"The first question ({qid}) must apply to every request")
    keys = [option["key"] for option in question["options"]]
    if len(set(keys)) != len(keys):
        raise ValueError(f"Question {qid}: option keys must be different")
    imported = bool(question.get("imported"))
    for key in keys:
        if imported:
            if not key or len(key) > 60:
                raise ValueError(f"Question {qid}: option key {key!r} must be 1 to 60 characters")
        elif not KEY.fullmatch(key) or key.strip().lstrip("-").replace(".", "", 1).isdigit():
            raise ValueError(f"Question {qid}: option key {key!r} must be short text, never a bare number")
    kind = question["type"]
    if kind == "noul":
        allowed = [["false", "true"], ["true"], []] if imported else [["false", "true"]]
        if sorted(keys) not in allowed:
            raise ValueError(f"Question {qid}: a yes/no question has the options true and false"
                             + (" (or only true, or none, when imported)" if imported else ""))
    if kind == "choice" and not 2 <= len(keys) <= MAX_OPTIONS:
        raise ValueError(f"Question {qid}: a choice needs 2 to {MAX_OPTIONS} options")
    if kind == "score" and not 2 <= len(keys) <= 10:
        raise ValueError(f"Question {qid}: a score needs 2 to 10 levels")


def answer_keys(question: dict[str, Any]) -> list[str]:
    """The answers a question can have: its option keys, or true and false for a yes/no question."""
    return ["true", "false"] if question["type"] == "noul" else [option["key"] for option in question["options"]]


def check_requires(question: dict[str, Any], earlier: dict[str, dict[str, Any]]) -> None:
    for condition in question.get("requires", []):
        other = earlier.get(condition["question"])
        if other is None:
            raise ValueError(f"Question {question['id']} requires {condition['question']!r}, which is not an earlier question")
        if condition["equals"] not in answer_keys(other):
            raise ValueError(f"Question {question['id']} requires {condition['question']} = {condition['equals']!r}, "
                             f"which is not one of its answers ({', '.join(answer_keys(other))})")


def applies(question: dict[str, Any], answers: dict[str, str | None]) -> bool:
    """Whether a question is asked about a request with these answers to earlier questions."""
    return all(answers.get(condition["question"]) == condition["equals"] for condition in question.get("requires", []))


def question_payload(question: dict[str, Any]) -> dict[str, Any]:
    """The question as /v1/systemone takes it (canonical instruction, options in spec order)."""
    options = question["options"]
    if question["type"] == "choice":
        return {"type": "choice", "instructions": question["instruction"],
                "criteria": {option["key"]: option["description"] for option in options}}
    if question["type"] == "noul":
        meaning = {option["key"]: option["description"] for option in options}
        payload: dict[str, Any] = {"type": "noul", "instructions": question["instruction"]}
        if meaning:
            payload["criteria"] = {key: meaning[key] for key in ("false", "true") if key in meaning}
        return payload
    return {"type": "score", "instructions": question["instruction"],
            "criteria": [f"{option['key']}: {option['description']}" for option in options]}


def label_of(question: dict[str, Any], key: str) -> str | int | bool:
    """A row's hard label from an option key: the key for choice, True or False for noul, the level index for score."""
    if question["type"] == "noul":
        return key == "true"
    if question["type"] == "score":
        return [option["key"] for option in question["options"]].index(key)
    return key


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "family", "live_field", "hard_cases", "styles"],
    "properties": {
        "summary": text,
        "family": {"type": "object", "additionalProperties": False, "required": ["noun", "description"],
                   "properties": {"noun": text, "description": text}},
        "live_field": {"type": "object", "additionalProperties": False, "required": ["name", "description"],
                       "properties": {"name": text, "description": text}},
        "hard_cases": SCHEMA["properties"]["hard_cases"],
        "styles": SCHEMA["properties"]["styles"],
    },
}

PLAN_PROMPT = """We will write training examples for a small classifier. Its questions are already fixed (listed below, \
do not change them). Read the task description and plan the examples.

How the classifier works: it reads only the text of one request and answers the questions below, each from its closed \
list of answers. Some questions are asked only when earlier answers make them relevant.

Write:
- "summary": one sentence saying what the classifier decides.
- "family": who writes the requests. "noun": the kind of person, singular (for example "trader"); "description": one \
sentence on the range of such people. Training examples will be grouped by invented people of this kind.
- "live_field": "name": a short lower-case name for the request text, with underscores (for example "command"); \
"description": one sentence describing what such a request looks like.
- "hard_cases": the kinds of difficult requests the examples must include, each with a short "kind" (lower case with \
underscores) and a one-sentence "description". Include every hard pattern the description names, plus requests that \
state some details and leave others out, and requests that could be mistaken for a different answer.
- "styles": three to six ways requests are phrased (for example "terse lower case", "with typos").

The task description:
<<<
{description}
>>>

The fixed questions (id: wording):
{questions}"""


def plan_for_import(teacher: Teacher, description: str, questions: list[dict[str, Any]], name: str) -> tuple[dict[str, Any], str]:
    """An import run's spec: the imported questions verbatim, plus the teacher's plan for the examples."""
    listing = "\n".join(f"- {q['id']}: {q['instruction']}" + (f" ({q['applies_when']})" if q["applies_when"] else "")
                        for q in questions)

    def check(value: Any) -> None:
        if not FIELD.fullmatch(value["live_field"]["name"]):
            raise ValueError(f"live_field name {value['live_field']['name']!r}: use lower case letters, digits and underscores")
        check_spec(assemble(value, questions, name))
    plan, model = teacher.ask(PLAN_PROMPT.format(description=description, questions=listing), PLAN_SCHEMA,
                              "example_plan", check=check)
    return assemble(plan, questions, name), model


def assemble(plan: dict[str, Any], questions: list[dict[str, Any]], name: str) -> dict[str, Any]:
    return {"name": name, "summary": plan["summary"],
            "family": {"noun": plan["family"]["noun"], "description": plan["family"]["description"], "fields": []},
            "live_field": plan["live_field"], "questions": questions, "hard_cases": plan["hard_cases"],
            "styles": plan["styles"], "sizes": dict(DEFAULT_SIZES, families=60)}
