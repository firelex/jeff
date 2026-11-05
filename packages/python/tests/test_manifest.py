import copy
from pathlib import Path

import pytest
import yaml

from jeff.errors import ManifestError, ContractError
from jeff.ids import parse_ref, version_matches
from jeff.manifest import function_of_test_row, load_manifest, manifest_from_dict, unversioned_id

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"
TRIAGE = FIXTURES / "jeff.support.triage" / "manifest.yaml"


def raw() -> dict:
    return yaml.safe_load(TRIAGE.read_text())


def test_parse_ref_full_and_alias() -> None:
    ref = parse_ref("jeff.support.triage.route@1.3")
    assert (ref.adapter, ref.key, ref.version) == ("jeff.support.triage", "route", "1.3")
    alias = parse_ref("triage.route")
    assert (alias.adapter, alias.key, alias.version) == ("triage", "route", None)
    adapter_only = parse_ref("jeff.support.triage")
    assert (adapter_only.adapter, adapter_only.key) == ("jeff.support.triage", None)


@pytest.mark.parametrize("wanted,actual,ok", [("1.3", "1.3.4", True), ("1", "1.9.0", True), ("1.3.0", "1.3.0", True),
                                              ("1.3.0", "1.3.1", False), ("2", "1.3.0", False)])
def test_version_matches(wanted: str, actual: str, ok: bool) -> None:
    assert version_matches(wanted, actual) is ok


def test_load_manifest_reads_functions_in_order() -> None:
    manifest = load_manifest(TRIAGE)
    assert manifest.id == "jeff.support.triage"
    assert list(manifest.functions) == ["route", "urgency", "needs_human"]
    route = manifest.functions["route"]
    assert route.id == "jeff.support.triage.route"
    assert route.labels.fixed_first == {"other": "Not for any of these teams"}
    assert route.labels.caller_keys == "k{n}"
    assert manifest.functions["urgency"].calibration is not None
    assert [field.key for field in manifest.context] == ["company", "channel"]


def test_interface_type_must_match() -> None:
    document = raw()
    document["functions"]["urgency"]["interface"] = "classify"
    with pytest.raises(ManifestError, match="urgency.*classify.*choice"):
        manifest_from_dict(document, TRIAGE)


def test_unknown_interface_is_refused() -> None:
    document = raw()
    document["functions"]["route"]["interface"] = "entails"
    with pytest.raises(ManifestError, match="entails"):
        manifest_from_dict(document, TRIAGE)


def test_abstain_labels_must_be_labels() -> None:
    document = raw()
    document["functions"]["route"]["abstain_labels"] = ["unsure"]
    with pytest.raises(ManifestError, match="unsure"):
        manifest_from_dict(document, TRIAGE)


def test_caller_labels_need_a_max() -> None:
    document = raw()
    del document["functions"]["route"]["labels"]["max"]
    with pytest.raises(ManifestError, match="max"):
        manifest_from_dict(document, TRIAGE)


def test_label_count_cannot_exceed_base() -> None:
    document = raw()
    document["functions"]["route"]["labels"]["max"] = 252
    with pytest.raises(ManifestError, match="252"):
        manifest_from_dict(document, TRIAGE)


def test_schema_problems_are_contract_errors() -> None:
    document = raw()
    document["version"] = "one"
    with pytest.raises(ContractError, match="version"):
        manifest_from_dict(document, TRIAGE)


def test_router_labels_must_equal_routes_to() -> None:
    router = {
        "id": "jeff.support.router", "version": "0.1.0", "aliases": [], "licence": {"kind": "open", "adapter": "Apache-2.0"},
        "weights": {"open": {"repo": "x/y", "revision": "v1"}},
        "base": {"id": "jeff-base", "version": "v1.3", "max_options": 252}, "context": [],
        "input": {"key": "message", "description": "The message.", "form": "fields"},
        "functions": {"target": {"interface": "route", "type": "choice", "instructions": "Which function?",
                                 "labels": {"source": "fixed", "fixed": {"jeff.support.triage.route": "Teams"},
                                            "subset": False},
                                 "abstain_labels": [], "calibration": None}},
        "routes_to": ["jeff.safety.spam.message_type"],
        "tests": {"published": None, "calibration": None, "function_by": "only"},
    }
    with pytest.raises(ManifestError, match="routes_to"):
        manifest_from_dict(copy.deepcopy(router), TRIAGE)


