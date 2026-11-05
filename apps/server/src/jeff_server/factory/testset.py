"""An independent test set of commands with their correct calls, for a function-calling import (the TypeSafe
cookbook): written by a different model from the factory's teacher, checked by a second judge, reviewed by the owner,
then frozen with a hash before any training run.

    python -m jeff_server.factory.testset write --reference DIR --out DIR --writer URL MODEL --judge URL MODEL [--count 500]

Writer: writes commands and their correct calls, function by function and pattern by pattern (thinking freely; the
JSON after the thinking is taken). Every call is checked against the functions' value lists and defaults
(importing.call_answers); invalid calls are recorded and dropped, never repaired.
Judge: a model from another family reads each command alone and writes the call it implies, without seeing the
writer's. Writer and judge agree when every question the call implies gets the same answer.
Review: agreed items, disputed items (both calls shown) and the cookbook's 14 example commands (their calls as the
cookbook printed them from jev-1.12, so unchecked) go to the owner, who confirms, corrects or drops; at least 100
owner checks and no open dispute are needed to freeze. The frozen file is JSON Lines of
{"id", "command", "function", "arguments"}, the factory's calls format."""

import argparse
import hashlib
import json
import random
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jeff_server.factory.importing import ROUTE, call_answers, load_bundle
from jeff_server.factory.store import read_jsonl, write_json, write_jsonl
from jeff_server.factory.teacher import InvalidReply, Teacher

IN_FLIGHT = 8
PER_CALL = 10
MIN_OWNER_CHECKS = 100
WORD = re.compile(r"[a-z0-9]+")

# The patterns the test must cover (Appendix A of the factory spec).
PATTERNS = [
    ("plain", "a plain, clear request"),
    ("relative_dates", "the time span is given as a relative date, such as 'this quarter', 'lately', 'since last week' or 'past few days'"),
    ("company_names", "companies are named by name or nickname instead of ticker, such as 'nvidia', 'apple', 'the s&p', 'tesla'"),
    ("typos", "casual typing with typos, missing punctuation and abbreviations"),
    ("partial", "the request states some of the function's settings and says nothing about the others"),
    ("lookalike", "the request could be mistaken for a different function but clearly means this one"),
    ("question_form", "the request is phrased as a question or a wish rather than a command"),
]
# Function-specific patterns: two tickers in different roles, several tickers in a comparison.
EXTRA = {"rolling_correlation": ("two_roles", "two tickers in different roles: the one measured, named first, and the "
                                 "yardstick it is compared against, named second"),
         "compare_returns": ("several_tickers", "three or more tickers to compare, named in different ways")}
# How many commands per function: list_symbols and market_summary have few ways to be asked.
WEIGHTS = {"list_symbols": 0.4, "market_summary": 0.6}

THE_FUNCTIONS = """A trading assistant answers short typed requests about six stock symbols (SPY, NVDA, AMD, AAPL, MSFT, \
TSLA) over the last three months of one-minute price bars. For each request it calls exactly one of its functions, \
with the settings the request implies. Settings the request does not mention are left out, and the function then uses \
its default.

The functions and their settings:
{guide}"""


def guide(bundle: dict[str, Any]) -> str:
    """The functions in plain English, from the imported questions (their wording) and the signatures (values,
    defaults, which settings may be left out)."""
    questions, signatures = bundle["questions"], bundle["signatures"]
    lines = []
    for function, description in questions[ROUTE]["criteria"].items():
        lines.append(f"\n{function}: {description}.")
        for name, info in signatures[function].items():
            asked = questions[f"{function}.{name}" if info["shape"] != "set" else f"{function}.{name}.{info['members'][0]}"]
            if info["shape"] == "choice":
                values = "; ".join(f"{json.dumps(k)} = {v}" for k, v in questions[f"{function}.{name}"]["criteria"].items())
                kind = f"one of these values (write the value exactly as shown, in quotes): {values}"
            elif info["shape"] == "set":
                kind = f"a list of tickers, any of {', '.join(json.dumps(m) for m in info['members'])}"
            else:
                kind = "true or false"
            if info["stated_question"]:
                presence = (f"include it whenever the request says anything about it, even when the value it implies is "
                            f"the same as the default {json.dumps(info['default'])}; leave it out only when the request "
                            "says nothing about it")
            elif info["required"]:
                presence = "always needed"
            else:
                presence = f"always set; if the request does not say, it is {json.dumps(info['default'])}"
            wording = asked["instructions"].replace("{}", "<ticker>") if info["shape"] != "set" else "which tickers to compare"
            lines.append(f"  - {name} ({wording}): {kind}; {presence}")
    return "\n".join(lines)


