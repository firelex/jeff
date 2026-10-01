"""Build the three ablation training sets and the LR-sweep set, after removing anything that copies the evaluation panel."""

import argparse
import json
import re
from collections import Counter
from pathlib import Path
from typing import cast

from jeff import adversarial, escape, layout
from jeff.data import NearDuplicates, choose, digest, normalized, ranking, text_parts, validate, write_rows
from jeff.types import Example


def exact_key(row: Example) -> str:
    return digest("\n".join(normalized(text) for text in text_parts(row)))


# A text found in this many panel items is a template (a fixed instruction or prompt), not part of any one item.
# Real content repeats far less: a RAGTruth article appears once per model response, at most 6 times in the panel.
SHARED_AT = 20
# Option descriptions with at least this many words are item content (candidate answers), not labels like "Yes".
OPTION_WORDS = 5
WORD = re.compile(r"[^\W_]+")


def loose(text: str) -> str:
    """Case folded, everything but letters and digits removed, spaces collapsed: "Quiet!" and "quiet" are the same."""
    return " ".join(WORD.findall(text.casefold()))


def state_text(row: Example) -> str:
    state = row["state"]
    return loose(state if isinstance(state, str) else json.dumps(state, sort_keys=True, ensure_ascii=False))


def option_texts(row: Example) -> list[str]:
    question = row["question"]
    if question["type"] != "choice":
        return []
    texts = [loose(value) for value in (question.get("criteria") or {}).values() if isinstance(value, str)]
    return [text for text in texts if len(text.split()) >= OPTION_WORDS]


def source_records(row: Example) -> set[str]:
    """Upstream records a row comes from, when its source names them: an Amazon MASSIVE utterance id (unique across
    MASSIVE's splits and shared by its locales), a MAUD or CUAD contract, a ContractNLI document, a ConditionalQA page. MAUD splits by question, not by
    contract, so its train and dev rows share contracts."""
    source = cast(dict[str, object], row.get("source") or {})
    dataset = str(source.get("dataset", row.get("suite")))
    records = set()
    if dataset == "massive" and "upstream_id" in source:
        records.add(f"massive:{str(source['upstream_id']).removeprefix('massive-')}")
    elif dataset == "voice_navigation" and "massive_id" in source:
        records.add(f"massive:{source['massive_id']}")
    elif dataset == "longlists-massive" and "upstream_id" in source:
        records.add(f"massive:{source['upstream_id']}")
    elif dataset in ("maud", "cuad") and "contract" in source:
        records.add(f"{dataset}:{source['contract']}")
    elif dataset == "contract_nli" and "document" in source:
        records.add(f"contract_nli:{source['document']}")
    elif dataset == "conditional_qa" and "url" in source:
        records.add(f"conditional_qa:{source['url']}")
    return records


class LeakGuard:
    """Flags a row that copies an evaluation item: the same item text (exact), a near-identical long text (5-word
    shingles, texts of 20+ words), the same state at ANY length (short texts such as voice commands included), the
    same candidate answer in the options (5+ words, found in one evaluation item only), or the same upstream record (see source_records). Texts shared by
    SHARED_AT or more evaluation items are fixed prompts and are not matched."""

    def __init__(self, panel: list[Example]) -> None:
        self.exact = {exact_key(row) for row in panel}
        counts = Counter(text for row in panel for text in {normalized(part) for part in text_parts(row)})
        self.near = NearDuplicates(frozenset(text for text, count in counts.items() if count >= SHARED_AT))
        for row in panel:
            self.near.add(row)
        states = Counter(state_text(row) for row in panel)
        options = Counter(text for row in panel for text in set(option_texts(row)))
        self.states = {text for text, count in states.items() if text and count < SHARED_AT}
        # A candidate answer belongs to one item; option text found in two or more items is a label (an intent name,
        # a MAUD answer choice), not item content.
        self.options = {text for text, count in options.items() if count == 1}
        self.records = {record for row in panel for record in source_records(row)}

    def leaks(self, row: Example) -> bool:
        return (exact_key(row) in self.exact or state_text(row) in self.states
                or any(text in self.options for text in option_texts(row))
                or not self.records.isdisjoint(source_records(row)) or self.near.matches(row))


def read(path: Path) -> list[Example]:
    return [json.loads(line) for line in path.read_text().split("\n") if line]  # not splitlines(): see generate.load_outcomes


def build(public: list[Example], synthetic: list[Example], dev: list[Example], calibration: list[Example],
          panel: list[Example], size: int, sweep_size: int, seed: int) -> tuple[dict[str, list[Example]], dict[str, object]]:
    guard = LeakGuard(panel)
    inputs = {"public": public, "synthetic": synthetic, "dev": dev, "calibration": calibration}
    clean = {name: [row for row in rows if not guard.leaks(row)] for name, rows in inputs.items()}
    chosen_public = sorted(choose(clean["public"], size, seed), key=lambda row: row["id"])
    if len(clean["synthetic"]) < len(chosen_public):
        raise ValueError(f"Only {len(clean['synthetic'])} clean synthetic rows; {len(chosen_public)} needed to match public")
    chosen_synthetic = sorted(sorted(clean["synthetic"], key=lambda row: ranking(row["id"], seed))[:len(chosen_public)],
                              key=lambda row: row["id"])
    combined = chosen_public + chosen_synthetic
    sweep = sorted(sorted(combined, key=lambda row: ranking(row["id"], seed + 1))[:sweep_size], key=lambda row: row["id"])
    sets = {"public": chosen_public, "synthetic": chosen_synthetic, "combined": combined, "sweep": sweep,
            "dev": clean["dev"], "calibration": clean["calibration"]}
    for rows in sets.values():
        validate(rows)
    report: dict[str, object] = {"leaks": {name: len(inputs[name]) - len(clean[name]) for name in inputs},
                                 "sizes": {name: len(rows) for name, rows in sets.items()}, "seed": seed}
    return sets, report


