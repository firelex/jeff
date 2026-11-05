"""Build catalogue/<id>/manifest.yaml for the open adapters from jeffhub's manifests and the adapters' Hugging Face
files (example.json for the exact instructions, test.jsonl for the label sets). Rules: plan Task 12, plus the hand
decisions below (each one is written into the manifest's hub.notes). Nothing is ever uploaded."""

import json
import sys
from pathlib import Path

import yaml
from huggingface_hub import hf_hub_download, list_repo_files, snapshot_download

from catalogue_import_common import NUMBERED, choice_labels, function_of, question_from_rows, read_rows

ROOT = Path(__file__).resolve().parents[1]
JEFFHUB = ROOT.parent / "jeffhub" / "adapters"
INTERFACES = {
    ("aml", "suspicious"): "classify", ("aml", "action"): "classify", ("code", "tool"): "route",
    ("code", "argument"): "route", ("code-router", "thinking"): "classify", ("emotion", "emotion"): "classify",
    ("ground", "grounding"): "is_supported", ("guard", "attempt"): "detect", ("guard", "kind"): "classify",
    ("legal-clauses", "clause_type"): "classify", ("nav", "target"): "route", ("sanctions", "screening"): "equivalent",
    ("soc", "action"): "classify", ("spam", "is_spam"): "detect", ("spam", "message_type"): "classify",
    ("support-intents", "intent"): "classify", ("tools", "tool"): "route", ("triage", "route"): "route",
    ("triage", "urgency"): "rate", ("triage", "sentiment"): "rate", ("triage", "needs_human"): "requires_escalation",
}
FIXED_VALUES = {"is_supported": ["supported", "partly_supported", "contradicted", "unsupported"],
                "equivalent": ["match", "possible_match", "no_match"]}


def interface_of(name: str, key: str) -> str:
    if name == "trading-desk":
        return "classify"
    if (name, key) not in INTERFACES:
        sys.exit(f"{name}.{key}: no interface in the table (plan Task 12)")
    return INTERFACES[(name, key)]


# Hand decisions, each recorded in the manifest's hub.notes.
# guard: the test row ids end with the question type (":noul", ":choice"), not the function key; its two functions
# have different question types (attempt is noul, kind is choice), so the type names the function.
FUNCTION_BY = {"guard": "question_type"}
# ground: jeffhub's request.rules describe a second request shape (passage re-ranking: state key `question`, options
# `none` and p1, p2, ...) that the test set mixes in. This manifest has the grounding shape only, so the rows of the
# other shape (state keys exactly these) are not its rows.
OTHER_SHAPE = {"ground": ["question"],
               # aml: 348 rows ask a third question (which laundering pattern, AMLworld data) with state keys `case`
               # and `transfers`; it has no jeffhub question and no interface yet.
               "aml": ["case", "transfers"]}
LATER = {"ground": "Passage re-ranking is a later function of this adapter (a separate manifest shape).",
         "aml": "The laundering-pattern question (AMLworld) is a later function of this adapter."}
# The test sets of these adapters are not on their Hugging Face model repos; they are in the private dataset
# teamsuzie/jeff-data (owner ruling 2026-10-22): tests.published is null and tests.internal names the private file.
INTERNAL = {"aml", "sanctions", "soc", "trading-desk"}
DATA_REPO, DATA_REVISION = "teamsuzie/jeff-data", "main"
# example.json shows only one of the questions of these adapters; the others take their instructions word for word
# from their test rows, where each function uses one fixed text (owner ruling 2026-10-22).
INSTRUCTIONS_FROM_ROWS = {"aml", "trading-desk"}
# Caller-label `max`: the training limit jeffhub documents (the quoted text must appear in its manifest), else the
# largest count in the test rows (owner ruling 2026-10-22).
TRAINED_MAX = {("triage", "route"): (40, "up to 40 teams in training"),
               ("tools", "tool"): (150, "2 to 150 tools in training"),
               ("support-intents", "intent"): (64, "64 for the home assistant (HWU64)"),
               }
# nav: max is the base's options less the fixed first ones (controller ruling 2026-10-22); jeffhub says to list every
# item on the screen, up to 254 options in all, and training screens had up to 200 items.
BASE_LIMITED_MAX = {("nav", "target")}

# nav: about 22% of the test rows are written in varied formats to test robustness (renamed state keys, reworded
# instructions and fixed options, other option keys such as i1 or item_1), so no prefix is common to all rows. The
# labels follow the documented format instead (request.rules and example.json: `ask_question` and `none_of_these`
# first, then the screen's items as o1, o2, ...), checked to be the format of most test rows.
DOCUMENTED_FORMAT = {"nav"}


