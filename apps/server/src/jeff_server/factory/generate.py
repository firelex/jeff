"""Training data from an approved spec, written by the teacher. COMMON.md's rules: one question per row, the changing
field last, options shuffled (score scales keep their order), splits by family with about 10% of families held out
for the test, about 70% canonical instructions / 27% variants from a checked pool / 3% none, provenance in every row.

data_prep: families (invented deployments with their fixed fields), the instruction pool, the split plan.
generate:  requests with their answers, many per teacher call, the first question's answers balanced; progress is
           reported through a callback so the console can draw it."""

import json
import math
import random
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any

from jeff_server.factory.spec import answer_keys, applies, label_of, question_payload
from jeff_server.factory.teacher import InvalidReply, Teacher

GENERATOR = "jeff-factory"
VERSION = "1"
IN_FLIGHT = 8  # teacher requests at once
FAMILIES_PER_CALL = 10
REQUESTS_PER_CALL = 10
POOL_TARGET = 70  # rewordings written per question; at least POOL_MINIMUM must pass the check
POOL_MINIMUM = 40
POOL_ROUNDS = 5
SHARES = {"canonical": 0.70, "variant": 0.27, "none": 0.03}
HARD_SHARE = 0.30  # requests written as one of the spec's hard cases
SURPLUS = 1.12  # write about 12% more than needed: checks drop duplicates and near-copies, then trim to size
MAX_WAVES = 6
MAX_FAILED_SHARE = 0.2  # a wave in which more teacher calls than this stay invalid stops the step

Progress = Callable[[dict[str, Any]], None]


def describe_spec(spec: dict[str, Any], questions: list[dict[str, Any]] | None = None) -> str:
    """The spec in plain English, for teacher prompts."""
    if spec["family"]["fields"]:
        lines = [f"Task: {spec['summary']}", "",
                 f"One deployment is a {spec['family']['noun']}: {spec['family']['description']}", "Its fixed fields:"]
        lines += [f"- {field['name']}: {field['description']}" for field in spec["family"]["fields"]]
        lines += ["", f"The field that changes on every request is \"{spec['live_field']['name']}\": "
                  f"{spec['live_field']['description']}"]
    else:
        lines = [f"Task: {spec['summary']}", "", f"The classifier reads only the request text: "
                 f"{spec['live_field']['description']}", f"Requests come from different kinds of {spec['family']['noun']}: "
                 f"{spec['family']['description']}"]
    lines += ["", "The questions asked about each request:"]
    for question in questions or spec["questions"]:
        when = "asked of every request" if question["applies_when"] is None else question["applies_when"]
        lines.append(f"- \"{question['id']}\" ({when}): {question['instruction']}")
        if question["type"] == "noul":
            meanings = {option["key"]: option["description"] for option in question["options"]}
            lines.append("    answer true or false" + "".join(f"; {key} means: {text}" for key, text in meanings.items()))
        else:
            lines += [f"    - {option['key']}: {option['description']}" for option in question["options"]]
    return "\n".join(lines)


# --- families -------------------------------------------------------------------------------------------------------

def family_schema(spec: dict[str, Any]) -> dict[str, Any]:
    fields = {field["name"]: {"type": "string"} for field in spec["family"]["fields"]}
    item = {"type": "object", "additionalProperties": False, "required": ["label", "profile", *fields],
            "properties": {"label": {"type": "string"}, "profile": {"type": "string"}, **fields}}
    return {"type": "object", "additionalProperties": False, "required": ["families"],
            "properties": {"families": {"type": "array", "items": item}}}


FAMILY_PROMPT = """We are writing realistic but invented test data for a classifier. Here is what the classifier does:

{spec}

Invent {count} different {noun}s. Each one is a separate, plausible, fictional example, different from the others \
in its details (who they are, what they care about, how they write). For each, write a short unique "label" (two to \
five words, for example a fictional name), a "profile" (one or two sentences: who this is, what they usually ask for \
and how they phrase things), and a value for every fixed field listed above, if any. Write each field value as a \
short, natural piece of text, the way it would appear in a real system. Do not use real company names.
{avoid}
Reply as JSON: {{"families": [{{"label": "...", "profile": "...", {fields}}}, ...]}}"""


