"""Jeff's authorise hook: called with every function just before it is decided -- a called function, each router and
the leaf a dispatch reaches, an escalation step's function and each graph node -- so a refusal (NotLicensed) stops
that function before the engine sees a row for it. with_authorise gives a per-caller view of one shared Jeff."""

import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml

from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.errors import EscalationFailed, NotLicensed
from jeff.escalation import apply_policy, escalate, function_step, remote
from jeff.manifest import FunctionSpec
from jeff.testing import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures"
CATALOGUE = FIXTURES / "catalogue"
ROUTERS = FIXTURES / "routers"
GRAPH = FIXTURES / "graphs" / "support_intake.yaml"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
PARAMS: dict[str, Any] = {"company": "A furniture shop.", "channel": "email", "teams": {"deliveries": "D", "refunds": "R"}}
ROUTED = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
UNSURE = [0.0, 0.5, 0.5, 0.0, 0.0]  # below triage.urgency's calibration threshold 0.6


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return UNSURE if adapter == "jeff.support.triage" and "unsure" in str(row["state"]) else [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


class Refuse:
    """An authorise hook that refuses every function of one adapter and records every function it was shown."""

    def __init__(self, adapter_id: str) -> None:
        self.adapter_id = adapter_id
        self.seen: list[str] = []

    def __call__(self, function: FunctionSpec) -> None:
        self.seen.append(function.id)
        if function.adapter_id == self.adapter_id:
            raise NotLicensed(f"{function.id}: no licence for {self.adapter_id}")


def adapters_seen(engine: FakeEngine) -> set[str]:
    return {adapter for adapter, _ in engine.calls}


def test_no_hook_by_default() -> None:
    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})
    jeff = Jeff(engine, Catalogue.from_dir(CATALOGUE))
    assert jeff.authorise is None
    jeff.decide([Call("spam.message_type", "x", {"channel": "email"})])
    assert adapters_seen(engine) == {"jeff.safety.spam"}


def test_a_refused_call_raises_before_the_engine_sees_it() -> None:
    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})
    refuse = Refuse("jeff.support.triage")
    jeff = Jeff(engine, Catalogue.from_dir(CATALOGUE), authorise=refuse)
    with pytest.raises(NotLicensed, match="jeff.support.triage"):
        jeff.decide([Call("spam.message_type", "x", {"channel": "email"}),
                     Call("triage.urgency", "x", CONTEXT)])
    assert engine.calls == []  # the whole batch is refused before any row is sent
    assert refuse.seen == ["jeff.safety.spam.message_type", "jeff.support.triage.urgency"]


def test_a_bound_function_is_refused_when_bound() -> None:
    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})
    jeff = Jeff(engine, Catalogue.from_dir(CATALOGUE), authorise=Refuse("jeff.support.triage"))
    with pytest.raises(NotLicensed):
        jeff.function("triage.urgency", context=CONTEXT)
    assert engine.calls == []


def test_a_dispatched_leaf_is_refused_after_routing_and_before_it_runs() -> None:
    engine = FakeEngine(answers, set(ROUTED))
    refuse = Refuse("jeff.support.triage")
    jeff = Jeff(engine, Catalogue.from_dir(ROUTERS), authorise=refuse)
    with pytest.raises(NotLicensed, match="jeff.support.triage"):
        jeff.dispatch("My wardrobe is broken", interface="rate", context=CONTEXT)
    assert adapters_seen(engine) == {"jeff.core.router", "jeff.support.router"}
    assert refuse.seen == ["jeff.core.router.domain", "jeff.support.router.function", "jeff.support.triage.urgency"]


def backup_catalogue(tmp_path: Path) -> Path:
    """The fixture catalogue plus jeff.support.backup, a second adapter with an uncalibrated urgency function."""
    catalogue = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, catalogue)
    second = yaml.safe_load((CATALOGUE / "jeff.support.triage" / "manifest.yaml").read_text())
    second["id"], second["aliases"] = "jeff.support.backup", ["backup"]
    second["functions"] = {"urgency": {**second["functions"]["urgency"], "calibration": None}}
    (catalogue / "jeff.support.backup").mkdir()
    (catalogue / "jeff.support.backup" / "manifest.yaml").write_text(yaml.safe_dump(second))
    return catalogue


