from pathlib import Path

import pytest

from jeff.binding import bind, build_row, option_keys
from jeff.catalogue import Catalogue
from jeff.errors import InvalidLabels, ManifestError, MissingContext, MissingLabels, TooManyLabels, UnknownContext
from jeff.manifest import manifest_from_dict

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
TEAMS = {"deliveries": "Late or damaged deliveries", "refunds": "Refunds and payments"}


@pytest.fixture
def catalogue() -> Catalogue:
    return Catalogue.from_dir(FIXTURES)


def binding(catalogue: Catalogue, ref: str, context=CONTEXT, labels=None):
    function = catalogue.function(ref)
    return bind(catalogue.manifest_of(function), function, context, labels,
                calibration=function.calibration)


def test_caller_labels_are_numbered_after_the_fixed_first(catalogue: Catalogue) -> None:
    bound = binding(catalogue, "triage.route", labels=TEAMS)
    assert bound.criteria == {"other": "Not for any of these teams", "k1": "Late or damaged deliveries",
                              "k2": "Refunds and payments"}
    assert bound.wire_to_caller == {"other": "other", "k1": "deliveries", "k2": "refunds"}
    assert option_keys(bound) == ["other", "k1", "k2"]


def test_labels_in_another_order_map_back_correctly(catalogue: Catalogue) -> None:
    bound = binding(catalogue, "triage.route", labels=dict(reversed(list(TEAMS.items()))))
    assert bound.wire_to_caller["k1"] == "refunds"


def test_row_puts_context_first_and_input_last(catalogue: Catalogue) -> None:
    row = build_row(binding(catalogue, "triage.route", labels=TEAMS), "My wardrobe arrived broken.")
    assert list(row["state"]) == ["company", "channel", "message"]
    assert row["state"]["message"] == "My wardrobe arrived broken."
    assert row["question"]["type"] == "choice"
    assert row["question"]["instructions"].startswith("Which team should handle this message?")


def test_score_row_lists_levels(catalogue: Catalogue) -> None:
    bound = binding(catalogue, "triage.urgency")
    row = build_row(bound, "hello")
    assert row["question"] == {"type": "score", "instructions": "How urgently does this need a response?",
                               "criteria": ["No action needed", "Can wait a few days", "Handle within a day",
                                            "Handle within hours", "Handle immediately"]}
    assert option_keys(bound) == ["0", "1", "2", "3", "4"]


def test_noul_row_has_no_criteria(catalogue: Catalogue) -> None:
    bound = binding(catalogue, "triage.needs_human")
    assert "criteria" not in build_row(bound, "hello")["question"]
    assert option_keys(bound) == ["false", "true"]


def test_missing_context_lists_every_field(catalogue: Catalogue) -> None:
    with pytest.raises(MissingContext, match="company, channel"):
        binding(catalogue, "triage.route", context={}, labels=TEAMS)


def test_unknown_context_field_is_refused(catalogue: Catalogue) -> None:
    with pytest.raises(UnknownContext, match="colour"):
        binding(catalogue, "triage.route", context={**CONTEXT, "colour": "red"}, labels=TEAMS)


def test_caller_labels_required(catalogue: Catalogue) -> None:
    with pytest.raises(MissingLabels):
        binding(catalogue, "triage.route")


def test_labels_refused_for_fixed_functions(catalogue: Catalogue) -> None:
    with pytest.raises(InvalidLabels, match="fixed"):
        binding(catalogue, "spam", context={"channel": "sms"}, labels=TEAMS)


def test_caller_key_colliding_with_fixed_first_is_refused(catalogue: Catalogue) -> None:
    with pytest.raises(InvalidLabels, match="other"):
        binding(catalogue, "triage.route", labels={"other": "Our other team"})


def test_number_like_keys_are_refused(catalogue: Catalogue) -> None:
    with pytest.raises(InvalidLabels, match="'12'"):
        binding(catalogue, "triage.route", labels={"12": "Team twelve"})


def test_too_many_labels(catalogue: Catalogue) -> None:
    with pytest.raises(TooManyLabels, match="41.*40"):
        binding(catalogue, "triage.route", labels={f"team{i}": f"Team {i}" for i in range(41)})


ACTIONS = {"ignore": "Ignore the alert", "investigate": "Open a ticket", "escalate": "Call the on-call lead",
           "isolate": "Isolate the host", "block": "Block the address"}
VENUES = ["venue_a", "venue_b", "venue_c"]


def small_manifest(labels: dict) -> dict:
    return {
        "id": "jeff.test.small", "version": "1.0.0", "aliases": [], "licence": {"kind": "open", "adapter": "MIT"},
        "weights": {"open": {"repo": "x/y", "revision": "v1"}},
        "base": {"id": "jeff-base", "version": "v1.3", "max_options": 252},
        "context": [{"key": "playbook", "description": "The rules.", "required": True}],
        "input": {"key": "alert", "description": "The alert.", "form": "fields"},
        "functions": {"pick": {"interface": "classify", "type": "choice", "instructions": "Which one?",
                               "labels": labels, "abstain_labels": [], "calibration": None}},
        "routes_to": [], "tests": {"published": None, "calibration": None, "function_by": "only"},
    }


def small_binding(labels_spec: dict, labels):
    manifest = manifest_from_dict(small_manifest(labels_spec), Path("small.yaml"))
    return bind(manifest, manifest.functions["pick"], {"playbook": "Rule 1."}, labels,
                calibration=None)


