"""contract/conformance/ (written by scripts/record_conformance.py) matches what the Python SDK does now. The
catalogues, rows, decisions and socket conversations are recorded again in memory and compared with the files; the
remote exchanges are replayed against the server answers recorded in remote.json. After a deliberate change of
behaviour, record again with `uv run python scripts/record_conformance.py` and copy the files into the Python package
with `uv run python scripts/sync_shipped.py`."""

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

import record_conformance as rc


def load(name: str) -> Any:
    return json.loads((rc.OUT / name).read_text(encoding="utf-8"))


def fixture_text(out: Path) -> str:
    return "".join(path.read_text(encoding="utf-8") for path in sorted(out.glob("*.json")))


def every_fixture_text() -> str:
    return fixture_text(rc.OUT)


def test_catalogues_are_as_recorded() -> None:
    assert rc.plain(rc.catalogues_file()) == load("catalogues.json")


def test_refused_manifests_are_as_recorded() -> None:
    recorded = load("manifests.json")
    assert rc.plain(rc.manifests_file()) == recorded
    assert [case["expect"]["error"]["type"] for case in recorded["cases"]] == ["ManifestError"] * 10 + ["ContractError"] * 6


def test_rows_are_as_recorded() -> None:
    assert rc.plain(rc.rows_file()) == load("rows.json")


def test_decisions_are_as_recorded() -> None:
    assert rc.plain(rc.decisions_file()) == load("decisions.json")


def test_socket_conversations_are_as_recorded() -> None:
    assert rc.plain(rc.socket_file()) == load("socket.json")


def test_remote_exchanges_replay_as_recorded() -> None:
    """The Python RemoteJeff, answered with the server answers in remote.json, sends exactly the recorded requests
    and returns exactly the recorded results."""
    recorded = load("remote.json")
    assert rc.plain(rc.replay_remote(recorded)) == recorded


def test_every_decisions_group_has_cases() -> None:
    groups = {case["group"] for case in load("decisions.json")["cases"]}
    assert groups == {"decide", "escalation", "dispatch", "graph"}


def test_every_manifest_in_the_fixtures_is_open() -> None:
    catalogues = load("catalogues.json")
    manifests = [*catalogues["shipped"]["manifests"].values(),
                 *(raw for group in catalogues["variants"].values() for raw in group)]
    assert manifests
    assert all(raw["licence"]["kind"] == "open" for raw in manifests)


# Shorter strings ("email", "sms", "true", "benign", a single word or code) are not private text - they are common
# words or fixed labels that legitimately recur everywhere, public and private alike - so searching for them would
# make the guard fire on coincidence rather than on an actual leaked value. Only a string this long or longer is
# distinctive enough to mean something if it reappears.
MIN_PRIVATE_LENGTH = 30