def make_families(teacher: Teacher, spec: dict[str, Any], count: int) -> list[dict[str, Any]]:
    fields = ", ".join(f'"{field["name"]}": "..."' for field in spec["family"]["fields"])
    families: list[dict[str, Any]] = []
    calls = math.ceil(count / FAMILIES_PER_CALL)

    def one(index: int) -> list[dict[str, Any]]:
        want = min(FAMILIES_PER_CALL, count - index * FAMILIES_PER_CALL)
        avoid = f"\n(This is batch {index + 1} of {calls}; make these different from typical first ideas.)\n"

        def check(value: Any) -> None:
            if len(value["families"]) != want:
                raise ValueError(f"asked for {want} families, got {len(value['families'])}")
        reply, model = teacher.ask(FAMILY_PROMPT.format(spec=describe_spec(spec), count=want,
                                                         noun=spec["family"]["noun"], avoid=avoid, fields=fields),
                                   family_schema(spec), "families", check=check)
        return [{**family, "teacher": model} for family in reply["families"]]

    with ThreadPoolExecutor(IN_FLIGHT) as pool:
        for batch in pool.map(one, range(calls)):
            families.extend(batch)
    seen: set[str] = set()
    unique = []
    for family in families:
        label = family["label"].strip().lower()
        if label not in seen:
            seen.add(label)
            unique.append(family)
    if len(unique) < count * 0.9:
        raise ValueError(f"Only {len(unique)} different families of {count} asked for; the teacher repeated itself")
    for index, family in enumerate(unique):
        family["id"] = f"f{index:03d}"
    return unique


# --- instruction pool -----------------------------------------------------------------------------------------------

POOL_PROMPT = """Here is an instruction given to a classifier:

"{instruction}"

The classifier answers it by choosing one of these options:
{options}

Write {count} rewordings of the instruction. Each must ask exactly the same thing and keep every rule, condition and \
exception the original states, with exactly the same meaning: never narrow, widen or reinterpret one (for example, \
"the last one" must not become "the second one", and "always" must not become "usually"), and never add one. Vary the wording, sentence structure and length (some short, some \
longer, some formal, some plain). Do not mention option letters, and do not change which option is right in any case.

Reply as JSON: {{"rewordings": ["...", ...]}}"""

POOL_CHECK_PROMPT = """Here is an instruction given to a classifier, and some proposed rewordings of it.

Original: "{instruction}"

For each rewording, decide whether it asks exactly the same question and keeps every rule, condition and exception of \
the original, without adding, dropping or changing any. Answer true only if a reader following the rewording would \
choose the same answer as a reader following the original in every case.

Rewordings:
{numbered}

Reply as JSON: {{"verdicts": [true or false for each rewording, in order]}}"""


def make_pool(teacher: Teacher, question: dict[str, Any]) -> dict[str, Any]:
    """Rewordings of the canonical instruction, each checked by the teacher to keep every rule. Writes up to
    POOL_ROUNDS batches until POOL_TARGET pass; fewer than POOL_MINIMUM after that is an error."""
    options = "\n".join(f"- {option['key']}: {option['description']}" for option in question["options"])
    schema = {"type": "object", "additionalProperties": False, "required": ["rewordings"],
              "properties": {"rewordings": {"type": "array", "items": {"type": "string"}}}}
    check_schema = {"type": "object", "additionalProperties": False, "required": ["verdicts"],
                    "properties": {"verdicts": {"type": "array", "items": {"type": "boolean"}}}}
    batch = POOL_TARGET // 2
    kept: list[str] = []
    seen = {question["instruction"]}
    written = rejected = 0
    writer = checker = ""
    for round_number in range(POOL_ROUNDS):
        if len(kept) >= POOL_TARGET:
            break
        def check_count(value: Any) -> None:
            if len(value["rewordings"]) < batch * 0.8:
                raise ValueError(f"asked for {batch} rewordings, got {len(value['rewordings'])}")
        avoid = "" if not kept else "\nThese rewordings exist already; write different ones:\n" + "\n".join(f"- {t}" for t in kept[-20:])
        reply, writer = teacher.ask(POOL_PROMPT.format(instruction=question["instruction"], options=options, count=batch)
                                    + avoid, schema, "rewordings", check=check_count)
        candidates = [text.strip() for text in reply["rewordings"] if text.strip() and text.strip() not in seen]
        seen.update(candidates)
        if not candidates:
            continue
        numbered = "\n".join(f"{index + 1}. {text}" for index, text in enumerate(candidates))

        def check_verdicts(value: Any, count: int = len(candidates)) -> None:
            if len(value["verdicts"]) != count:
                raise ValueError(f"{count} rewordings but {len(value['verdicts'])} verdicts")
        verdicts, checker = teacher.ask(POOL_CHECK_PROMPT.format(instruction=question["instruction"], numbered=numbered),
                                        check_schema, "rewording_check", check=check_verdicts)
        passed = [text for text, ok in zip(candidates, verdicts["verdicts"], strict=True) if ok]
        written += len(candidates)
        rejected += len(candidates) - len(passed)
        kept += passed
    kept = kept[:POOL_TARGET]
    if len(kept) < POOL_MINIMUM:
        raise ValueError(f"Question {question['id']}: only {len(kept)} of {written} rewordings kept the instruction's "
                         f"rules after {POOL_ROUNDS} rounds; at least {POOL_MINIMUM} are needed")
    return {"question": question["id"], "canonical": question["instruction"], "variants": kept,
            "written": written, "rejected": rejected, "writer": writer, "checker": checker}


