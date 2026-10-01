"""The shortcut report: does a surface feature (length, punctuation, position, a style word) give the answer away?

Every check compares the training file with the test file and writes its numbers, the chance level and, where the
data beats chance clearly, a plain-English flag. A flag asks for a human look; it is not always a problem (a "question"
label should have more question marks than a "command" label). The thresholds are the constants below."""

import collections
import hashlib
import math
import random
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from jeff.kit.learn import Classifier, FloatArray, IntArray, Picker, balanced_accuracy
from jeff.kit.rows import RowError, main_text, normalize, strings
from jeff.kit.split import label_name
from jeff.types import Example

# A rate or model "beats chance clearly" when it is at least MARGIN above chance AND at least Z standard errors above
# it (the standard error of a guess at chance on the same rows; for balanced accuracy, with each class's size), so
# small files and rare labels do not flag on noise alone.
MARGIN = 0.10
Z = 3.0
# A label's share differs between train and test by more than this.
BALANCE_GAP = 0.05
# A word or punctuation feature differs between labels (or row kinds) when the highest and lowest shares differ by at
# least FORMAT_GAP, the highest is at least FORMAT_RATIO times the lowest, and the two shares are at least Z standard
# errors apart. Labels with fewer than MIN_CLASS_ROWS training rows are left out of this and the per-label models.
FORMAT_GAP = 0.15
FORMAT_RATIO = 1.8
MIN_CLASS_ROWS = 5
# A row kind's label mix differs when one label's share inside the kind is this far from its share overall.
KIND_LABEL_GAP = 0.10
# Near duplicates: word 3-gram (or, for texts under 5 words, character 5-gram) Jaccard similarity of the main texts.
NEAR_DUPLICATE = 0.8
NEAR_SHARE = 0.02
# An option key used by at least this share of the training choice rows, with the same description in at least half
# of them, is a fixed option ("other", "none of these"); other keys are per-row listed options, such as o1 ... o12
# naming different items in each row.
FIXED_SHARE = 0.20
# A phrase of 1 to 3 words mostly found in one label: in more than PHRASE_SHARE of that label's rows, with at least
# PHRASE_PRECISION of the rows containing it (and at least twice the label's base rate) in that label, and in at least
# PHRASE_MIN_ROWS rows overall.
PHRASE_SHARE = 0.02
PHRASE_PRECISION = 0.70
PHRASE_MIN_ROWS = 10
SAMPLE_CAP = 50000
# The option picker learns from at most this many training rows (each row contributes one feature row per option).
PICKER_CAP = 5000
LISTED = "<listed option>"

FILLER = re.compile(r"\b(?:um+|uh+|uhm+|erm+|er|hmm+|mm+|ah+)\b", re.I)
QUESTION_START = re.compile(r"^\W*(?:what|who|whom|whose|where|when|why|how|which|is|are|am|was|were|do|does|did|can|"
                            r"could|will|would|should|shall|may|might|has|have|had)\b", re.I)
ARTICLE = re.compile(r"\b(?:a|an|the)\b", re.I)
DIGIT = re.compile(r"\d")
TAG = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]*[^<>]{0,200}>")
URL = re.compile(r"https?://|www\.")
MARKDOWN = re.compile(r"(\*\*|__|^#+ |^[-*] |```)", re.M)
EMOJI = re.compile("[\U0001F300-\U0001FAFF\u2600-\u27BF]")
PUNCTUATION = "?!.,:;\"'()-"

WORD_FEATURES: list[tuple[str, Callable[[str], bool]]] = [
    ("filler word (um, uh, er, hmm)", lambda t: bool(FILLER.search(t))),
    ("starts with a question word", lambda t: bool(QUESTION_START.search(t))),
    ("has a question mark", lambda t: "?" in t),
    ("has an article (a, an, the)", lambda t: bool(ARTICLE.search(t))),
    ("ends with .", lambda t: t.rstrip().endswith(".")),
    ("ends with !", lambda t: t.rstrip().endswith("!")),
    ("no end punctuation", lambda t: bool(t.strip()) and t.rstrip()[-1] not in "?.!\"')]"),
    ("starts lowercase", lambda t: t[:1].islower()),
    ("all lowercase", lambda t: t == t.lower() and any(c.isalpha() for c in t)),
    ("has a digit", lambda t: bool(DIGIT.search(t))),
    ("has a line break", lambda t: "\n" in t),
    ("has quotes", lambda t: '"' in t or "\u201c" in t),
    ("has markup (HTML or Markdown)", lambda t: bool(TAG.search(t) or MARKDOWN.search(t))),
    ("non-ASCII character", lambda t: any(ord(c) > 127 for c in t)),
    ("emoji", lambda t: bool(EMOJI.search(t))),
    ("ALL-CAPS word (4+ letters)", lambda t: bool(re.search(r"\b[A-Z]{4,}\b", t))),
]