def test_function_of_test_row_by_id_suffix_with_dash_or_colon() -> None:
    manifest = load_manifest(TRIAGE)
    assert function_of_test_row(manifest, {"id": "triage-c000-m063-route", "question": {"type": "choice"}}).key == "route"
    assert function_of_test_row(manifest, {"id": "triage:x:urgency", "question": {"type": "score"}}).key == "urgency"
    with pytest.raises(ManifestError, match="does not end with"):
        function_of_test_row(manifest, {"id": "triage-c000-m063", "question": {"type": "choice"}})


def test_function_of_test_row_by_question_type() -> None:
    body = raw()
    del body["functions"]["urgency"]
    body["tests"]["function_by"] = "question_type"
    manifest = manifest_from_dict(body, TRIAGE)
    assert function_of_test_row(manifest, {"id": "a:noul", "question": {"type": "noul"}}).key == "needs_human"
    assert function_of_test_row(manifest, {"id": "a:choice", "question": {"type": "choice"}}).key == "route"
    with pytest.raises(ManifestError, match="no single function of type score"):
        function_of_test_row(manifest, {"id": "a", "question": {"type": "score"}})


def test_question_type_needs_one_function_per_type() -> None:
    body = raw()
    body["functions"]["urgency"]["type"], body["functions"]["urgency"]["interface"] = "noul", "detect"
    body["functions"]["urgency"]["labels"] = {"source": "fixed"}
    body["tests"]["function_by"] = "question_type"
    with pytest.raises(ManifestError, match="question_type"):
        manifest_from_dict(body, TRIAGE)


def test_tests_may_name_an_internal_test_set() -> None:
    body = raw()
    body["tests"]["internal"] = "hf://datasets/teamsuzie/jeff-data@main/v1.3/adapters/triage/test.jsonl"
    body["tests"]["internal_calibration"] = None
    assert manifest_from_dict(body, TRIAGE).tests["internal"].startswith("hf://datasets/")



@pytest.mark.parametrize("ref", ["", "jeff..triage", "jeff.support", "jeff.support.triage.route.x", "triage.route.x",
                                 "triage."])
def test_malformed_references_raise_invalid_reference(ref: str) -> None:
    from jeff.errors import InvalidReference, JeffError
    with pytest.raises(InvalidReference):
        parse_ref(ref)
    assert issubclass(InvalidReference, JeffError)


@pytest.mark.parametrize("adapter_id", ["jeff.support.triage.extra", "jeff.support"])
def test_adapter_ids_have_exactly_three_parts(adapter_id: str) -> None:
    document = raw()
    document["id"] = adapter_id
    with pytest.raises(ContractError, match="/id"):
        manifest_from_dict(document, TRIAGE)


def test_aliases_may_not_contain_dots() -> None:
    document = raw()
    document["aliases"] = ["support.triage"]
    with pytest.raises(ContractError, match="aliases"):
        manifest_from_dict(document, TRIAGE)


@pytest.mark.parametrize("alias", ["support.triage.route", "jeff.support", "Jeff.support.triage"])
def test_an_alias_is_a_short_name_or_of_the_id_form_only(alias: str) -> None:
    document = raw()
    document["aliases"] = [alias]
    with pytest.raises(ContractError, match="aliases"):
        manifest_from_dict(document, TRIAGE)


@pytest.mark.parametrize(("adapter_id", "unversioned"), [
    ("jeff.example.router-v14-fast", "jeff.example.router-fast"),  # the version part between other segments
    ("jeff.routing.router-open-v14", "jeff.routing.router-open"),
    ("jeff.v2.router-v3", "jeff.v2.router"),  # only the name carries the version part
    ("jeff.support.triage", None),
    ("jeff.routing.v14", None),  # nothing besides the version part
    ("jeff.routing.router-v14-v15", None),  # two version parts
    ("jeff.routing.router-v", None),
    ("jeff.routing.router-v1x", None),
    ("jeff.routing.router-14", None),
    ("jeff.routing.router-open-v14\n", None),  # a line break after the digits is not part of a version part
])
def test_unversioned_id_removes_the_one_version_segment(adapter_id: str, unversioned: str | None) -> None:
    assert unversioned_id(adapter_id) == unversioned