# --- the split plan -------------------------------------------------------------------------------------------------

def plan_splits(spec: dict[str, Any], families: list[dict[str, Any]], seed: int) -> dict[str, list[str]]:
    """Family ids per split, every split with its own families: about 10% each for test, dev and calibration (at
    least two each), the rest for training. The pinned trainer refuses training, dev and calibration rows that share a
    family, and families held out of training measure new situations."""
    ids = [family["id"] for family in families]
    random.Random(seed).shuffle(ids)
    held = max(2, round(len(ids) * 0.10))
    if len(ids) < 3 * held + 4:
        raise ValueError(f"{len(ids)} families are too few to hold out {held} each for test, dev and calibration")
    return {"test": sorted(ids[:held]), "dev": sorted(ids[held:2 * held]), "calibration": sorted(ids[2 * held:3 * held]),
            "train": sorted(ids[3 * held:])}


# --- requests -------------------------------------------------------------------------------------------------------

def questions_for_target(spec: dict[str, Any], target: str) -> list[dict[str, Any]]:
    """The questions that can apply to a request whose first answer is `target`: those whose conditions on the first
    question all name `target` (every other question is unanswered for such a request)."""
    first = spec["questions"][0]["id"]
    return [question for question in spec["questions"]
            if all(c["equals"] == target for c in question.get("requires", []) if c["question"] == first)]


def request_schema(questions: list[dict[str, Any]]) -> dict[str, Any]:
    answers: dict[str, Any] = {}
    for index, question in enumerate(questions):
        keys = answer_keys(question)
        answers[question["id"]] = ({"type": "string", "enum": keys} if index == 0
                                   else {"type": ["string", "null"], "enum": [*keys, None]})
    item = {"type": "object", "additionalProperties": False, "required": ["text", "answers"],
            "properties": {"text": {"type": "string"},
                           "answers": {"type": "object", "additionalProperties": False, "required": list(answers),
                                       "properties": answers}}}
    return {"type": "object", "additionalProperties": False, "required": ["requests"],
            "properties": {"requests": {"type": "array", "items": item}}}


REQUEST_PROMPT = """We are writing realistic training examples for a small classifier. Here is what it does:

{spec}

The {noun} writing these requests:
{family}

Write {count} different values of the changing field "{live}", each one a realistic request this {noun} could \
receive. Requirements:
- For every request, the correct answer to the first question ("{first}") must be "{target}".
- {kind}
- Phrasing style: {style}.
- Make the requests differ from each other in content, wording, length and details. Do not repeat a template. Do not \
mention option keys or the word "option".
- For each request, give the correct answer to every question: the option key ("true" or "false" for a yes/no \
question), or null for a question whose condition does not hold for that request. Give an answer exactly when the \
question's condition holds. Answer as a careful expert would, following each question's wording exactly.

Reply as JSON: {{"requests": [{{"text": "...", "answers": {{{answers}}}}}, ...]}}"""


def kind_line(spec: dict[str, Any], kind: str) -> str:
    if kind == "ordinary":
        return "These are ordinary, clear requests."
    case = next(case for case in spec["hard_cases"] if case["kind"] == kind)
    return f"These are hard cases of this kind: {case['description']} The right answer must still be clear to an expert."


