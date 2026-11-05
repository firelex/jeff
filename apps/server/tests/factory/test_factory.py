"""jeff-server's 22 factory tests (tests/test_factory.py there), ported: the spec checks, data generation, the
data checks, imports, test sets, replays, the teacher's reply checks, starting runs, and the build endpoints with
the factory off."""

import json
import os
import sys
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from jeff.errors import Conflict
from jeff.testing import FakeEngine

from jeff_server.app import create_app
from jeff_server.factory import checks, generate, replay, spec as spec_module, training
from jeff_server.factory.config import FactoryConfig
from jeff_server.factory.publish import example_request
from jeff_server.factory.runner import parse_test_set
from jeff_server.factory.store import Run, new_run, write_json, write_jsonl
from jeff_server.settings import ServerSettings

from factory_testing import FakeTeacher, build, trading_spec


def test_a_good_spec_passes_and_bad_ones_name_the_problem() -> None:
    spec_module.check_spec(trading_spec())
    for change, message in [
        (lambda s: s.update(name="Trading Calls"), "name"),
        (lambda s: s.update(name="trading-calls\n"), "lower case words joined by hyphens"),
        (lambda s: s.update(name="jeff"), "kept for the base model"),
        (lambda s: s.update(name="jeff-latest"), "kept for the base model"),
        (lambda s: s["live_field"].update(name="request\n"), "lower case letters"),
        (lambda s: s["questions"][0].update(id="function\n"), "Question id"),
        (lambda s: s["questions"][0]["options"][0].update(key="place_order\n"), "never a bare number"),
        (lambda s: s["questions"][0].update(applies_when="sometimes"), "must apply to every request"),
        (lambda s: s["questions"][0]["options"].append({"key": "42", "description": "x"}), "never a bare number"),
        (lambda s: s["questions"][2]["options"].pop(), "the options true and false"),
        (lambda s: s["sizes"].update(train=600), "whole number of passes"),
        (lambda s: s["sizes"].update(updates=10), "between 50 and 5,000"),
        (lambda s: s["live_field"].update(name="desk"), "different"),
    ]:
        spec = trading_spec()
        change(spec)
        with pytest.raises(ValueError, match=message):
            spec_module.check_spec(spec)


def test_labels_follow_the_row_format() -> None:
    spec = trading_spec()
    assert spec_module.label_of(spec["questions"][0], "none") == "none"
    assert spec_module.label_of(spec["questions"][2], "true") is True
    score = {"type": "score", "options": [{"key": "low", "description": ""}, {"key": "high", "description": ""}]}
    assert spec_module.label_of(score, "high") == 1


def test_generation_balances_holds_out_families_and_records_provenance() -> None:
    spec = trading_spec()
    teacher = FakeTeacher()
    rows, plan, failures, lines = build(spec, teacher)
    assert not failures and lines[-1]["phase"] == "rows"
    assert all(set(plan[a]).isdisjoint(plan[b]) for a in plan for b in plan if a < b) and len(plan["test"]) == 2
    assert all(row["family"] in plan["test"] for row in rows if row["split"] == "test")
    for row in rows:
        assert list(row["state"])[-1] == "request"  # the changing field last
        assert row["source"]["teacher"] == "fake-teacher-1"
        assert row["source"]["question_id"] in {"function", "side", "urgent"}
    sides = [row for row in rows if row["source"]["question_id"] == "side"]
    assert sides and all(row["state"]["request"].endswith("place_order") for row in sides)  # conditional question
    shares = {kind: sum(row["source"]["instruction"] == kind for row in rows) / len(rows) for kind in ("canonical", "variant")}
    assert 0.6 < shares["canonical"] < 0.8 and 0.17 < shares["variant"] < 0.37
    targets = [row["label"] for row in rows if row["source"]["question_id"] == "function" and row["split"] == "train"]
    counts = [targets.count(key) for key in set(targets)]
    assert max(counts) - min(counts) <= generate.REQUESTS_PER_CALL  # balanced to within one call


