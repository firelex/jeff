"""Read and validate training rows (JSON Lines), plus the text helpers the other kit commands share."""

import json
import math
import re
from collections import Counter
from collections.abc import Sequence
from pathlib import Path
from typing import cast

from jeff.types import Example

REQUIRED = ("id", "suite", "family", "state", "question", "label", "target", "source")
QUESTION_TYPES = ("choice", "noul", "score")
MAX_OPTIONS = 255  # the most answer codes a Jeff model has (jeff.model.MAX_OPTIONS)
BARE_NUMBER = re.compile(r"^\d+$")
TEMPLATE_SLOT = re.compile(r"\{\{[^{}]*\}\}")
WORD = re.compile(r"[^\W_]+")
SHOWN_PROBLEMS = 50


class RowError(ValueError):
    """A data file that cannot be used as it is: the message names the file, the row and the problem."""


def read_jsonl(path: Path) -> list[dict[str, object]]:
    """Every line of a JSON Lines file as an object. A missing or empty file, or a line that is not a JSON object,
    is an error naming the file and line."""
    if not path.is_file():
        raise RowError(f"{path}: file does not exist")
    rows: list[dict[str, object]] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").split("\n"), 1):
        if not line.strip():
            continue
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise RowError(f"{path}: line {number} is not valid JSON ({error})") from error
        if not isinstance(value, dict):
            raise RowError(f"{path}: line {number} is not a JSON object")
        rows.append(value)
    if not rows:
        raise RowError(f"{path}: file has no rows")
    return rows


def strings(value: object) -> list[str]:
    """All strings inside a value (nested objects and lists included), in order."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in strings(item)]
    return []


def is_probability_list(value: object, size: int) -> bool:
    return (isinstance(value, list) and len(value) == size
            and all(isinstance(p, (int, float)) and not isinstance(p, bool) and math.isfinite(p) and p >= 0 for p in value)
            and math.isclose(sum(value), 1, abs_tol=1e-6))


def row_problems(row: dict[str, object]) -> list[str]:
    """What is wrong with one row, in plain words; empty when the row is usable."""
    found = [f"missing field {name!r}" for name in REQUIRED if name not in row]
    for name in ("id", "suite", "family"):
        if name in row and (not isinstance(row[name], str) or not str(row[name]).strip()):
            found.append(f"{name!r} must be a non-empty string")
    if "source" in row and not isinstance(row["source"], dict):
        found.append("'source' must be an object")
    state = row.get("state")
    if "state" in row and (not isinstance(state, (str, dict, list)) or not state):
        found.append("'state' must be a non-empty string, object or list")
    question = row.get("question")
    if "question" not in row:
        return found
    if not isinstance(question, dict):
        return found + ["'question' must be an object"]
    kind, criteria, label, target = question.get("type"), question.get("criteria"), row.get("label"), row.get("target")
    if kind not in QUESTION_TYPES:
        return found + [f"question type {kind!r} is not one of {', '.join(QUESTION_TYPES)}"]
    if kind == "choice":
        if not isinstance(criteria, dict) or len(criteria) < 2:
            return found + ["a choice question needs 'criteria': an object with at least 2 options"]
        keys = list(criteria)
        if len(keys) > MAX_OPTIONS:
            found.append(f"{len(keys)} options; a Jeff model answers at most {MAX_OPTIONS}")
        numbers = [key for key in keys if BARE_NUMBER.match(key)]
        if numbers:
            found.append(f"option keys are bare numbers ({', '.join(numbers[:5])}); JavaScript reorders such keys, "
                         "use words or o1, o2, ...")
        if any(not key.strip() for key in keys):
            found.append("an option key is empty")
        if any(value is not None and (not isinstance(value, (str, dict, list)) or not value) for value in criteria.values()):
            found.append("an option description is empty")
        if not isinstance(label, str) or label not in criteria:
            found.append(f"label {label!r} is not one of the option keys ({', '.join(keys[:8])}{', ...' if len(keys) > 8 else ''})")
        if not (target == label or is_probability_list(target, len(keys))):
            found.append("'target' must equal the label or be one probability per option, summing to 1")
    elif kind == "noul":
        if not isinstance(label, bool):
            found.append(f"a yes/no (noul) label must be true or false, not {label!r}")
        if not (isinstance(target, bool) or (isinstance(target, (int, float)) and 0 <= target <= 1)):
            found.append("a yes/no (noul) target must be true, false or a probability")
    else:
        if not isinstance(criteria, list) or len(criteria) < 2:
            return found + ["a score question needs 'criteria': a list of at least 2 levels"]
        if not isinstance(label, int) or isinstance(label, bool) or not 0 <= label < len(criteria):
            found.append(f"a score label must be the index of a level (0 to {len(criteria) - 1}), not {label!r}")
        if not (target == label or is_probability_list(target, len(criteria))):
            found.append("'target' must equal the label or be one probability per level, summing to 1")
    texts = strings(state) + strings(question.get("instructions")) + strings(criteria)
    if isinstance(criteria, dict):
        texts += list(criteria)
    slots = sorted({slot for text in texts for slot in TEMPLATE_SLOT.findall(text)})
    if slots:
        found.append(f"unfilled template slots: {', '.join(slots[:5])}")
    return found


def check_rows(rows: Sequence[dict[str, object]], path: Path) -> None:
    """Raise a RowError listing every problem (the first SHOWN_PROBLEMS of them) with its row number and id."""
    problems = [f"row {number} (id {row.get('id')!r}): {problem}"
                for number, row in enumerate(rows, 1) for problem in row_problems(row)]
    counts = Counter(row.get("id") for row in rows if isinstance(row.get("id"), str))
    problems += [f"id {identifier!r} is used by {count} rows" for identifier, count in counts.items() if count > 1]
    if problems:
        shown = "\n  ".join(problems[:SHOWN_PROBLEMS])
        more = f"\n  ... and {len(problems) - SHOWN_PROBLEMS} more" if len(problems) > SHOWN_PROBLEMS else ""
        raise RowError(f"{path}: {len(problems)} problem(s):\n  {shown}{more}")


def load(path: Path) -> list[Example]:
    """Read a rows file and validate every row; any problem is an error."""
    rows = read_jsonl(path)
    check_rows(rows, path)
    return cast(list[Example], rows)


def normalize(text: str) -> str:
    """Case folded, everything but letters and digits removed, spaces collapsed: "Quiet!" and "quiet" match."""
    return " ".join(WORD.findall(text.casefold()))


def as_text(value: object) -> str:
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def main_text(row: Example, field: str | None) -> str:
    """The text a person would read to answer: the state itself when it is a string, otherwise the state field named
    by `field`, or else the state's last field (by convention the one that changes per request). A missing field or a
    last field that is not a string is an error."""
    state = row["state"]
    if isinstance(state, str):
        return state
    if not isinstance(state, dict):
        raise RowError(f"row {row['id']!r}: the state is a list; name the field to read with --text-field")
    if field is not None:
        if field not in state:
            raise RowError(f"row {row['id']!r}: the state has no field {field!r}")
        value = state[field]
    else:
        value = state[list(state)[-1]]
    if not isinstance(value, str):
        raise RowError(f"row {row['id']!r}: the main text field is not a string; name another with --text-field")
    return value


def write_jsonl(path: Path, rows: Sequence[Example]) -> None:
    """Write rows, refusing to replace an existing file."""
    if path.exists():
        raise FileExistsError(f"Refusing to overwrite {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, ensure_ascii=False, allow_nan=False) + "\n" for row in rows), encoding="utf-8")
