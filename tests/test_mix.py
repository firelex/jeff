import json
import sys
from pathlib import Path

import pytest

from jeff import mix


def row(identifier: str, state: str, family: str | None = None, suite: str = "s") -> dict:
    return {"id": identifier, "suite": suite, "family": family or identifier, "state": state,
            "question": {"type": "noul"}, "label": True, "target": True, "source": {"dataset": suite}}


LONG = " ".join(f"word{i}" for i in range(40))


def test_short_exact_copy_is_dropped() -> None:
    guard = mix.LeakGuard([row("p1", "Sarah beat Maria so _ won.")])
    assert guard.leaks(row("t1", "  sarah beat maria so _ won. "))
    assert not guard.leaks(row("t2", "Maria beat Sarah so _ lost."))


def test_near_duplicate_long_text_is_dropped() -> None:
    guard = mix.LeakGuard([row("p1", LONG)])
    assert guard.leaks(row("t1", LONG + " extra"))
    assert not guard.leaks(row("t2", " ".join(f"other{i}" for i in range(40))))


def choice(identifier: str, state: str, options: dict[str, str]) -> dict:
    return {**row(identifier, state), "question": {"type": "choice", "instructions": "Pick.", "criteria": options}, "label": "A", "target": "A"}


def test_shared_prompt_with_different_candidates_is_not_a_leak() -> None:
    """In the panel's layout the state can be a prompt every item shares; the candidates in the options decide."""
    prompt_items = [choice(f"p{i}", "Which statement is sarcastic?", {"A": f"Great, rain again {i}.", "B": "Nice, sun again."})
                    for i in range(1, mix.SHARED_AT)]
    guard = mix.LeakGuard([choice("p0", "Which statement is sarcastic?", {"A": "Great, rain again.", "B": "Nice, sun again."}), *prompt_items])
    assert not guard.leaks(choice("t1", "Which statement is sarcastic?", {"A": "Oh wonderful, a flat tyre.", "B": "I fixed the tyre."}))
    assert guard.leaks(choice("t2", "Which statement is sarcastic?", {"A": "Great, rain again.", "B": "Nice, sun again."}))
    guard_long = mix.LeakGuard([choice("p2", "Pick one.", {"A": LONG, "B": "y"})])
    assert guard_long.leaks(choice("t4", "Choose.", {"A": LONG + " extra", "B": "z"}))  # a copied long candidate


def test_a_template_shared_by_many_panel_items_is_not_a_leak() -> None:
    template = "Write an objective overview of the business below using only the structured data and do not invent anything at all."
    article = "An article reused across several panel items, one per model response, " + LONG
    panel = [{**row(f"p{i}", ""), "state": {"instruction": template, "source": f"record {i} " + LONG}} for i in range(mix.SHARED_AT)]
    panel += [{**row(f"a{i}", ""), "state": {"instruction": "Summarize.", "source": article}} for i in range(6)]
    guard = mix.LeakGuard(panel)
    assert guard.leaks({**row("t3", ""), "state": {"instruction": "Summarize.", "source": article}})  # repeated content, not a template
    assert not guard.leaks({**row("t1", ""), "state": {"instruction": template, "source": "a different record " + " ".join(f"x{i}" for i in range(40))}})
    assert guard.leaks({**row("t2", ""), "state": {"instruction": template, "source": "record 1 " + LONG}})  # the item itself


def test_build_equal_sizes_and_disjoint_combined() -> None:
    public = [row(f"pub{i}", f"public text {i}", suite="boolq") for i in range(100)]
    synthetic = [row(f"syn{i}", f"synthetic text {i}", suite="synthetic") for i in range(100)]
    dev = [row(f"dev{i}", f"dev text {i}", suite="boolq") for i in range(10)]
    cal = [row(f"cal{i}", f"cal text {i}", suite="boolq") for i in range(10)]
    panel = [row("panel0", "public text 3"), row("panel1", "synthetic text 4"), row("panel2", "dev text 5")]
    sets, report = mix.build(public, synthetic, dev, cal, panel, size=50, sweep_size=20, seed=1)
    assert len(sets["public"]) == len(sets["synthetic"]) == 50
    assert len(sets["combined"]) == 100 and len(sets["sweep"]) == 20
    assert report["leaks"] == {"public": 1, "synthetic": 1, "dev": 1, "calibration": 0}
    assert all(r["id"] != "pub3" for r in sets["public"]) and len(sets["dev"]) == 9


def test_build_raises_when_synthetic_is_short() -> None:
    public = [row(f"pub{i}", f"p {i}") for i in range(100)]
    synthetic = [row(f"syn{i}", f"s {i}") for i in range(10)]
    with pytest.raises(ValueError, match="synthetic"):
        mix.build(public, synthetic, [row("d", "d")], [row("c", "c")], [], size=50, sweep_size=5, seed=1)


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def test_extra_rows_are_appended_to_public_before_mixing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    public_rows = [row(f"pub{i}", f"public text {i}", suite="boolq") for i in range(5)]
    extra_rows = [row(f"ext{i}", f"extra text {i}", suite="extra_ds") for i in range(5)]
    write_jsonl(tmp_path / "public.jsonl", public_rows)
    write_jsonl(tmp_path / "extra.jsonl", extra_rows)
    write_jsonl(tmp_path / "dev.jsonl", [row("dev0", "dev text 0", suite="boolq")])
    write_jsonl(tmp_path / "cal.jsonl", [row("cal0", "cal text 0", suite="boolq")])
    write_jsonl(tmp_path / "panel.jsonl", [])
    monkeypatch.setattr(sys, "argv", [
        "jeff-mix", "--public", str(tmp_path / "public.jsonl"), "--extra", str(tmp_path / "extra.jsonl"),
        "--dev", str(tmp_path / "dev.jsonl"), "--calibration", str(tmp_path / "cal.jsonl"),
        "--panel", str(tmp_path / "panel.jsonl"), "--also-exclude", str(tmp_path / "panel.jsonl"), "--size", "10", "--sweep-size", "10",
        "--seed", "1", "--out", str(tmp_path / "mix"), "--public-only",
    ])
    mix.main()
    written = mix.read(tmp_path / "mix" / "public.jsonl")
    assert len(written) == 10
    assert any(r["suite"] == "extra_ds" for r in written)


