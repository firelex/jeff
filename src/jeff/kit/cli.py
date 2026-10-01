"""jeff-kit: check and prepare the training data of a Jeff adapter. See examples/adapter-kit/README.md."""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from jeff.kit import leaks, measure, replay, shortcuts, split
from jeff.kit.rows import RowError, load, write_jsonl

SHOWN_LEAKS = 50


def check_rows_command(args: argparse.Namespace) -> None:
    rows = load(args.file)
    print(f"{args.file}: {len(rows):,} rows, {len({row['family'] for row in rows})} families: all rows valid")


def split_command(args: argparse.Namespace) -> None:
    rows = load(args.file)
    paths = split.output_paths(args.out)
    for path in paths.values():
        if path.exists():
            raise FileExistsError(f"Refusing to overwrite {path}; choose an empty --out folder")
    splits = split.split_by_family(rows, {"test": args.test, "development": args.development,
                                          "calibration": args.calibration}, args.seed)
    for name, path in paths.items():
        write_jsonl(path, splits[name])
    print(split.summary(splits))
    print(f"\nWrote {', '.join(str(path) for path in paths.values())}")


def leak_check_command(args: argparse.Namespace) -> None:
    train = load(args.train)
    features = [leaks.Features(row) for row in train]
    offending: dict[str, list[leaks.Leak]] = {}
    print(f"leak check: {len(train):,} training rows from {args.train}")
    for path in args.against:
        evaluation = load(path)
        index = leaks.Index(evaluation)
        found: list[leaks.Leak] = []
        for row, row_features in zip(train, features, strict=True):
            matches = index.match(row_features)
            if matches:
                found.append({"train_id": row["id"], "matches": matches})
        rules = Counter(match["rule"] for leak in found for match in leak["matches"])
        print(f"  {path}: {len(evaluation):,} rows, {len(index.templates)} shared prompt texts ignored; "
              f"{len(found):,} overlapping training rows" + (f" ({', '.join(f'{k} {v}' for k, v in sorted(rules.items()))})" if rules else ""))
        if found:
            offending[str(path)] = found
    if args.report is not None:
        args.report.write_text(json.dumps(offending, indent=1) + "\n")
    if offending:
        lines = [f"  {name}: {leak['train_id']} -> {leak['matches'][0]['evaluation_id']} "
                 f"({', '.join(match['rule'] for match in leak['matches'])})"
                 for name, found in offending.items() for leak in found]
        more = f"\n  ... and {len(lines) - SHOWN_LEAKS} more" if len(lines) > SHOWN_LEAKS else ""
        print("\nleak check FAILED. Training id -> evaluation id (rules):\n" + "\n".join(lines[:SHOWN_LEAKS]) + more,
              file=sys.stderr)
        raise SystemExit(1)
    print("leak check passed: no training row overlaps an evaluation row")


def shortcut_report_command(args: argparse.Namespace) -> None:
    train, test = load(args.train), load(args.test)
    text, flags = shortcuts.shortcut_report(train, test, text_field=args.text_field, class_field=args.class_field,
                                            names=(str(args.train), str(args.test)), seed=args.seed)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text, encoding="utf-8")
    print(f"Wrote {args.out}: {len(flags)} flag(s)")
    for flag in flags:
        print(f"  - {flag}")


def replay_mix_command(args: argparse.Namespace) -> None:
    train, extra = load(args.train), load(args.replay)
    protected = [row for path in args.protect for row in load(path)]
    chosen, report = replay.replay_sample(train, extra, protected, args.share, args.seed)
    write_jsonl(args.out, replay.mix(train, chosen, args.seed))
    print(json.dumps(report, indent=1))
    print(f"Wrote {args.out}: {len(train):,} training rows + {len(chosen):,} replay rows")