def build_public(public: list[Example], dev: list[Example], calibration: list[Example], panel: list[Example], size: int,
                 sweep_size: int, seed: int) -> tuple[dict[str, list[Example]], dict[str, object]]:
    """The public-only arm before synthetic data exists. Its public set is identical to the one build() picks."""
    guard = LeakGuard(panel)
    inputs = {"public": public, "dev": dev, "calibration": calibration}
    clean = {name: [row for row in rows if not guard.leaks(row)] for name, rows in inputs.items()}
    chosen_public = sorted(choose(clean["public"], size, seed), key=lambda row: row["id"])
    sweep = sorted(sorted(chosen_public, key=lambda row: ranking(row["id"], seed + 1))[:sweep_size], key=lambda row: row["id"])
    sets = {"public": chosen_public, "sweep": sweep, "dev": clean["dev"], "calibration": clean["calibration"]}
    for rows in sets.values():
        validate(rows)
    report: dict[str, object] = {"leaks": {name: len(inputs[name]) - len(clean[name]) for name in inputs},
                                 "sizes": {name: len(rows) for name, rows in sets.items()}, "seed": seed, "public_only": True}
    return sets, report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public", type=Path, default=Path("data/public/train.jsonl"))
    parser.add_argument("--extra", type=Path, nargs="+", default=[],
                        help="Extra JSONL files whose rows are appended to --public before mixing")
    parser.add_argument("--synthetic", type=Path, default=Path("gen/full/synthetic.jsonl"))
    parser.add_argument("--dev", type=Path, default=Path("data/public/dev.jsonl"))
    parser.add_argument("--calibration", type=Path, default=Path("data/public/temperature.jsonl"))
    parser.add_argument("--panel", type=Path, default=Path("data/panel.jsonl"))
    parser.add_argument("--also-exclude", type=Path, nargs="+",
                        default=[Path("data/jevbench-hard.jsonl"), Path("data/documents/check.jsonl"), Path("data/voice/test.jsonl"),
                                 Path("data/longlists-test/test.jsonl")],
                        help="Further evaluation sets the leak filter protects, like the panel (every file must exist)")
    parser.add_argument("--size", type=int, default=50000)
    parser.add_argument("--sweep-size", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20260920)
    parser.add_argument("--out", type=Path, default=Path("data/mix"))
    parser.add_argument("--public-only", action="store_true", help="Build only the public arm; --synthetic is not read")
    parser.add_argument("--adversarial", action="store_true",
                        help=f"Plant a hijack attempt in {adversarial.SHARE:.0%} of the training rows (jeff.adversarial)")
    parser.add_argument("--escape", action="store_true",
                        help=f"Add an escape option to {escape.SHARE:.0%} of eligible choice rows (jeff.escape)")
    parser.add_argument("--panel-layout", action="store_true",
                        help="Rewrite a share of the training rows into the panel's layouts and balance answer positions (jeff.layout)")
    args = parser.parse_args()
    public = read(args.public)
    for path in args.extra:
        public = public + read(path)
    synthetic = [] if args.public_only else read(args.synthetic)
    layouts: dict[str, object] | None = None
    if args.panel_layout:
        public, public_layouts = layout.rearrange(public, args.seed)
        synthetic, synthetic_layouts = layout.rearrange(synthetic, args.seed)
        layouts = {"public": public_layouts, "synthetic": synthetic_layouts}
    escaped: dict[str, int] | None = None
    if args.escape:
        public, public_escaped = escape.add(public, args.seed)
        synthetic, synthetic_escaped = escape.add(synthetic, args.seed)
        escaped = {"public": public_escaped, "synthetic": synthetic_escaped}
    planted: dict[str, int] | None = None
    if args.adversarial:
        public, public_planted = adversarial.add(public, args.seed)
        synthetic, synthetic_planted = adversarial.add(synthetic, args.seed)
        planted = {"public": public_planted, "synthetic": synthetic_planted}
    if args.public_only:
        sets, report = build_public(public, read(args.dev), read(args.calibration), read(args.panel) + [row for path in args.also_exclude for row in read(path)],
                                    args.size, args.sweep_size, args.seed)
    else:
        sets, report = build(public, synthetic, read(args.dev), read(args.calibration), read(args.panel) + [row for path in args.also_exclude for row in read(path)],
                             args.size, args.sweep_size, args.seed)
    report["layouts"] = layouts
    report["adversarial_rows"] = planted
    report["escape_rows"] = escaped
    report["sha256"] = {name: write_rows(args.out / f"{name}.jsonl", rows) for name, rows in sets.items()}
    (args.out / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
