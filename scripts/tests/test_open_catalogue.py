import pytest

from open_catalogue import open_manifest

SERVED = {
    "id": "jeff.reasoning.entails", "version": "1.4.0", "aliases": ["oss-entails", "entails"],
    "licence": {"kind": "proprietary", "adapter": "Scissero proprietary"},
    "weights": {"local": {}},
    "base": {"id": "jeff-base", "version": "v1.4", "max_options": 3},
    "functions": {"entails": {"interface": "is_supported", "calibration": {
        "data": "hf://datasets/teamsuzie/jeff-data@main/apps/oss-entails/calibration.jsonl",
        "data_mirror": {"path": "/mnt/d/jeff-data/apps/oss-entails/calibration.jsonl"}, "rows": 900}}},
    "tests": {"published": None, "calibration": None,
              "internal": "hf://datasets/teamsuzie/jeff-data@main/apps/oss-entails/test.jsonl",
              "internal_calibration": "hf://datasets/teamsuzie/jeff-data@main/apps/oss-entails/calibration.jsonl",
              "function_by": "only"},
    "hub": {"title": "oss-entails", "summary": "Jeff v1.4 adapter", "source": "local adapter folder oss-entails",
            "notes": ["from /mnt/d/..."]},
}


def test_open_manifest_points_at_public_locations():
    out = open_manifest(SERVED, None, "entails")
    assert out["licence"] == {"kind": "open", "adapter": "Apache-2.0"}
    assert out["weights"] == {"open": {"repo": "mstrasser/jeff-adapter-entails", "revision": "v1.4"}}
    assert out["tests"] == {"published": "hf://mstrasser/jeff-adapter-entails@v1.4/test.jsonl",
                            "calibration": "hf://mstrasser/jeff-adapter-entails@v1.4/calibration.jsonl",
                            "function_by": "only"}
    calibration = out["functions"]["entails"]["calibration"]
    assert calibration["data"] == "hf://mstrasser/jeff-adapter-entails@v1.4/calibration.jsonl"
    assert "data_mirror" not in calibration
    assert "teamsuzie" not in repr(out) and "/mnt/" not in repr(out)


def test_open_manifest_keeps_version_and_functions():
    out = open_manifest(SERVED, None, "entails")
    assert out["version"] == "1.4.0" and out["base"]["version"] == "v1.4"
    assert out["functions"]["entails"]["interface"] == "is_supported"


def test_open_manifest_carries_v13_hub_prose():
    v13 = {"licence": {"kind": "open", "adapter": "Apache-2.0"},
           "hub": {"title": "Prompt injection guard", "summary": "Checks text.", "task": "safety"}}
    out = open_manifest(SERVED, v13, "entails")
    assert out["hub"]["title"] == "Prompt injection guard" and out["hub"]["task"] == "safety"
    assert "notes" not in out["hub"] and "source" not in out["hub"]


def test_open_manifest_takes_only_prose_keys_from_the_v13_hub():
    """Everything else in the v1.3 hub (its own version, base, huggingface revision, data card, provenance,
    submission...) is about the v1.3 release, not v1.4; only title/summary/task/kind/maintainer are prose that
    still applies and is carried over. Everything else in the result comes from the served v1.4 hub."""
    v13 = {"licence": {"kind": "open", "adapter": "Apache-2.0"},
           "hub": {"title": "Prompt injection guard", "summary": "Checks text.", "task": "safety",
                   "version": "1.3.0", "huggingface": {"revision": "v1.3"},
                   "base": {"version": "v1.3", "max_options": 254},
                   "data_card": {"training_data": "not published"}}}
    out = open_manifest(SERVED, v13, "entails")
    assert out["hub"]["title"] == "Prompt injection guard" and out["hub"]["task"] == "safety"
    for key in ("version", "huggingface", "base", "data_card", "provenance", "submission"):
        assert key not in out["hub"], key


def test_open_manifest_refuses_a_name_with_a_slash():
    with pytest.raises(ValueError, match="public name"):
        open_manifest(SERVED, None, "mstrasser/entails")


def test_open_manifest_is_apache_without_a_v13_manifest():
    assert open_manifest(SERVED, None, "entails")["licence"] == {"kind": "open", "adapter": "Apache-2.0"}


def test_open_manifest_keeps_the_v13_licence_text():
    v13 = {"licence": {"kind": "open", "adapter": "To be decided (trained on data licensed for non-commercial use)"},
           "hub": {"title": "Sanctions screening"}}
    out = open_manifest(SERVED, v13, "sanctions")
    assert out["licence"] == {"kind": "open",
                              "adapter": "To be decided (trained on data licensed for non-commercial use)"}


def test_open_manifest_adds_the_short_name_its_local_calls_use():
    """The hub and Jeff.local name an open adapter by the last part of its ID; a served manifest without that alias
    (the open router's aliases were chosen before it was opened) gets it, after its own aliases."""
    served = {**SERVED, "id": "jeff.routing.router-open-v14",
              "aliases": ["jeff-router-open-v14", "jeff.routing.router-open"]}
    out = open_manifest(served, None, "router-open-v14")
    assert out["aliases"] == ["jeff-router-open-v14", "jeff.routing.router-open", "router-open-v14"]
    assert served["aliases"] == ["jeff-router-open-v14", "jeff.routing.router-open"]
    assert open_manifest(SERVED, None, "entails")["aliases"] == ["oss-entails", "entails"]
