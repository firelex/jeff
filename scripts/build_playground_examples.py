"""Write playground-examples.json into adapter folders: up to --per-function examples for each function, taken from
the adapter's own test set (tests.published, else tests.internal) and rebuilt as the Call a user would make (template
mode, jeff.evaluate.template_rows). GET /ui/examples offers them in the console's playground beside the golden cases.

The rows are spread over the test set's answers (one row per answer in turn, so a function with four labels shows
all four before any repeats), each picked at random (fixed seed) from the shorter half of that answer's rows, so the
form stays readable. Each example records the test set's answer ("expected") and the row it came from.

The file holds data rows, so it lives in the adapter folder (on datigator under JEFF_ADAPTER_DIRS), never in git
(the private data set). Run it where the test sets can be read (on datigator with JEFF_DATA_MIRROR set):

    uv run python scripts/build_playground_examples.py ~/jeff-v14/adapters [--only guard,spam] [--per-function 8]
"""

import argparse
import json
import random
from collections import defaultdict
from pathlib import Path
from typing import Any

from jeff.errors import JeffError
from jeff.evaluate import TestRow, published_set
from jeff.manifest import Manifest, load_manifest

FILE_NAME = "playground-examples.json"
SNIPPET = 70  # characters of the input shown in an example's name


def _texts(value: Any) -> list[str]:
    """Every text in a (possibly nested) input."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _texts(item)]
    if isinstance(value, list):
        return [text for item in value for text in _texts(item)]
    return []


def _short(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1].rstrip() + "…"


def _name(row: TestRow) -> str:
    """The answer (with its text when the caller wrote the labels, whose keys say nothing), then the start of the
    input's longest text."""
    answer = row.expected if isinstance(row.expected, str) else json.dumps(row.expected)
    labels = row.call.labels
    if isinstance(labels, dict) and isinstance(labels.get(row.expected), str):
        answer = f"{answer} ({_short(labels[row.expected], 40)})"
    texts = _texts(row.call.input)
    snippet = _short(max(texts, key=len), SNIPPET) if texts else json.dumps(row.call.input)[:SNIPPET]
    return f"{answer} · {snippet}"


def _size(row: TestRow) -> int:
    return len(json.dumps([row.call.input, row.call.context], ensure_ascii=False))


def pick(rows: list[TestRow], count: int, seed: int) -> list[TestRow]:
    """Up to `count` rows, one per answer in turn (answers in order of their first row), each at random from the
    shorter half of that answer's rows that are left."""
    by_answer: dict[str, list[TestRow]] = defaultdict(list)
    for row in rows:
        by_answer[json.dumps(row.expected)].append(row)
    rng = random.Random(seed)
    pools: list[list[TestRow]] = []
    for group in by_answer.values():
        ordered = sorted(group, key=_size)
        shorter = ordered[:max(1, (len(ordered) + 1) // 2)]
        rng.shuffle(shorter)
        pools.append(shorter)
    chosen: list[TestRow] = []
    while len(chosen) < count and any(pools):
        for pool in pools:
            if pool and len(chosen) < count:
                chosen.append(pool.pop())
    return chosen


def examples_for(manifest: Manifest, per_function: int, seed: int) -> list[dict[str, Any]]:
    test_set = published_set(manifest, "test")
    by_function: dict[str, list[TestRow]] = defaultdict(list)
    for row in test_set.rows:
        by_function[row.function].append(row)
    examples = []
    for function in manifest.functions.values():
        rows = by_function.get(function.id)
        if not rows:
            raise JeffError(f"{function.id}: no row of its test set is one the manifest can rebuild as a call "
                            f"(skipped rows by reason: {test_set.skipped})")
        for row in pick(rows, per_function, seed):
            examples.append({
                "name": _name(row),
                "call": {"function": row.function, "input": row.call.input, "context": dict(row.call.context or {}),
                         "labels": row.call.labels},
                "expected": row.expected,
                "source": f"test row {row.id}",
            })
    return examples


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("roots", nargs="+", type=Path, help="folders of adapter folders (each with manifest.yaml)")
    parser.add_argument("--only", help="adapter folder names, comma-separated (default: every folder)")
    parser.add_argument("--per-function", type=int, default=8)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    folders = sorted(path.parent for root in args.roots for path in root.glob("*/manifest.yaml"))
    if args.only:
        wanted = set(args.only.split(","))
        missing = wanted - {folder.name for folder in folders}
        if missing:
            raise SystemExit(f"No adapter folder named {sorted(missing)} under {[str(root) for root in args.roots]}")
        folders = [folder for folder in folders if folder.name in wanted]
    if not folders:
        raise SystemExit(f"No adapter folder (a folder with manifest.yaml) under {[str(root) for root in args.roots]}")
    for folder in folders:
        manifest = load_manifest(folder / "manifest.yaml")
        examples = examples_for(manifest, args.per_function, args.seed)
        (folder / FILE_NAME).write_text(json.dumps({"adapter": manifest.id, "examples": examples}, indent=1,
                                                   ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{folder.name}: {len(examples)} examples for {len(manifest.functions)} functions")


if __name__ == "__main__":
    main()