# Leftovers of text generation and personal data; (name, pattern, flagged). Unflagged patterns are only counted.
JUNK: list[tuple[str, re.Pattern[str], bool]] = [
    ("unfilled template slot ({{...}}, [NAME], <NAME>)",
     re.compile(r"\{\{[^{}]*\}\}|\[(?:NAME|YOUR NAME|NAME HERE|COMPANY|DATE|INSERT[^\]]*|PLACEHOLDER|X+)\]|<(?:NAME|PLACEHOLDER)>", re.I), True),
    ("lorem ipsum", re.compile(r"lorem ipsum", re.I), True),
    ("TODO / TBD / FIXME", re.compile(r"\b(?:TODO|FIXME|TBD)\b"), True),
    ("'As an AI' or a refusal", re.compile(r"\bas an ai\b|as a language model|i(?: a|')m sorry, but i can(?:no|')t|"
                                           r"i cannot (?:help|assist|fulfil|fulfill|provide)|i can't (?:help|assist) with", re.I), True),
    ("chat preamble ('Here is...', 'Sure!')", re.compile(r"^(?:here(?: is|'s| are)\b|sure[,!]|certainly[,!]|of course[,!]|okay, here)", re.I), True),
    ("label words ('Example 3:', 'Output:')", re.compile(r"^(?:example|variation|message|user message|output|text)\s*\d*\s*:", re.I), True),
    ("model control tokens (<think>, <|im_start|>)", re.compile(r"</?think>|<\|im_(?:start|end)\|>|<\|endoftext\|>", re.I), True),
    ("garbled encoding", re.compile("\ufffd|\u00c3[\u0080-\u00bf]|\u00e2\u20ac"), True),
    ("email address (personal data?)", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), True),
    ("phone number (personal data?)", re.compile(r"(?<!\w)\+?\d[\d ().-]{8,}\d(?!\w)"), True),
    ("URL", URL, False),
    ("HTML tag", TAG, False),
]


@dataclass
class Record:
    """One row as the checks see it."""
    split: str
    id: str
    family: str
    label: str
    lclass: str
    kind: str | None
    text: str
    state_text: str
    instructions: str | None
    options: list[tuple[str, str]] = field(default_factory=list)
    position: int | None = None  # the answer's index among the options


def dotted(row: Example, path: str) -> str:
    """The value at a dotted path such as source.kind; a missing value is an error."""
    value: Any = row
    for part in path.split("."):
        if not isinstance(value, dict) or part not in value:
            raise RowError(f"row {row['id']!r} has no {path!r} (for --class-field)")
        value = value[part]
    return str(value)


def records(rows: Sequence[Example], split: str, text_field: str | None, class_field: str | None) -> list[Record]:
    result = []
    for row in rows:
        question = row["question"]
        options: list[tuple[str, str]] = []
        position = None
        if question["type"] == "choice":
            options = [(key, key if value is None else " ".join(strings(value))) for key, value in question["criteria"].items()]
            position = list(question["criteria"]).index(str(row["label"]))
        instructions = question.get("instructions")
        result.append(Record(split=split, id=row["id"], family=row["family"], label=label_name(row), lclass=label_name(row),
                             kind=dotted(row, class_field) if class_field else None, text=main_text(row, text_field),
                             state_text="\n".join(strings(row["state"])),
                             instructions=None if instructions is None else " ".join(strings(instructions)),
                             options=options, position=position))
    return result


def fixed_keys(train: Sequence[Record]) -> set[str]:
    """Option keys that mean the same in most rows: used by at least FIXED_SHARE of the choice rows, with one
    description in at least half of them. Keys such as o1 ... o20 that name a different item in each row are listed."""
    choice = [r for r in train if r.options]
    texts: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for r in choice:
        for key, text in r.options:
            texts[key][text] += 1
    return {key for key, counts in texts.items() if counts.total() >= FIXED_SHARE * len(choice)
            and counts.most_common(1)[0][1] >= 0.5 * counts.total()}


def beats_chance(observed: float, chances: Sequence[float]) -> tuple[bool, float]:
    """Whether `observed` (a rate over these rows) beats per-row chance clearly (see MARGIN and Z), and its z-score."""
    if not chances:
        return False, 0.0
    chance = sum(chances) / len(chances)
    error = math.sqrt(sum(c * (1 - c) for c in chances)) / len(chances)
    z = (observed - chance) / error if error > 0 else (math.inf if observed > chance else 0.0)
    return observed - chance >= MARGIN and z >= Z, z


def balanced_beats_chance(balanced: float, sizes: Sequence[int]) -> tuple[bool, float]:
    """The same rule for balanced accuracy (the mean of each class's recall), whose chance is 1 / classes and whose
    standard error depends on each class's size: a small class makes it noisier."""
    chance = 1 / len(sizes)
    error = math.sqrt(sum(chance * (1 - chance) / size for size in sizes)) / len(sizes)
    z = (balanced - chance) / error
    return balanced - chance >= MARGIN and z >= Z, z


def pct(value: float) -> str:
    return f"{100 * value:.1f}%"