def test_an_adapter_whose_id_has_a_version_part_answers_to_its_unversioned_id() -> None:
    from jeff.catalogue import Catalogue
    document = raw()
    document["id"], document["aliases"] = "jeff.support.triage-v14", ["triage", "jeff.support.triage"]
    manifest = manifest_from_dict(document, TRIAGE)
    catalogue = Catalogue([manifest], {})
    assert catalogue.adapter("jeff.support.triage") is manifest
    assert catalogue.adapter("jeff.support.triage@1.3") is manifest
    assert catalogue.function("jeff.support.triage.route").id == "jeff.support.triage-v14.route"
    assert catalogue.function("jeff.support.triage-v14.route").id == "jeff.support.triage-v14.route"


@pytest.mark.parametrize(("adapter_id", "alias", "match"), [
    ("jeff.support.triage", "jeff.support.helpdesk", "jeff.support.triage has no version part"),
    ("jeff.support.triage-v14", "jeff.support.triage-v15", "so it must be jeff.support.triage, the ID"),
    ("jeff.support.triage-v14", "jeff.support.triage-v14", "so it must be jeff.support.triage, the ID"),
    ("jeff.support.triage-v14", "jeff.other.triage", "so it must be jeff.support.triage, the ID"),
    ("jeff.support.triage-v14-v2", "jeff.support.triage", r"has more than one version part \(v14, v2\)"),
    ("jeff.support.v14", "jeff.support.triage", "the name of jeff.support.v14 is only a version part"),
])
def test_an_alias_of_the_id_form_must_be_the_unversioned_id(adapter_id: str, alias: str, match: str) -> None:
    document = raw()
    document["id"], document["aliases"] = adapter_id, [alias]
    with pytest.raises(ManifestError, match=match):
        manifest_from_dict(document, TRIAGE)


@pytest.mark.parametrize("aliases", [["triage", "triage"], ["triage", "jeff.support.triage", "jeff.support.triage"]])
def test_an_alias_listed_twice_is_refused(aliases: list[str]) -> None:
    document = raw()
    document["id"], document["aliases"] = "jeff.support.triage-v14", aliases
    with pytest.raises(ManifestError, match=f"the alias {aliases[-1]} is listed twice"):
        manifest_from_dict(document, TRIAGE)


def test_an_unversioned_id_that_is_another_adapters_id_is_refused_by_the_catalogue() -> None:
    from jeff.catalogue import Catalogue
    plain, versioned = raw(), raw()
    versioned["id"], versioned["aliases"] = "jeff.support.triage-v14", ["jeff.support.triage"]
    with pytest.raises(ManifestError, match="the name jeff.support.triage is already used by another adapter"):
        Catalogue([manifest_from_dict(plain, TRIAGE), manifest_from_dict(versioned, TRIAGE)], {})


def test_every_catalogue_manifest_still_validates() -> None:
    """34 adapters served openly at v1.4 (catalogue-imports/open-set.yaml's v14: 31, plus knowledge-qa,
    assistant-routing and router-open-v14, opened on 4 November 2026). The 2 kept at their v1.3 release (aml, nav)
    left on 5 November 2026: only v1.4 stays on Hugging Face."""
    from jeff.catalogue import Catalogue
    catalogue = Catalogue.from_dir(Path(__file__).resolve().parents[3] / "catalogue")
    assert len(catalogue.manifests()) == 34


def router_manifest(labels: dict, routes_to: list[str]) -> dict:
    return {
        "id": "jeff.support.router", "version": "0.1.0", "aliases": [], "licence": {"kind": "open", "adapter": "Apache-2.0"},
        "weights": {"open": {"repo": "x/y", "revision": "v1"}},
        "base": {"id": "jeff-base", "version": "v1.3", "max_options": 252}, "context": [],
        "input": {"key": "message", "description": "The message.", "form": "fields"},
        "functions": {"target": {"interface": "route", "type": "choice", "instructions": "Which function?",
                                 "labels": labels, "abstain_labels": [], "calibration": None}},
        "routes_to": routes_to,
        "tests": {"published": None, "calibration": None, "function_by": "only"},
    }


