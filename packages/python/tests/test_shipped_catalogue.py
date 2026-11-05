from jeff.catalogue import Catalogue

# code and code-router are left out of step 1 until the owner decides their request format and test set.
# At v1.4 the open set grew from 13 to 31 served adapters (scripts/open_catalogue.py and
# catalogue-imports/open-set.yaml). On 4 November 2026 the owner opened three more (assistant-routing, knowledge-qa-v2,
# the open router jeff-router-open-v14), and on 5 November dropped the two kept at their v1.3 release (aml, nav), as
# only v1.4 stays on Hugging Face: 34 in all.
NAMES = ["assistant-routing", "jeff-router-open-v14", "knowledge-qa-v2", "emotion", "ground", "guard", "legal-clauses", "oss-action-check", "oss-after-result",
         "oss-ci-failure", "oss-contradicts", "oss-doc-type", "oss-entails", "oss-equivalent",
         "oss-evidence-sufficient-v2", "oss-extract-check", "oss-is-complete", "oss-issue-route",
         "oss-message-triage", "oss-passage-relevant-v2", "oss-query-route", "oss-requires-action",
         "oss-result-relevant", "oss-satisfies", "oss-section-id", "oss-task-complete", "oss-tool-route",
         "sanctions", "soc", "spam", "support-intents", "tools", "trading-desk", "triage"]


def test_all_34_shipped_adapters_load() -> None:
    catalogue = Catalogue.shipped()
    assert sorted(manifest.aliases[0] for manifest in catalogue.manifests()) == sorted(NAMES)
    for manifest in catalogue.manifests():
        assert manifest.licence_kind == "open" and "open" in manifest.weights
        assert manifest.weights["open"]["revision"] == "v1.4"
        assert manifest.tests["published"].startswith("hf://")


def test_triage_route_takes_caller_teams_after_other() -> None:
    route = Catalogue.shipped().function("triage.route")
    assert route.labels.source == "caller"
    assert list(route.labels.fixed_first) == ["other"]
    assert route.labels.caller_keys == "k{n}"


def test_interfaces_from_the_spec_table() -> None:
    catalogue = Catalogue.shipped()
    assert catalogue.function("ground.grounding").interface == "is_supported"
    assert catalogue.function("sanctions").interface == "equivalent"
    assert catalogue.function("triage.needs_human").interface == "requires_escalation"


def test_caller_label_limits_follow_the_documented_training_limits() -> None:
    """The v1.4 trained limits (catalogue/<id>/manifest.yaml), lower than v1.3's for triage.route and tools."""
    catalogue = Catalogue.shipped()
    assert catalogue.function("triage.route").labels.max == 35
    assert catalogue.function("tools").labels.max == 134


def test_soc_offers_a_subset_and_trading_desk_route_describes_its_venues() -> None:
    """At v1.4 soc's labels are fixed with subset=true (the caller may offer a subset of the fixed labels), not
    the labels.source literally named 'subset' that the v1.3 manifest used."""
    catalogue = Catalogue.shipped()
    soc = catalogue.function("soc").labels
    assert soc.source == "fixed" and soc.subset is True
    assert list(soc.fixed) == ["block", "isolate", "escalate", "investigate", "ignore"]
    route = catalogue.function("trading-desk.route").labels
    assert route.source == "describe" and route.keys == ("venue_a", "venue_b", "venue_c")