def string_leaves(value: Any) -> list[str]:
    """Every string at a leaf position inside `value` (walking dicts and lists; a dict's keys are not leaves,
    only its values are, and neither numbers, booleans, null nor (for a raw manifest) a YAML date are)."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [leaf for item in value.values() for leaf in string_leaves(item)]
    if isinstance(value, list):
        return [leaf for item in value for leaf in string_leaves(item)]
    return []


def check_no_private_data(out: Path, golden_cases: list[dict[str, Any]] | None = None) -> None:
    """No golden case taken from a private test set (its adapter has no public test set) appears anywhere under
    `out` - not just rows.json, but every fixture file Tasks 1-4 write there (catalogues.json, and later
    decisions.json, socket.json, remote.json), whatever exists - neither its input nor any of its context values,
    walked to every string leaf. Only a value of at least MIN_PRIVATE_LENGTH characters is searched for (see its
    comment). The one exemption is a value that is, in full, an exact string leaf of some shipped open manifest
    (catalogue/'s hub.example and the like): that text is already public, and legitimately recurs in
    catalogues.json's "shipped" and "variants" sections regardless of any golden case. A value that merely
    overlaps with public text - a substring of it, or vice versa, without matching one whole leaf - is not
    exempted: it is still searched for and must not appear. No path of this machine appears in any fixture
    either.

    `golden_cases` defaults to the real contract/golden/step1.json cases; a test may pass a short synthetic list
    instead, to check the exemption and length rules precisely without depending on, or editing, real golden
    data."""
    text = fixture_text(out)
    assert str(rc.ROOT) not in text
    public_leaves = {leaf for raw in rc.shipped_raw().values() for leaf in string_leaves(raw)}
    shipped = rc.catalogue("shipped")
    if golden_cases is None:
        golden_cases = json.loads(rc.GOLDEN.read_text(encoding="utf-8"))["cases"]
    for case in golden_cases:
        call = case["call"]
        function = shipped.function(call["function"])
        if shipped.manifest_of(function).tests["published"] is not None:
            continue
        values = [*string_leaves(call["input"]), *string_leaves(call.get("context") or {})]
        for value in values:
            if len(value) < MIN_PRIVATE_LENGTH:
                continue
            if value in public_leaves:
                continue  # already public: an exact string the shipped manifest itself carries (e.g. hub.example)
            assert json.dumps(value, ensure_ascii=False) not in text, case["name"]


def test_the_fixtures_hold_no_private_data() -> None:
    check_no_private_data(rc.OUT)


def _plant_and_expect_a_catch(tmp_path: Path, call: dict[str, Any]) -> None:
    """A copy of contract/conformance/ (the real files are never touched) with `call`'s input planted into a new
    decisions.json - a stand-in for a Task 2-4 fixture, proving the guard scans every file, not rows.json alone -
    checked against the single synthetic golden case {"call": call} of a private adapter's function."""
    out = tmp_path / "conformance"
    shutil.copytree(rc.OUT, out)
    (out / "decisions.json").write_text(
        json.dumps({"description": "a stand-in for a Task 2-4 fixture", "planted_for_this_test": call["input"]},
                   ensure_ascii=False),
        encoding="utf-8")
    with pytest.raises(AssertionError):
        check_no_private_data(out, golden_cases=[{"name": "planted", "call": call}])


# Since aml left the catalogue (5 November 2026) every shipped adapter has a public test set, so these tests make soc
# look private (its manifest's tests.published set to None for the test's length) to check the guard.
PRIVATE_FUNCTION = "jeff.security.soc.action"


@pytest.fixture
def private_soc(monkeypatch: pytest.MonkeyPatch) -> None:
    shipped = rc.catalogue("shipped")
    monkeypatch.setitem(shipped.manifest_of(shipped.function(PRIVATE_FUNCTION)).tests, "published", None)


def test_the_guard_catches_a_private_value_planted_outside_rows_json(tmp_path: Path, private_soc: None) -> None:
    """The guard must scan every fixture file, not rows.json alone: planting a private golden value (one whose
    text is not already public, i.e. not an exact string leaf of any shipped manifest) in some other fixture -
    here a stand-in for a Task 2-4 file such as decisions.json - must still make check_no_private_data fail."""
    shipped = rc.catalogue("shipped")
    public_leaves = {leaf for raw in rc.shipped_raw().values() for leaf in string_leaves(raw)}

    def is_private_and_not_public(case: dict[str, Any]) -> bool:
        function = shipped.function(case["call"]["function"])
        if shipped.manifest_of(function).tests["published"] is not None:
            return False
        value = case["call"]["input"]
        return len(value) >= MIN_PRIVATE_LENGTH and value not in public_leaves

    cases = json.loads(rc.GOLDEN.read_text(encoding="utf-8"))["cases"]
    planted = next(case for case in cases if is_private_and_not_public(case))
    _plant_and_expect_a_catch(tmp_path, planted["call"])


def test_the_guard_catches_a_value_that_is_only_a_substring_of_public_text(tmp_path: Path, private_soc: None) -> None:
    """The exemption is exact: a value that merely overlaps with public text - here, a 40-character fragment cut
    from the middle of a real shipped manifest's own string (so the old substring-based exemption would have
    wrongly let it through) - is not a whole leaf of any manifest, so it is not exempted and the guard still
    catches it when planted into a fixture."""
    shipped = rc.catalogue("shipped")
    manifest = shipped.manifest_of(shipped.function(PRIVATE_FUNCTION))
    assert manifest.tests["published"] is None  # made private by the private_soc fixture
    long_leaves = [leaf for leaf in string_leaves(manifest.raw) if len(leaf) >= 80]
    assert long_leaves
    fragment = long_leaves[0][20:60]
    assert len(fragment) >= MIN_PRIVATE_LENGTH

    public_leaves = {leaf for raw in rc.shipped_raw().values() for leaf in string_leaves(raw)}
    assert fragment not in public_leaves  # a fragment, not a whole leaf: must not be exempted

    _plant_and_expect_a_catch(tmp_path, {"function": PRIVATE_FUNCTION, "input": fragment})


def test_a_value_under_the_minimum_length_is_not_searched(tmp_path: Path, private_soc: None) -> None:
    """A short value (here 20 characters, under MIN_PRIVATE_LENGTH's 30) is never searched for, even when it is
    neither public nor exempted and is planted right into a fixture: check_no_private_data must not raise."""
    short_value = "x" * (MIN_PRIVATE_LENGTH - 10)
    assert len(short_value) < MIN_PRIVATE_LENGTH

    shipped = rc.catalogue("shipped")
    manifest = shipped.manifest_of(shipped.function(PRIVATE_FUNCTION))
    assert manifest.tests["published"] is None  # made private by the private_soc fixture

    out = tmp_path / "conformance"
    shutil.copytree(rc.OUT, out)
    (out / "decisions.json").write_text(
        json.dumps({"description": "a stand-in for a Task 2-4 fixture", "planted_for_this_test": short_value}),
        encoding="utf-8")

    check_no_private_data(out, golden_cases=[{"name": "planted", "call":
                                              {"function": PRIVATE_FUNCTION, "input": short_value}}])
