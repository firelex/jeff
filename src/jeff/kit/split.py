"""Split rows into train, development, calibration and test files by family, so no family is in two splits."""

import hashlib
from collections import Counter, defaultdict
from collections.abc import Sequence
from pathlib import Path

from jeff.types import Example

HELD_OUT = ("test", "development", "calibration")
SPLITS = ("train", "development", "calibration", "test")


def ranking(value: str, seed: int) -> str:
    """A fixed pseudo-random order that depends only on the value and the seed."""
    return hashlib.sha256(f"{seed}:{value}".encode()).hexdigest()


def label_name(row: Example) -> str:
    label = row["label"]
    return str(label).lower() if isinstance(label, bool) else str(label)


def split_by_family(rows: Sequence[Example], shares: dict[str, float], seed: int) -> dict[str, list[Example]]:
    """Families in a seeded random order go to test, then development, then calibration, each taking families while
    that brings it closer to its share of the rows; the rest is train. Every held-out split with a positive share gets at least one family. Impossible
    settings (shares that leave no training rows, too few families) are errors."""
    if set(shares) != set(HELD_OUT):
        raise ValueError(f"Give a share for each of {', '.join(HELD_OUT)}")
    if any(not 0 <= share < 1 for share in shares.values()) or sum(shares.values()) >= 1:
        raise ValueError(f"Each share must be from 0 to below 1 and together below 1; got {shares}")
    families: dict[str, list[Example]] = defaultdict(list)
    for row in rows:
        families[row["family"]].append(row)
    needed = sum(share > 0 for share in shares.values()) + 1
    if len(families) < needed:
        raise ValueError(f"{len(families)} families, but these shares need at least {needed} (one per split); "
                         "rows of one family always stay together")
    order = sorted(families, key=lambda family: ranking(family, seed))
    result: dict[str, list[Example]] = {name: [] for name in SPLITS}
    position = 0
    for index, name in enumerate(HELD_OUT):
        target = shares[name] * len(rows)
        reserved = 1 + sum(shares[later] > 0 for later in HELD_OUT[index + 1:])
        while shares[name] > 0 and position < len(order) - reserved:
            size = len(families[order[position]])
            if result[name] and abs(len(result[name]) + size - target) >= abs(len(result[name]) - target):
                break  # the next family would take the split further from its share
            result[name].extend(families[order[position]])
            position += 1
        if shares[name] > 0 and not result[name]:
            raise ValueError(f"No family left for the {name} split; lower the shares or add families")
    for family in order[position:]:
        result["train"].extend(families[family])
    if not result["train"]:
        raise ValueError("No family left for training; lower the held-out shares")
    return result


def summary(splits: dict[str, list[Example]]) -> str:
    """Rows, families and label shares per split, as a plain-text table."""
    total = sum(len(rows) for rows in splits.values())
    labels = sorted({label_name(row) for rows in splits.values() for row in rows})
    width = max(12, *(len(label) + 2 for label in labels))
    lines = [f"{'split':<13}{'rows':>7}{'share':>8}{'families':>10}"]
    for name in SPLITS:
        rows = splits[name]
        lines.append(f"{name:<13}{len(rows):>7}{len(rows) / total:>8.1%}{len({row['family'] for row in rows}):>10}")
    lines += ["", "Label share per split:", f"{'label':<{width}}" + "".join(f"{name:>13}" for name in SPLITS)]
    for label in labels:
        cells = []
        for name in SPLITS:
            counts = Counter(label_name(row) for row in splits[name])
            cells.append(f"{counts[label] / len(splits[name]):>13.1%}" if splits[name] else f"{'-':>13}")
        lines.append(f"{label:<{width}}" + "".join(cells))
    return "\n".join(lines)


def output_paths(directory: Path) -> dict[str, Path]:
    return {name: directory / f"{name}.jsonl" for name in SPLITS}
