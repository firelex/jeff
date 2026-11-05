"""Import mode: questions already written for a decision API, taken verbatim.

A run can start from a plain-English description (the teacher writes the questions) or from an imported question set:
the questions exactly as another decision API receives them ({id: {"type", "instructions", "criteria"}}), the
conditions under which each is read, and a short task description that the teacher uses only to write training data.
The teacher never writes or rewords imported questions, so Jeff and the other API answer the same questions.

Bundles live in JEFF_FACTORY_DIR/references/<name>/: questions.json, conditions.json, description.md, and optionally
signatures.json (the functions behind a function-calling task: lets an independent test set be given as correct calls,
checked against the functions' value lists and defaults, as the TypeSafe cookbook's dispatch.py reads them)."""

import json
from pathlib import Path
from typing import Any

from jeff_server.factory.spec import check_requires, question_payload

ROUTE = "__tool__"  # the TypeSafe cookbook's function-choice question (dispatch.py)


def to_spec_questions(questions: dict[str, Any], conditions: dict[str, list[dict[str, str]]]) -> list[dict[str, Any]]:
    """The factory's question list: first the questions read for every request, then each question after the ones its
    conditions name. Every question must turn back into exactly the imported payload."""
    if set(conditions) != set(questions):
        raise ValueError(f"conditions.json and questions.json name different questions: "
                         f"{sorted(set(conditions) ^ set(questions))[:5]}")
    converted: dict[str, dict[str, Any]] = {}
    for qid, payload in questions.items():
        kind = payload["type"]
        criteria = payload.get("criteria")
        if kind == "choice":
            options = [{"key": key, "description": text} for key, text in criteria.items()]
        elif kind == "noul":
            options = [{"key": key, "description": criteria[key]} for key in ("false", "true") if criteria and key in criteria]
        else:
            raise ValueError(f"Question {qid}: importing {kind!r} questions is not supported (choice and noul are)")
        requires = conditions[qid]
        question = {"id": qid, "type": kind, "instruction": payload["instructions"], "options": options,
                    "applies_when": describe_conditions(requires), "requires": requires, "imported": True}
        if question_payload(question) != payload:
            raise ValueError(f"Question {qid} does not convert back to the imported payload exactly")
        converted[qid] = question
    ordered: list[dict[str, Any]] = []
    placed: set[str] = set()
    pending = list(converted)
    while pending:
        ready = [qid for qid in pending if all(c["question"] in placed for c in conditions[qid])]
        if not ready:
            raise ValueError(f"The conditions of {pending[:3]} refer to each other in a circle")
        for qid in ready:
            ordered.append(converted[qid])
            placed.add(qid)
            pending.remove(qid)
    if ordered[0]["requires"]:
        raise ValueError("No question is read for every request")
    first = [q["id"] for q in ordered if not q["requires"]]
    if len(first) != 1:
        raise ValueError(f"Exactly one question must be read for every request (it is balanced); found {first}")
    for index, question in enumerate(ordered):
        check_requires(question, {q["id"]: q for q in ordered[:index]})
    return ordered


def describe_conditions(requires: list[dict[str, str]]) -> str | None:
    if not requires:
        return None
    return "only when " + " and ".join(f"{c['question']} is {c['equals']}" for c in requires)


def load_bundle(folder: Path) -> dict[str, Any]:
    for name in ("questions.json", "conditions.json", "description.md"):
        if not (folder / name).exists():
            raise ValueError(f"The reference {folder.name} has no {name}")
    bundle = {"name": folder.name, "questions": json.loads((folder / "questions.json").read_text()),
              "conditions": json.loads((folder / "conditions.json").read_text()),
              "description": (folder / "description.md").read_text().strip()}
    if (folder / "signatures.json").exists():
        bundle["signatures"] = json.loads((folder / "signatures.json").read_text())
    return bundle


def bundles(root: Path) -> list[dict[str, Any]]:
    found = []
    for folder in sorted(root.iterdir()):
        if folder.is_dir() and (folder / "questions.json").exists():
            bundle = load_bundle(folder)
            found.append({"name": bundle["name"], "questions": len(bundle["questions"]), "description": bundle["description"],
                          "calls": "signatures" in bundle, "files": bundle_files(folder),
                          "readme": (folder / "README.md").read_text() if (folder / "README.md").exists() else None})
    return found


# --- independent test sets given as correct calls (function-calling tasks) ------------------------------------------

def call_answers(call: dict[str, Any], signatures: dict[str, Any]) -> dict[str, str]:
    """The answers a correct call implies, question by question, as the cookbook's dispatch.read reads them:
    __tool__ is the function; an argument with a "stated" question is stated exactly when the call gives it; a stated
    (or always-read) choice gets its value, a flag true or false, a set one yes/no per member. An argument without a
    "stated" question that the call leaves out takes the function's default. Values outside the function's lists, unknown
    arguments and missing required arguments are errors."""
    function = call.get("function")
    if function not in signatures:
        raise ValueError(f"unknown function {function!r}; the functions are {', '.join(signatures)}")
    arguments = call.get("arguments") or {}
    if not isinstance(arguments, dict):
        raise ValueError("arguments must be an object")
    closed = signatures[function]
    unknown = [name for name in arguments if name not in closed]
    if unknown:
        raise ValueError(f"{function} has no closed-set argument {', '.join(unknown)} (free-text, number and date "
                         "arguments are not decided and must be left out)")
    answers = {ROUTE: function}
    for name, info in closed.items():
        qid = f"{function}.{name}"
        given = name in arguments
        if info["stated_question"]:
            answers[f"{qid}?"] = "true" if given else "false"
            if not given:
                continue
        elif not given:
            if info["required"]:
                raise ValueError(f"{function} needs {name}")
            arguments = {**arguments, name: info["default"]}
        value = arguments[name]
        if info["shape"] == "choice":
            if value not in info["members"]:
                raise ValueError(f"{function}.{name} = {value!r} is not one of {info['members']}")
            answers[qid] = value
        elif info["shape"] == "set":
            if not isinstance(value, list) or any(v not in info["members"] for v in value) or len(set(value)) != len(value):
                raise ValueError(f"{function}.{name} must be a list of different values from {info['members']}")
            for member in info["members"]:
                answers[f"{qid}.{member}"] = "true" if member in value else "false"
        else:
            if not isinstance(value, bool):
                raise ValueError(f"{function}.{name} must be true or false")
            answers[qid] = "true" if value else "false"
    return answers


def bundle_files(folder: Path) -> list[str]:
    """The files of a reference that may be downloaded: the bundle's own files and its source/ folder (the original
    files it was derived from, under their own licence, see source/SOURCE.md)."""
    names = sorted(path.name for path in folder.iterdir() if path.is_file())
    source = folder / "source"
    if source.is_dir():
        names += sorted(f"source/{path.name}" for path in source.iterdir() if path.is_file())
    return names