def test_public_only_arm_matches_the_public_set_of_the_full_build() -> None:
    public = [row(f"pub{i}", f"public text {i}", suite="boolq") for i in range(100)]
    synthetic = [row(f"syn{i}", f"synthetic text {i}", suite="synthetic") for i in range(100)]
    dev = [row(f"dev{i}", f"dev text {i}", suite="boolq") for i in range(10)]
    cal = [row(f"cal{i}", f"cal text {i}", suite="boolq") for i in range(10)]
    panel = [row("panel0", "public text 3")]
    full, _ = mix.build(public, synthetic, dev, cal, panel, size=50, sweep_size=20, seed=1)
    alone, report = mix.build_public(public, dev, cal, panel, size=50, sweep_size=20, seed=1)
    assert alone["public"] == full["public"]
    assert len(alone["sweep"]) == 20 and all(r["suite"] == "boolq" for r in alone["sweep"])
    assert report["leaks"] == {"public": 1, "dev": 0, "calibration": 0}


def test_short_state_copied_with_different_options_is_a_leak() -> None:
    """Voice commands and long-list messages are short; the near rule needs 20 words, so the state is matched exactly."""
    test_item = choice("v1", "Voice transcript: \"Quiet!\"", {"a": "Audio volume mute", "b": "Play music"})  # labels, not content
    guard = mix.LeakGuard([test_item])
    assert guard.leaks(choice("t1", "voice transcript  quiet", {"x": "Mute the audio", "y": "Weather ask", "z": "Joke"}))
    assert not guard.leaks(choice("t2", "Voice transcript: \"Louder!\"", {"a": "Audio volume mute", "b": "Play music"}))


def test_a_state_many_evaluation_items_share_is_a_prompt_not_a_leak() -> None:
    panel = [choice(f"p{i}", "Which statement is sarcastic?", {"A": f"statement number {i} is here", "B": "x"}) for i in range(mix.SHARED_AT)]
    guard = mix.LeakGuard(panel)
    assert not guard.leaks(choice("t1", "Which statement is sarcastic?", {"A": "a new statement that nobody wrote", "B": "y"}))
    assert guard.leaks(choice("t2", "Which statement is sarcastic?", {"A": "Statement number 3 is here!", "B": "y"}))  # a copied candidate


def test_short_option_labels_are_not_matched() -> None:
    guard = mix.LeakGuard([choice("p1", "Is the sky green?", {"A": "Yes", "B": "No"})])
    assert not guard.leaks(choice("t1", "Is grass blue?", {"A": "Yes", "B": "No"}))


def test_the_same_upstream_record_is_a_leak() -> None:
    def sourced(identifier: str, state: str, source: dict) -> dict:
        return {**row(identifier, state), "source": source}
    guard = mix.LeakGuard([sourced("c1", "clause text of the check set", {"dataset": "maud", "contract": "contract_58"}),
                           sourced("v1", "turn it up", {"dataset": "voice_navigation", "massive_id": "17"})])
    assert guard.leaks(sourced("t1", "another clause of the same merger agreement", {"dataset": "maud", "contract": "contract_58"}))
    assert guard.leaks(sourced("t2", "pump up the volume", {"dataset": "massive", "upstream_id": "massive-17"}))
    assert not guard.leaks(sourced("t3", "another clause", {"dataset": "maud", "contract": "contract_59"}))
    assert not guard.leaks(sourced("t4", "turn it down", {"dataset": "cuad", "contract": "contract_58"}))


def test_every_evaluation_set_is_protected_by_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    captured: dict[str, list[dict]] = {}

    def fake_build_public(public, dev, calibration, panel, size, sweep_size, seed):  # type: ignore[no-untyped-def]
        captured["panel"] = panel
        raise SystemExit(0)

    files = {name: tmp_path / "data" / name for name in ("panel.jsonl", "jevbench-hard.jsonl", "documents/check.jsonl", "voice/test.jsonl", "longlists-test/test.jsonl")}
    for name, path in files.items():
        write_jsonl(path, [row(name, f"item of {name}")])
    write_jsonl(tmp_path / "public.jsonl", [row("pub0", "public text")])
    write_jsonl(tmp_path / "dev.jsonl", [row("dev0", "dev text")])
    write_jsonl(tmp_path / "cal.jsonl", [row("cal0", "cal text")])
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mix, "build_public", fake_build_public)
    monkeypatch.setattr(sys, "argv", ["jeff-mix", "--public", "public.jsonl", "--dev", "dev.jsonl", "--calibration", "cal.jsonl",
                                      "--panel", "data/panel.jsonl", "--public-only", "--out", "mix"])
    with pytest.raises(SystemExit):
        mix.main()
    assert sorted(r["id"] for r in captured["panel"]) == sorted(files)


def test_an_option_text_many_items_share_is_a_label_not_a_leak() -> None:
    labels = {"a": "Not a command: the user is asking a question", "b": "Play music"}
    guard = mix.LeakGuard([choice("v1", "what is the capital of peru", labels), choice("v2", "who wrote hamlet", labels)])
    assert not guard.leaks(choice("t1", "how tall is everest", labels))