def test_invalid_teacher_calls_are_recorded_and_made_up_in_the_next_wave() -> None:
    spec = trading_spec()
    teacher = FakeTeacher()
    teacher.bad_requests = 1
    rows, _, failures, _ = build(spec, teacher)
    assert len(failures) == 1 and "bad reply" in failures[0]["error"]
    files, _ = checks.run_checks(spec, rows, 1, lambda row: 100)
    assert len(files["train"]) == 100


def test_checks_trim_to_exact_sizes_and_drop_copies() -> None:
    spec = trading_spec()
    rows, _, _, _ = build(spec, FakeTeacher())
    copy = next(row for row in rows if row["split"] == "test")
    train = next(row for row in rows if row["split"] == "train")
    for row in rows:  # make one test request an exact copy of a training request
        if row["split"] == "test" and row["source"]["scenario"] == copy["source"]["scenario"]:
            row["state"] = {**row["state"], "request": train["state"]["request"].upper() + "!"}
    files, report = checks.run_checks(spec, rows, 1, lambda row: len(json.dumps(row)) // 4)
    assert {split: len(files[split]) for split in files} == {"train": 100, "dev": 50, "calibration": 50, "test": 50}
    assert report["duplicates_dropped"] == {"test": 1}
    assert report["test_family_overlap"] == 0 and len(report["sample"]) == 20
    assert all("split" not in row for split in files.values() for row in split)


def test_too_few_rows_is_an_error_not_a_smaller_file() -> None:
    spec = trading_spec()
    rows, _, _, _ = build(spec, FakeTeacher())
    spec["sizes"]["test"] = 2000
    with pytest.raises(ValueError, match="test: only"):
        checks.run_checks(spec, rows, 1, lambda row: 1)


def test_the_example_request_and_independent_test_use_the_spec() -> None:
    spec = trading_spec()
    rows, _, _, _ = build(spec, FakeTeacher())
    example = example_request(spec, [row for row in rows if row["split"] == "test"])
    assert example["model"] == "trading-calls" and set(example["questions"]) <= {"function", "side", "urgent"}
    csv_text = "question_id,label,desk,asset_classes,request\nfunction,place_order,A,equity,buy 10 apple\nurgent,true,A,equity,now!\n"
    parsed = parse_test_set(spec, csv_text, "csv")
    assert [row["label"] for row in parsed] == ["place_order", True]
    with pytest.raises(ValueError, match="not an option"):
        parse_test_set(spec, "question_id,label,desk,asset_classes,request\nfunction,buy,A,equity,x\n", "csv")


def test_only_clean_finished_runs_become_replays(tmp_path: Path) -> None:
    run = new_run(tmp_path / "runs", "demo", "describe", None)
    for name in ("spec", "checks", "metrics", "train", "publish"):
        (run.folder / f"{name}.json").write_text(json.dumps(trading_spec() if name == "spec" else {"synthetic": {"accuracy": 0.9}}))
    run.event("run.start")
    with pytest.raises(ValueError, match="has not finished"):
        replay.save(run, tmp_path / "replays")
    run.event("run.end")
    run.update(clean=False, unclean_reasons=["generate was run again"])
    with pytest.raises(ValueError, match="generate was run again"):
        replay.save(run, tmp_path / "replays")
    run.update(clean=True, unclean_reasons=[])
    (tmp_path / "replays").mkdir()
    saved = replay.save(Run(run.folder), tmp_path / "replays")
    assert saved["name"] == "trading-calls"
    detail = replay.detail(tmp_path / "replays", run.id)
    assert [event["kind"] for event in detail["events"]] == ["run.start", "run.end"]


def test_build_endpoints_answer_403_when_the_factory_is_off(tmp_path: Path) -> None:
    settings = ServerSettings(engine_sockets=(tmp_path / "e.sock",), model_name="jeff-v1.3", api_key="the-key",
                              factory=FactoryConfig(mode="off", root=tmp_path))
    with TestClient(create_app(settings, engine=FakeEngine(lambda adapter, row: [1.0], set()))) as client:
        client.headers["Authorization"] = "Bearer the-key"
        assert client.get("/v1/factory/mode").json() == {"mode": "off", "replays": True, "description_check": False, "preview": False}
        assert client.post("/v1/factory/description-checks", json={"description": "d"}).status_code == 403
        for method, path in [("post", "/v1/factory/runs"), ("get", "/v1/factory/runs"), ("get", "/v1/factory/runs/x"),
                             ("post", "/v1/factory/runs/x/approve"), ("post", "/v1/factory/runs/x/run-rest")]:
            response = getattr(client, method)(path, **({"json": {"description": "d"}} if method == "post" else {}))
            assert response.status_code == 403, path
            assert response.json()["error"]["type"] == "FactoryOff", path
        assert client.get("/v1/factory/replays").json() == {"replays": []}


def factory_with_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Any, str]:
    from jeff_server.factory.runner import Factory
    from jeff_server.factory.store import write_json

    config = FactoryConfig(mode="owner", root=tmp_path, teacher_url="u", teacher_key="k", teacher_model="m")
    factory = Factory(config, None)
    monkeypatch.setattr(factory, "launch", lambda run, steps: None)
    write_json(config.folder("description-checks") / "check-1.json", {
        "id": "check-1", "started": "2026-10-17T10:00:00.000+00:00", "ended": "2026-10-17T10:00:09.000+00:00",
        "description": "Route emails.", "examples": None, "teacher_model": "glm", "teacher": {"calls": 1},
        "result": {"ready": False, "summary": "s", "problems": [{"severity": "important", "text": "no none option"}],
                   "questions": ["What if nothing fits?"], "improved_description": "Route emails; else none (assumed).",
                   "changes": ["added none"]}, "used_by": None})
    return factory, "check-1"