def table(headers: Sequence[str], rows: Sequence[Sequence[object]]) -> list[str]:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    out += ["| " + " | ".join(str(c).replace("|", "\\|").replace("\n", " ") for c in row) + " |" for row in rows]
    return out + [""]


def short(text: str, size: int = 100) -> str:
    text = text.replace("\n", " ")
    return text if len(text) <= size else text[:size - 1] + "\u2026"


def sample(rows: Sequence[Record], cap: int, seed: int) -> list[Record]:
    """At most `cap` rows, drawn in proportion from each label so the label mix stays the same."""
    if len(rows) <= cap:
        return list(rows)
    groups: dict[str, list[Record]] = collections.defaultdict(list)
    for r in rows:
        groups[r.lclass].append(r)
    rng = random.Random(seed)
    return [r for key in sorted(groups) for r in rng.sample(groups[key], max(1, round(cap * len(groups[key]) / len(rows))))]


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []
        self.flags: list[str] = []

    def heading(self, text: str) -> None:
        self.lines += [f"## {text}", ""]

    def add(self, *lines: str) -> None:
        self.lines += [*lines, ""]


# ------------------------------------------------------------------------------------------------ the checks

def check_balance(report: Report, train: list[Record], test: list[Record]) -> None:
    report.heading("1. Label balance")
    classes = sorted({r.lclass for r in train + test})
    rows = []
    for c in classes:
        shares = [sum(r.lclass == c for r in rs) / len(rs) for rs in (train, test)]
        rows.append([c, pct(shares[0]), pct(shares[1]), sum(r.lclass == c for r in train)])
        if abs(shares[0] - shares[1]) > BALANCE_GAP:
            report.flags.append(f"Label '{c}' is {pct(shares[0])} of train but {pct(shares[1])} of test (more than "
                                f"{BALANCE_GAP:.0%} apart): the test does not measure the same mix you train on.")
    majority = collections.Counter(r.lclass for r in train).most_common(1)[0]
    report.lines += table(["label", "train", "test", "train rows"], rows)
    report.add(f"Always answering the most common training label ('{majority[0]}') would score "
               f"{pct(sum(r.lclass == majority[0] for r in test) / len(test))} on test; uniform chance is {pct(1 / len(classes))}.",
               f"`{LISTED}` stands for any per-row listed option: a key such as o1 ... o12 that names a different item "
               f"from row to row (used by fewer than {FIXED_SHARE:.0%} of training choice rows, or with no one description "
               "in half of them).")


def compared_options(r: Record, fixed: set[str]) -> list[int] | None:
    """The option indexes the position and length checks compare: the listed options when the answer is one of them,
    every option when the row has no listed options, and None (row skipped) when the answer is a fixed option."""
    if r.position is None:
        return None
    listed = [i for i, (key, _) in enumerate(r.options) if key not in fixed]
    if not listed:
        return list(range(len(r.options)))
    return listed if r.position in listed and len(listed) >= 2 else None


def check_options(report: Report, train: list[Record], test: list[Record], fixed: set[str]) -> None:
    report.heading("2. The correct option: position and length")
    rows = []
    for split, records_ in (("train", train), ("test", test)):
        counts = {"longest": 0, "shortest": 0, "first": 0, "last": 0}
        chances: dict[str, list[float]] = {name: [] for name in counts}
        relative = []
        for r in records_:
            compared = compared_options(r, fixed)
            if compared is None or r.position is None:
                continue
            lengths = [len(r.options[i][1]) for i in compared]
            mine = len(r.options[r.position][1])
            where = compared.index(r.position)
            counts["longest"] += mine == max(lengths)
            counts["shortest"] += mine == min(lengths)
            counts["first"] += where == 0
            counts["last"] += where == len(compared) - 1
            chances["longest"].append(lengths.count(max(lengths)) / len(lengths))
            chances["shortest"].append(lengths.count(min(lengths)) / len(lengths))
            chances["first"].append(1 / len(compared))
            chances["last"].append(1 / len(compared))
            relative.append(where / (len(compared) - 1))
        n = len(relative)
        if not n:
            continue
        cells: list[object] = [split, n]
        for name in counts:
            rate = counts[name] / n
            chance = sum(chances[name]) / n
            flagged, z = beats_chance(rate, chances[name])
            cells.append(f"{pct(rate)} (chance {pct(chance)}){' **flag**' if flagged else ''}")
            if flagged:
                report.flags.append(f"{split}: the correct option is the {name} one in {pct(rate)} of rows against "
                                    f"{pct(chance)} by chance (z = {z:.1f}). A model can learn to pick the {name} option "
                                    "without reading the input.")
        cells.append(f"{np.mean(relative):.2f}")
        rows.append(cells)
    if not rows:
        report.add("No choice rows whose answer can be compared with other options.")
        return
    report.lines += table(["split", "rows compared", "correct is longest", "correct is shortest", "correct is first",
                           "correct is last", "mean relative position (0 first, 1 last; 0.50 expected)"], rows)
    report.add("Compared among the listed options when the answer is one of them, among all options when every option "
               "is fixed; rows whose answer is a fixed option are left out (their share is the label balance above). "
               "Chance counts ties: if two options share the greatest length, either counts as longest.")