def build_jobs(spec: dict[str, Any], plan: dict[str, list[str]], needed: dict[str, int], seed: int,
               wave: int, written: dict[str, Counter[str]]) -> list[dict[str, Any]]:
    """Teacher calls for one wave: per split, enough calls of REQUESTS_PER_CALL requests to cover `needed` requests.
    Each call's answer to the first question is the one with the fewest requests in that split so far, across waves
    (balance); hard-case kinds, styles and the split's families rotate."""
    rng = random.Random(f"{seed}:{wave}")
    first = spec["questions"][0]
    targets = [option["key"] for option in first["options"]]
    kinds = [case["kind"] for case in spec["hard_cases"]]
    jobs = []
    for split, count in needed.items():
        calls = math.ceil(count / REQUESTS_PER_CALL)
        families = list(plan[split])
        rng.shuffle(families)
        planned = Counter({target: written[split][target] for target in targets})
        for index in range(calls):
            target = min(targets, key=lambda key: (planned[key], rng.random()))
            planned[target] += REQUESTS_PER_CALL
            hard = kinds and rng.random() < HARD_SHARE
            jobs.append({"split": split, "wave": wave, "index": index, "family": families[index % len(families)],
                         "target": target, "kind": rng.choice(kinds) if hard else "ordinary",
                         "style": rng.choice(spec["styles"])})
    rng.shuffle(jobs)
    return jobs


def run_job(teacher: Teacher, spec: dict[str, Any], families: dict[str, dict[str, Any]], job: dict[str, Any]) -> list[dict[str, Any]]:
    family = families[job["family"]]
    fixed = {field["name"]: family[field["name"]] for field in spec["family"]["fields"]}
    questions = questions_for_target(spec, job["target"])
    first = questions[0]
    answers = ", ".join(f'"{question["id"]}": "..."' for question in questions)
    shown = {"profile": family["profile"], **fixed}
    prompt = REQUEST_PROMPT.format(
        spec=describe_spec(spec, questions), noun=spec["family"]["noun"], family=json.dumps(shown, ensure_ascii=False, indent=1),
        count=REQUESTS_PER_CALL, live=spec["live_field"]["name"], first=first["id"], target=job["target"],
        kind=kind_line(spec, job["kind"]), style=job["style"], answers=answers)

    def check(value: Any) -> None:
        requests = value["requests"]
        if len(requests) < REQUESTS_PER_CALL * 0.8:
            raise ValueError(f"asked for {REQUESTS_PER_CALL} requests, got {len(requests)}")
        for request in requests:
            given = request["answers"]
            if given[first["id"]] != job["target"]:
                raise ValueError(f"a request answers {first['id']} with {given[first['id']]!r}, not the asked-for {job['target']!r}")
            for question in questions[1:]:
                expected = applies(question, given)
                if expected and given[question["id"]] is None:
                    raise ValueError(f"{question['id']} applies ({question['applies_when']}) but was answered null")
                if not expected and given[question["id"]] is not None:
                    raise ValueError(f"{question['id']} does not apply ({question['applies_when']}) but was answered")
            if not request["text"].strip():
                raise ValueError("an empty request")
    reply, model = teacher.ask(prompt, request_schema(questions), "requests", check=check)
    return [{"split": job["split"], "family": job["family"], "fixed": fixed, "text": request["text"].strip(),
             "answers": request["answers"], "kind": job["kind"], "style": job["style"], "target": job["target"],
             "teacher": model, "job": f"w{job['wave']}-{job['split']}-{job['index']}"}
            for request in reply["requests"]]


def scenario_rows(scenario: dict[str, Any]) -> int:
    return sum(1 for value in scenario["answers"].values() if value is not None)


