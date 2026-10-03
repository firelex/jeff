import json
from pathlib import Path
from typing import Any

import pytest

from jeff.kit import cli, leaks, measure, replay, shortcuts, split
from jeff.kit.rows import RowError, check_rows, load

CRITERIA = {"question": "The user is asking a question", "command": "The user wants something done", "other": "None of these"}
QUESTIONS = ["what time is it", "where is my order", "how do I reset it", "who sent this", "why is it slow",
             "when does it open", "which one is mine", "what does this cost", "how far is it", "where do I sign"]
COMMANDS = ["open it", "stop", "go back", "send it", "close", "next page", "save", "play", "undo", "call home"]
OTHERS = ["nice weather", "blue sky", "my cat", "a sandwich", "cold coffee", "old shoes", "red car", "tall tree", "warm soup", "wet dog"]


def row(identifier: str, family: str, text: str, label: str, criteria: dict[str, str] | None = None, **extra: Any) -> dict[str, Any]:
    return {"id": identifier, "suite": "test", "family": family, "state": {"screen": "home", "transcript": text},
            "question": {"type": "choice", "instructions": "What does the user want?", "criteria": criteria or CRITERIA},
            "label": label, "target": label, "source": {"dataset": "unit", **extra}}


def rows(prefix: str, families: int, padded: bool = False) -> list[dict[str, Any]]:
    """Three labels, ten rows each, spread over families. Without `padded` the text says nothing about the label (no
    surface signal at all); `padded` uses real texts and plants a length shortcut: every question is long."""
    result = []
    for index in range(30):
        label, pool = [("question", QUESTIONS), ("command", COMMANDS), ("other", OTHERS)][index % 3]
        text = f"{prefix} {pool[index // 3]}" if padded else f"{prefix} note {OTHERS[index % 10]}"
        if padded and label == "question":
            text += " and could you please explain it to me in some detail because I really need to know today"
        result.append(row(f"{prefix}-{index}", f"{prefix}-family-{index % families}", text, label))
    return result


def write(path: Path, values: list[dict[str, Any]]) -> Path:
    path.write_text("".join(json.dumps(value) + "\n" for value in values))
    return path


# ---------------------------------------------------------------- check-rows

def test_check_rows_accepts_good_rows(tmp_path: Path) -> None:
    assert len(load(write(tmp_path / "rows.jsonl", rows("a", 5)))) == 30


@pytest.mark.parametrize(("change", "message"), [
    ({"label": "maybe"}, "label 'maybe' is not one of the option keys"),
    ({"family": ""}, "'family' must be a non-empty string"),
    ({"criteria": {"1": "One", "2": "Two"}, "label": "1"}, "bare numbers"),
    ({"text": "Your order {{Order Number}} has shipped"}, "unfilled template slots: {{Order Number}}"),
])
def test_check_rows_names_the_row_and_problem(change: dict[str, Any], message: str) -> None:
    bad = row("r2", "f", change.get("text", "hello"), change.get("label", "other"), change.get("criteria"))
    if "family" in change:
        bad["family"] = change["family"]
    with pytest.raises(RowError, match="row 2 \\(id 'r2'\\)") as error:
        check_rows([row("r1", "f", "hi", "other"), bad], Path("x.jsonl"))
    assert message in str(error.value)


def test_check_rows_finds_missing_fields_and_duplicate_ids(tmp_path: Path) -> None:
    first = row("same", "f", "hi", "other")
    second = row("same", "f", "hello", "other")
    del second["source"]
    with pytest.raises(RowError) as error:
        load(write(tmp_path / "rows.jsonl", [first, second]))
    assert "missing field 'source'" in str(error.value) and "id 'same' is used by 2 rows" in str(error.value)


def test_check_rows_rejects_missing_file_and_bad_json(tmp_path: Path) -> None:
    with pytest.raises(RowError, match="does not exist"):
        load(tmp_path / "absent.jsonl")
    (tmp_path / "broken.jsonl").write_text('{"id": 1}\n{not json\n')
    with pytest.raises(RowError, match="line 2 is not valid JSON"):
        load(tmp_path / "broken.jsonl")