SURFACE_NAMES = (["characters (log)", "words (log)", "capital letter share", "digit share", "non-ASCII share", "line breaks",
                  "starts with a capital", "all lowercase", "ends with ?", "ends with .", "ends with !", "ends otherwise"]
                 + [f"count of {c}" for c in PUNCTUATION]
                 + ["whole state characters (log)", "rest of state characters (log)", "options (log)",
                    "no instructions", "most common instructions"])
TEXT_ONLY = 12 + len(PUNCTUATION)


def surface(r: Record, common_instructions: str | None) -> list[float]:
    """Numbers that describe a row's form without its words."""
    t = r.text
    letters = sum(c.isalpha() for c in t) or 1
    last = t.rstrip()[-1:]
    values = [math.log1p(len(t)), math.log1p(len(t.split())), sum(c.isupper() for c in t) / letters,
              len(DIGIT.findall(t)) / (len(t) or 1), sum(ord(c) > 127 for c in t) / (len(t) or 1), math.log1p(t.count("\n")),
              float(t[:1].isupper()), float(t == t.lower()), float(last == "?"), float(last == "."), float(last == "!"),
              float(bool(last) and last not in "?.!")]
    values += [math.log1p(t.count(c)) for c in PUNCTUATION]
    values += [math.log1p(len(r.state_text)), math.log1p(max(len(r.state_text) - len(t), 0)), math.log1p(len(r.options)),
               float(not r.instructions), float(r.instructions == common_instructions)]
    return values


def surface_matrix(train: list[Record], other: list[Record]) -> tuple[FloatArray, FloatArray]:
    """Surface features of both files; "most common instructions" means the training file's."""
    common = collections.Counter(r.instructions for r in train if r.instructions).most_common(1)
    instructions = common[0][0] if common else None
    return (np.asarray([surface(r, instructions) for r in train], dtype=np.float64),
            np.asarray([surface(r, instructions) for r in other], dtype=np.float64))


def class_model(train: list[Record], test: list[Record], features: tuple[FloatArray, FloatArray],
                key: Callable[[Record], str], columns: list[int]) -> tuple[float, float, list[int]] | None:
    """Train on train, score on test: accuracy, balanced accuracy and the test rows of each class present; None when
    test has a class train never saw or train has fewer than two classes."""
    classes = sorted({key(r) for r in train})
    if len(classes) < 2 or any(key(r) not in classes for r in test):
        return None
    index = {c: i for i, c in enumerate(classes)}
    y_train = np.asarray([index[key(r)] for r in train], dtype=np.int64)
    y_test = np.asarray([index[key(r)] for r in test], dtype=np.int64)
    predicted = Classifier(features[0][:, columns], y_train, len(classes)).predict(features[1][:, columns])
    sizes = [int(n) for n in np.bincount(y_test) if n]
    return float((predicted == y_test).mean()), balanced_accuracy(predicted, y_test), sizes


def check_surface(report: Report, train: list[Record], test: list[Record], fixed: set[str]) -> None:
    report.heading("3. Models that see only surface features")
    report.add("Each model is trained on the training file and scored on the test file. None of them reads a word of "
               "meaning. If one beats chance clearly, the data contains a pattern Jeff could learn instead of the task.")
    lengths = []
    for c in sorted({r.lclass for r in train}):
        for split, rs in (("train", train), ("test", test)):
            values = [len(r.text) for r in rs if r.lclass == c]
            if values:
                lengths.append([c, split, len(values), int(np.percentile(values, 10)), int(np.median(values)),
                                int(np.percentile(values, 90))])
    report.add("**Main text length by label (characters)**")
    report.lines += table(["label", "split", "rows", "10th percentile", "median", "90th percentile"], lengths)
    everything = list(range(len(SURFACE_NAMES)))
    features = surface_matrix(train, test)
    models = [("the length of the main text (characters and words)", [0, 1]),
              ("the form of the main text (length, punctuation, case, digits)", list(range(TEXT_ONLY))),
              ("all surface features (adding state size, option count and instructions)", everything)]
    rows = []
    for name, columns in models:
        scored = class_model(train, test, features, lambda r: r.lclass, columns)
        if scored is None:
            report.add("The test file has a label the training file never has, or train has one label only: "
                       "the surface models were not run.")
            break
        accuracy, balanced, sizes = scored
        classes = len(sizes)
        flagged, z = balanced_beats_chance(balanced, sizes)
        rows.append([name, pct(accuracy), pct(balanced), pct(1 / classes), "**flag**" if flagged else ""])
        if flagged:
            report.flags.append(f"A model that sees only {name} predicts the label with {pct(balanced)} balanced "
                                f"accuracy on test, against {pct(1 / classes)} by chance (z = {z:.1f}).")
    if rows:
        report.lines += table(["model (predicts the label)", "accuracy", "balanced accuracy", "chance", ""], rows)
        single = []
        for i, name in enumerate(SURFACE_NAMES):
            scored = class_model(train, test, features, lambda r: r.lclass, [i])
            if scored is not None:
                single.append((scored[1], name))
        single.sort(reverse=True)
        report.add("**Strongest single features** (balanced accuracy on test from one feature alone): "
                   + "; ".join(f"{name} {pct(value)}" for value, name in single[:6]))
    one_against_rest(report, train, test, features, models[:2])
    picker(report, train, test, fixed)


