"""Find training rows that copy an evaluation row.

A training row overlaps an evaluation row when any of these rules holds:
  state   the whole state is identical after normalising (case folded, every character that is not a letter or digit
          removed, spaces collapsed), at ANY length, short texts such as voice commands included;
  option  an option description of OPTION_WORDS or more words that belongs to a single evaluation row is identical
          after the same normalising (catches candidate answers that sit in the options while the state is a fixed
          prompt). Option text found in two or more evaluation rows is an answer label, such as a team name, and is
          not matched;
  near    a text of LONG_WORDS or more words (the state, a string field of the state, or an option description) shares
          at least NEAR of its 5-word sequences with one of the evaluation row's texts (Jaccard similarity);
  family  both rows belong to the same family: test sets hold out whole families.
A text shared by TEMPLATE_AT or more rows of one evaluation file is a fixed prompt (for example the same instruction or
the same "Which statement is sarcastic?" state in every row), not row content; the state, option and near rules do
not match it. Such texts are counted in the output."""

import hashlib
import re
from collections import Counter, defaultdict
from collections.abc import Sequence
from typing import TypedDict

from jeff.kit.rows import as_text, normalize
from jeff.types import Example

TEMPLATE_AT = 20
NEAR = 0.85
LONG_WORDS = 20
OPTION_WORDS = 5
SHINGLE = 5
ANCHORS = 8
WORD = re.compile(r"[^\W_]+")


class Match(TypedDict):
    rule: str
    evaluation_id: str


def long_parts(row: Example) -> list[str]:
    """Texts long enough for the near rule."""
    state = row["state"]
    parts = [as_text(state)]
    if isinstance(state, dict):
        parts += [value for value in state.values() if isinstance(value, str)]
    question = row["question"]
    if question["type"] == "choice":
        parts += [value for value in question["criteria"].values() if isinstance(value, str)]
    return [part for part in parts if len(WORD.findall(part)) >= LONG_WORDS]


def option_texts(row: Example) -> list[str]:
    question = row["question"]
    if question["type"] != "choice":
        return []
    texts = [normalize(value) for value in question["criteria"].values() if isinstance(value, str)]
    return [text for text in texts if len(text.split()) >= OPTION_WORDS]


def shingles(text: str) -> frozenset[str]:
    words = normalize(text).split()
    return frozenset(" ".join(words[index:index + SHINGLE]) for index in range(len(words) - SHINGLE + 1))


def anchors(values: frozenset[str]) -> list[str]:
    """A few of a text's shingles, chosen the same way for every text, to find candidate matches quickly."""
    return sorted(values, key=lambda value: hashlib.md5(value.encode()).digest())[:ANCHORS]


class Features:
    """What the check compares, computed once per training row."""

    def __init__(self, row: Example) -> None:
        self.state = normalize(as_text(row["state"]))
        self.options = option_texts(row)
        self.longs = [(values, anchors(values)) for values in (shingles(part) for part in long_parts(row))]
        self.family = row["family"]


class Index:
    """One evaluation file, indexed for the rules in this module's docstring."""

    def __init__(self, rows: Sequence[Example]) -> None:
        states = Counter(normalize(as_text(row["state"])) for row in rows)
        options = Counter(text for row in rows for text in set(option_texts(row)))
        longs = Counter(normalize(part) for row in rows for part in set(long_parts(row)))
        self.templates = sorted({text for counts in (states, options, longs) for text, count in counts.items() if count >= TEMPLATE_AT})
        skip = set(self.templates)
        self.states = {text: index for index, row in enumerate(rows)
                       for text in [normalize(as_text(row["state"]))] if text and text not in skip}
        # A candidate answer belongs to one row; option text found in two or more rows is a label, not row content.
        self.options = {text: index for index, row in enumerate(rows) for text in option_texts(row) if options[text] == 1}
        self.parts: list[tuple[frozenset[str], int]] = []
        self.anchor: dict[str, list[int]] = defaultdict(list)
        for index, row in enumerate(rows):
            for part in long_parts(row):
                if normalize(part) in skip:
                    continue
                values = shingles(part)
                self.parts.append((values, index))
                for anchor in anchors(values):
                    self.anchor[anchor].append(len(self.parts) - 1)
        self.families: dict[str, int] = {}
        for index, row in enumerate(rows):
            self.families.setdefault(row["family"], index)
        self.ids = [row["id"] for row in rows]

    def match(self, features: Features) -> list[Match]:
        found: list[Match] = []
        if features.state in self.states:
            found.append({"rule": "state", "evaluation_id": self.ids[self.states[features.state]]})
        for text in features.options:
            if text in self.options:
                found.append({"rule": "option", "evaluation_id": self.ids[self.options[text]]})
                break
        near = self.near(features)
        if near is not None:
            found.append({"rule": "near", "evaluation_id": self.ids[near]})
        if features.family in self.families:
            found.append({"rule": "family", "evaluation_id": self.ids[self.families[features.family]]})
        return found

    def near(self, features: Features) -> int | None:
        for values, keys in features.longs:
            for candidate in sorted({k for anchor in keys for k in self.anchor.get(anchor, [])}):
                other, index = self.parts[candidate]
                if len(values & other) / len(values | other) >= NEAR:
                    return index
        return None


class Leak(TypedDict):
    train_id: str
    matches: list[Match]


def find_leaks(train: Sequence[Example], evaluation: Sequence[Example]) -> tuple[list[Leak], Index]:
    """Every training row that overlaps a row of one evaluation file, with the rules that matched."""
    index = Index(evaluation)
    leaks: list[Leak] = []
    for row in train:
        found = index.match(Features(row))
        if found:
            leaks.append({"train_id": row["id"], "matches": found})
    return leaks, index
