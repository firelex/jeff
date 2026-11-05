import json
from pathlib import Path

import pytest

from jeff.catalogue import Catalogue
from jeff.cli import main
from jeff.client import Call, Jeff
from jeff.errors import JeffError, ManifestError
from jeff.evaluate import Report, engine_score, mine_rows, predicted, resolve, score, template_rows, wilson
from jeff.testing import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
URGENCY = ["No action needed", "Can wait a few days", "Handle within a day", "Handle within hours", "Handle immediately"]


def test_wilson_interval() -> None:
    low, high = wilson(90, 100)
    assert 0.82 < low < 0.83 and 0.94 < high < 0.95


def write_cases(path: Path, expected: int) -> None:
    lines = [{"id": f"c{i}", "function": "triage.urgency", "input": "x", "context": CONTEXT, "expected": expected}
             for i in range(3)]
    path.write_text("\n".join(json.dumps(line) for line in lines))


def jeff() -> Jeff:
    return Jeff(FakeEngine(lambda a, r: [0.0, 0.0, 0.9, 0.1, 0.0], {"jeff.support.triage"}), Catalogue.from_dir(FIXTURES))


def test_score_reports_accuracy_and_abstentions(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    write_cases(cases, 2)
    [report] = score(jeff(), mine_rows(cases))
    assert isinstance(report, Report)
    assert (report.n, report.correct, report.abstained) == (3, 3, 0)
    assert report.accuracy == 1.0


def test_cli_mine_fails_on_wrong_cases_and_detects_regressions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import jeff.cli as cli
    monkeypatch.setattr(cli, "make_jeff", lambda adapters, backend: jeff())
    good, bad = tmp_path / "good.jsonl", tmp_path / "bad.jsonl"
    write_cases(good, 2)
    write_cases(bad, 4)
    baseline = tmp_path / "baseline.json"
    assert main(["test", "triage", "--mine", str(good), "--save-baseline", str(baseline)]) == 0
    assert json.loads(baseline.read_text())["versions"] == {"jeff.support.triage": "1.3.0"}
    assert main(["test", "triage", "--mine", str(bad)]) == 1
    assert main(["test", "triage", "--mine", str(bad), "--against", str(baseline)]) == 1


def test_against_fails_only_on_regressions_not_on_cases_that_already_failed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli
    monkeypatch.setattr(cli, "make_jeff", lambda adapters, backend: jeff())
    bad = tmp_path / "bad.jsonl"
    write_cases(bad, 4)  # every case fails
    baseline = tmp_path / "baseline.json"
    assert main(["test", "triage", "--mine", str(bad), "--save-baseline", str(baseline)]) == 1
    assert main(["test", "triage", "--mine", str(bad)]) == 1
    assert main(["test", "triage", "--mine", str(bad), "--against", str(baseline)]) == 0
    assert "already failed in the baseline" in capsys.readouterr().out


def test_mine_rows_name_the_missing_field(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"id": "c1", "function": "triage.urgency", "input": "x"}))
    with pytest.raises(JeffError, match="missing expected"):
        mine_rows(cases)


def test_a_bool_expected_is_not_matched_by_an_int_value(tmp_path: Path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"id": "c1", "function": "triage.urgency", "input": "x", "context": CONTEXT,
                                 "expected": True}))
    engine = FakeEngine(lambda a, r: [0.0, 0.9, 0.1, 0.0, 0.0], {"jeff.support.triage"})  # value 1, which == True
    [report] = score(Jeff(engine, Catalogue.from_dir(FIXTURES)), mine_rows(cases))
    assert report.correct == 0


# Test rows, as the published test sets write them.
def row(id: str, state: dict, question: dict, label: object) -> dict:
    return {"id": id, "suite": "s", "state": state, "question": question, "label": label}


ROUTE = row("t1-route", {**CONTEXT, "message": "Broken"},
            {"type": "choice", "instructions": "Reworded",
             "criteria": {"other": "None", "k1": "Deliveries", "k2": "Refunds"}}, "k1")
NEEDS_HUMAN = row("t1-needs_human", {**CONTEXT, "message": "Broken"}, {"type": "noul", "instructions": "x"}, True)
URGENT = row("t1-urgency", {**CONTEXT, "message": "Broken"}, {"type": "score", "criteria": URGENCY}, 3)


def manifest():
    return Catalogue.from_dir(FIXTURES).adapter("triage")