def generate_scenarios(teacher: Teacher, spec: dict[str, Any], families: list[dict[str, Any]],
                       plan: dict[str, list[str]], seed: int, progress: Progress
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Requests with answers until every split has its rows plus the surplus (at most MAX_WAVES waves), and the
    teacher calls whose replies stayed invalid."""
    by_id = {family["id"]: family for family in families}
    goal = {split: math.ceil(spec["sizes"][split] * SURPLUS) for split in ("train", "dev", "calibration", "test")}
    scenarios: list[dict[str, Any]] = []
    rows = Counter[str]()
    failures: list[dict[str, Any]] = []
    for wave in range(MAX_WAVES):
        missing = {split: goal[split] - rows[split] for split in goal if rows[split] < goal[split]}
        if not missing:
            return scenarios, failures
        if scenarios:  # rows per request, measured so far
            needed = {split: math.ceil(count / (sum(scenario_rows(s) for s in scenarios) / len(scenarios)))
                      for split, count in missing.items()}
        else:  # rows per request are unknown before the first wave: one call per first answer and split measures them
            needed = {split: len(spec["questions"][0]["options"]) * REQUESTS_PER_CALL for split in missing}
        written: dict[str, Counter[str]] = {split: Counter() for split in goal}
        for scenario in scenarios:
            written[scenario["split"]][scenario["target"]] += 1
        jobs = build_jobs(spec, plan, needed, seed, wave, written)
        progress({"phase": "wave", "wave": wave + 1, "calls": len(jobs), "rows": dict(rows), "goal": goal})
        with ThreadPoolExecutor(IN_FLIGHT) as pool:
            futures = {pool.submit(run_job, teacher, spec, by_id, job): job for job in jobs}
            failed = 0
            for done, (job, future) in enumerate(((futures[f], f) for f in as_completed(futures)), 1):
                try:
                    batch = future.result()
                except InvalidReply as error:  # recorded and counted; the next wave writes the missing rows
                    failed += 1
                    failures.append({"job": job, "error": str(error)})
                    batch = []
                for scenario in batch:
                    scenarios.append(scenario)
                    rows[scenario["split"]] += scenario_rows(scenario)
                progress({"phase": "rows", "wave": wave + 1, "done": done, "calls": len(jobs), "rows": dict(rows),
                          "goal": goal, "failed_calls": len(failures), "teacher": teacher.usage.snapshot()})
            if failed > len(jobs) * MAX_FAILED_SHARE:
                raise ValueError(f"Wave {wave + 1}: {failed} of {len(jobs)} teacher calls stayed invalid; first error: "
                                 f"{failures[-failed]['error']}")
    missing = {split: goal[split] - rows[split] for split in goal if rows[split] < goal[split]}
    if missing:
        raise ValueError(f"After {MAX_WAVES} waves some splits are still short of rows: {missing}")
    return scenarios, failures


# --- rows -----------------------------------------------------------------------------------------------------------

def choose_instruction(rng: random.Random, pool: dict[str, Any]) -> tuple[str | None, str]:
    draw = rng.random()
    if draw < SHARES["canonical"]:
        return pool["canonical"], "canonical"
    if draw < SHARES["canonical"] + SHARES["variant"]:
        return rng.choice(pool["variants"]), "variant"
    return None, "none"


def make_rows(spec: dict[str, Any], scenarios: list[dict[str, Any]], pools: dict[str, dict[str, Any]],
              spec_sha256: str, run_id: str, seed: int) -> list[dict[str, Any]]:
    """One row per (request, applicable question). The state is the family's fixed fields, then the changing field."""
    rng = random.Random(seed)
    rows = []
    for number, scenario in enumerate(scenarios):
        state = {**scenario["fixed"], spec["live_field"]["name"]: scenario["text"]} if scenario["fixed"] else scenario["text"]
        for question in spec["questions"]:
            key = scenario["answers"].get(question["id"])
            if key is None:
                continue
            payload = question_payload(question)
            if question["type"] == "choice":
                items = list(payload["criteria"].items())
                rng.shuffle(items)
                payload["criteria"] = dict(items)
            instruction, used = choose_instruction(rng, pools[question["id"]])
            if instruction is None:
                del payload["instructions"]
            else:
                payload["instructions"] = instruction
            label = label_of(question, key)
            rows.append({
                "id": f"{spec['name']}:{scenario['split']}:{number:05d}:{question['id']}",
                "suite": spec["name"], "family": scenario["family"], "state": state, "question": payload,
                "label": label, "target": label, "split": scenario["split"],
                "source": {"generator": GENERATOR, "version": VERSION, "teacher": scenario["teacher"],
                           "spec_sha256": spec_sha256, "run": run_id, "seed": seed, "kind": scenario["kind"],
                           "style": scenario["style"], "instruction": used, "question_id": question["id"],
                           "scenario": number, "job": scenario["job"], "dataset": spec["name"]},
            })
    return rows
