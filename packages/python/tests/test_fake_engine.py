from pathlib import Path

import pytest

from jeff.engine import BASE_ADAPTER, check_option_limit
from jeff.errors import EngineError, InputTooLong, InvalidRequest, NotInstalled, TooManyLabels
from jeff.testing import FakeEngine, fake_tokens

ROW = {"state": {"message": "hi"}, "question": {"type": "noul", "instructions": "?"}}


def make() -> FakeEngine:
    return FakeEngine(lambda adapter, row: [0.5, 0.5], {"jeff.safety.spam"})


def test_check_accepts_good_rows() -> None:
    make().check("jeff.safety.spam", [ROW])


def test_check_errors() -> None:
    engine = make()
    with pytest.raises(NotInstalled, match="jeff.nope.zzz"):
        engine.check("jeff.nope.zzz", [ROW])
    with pytest.raises(NotInstalled, match="jeff.nope.zzz"):
        engine.predict("jeff.nope.zzz", [ROW])
    with pytest.raises(InvalidRequest, match="question"):
        engine.check("jeff.safety.spam", [{"state": {}}])
    with pytest.raises(InputTooLong):
        engine.check("jeff.safety.spam", [{"state": {"__too_long__": 1}, "question": ROW["question"]}])


def test_add_and_remove_adapters() -> None:
    engine = make()
    engine.add_adapter("jeff.support.triage", Path("/unused"))
    assert engine.installed() == {"jeff.safety.spam", "jeff.support.triage"}
    engine.remove_adapter("jeff.safety.spam")
    assert engine.installed() == {"jeff.support.triage"}
    with pytest.raises(EngineError):
        engine.remove_adapter("jeff.safety.spam")
    with pytest.raises(EngineError):
        engine.add_adapter("jeff.support.triage", Path("/unused"))


def test_installed_accepts_refresh_and_ignores_it() -> None:
    assert make().installed(refresh=True) == {"jeff.safety.spam"}


def test_predict_reports_each_rows_input_tokens() -> None:
    rows = [ROW, {"state": "a b c", "question": ROW["question"]}]
    result = make().predict("jeff.safety.spam", rows)
    assert result.input_tokens == [fake_tokens(row) for row in rows]
    assert result.input_tokens[0] != result.input_tokens[1]


def test_with_option_limits_check_and_predict_refuse_a_choice_with_too_many_options() -> None:
    engine = FakeEngine(lambda adapter, row: [1.0] + [0.0] * (len(row["question"]["criteria"]) - 1),
                        {"base", "jeff.safety.spam"}, max_options={"base": 3, "jeff.safety.spam": 2})
    three = {"state": "s", "question": {"type": "choice", "criteria": {"a": None, "b": None, "c": None}}}
    engine.check("base", [three])
    assert engine.predict("base", [three]).probabilities == [[1.0, 0.0, 0.0]]
    for refuse in (engine.check, engine.predict):
        with pytest.raises(TooManyLabels, match="3 options, but this model handles at most 2"):
            refuse("jeff.safety.spam", [three])
    engine.check("jeff.safety.spam", [ROW])  # only choice questions have a limit here


def test_the_option_limit_names_the_count_the_limit_and_what_to_do() -> None:
    rows = [{"state": "s", "question": {"type": "choice", "criteria": {f"o{i}": None for i in range(27)}}}]
    check_option_limit("base", rows, 27)
    with pytest.raises(TooManyLabels) as refused:
        check_option_limit("base", rows, 26)
    assert str(refused.value) == ("A choice question has 27 options, but this model handles at most 26 (base was "
                                  "trained on questions of up to 26 options). Shortlist the options first, or split "
                                  "the question.")


def test_the_base_adapter_id_cannot_be_a_manifest_id() -> None:
    assert BASE_ADAPTER == "base" and "." not in BASE_ADAPTER  # manifest IDs have three dotted parts


def test_the_bases_checkpoint_and_option_limit() -> None:
    assert make().base_checkpoint == "fake-base"
    assert make().base_max_options == 255  # no limits configured: any count the answer codes allow
    limited = FakeEngine(lambda adapter, row: [0.5, 0.5], {"base"}, max_options={"base": 26})
    assert limited.base_max_options == 26
