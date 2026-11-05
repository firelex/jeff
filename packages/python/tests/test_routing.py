from pathlib import Path
import tempfile

import pytest

from jeff.catalogue import Catalogue
from jeff.client import Jeff
from jeff.errors import MissingContext, NotInstalled, ManifestError, LowConfidence
from jeff.testing import FakeEngine

FIXTURES = Path(__file__).parent / "fixtures" / "routers"
ALL = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
CONTEXT = {"company": "A furniture shop.", "channel": "email"}


def choose(first: dict[str, float]):
    def answers(adapter: str, row: dict) -> list[float]:
        question = row["question"]
        if adapter in first:
            keys = list(question["criteria"])
            if len(keys) == 1:
                return [1.0]  # a softmax over one option, as the engine answers it
            return [first[adapter] if index == 0 else (1 - first[adapter]) / max(1, len(keys) - 1)
                    for index in range(len(keys))]
        if question["type"] == "score":
            return [0.0, 0.0, 0.9, 0.1, 0.0]
        return [1.0] + [0.0] * (len(question["criteria"]) - 1)
    return answers


def make(installed=ALL, first=None) -> Jeff:
    return Jeff(FakeEngine(choose(first or {"jeff.core.router": 0.95, "jeff.support.router": 0.9}), set(installed)),
                Catalogue.from_dir(FIXTURES))


def test_dispatch_walks_two_levels_and_records_the_route() -> None:
    decision = make().rate("My wardrobe is broken", context=CONTEXT)
    assert decision.function == "jeff.support.triage.urgency" and decision.value == 2
    assert [(step.router, step.chose) for step in decision.route] == [
        ("jeff.core.router.domain", "jeff.support.router"),
        ("jeff.support.router.function", "jeff.support.triage.urgency")]
    assert decision.route[0].confidence == pytest.approx(0.95)


def test_abstaining_router_stops_the_walk() -> None:
    decision = make(first={"jeff.core.router": 0.3}).rate("??", context=CONTEXT)
    assert decision.abstained is True and decision.function == "jeff.core.router.domain"
    assert len(decision.route) == 1


def test_routed_function_needs_its_context() -> None:
    with pytest.raises(MissingContext, match="company, channel"):
        make().rate("My wardrobe is broken", context={})


def test_missing_target_fails_at_load() -> None:
    with pytest.raises(NotInstalled, match="jeff.support.triage"):
        make(installed=ALL - {"jeff.support.triage"})


def offered(engine, adapter: str) -> list[list[str]]:
    return [list(row["question"]["criteria"]) for name, rows in engine.calls if name == adapter for row in rows]


def test_a_mixed_interface_router_offers_only_targets_that_lead_to_the_interface() -> None:
    jeff = make()
    decision = jeff.classify("WIN A PRIZE", context=CONTEXT)
    assert decision.function == "jeff.safety.spam.message_type"
    assert [(step.router, step.chose) for step in decision.route] == [("jeff.core.router.domain", "jeff.safety.spam")]
    assert offered(jeff.engine, "jeff.core.router") == [["jeff.safety.spam"]]
    jeff = make()
    jeff.rate("My wardrobe is broken", context=CONTEXT)
    assert offered(jeff.engine, "jeff.core.router") == [["jeff.support.router", "jeff.support.triage.urgency"]]
    assert offered(jeff.engine, "jeff.support.router") == [["jeff.support.triage.urgency"]]


def test_a_tree_with_no_target_for_the_interface_is_a_manifest_error() -> None:
    jeff = make()
    with pytest.raises(ManifestError, match="jeff.core.router.*equivalent"):
        jeff.equivalent("x", context=CONTEXT)
    assert jeff.engine.calls == []


def test_caller_labels_reach_a_caller_label_leaf_and_are_dropped_for_a_fixed_one() -> None:
    teams = {"deliveries": "Deliveries", "refunds": "Refunds"}
    jeff = make()
    decision = jeff.route("My wardrobe is broken", context=CONTEXT, labels=teams)
    assert decision.function == "jeff.support.triage.route" and set(decision.probabilities) == {"other", *teams}
    assert make().rate("My wardrobe is broken", context=CONTEXT, labels=teams).value == 2
    assert make().classify("WIN", context=CONTEXT, labels=teams).function == "jeff.safety.spam.message_type"


def test_dispatch_without_a_router_says_so_and_suggests_naming_the_function() -> None:
    from jeff.errors import UnknownFunction
    jeff = Jeff(FakeEngine(choose({}), {"jeff.support.triage"}), Catalogue.from_dir(FIXTURES))
    with pytest.raises(NotInstalled, match="no router.*function="):
        jeff.rate("x", context=CONTEXT)
    plain = Jeff(FakeEngine(choose({}), {"jeff.support.triage"}),
                 Catalogue.from_dir(Path(__file__).parent / "fixtures" / "catalogue"))
    with pytest.raises(UnknownFunction, match="no router.*function="):
        plain.rate("x", context=CONTEXT)