def documented_labels(name: str, key: str, rows: list[dict], example: dict, notes: list[str], hub_options: str) -> dict:
    pairs = list(example.items())
    numbered = [k for k, _ in pairs if NUMBERED.fullmatch(k)]
    first = pairs[:len(pairs) - len(numbered)]
    letters = {NUMBERED.fullmatch(k).group(1) for k in numbered}
    if not first or len(letters) != 1 or [k for k, _ in pairs[len(first):]] != numbered:
        sys.exit(f"{name}.{key}: example.json's options are not fixed options followed by numbered ones")
    letter = letters.pop()
    rest = [list(row["question"]["criteria"])[len(first):] for row in rows
            if list(row["question"]["criteria"].items())[:len(first)] == first]
    rest = [keys for keys in rest if keys == [f"{letter}{n}" for n in range(1, len(keys) + 1)]]
    if 2 * len(rest) <= len(rows):
        sys.exit(f"{name}.{key}: only {len(rest)} of {len(rows)} test rows follow the documented option format")
    notes.append(f"{key}: decided by hand: labels follow the documented format ({', '.join(k for k, _ in first)} "
                 f"first, then {letter}1, {letter}2, ...), which {len(rest)} of the {len(rows)} test rows use; the "
                 f"others vary the format to test robustness.")
    return {"source": "caller", "fixed_first": dict(first), "caller_keys": f"{letter}{{n}}", "max": max(map(len, rest))}


def internal_uri(name: str, file: str) -> str:
    return f"hf://datasets/{DATA_REPO}@{DATA_REVISION}/v1.3/adapters/{name}/{file}"


def internal_rows(name: str, files: list[str]) -> list[dict]:
    path = f"v1.3/adapters/{name}/test.jsonl"
    if path not in files:
        sys.exit(f"{name}: {path} is not in {DATA_REPO}")
    return read_rows(Path(hf_hub_download(DATA_REPO, path, repo_type="dataset", revision=DATA_REVISION)))


