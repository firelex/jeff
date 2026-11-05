"""Helpers both catalogue importers use (import_jeffhub.py for the open adapters, import_private.py for the private
ones in teamsuzie/jeff-adapters): reading test rows, mapping a row to its function, and deriving a function's
instructions and labels from its test rows. Each stops the import (sys.exit) naming the adapter and function when the
rows do not decide the answer."""

import json
import re
import sys
from pathlib import Path

from jeff.manifest import StateShape, function_key_of_test_row

NUMBERED = re.compile(r"^([a-z]+)(\d+)$")


def read_rows(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").split("\n") if line.strip()]


def function_of(name: str, row: dict, questions: dict[str, dict], mode: str | list[str],
                shapes: dict[str, StateShape] | None, kinds: dict[str, str | list[str]] | None,
                id_patterns: dict[str, str] | None) -> str:
    """The function a row belongs to by tests.function_by `mode`. `shapes` maps each function key to its request shape
    (jeff.manifest.request_shape) for the state_shape criterion; `kinds` and `id_patterns` are the import file's
    tests.kinds and tests.id_patterns; each is None when the import does not use it."""
    return function_key_of_test_row(name, mode, {key: q["type"] for key, q in questions.items()}, row, kinds, shapes,
                                    id_patterns)


def choice_labels(name: str, key: str, rows: list[dict], example: dict, notes: list[str], hub_options: str) -> dict:
    criteria = [list(row["question"]["criteria"].items()) for row in rows]
    # Fixed-label adapters were trained on shuffled options, so compare the sets, and keep example.json's order.
    if all(sorted(map(repr, item)) == sorted(map(repr, criteria[0])) for item in criteria):
        trained, shown = set(map(repr, criteria[0])), set(map(repr, example.items()))
        if shown == trained:
            return {"source": "fixed", "fixed": dict(example), "subset": False}
        if not shown < trained:
            sys.exit(f"{name}.{key}: example.json's options differ from the test rows' options")
        # example.json shows only some of the trained options (a short example): take the full set from the test
        # rows, ordered by key (the order does not matter, training shuffled it).
        notes.append(f"{key}: example.json lists {len(shown)} of the {len(trained)} options every test row has; the "
                     f"labels are the test rows' full set, ordered by key (training shuffled the order).")
        return {"source": "fixed", "fixed": dict(sorted(criteria[0])), "subset": False}
    # Every row has the same keys in the same order, but the descriptions vary: the caller describes fixed keys.
    if all([k for k, _ in item] == [k for k, _ in criteria[0]] for item in criteria):
        notes.append(f"{key}: every test row has the keys {[k for k, _ in criteria[0]]} in this order, with "
                     f"descriptions that vary, so the labels are 'describe': the caller describes these keys.")
        return {"source": "describe", "keys": [k for k, _ in criteria[0]]}
    # Every row offers a different subset of one set of (key, text) pairs, with some keys in every row: the caller
    # chooses which of the fixed labels to offer, and the keys in every row must always be offered.
    texts: dict[str, set] = {}
    for item in criteria:
        for k, text in item:
            texts.setdefault(k, set()).add(text)
    always = [k for k in texts if all(k in dict(item) for item in criteria)]
    if all(len(found) == 1 for found in texts.values()) and always:
        order = re.findall(r"`([a-z0-9_]+)`", hub_options)
        if sorted(order) != sorted(texts):
            sys.exit(f"{name}.{key}: jeffhub's options {order} differ from the test rows' labels {sorted(texts)}")
        required = [k for k in order if k in always]
        notes.append(f"{key}: every test row offers a subset of the same {len(texts)} labels (with the same texts), "
                     f"always including {required}, so the labels are 'subset'; order as in jeffhub's options.")
        return {"source": "subset", "fixed": {k: next(iter(texts[k])) for k in order}, "required": required}
    prefix: list[tuple[str, object]] = []
    for index, pair in enumerate(criteria[0]):
        if all(len(item) > index and item[index] == pair for item in criteria):
            prefix.append(pair)
        else:
            break
    rest = [[k for k, _ in item[len(prefix):]] for item in criteria]
    pattern = None
    letters = {NUMBERED.fullmatch(k).group(1) for keys in rest for k in keys if NUMBERED.fullmatch(k)}
    if len(letters) == 1 and all(keys == [f"{next(iter(letters))}{n}" for n in range(1, len(keys) + 1)] for keys in rest):
        pattern = f"{next(iter(letters))}{{n}}"
    return {"source": "caller", "fixed_first": dict(prefix), "caller_keys": pattern, "max": max(map(len, rest))}


def instructions_from_rows(name: str, key: str, rows: list[dict]) -> str:
    """The one instruction text every test row of a function uses, word for word."""
    bare = [row["id"] for row in rows if "instructions" not in row["question"]]
    if bare:
        sys.exit(f"{name}.{key}: {len(bare)} test rows (e.g. {bare[0]}) have no instructions; owner decides")
    texts = {row["question"]["instructions"] for row in rows}
    if len(texts) != 1:
        sys.exit(f"{name}.{key}: its test rows use {len(texts)} instruction texts; owner decides")
    return texts.pop()


def question_from_rows(name: str, key: str, kind: str, rows: list[dict], hub_options: str) -> dict:
    """A question example.json does not show: instructions and options from its test rows."""
    question = {"type": kind, "instructions": instructions_from_rows(name, key, rows)}
    if kind == "choice":
        # The options' order: as jeffhub's request.questions lists them (`a`, `b`, ...); training shuffled them.
        order = re.findall(r"`([a-z0-9_]+)`", hub_options)
        criteria = dict(rows[0]["question"]["criteria"])
        if sorted(order) != sorted(criteria):
            sys.exit(f"{name}.{key}: jeffhub's options {order} differ from the test rows' {list(criteria)}")
        question["criteria"] = {option: criteria[option] for option in order}
    return question