def test_router_labels_are_a_subset_over_routes_to() -> None:
    targets = {"jeff.support.triage.route": "Teams", "jeff.safety.spam": "Spam"}
    ok = manifest_from_dict(router_manifest({"source": "subset", "fixed": targets}, list(targets)), TRIAGE)
    assert ok.is_router
    with pytest.raises(ManifestError, match="subset"):
        manifest_from_dict(router_manifest({"source": "fixed", "fixed": targets, "subset": False}, list(targets)), TRIAGE)
    with pytest.raises(ManifestError, match="required"):
        manifest_from_dict(router_manifest({"source": "subset", "fixed": targets,
                                            "required": ["jeff.safety.spam"]}, list(targets)), TRIAGE)


def kinds_manifest(function_by, kinds=None) -> dict:
    """Two choice functions and one noul function, told apart by the row's source.kind."""
    body = raw()
    body["functions"] = {
        "doc_type": {"interface": "classify", "type": "choice", "instructions": "Which document?",
                     "labels": {"source": "fixed", "fixed": {"passport": None, "bill": None}, "subset": False},
                     "abstain_labels": [], "calibration": None},
        "outcome": {"interface": "classify", "type": "choice", "instructions": "Which outcome?",
                    "labels": {"source": "fixed", "fixed": {"pass": None, "fail": None}, "subset": False},
                    "abstain_labels": [], "calibration": None},
        "edd_needed": {"interface": "detect", "type": "noul", "instructions": "Is EDD needed?",
                       "labels": {"source": "fixed"}, "abstain_labels": [], "calibration": None},
    }
    body["tests"]["function_by"] = function_by
    if kinds is not None:
        body["tests"]["kinds"] = kinds
    return body


def kind_row(kind: str | None, question_type: str = "choice", row_id: str = "kyc-1") -> dict:
    row = {"id": row_id, "question": {"type": question_type}, "source": {}}
    if kind is not None:
        row["source"]["kind"] = kind
    return row


def test_function_by_source_kind_names_the_function() -> None:
    manifest = manifest_from_dict(kinds_manifest("source_kind"), TRIAGE)
    assert function_of_test_row(manifest, kind_row("outcome")).key == "outcome"
    assert function_of_test_row(manifest, kind_row("edd_needed", "noul")).key == "edd_needed"


def test_function_by_source_kind_refuses_a_row_without_a_kind_or_with_an_unknown_kind() -> None:
    manifest = manifest_from_dict(kinds_manifest("source_kind"), TRIAGE)
    with pytest.raises(ManifestError, match="kyc-7.*source.kind"):
        function_of_test_row(manifest, kind_row(None, row_id="kyc-7"))
    with pytest.raises(ManifestError, match="kyc-8.*risk_rating"):
        function_of_test_row(manifest, kind_row("risk_rating", row_id="kyc-8"))


def test_tests_kinds_maps_kind_values_to_function_keys() -> None:
    manifest = manifest_from_dict(kinds_manifest("source_kind", {"doc-type": "doc_type", "outcome": "outcome",
                                                                 "edd": "edd_needed"}), TRIAGE)
    assert function_of_test_row(manifest, kind_row("doc-type")).key == "doc_type"
    assert function_of_test_row(manifest, kind_row("edd", "noul")).key == "edd_needed"
    with pytest.raises(ManifestError, match="kyc-9.*'outcome-2'.*tests.kinds"):
        function_of_test_row(manifest, kind_row("outcome-2", row_id="kyc-9"))


@pytest.mark.parametrize("function_by,kinds,match", [
    ("id_suffix", {"doc-type": "doc_type"}, "kinds.*source_kind"),
    ("source_kind", {"doc-type": "doc_types"}, "doc_types"),
])
def test_tests_kinds_is_checked(function_by, kinds: dict, match: str) -> None:
    with pytest.raises(ManifestError, match=match):
        manifest_from_dict(kinds_manifest(function_by, kinds), TRIAGE)