def test_bare_multifuncton_adapter_in_routes_to_fails_at_load() -> None:
    """A router naming a bare multi-function adapter (not a router, not a specific function) must fail at load."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Create bases.yaml
        (tmp_path / "bases.yaml").write_text("""
jeff-base:
  v1.3: { repo: mstrasser/jeff-base, revision: v1.3 }
""")

        # Create a bad router that routes to a bare multi-function adapter
        router_dir = tmp_path / "jeff.bad.router"
        router_dir.mkdir()
        (router_dir / "manifest.yaml").write_text("""
id: jeff.bad.router
version: 0.1.0
aliases: []
licence: { kind: open, adapter: Apache-2.0 }
weights: { open: { repo: fixture/router, revision: v0 } }
base: { id: jeff-base, version: v1.3, max_options: 252 }
context: []
input: { key: text, description: Any text., form: fields }
functions:
  route:
    interface: route
    type: choice
    instructions: Route somewhere.
    labels: { source: subset, fixed: { jeff.support.triage: Must name a specific function } }
    abstain_labels: []
    calibration: null
routes_to: [jeff.support.triage]
tests: { published: null, calibration: null, function_by: only }
""")

        # Copy support triage (multi-function adapter)
        import shutil
        shutil.copytree(FIXTURES / "jeff.support.triage", tmp_path / "jeff.support.triage")

        with pytest.raises(ManifestError, match="jeff.bad.router.*jeff.support.triage.*3 functions"):
            Jeff(FakeEngine(choose({"jeff.bad.router": 0.9}), {"jeff.bad.router", "jeff.support.triage"}),
                 Catalogue.from_dir(tmp_path))


def test_nonexistent_function_in_routes_to_fails_at_load() -> None:
    """A router naming a non-existent function on an installed adapter must fail at load."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Create bases.yaml
        (tmp_path / "bases.yaml").write_text("""
jeff-base:
  v1.3: { repo: mstrasser/jeff-base, revision: v1.3 }
""")

        # Create a bad router that routes to nonexistent function
        router_dir = tmp_path / "jeff.bad.router"
        router_dir.mkdir()
        (router_dir / "manifest.yaml").write_text("""
id: jeff.bad.router
version: 0.1.0
aliases: []
licence: { kind: open, adapter: Apache-2.0 }
weights: { open: { repo: fixture/router, revision: v0 } }
base: { id: jeff-base, version: v1.3, max_options: 252 }
context: []
input: { key: text, description: Any text., form: fields }
functions:
  route:
    interface: route
    type: choice
    instructions: Route somewhere.
    labels: { source: subset, fixed: { jeff.support.triage.nonexistent: Does not exist } }
    abstain_labels: []
    calibration: null
routes_to: [jeff.support.triage.nonexistent]
tests: { published: null, calibration: null, function_by: only }
""")

        # Copy the support triage adapter for reference
        import shutil
        shutil.copytree(FIXTURES / "jeff.support.triage", tmp_path / "jeff.support.triage")
        shutil.copytree(FIXTURES / "jeff.support.router", tmp_path / "jeff.support.router")

        with pytest.raises(ManifestError, match="jeff.bad.router.*nonexistent"):
            Jeff(FakeEngine(choose({"jeff.bad.router": 0.9}), {"jeff.bad.router", "jeff.support.triage"}),
                 Catalogue.from_dir(tmp_path))


def test_abstaining_router_with_error_policy_raises_low_confidence() -> None:
    """When a router abstains and on_low_confidence='error', raise LowConfidence."""
    with pytest.raises(LowConfidence) as exc_info:
        make(first={"jeff.core.router": 0.3}).rate("??", context=CONTEXT, on_low_confidence="error")

    assert exc_info.value.decision.abstained is True
    assert exc_info.value.decision.function == "jeff.core.router.domain"
    assert len(exc_info.value.decision.route) == 1


def test_routing_loop_raises_manifest_error() -> None:
    """A routing loop (two routers routing to each other) raises ManifestError."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)

        # Create bases.yaml
        (tmp_path / "bases.yaml").write_text("""
jeff-base:
  v1.3: { repo: mstrasser/jeff-base, revision: v1.3 }
""")

        # Create router A that routes to router B
        router_a_dir = tmp_path / "jeff.loop.a"
        router_a_dir.mkdir()
        (router_a_dir / "manifest.yaml").write_text("""
