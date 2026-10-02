from collections import Counter

from jeff import layout
from jeff.data import validate
from jeff.families import BY_NAME
from jeff.types import Example


def synthetic(family: str, state: str, label: object, number: int = 0) -> Example:
    return {"id": f"syn-1-{family}-{number:06d}", "suite": "synthetic", "family": f"syn-{family}-00000", "state": state,
            "question": BY_NAME[family].question(), "label": label, "target": label,  # type: ignore[typeddict-item]
            "source": {"dataset": "synthetic-spark", "family": family}}


def test_labelled_splits_parts_in_order_and_rejects_bad_layouts() -> None:
    assert layout.labelled("Source: a b\nInstruction: c\nResponse: d", ("Source", "Instruction", "Response")) == ("", ["a b", "c", "d"])
    assert layout.labelled("Response: d\nSource: a", ("Source", "Response")) is None             # out of order
    assert layout.labelled("Source: a\nSource: b\nResponse: c", ("Source", "Response")) is None  # repeated
    assert layout.labelled("Source: a", ("Source", "Response")) is None                          # missing
    assert layout.labelled("The Source: a\nResponse: b", ("Source", "Response")) is None         # label not at a line start


def test_trailing_options_rejects_gaps_in_lettered_options() -> None:
    assert layout.trailing_options("Question?\nA: first\nC: third") is None


def test_pairwise_moves_the_correct_response_and_its_label_together() -> None:
    row = synthetic("pairwise_answer_quality", "Question: 2+2?\nResponse A: 4, right.\nResponse B: 5, wrong.", "A")
    panel = layout.pairwise(row, panel=True, target=1)
    assert panel["state"] == {"question": "2+2?", "response_A": "5, wrong.", "response_B": "4, right."}
    assert panel["label"] == panel["target"] == "B"
    text = layout.pairwise(row, panel=False, target=1)
    assert text["state"] == "Question: 2+2?\nResponse A: 5, wrong.\nResponse B: 4, right." and text["label"] == "B"
    validate([panel])
    validate([text])


def test_lettered_options_move_into_descriptions_and_keep_the_right_answer() -> None:
    state = "Ann has the red ball.\nQuestion: Which ball does Ann hold?\nA: blue\nB: red\nC: green\nD: pink"
    row = synthetic("object_tracking", state, "B")
    for target in range(4):
        panel = layout.lettered(row, panel=True, target=target)
        assert panel["question"]["instructions"] == layout.GENERIC
        assert panel["question"]["criteria"][panel["label"]] == "red" and panel["label"] == "ABCD"[target]
        assert panel["state"] == "Ann has the red ball.\nWhich ball does Ann hold?"
        text = layout.lettered(row, panel=False, target=target)
        assert f"{text['label']}: red" in text["state"] and text["question"] == row["question"]
        validate([panel])
    validate([text])


def test_summary_consistency_asks_the_faithfulness_question_so_the_label_flips() -> None:
    row = synthetic("summary_consistency", "Document: The shop opened in May.\nSummary: The shop opened in June.", False)
    panel = layout.summary_consistency(row, panel=True, target=0)
    assert panel["question"] == layout.FAITHFULNESS and panel["label"] is True
    assert panel["state"] == {"task": "Summary", "instruction": "Summarize the following text within 10 words:",
                              "source": "The shop opened in May.", "response": "The shop opened in June."}
    validate([panel])


def test_yes_no_families_take_bbh_form() -> None:
    panel = layout.CONVERTERS["navigate"](synthetic("navigate", "Always face forward. Take 1 step left.", False), True, 0)
    assert panel["label"] == "No" and panel["question"]["criteria"] == {"Yes": None, "No": None}
    assert panel["state"].startswith("If you follow these instructions, do you return to the starting point? Always face")
    validity = layout.CONVERTERS["argument_validity"](synthetic("argument_validity", "All A are B. So B.", "valid"), True, 0)
    assert validity["label"] == "valid" and validity["state"].endswith("deductively valid or invalid?")
    validate([panel])
    validate([validity])


def test_rearrange_uses_the_panel_layout_for_most_rows_and_balances_positions() -> None:
    rows = [synthetic("pairwise_answer_quality", f"Question: q{i}?\nResponse A: right {i}\nResponse B: wrong {i}", "A", i)
            for i in range(400)]
    rows += [synthetic("sarcasm", f"Statement {i}.", i % 2 == 0, i) for i in range(40)]
    rows.append(synthetic("pairwise_answer_quality", "no parts here", "A", 999))
    result, report = layout.rearrange(rows, seed=7)
    pairwise = [r for r in result if r["source"]["family"] == "pairwise_answer_quality" and r["id"] != "syn-1-pairwise_answer_quality-000999"]
    panel_share = sum(r["source"]["layout"] == "panel" for r in pairwise) / len(pairwise)
    assert 0.68 < panel_share < 0.82
    labels = Counter(r["label"] for r in pairwise)
    assert abs(labels["A"] - labels["B"]) < 60
    def correct(row: Example) -> str:
        if isinstance(row["state"], dict):
            return row["state"]["response_" + row["label"]]
        return next(line for line in row["state"].split("\n") if line.startswith(f"Response {row['label']}:"))
    assert all("right" in correct(row) for row in pairwise)
    assert report["pairwise_answer_quality: unparsed, kept as is"] == 1
    pairs = [r for r in result if "+" in r["id"]]
    assert pairs and all(r["state"] == "Which statement is sarcastic?" for r in pairs)
    assert all(r["question"]["criteria"][r["label"]].startswith("Statement") for r in pairs)
    assert all(int(r["question"]["criteria"][r["label"]].split()[1].rstrip(".")) % 2 == 0 for r in pairs)  # the sarcastic one
    validate(result)


def test_new_families_take_bbh_form() -> None:
    translation = synthetic("translation_error", "Source: Die Brücke wurde 1901 eröffnet.\nTranslation: The bridge was opened in 1910.",
                            "numerical_values")
    panel = layout.translation_error(translation, panel=True, target=0)
    assert panel["question"]["criteria"][panel["label"]] == "Numerical Values"
    assert panel["state"].endswith("Translation: The bridge was opened in 1910.\nThe translation contains an error pertaining to")
    text = "Sentence: Mia told Ann that she had won.\nA: Mia had won\nB: Ann had won\nC: Ambiguous"
    for label, target, expected in (("A", 1, "Mia had won"), ("C", 1, "Ambiguous")):
        row = layout.disambiguation(synthetic("disambiguation", text, label), panel=True, target=target)
        assert row["question"]["criteria"][row["label"]] == expected and row["question"]["criteria"]["C"] == "Ambiguous"
        assert row["state"].startswith(layout.DISAMBIGUATION_PROMPT + "Sentence: Mia")
        validate([row])
    causal = layout.CONVERTERS["causal_judgement"](synthetic("causal_judgement", "Tom left the gate open. Did Tom cause the escape?", True), True, 0)
    assert causal["label"] == "Yes" and causal["state"].startswith("How would a typical person answer")
    validate([panel, causal])
