"""Arrange training rows in the layouts the evaluation panel uses, deterministically and without changing any facts.

The decision API accepts many layouts for the same decision: a state can be plain text or named fields, and an option can be a bare
key or carry the candidate text as its description. The panel is consistent about these choices; our data was not.
This module rewrites a fixed share of rows (PANEL_SHARE, picked by a hash of the row id) into the panel's conventions
and leaves the rest in their original layout, so the model learns the panel's conventions without depending on them:

- inputs with parts become named fields (RAGTruth: task, instruction, source, response; JudgeBench: question,
  response_A, response_B);
- candidate answers move out of the state into the option descriptions (WinoGrande, BBH multiple choice);
- BBH-style puzzles get BBH's one generic instruction and its bare options (Yes/No, True/False, valid/invalid).

It also rebalances every family whose labels are only positions (A/B, 1/2, A-D), in both layouts, so the correct
answer sits at each position equally often. Rows whose text does not have the expected parts keep their original
layout and are counted in the report."""

import hashlib
import math
import re
from collections import Counter
from copy import deepcopy

from jeff.types import Example, Question

PANEL_SHARE = 0.75
GENERIC = "Solve the problem described in the state and choose the correct answer."
FAITHFULNESS: Question = {
    "type": "noul", "instructions": "Does the response contain any information that conflicts with the source or is not supported by it?",
    "criteria": {"true": "The response contains conflicting or unsupported information.",
                 "false": "Every claim in the response is supported by the source."}}
LETTERS = "ABCDEFGHIJKLMNOPQ"


def fraction(key: str) -> float:
    """A stable number in [0, 1) for a key."""
    return int(hashlib.sha256(key.encode()).hexdigest()[:12], 16) / 16 ** 12


def labelled(text: str, labels: tuple[str, ...]) -> tuple[str, list[str]] | None:
    """Split text at lines that start with each "<label>:", in this order. Returns the text before the first label and
    each part's content, or None when a label is missing, repeated or out of order."""
    found = [list(re.finditer(rf"(?m)^{re.escape(label)}:[ \t]*\n?", text)) for label in labels]
    if any(len(matches) != 1 for matches in found):
        return None
    marks = [matches[0] for matches in found]
    if any(a.start() >= b.start() for a, b in zip(marks, marks[1:])):
        return None
    ends = [mark.start() for mark in marks[1:]] + [len(text)]
    parts = [text[mark.end():end].strip() for mark, end in zip(marks, ends)]
    return (None if any(not part for part in parts) else (text[:marks[0].start()].strip(), parts))


def trailing_options(text: str) -> tuple[str, list[str]] | None:
    """Split off the lettered option lines ("A: ...", "B: ...", in order from A) that end the text."""
    lines = text.rstrip().split("\n")
    options: list[str] = []
    labels: list[str] = []
    while lines and (match := re.fullmatch(r"([A-Q]): (.+)", lines[-1].strip())):
        labels.insert(0, match.group(1))
        options.insert(0, match.group(2).strip())
        lines.pop()
        if match.group(1) == "A":
            break
    expected = [chr(ord("A") + offset) for offset in range(len(labels))]
    if len(options) < 2 or not lines or labels != expected:
        return None
    return "\n".join(lines).strip(), options


def place(options: list[str], answer: int, target: int) -> list[str]:
    """Move the correct option to position `target`, keeping the others in their order."""
    others = options[:answer] + options[answer + 1:]
    return others[:target] + [options[answer]] + others[target:]


def with_layout(row: Example, state: object, question: Question, label: object, layout: str) -> Example:
    result = deepcopy(row)
    result["state"] = state  # type: ignore[typeddict-item]
    result["question"] = question
    result["label"] = result["target"] = label  # type: ignore[typeddict-item]
    result["source"] = {**result["source"], "layout": layout}
    return result


def family_of(row: Example) -> str:
    return str(row["source"]["family"]) if row["suite"] == "synthetic" else row["suite"]


# ---- One function per family: (row, panel layout?, target position) -> rearranged row, or None if unparsable. ----