def test_a_run_starts_only_from_a_confirmed_check_and_records_both_times(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    factory, check_id = factory_with_check(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match="not the improved one"):
        factory.start(check_id, "improved", "something else", None)
    with pytest.raises(ValueError, match="No description check"):
        factory.start("check-missing", "original", "Route emails.", None)
    run = factory.start(check_id, "improved", "Route emails; else none (assumed).", "email-routing")
    kinds = [(event["kind"], event["t"][:19]) for event in run.events()]
    assert kinds[:3] == [("description_check.start", "2026-10-17T10:00:00"), ("description_check.end", "2026-10-17T10:00:09"),
                         ("confirm.start", "2026-10-17T10:00:09")]
    assert [kind for kind, _ in kinds[3:]] == ["confirm.end", "run.start"]
    assert run.state["description_check"]["choice"] == "improved" and run.state["description"].endswith("(assumed).")
    with pytest.raises(Conflict, match="already started run"):
        factory.start(check_id, "original", "Route emails.", None)


def test_every_run_object_for_a_folder_shares_one_lock(tmp_path: Path) -> None:
    run = new_run(tmp_path, "demo", "d", None)
    assert Run(run.folder).lock is Run(run.folder).lock is run.lock


def test_teacher_replies_are_checked_against_their_schema() -> None:
    from jeff_server.factory.teacher import conforms
    schema = generate.request_schema(trading_spec()["questions"])
    good = {"requests": [{"text": "buy", "answers": {"function": "place_order", "side": "buy", "urgent": None}}]}
    conforms(good, schema, "reply")
    for bad, message in [({"requests": [{"text": "x", "answers": {"function": "sell_all", "side": None, "urgent": None}}]}, "not one of"),
                         ({"requests": [{"text": "x"}]}, "missing answers"), ({"requests": "none"}, "expected array"),
                         ({"requests": [], "extra": 1}, "unexpected extra")]:
        with pytest.raises(ValueError, match=message):
            conforms(bad, schema, "reply")


def test_a_call_is_right_only_when_every_question_about_it_is() -> None:
    from jeff_server.factory.evaluation import whole_calls
    rows = [{"source": {"scenario": 1}}, {"source": {"scenario": 1}}, {"source": {"scenario": 2}}]
    predictions = [{"correct": True, "confidence": 0.9}, {"correct": False, "confidence": 0.6}, {"correct": True, "confidence": 0.95}]
    result = whole_calls(rows, predictions)
    assert result["calls"] == 2 and result["exact_match"] == 0.5 and result["questions_per_call"] == 1.5
    assert whole_calls([{"source": {}}], [{"correct": True, "confidence": 1.0}]) is None


def test_conditions_are_checked_and_imported_questions_keep_their_shape() -> None:
    spec = trading_spec()
    spec["questions"][1]["requires"] = [{"question": "function", "equals": "place_order"}]
    spec_module.check_spec(spec)
    assert spec_module.applies(spec["questions"][1], {"function": "place_order"})
    assert not spec_module.applies(spec["questions"][1], {"function": "none"})
    spec["questions"][1]["requires"] = [{"question": "function", "equals": "sell_everything"}]
    with pytest.raises(ValueError, match="not one of its answers"):
        spec_module.check_spec(spec)
    flag = {"id": "plot_price.include_volume", "type": "noul", "instruction": "Volume?", "options": [], "applies_when": None, "imported": True}
    member = {"id": "compare_returns.symbols.NVDA", "type": "noul", "instruction": "NVDA?", "options": [{"key": "true", "description": "Nvidia"}],
              "applies_when": None, "imported": True}
    average = {"id": "plot_price.moving_average", "type": "choice", "instruction": "MA?", "imported": True, "applies_when": None,
               "options": [{"key": "9", "description": "fast"}, {"key": "20", "description": "mid"}]}
    for question in (flag, member, average):
        spec_module.check_question(question, first=False)
    assert spec_module.question_payload(flag) == {"type": "noul", "instructions": "Volume?"}
    assert spec_module.question_payload(member) == {"type": "noul", "instructions": "NVDA?", "criteria": {"true": "Nvidia"}}
    with pytest.raises(ValueError, match="bare number"):
        spec_module.check_question({**average, "imported": False}, first=False)


COOKBOOK = Path(__file__).parent / "fixtures/typesafe-function-calling-derived"  # derived from TypeSafe's MIT-licensed cookbook


def test_the_cookbook_imports_verbatim_with_its_reading_order() -> None:
    from jeff_server.factory import importing
    bundle = importing.load_bundle(COOKBOOK)
    questions = importing.to_spec_questions(bundle["questions"], bundle["conditions"])
    assert len(questions) == 54 and questions[0]["id"] == "__tool__"
    order = [q["id"] for q in questions]
    assert order.index("plot_price.style?") < order.index("plot_price.style")
    assert {q["id"]: spec_module.question_payload(q) for q in questions} == bundle["questions"]
    spec = {**trading_spec(), "family": {"noun": "trader", "description": "d", "fields": []}, "questions": questions}
    spec_module.check_spec(spec)


def test_a_correct_call_becomes_the_answers_dispatch_reads() -> None:
    from jeff_server.factory import importing
    signatures = importing.load_bundle(COOKBOOK)["signatures"]
    answers = importing.call_answers({"function": "plot_price", "arguments": {"symbol": "TSLA", "style": "candles", "moving_average": "20"}}, signatures)
    assert answers["__tool__"] == "plot_price" and answers["plot_price.symbol"] == "TSLA"
    assert answers["plot_price.style?"] == "true" and answers["plot_price.style"] == "candles"
    assert answers["plot_price.resolution?"] == "false" and "plot_price.resolution" not in answers
    compare = importing.call_answers({"function": "compare_returns", "arguments": {"symbols": ["NVDA", "AMD", "MSFT"], "window": "3mo"}}, signatures)
    assert [compare[f"compare_returns.symbols.{m}"] for m in ("SPY", "NVDA", "AMD")] == ["false", "true", "true"]
    movers = importing.call_answers({"function": "top_movers", "arguments": {"window": "1d"}}, signatures)
    assert movers["top_movers.direction"] == "gainers"  # no "stated" question: the default is the answer
    for call, message in [({"function": "plot_price", "arguments": {"symbol": "IBM"}}, "not one of"),
                          ({"function": "plot_price", "arguments": {}}, "needs symbol"),
                          ({"function": "top_movers", "arguments": {"limit": 5}}, "no closed-set argument")]:
        with pytest.raises(ValueError, match=message):
            importing.call_answers(call, signatures)


def test_an_import_run_keeps_the_questions_and_reads_a_test_set_of_calls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    from jeff_server.factory import importing
    from jeff_server.factory.runner import parse_calls
    factory, _ = factory_with_check(tmp_path, monkeypatch)
    shutil.copytree(COOKBOOK, factory.config.folder("references") / "typesafe-function-calling")
    assert factory.references()[0]["questions"] == 54
    with pytest.raises(ValueError, match="No reference"):
        factory.start_import("missing", "trading-function-calls", "d")
    run = factory.start_import("typesafe-function-calling", "trading-function-calls", "Traders type short requests.")
    assert run.state["mode"] == "import" and (run.folder / "reference" / "questions.json").exists()
    bundle = importing.load_bundle(run.folder / "reference")
    spec = {**trading_spec(), "family": {"noun": "trader", "description": "d", "fields": []},
            "live_field": {"name": "command", "description": "d"},
            "questions": importing.to_spec_questions(bundle["questions"], bundle["conditions"])}
    lines = "\n".join(json.dumps(line) for line in [
        {"id": "c1", "command": "show nvda 1h", "function": "plot_price", "arguments": {"symbol": "NVDA", "resolution": "1h"}},
        {"id": "c2", "command": "what tickers do you have", "function": "list_symbols", "arguments": {}}])
    rows = parse_calls(spec, lines, bundle["signatures"])
    first = [row for row in rows if row["source"]["call_id"] == "c1"]
    assert len(first) == 9  # tool, symbol, six "stated" questions, and resolution (the only one stated)
    assert all(row["state"] == "show nvda 1h" for row in first)
    with pytest.raises(ValueError, match="1 calls do not fit"):
        parse_calls(spec, json.dumps({"command": "x", "function": "plot_price", "arguments": {"symbol": "IBM"}}), bundle["signatures"])


def test_a_test_set_freezes_only_after_the_owners_checks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jeff_server.factory import importing, testset
    signatures = importing.load_bundle(COOKBOOK)["signatures"]
    folder = tmp_path / "set"
    folder.mkdir()
    call = {"function": "volatility", "arguments": {"symbol": "TSLA"}}
    items = [{"id": f"t{i:04d}", "command": f"how volatile is tsla {i}", "writer_call": call, "status": "agreed"} for i in range(110)]
    items.append({"id": "t9999", "command": "tsla risk", "writer_call": call, "judge_call": {"function": "drawdown", "arguments": {"symbol": "TSLA"}}, "status": "disputed"})
    write_jsonl(folder / "candidates.jsonl", items)
    write_json(folder / "summary.json", {"agreed": 110, "disputed": 1})
    testset.add_cookbook(folder, [{"command": "how volatile is tsla", "function": "volatility", "arguments": {"symbol": "TSLA"}}], signatures)
    with pytest.raises(ValueError, match="need your verdict"):
        testset.freeze(folder, signatures)
    testset.review(folder, "t9999", "correct", {"function": "drawdown", "arguments": {"symbol": "TSLA"}}, signatures)
    testset.review(folder, "c01", "confirm", None, signatures)
    with pytest.raises(ValueError, match="at least 100"):
        testset.freeze(folder, signatures)
    for i in range(98):
        testset.review(folder, f"t{i:04d}", "drop" if i == 0 else "confirm", None, signatures)
    with pytest.raises(ValueError, match="not one of"):
        testset.review(folder, "t0001", "correct", {"function": "volatility", "arguments": {"symbol": "IBM"}}, signatures)
    info = testset.freeze(folder, signatures)
    assert info["items"] == 111 and info["checked_by_owner"] == 99
    frozen = [json.loads(line) for line in (folder / "frozen.jsonl").read_text().splitlines()]
    assert next(row for row in frozen if row["id"] == "t9999")["function"] == "drawdown"
    with pytest.raises(ValueError, match="frozen"):
        testset.review(folder, "t0001", "drop", None, signatures)


def test_training_never_keeps_a_copy_of_a_test_command() -> None:
    spec = trading_spec()
    rows, _, _, _ = build(spec, FakeTeacher())
    copied = next(row for row in rows if row["split"] == "train")["state"]["request"]
    spec["sizes"]["train"] = 50
    files, report = checks.run_checks(spec, rows, 1, lambda row: 1, avoid=[copied.upper()])
    assert report["independent_test_copies_dropped"]["exact"] == {"train": 1}
    assert all(row["state"]["request"] != copied for split in files.values() for row in split)


def test_a_waived_freeze_keeps_agreed_items_drops_disputes_and_says_so(tmp_path: Path) -> None:
    from jeff_server.factory import importing, testset
    signatures = importing.load_bundle(COOKBOOK)["signatures"]
    folder = tmp_path / "set"
    folder.mkdir()
    call = {"function": "volatility", "arguments": {"symbol": "TSLA"}}
    write_jsonl(folder / "candidates.jsonl", [
        {"id": "t0001", "command": "how volatile is tsla", "writer_call": call, "status": "agreed"},
        {"id": "t0002", "command": "tsla risk", "writer_call": call, "judge_call": {"function": "drawdown", "arguments": {"symbol": "TSLA"}}, "status": "disputed"}])
    write_json(folder / "summary.json", {})
    testset.add_cookbook(folder, [{"command": "how volatile is tsla?", "function": "volatility", "arguments": {"symbol": "TSLA"}}], signatures)
    info = testset.freeze(folder, signatures, waiver="Owner: assume I approved everything")
    assert info["items"] == 2 and info["checked_by_owner"] == 0 and info["disputed_dropped_unreviewed"] == 1
    assert info["owner_checks_waived"].startswith("Owner") and info["cookbook_accepted_unreviewed"] == 1



def test_a_run_can_start_from_an_earlier_approved_spec_with_new_sizes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    factory, _ = factory_with_check(tmp_path, monkeypatch)
    shutil.copytree(COOKBOOK, factory.config.folder("references") / "typesafe-function-calling")
    first = factory.start_import("typesafe-function-calling", "trading-function-calls", "Traders type short requests.")
    from jeff_server.factory import importing
    bundle = importing.load_bundle(first.folder / "reference")
    spec = {**trading_spec(), "name": "trading-function-calls", "family": {"noun": "trader", "description": "d", "fields": []},
            "live_field": {"name": "command", "description": "d"},
            "questions": importing.to_spec_questions(bundle["questions"], bundle["conditions"])}
    write_json(first.folder / "spec.json", spec)
    first.set_step("interpret", status="done")
    with pytest.raises(Exception, match="no approved spec"):
        factory.start_from_run(first.id, "trading-function-calls-1500", {**spec["sizes"], "train": 6400, "updates": 400})
    factory.approve(first)
    with pytest.raises(ValueError, match="whole number of passes"):
        factory.start_from_run(first.id, "trading-function-calls-1500", {**spec["sizes"], "train": 6400, "updates": 250})
    again = factory.start_from_run(first.id, "trading-function-calls-1500", {**spec["sizes"], "train": 6400, "updates": 400})
    assert again.state["spec_from"] == first.id and (again.folder / "reference" / "questions.json").exists()
    copied = json.loads((again.folder / "copied-spec.json").read_text())
    assert copied["questions"] == spec["questions"] and copied["sizes"]["updates"] == 400 and copied["name"].endswith("1500")
    assert not (again.folder / "data").exists() and not (again.folder / "teacher-cache").exists()


def test_an_empty_answer_is_retried_as_an_invalid_reply(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from jeff_server.factory.teacher import InvalidReply, Teacher
    replies = [{"model": "glm", "choices": [{"finish_reason": "stop", "message": {"content": "", "reasoning_content": "thinking only"}}]},
               {"model": "glm", "choices": [{"finish_reason": "stop", "message": {"content": '{"ok": true}'}}], "usage": {}}]
    teacher = Teacher("u", "k", "m", tmp_path)
    monkeypatch.setattr(teacher, "post", lambda request: replies.pop(0))
    schema = {"type": "object", "required": ["ok"], "properties": {"ok": {"type": "boolean"}}}
    assert teacher.ask("p", schema, "x") == ({"ok": True}, "glm")
    assert teacher.usage.invalid == 1 and any("empty" in f.name for f in (tmp_path / "invalid").iterdir())
    teacher = Teacher("u", "k", "m", tmp_path / "b")
    monkeypatch.setattr(teacher, "post", lambda request: {"model": "glm", "choices": [{"finish_reason": "stop", "message": {"content": ""}}]})
    with pytest.raises(InvalidReply, match="empty"):
        teacher.ask("p", schema, "x")


def test_the_console_smoke_tests_recorded_run_is_a_valid_finished_replay(tmp_path: Path) -> None:
    """The fixture factory folder the console's Playwright smoke test serves (tests/console_server.py) holds one replay
    in the format replay.save writes: a checked spec, a finished clean run, and every file the factory page reads."""
    fixture = Path(__file__).resolve().parents[1] / "fixtures" / "console-factory"
    settings = ServerSettings(engine_sockets=(tmp_path / "e.sock",), model_name="jeff-v1.3", api_key="the-key",
                              factory=FactoryConfig(mode="off", root=fixture))
    with TestClient(create_app(settings, engine=FakeEngine(lambda adapter, row: [1.0], set()))) as client:
        client.headers["Authorization"] = "Bearer the-key"
        listing = client.get("/v1/factory/replays").json()["replays"]
        assert [item["name"] for item in listing] == ["parcel-claims"]
        detail = client.get(f"/v1/factory/replays/{listing[0]['id']}").json()
    spec_module.check_spec(detail["spec"])
    assert detail["run"]["clean"] and detail["run"]["status"] == "published"
    kinds = [event["kind"] for event in detail["events"]]
    assert kinds[0] == "description_check.start" and kinds[-1] == "run.end"
    assert detail["publish"]["function"] == "jeff.factory.parcel-claims.claim"
    assert set(detail) == {"run", "spec", "events", "checks", "metrics", "train", "publish"}


FAKE_TRAINER = """\
import json, os, sys
from pathlib import Path
args = sys.argv[1:]
output = Path(args[args.index("--output") + 1])
(output.parent / "environment.json").write_text(json.dumps(dict(os.environ)))
final = output / "final"
final.mkdir(parents=True)
(final / "adapter_model.safetensors").write_bytes(b"")
(final / "decision_config.json").write_text(json.dumps({"prompt_layout": "live-last", "max_options": 26}))
with open(os.environ["JEFF_EVENTS"], "a") as events:
    events.write(json.dumps({"kind": "training_finished", "steps": 250, "complete": True,
                             "selected_temperature": 1.0, "git_commit": "abc"}) + "\\n")
"""


def test_the_trainer_gets_no_key_from_the_workers_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    trainer = tmp_path / "trainer"
    (trainer / ".venv" / "bin").mkdir(parents=True)
    script = trainer / ".venv" / "bin" / "jeff-train"
    script.write_text(f"#!{sys.executable}\n{FAKE_TRAINER}")
    script.chmod(0o755)
    for name, value in {"JEFF_API_KEY": "owner-key", "JEFF_TEACHER_KEY": "teacher-key", "HF_TOKEN": "hf-token",
                        "OPENAI_API_KEY": "other", "HF_HOME": "/models", "CUDA_VISIBLE_DEVICES": "3"}.items():
        monkeypatch.setenv(name, value)
    finished = training.train(trainer, str(tmp_path / "base"), tmp_path, tmp_path / "out", lambda *_: None,
                              lambda pid: None, 250)
    assert finished["complete"] is True
    environment = json.loads((tmp_path / "out" / "environment.json").read_text())
    # Names only, so a failure never prints a value from this machine's environment.
    names = set(environment)
    leaked = sorted({"JEFF_API_KEY", "JEFF_TEACHER_KEY", "HF_TOKEN", "OPENAI_API_KEY"} & names)
    assert leaked == []
    assert names <= {*training.TRAINER_VARIABLES, "JEFF_EVENTS", "PYTHONUNBUFFERED", "PYTHONDONTWRITEBYTECODE",
                     "__CF_USER_TEXT_ENCODING"} | {name for name in names if name.startswith("HF_")}, sorted(names)
    assert environment["HF_HOME"] == "/models" and environment["CUDA_VISIBLE_DEVICES"] == "3"
    assert environment["HF_HUB_OFFLINE"] == "1" and environment["PATH"] == os.environ["PATH"]
    assert environment["JEFF_EVENTS"] == str(tmp_path / "out" / "trainer-events.jsonl")