def evaluate_command(args: argparse.Namespace) -> None:
    planned = measure.commands(args.test, args.base, args.adapter, args.out, untrained=args.untrained,
                               untrained_revision=args.untrained_revision, calibration=args.calibration,
                               orders=args.orders, batch_size=args.batch_size)
    results = measure.run(planned, args.out)
    print()
    print(measure.results_block(results))
    if args.untrained is None:
        print("\nThe untrained base model was not measured: add --untrained MODEL --untrained-revision COMMIT.")


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(prog="jeff-kit", description=__doc__)
    commands = top.add_subparsers(dest="command", required=True)

    p = commands.add_parser("check-rows", help="Validate every row of a JSON Lines file")
    p.add_argument("file", type=Path)
    p.set_defaults(run=check_rows_command)

    p = commands.add_parser("split", help="Split rows by family into train, development, calibration and test")
    p.add_argument("file", type=Path)
    p.add_argument("--out", type=Path, required=True, help="Folder for train.jsonl, development.jsonl, calibration.jsonl, test.jsonl")
    p.add_argument("--test", type=float, required=True, help="Share of rows for test, for example 0.1")
    p.add_argument("--development", type=float, required=True, help="Share of rows for development (choosing checkpoints)")
    p.add_argument("--calibration", type=float, required=True, help="Share of rows for calibration (fitting the temperature)")
    p.add_argument("--seed", type=int, required=True)
    p.set_defaults(run=split_command)

    p = commands.add_parser("leak-check", help="Fail if an evaluation row, or a near copy, appears in training",
                            description=leaks.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--train", type=Path, required=True)
    p.add_argument("--against", type=Path, nargs="+", required=True, help="Evaluation files to protect (test, development, calibration)")
    p.add_argument("--report", type=Path, help="Also write every overlapping row as JSON here")
    p.set_defaults(run=leak_check_command)

    p = commands.add_parser("shortcut-report", help="Write a Markdown report of surface shortcuts",
                            description=shortcuts.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--train", type=Path, required=True)
    p.add_argument("--test", type=Path, required=True)
    p.add_argument("--out", type=Path, required=True, help="Markdown report to write")
    p.add_argument("--text-field", help="State field holding the main text (default: the state's last field)")
    p.add_argument("--class-field", help="Dotted path naming each row's kind, for example source.round, to compare kinds of row")
    p.add_argument("--seed", type=int, default=0, help="Seed for sampling training rows above 50,000")
    p.set_defaults(run=shortcut_report_command)

    p = commands.add_parser("replay-mix", help="Add a share of replay rows that you supply to a training file",
                            description=replay.__doc__ + "\n\nNo replay data ships with Jeff: supply your own file, or "
                            "skip this step and train on your rows alone.", formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--train", type=Path, required=True)
    p.add_argument("--replay", type=Path, required=True, help="Your replay rows (Jeff ships none)")
    p.add_argument("--protect", type=Path, nargs="+", required=True,
                   help="Evaluation files the replay rows must not overlap (test, development, calibration)")
    p.add_argument("--share", type=float, required=True, help="Replay rows as a share of the training rows, for example 0.10")
    p.add_argument("--out", type=Path, required=True, help="The combined training file to write")
    p.add_argument("--seed", type=int, required=True)
    p.set_defaults(run=replay_mix_command)

    p = commands.add_parser("evaluate", help="Measure the untrained base, Jeff alone and Jeff with the adapter on one test set",
                            description=measure.__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--test", type=Path, required=True)
    p.add_argument("--base", type=Path, required=True, help="Jeff checkpoint folder (decision_config.json)")
    p.add_argument("--adapter", type=Path, required=True, help="Jeff with the adapter merged in, as a checkpoint folder")
    p.add_argument("--out", type=Path, required=True, help="Folder for each measurement's summary and predictions")
    p.add_argument("--untrained", help="Hugging Face id of the untrained base model, for example Qwen/Qwen3.5-0.8B")
    p.add_argument("--untrained-revision", help="40-character commit of --untrained")
    p.add_argument("--calibration", type=Path, help="Fit the untrained model's temperature on these rows first")
    p.add_argument("--orders", type=int, default=1, help="2: also answer with options reversed and average (twice the cost)")
    p.add_argument("--batch-size", type=int, default=8)
    p.set_defaults(run=evaluate_command)
    return top


def main(argv: list[str] | None = None) -> None:
    args = parser().parse_args(argv)
    try:
        args.run(args)
    except (RowError, ValueError, FileNotFoundError, FileExistsError) as error:
        raise SystemExit(f"jeff-kit {args.command}: {error}") from error


if __name__ == "__main__":
    main()