def test_an_escalation_step_is_refused_as_not_licensed_not_as_a_failed_step(tmp_path: Path) -> None:
    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage", "jeff.support.backup"})
    jeff = Jeff(engine, Catalogue.from_dir(backup_catalogue(tmp_path)))
    policy = escalate(function_step("backup.urgency"))
    call = Call("triage.urgency", "unsure", CONTEXT)
    [abstained] = jeff.decide([call])
    assert abstained.abstained
    # the step's licence is checked as the step runs (the policy was not checked by this caller's view up front)
    with pytest.raises(NotLicensed, match="jeff.support.backup"):
        apply_policy(jeff.with_authorise(Refuse("jeff.support.backup")), call, abstained, policy)
    assert adapters_seen(engine) == {"jeff.support.triage"}


def test_an_escalation_step_is_checked_with_the_policy_before_anything_is_decided(tmp_path: Path) -> None:
    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage", "jeff.support.backup"})
    jeff = Jeff(engine, Catalogue.from_dir(backup_catalogue(tmp_path)), authorise=Refuse("jeff.support.backup"))
    with pytest.raises(NotLicensed, match="jeff.support.backup"):
        jeff.function("triage.urgency", context=CONTEXT, on_low_confidence=escalate(function_step("backup.urgency")))
    assert engine.calls == []


def test_a_remote_step_refused_by_its_server_is_a_failed_step(tmp_path: Path) -> None:
    """Only this process's own hook raises NotLicensed through escalation; a remote server's refusal is that step
    failing."""
    class RefusingServer:
        def decide(self, calls: list[Call]) -> list[Any]:
            raise NotLicensed("the remote server has no licence for this caller")

    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})
    jeff = Jeff(engine, Catalogue.from_dir(CATALOGUE), authorise=Refuse("jeff.nothing.here"))
    urgency = jeff.function("triage.urgency", context=CONTEXT, on_low_confidence=escalate(remote(RefusingServer())))
    with pytest.raises(EscalationFailed, match="remote"):
        urgency("unsure")


def test_a_graph_node_is_refused_as_not_licensed_before_anything_is_decided() -> None:
    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})
    jeff = Jeff(engine, Catalogue.from_dir(CATALOGUE), authorise=Refuse("jeff.support.triage"))
    with pytest.raises(NotLicensed, match="jeff.support.triage"):
        jeff.run_graph(GRAPH, "My wardrobe is broken", PARAMS, code={"make_ticket": lambda state: None})
    assert engine.calls == []


def test_with_authorise_is_a_view_that_leaves_the_shared_jeff_unchanged() -> None:
    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})
    shared = Jeff(engine, Catalogue.from_dir(CATALOGUE))
    refuse = Refuse("jeff.safety.spam")
    view = shared.with_authorise(refuse)
    assert view is not shared and view.authorise is refuse and shared.authorise is None
    assert view.engine is shared.engine and view.catalogue is shared.catalogue and view.log is shared.log
    with pytest.raises(NotLicensed):
        view.decide([Call("spam.message_type", "x", {"channel": "email"})])
    shared.decide([Call("spam.message_type", "x", {"channel": "email"})])
    assert adapters_seen(engine) == {"jeff.safety.spam"}


def test_an_unlicensed_escalation_step_is_not_licensed_before_it_is_found_not_installed(tmp_path: Path) -> None:
    """As in binding, the licence is checked first, so an unlicensed caller never learns whether the step's adapter
    is installed."""
    engine = FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})  # jeff.support.backup not installed
    jeff = Jeff(engine, Catalogue.from_dir(backup_catalogue(tmp_path)), authorise=Refuse("jeff.support.backup"))
    with pytest.raises(NotLicensed, match="jeff.support.backup"):
        jeff.function("triage.urgency", context=CONTEXT, on_low_confidence=escalate(function_step("backup.urgency")))