def faithfulness(row: Example, panel: bool, target: int) -> Example | None:
    split = labelled(row["state"], ("Source", "Instruction", "Response"))  # type: ignore[arg-type]
    if split is None or split[0]:
        return None
    if not panel:
        return row
    source, instruction, response = split[1]
    task = "Summary" if re.match(r"(?i)\s*(summari[sz]e|write a summary)", instruction) else "QA"
    return with_layout(row, {"task": task, "instruction": instruction, "source": source, "response": response},
                       deepcopy(FAITHFULNESS), row["label"], "panel")


def word_limit(summary: str) -> int:
    return 10 * math.ceil(len(summary.split()) * 1.2 / 10)


def summary_consistency(row: Example, panel: bool, target: int) -> Example | None:
    """Panel layout asks the RAGTruth question, whose "true" means the opposite ("has a problem"), so the label flips."""
    split = labelled(row["state"], ("Document", "Summary"))  # type: ignore[arg-type]
    if split is None or split[0]:
        return None
    if not panel:
        return row
    document, summary = split[1]
    return with_layout(row, {"task": "Summary", "instruction": f"Summarize the following text within {word_limit(summary)} words:",
                             "source": document, "response": summary}, deepcopy(FAITHFULNESS), not row["label"], "panel")


def halueval_qa(row: Example, panel: bool, target: int) -> Example | None:
    split = labelled(row["state"], ("Knowledge", "Question", "Response"))  # type: ignore[arg-type]
    if split is None or split[0]:
        return None
    if not panel:
        return row
    knowledge, question, response = split[1]
    return with_layout(row, {"task": "QA", "instruction": question, "source": knowledge, "response": response},
                       deepcopy(FAITHFULNESS), row["label"], "panel")


def halueval_summarization(row: Example, panel: bool, target: int) -> Example | None:
    split = labelled(row["state"], ("Document", "Summary"))  # type: ignore[arg-type]
    if split is None or split[0]:
        return None
    if not panel:
        return row
    document, summary = split[1]
    return with_layout(row, {"task": "Summary", "instruction": f"Summarize the following news within {word_limit(summary)} words:",
                             "source": document, "response": summary}, deepcopy(FAITHFULNESS), row["label"], "panel")


def pairwise(row: Example, panel: bool, target: int) -> Example | None:
    split = labelled(row["state"], ("Question", "Response A", "Response B"))  # type: ignore[arg-type]
    if split is None or split[0]:
        return None
    question, *responses = split[1]
    responses = place(responses, "AB".index(str(row["label"])), target)
    label = "AB"[target]
    if panel:
        state: object = {"question": question, "response_A": responses[0], "response_B": responses[1]}
    else:
        state = f"Question: {question}\nResponse A: {responses[0]}\nResponse B: {responses[1]}"
    return with_layout(row, state, deepcopy(row["question"]), label, "panel" if panel else "text")


def pronoun_resolution(row: Example, panel: bool, target: int) -> Example | None:
    split = labelled(row["state"], ("Option 1", "Option 2"))  # type: ignore[arg-type]
    if split is None or not split[0]:
        return None
    sentence, candidates = split
    candidates = place(candidates, "12".index(str(row["label"])), target)
    label = "12"[target]
    if panel:
        question: Question = {"type": "choice", "instructions": row["question"]["instructions"],
                              "criteria": {"1": candidates[0], "2": candidates[1]}}
        return with_layout(row, sentence, question, label, "panel")
    return with_layout(row, f"{sentence}\nOption 1: {candidates[0]}\nOption 2: {candidates[1]}", deepcopy(row["question"]), label, "text")


def lettered(row: Example, panel: bool, target: int) -> Example | None:
    """Families whose text ends with lettered option lines; panel layout moves the options into the descriptions."""
    split = trailing_options(row["state"])  # type: ignore[arg-type]
    if split is None or len(split[1]) != len(row["question"]["criteria"] or {}):
        return None
    body, options = split
    options = place(options, LETTERS.index(str(row["label"])), target)
    label = LETTERS[target]
    if panel:
        question: Question = {"type": "choice", "instructions": GENERIC,
                              "criteria": {letter: option for letter, option in zip(LETTERS, options)}}
        return with_layout(row, re.sub(r"(?m)^Question: ", "", body), question, label, "panel")
    text = body + "\n" + "\n".join(f"{letter}: {option}" for letter, option in zip(LETTERS, options))
    return with_layout(row, text, deepcopy(row["question"]), label, "text")