WRITE_PROMPT = """{functions}

Write {count} different requests that a trader or analyst might type to this assistant, each of which should call \
{function}. Requirement for these requests: {pattern}. Make them varied: different tickers, different wording, \
different settings mentioned or left out, short and long, the way people really type in a chat box. For each request, \
give the exact call: every setting the request says anything about, with values exactly as listed above, even when a \
value equals the default (for example "this month" states the window), and no setting the request says nothing \
about. Never include a setting the function does not have, and never a free-text, number or date setting.

Reply with JSON: {{"items": [{{"command": "the request", "arguments": {{"setting": value, ...}}}}, ...]}}"""

JUDGE_PROMPT = """{functions}

A user typed this request:
<<<
{command}
>>>

Which function should the assistant call, and with which settings? Include every setting the request says anything \
about, even when the value equals the default, plus any always-needed ones, with values exactly as listed above; leave \
out every setting the request says nothing about.

Reply with JSON: {{"function": "...", "arguments": {{"setting": value, ...}}}}"""

WRITE_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["items"],
                "properties": {"items": {"type": "array", "items": {
                    "type": "object", "additionalProperties": False, "required": ["command", "arguments"],
                    "properties": {"command": {"type": "string"}, "arguments": {"type": "object"}}}}}}


def judge_schema(functions: list[str]) -> dict[str, Any]:
    return {"type": "object", "additionalProperties": False, "required": ["function", "arguments"],
            "properties": {"function": {"type": "string", "enum": functions}, "arguments": {"type": "object"}}}


def plan(signatures: dict[str, Any], count: int, seed: int) -> list[dict[str, Any]]:
    """Writer calls: per function a share of `count` (list_symbols and market_summary smaller), spread over the
    patterns (plus the function's own pattern), PER_CALL commands per call."""
    weights = {name: WEIGHTS.get(name, 1.0) for name in signatures}
    total = sum(weights.values())
    jobs = []
    rng = random.Random(seed)
    for function, weight in weights.items():
        calls = max(1, round(count * weight / total / PER_CALL))
        patterns = PATTERNS + ([EXTRA[function]] if function in EXTRA else [])
        if not signatures[function]:  # no settings: partial and two-role patterns do not apply
            patterns = [p for p in patterns if p[0] not in ("partial",)]
        for index in range(calls):
            kind, text = patterns[index % len(patterns)]
            jobs.append({"function": function, "pattern": kind, "pattern_text": text, "index": index})
    rng.shuffle(jobs)
    return jobs


def normal(text: str) -> str:
    return " ".join(WORD.findall(text.lower()))