def test_template_rows_rebuild_calls_through_the_manifest() -> None:
    rows, skipped = template_rows(manifest(), [ROUTE, NEEDS_HUMAN, URGENT])
    assert skipped == {}
    route, needs_human, urgency = rows
    assert route.function == "jeff.support.triage.route"
    assert route.call == Call("jeff.support.triage.route", "Broken", CONTEXT, {"k1": "Deliveries", "k2": "Refunds"})
    assert route.expected == "k1"
    assert needs_human.call.labels is None and needs_human.expected is True
    assert urgency.call.labels is None and urgency.expected == 3


def test_template_rows_skip_and_count_rows_the_manifest_does_not_describe() -> None:
    renamed = row("t2-route", {"organisation": "A shop", "channel": "email", "message": "Hi"}, ROUTE["question"], "k1")
    no_input = row("t3-route", {**CONTEXT, "question": "Is it?"}, ROUTE["question"], "k1")
    no_other = row("t5-route", {**CONTEXT, "message": "Hi"},
                   {"type": "choice", "criteria": {"i1": "Deliveries", "i2": "Refunds"}}, "i1")
    wrong_levels = row("t6-urgency", {**CONTEXT, "message": "Hi"}, {"type": "score", "criteria": URGENCY[:3]}, 1)
    rows, skipped = template_rows(manifest(), [ROUTE, renamed, no_input, no_other, wrong_levels])
    assert [r.id for r in rows] == ["t1-route"]
    assert sum(skipped.values()) == 4
    assert set(skipped) == {"state fields differ from the manifest", "no input field", "fixed_first options missing",
                            "score levels differ"}


def test_template_rows_raise_on_a_row_that_maps_to_no_function() -> None:
    """A row of no function is never skipped: it raises ManifestError naming the row, as the importers do."""
    no_function = row("t4-pattern", {**CONTEXT, "message": "Hi"}, ROUTE["question"], "k1")
    with pytest.raises(ManifestError, match="t4-pattern"):
        template_rows(manifest(), [ROUTE, no_function])


def subset_and_describe_manifest():
    from jeff.manifest import manifest_from_dict
    raw = manifest().raw | {"id": "jeff.test.forms", "aliases": ["forms"], "tests": {
        "published": None, "calibration": None, "function_by": "id_suffix"}}
    raw["functions"] = {
        "action": {"interface": "classify", "type": "choice", "instructions": "What to do?", "abstain_labels": [],
                   "calibration": None, "labels": {"source": "subset", "fixed": {"ignore": "Ignore", "block": "Block",
                                                                                  "escalate": "Escalate"},
                                                   "required": ["ignore"]}},
        "venue": {"interface": "classify", "type": "choice", "instructions": "Which venue?", "abstain_labels": [],
                  "calibration": None, "labels": {"source": "describe", "keys": ["venue_a", "venue_b"]}},
        "kind": {"interface": "classify", "type": "choice", "instructions": "Which kind?", "abstain_labels": [],
                 "calibration": None, "labels": {"source": "fixed", "fixed": {"a": "A", "b": "B"}, "subset": False}}}
    return manifest_from_dict(raw, Path("forms.yaml"))


def test_template_rows_for_subset_describe_and_fixed_labels() -> None:
    state = {**CONTEXT, "message": "Hi"}
    rows, skipped = template_rows(subset_and_describe_manifest(), [
        row("r-action", state, {"type": "choice", "criteria": {"block": "Block", "ignore": "Ignore"}}, "block"),
        row("r-venue", state, {"type": "choice", "criteria": {"venue_a": "LSE", "venue_b": "Xetra"}}, "venue_b"),
        row("r-kind", state, {"type": "choice", "criteria": {"b": "B", "a": "A"}}, "a"),
        row("s-kind", state, {"type": "choice", "criteria": {"a": "A", "c": "C"}}, "a")])
    assert [r.call.labels for r in rows] == [["block", "ignore"], {"venue_a": "LSE", "venue_b": "Xetra"}, None]
    assert skipped == {"fixed options differ from the manifest": 1}


def test_predicted_takes_the_argmax_in_each_question_form() -> None:
    assert predicted({"type": "choice", "criteria": {"a": None, "b": None, "c": None}}, [0.2, 0.5, 0.3]) == "b"
    assert predicted({"type": "noul"}, [0.5, 0.5]) is True
    assert predicted({"type": "noul"}, [0.6, 0.4]) is False
    assert predicted({"type": "score", "criteria": URGENCY}, [0.1, 0.1, 0.1, 0.6, 0.1]) == 3