# ---------------------------------------------------------------- split

def test_split_keeps_families_together(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    source = write(tmp_path / "rows.jsonl", rows("a", 10))
    cli.main(["split", str(source), "--out", str(tmp_path / "out"), "--test", "0.2", "--development", "0.1",
              "--calibration", "0.1", "--seed", "3"])
    families = {name: {r["family"] for r in load(tmp_path / "out" / f"{name}.jsonl")} for name in split.SPLITS}
    assert sum(len(f) for f in families.values()) == 10 == len(set().union(*families.values()))
    assert all(families.values())
    assert "Label share per split" in capsys.readouterr().out


@pytest.mark.parametrize(("test_share", "development_share", "calibration_share"), [
    (0.1, 0.1, 0.1),
    (0.1, 0.0, 0.1),
    (0.0, 0.1, 0.1),
    (0.1, 0.1, 0.0),
    (0.1, 0.0, 0.0),
    (0.0, 0.0, 0.0),
])
def test_split_reserves_families_for_later_splits(
    test_share: float, development_share: float, calibration_share: float,
) -> None:
    families = sorted(["a", "b", "c", "d"], key=lambda family: split.ranking(family, 0))
    examples: Any = [
        row(f"{family}-{index}", family, "hello", "other")
        for position, family in enumerate(families)
        for index in range(100 if position == 3 else 1)
    ]
    shares = {"test": test_share, "development": development_share, "calibration": calibration_share}

    result = split.split_by_family(examples, shares, 0)

    assert result["train"]
    for name, share in shares.items():
        assert bool(result[name]) == (share > 0)
    assigned = [{example["family"] for example in values} for values in result.values()]
    assert sum(len(group) for group in assigned) == len(set().union(*assigned)) == 4
    assert sorted(example["id"] for values in result.values() for example in values) == sorted(
        example["id"] for example in examples
    )
    assert result == split.split_by_family(examples, shares, 0)


def test_split_refuses_impossible_shares() -> None:
    examples: Any = rows("a", 3)
    with pytest.raises(ValueError, match="at least 4"):
        split.split_by_family(examples, {"test": 0.2, "development": 0.2, "calibration": 0.2}, 1)
    with pytest.raises(ValueError, match="together below 1"):
        split.split_by_family(examples, {"test": 0.5, "development": 0.3, "calibration": 0.2}, 1)


# ---------------------------------------------------------------- leak-check

def test_leak_check_finds_copies_and_near_copies(tmp_path: Path) -> None:
    long = " ".join(f"word{i}" for i in range(30))
    evaluation = [row("e1", "test-family", "open the blinds", "command"), row("e2", "test-family", long, "other")]
    train = [row("t1", "train-family", "  Open the blinds! ", "command"), row("t2", "train-family", long + " extra", "other"),
             row("t3", "train-family", "close the door", "command")]
    found, _ = leaks.find_leaks(train, evaluation)  # type: ignore[arg-type]
    assert {leak["train_id"]: [m["rule"] for m in leak["matches"]] for leak in found} == {"t1": ["state"], "t2": ["near"]}
    with pytest.raises(SystemExit) as stopped:
        cli.main(["leak-check", "--train", str(write(tmp_path / "t.jsonl", train)), "--against", str(write(tmp_path / "e.jsonl", evaluation))])
    assert stopped.value.code == 1


def test_leak_check_ignores_text_shared_by_many_evaluation_rows() -> None:
    """A state every evaluation row shares is a fixed prompt; the candidates in the options decide."""
    prompt = "Which statement is sarcastic?"
    evaluation = [row(f"e{i}", f"f{i}", prompt, "a", {"a": f"Great, rain again on day {i} of my holiday.", "b": "Nice and sunny today."})
                  for i in range(leaks.TEMPLATE_AT)]
    fresh = row("t1", "t", prompt, "a", {"a": "Oh wonderful, a flat tyre on the motorway.", "b": "Nice and sunny today."})
    copied = row("t2", "t", prompt, "a", {"a": "Great, rain again on day 3 of my holiday.", "b": "Nice and sunny today."})
    found, index = leaks.find_leaks([fresh, copied], evaluation)  # type: ignore[arg-type]
    assert [leak["train_id"] for leak in found] == ["t2"] and index.templates


def test_leak_check_flags_a_shared_family() -> None:
    evaluation = [row("e1", "company-1", "something else", "other"), row("e2", "company-2", "more", "other")]
    found, _ = leaks.find_leaks([row("t1", "company-1", "hello there", "other")], evaluation)  # type: ignore[arg-type]
    assert [m["rule"] for m in found[0]["matches"]] == ["family"]


# ---------------------------------------------------------------- shortcut-report

def test_report_flags_a_planted_length_shortcut() -> None:
    report, flags = shortcuts.shortcut_report(rows("train", 6, padded=True), rows("test", 3, padded=True))  # type: ignore[arg-type]
    assert any("length of the main text" in flag and "'question'" in flag for flag in flags), flags
    assert "**flag**" in report and "## How the flags are decided" in report


def test_report_is_quiet_on_clean_data() -> None:
    _, flags = shortcuts.shortcut_report(rows("train", 6), rows("test", 3))  # type: ignore[arg-type]
    assert not any("A model that sees only" in flag or "option picker" in flag for flag in flags), flags


def test_report_flags_longest_correct_option_and_kinds(tmp_path: Path) -> None:
    def listed(prefix: str, count: int) -> list[dict[str, Any]]:
        result = []
        for index in range(count):
            options = {f"o{k}": f"item {k}" for k in range(1, 5)}
            answer = f"o{1 + index % 4}"
            options[answer] = "the item that is described at much greater length than the others"
            result.append(row(f"{prefix}{index}", f"{prefix}{index % 5}", f"pick {index}", answer, options,
                              round="one" if index % 2 else "two"))
        return result
    train = write(tmp_path / "train.jsonl", listed("a", 40))
    test = write(tmp_path / "test.jsonl", listed("b", 40))
    cli.main(["shortcut-report", "--train", str(train), "--test", str(test), "--out", str(tmp_path / "r.md"), "--class-field", "source.round"])
    text = (tmp_path / "r.md").read_text()
    assert "the correct option is the longest one" in text
    assert "## 6. Kinds of row (`source.round`)" in text


def test_keys_naming_a_different_item_per_row_are_listed_options() -> None:
    examples = [row(f"r{i}", "f", "pick one", "o1", {"o1": f"photo {i}", "o2": f"song {i}", "none": "None of these"})
                for i in range(10)]
    train = shortcuts.records(examples, "train", None, None)  # type: ignore[arg-type]
    assert shortcuts.fixed_keys(train) == {"none"}


def test_report_requires_the_class_field_on_every_row() -> None:
    with pytest.raises(RowError, match="no 'source.round'"):
        shortcuts.shortcut_report(rows("a", 3), rows("b", 3), class_field="source.round")  # type: ignore[arg-type]


# ---------------------------------------------------------------- replay-mix

def replay_rows(count: int) -> list[dict[str, Any]]:
    return [{"id": f"replay-{i}", "suite": "general" if i % 2 else "other-suite", "family": f"g{i}", "state": f"sentence number {i}",
             "question": {"type": "noul", "instructions": "Is this a sentence?"}, "label": True, "target": True,
             "source": {"dataset": "mine"}} for i in range(count)]


def test_replay_mix_adds_the_share_and_skips_overlaps(tmp_path: Path) -> None:
    train = rows("a", 5)
    extra = replay_rows(20)
    protected = [{**extra[0], "id": "test-0", "family": "t"}]  # the replay row with the same state must be skipped
    chosen, report = replay.replay_sample(train, extra, protected, 0.1, seed=4)  # type: ignore[arg-type]
    assert len(chosen) == 3 == report["replay_rows"] and "replay-0" not in {r["id"] for r in chosen}
    out = tmp_path / "mixed.jsonl"
    cli.main(["replay-mix", "--train", str(write(tmp_path / "t.jsonl", train)), "--replay", str(write(tmp_path / "r.jsonl", extra)),
              "--protect", str(write(tmp_path / "p.jsonl", protected)), "--share", "0.1", "--out", str(out), "--seed", "4"])
    assert len(load(out)) == 33


def test_replay_mix_refuses_bad_settings() -> None:
    with pytest.raises(ValueError, match="above 0 and below 1"):
        replay.replay_sample(rows("a", 5), replay_rows(5), [], 1.5, 0)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="only 5"):
        replay.replay_sample(rows("a", 5), replay_rows(5), [], 0.5, 0)  # type: ignore[arg-type]