def in_order(row: Example, panel: bool, target: int) -> Example | None:
    """Like lettered, but the options keep their order (counts zero to sixteen), so the answer is not moved."""
    return lettered(row, panel, LETTERS.index(str(row["label"])))


def bare(keys: tuple[str, str], prefix: str = "", suffix: str = ""):
    """Yes/no families in BBH form: the generic instruction and two bare options; true maps to the first key."""
    def convert(row: Example, panel: bool, target: int) -> Example | None:
        if not panel:
            return row
        state = f"{prefix}{row['state']}{suffix}"
        label = keys[0] if row["label"] in (True, keys[0]) else keys[1]
        question: Question = {"type": "choice", "instructions": GENERIC, "criteria": {keys[0]: None, keys[1]: None}}
        return with_layout(row, state, question, label, "panel")
    return convert


TRANSLATION_PREAMBLE = (
    "The following translations from German to English contain a particular error. That error will be one of the following "
    "types: Named Entities: An entity (names, places, locations, etc.) is changed to a different entity. Numerical Values: "
    "Numerical values (ordinals or cardinals), dates, and/or units are changed. Modifiers or Adjectives: The modifiers and "
    "adjectives pertaining to a noun are changed. Negation or Antonyms: Introduce or remove a negation or change comparatives "
    "to their antonyms. Facts: Trivial factual errors not pertaining to the above classes are introduced in the translations. "
    "Dropped Content: A significant clause in the translation is removed. Please identify that error.  ")
# BBH's fixed option order for the translation task, and our label for each.
TRANSLATION_OPTIONS = (("modifiers_or_adjectives", "Modifiers or Adjectives"), ("numerical_values", "Numerical Values"),
                       ("negation_or_antonyms", "Negation or Antonyms"), ("named_entities", "Named Entities"),
                       ("dropped_content", "Dropped Content"), ("facts", "Facts"))


def translation_error(row: Example, panel: bool, target: int) -> Example | None:
    split = labelled(row["state"], ("Source", "Translation"))  # type: ignore[arg-type]
    if split is None or split[0]:
        return None
    if not panel:
        return row
    source, translation = split[1]
    state = f"{TRANSLATION_PREAMBLE}Source: {source}\nTranslation: {translation}\nThe translation contains an error pertaining to"
    keys = [key for key, _ in TRANSLATION_OPTIONS]
    question: Question = {"type": "choice", "instructions": GENERIC,
                          "criteria": {letter: name for letter, (_, name) in zip(LETTERS, TRANSLATION_OPTIONS)}}
    return with_layout(row, state, question, LETTERS[keys.index(str(row["label"]))], "panel")


DISAMBIGUATION_PROMPT = ("In the following sentences, explain the antecedent of the pronoun (which thing the pronoun refers to), "
                         "or state that it is ambiguous.\n")


def disambiguation(row: Example, panel: bool, target: int) -> Example | None:
    """Options A and B are the two readings and swap places to balance them; C ("Ambiguous") stays last."""
    split = trailing_options(row["state"])  # type: ignore[arg-type]
    if split is None or len(split[1]) != 3 or split[1][2] != "Ambiguous" or not split[0].startswith("Sentence: "):
        return None
    body, options = split
    label = str(row["label"])
    if label != "C":
        readings = place(options[:2], "AB".index(label), target % 2)
        options, label = readings + options[2:], "AB"[target % 2]
    if panel:
        question: Question = {"type": "choice", "instructions": GENERIC, "criteria": dict(zip("ABC", options))}
        return with_layout(row, DISAMBIGUATION_PROMPT + body, question, label, "panel")
    text = body + "\n" + "\n".join(f"{letter}: {option}" for letter, option in zip("ABC", options))
    return with_layout(row, text, deepcopy(row["question"]), label, "text")