def test_engine_score_sends_each_row_unchanged() -> None:
    engine = FakeEngine(lambda a, r: [0.1, 0.8, 0.1] if r["question"]["type"] == "choice" else [0.2, 0.8],
                        {"jeff.support.triage"})
    report = engine_score(engine, "jeff.support.triage", [ROUTE, NEEDS_HUMAN, {**NEEDS_HUMAN, "label": False}], batch=2)
    assert (report.function, report.n, report.correct) == ("jeff.support.triage", 3, 2)
    assert [len(rows) for _, rows in engine.calls] == [2, 1]
    assert engine.calls[0][1][0] == {"state": ROUTE["state"], "question": ROUTE["question"]}


def test_resolve_reads_model_and_dataset_repos(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import huggingface_hub
    seen = []

    def download(repo_id: str, filename: str, *, revision: str, repo_type: str | None) -> str:
        seen.append((repo_id, filename, revision, repo_type))
        (tmp_path / "test.jsonl").write_text("")
        return str(tmp_path / "test.jsonl")

    monkeypatch.setattr(huggingface_hub, "hf_hub_download", download)
    resolve("hf://mstrasser/jeff-adapter-triage@v1.3/test.jsonl")
    resolve("hf://datasets/teamsuzie/jeff-data@main/v1.3/adapters/aml/test.jsonl")
    assert seen == [("mstrasser/jeff-adapter-triage", "test.jsonl", "v1.3", None),
                    ("teamsuzie/jeff-data", "v1.3/adapters/aml/test.jsonl", "main", "dataset")]
    with pytest.raises(JeffError, match="does not exist"):
        resolve(str(tmp_path / "missing.jsonl"))


# The published-set paths of `jeff test`, with no weights: a fake engine and a fixed set of rows.
def answers(adapter: str, request: dict) -> list[float]:
    question = request["question"]
    if question["type"] == "noul":
        return [0.2, 0.8]
    if question["type"] == "score":
        return [0.0, 0.0, 0.0, 1.0, 0.0]
    return [0.1, 0.8] + [0.1 / (len(question["criteria"]) - 2)] * (len(question["criteria"]) - 2)


@pytest.fixture
def published_cli(monkeypatch: pytest.MonkeyPatch):
    import jeff.cli as cli
    from jeff.evaluate import PublishedSet

    engine = FakeEngine(answers, {"jeff.support.triage"})
    monkeypatch.setattr(cli, "make_jeff", lambda adapters, backend: Jeff(engine, Catalogue.from_dir(FIXTURES)))
    other_shape = row("t9-route", {**CONTEXT, "question": "Is it?"}, ROUTE["question"], "k1")

    def fixed_set(manifest, which, sample=None, seed=0):
        raws = [ROUTE, NEEDS_HUMAN, URGENT, other_shape]
        rows, skipped = template_rows(manifest, raws)
        return PublishedSet(raws, rows, skipped)

    monkeypatch.setattr(cli, "published_set", fixed_set)
    return engine


def test_cli_published_sets_print_one_line_per_function_and_the_skipped_rows(
        published_cli: FakeEngine, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["test", "triage"]) == 0
    out, err = capsys.readouterr()
    lines = out.strip().splitlines()
    assert [line.split(":")[0] for line in lines] == ["jeff.support.triage.route", "jeff.support.triage.needs_human",
                                                      "jeff.support.triage.urgency"]
    assert all("100.0% (1/1" in line for line in lines)
    assert "skipped 1 rows the manifest does not describe (1 no input field)" in err
    assert published_cli.calls[0][1][0]["question"]["instructions"] != "Reworded"  # rebuilt through the manifest


def test_cli_published_sets_can_name_one_function(published_cli: FakeEngine, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["test", "triage.route"]) == 0
    assert [line.split(":")[0] for line in capsys.readouterr().out.strip().splitlines()] == ["jeff.support.triage.route"]


def test_cli_engine_rows_send_the_rows_as_written(published_cli: FakeEngine, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["test", "triage", "--engine-rows"]) == 0
    [line] = capsys.readouterr().out.strip().splitlines()
    assert line.startswith("jeff.support.triage: 100.0% (4/4")
    assert published_cli.calls[0][1][0] == {"state": ROUTE["state"], "question": ROUTE["question"]}


def test_cli_engine_rows_refuse_a_function_and_mine(published_cli: FakeEngine, tmp_path: Path,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["test", "triage.route", "--engine-rows"]) == 1
    assert "--engine-rows scores whole adapters" in capsys.readouterr().err
    with pytest.raises(SystemExit):
        main(["test", "triage", "--engine-rows", "--mine", str(tmp_path / "cases.jsonl")])
    assert "--engine-rows scores published test sets; it does not take --mine" in capsys.readouterr().err


def test_cli_prints_jeff_errors_without_a_traceback(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["gen", "nosuch", "--out", "unused.py"]) == 1
    captured = capsys.readouterr()
    assert captured.err.startswith("jeff: ") and "nosuch" in captured.err and "Traceback" not in captured.err


def test_cli_refuses_sample_with_mine(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        main(["test", "triage", "--sample", "10", "--mine", str(tmp_path / "cases.jsonl")])
    assert "--sample samples published test sets; it does not take --mine" in capsys.readouterr().err


def test_cli_against_names_baseline_cases_missing_from_mine(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                            capsys: pytest.CaptureFixture[str]) -> None:
    import jeff.cli as cli
    monkeypatch.setattr(cli, "make_jeff", lambda adapters, backend: jeff())
    cases = tmp_path / "cases.jsonl"
    write_cases(cases, 2)
    baseline = tmp_path / "baseline.json"
    baseline.write_text(json.dumps({"versions": {"jeff.support.triage": "1.3.0"},
                                    "cases": {"c0": True, "c1": True, "c2": True, "gone": True}}))
    assert main(["test", "triage", "--mine", str(cases), "--against", str(baseline)]) == 1
    out, err = capsys.readouterr()
    assert f"The baseline {baseline} has cases that {cases} does not: gone" in err
    assert "Regressions" not in out


def test_jsonl_rows_with_unicode_line_separators_inside_strings_are_read_whole(tmp_path: Path) -> None:
    """A JSON string may hold U+2028 or U+2029 (a CI log does); only "\\n" ends a JSON line, so str.splitlines(),
    which also splits on those, must not be used to read rows."""
    import jeff.cli
    from jeff.evaluate import read_rows

    path = tmp_path / "rows.jsonl"
    rows = [{"id": "a", "text": "one two three\x1cfour"}, {"id": "b", "text": "plain"}]
    path.write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
    assert jeff.cli._jsonl(path) == rows

    class Manifest:
        id = "x"
        tests = {"published": str(path), "internal": None}

    assert read_rows(Manifest(), "test") == rows  # type: ignore[arg-type]


def test_template_rows_offer_a_fixed_subset_when_the_function_allows_one() -> None:
    from jeff.manifest import manifest_from_dict
    raw = subset_and_describe_manifest().raw
    raw["functions"]["kind"]["labels"] = {"source": "fixed", "fixed": {"a": "A", "b": "B", "c": "C"}, "subset": True}
    forms = manifest_from_dict(raw, Path("forms.yaml"))
    state = {**CONTEXT, "message": "Hi"}
    rows, skipped = template_rows(forms, [
        row("r-kind", state, {"type": "choice", "criteria": {"c": "C", "a": "A"}}, "a"),
        row("t-kind", state, {"type": "choice", "criteria": {"b": "B", "c": "C", "a": "A"}}, "a"),
        row("s-kind", state, {"type": "choice", "criteria": {"a": "A", "d": "D"}}, "a")])
    assert [r.call.labels for r in rows] == [["a", "c"], None]
    assert skipped == {"fixed options differ from the manifest": 1}


def test_template_rows_skip_fixed_options_whose_texts_differ_from_the_manifest() -> None:
    from jeff.manifest import manifest_from_dict
    raw = subset_and_describe_manifest().raw
    raw["functions"]["kind"]["labels"] = {"source": "fixed", "fixed": {"a": "A", "b": "B", "c": "C"}, "subset": True}
    forms = manifest_from_dict(raw, Path("forms.yaml"))
    state = {**CONTEXT, "message": "Hi"}
    rows, skipped = template_rows(forms, [
        row("r-kind", state, {"type": "choice", "criteria": {"c": "C", "a": "Another text"}}, "a"),
        row("t-kind", state, {"type": "choice", "criteria": {"b": "B", "c": "C", "a": "Other"}}, "a"),
        row("u-kind", state, {"type": "choice", "criteria": {"b": "B", "a": "A"}}, "a")])
    assert [r.id for r in rows] == ["u-kind"]
    assert skipped == {"fixed option texts differ from the manifest": 2}