def write(writer: Teacher, judge: Teacher, bundle: dict[str, Any], count: int, seed: int, out: Path) -> dict[str, Any]:
    signatures = bundle["signatures"]
    functions = THE_FUNCTIONS.format(guide=guide(bundle))
    jobs = plan(signatures, count, seed)
    written: list[dict[str, Any]] = []
    invalid: list[dict[str, Any]] = []
    failed_calls: list[dict[str, Any]] = []

    def one(job: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]], str]:
        def check(value: Any) -> None:
            if len(value["items"]) < PER_CALL * 0.8:
                raise ValueError(f"asked for {PER_CALL} requests, got {len(value['items'])}")
        reply, model = writer.ask(WRITE_PROMPT.format(functions=functions, count=PER_CALL, function=job["function"],
                                                      pattern=job["pattern_text"]), WRITE_SCHEMA, "test_commands", check=check)
        return job, reply["items"], model

    with ThreadPoolExecutor(IN_FLIGHT) as pool:
        for future in as_completed([pool.submit(one, job) for job in jobs]):
            try:
                job, items, model = future.result()
            except InvalidReply as error:
                failed_calls.append({"error": str(error)})
                continue
            for item in items:
                call = {"function": job["function"], "arguments": item["arguments"]}
                record = {"command": item["command"].strip(), "writer_call": call, "writer": model,
                          "pattern": job["pattern"], "function": job["function"]}
                try:
                    call_answers(call, signatures)
                except ValueError as error:
                    invalid.append({**record, "error": str(error)})
                    continue
                written.append(record)
    seen: set[str] = set()
    unique = []
    for record in sorted(written, key=lambda r: (r["function"], r["pattern"], r["command"])):
        key = normal(record["command"])
        if key and key not in seen:
            seen.add(key)
            unique.append(record)
    print(f"written {len(written)}, invalid {len(invalid)}, duplicates {len(written) - len(unique)}, "
          f"failed writer calls {len(failed_calls)}", flush=True)

    def blind(record: dict[str, Any]) -> dict[str, Any]:
        def check(value: Any) -> None:
            call_answers(value, signatures)
        try:
            reply, model = judge.ask(JUDGE_PROMPT.format(functions=functions, command=record["command"]),
                                     judge_schema(list(signatures)), "judge_call", check=check)
        except InvalidReply as error:
            return {**record, "judge_call": None, "judge_error": str(error), "status": "disputed"}
        agree = call_answers(record["writer_call"], signatures) == call_answers(reply, signatures)
        return {**record, "judge_call": reply, "judge": model, "status": "agreed" if agree else "disputed"}

    with ThreadPoolExecutor(IN_FLIGHT) as pool:
        judged = list(pool.map(blind, unique))
    for number, record in enumerate(judged, 1):
        record["id"] = f"t{number:04d}"
    write_json(out / "signatures.json", signatures)  # what calls are checked against, kept with the set
    write_jsonl(out / "candidates.jsonl", judged)
    write_jsonl(out / "invalid.jsonl", invalid)
    summary = {"created": datetime.now(timezone.utc).isoformat(timespec="seconds"), "reference": bundle["name"],
               "writer": writer.model, "judge": judge.model, "requested": count, "writer_calls": len(jobs),
               "failed_writer_calls": len(failed_calls), "written": len(written), "invalid": len(invalid),
               "duplicates": len(written) - len(unique), "agreed": sum(r["status"] == "agreed" for r in judged),
               "disputed": sum(r["status"] == "disputed" for r in judged), "seed": seed,
               "prompts_sha256": hashlib.sha256((WRITE_PROMPT + JUDGE_PROMPT + functions).encode()).hexdigest()}
    write_json(out / "summary.json", summary)
    return summary


# --- review and freeze -----------------------------------------------------------------------------------------------

def signatures_of(folder: Path) -> dict[str, Any]:
    return json.loads((folder / "signatures.json").read_text())


def load(folder: Path) -> dict[str, Any]:
    candidates = read_jsonl(folder / "candidates.jsonl")
    reviews_path = folder / "reviews.json"
    reviews = json.loads(reviews_path.read_text()) if reviews_path.exists() else {}
    frozen = folder / "frozen.json"
    return {"summary": json.loads((folder / "summary.json").read_text()), "candidates": candidates, "reviews": reviews,
            "frozen": json.loads(frozen.read_text()) if frozen.exists() else None}