def one_against_rest(report: Report, train: list[Record], test: list[Record], features: tuple[FloatArray, FloatArray],
                     models: list[tuple[str, list[int]]]) -> None:
    """A shortcut can single out one label (refund requests are long) while the others look alike, which a model over
    all labels dilutes. So each label with enough rows is also told apart from all the others (chance 50%)."""
    labels = [c for c, n in sorted(collections.Counter(r.lclass for r in train).items()) if n >= MIN_CLASS_ROWS]
    if len(labels) < 3:
        return
    rows = []
    for label in labels:
        cells: list[object] = [label]
        for name, columns in models:
            scored = class_model(train, test, features, lambda r: "this label" if r.lclass == label else "other labels", columns)
            if scored is None or len(scored[2]) < 2:
                cells.append("not run (test lacks one side)")
                continue
            flagged, z = balanced_beats_chance(scored[1], scored[2])
            cells.append(f"{pct(scored[1])}{' **flag**' if flagged else ''}")
            if flagged:
                report.flags.append(f"A model that sees only {name} tells '{label}' from the other labels with "
                                    f"{pct(scored[1])} balanced accuracy on test, against 50.0% by chance (z = {z:.1f}).")
        rows.append(cells)
    report.add("**One label against all the others** (balanced accuracy on test; chance 50%):")
    report.lines += table(["label", *(name for name, _ in models)], rows)


def option_features(r: Record, compared: list[int]) -> list[list[float]]:
    """Per-option numbers that ignore meaning: position, length and shape."""
    texts = [r.options[i][1] for i in compared]
    lengths = np.asarray([len(t) for t in texts], dtype=np.float64)
    spread = lengths.std() or 1.0
    n = len(texts)
    return [[i / max(n - 1, 1), float(i == 0), float(i == n - 1), math.log1p(len(t)), (len(t) - lengths.mean()) / spread,
             float(len(t) == lengths.max()), float(len(t) == lengths.min()), math.log1p(n), math.log1p(len(t.split())),
             float(t.count(",")), float(t.count("?")), float(t.count("(")), float(bool(DIGIT.search(t))),
             sum(c.isupper() for c in t) / (len(t) or 1)] for i, t in enumerate(texts)]


def picker_data(records_: list[Record], fixed: set[str]) -> tuple[FloatArray, IntArray, IntArray, list[float]]:
    features: list[list[float]] = []
    starts, correct, chances = [], [], []
    for r in records_:
        compared = compared_options(r, fixed)
        if compared is None or r.position is None:
            continue
        starts.append(len(features))
        correct.append(len(features) + compared.index(r.position))
        chances.append(1 / len(compared))
        features += option_features(r, compared)
    return (np.asarray(features, dtype=np.float64), np.asarray(starts, dtype=np.int64),
            np.asarray(correct, dtype=np.int64), chances)


