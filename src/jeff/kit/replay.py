"""Add replay rows to an adapter's training file: a sample of rows like the base model's own training data, so the
adapter keeps its general skills. The kit ships no replay data; the user supplies the replay file (or uses none).

The sample has round(share x training rows) rows. It is stratified by (suite, source dataset) in proportion to the
replay file (largest remainder), and each stratum is walked in one seeded random order. A replay row is skipped when
it overlaps a protected evaluation file by the leak check's state, option or near rules (jeff.kit.leaks), or when one
of its texts equals the changing text (the state's last field) of a protected row."""

import random
from collections import defaultdict
from collections.abc import Sequence
from typing import TypedDict

from jeff.kit.leaks import Features, Index
from jeff.kit.rows import normalize, strings
from jeff.types import Example


class ReplayReport(TypedDict):
    train_rows: int
    replay_rows: int
    share: float
    per_stratum: dict[str, int]
    skipped_for_overlap: dict[str, int]


def stratum(row: Example) -> str:
    dataset = row["source"].get("dataset")
    return f"{row['suite']} / {dataset}" if isinstance(dataset, str) else row["suite"]


def allocate(sizes: dict[str, int], total: int) -> dict[str, int]:
    """Split `total` across strata in proportion to their sizes, giving leftover rows to the largest remainders."""
    whole = sum(sizes.values())
    if total > whole:
        raise ValueError(f"{total} replay rows wanted but the replay file has only {whole}")
    exact = {key: total * size / whole for key, size in sizes.items()}
    counts = {key: int(value) for key, value in exact.items()}
    for key in sorted(exact, key=lambda key: (counts[key] - exact[key], key))[:total - sum(counts.values())]:
        counts[key] += 1
    return counts


def changing_text(row: Example) -> str:
    state = row["state"]
    if isinstance(state, dict):
        return normalize(" ".join(strings(state[list(state)[-1]])))
    return normalize(" ".join(strings(state)))


def replay_sample(train: Sequence[Example], replay: Sequence[Example], protected: Sequence[Example], share: float,
                  seed: int) -> tuple[list[Example], ReplayReport]:
    """The replay rows to add, chosen as this module's docstring describes. Running out of clean rows in a stratum is an
    error: lower the share or supply a larger replay file."""
    if not 0 < share < 1:
        raise ValueError(f"--share must be above 0 and below 1, got {share}")
    strata: dict[str, list[int]] = defaultdict(list)
    for position, row in enumerate(replay):
        strata[stratum(row)].append(position)
    wanted = round(len(train) * share)
    if wanted < 1:
        raise ValueError(f"A share of {share} of {len(train)} training rows is less than one row")
    counts = allocate({key: len(value) for key, value in strata.items()}, wanted)
    guard = Index(protected)
    texts = {text for row in protected for text in [changing_text(row)] if text}
    rng = random.Random(seed)
    chosen: list[Example] = []
    skipped: dict[str, int] = {}
    for key in sorted(strata):
        order = rng.sample(strata[key], len(strata[key]))
        picked = 0
        skipped[key] = 0
        for position in order:
            if picked == counts[key]:
                break
            row = replay[position]
            overlaps = [match for match in guard.match(Features(row)) if match["rule"] != "family"]
            if overlaps or any(normalize(text) in texts for text in strings(row["state"])):
                skipped[key] += 1
                continue
            chosen.append(row)
            picked += 1
        if picked < counts[key]:
            raise ValueError(f"Replay stratum {key!r} ran out of rows that do not overlap the protected files "
                             f"({picked} of {counts[key]}); lower --share or supply more replay rows")
    report: ReplayReport = {"train_rows": len(train), "replay_rows": len(chosen), "share": share,
                            "per_stratum": counts, "skipped_for_overlap": {k: v for k, v in skipped.items() if v}}
    return chosen, report


def mix(train: Sequence[Example], chosen: Sequence[Example], seed: int) -> list[Example]:
    """Training rows and replay rows in one seeded random order. Ids must stay unique across both."""
    clash = {row["id"] for row in train} & {row["id"] for row in chosen}
    if clash:
        raise ValueError(f"Replay rows reuse training ids, for example {sorted(clash)[:5]}; give replay rows distinct ids")
    rows = [*train, *chosen]
    random.Random(seed + 1).shuffle(rows)
    return rows