SUBSET = {"source": "subset", "fixed": ACTIONS, "required": ["ignore", "investigate", "escalate"]}
DESCRIBE = {"source": "describe", "keys": VENUES}


def test_subset_labels_take_the_fixed_texts_in_the_callers_order() -> None:
    bound = small_binding(SUBSET, ["escalate", "block", "ignore", "investigate"])
    assert bound.criteria == {key: ACTIONS[key] for key in ["escalate", "block", "ignore", "investigate"]}
    assert bound.wire_to_caller == {key: key for key in ["escalate", "block", "ignore", "investigate"]}


@pytest.mark.parametrize("labels,match", [
    (["ignore", "investigate", "escalate", "shutdown"], "shutdown"),
    (["ignore", "investigate"], "escalate"),
    (["ignore", "investigate", "escalate", "ignore"], "more than once"),
    ({"ignore": "x", "investigate": "y", "escalate": "z"}, "list of keys"),
])
def test_subset_labels_errors(labels, match: str) -> None:
    with pytest.raises(InvalidLabels, match=match):
        small_binding(SUBSET, labels)


def test_subset_labels_are_required() -> None:
    with pytest.raises(MissingLabels):
        small_binding(SUBSET, None)


def test_describe_labels_keep_the_manifest_order_with_the_callers_texts() -> None:
    texts = {"venue_c": "Dark pool, 0.1 bp", "venue_a": "Lit exchange, 0.3 bp", "venue_b": "MTF, 0.2 bp"}
    bound = small_binding(DESCRIBE, texts)
    assert list(bound.criteria or {}) == VENUES
    assert bound.criteria["venue_a"] == "Lit exchange, 0.3 bp"
    assert bound.wire_to_caller == {key: key for key in VENUES}


@pytest.mark.parametrize("labels,match", [
    ({"venue_a": "a", "venue_b": "b"}, "venue_c"),
    ({"venue_a": "a", "venue_b": "b", "venue_c": "c", "venue_d": "d"}, "venue_d"),
    (["venue_a", "venue_b", "venue_c"], "object"),
])
def test_describe_labels_errors(labels, match: str) -> None:
    with pytest.raises(InvalidLabels, match=match):
        small_binding(DESCRIBE, labels)


def test_describe_labels_are_required() -> None:
    with pytest.raises(MissingLabels):
        small_binding(DESCRIBE, None)


@pytest.mark.parametrize("labels,match", [
    ({"source": "subset", "fixed": ACTIONS, "required": ["shutdown"]}, "shutdown"),
    ({"source": "subset", "fixed": {}}, "non-empty 'fixed'"),
    ({"source": "describe", "keys": VENUES, "fixed": ACTIONS}, "describe labels need"),
    ({"source": "fixed", "fixed": ACTIONS, "subset": False, "required": ["ignore"]}, "only subset"),
    ({"source": "fixed", "fixed": ACTIONS, "subset": False, "keys": ["ignore"]}, "only describe"),
])
def test_subset_and_describe_manifests_are_checked(labels: dict, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        manifest_from_dict(small_manifest(labels), Path("small.yaml"))


FIXED_SUBSET = {"source": "fixed", "fixed": ACTIONS, "subset": True}


def test_fixed_subset_labels_render_in_the_manifest_order_with_the_fixed_texts() -> None:
    bound = small_binding(FIXED_SUBSET, ["block", "isolate", "ignore"])
    assert bound.criteria == {"ignore": ACTIONS["ignore"], "isolate": ACTIONS["isolate"], "block": ACTIONS["block"]}
    assert list(bound.criteria) == ["ignore", "isolate", "block"]
    assert bound.wire_to_caller == {key: key for key in ["ignore", "isolate", "block"]}
    assert build_row(bound, "alert 1")["question"]["criteria"] == bound.criteria


def test_fixed_subset_labels_without_labels_offer_every_fixed_label() -> None:
    bound = small_binding(FIXED_SUBSET, None)
    assert bound.criteria == ACTIONS
    assert list(bound.criteria) == list(ACTIONS)


@pytest.mark.parametrize("labels,match", [
    ([], "empty"),
    (["block", "shutdown"], "shutdown"),
    (["block", "isolate", "block"], "more than once"),
    ({"block": "x"}, "list of keys"),
    ("block", "list of keys"),
])
def test_fixed_subset_labels_errors(labels, match: str) -> None:
    with pytest.raises(InvalidLabels, match=match):
        small_binding(FIXED_SUBSET, labels)


def test_fixed_labels_without_subset_refuse_a_subset() -> None:
    with pytest.raises(InvalidLabels, match="fixed"):
        small_binding({"source": "fixed", "fixed": ACTIONS, "subset": False}, ["block"])


@pytest.mark.parametrize("labels", [{"block"}, iter(["block"]), [1], ["block", None], [["block"]]])
def test_fixed_subset_labels_must_be_a_list_of_strings(labels) -> None:
    with pytest.raises(InvalidLabels, match="list of keys"):
        small_binding(FIXED_SUBSET, labels)


@pytest.mark.parametrize("labels", [{"block"}, [1], [["ignore"]]])
def test_subset_labels_must_be_a_list_of_strings(labels) -> None:
    with pytest.raises(InvalidLabels, match="list of keys"):
        small_binding(SUBSET, labels)


def test_fixed_subset_labels_may_be_a_tuple() -> None:
    assert list(small_binding(FIXED_SUBSET, ("block", "ignore")).criteria or {}) == ["ignore", "block"]
