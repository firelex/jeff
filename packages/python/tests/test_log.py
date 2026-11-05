import json
from pathlib import Path

from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.escalation import escalate, human
from jeff.log import DecisionLog
from jeff.testing import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}


def test_log_writes_hash_not_input_and_records_escalations(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    jeff = Jeff(FakeEngine(lambda a, r: [0.0, 0.5, 0.5, 0.0, 0.0], {"jeff.support.triage"}),
                Catalogue.from_dir(FIXTURES), log=DecisionLog(path))
    jeff.rate("secret message", function="triage.urgency", context=CONTEXT,
              on_low_confidence=escalate(human(lambda q: "4")))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(lines) == 2
    assert lines[0]["abstained"] is True and "input" not in lines[0] and len(lines[0]["input_sha256"]) == 64
    assert lines[1]["decided_by"] == "human" and lines[1]["value"] == 4 and len(lines[1]["escalations"]) == 1
    assert "secret message" not in path.read_text()


def test_log_can_include_input(tmp_path: Path) -> None:
    path = tmp_path / "decisions.jsonl"
    jeff = Jeff(FakeEngine(lambda a, r: [0.0, 0.0, 1.0, 0.0, 0.0], {"jeff.support.triage"}),
                Catalogue.from_dir(FIXTURES), log=DecisionLog(path, include_input=True))
    jeff.rate("hello", function="triage.urgency", context=CONTEXT)
    assert json.loads(path.read_text())["input"] == "hello"


def test_log_hashes_subset_labels_given_as_a_list(tmp_path: Path) -> None:
    from types import SimpleNamespace

    from jeff.client import Call
    path = tmp_path / "decisions.jsonl"
    decision = SimpleNamespace(function="jeff.security.soc.action", version="1.3.0", value="ignore", confidence=0.9,
                               abstained=False, calibrated=False, decided_by="jeff.security.soc.action@1.3.0",
                               to_json=lambda: {"route": [], "escalations": []})
    DecisionLog(path).write(Call("soc", "alert", {"playbook": "p"}, ["ignore", "investigate"]), decision)  # type: ignore[arg-type]
    assert len(json.loads(path.read_text())["input_sha256"]) == 64