def test_function_by_criteria_narrow_in_order() -> None:
    manifest = manifest_from_dict(kinds_manifest(["question_type", "id_suffix"]), TRIAGE)
    assert function_of_test_row(manifest, {"id": "kyc-1-outcome", "question": {"type": "choice"}}).key == "outcome"
    assert function_of_test_row(manifest, {"id": "kyc-1-edd_needed", "question": {"type": "noul"}}).key == "edd_needed"
    with pytest.raises(ManifestError, match="kyc-1 matches no function: after id_suffix"):
        function_of_test_row(manifest, {"id": "kyc-1", "question": {"type": "noul"}})
    with pytest.raises(ManifestError, match="kyc-2.*doc_type.*outcome"):
        function_of_test_row(manifest, {"id": "kyc-2", "question": {"type": "choice"}})


def test_function_by_criteria_refuse_a_row_that_no_function_matches() -> None:
    manifest = manifest_from_dict(kinds_manifest(["question_type", "source_kind"]), TRIAGE)
    assert function_of_test_row(manifest, kind_row("doc_type")).key == "doc_type"
    with pytest.raises(ManifestError, match="kyc-3"):
        function_of_test_row(manifest, kind_row("edd_needed", "choice", row_id="kyc-3"))


ID_PATTERNS = {"doc_type": r"kyc-\d+-doc-.+", "outcome": r"kyc-\d+-(outcome|result)", "edd_needed": r"kyc-\d+-edd"}


def test_function_by_id_pattern_matches_the_whole_row_id() -> None:
    """id_pattern: for row ids that do not end with the function key (an adapter's verdict ids end with a brand name,
    say), each function's regular expression in tests.id_patterns must match the whole id."""
    body = kinds_manifest("id_pattern")
    body["tests"]["id_patterns"] = dict(ID_PATTERNS)
    manifest = manifest_from_dict(body, TRIAGE)
    assert function_of_test_row(manifest, {"id": "kyc-1-doc-Acme Ltd", "question": {"type": "choice"}}).key \
        == "doc_type"
    assert function_of_test_row(manifest, {"id": "kyc-2-result", "question": {"type": "choice"}}).key == "outcome"
    with pytest.raises(ManifestError, match="kyc-3-edd-x matches no function: after id_pattern"):
        function_of_test_row(manifest, {"id": "kyc-3-edd-x", "question": {"type": "noul"}})


def test_id_patterns_may_overlap_when_a_later_criterion_tells_the_functions_apart() -> None:
    body = kinds_manifest(["id_pattern", "question_type"])
    body["tests"]["id_patterns"] = {**ID_PATTERNS, "edd_needed": r"kyc-\d+-(outcome|result)"}
    manifest = manifest_from_dict(body, TRIAGE)
    assert function_of_test_row(manifest, {"id": "kyc-4-outcome", "question": {"type": "noul"}}).key == "edd_needed"
    assert function_of_test_row(manifest, {"id": "kyc-4-outcome", "question": {"type": "choice"}}).key == "outcome"
    body["tests"]["function_by"] = "id_pattern"
    manifest = manifest_from_dict(body, TRIAGE)
    with pytest.raises(ManifestError, match="kyc-4-outcome matches several functions \\(outcome, edd_needed\\)"):
        function_of_test_row(manifest, {"id": "kyc-4-outcome", "question": {"type": "noul"}})


@pytest.mark.parametrize("function_by,patterns,match", [
    ("id_pattern", None, "uses id_pattern, so tests.id_patterns must give"),
    ("id_suffix", ID_PATTERNS, "id_patterns is given, but tests.function_by does not use id_pattern"),
    ("id_pattern", {key: ID_PATTERNS[key] for key in ("doc_type", "outcome")}, "must name every function"),
    ("id_pattern", {"outcome": ID_PATTERNS["outcome"], "doc_type": ID_PATTERNS["doc_type"],
                    "edd_needed": ID_PATTERNS["edd_needed"]}, "in the functions' order"),
    ("id_pattern", {**ID_PATTERNS, "edd_needed": "kyc-(edd"}, "edd_needed: 'kyc-\\(edd' is not a regular expression"),
])
def test_tests_id_patterns_is_checked_when_the_manifest_loads(function_by, patterns, match: str) -> None:
    body = kinds_manifest(function_by)
    if patterns is not None:
        body["tests"]["id_patterns"] = patterns
    with pytest.raises(ManifestError, match=match):
        manifest_from_dict(body, TRIAGE)