id: jeff.loop.a
version: 0.1.0
aliases: []
licence: { kind: open, adapter: Apache-2.0 }
weights: { open: { repo: fixture/router, revision: v0 } }
base: { id: jeff-base, version: v1.3, max_options: 252 }
context: []
input: { key: text, description: Any text., form: fields }
functions:
  route:
    interface: route
    type: choice
    instructions: Route.
    labels: { source: subset, fixed: { jeff.loop.b: To router B } }
    abstain_labels: []
    calibration: null
routes_to: [jeff.loop.b]
tests: { published: null, calibration: null, function_by: only }
""")

        # Create router B that routes back to router A
        router_b_dir = tmp_path / "jeff.loop.b"
        router_b_dir.mkdir()
        (router_b_dir / "manifest.yaml").write_text("""
id: jeff.loop.b
version: 0.1.0
aliases: []
licence: { kind: open, adapter: Apache-2.0 }
weights: { open: { repo: fixture/router, revision: v0 } }
base: { id: jeff-base, version: v1.3, max_options: 252 }
context: []
input: { key: text, description: Any text., form: fields }
functions:
  route:
    interface: route
    type: choice
    instructions: Route.
    labels: { source: subset, fixed: { jeff.loop.a: To router A } }
    abstain_labels: []
    calibration: null
routes_to: [jeff.loop.a]
tests: { published: null, calibration: null, function_by: only }
""")

        def loop_answers(adapter: str, row: dict) -> list[float]:
            # Router A chooses router B, router B chooses router A
            # Each router has only one option in its labels
            return [1.0]  # Always choose the first (and only) option

        with pytest.raises(ManifestError, match="loop.*jeff.loop.a -> jeff.loop.b -> jeff.loop.a"):
            Jeff(FakeEngine(loop_answers, {"jeff.loop.a", "jeff.loop.b"}), Catalogue.from_dir(tmp_path))


def test_an_invalid_policy_is_refused_before_routing() -> None:
    from jeff.errors import InvalidPolicy
    for first in ({"jeff.core.router": 0.95, "jeff.support.router": 0.9}, {"jeff.core.router": 0.3}):
        jeff = make(first=first)
        with pytest.raises(InvalidPolicy, match="eror"):
            jeff.rate("x", context=CONTEXT, on_low_confidence="eror")  # type: ignore[arg-type]
        assert jeff.engine.calls == []  # type: ignore[attr-defined]


class StaleCache(FakeEngine):
    """An engine whose installed() list is a cached copy (as SocketEngine's is) that another process has since
    changed: only installed(refresh=True) asks the engine itself, and updates the cache."""

    def __init__(self, cached: set[str], real: set[str]) -> None:
        super().__init__(choose({"jeff.core.router": 0.95, "jeff.support.router": 0.9}), set(real))
        self.cached = set(cached)
        self.refreshes = 0

    def installed(self, refresh: bool = False) -> set[str]:
        if refresh:
            self.refreshes += 1
            self.cached = super().installed()
        return set(self.cached)


def test_dispatch_rechecks_a_stale_installed_list_once_before_not_installed() -> None:
    # Built while no router was installed; another process has since served both routers.
    engine = StaleCache(cached={"jeff.support.triage", "jeff.safety.spam"}, real=ALL)
    jeff = Jeff(engine, Catalogue.from_dir(FIXTURES))
    decision = jeff.rate("My wardrobe is broken", context=CONTEXT)
    assert decision.function == "jeff.support.triage.urgency"
    assert engine.refreshes == 1  # asked once for the starting router; the cache then has the nested one too


def test_a_router_reached_while_walking_is_rechecked_against_a_stale_list() -> None:
    engine = StaleCache(cached={"jeff.core.router", "jeff.support.triage", "jeff.safety.spam"}, real=ALL)
    jeff = Jeff.__new__(Jeff)  # the stale list fails the load-time router check; the walk is what is tested here
    jeff.engine, jeff.catalogue, jeff.log, jeff.authorise = engine, Catalogue.from_dir(FIXTURES), None, None
    jeff.base_format, jeff.format_id, jeff._calibrations, jeff._exact_keys = engine.base_format, None, {}, {}
    decision = jeff.rate("My wardrobe is broken", context=CONTEXT)
    assert decision.function == "jeff.support.triage.urgency" and engine.refreshes == 1


def test_a_router_missing_after_the_recheck_is_not_installed() -> None:
    engine = StaleCache(cached=ALL, real=ALL - {"jeff.core.router", "jeff.support.router"})
    jeff = Jeff(engine, Catalogue.from_dir(FIXTURES))
    with pytest.raises(NotInstalled, match="jeff.core.router"):
        jeff.rate("My wardrobe is broken", context=CONTEXT)