def review(folder: Path, item_id: str, verdict: str, call: dict[str, Any] | None, signatures: dict[str, Any]) -> dict[str, Any]:
    """The owner's verdict on one item: confirm (the writer's call is right), correct (with the right call), or drop."""
    if (folder / "frozen.json").exists():
        raise ValueError("This test set is frozen")
    state = load(folder)
    item = next((c for c in state["candidates"] if c["id"] == item_id), None)
    if item is None:
        raise KeyError(item_id)
    if verdict not in ("confirm", "correct", "drop"):
        raise ValueError("verdict must be confirm, correct or drop")
    if verdict == "correct":
        if call is None:
            raise ValueError("A correction needs the right call")
        call_answers(call, signatures)
    entry = {"verdict": verdict, "call": call if verdict == "correct" else None,
             "at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    reviews = {**state["reviews"], item_id: entry}
    write_json(folder / "reviews.json", reviews)
    return entry


def freeze(folder: Path, signatures: dict[str, Any], waiver: str | None = None) -> dict[str, Any]:
    """Write the frozen test set: confirmed and corrected items, plus agreed items not reviewed; never a disputed item
    without the owner's verdict. Needs MIN_OWNER_CHECKS reviews, every cookbook command among them.

    waiver: the owner's instruction to freeze without the hand-checks, quoted. Then agreed items and the cookbook's
    commands are taken as they are, disputed items without a verdict are dropped (two models did not agree, so neither
    call is safe gold), and the frozen record says so: no review is invented."""
    state = load(folder)
    if state["frozen"]:
        raise ValueError("This test set is already frozen")
    reviews, rows, open_items, dropped_disputed = state["reviews"], [], [], []
    for item in state["candidates"]:
        verdict = reviews.get(item["id"])
        if verdict is None and item["status"] == "disputed" and waiver:
            dropped_disputed.append(item["id"])
            continue
        if verdict is None and item["status"] not in ("agreed",) and not (waiver and item["status"] == "cookbook"):
            open_items.append(item["id"])
            continue
        if verdict and verdict["verdict"] == "drop":
            continue
        call = verdict["call"] if verdict and verdict["verdict"] == "correct" else item["writer_call"]
        call_answers(call, signatures)
        rows.append({"id": item["id"], "command": item["command"], "function": call["function"],
                     "arguments": call["arguments"], "source": item.get("source", "written"),
                     "checked_by_owner": verdict is not None})
    if open_items:
        raise ValueError(f"{len(open_items)} items need your verdict first (disputed or from the cookbook): {open_items[:8]}")
    if len(reviews) < MIN_OWNER_CHECKS and not waiver:
        raise ValueError(f"{len(reviews)} items checked; at least {MIN_OWNER_CHECKS} are needed before freezing")
    text = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    (folder / "frozen.jsonl").write_text(text, encoding="utf-8")
    info = {"frozen": datetime.now(timezone.utc).isoformat(timespec="seconds"), "items": len(rows),
            "checked_by_owner": sum(r["checked_by_owner"] for r in rows), "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "by_function": {f: sum(r["function"] == f for r in rows) for f in signatures},
            "owner_checks_waived": waiver, "disputed_dropped_unreviewed": len(dropped_disputed),
            "cookbook_accepted_unreviewed": sum(1 for r in rows if r["source"] == "cookbook" and not r["checked_by_owner"])}
    write_json(folder / "frozen.json", info)
    return info


def add_cookbook(folder: Path, commands: list[dict[str, Any]], signatures: dict[str, Any]) -> None:
    """The cookbook's example commands, with the calls the cookbook printed (jev-1.12's answers, so unchecked: the
    owner must confirm or correct each before freezing)."""
    candidates = read_jsonl(folder / "candidates.jsonl")
    for number, item in enumerate(commands, 1):
        call = {"function": item["function"], "arguments": item["arguments"]}
        call_answers(call, signatures)
        candidates.append({"id": f"c{number:02d}", "command": item["command"], "writer_call": call, "writer": "cookbook (jev-1.12)",
                           "pattern": "cookbook", "function": item["function"], "status": "cookbook", "source": "cookbook"})
    write_jsonl(folder / "candidates.jsonl", candidates)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("action", choices=["write"])
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--writer", nargs=2, metavar=("URL", "MODEL"), required=True)
    parser.add_argument("--judge", nargs=2, metavar=("URL", "MODEL"), required=True)
    parser.add_argument("--cookbook", type=Path, required=True, help="JSON Lines of the cookbook's example commands and calls")
    parser.add_argument("--count", type=int, default=500)
    parser.add_argument("--seed", type=int, default=20261017)
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    bundle = load_bundle(args.reference)
    # Qwen3 on the B200's vLLM has no reasoning parser: it thinks inside the content, so it writes freely.
    writer = Teacher(args.writer[0], "none", args.writer[1], args.out / "cache-writer", settings={"temperature": 0.8}, structured=False)
    judge = Teacher(args.judge[0], "none", args.judge[1], args.out / "cache-judge", settings={"temperature": 0.0})
    summary = write(writer, judge, bundle, args.count, args.seed, args.out)
    add_cookbook(args.out, read_jsonl(args.cookbook), bundle["signatures"])
    print(json.dumps(summary, indent=1))


if __name__ == "__main__":
    main()