def test_id_pattern_without_patterns_is_refused_when_mapping() -> None:
    from jeff.manifest import function_key_of_test_row
    with pytest.raises(ManifestError, match="uses id_pattern, but no tests.id_patterns were given"):
        function_key_of_test_row("x", "id_pattern", {"a": "choice"}, {"id": "r", "question": {"type": "choice"}},
                                 None, None, None)


@pytest.mark.parametrize("function_by", [[], ["only"], ["source_kind", "source_kind"], "by_magic"])
def test_function_by_list_is_checked_by_the_schema(function_by) -> None:
    with pytest.raises(ContractError, match="function_by"):
        manifest_from_dict(kinds_manifest(function_by), TRIAGE)


def test_fixed_choice_labels_must_say_whether_a_subset_is_allowed() -> None:
    body = kinds_manifest("source_kind")
    del body["functions"]["outcome"]["labels"]["subset"]
    with pytest.raises(ContractError, match="subset"):
        manifest_from_dict(body, TRIAGE)


@pytest.mark.parametrize("function,labels", [
    ("edd_needed", {"source": "fixed", "subset": False}),
    ("doc_type", {"source": "subset", "fixed": {"passport": None, "bill": None}, "subset": True}),
])
def test_subset_flag_belongs_only_to_fixed_choice_labels(function: str, labels: dict) -> None:
    body = kinds_manifest("source_kind")
    body["functions"][function]["labels"] = labels
    with pytest.raises(ContractError, match="subset"):
        manifest_from_dict(body, TRIAGE)


def test_fixed_subset_flag_is_read() -> None:
    body = kinds_manifest("source_kind")
    body["functions"]["outcome"]["labels"]["subset"] = True
    manifest = manifest_from_dict(body, TRIAGE)
    assert manifest.functions["outcome"].labels.subset is True
    assert manifest.functions["doc_type"].labels.subset is False
    assert manifest.functions["edd_needed"].labels.subset is None


def test_an_unknown_single_function_by_keeps_its_old_error() -> None:
    from jeff.manifest import function_key_of_test_row
    with pytest.raises(ManifestError, match=r"^x: unknown tests.function_by 'by_magic'$"):
        function_key_of_test_row("x", "by_magic", {"a": "choice"}, {"id": "r", "question": {"type": "choice"}}, None,
                                 None, None)


# ---- contract/conformance/manifests.json: manifests every SDK refuses

REFUSED = Path(__file__).resolve().parents[3] / "contract" / "conformance" / "manifests.json"


@pytest.mark.parametrize("case", __import__("json").loads(REFUSED.read_text(encoding="utf-8"))["cases"],
                         ids=lambda case: case["name"])
def test_a_manifest_with_a_number_that_is_not_finite_is_refused_naming_the_field_and_the_value(case: dict) -> None:
    with pytest.raises((ManifestError, ContractError)) as raised:
        manifest_from_dict(yaml.safe_load(case["yaml"]), Path(case["path"]))
    assert {"type": type(raised.value).__name__, "message": str(raised.value)} == {
        "type": case["expect"]["error"]["type"], "message": case["expect"]["error"]["message"]}


@pytest.mark.parametrize("access", ["owner", {"accounts": ["acct1"]}, {"accounts": ["acct_1", "B-2"]}])
def test_a_manifest_with_access_is_read_and_access_is_kept_only_in_its_raw_form(access: object) -> None:
    """access is for a server only: the SDK accepts it and does nothing with it."""
    manifest = raw()
    manifest["access"] = access
    assert manifest_from_dict(manifest, Path("x")).raw["access"] == access


def test_a_number_that_is_not_finite_is_refused_wherever_it_is() -> None:
    manifest = raw()
    manifest["changelog"] = [{"note": float("nan")}]
    with pytest.raises(ManifestError, match=r"x: /changelog/0/note is nan, not a finite number"):
        manifest_from_dict(manifest, Path("x"))