def test_allocate_uses_largest_remainders() -> None:
    assert replay.allocate({"a": 5, "b": 3, "c": 2}, 5) == {"a": 3, "b": 1, "c": 1}  # a and b tie at .5; a sorts first
    assert sum(replay.allocate({"a": 7, "b": 2, "c": 1}, 4).values()) == 4


# ---------------------------------------------------------------- evaluate (argument handling only: no model is loaded)

def checkpoint(path: Path, kind: str = "decision_config.json") -> Path:
    path.mkdir()
    (path / kind).write_text("{}")
    return path


def test_evaluate_builds_three_jeff_evaluate_runs(tmp_path: Path) -> None:
    test = write(tmp_path / "test.jsonl", rows("a", 3))
    planned = measure.commands(test, checkpoint(tmp_path / "jeff"), checkpoint(tmp_path / "merged"), tmp_path / "out",
                               untrained="Qwen/Qwen3.5-0.8B", untrained_revision="a" * 40, calibration=test, orders=2, batch_size=4)
    assert [name for name, _ in planned] == ["untrained", "jeff", "adapter"]
    untrained = planned[0][1]
    assert untrained[1:3] == ["-m", "jeff.evaluate"] and "--base-model" in untrained and "--calibration" in untrained
    assert planned[2][1][-2:] == ["--checkpoint", str(tmp_path / "merged")]


