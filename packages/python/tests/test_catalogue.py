from pathlib import Path

import pytest

from jeff.catalogue import Catalogue
from jeff.errors import UnknownFunction, ManifestError

FIXTURES = Path(__file__).parent / "fixtures" / "catalogue"


@pytest.fixture
def catalogue() -> Catalogue:
    return Catalogue.from_dir(FIXTURES)


def test_lookup_by_id_alias_and_version(catalogue: Catalogue) -> None:
    assert catalogue.function("jeff.support.triage.route").key == "route"
    assert catalogue.function("triage.route").id == "jeff.support.triage.route"
    assert catalogue.function("jeff.support.triage.route@1.3").id == "jeff.support.triage.route"
    assert catalogue.adapter("spam").id == "jeff.safety.spam"


def test_wrong_version_names_the_one_available(catalogue: Catalogue) -> None:
    with pytest.raises(UnknownFunction, match="1.3.0"):
        catalogue.function("jeff.support.triage.route@2")


def test_unknown_function_lists_the_adapters_functions(catalogue: Catalogue) -> None:
    with pytest.raises(UnknownFunction, match="route, urgency, needs_human"):
        catalogue.function("jeff.support.triage.team")


def test_adapter_reference_is_not_a_function(catalogue: Catalogue) -> None:
    with pytest.raises(UnknownFunction, match="has 3 functions"):
        catalogue.function("jeff.support.triage")


def test_single_function_adapter_reference_is_its_function(catalogue: Catalogue) -> None:
    assert catalogue.function("spam").id == "jeff.safety.spam.message_type"


def test_implementing(catalogue: Catalogue) -> None:
    assert [f.id for f in catalogue.implementing("classify")] == ["jeff.safety.spam.message_type"]


def test_base_lookup(catalogue: Catalogue) -> None:
    assert catalogue.base("jeff-base", "v1.3") == {"repo": "mstrasser/jeff-base", "revision": "v1.3",
                                                   "released": "2026-10-05"}
    with pytest.raises(ManifestError, match="v9"):
        catalogue.base("jeff-base", "v9")


def test_duplicate_alias_is_refused(catalogue: Catalogue) -> None:
    manifests = catalogue.manifests()
    triage = next(m for m in manifests if m.id == "jeff.support.triage")
    with pytest.raises(ManifestError, match="triage"):
        Catalogue(manifests + [triage], {})


def test_without_drops_an_adapter_its_id_and_its_aliases(catalogue: Catalogue) -> None:
    smaller = catalogue.without(["jeff.safety.spam"])
    assert [manifest.id for manifest in smaller.manifests()] == ["jeff.support.triage"]
    for ref in ("spam.message_type", "jeff.safety.spam.message_type"):
        with pytest.raises(UnknownFunction, match="No adapter"):
            smaller.function(ref)
    assert smaller.base_versions() == catalogue.base_versions()
    assert catalogue.adapter("spam").id == "jeff.safety.spam"  # the catalogue itself is unchanged


def test_without_an_id_the_catalogue_does_not_have_is_refused(catalogue: Catalogue) -> None:
    with pytest.raises(ManifestError, match="jeff.nothing.here"):
        catalogue.without(["jeff.nothing.here"])