CONVERTERS = {
    "response_faithfulness": faithfulness, "summary_consistency": summary_consistency,
    "halueval_qa": halueval_qa, "halueval_summarization": halueval_summarization,
    "pairwise_answer_quality": pairwise, "grounded_pairwise": pairwise, "pronoun_resolution": pronoun_resolution,
    "object_tracking": lettered, "date_arithmetic": lettered, "coloured_objects": lettered, "logical_deduction": lettered,
    "adjective_order": lettered,
    "navigate": bare(("Yes", "No"), prefix="If you follow these instructions, do you return to the starting point? "),
    "boolean_expressions": bare(("True", "False")),
    "argument_validity": bare(("valid", "invalid"),
                              suffix="\nIs the argument, given the explicitly stated premises, deductively valid or invalid?"),
    "long_pairwise": pairwise, "sampled_pairwise": pairwise, "prm800k": pairwise, "codecontests": pairwise, "penguins_table": lettered, "temporal_sequences": lettered,
    "causal_judgement": bare(("Yes", "No"), prefix="How would a typical person answer each of the following questions about causation?\n"),
    "translation_error": translation_error, "disambiguation": disambiguation,
    "tracking_five": lettered, "date_understanding": lettered, "ordering_five": lettered, "ordering_seven": lettered,
    "colour_counting": in_order,
    "formal_fallacies": bare(("valid", "invalid"),
                             suffix="\nIs the argument, given the explicitly stated premises, deductively valid or invalid?"),
    "navigate_turns": bare(("Yes", "No"), prefix="If you follow these instructions, do you return to the starting point? "),
}
# Families whose label is a position: the correct answer is spread evenly over the positions.
POSITIONS = {"pairwise_answer_quality": 2, "grounded_pairwise": 2, "pronoun_resolution": 2, "adjective_order": 2,
             "long_pairwise": 2, "disambiguation": 2, "sampled_pairwise": 2, "prm800k": 2, "codecontests": 2}


def snarks(sarcastic: Example, sincere: Example, target: int) -> Example:
    """BBH's sarcasm task compares two statements; pair one sarcastic and one sincere row into that form."""
    statements = place([str(sarcastic["state"]), str(sincere["state"])], 0, target)
    question: Question = {"type": "choice", "instructions": GENERIC, "criteria": {"A": statements[0], "B": statements[1]}}
    row = with_layout(sarcastic, "Which statement is sarcastic?", question, "AB"[target], "panel")
    row["id"] = f"{sarcastic['id']}+{sincere['id']}"
    row["source"] = {**row["source"], "paired_with": sincere["id"]}
    return row


def rearrange(rows: list[Example], seed: int) -> tuple[list[Example], dict[str, object]]:
    """The panel layout for PANEL_SHARE of each converted family, the original layout for the rest, positions balanced."""
    result: list[Example] = []
    counts: Counter[str] = Counter()
    sarcasm: dict[bool, list[Example]] = {True: [], False: []}
    for row in rows:
        family = family_of(row)
        panel = fraction(f"{seed}-layout-{row['id']}") < PANEL_SHARE
        if family == "sarcasm" and panel:
            sarcasm[bool(row["label"])].append(row)
            continue
        convert = CONVERTERS.get(family)
        if convert is None:
            result.append(row)
            continue
        positions = POSITIONS.get(family) or len(row["question"].get("criteria") or {})
        target = int(fraction(f"{seed}-position-{row['id']}") * positions)
        converted = convert(row, panel, target)
        if converted is None:
            counts[f"{family}: unparsed, kept as is"] += 1
            result.append(row)
        else:
            counts[f"{family}: {converted['source'].get('layout', 'text')}"] += 1
            result.append(converted)
    ordered = {label: sorted(group, key=lambda row: fraction(f"{seed}-pair-{row['id']}")) for label, group in sarcasm.items()}
    pairs = min(len(ordered[True]), len(ordered[False]))
    for sarcastic, sincere in zip(ordered[True][:pairs], ordered[False][:pairs]):
        result.append(snarks(sarcastic, sincere, int(fraction(f"{seed}-position-{sarcastic['id']}") * 2)))
    result.extend(ordered[True][pairs:] + ordered[False][pairs:])  # unpaired rows keep their original layout
    counts["sarcasm: panel pairs"] = pairs
    counts["sarcasm: unpaired, kept as is"] = abs(len(ordered[True]) - len(ordered[False]))
    return result, dict(sorted(counts.items()))