def test_evaluate_refuses_a_lora_only_folder_and_half_given_options(tmp_path: Path) -> None:
    test = write(tmp_path / "test.jsonl", rows("a", 3))
    jeff, lora = checkpoint(tmp_path / "jeff"), checkpoint(tmp_path / "lora", "adapter_config.json")
    with pytest.raises(ValueError, match="LoRA adapter weights only"):
        measure.commands(test, jeff, lora, tmp_path / "out", untrained=None, untrained_revision=None, calibration=None, orders=1, batch_size=8)
    with pytest.raises(ValueError, match="go together"):
        measure.commands(test, jeff, jeff, tmp_path / "out", untrained="Qwen/Qwen3.5-0.8B", untrained_revision=None,
                         calibration=None, orders=1, batch_size=8)
    with pytest.raises(ValueError, match="needs --untrained"):
        measure.commands(test, jeff, jeff, tmp_path / "out", untrained=None, untrained_revision=None, calibration=test, orders=1, batch_size=8)
    with pytest.raises(SystemExit, match="is not a folder"):
        cli.main(["evaluate", "--test", str(test), "--base", str(tmp_path / "absent"), "--adapter", str(jeff), "--out", str(tmp_path / "o")])


def test_results_block_lists_each_measurement() -> None:
    block = measure.results_block([{"name": "jeff", "accuracy": 0.5, "ece": 0.12, "count": 10},
                                   {"name": "adapter", "accuracy": 0.9, "ece": 0.03, "count": 10}])
    assert "Jeff alone" in block and "90.0%" in block and "0.030" in block
