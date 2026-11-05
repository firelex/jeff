"""Checks before training, shown in the console: duplicates and near-copies (dropped, and counted), rows per split
(trimmed to exactly the spec's sizes; too few is an error), label balance, token lengths, instruction shares, and a
sample of 20 rows to read.

Near-copies use jeff-asic's text_overlap logic: texts are lower-cased, cut into 8-word shingles, and a text whose
shingles are at least NEAR_COPY of one in the training split counts as a near-copy. Texts under 20 words are too short
to judge that way; for them only exact copies (after normalising case, punctuation and spacing) count."""

import hashlib
import random
import re
import statistics
from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

SHINGLE_WORDS = 8
MIN_WORDS = 20
NEAR_COPY = 0.5
WORD = re.compile(r"[a-z0-9]+")
SPLITS = ("train", "dev", "calibration", "test")
SAMPLE_ROWS = 20


def normal(text: str) -> str:
    return " ".join(WORD.findall(text.lower()))


def shingles(text: str) -> set[bytes]:
    words = WORD.findall(text.lower())
    return {hashlib.blake2b(" ".join(words[i:i + SHINGLE_WORDS]).encode(), digest_size=8).digest()
            for i in range(len(words) - SHINGLE_WORDS + 1)}


def live_text(row: dict[str, Any], live: str) -> str:
    return row["state"] if isinstance(row["state"], str) else row["state"][live]


def scenario_key(row: dict[str, Any]) -> tuple[str, int]:
    return row["split"], row["source"]["scenario"]


def run_checks(spec: dict[str, Any], rows: list[dict[str, Any]], seed: int,
               count_tokens: Callable[[dict[str, Any]], int], avoid: list[str] | None = None
               ) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    """`avoid`: the independent test set's texts. Generated requests that copy one exactly, or near-copy a long one,
    are dropped from every split, so no training row repeats a test command."""
    """The final split files and the report. Whole requests (all rows of one scenario) are dropped or kept together."""
    live = spec["live_field"]["name"]
    scenarios: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        scenarios[scenario_key(row)].append(row)
    order = sorted(scenarios, key=lambda key: (SPLITS.index(key[0]), key[1]))

    # 1. Exact duplicates of the changing text anywhere (the first one, train first, is kept), and exact copies of an
    # independent test command.
    seen: dict[str, str] = {normal(text): "independent" for text in avoid or []}
    duplicates = Counter[str]()
    kept_keys = []
    exact_independent = Counter[str]()
    for key in order:
        text = normal(live_text(scenarios[key][0], live))
        if text in seen:
            (exact_independent if seen[text] == "independent" else duplicates)[key[0]] += 1
            continue
        seen[text] = key[0]
        kept_keys.append(key)

    # 2. Near-copies of a training text in dev, calibration and test, and of an independent test command anywhere
    # (long texts only).
    independent_shingles: set[bytes] = set().union(*(shingles(text) for text in avoid or []))
    near_independent = Counter[str]()
    train_shingles: set[bytes] = set()
    for key in kept_keys:
        if key[0] == "train":
            train_shingles |= shingles(live_text(scenarios[key][0], live))
    near = Counter[str]()
    judged = Counter[str]()
    final_keys = []
    for key in kept_keys:
        text = live_text(scenarios[key][0], live)
        if independent_shingles and len(WORD.findall(text.lower())) >= MIN_WORDS:
            own = shingles(text)
            if own and len(own & independent_shingles) / len(own) >= NEAR_COPY:
                near_independent[key[0]] += 1
                continue
        if key[0] != "train" and len(WORD.findall(text.lower())) >= MIN_WORDS:
            own = shingles(text)
            judged[key[0]] += 1
            if own and len(own & train_shingles) / len(own) >= NEAR_COPY:
                near[key[0]] += 1
                continue
        final_keys.append(key)

    # 3. Trim each split to exactly its size: shuffle its requests, keep whole requests while they fit, then cut rows.
    rng = random.Random(seed)
    files: dict[str, list[dict[str, Any]]] = {}
    for split in SPLITS:
        keys = [key for key in final_keys if key[0] == split]
        rng.shuffle(keys)
        chosen: list[dict[str, Any]] = []
        for key in keys:
            if len(chosen) >= spec["sizes"][split]:
                break
            chosen.extend(scenarios[key])
        if len(chosen) < spec["sizes"][split]:
            raise ValueError(f"{split}: only {len(chosen)} rows remain after removing duplicates and near-copies, "
                             f"{spec['sizes'][split]} needed")
        files[split] = [{key: value for key, value in row.items() if key != "split"} for row in chosen[:spec["sizes"][split]]]

    report: dict[str, Any] = {"rows": {split: len(files[split]) for split in SPLITS},
                              "requests_written": {split: sum(1 for key in order if key[0] == split) for split in SPLITS},
                              "duplicates_dropped": dict(duplicates), "near_copies_dropped": dict(near),
                              "independent_test_copies_dropped": {"exact": dict(exact_independent),
                                                                  "near": dict(near_independent)},
                              "near_copy_judged": dict(judged), "near_copy_rule": f"{SHINGLE_WORDS}-word shingles, "
                              f"containment >= {NEAR_COPY}, texts of {MIN_WORDS}+ words"}
    report["families"] = {split: len({row["family"] for row in files[split]}) for split in SPLITS}
    report["test_family_overlap"] = len({row["family"] for row in files["test"]} &
                                        {row["family"] for split in ("train", "dev", "calibration") for row in files[split]})
    if report["test_family_overlap"]:
        raise ValueError(f"{report['test_family_overlap']} test families also appear in training splits")
    report["balance"] = {}
    for question in spec["questions"]:
        counts = Counter(str(row["label"]) for row in files["train"] if row["source"]["question_id"] == question["id"])
        report["balance"][question["id"]] = dict(sorted(counts.items(), key=lambda item: -item[1]))
    report["instructions"] = {split: dict(Counter(row["source"]["instruction"] for row in files[split])) for split in SPLITS}
    report["kinds"] = dict(Counter(row["source"]["kind"] for row in files["train"]))
    lengths = sorted(count_tokens(row) for split in SPLITS for row in files[split])
    report["tokens"] = {"min": lengths[0], "median": statistics.median(lengths),
                        "p95": lengths[int(len(lengths) * 0.95) - 1], "max": lengths[-1],
                        "histogram": histogram(lengths)}
    if lengths[-1] > 8192:
        raise ValueError(f"The longest row has {lengths[-1]} tokens; the limit is 8,192")
    sample = rng.sample(files["train"], min(SAMPLE_ROWS, len(files["train"])))
    report["sample"] = [{"family": row["family"], "state": row["state"], "question": row["question"],
                         "label": row["label"], "kind": row["source"]["kind"], "style": row["source"]["style"],
                         "instruction": row["source"]["instruction"]} for row in sample]
    return files, report


def histogram(values: list[int], bins: int = 12) -> list[dict[str, int]]:
    low, high = values[0], values[-1]
    width = max(1, -(-(high - low + 1) // bins))
    counts = Counter((value - low) // width for value in values)
    return [{"from": low + index * width, "to": low + (index + 1) * width - 1, "count": counts.get(index, 0)}
            for index in range(bins) if low + index * width <= high]