def picker(report: Report, train: list[Record], test: list[Record], fixed: set[str]) -> None:
    x_train, s_train, c_train, _ = picker_data(train[:: max(1, len(train) // PICKER_CAP)], fixed)
    x_test, s_test, c_test, chances = picker_data(test, fixed)
    if not len(s_train) or not len(s_test):
        return
    picked = Picker(x_train, s_train, c_train).pick(x_test, s_test)
    accuracy = float(np.mean(s_test + picked == c_test))
    flagged, z = beats_chance(accuracy, chances)
    report.add(f"**Option picker without meaning**: scores each option from its position, length and shape (commas, "
               f"brackets, digits, capitals), never its words or the input, and picks the top option per row. Test "
               f"accuracy {pct(accuracy)} against chance {pct(sum(chances) / len(chances))} over {len(chances)} rows"
               + (" **flag**." if flagged else "."))
    if flagged:
        report.flags.append(f"An option picker that never reads the input finds the answer in {pct(accuracy)} of test "
                            f"rows against {pct(sum(chances) / len(chances))} by chance (z = {z:.1f}).")


def gap_z(first: float, first_rows: int, second: float, second_rows: int) -> float:
    """Two-proportion z-score: how many standard errors apart two shares are."""
    pooled = (first * first_rows + second * second_rows) / (first_rows + second_rows)
    error = math.sqrt(pooled * (1 - pooled) * (1 / first_rows + 1 / second_rows))
    return (first - second) / error if error > 0 else 0.0


def shares_table(report: Report, records_: list[Record], key: Callable[[Record], str], what: str) -> None:
    classes = [c for c, n in sorted(collections.Counter(key(r) for r in records_).items()) if n >= MIN_CLASS_ROWS]
    if len(classes) < 2:
        report.add(f"Fewer than two {what}s have {MIN_CLASS_ROWS} or more training rows: not compared.")
        return
    sizes = collections.Counter(key(r) for r in records_)
    rows = []
    for name, test in WORD_FEATURES:
        shares = [float(np.mean([test(r.text) for r in records_ if key(r) == c])) for c in classes]
        high, low = max(shares), min(shares)
        flagged = (high - low >= FORMAT_GAP and high >= FORMAT_RATIO * max(low, 1e-9)
                   and gap_z(high, sizes[classes[shares.index(high)]], low, sizes[classes[shares.index(low)]]) >= Z)
        rows.append([name, *(pct(s) for s in shares), "**gap**" if flagged else ""])
        if flagged:
            order = sorted(zip(shares, classes), reverse=True)
            report.flags.append(f"'{name}' differs by {what}: {order[0][1]} {pct(order[0][0])} against {order[-1][1]} "
                                f"{pct(order[-1][0])}. If that is not what the {what} means, Jeff can learn it as a shortcut.")
    report.lines += table(["feature (share of training rows)", *classes, ""], rows)


def check_words(report: Report, train: list[Record]) -> None:
    report.heading("4. Words and punctuation by label")
    report.add("Share of each label's training rows whose main text has the feature. One kind of request missing filler "
               "words, or questions always having articles, lets a model tell labels apart by style.")
    shares_table(report, train, lambda r: r.lclass, "label")
    phrases(report, train)


def ngrams(text: str) -> set[str]:
    words = normalize(text).split()
    return {" ".join(words[i:i + n]) for n in (1, 2, 3) for i in range(len(words) - n + 1)}


def phrases(report: Report, train: list[Record]) -> None:
    labels = collections.Counter(r.lclass for r in train)
    found = collections.Counter[str]()
    by_label: dict[str, collections.Counter[str]] = collections.defaultdict(collections.Counter)
    for r in train:
        grams = ngrams(r.text)
        found.update(grams)
        by_label[r.lclass].update(grams)
    flagged = []
    for label, count in labels.items():
        base = count / len(train)
        for phrase, rows in by_label[label].items():
            precision = rows / found[phrase]
            if (found[phrase] >= PHRASE_MIN_ROWS and rows / count > PHRASE_SHARE and precision >= PHRASE_PRECISION
                    and precision >= 2 * base):
                flagged.append((label, phrase, rows / count, precision, found[phrase]))
    # Keep the longest of nested phrases that cover (almost) the same rows.
    flagged.sort(key=lambda f: (-f[2], f[1]))
    kept = [f for f in flagged if not any(g[0] == f[0] and g[1] != f[1] and f" {f[1]} " in f" {g[1]} " and g[4] >= 0.9 * f[4]
                                         for g in flagged)]
    report.add(f"**Phrases mostly found in one label** (1 to 3 words, in more than {PHRASE_SHARE:.0%} of the label's rows, "
               f"at least {PHRASE_PRECISION:.0%} of the rows with it in that label, at least {PHRASE_MIN_ROWS} rows): "
               + ("; ".join(f"{label}: `{phrase}` ({pct(share)} of the label, {pct(precision)} of its {rows} rows)"
                            for label, phrase, share, precision, rows in kept[:30]) or "none"))
    if kept:
        report.flags.append(f"{len(kept)} phrases are mostly found in one label (see section 4). Words that carry the "
                            "meaning are expected; style words of one generation round or one source are shortcuts.")


def shingles(text: str) -> set[str]:
    words = re.findall(r"\w+", text.lower())
    if len(words) >= 5:
        return {" ".join(words[i:i + 3]) for i in range(len(words) - 2)}
    joined = " ".join(words)
    return {joined[i:i + 5] for i in range(max(1, len(joined) - 4))} if joined else set()


PERMUTATIONS = 64
BANDS = 16


def near_pairs(texts: list[str]) -> list[tuple[int, int, float]]:
    """Pairs of texts whose shingle Jaccard similarity is at least NEAR_DUPLICATE: candidates from MinHash with
    banding, each confirmed exactly."""
    rng = np.random.RandomState(7)
    multipliers = rng.randint(1, 2 ** 62, PERMUTATIONS, dtype=np.int64).astype(np.uint64) | np.uint64(1)
    offsets = rng.randint(0, 2 ** 62, PERMUTATIONS, dtype=np.int64).astype(np.uint64)
    sets = [shingles(t) for t in texts]
    signatures = np.zeros((len(texts), PERMUTATIONS), dtype=np.uint64)
    for i, values in enumerate(sets):
        if values:
            hashes = np.asarray([int.from_bytes(hashlib.blake2b(v.encode(), digest_size=8).digest(), "little") for v in values],
                                dtype=np.uint64)
            signatures[i] = (np.outer(hashes, multipliers) + offsets).min(axis=0)  # wraps around 2**64 on purpose
    width = PERMUTATIONS // BANDS
    candidates: set[tuple[int, int]] = set()
    for band in range(BANDS):
        buckets: dict[bytes, list[int]] = collections.defaultdict(list)
        for i in range(len(texts)):
            if sets[i]:
                buckets[signatures[i, band * width:(band + 1) * width].tobytes()].append(i)
        for members in buckets.values():
            candidates.update((members[a], members[b]) for a in range(len(members)) for b in range(a + 1, min(len(members), a + 200)))
    pairs = []
    for i, j in sorted(candidates):
        similarity = len(sets[i] & sets[j]) / len(sets[i] | sets[j])
        if similarity >= NEAR_DUPLICATE:
            pairs.append((i, j, similarity))
    return pairs


def check_duplicates(report: Report, train: list[Record], test: list[Record]) -> None:
    report.heading("5. Near duplicates and split separation")
    shared = sorted({r.family for r in train} & {r.family for r in test})
    report.add(f"- Families in both train and test: {len(shared)}" + (f" ({', '.join(shared[:8])})" if shared else "") + ".")
    if shared:
        report.flags.append(f"{len(shared)} families appear in both train and test; hold out whole families "
                            "(jeff-kit split) so the test measures unseen ones.")
    prompts: dict[tuple[str, str, str], set[str]] = collections.defaultdict(set)
    for r in train:
        prompts[(r.state_text, repr(r.options), r.instructions or "")].add(r.label)
    conflicts = sum(len(labels) > 1 for labels in prompts.values())
    report.add(f"- Identical training prompts (state, options and instructions) with different answers: {conflicts}.")
    if conflicts:
        report.flags.append(f"{conflicts} identical training prompts have different answers: at least one label is wrong.")
    exact = {normalize(r.text) for r in train}
    copied = [r for r in test if normalize(r.text) in exact]
    report.add(f"- Test rows whose main text appears verbatim in train (normalised): {len(copied)} of {len(test)}"
               + ("; for example " + "; ".join(f'"{short(r.text, 60)}"' for r in copied[:3]) if copied else "")
               + ". Short generic texts can be legitimate on different screens or companies; jeff-kit leak-check decides.")
    rows = train + test
    pairs = near_pairs([r.text for r in rows])
    cross = [(i, j, s) for i, j, s in pairs if (rows[i].split == "train") != (rows[j].split == "train")]
    held = {j if rows[i].split == "train" else i for i, j, _ in cross}
    share = len(held) / len(test)
    report.add(f"- Test rows with a near duplicate in train (main-text similarity of at least {NEAR_DUPLICATE}): "
               f"{len(held)} ({pct(share)})" + (" **flag**." if share > NEAR_SHARE else "."))
    for i, j, s in cross[:5]:
        report.lines.append(f"  - \"{short(rows[i].text, 70)}\" ~ \"{short(rows[j].text, 70)}\" (similarity {s:.2f})")
    if cross:
        report.lines.append("")
    if share > NEAR_SHARE:
        report.flags.append(f"{pct(share)} of test rows have a near duplicate in train (more than {NEAR_SHARE:.0%}): the "
                            "test partly measures memory, not understanding.")
    within = [(i, j) for i, j, _ in pairs if rows[i].split == rows[j].split == "train" and rows[i].lclass != rows[j].lclass]
    report.add(f"- Near-duplicate training pairs with different labels: {len(within)} (can be right when the rest of "
               "the state or the options differ).")


def check_kinds(report: Report, train: list[Record], test: list[Record], class_field: str) -> None:
    report.heading(f"6. Kinds of row (`{class_field}`)")
    kinds = sorted({r.kind or "" for r in train})
    labels = sorted({r.lclass for r in train})
    overall = {label: sum(r.lclass == label for r in train) / len(train) for label in labels}
    rows = []
    for kind in kinds:
        members = [r for r in train if r.kind == kind]
        shares = {label: sum(r.lclass == label for r in members) / len(members) for label in labels}
        rows.append([kind, len(members), *(pct(shares[label]) for label in labels)])
        if len(members) >= MIN_CLASS_ROWS:
            for label in labels:
                if abs(shares[label] - overall[label]) > KIND_LABEL_GAP:
                    report.flags.append(f"Rows of kind '{kind}' are {pct(shares[label])} '{label}' against "
                                        f"{pct(overall[label])} overall: if this kind is recognisable by its style, "
                                        "the style predicts the label.")
    rows.append(["all", len(train), *(pct(overall[label]) for label in labels)])
    report.add("**Label mix per kind (train)**")
    report.lines += table(["kind", "rows", *labels], rows)
    scored = class_model(train, test, surface_matrix(train, test), lambda r: r.kind or "", list(range(len(SURFACE_NAMES))))
    if scored is None:
        report.add("The test file has a kind the training file never has, or train has one kind only: the kind model was not run.")
    else:
        accuracy, balanced, sizes = scored
        classes = len(sizes)
        flagged, z = balanced_beats_chance(balanced, sizes)
        report.add(f"A surface-feature model tells the kinds apart on test with balanced accuracy {pct(balanced)} against "
                   f"{pct(1 / classes)} by chance" + (" **flag**." if flagged else "."))
        if flagged:
            report.flags.append(f"Surface features tell the row kinds apart ({pct(balanced)} balanced accuracy against "
                                f"{pct(1 / classes)}, z = {z:.1f}): each kind has its own style.")
    report.add("**Words and punctuation by kind (train)**")
    shares_table(report, train, lambda r: r.kind or "", "kind")


def check_junk(report: Report, train: list[Record], test: list[Record]) -> None:
    report.heading("7. Leftovers and personal data")
    rows = []
    for name, pattern, flag in JUNK:
        hits = [r for r in train + test if pattern.search(r.state_text) or any(pattern.search(t) for _, t in r.options)]
        rows.append([name, len(hits), "; ".join(f"`{r.id}`" for r in hits[:4])])
        if hits and flag:
            report.flags.append(f"{len(hits)} rows match '{name}', for example `{hits[0].id}`.")
    report.lines += table(["pattern (whole state and options, train and test)", "rows", "examples"], rows)


THRESHOLDS = f"""## How the flags are decided

- **Beats chance clearly**: at least {MARGIN:.0%} (percentage points) above chance and at least {Z:g} standard errors
  above it, where the standard error is that of guessing at chance on the same rows (for balanced accuracy, with
  each label's number of test rows). Small files and rare labels need a bigger lead.
- **Label balance**: a label's share differs between train and test by more than {BALANCE_GAP:.0%} (percentage points).
- **One label against the rest**: the same "beats chance clearly" rule against 50%, for each label with at least
  {MIN_CLASS_ROWS} training rows (run when there are 3 or more labels).
- **Option picker, option position and option length**: the same rule, against the chance of guessing among each row's
  options.
- **Words and punctuation**: shares differ by at least {FORMAT_GAP:.0%} (percentage points), the highest is at least
  {FORMAT_RATIO:g} times the lowest, and they are at least {Z:g} standard errors apart (two-proportion test); labels or
  kinds with fewer than {MIN_CLASS_ROWS} training rows are left out.
- **Phrases**: in more than {PHRASE_SHARE:.0%} of one label's rows, at least {PHRASE_PRECISION:.0%} of the rows
  containing it in that label (and at least twice the label's share), and in at least {PHRASE_MIN_ROWS} rows.
- **Kinds of row**: a label's share inside a kind (of at least {MIN_CLASS_ROWS} rows) is more than {KIND_LABEL_GAP:.0%}
  (percentage points) from its share overall; a surface model telling the kinds apart uses the "beats chance" rule.
- **Near duplicates**: more than {NEAR_SHARE:.0%} of test rows have a training row with main-text similarity of at
  least {NEAR_DUPLICATE} (Jaccard similarity of word 3-grams, or of character 5-grams for texts under 5 words).
- **Leftovers**: any row matching a flagged pattern in section 7.

A flag asks for a person to look. Some are expected: a "question" label should end with "?" more often. A flag is a
problem when the feature is not what the label means, or when it would not hold for real inputs.
"""


def shortcut_report(train_rows: Sequence[Example], test_rows: Sequence[Example], *, text_field: str | None = None,
                    class_field: str | None = None, names: tuple[str, str] = ("train", "test"), seed: int = 0) -> tuple[str, list[str]]:
    """The Markdown report and its flags."""
    train = records(train_rows, "train", text_field, class_field)
    test = records(test_rows, "test", text_field, class_field)
    fixed = fixed_keys(train)
    for r in train + test:
        if r.options and r.label not in fixed:
            r.lclass = LISTED
    if not any(r.options for r in train) or not fixed:
        for r in train + test:
            r.lclass = r.label
    analysed = sample(train, SAMPLE_CAP, seed)
    report = Report()
    check_balance(report, train, test)
    check_options(report, train, test, fixed)
    check_surface(report, analysed, test, fixed)
    check_words(report, analysed)
    check_duplicates(report, train, test)
    if class_field:
        check_kinds(report, analysed, test, class_field)
    check_junk(report, train, test)
    head = ["# Shortcut report", "",
            f"Training file `{names[0]}`: {len(train):,} rows, {len({r.family for r in train})} families. "
            f"Test file `{names[1]}`: {len(test):,} rows, {len({r.family for r in test})} families.",
            "" if len(analysed) == len(train) else f"\nModels and phrase counts use a label-stratified sample of {len(analysed):,} training rows.",
            "", "## Flags", ""]
    head += [f"- {flag}" for flag in report.flags] or ["- none: no check beat chance clearly."]
    return "\n".join(head + [""] + report.lines + [THRESHOLDS]), report.flags