def build(name: str, data_files: list[str]) -> dict:
    hub = yaml.safe_load((JEFFHUB / name / "manifest.yaml").read_text())
    hub_text = (JEFFHUB / name / "manifest.yaml").read_text()
    repo, revision = hub["huggingface"]["repo"], hub["huggingface"]["revision"]
    folder = Path(snapshot_download(repo, revision=revision))
    example = json.loads((folder / "example.json").read_text())
    config = json.loads((folder / "decision_config.json").read_text())
    notes: list[str] = []
    if name in INTERNAL:
        rows = internal_rows(name, data_files)
        has_calibration = f"v1.3/adapters/{name}/calibration.jsonl" in data_files
        tests = {"published": None, "calibration": None, "internal": internal_uri(name, "test.jsonl"),
                 "internal_calibration": internal_uri(name, "calibration.jsonl") if has_calibration else None}
        notes.append(f"tests: the test set is not on {repo}; it is the private {tests['internal']} (owner ruling "
                     f"2026-10-22), so tests.published is null.")
    else:
        rows = read_rows(folder / "test.jsonl")
        tests = {"published": f"hf://{repo}@{revision}/test.jsonl",
                 "calibration": f"hf://{repo}@{revision}/calibration.jsonl" if (folder / "calibration.jsonl").exists()
                 else None}
    state = hub["request"]["state"]
    changing = [field for field in state if field["changing"]]
    if len(changing) != 1 or state[-1] is not changing[0]:
        sys.exit(f"{name}: exactly one changing state field, last, is required; got {[f['key'] for f in state]}")
    if name in OTHER_SHAPE:
        kept = [row for row in rows if list(row["state"]) != OTHER_SHAPE[name]]
        notes.append(f"tests: {len(rows) - len(kept)} of the {len(rows)} test rows have state keys "
                     f"{OTHER_SHAPE[name]}, another request shape this manifest does not describe; they are not this "
                     f"manifest's rows. {LATER[name]}")
        rows = kept
    hub_questions = {q["key"]: q for q in hub["request"]["questions"]}
    if not set(example["questions"]) <= set(hub_questions):
        sys.exit(f"{name}: example.json's questions {list(example['questions'])} are not all in jeffhub's")
    if name in INSTRUCTIONS_FROM_ROWS:
        keys = list(hub_questions)
        mode = "id_suffix"
        found = [(function_of(name, row, hub_questions, mode, None, None, None), row) for row in rows]
        rows_of = {key: [row for function, row in found if function == key] for key in keys}
        questions = {}
        for key in keys:
            if key in example["questions"]:
                questions[key] = example["questions"][key]
            else:
                questions[key] = question_from_rows(name, key, hub_questions[key]["type"], rows_of[key],
                                                    hub_questions[key].get("options", ""))
                notes.append(f"{key}: example.json does not show this question; its instructions are the one text "
                             f"its {len(rows_of[key])} test rows use, word for word, and its options are in the order "
                             f"of jeffhub's request.questions (owner ruling 2026-10-22).")
    else:
        questions = example["questions"]
        mode = "only" if len(questions) == 1 else FUNCTION_BY.get(name, "id_suffix")
    if name in FUNCTION_BY:
        notes.append(f"tests.function_by {mode}: decided by hand (the test row ids do not end with a function key).")
    tests["function_by"] = mode
    keyed = [(function_of(name, row, questions, mode, None, None, None), row) for row in rows]
    functions = {}
    for key, question in questions.items():
        interface = interface_of(name, key)
        mine = [row for found, row in keyed if found == key]
        if not mine:
            sys.exit(f"{name}.{key}: no test rows")
        if question["type"] == "choice":
            labels = (documented_labels if name in DOCUMENTED_FORMAT else choice_labels)(
                name, key, mine, question["criteria"], notes, hub_questions[key].get("options", ""))
            wanted = FIXED_VALUES.get(interface)
            if wanted is not None and (labels["source"] != "fixed" or list(labels["fixed"]) != wanted):
                sys.exit(f"{name}.{key}: labels {labels} differ from interface {interface}'s {wanted}; owner decides")
            if labels["source"] == "caller":
                seen = labels["max"]
                if (name, key) in BASE_LIMITED_MAX:
                    labels["max"] = config["max_options"] - len(labels["fixed_first"])
                    notes.append(f"{key}: max {labels['max']} is the base's {config['max_options']} options less the "
                                 f"{len(labels['fixed_first'])} fixed first ones (controller ruling 2026-10-22); the "
                                 f"test rows have up to {seen}.")
                elif (name, key) in TRAINED_MAX:
                    limit, quote = TRAINED_MAX[(name, key)]
                    if quote not in hub_text:
                        sys.exit(f"{name}.{key}: the documented limit {quote!r} is not in jeffhub's manifest")
                    if seen > limit:
                        sys.exit(f"{name}.{key}: test rows have {seen} caller labels, more than the documented {limit}")
                    labels["max"] = limit
                    notes.append(f"{key}: max {limit} is the training limit jeffhub documents ({quote!r}); the test "
                                 f"rows have up to {seen}.")
                else:
                    notes.append(f"{key}: max {seen} is the largest number of caller labels in the test rows "
                                 f"(jeffhub documents no training limit).")
        elif question["type"] == "score":
            labels = {"source": "fixed", "levels": question["criteria"]}
        else:
            labels = {"source": "fixed", **({"fixed": question["criteria"]} if question.get("criteria") else {})}
        functions[key] = {"interface": interface, "type": question["type"], "instructions": question["instructions"],
                          "labels": labels, "abstain_labels": [], "calibration": None}
    domain = hub["task"]
    return {
        "id": f"jeff.{domain}.{name}", "version": hub["version"], "aliases": [name],
        "licence": {"kind": "open", "adapter": hub["licence"]["adapter"]},
        "weights": {"open": {"repo": repo, "revision": revision}},
        "base": {"id": "jeff-base", "version": hub["base"]["version"], "max_options": config["max_options"]},
        "context": [{"key": f["key"], "description": f["description"], "required": True} for f in state[:-1]],
        "input": {"key": state[-1]["key"], "description": state[-1]["description"], "form": "fields"},
        "functions": functions, "routes_to": [], "tests": tests,
        "hub": {**hub, "notes": notes} if notes else hub,
    }


# code and code-router stay out of step 1 (owner ruling 2026-10-22): no test set, and their development rows do not
# use the documented request format.
PENDING = {"code", "code-router"}


def main() -> None:
    names = sys.argv[1:] or sorted(path.name for path in JEFFHUB.iterdir()
                                   if (path / "manifest.yaml").exists() and path.name not in PENDING)
    data_files = list_repo_files(DATA_REPO, repo_type="dataset", revision=DATA_REVISION)
    for name in names:
        if name in PENDING:
            sys.exit(f"{name} is pending the owner's decision (no test set); it is not imported")
        manifest = build(name, data_files)
        target = ROOT / "catalogue" / manifest["id"] / "manifest.yaml"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(yaml.safe_dump(manifest, sort_keys=False, allow_unicode=True, width=120))
        print(f"{name} -> {target.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
