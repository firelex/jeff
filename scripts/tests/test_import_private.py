"""scripts/import_private.py on a fake Hugging Face tree (no network), and the helpers it shares with
import_jeffhub.py, checked against the outputs the helpers gave before they moved (fixtures/jeffhub_helpers.json)."""

import json
from pathlib import Path
from typing import Any

import pytest
import yaml

import catalogue_import_common as common
import import_private
from jeff.manifest import manifest_from_dict

BASE_SHA = "b" * 64


@pytest.fixture(autouse=True)
def fake_bases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bases.yaml whose base is on Hugging Face with no recorded model_sha256, so the importer compares an adapter's
    base by the repo's listing (FakeHub.listed). The real catalogue/bases.yaml records the SHA-256 of its only base."""
    bases = tmp_path / "fake-bases.yaml"
    bases.write_text(yaml.safe_dump({"jeff-base": {
        "v1.3": {"repo": "mstrasser/jeff-base", "revision": "v1.3", "released": "2026-10-05"}}}))
    monkeypatch.setattr(import_private, "BASES", bases)
ADAPTER_FILES = ["adapter_config.json", "adapter_model.safetensors", "decision_config.json", "readout.safetensors"]
TICKERS = [f"T{n:02d}" for n in range(60)]  # more companies than the fake base's 50 options
KINDS = {"halt": "A trading halt.", "results": "Results.", "other": "Anything else."}
URGENT = {"true": "A person must read it now.", "false": "It can wait."}
INSTRUCTIONS = {"company": "Which company is the message about?", "kind": "What kind of event is it?",
                "urgent": "Must a person read this message now?"}


def rows_for(split: str, count: int, shape: tuple[str, ...] = ("watchlist", "message")) -> list[dict[str, Any]]:
    rows = []
    for n in range(count):
        watchlist = TICKERS[n * 7 % 60:n * 7 % 60 + 5 + n % 3]
        state = {"watchlist": [f"{t}: Company {t}" for t in watchlist], "message": f"{split} message {n}"}
        state = {key: state[key] for key in shape}
        company = {t: f"{t}: Company {t}" for t in watchlist}
        company = dict(list(company.items())[:n % 4] + [("none", "None of them.")] + list(company.items())[n % 4:])
        kinds = dict(list(KINDS.items())[n % 3:] + list(KINDS.items())[:n % 3])  # shuffled, as in training
        for key, criteria, kind in (("company", company, "choice"), ("kind", kinds, "choice"),
                                    ("urgent", URGENT, "noul")):
            rows.append({"id": f"{split}-{n:03d}-{key}", "state": state,
                         "question": {"type": kind, "instructions": INSTRUCTIONS[key], "criteria": criteria},
                         "label": next(iter(criteria))})
    return rows


class FakeHub:
    """teamsuzie/jeff-adapters, teamsuzie/jeff-data and the base, as local files plus a listing with LFS hashes."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.listed: dict[tuple[str, str, str], dict[str, str | None]] = {
            ("mstrasser/jeff-base", "model", "v1.3"): {"model.safetensors": "a" * 64, "config.json": None}}
        self.fetched: list[tuple[str, str, str]] = []

    def put(self, repo: str, repo_type: str, path: str, text: str, sha: str | None = None) -> None:
        target = self.root / repo_type / repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        self.listed.setdefault((repo, repo_type, "main"), {})[path] = sha

    def drop(self, repo: str, repo_type: str, path: str) -> None:
        (self.root / repo_type / repo / path).unlink()
        del self.listed[(repo, repo_type, "main")][path]

    def fetch(self, repo: str, path: str, repo_type: str, revision: str) -> Path:
        self.fetched.append((repo, path, repo_type))
        target = self.root / repo_type / repo / ("" if revision == "main" else f"@{revision}") / path
        if not target.exists():
            raise FileNotFoundError(target)
        return target

    def listing(self, repo: str, repo_type: str, revision: str) -> dict[str, str | None]:
        return self.listed[(repo, repo_type, revision)]


def make_hub(tmp_path: Path, test_rows: list[dict] | None = None,
             calibration_rows: list[dict] | None = None) -> FakeHub:
    hub = FakeHub(tmp_path / "hub")
    hub.listed[("mstrasser/jeff-base", "model", "v1.3")]["model.safetensors"] = BASE_SHA
    config = {"max_options": 50, "adapter": {"base_checkpoint": {"weights_sha256": {"model.safetensors": BASE_SHA}}}}
    for name in ADAPTER_FILES:
        text = json.dumps(config) if name == "decision_config.json" else "weights"
        hub.put("teamsuzie/jeff-adapters", "model", f"apps/desk/{name}", text,
                "c" * 64 if name.endswith(".safetensors") else None)
    hub.put("teamsuzie/jeff-adapters", "model", "apps/desk/README.md", "# Desk news adapter\n\nReads messages.\n")
    for split, rows in (("test", test_rows or rows_for("test", 12)), ("calibration", calibration_rows or rows_for("calibration", 6))):
        hub.put("teamsuzie/jeff-data", "dataset", f"apps/desk/{split}.jsonl",
                "".join(json.dumps(row) + "\n" for row in rows))
    return hub


# rows_for's company rows offer `none` at varying positions and its kind rows shuffle the kinds, as in training: the
# import file says so (option_order: shuffled), or the import stops at the first row whose order the manifest changes
SHUFFLED_CLASSIFY = {"interface": "classify", "option_order": "shuffled"}


def write_import(folder: Path, **changes: Any) -> Path:
    spec = {"path": "apps/desk", "id": "jeff.finance.desk", "aliases": ["desk"], "version": "1.0.0",
            "function_by": "id_suffix",
            "context": [{"key": "watchlist", "description": "The desk's companies, as 'TICKER: Company name'."}],
            "input": {"key": "message", "description": "One message about a company.", "form": "fields"},
            "functions": {"company": SHUFFLED_CLASSIFY, "kind": SHUFFLED_CLASSIFY, "urgent": {"interface": "detect"}}}
    spec.update(changes)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "desk.yaml").write_text(yaml.safe_dump(spec, sort_keys=False))
    return folder


def run(tmp_path: Path, hub: FakeHub, imports: Path) -> Path:
    out = tmp_path / "out"
    import_private.main(["desk", "--out", str(out)], fetch=hub.fetch, listing=hub.listing, imports=imports)
    return out


def test_an_adapter_with_three_functions_imports_and_its_manifest_validates(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    out = run(tmp_path, hub, write_import(tmp_path / "imports"))
    path = out / "jeff.finance.desk" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    manifest = manifest_from_dict(raw, path)
    assert raw["licence"] == {"kind": "proprietary", "adapter": "Scissero proprietary"}
    assert raw["weights"] == {"proprietary": {"repo": "teamsuzie/jeff-adapters", "path": "apps/desk"}}
    assert raw["base"] == {"id": "jeff-base", "version": "v1.3", "max_options": 50}
    assert raw["tests"] == {
        "published": None, "calibration": None, "function_by": "id_suffix",
        "internal": "hf://datasets/teamsuzie/jeff-data@main/apps/desk/test.jsonl",
        "internal_calibration": "hf://datasets/teamsuzie/jeff-data@main/apps/desk/calibration.jsonl"}
    assert raw["context"] == [{"key": "watchlist", "description": "The desk's companies, as 'TICKER: Company name'.",
                               "required": True}]
    assert raw["input"] == {"key": "message", "description": "One message about a company.", "form": "fields"}
    company, kind, urgent = (manifest.functions[key] for key in ("company", "kind", "urgent"))
    assert (company.interface, company.type, company.instructions) == ("classify", "choice", INSTRUCTIONS["company"])
    # 60 companies cannot all be listed (the base takes 50 options): the caller passes its watchlist, after `none`,
    # which every row offers.
    assert raw["functions"]["company"]["labels"] == {"source": "caller", "fixed_first": {"none": "None of them."},
                                                     "caller_keys": None, "max": 7}
    # Training shuffled the event kinds: the same three in every row, listed by key.
    assert raw["functions"]["kind"]["labels"] == {"source": "fixed", "fixed": dict(sorted(KINDS.items())),
                                                  "subset": False}
    assert (urgent.interface, urgent.type) == ("detect", "noul")
    assert raw["functions"]["urgent"]["labels"] == {"source": "fixed", "fixed": URGENT}
    assert all(body["calibration"] is None and body["abstain_labels"] == [] for body in raw["functions"].values())
    assert raw["hub"]["title"] == "Desk news adapter"
    assert kind.labels.source == "fixed"
    # The weights are checked by listing only: no .safetensors file is downloaded.
    assert not [path for _, path, _ in hub.fetched if path.endswith(".safetensors")]


@pytest.mark.parametrize("access", ["owner", {"accounts": ["acct1", "acct2"]}])
def test_an_import_files_access_becomes_the_manifests(tmp_path: Path, access: object) -> None:
    out = run(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports", access=access))
    path = out / "jeff.finance.desk" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    assert raw["access"] == access
    manifest_from_dict(raw, path)


def test_an_import_file_without_access_gives_a_manifest_without_it(tmp_path: Path) -> None:
    out = run(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports"))
    assert "access" not in yaml.safe_load((out / "jeff.finance.desk" / "manifest.yaml").read_text())


def test_an_access_the_manifest_schema_refuses_stops_it(tmp_path: Path) -> None:
    with pytest.raises(SystemExit, match=r"(?s)the manifest is not valid: .*at /access"):
        run(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports", access={"accounts": []}))


def test_a_function_in_the_rows_missing_from_the_import_file_stops_it(tmp_path: Path) -> None:
    imports = write_import(tmp_path / "imports", functions={"company": {"interface": "classify"},
                                                             "kind": {"interface": "classify"}})
    with pytest.raises(SystemExit, match=r"desk: .*urgent.*not in .*desk.yaml"):
        run(tmp_path, make_hub(tmp_path), imports)


def test_a_function_in_the_import_file_with_no_rows_stops_it(tmp_path: Path) -> None:
    imports = write_import(tmp_path / "imports", functions={"company": {"interface": "classify"},
                                                             "kind": {"interface": "classify"},
                                                             "urgent": {"interface": "detect"},
                                                             "price": {"interface": "rate"}})
    with pytest.raises(SystemExit, match=r"desk\.price: no test rows"):
        run(tmp_path, make_hub(tmp_path), imports)


def test_mixed_state_shapes_stop_it(tmp_path: Path) -> None:
    rows = rows_for("test", 6) + rows_for("test-b", 3, shape=("message",))
    with pytest.raises(SystemExit, match=r"desk: the rows mix state shapes"):
        run(tmp_path, make_hub(tmp_path, rows), write_import(tmp_path / "imports"))


def test_a_missing_readout_stops_it(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    hub.drop("teamsuzie/jeff-adapters", "model", "apps/desk/readout.safetensors")
    with pytest.raises(SystemExit, match=r"desk: teamsuzie/jeff-adapters apps/desk lacks .*readout\.safetensors"):
        run(tmp_path, hub, write_import(tmp_path / "imports"))


def test_a_full_model_folder_is_refused_naming_the_missing_adapter_files(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    for name in ("adapter_config.json", "adapter_model.safetensors", "readout.safetensors"):
        hub.drop("teamsuzie/jeff-adapters", "model", f"apps/desk/{name}")
    hub.put("teamsuzie/jeff-adapters", "model", "apps/desk/model.safetensors", "weights", "d" * 64)
    with pytest.raises(SystemExit, match=r"lacks adapter_config\.json, adapter_model\.safetensors, "
                                         r"readout\.safetensors"):
        run(tmp_path, hub, write_import(tmp_path / "imports"))


def test_an_unknown_interface_stops_it(tmp_path: Path) -> None:
    imports = write_import(tmp_path / "imports", functions={"company": {"interface": "identify"},
                                                             "kind": {"interface": "classify"},
                                                             "urgent": {"interface": "detect"}})
    with pytest.raises(SystemExit, match=r"desk\.company: interface 'identify' is not in contract/interfaces\.yaml"):
        run(tmp_path, make_hub(tmp_path), imports)


def test_an_interface_of_another_question_type_stops_it(tmp_path: Path) -> None:
    imports = write_import(tmp_path / "imports", functions={"company": {"interface": "classify"},
                                                             "kind": {"interface": "rate"},
                                                             "urgent": {"interface": "detect"}})
    with pytest.raises(SystemExit, match=r"desk\.kind: interface rate compiles to score, but its rows ask choice"):
        run(tmp_path, make_hub(tmp_path), imports)


def test_an_import_file_missing_a_field_stops_it(tmp_path: Path) -> None:
    imports = write_import(tmp_path / "imports")
    spec = yaml.safe_load((imports / "desk.yaml").read_text())
    del spec["version"]
    (imports / "desk.yaml").write_text(yaml.safe_dump(spec))
    with pytest.raises(SystemExit, match=r"desk\.yaml: missing version"):
        run(tmp_path, make_hub(tmp_path), imports)


def test_state_keys_that_differ_from_the_import_file_stop_it(tmp_path: Path) -> None:
    imports = write_import(tmp_path / "imports", input={"key": "text", "description": "A message.", "form": "fields"})
    with pytest.raises(SystemExit, match=r"desk: the rows' state keys \['watchlist', 'message'\] are not"):
        run(tmp_path, make_hub(tmp_path), imports)


def test_an_adapter_on_an_unknown_base_stops_it(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    hub.listed[("mstrasser/jeff-base", "model", "v1.3")]["model.safetensors"] = "e" * 64
    with pytest.raises(SystemExit, match=r"desk: its base weights .* match no base in catalogue/bases\.yaml"):
        run(tmp_path, hub, write_import(tmp_path / "imports"))


def test_labels_that_look_like_a_subset_with_no_documented_order_stop_it(tmp_path: Path) -> None:
    # Ten companies in all: they fit in the base's options, so a subset is possible, but nothing documents their order.
    rows = [row for row in rows_for("test", 12) if not row["id"].endswith("-company")]
    for n in range(4):
        criteria = {"none": "None of them.", **{f"C{m}": f"Company {m}" for m in range(n, n + 4)}}
        rows.append({"id": f"test-{n:03d}-company", "state": rows[0]["state"],
                     "question": {"type": "choice", "instructions": "Which?", "criteria": criteria}, "label": "none"})
    with pytest.raises(SystemExit, match=r"desk\.company: .*subset.*owner decides"):
        run(tmp_path, make_hub(tmp_path, rows), write_import(tmp_path / "imports"))


@pytest.mark.parametrize("where", ["context", "input"])
def test_a_field_without_a_description_stops_it(tmp_path: Path, where: str) -> None:
    change = {"context": [{"key": "watchlist"}]} if where == "context" else {"input": {"key": "message"}}
    label = "context\\[0\\]" if where == "context" else "input"
    with pytest.raises(SystemExit, match=rf"desk: .*desk\.yaml: {label} is missing description"):
        run(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports", **change))


def test_function_by_only_with_several_functions_stops_it(tmp_path: Path) -> None:
    imports = write_import(tmp_path / "imports", function_by="only")
    with pytest.raises(SystemExit, match=r"desk: .*function_by only needs exactly one function"):
        run(tmp_path, make_hub(tmp_path), imports)


def test_rows_the_mapping_cannot_place_stop_it_naming_the_adapter(tmp_path: Path) -> None:
    # Two choice functions cannot be told apart by question type: the shared mapping refuses, and the import stops.
    imports = write_import(tmp_path / "imports", function_by="question_type")
    with pytest.raises(SystemExit, match=r"^desk: desk: test row .* no single function of type choice"):
        run(tmp_path, make_hub(tmp_path), imports)


LEVELS = ["low: hardly moves the price", "medium: moves it somewhat", "high: moves it a lot"]
GROUNDED = {"supported": "Supported.", "partly_supported": "Partly.", "contradicted": "Contradicted.",
            "unsupported": "Unsupported."}


def score_and_grounded_rows(split: str, count: int) -> list[dict]:
    rows = []
    for n in range(count):
        state = {"watchlist": ["T01: Company T01"], "message": f"{split} {n}"}
        shuffled = dict(list(GROUNDED.items())[n % 4:] + list(GROUNDED.items())[:n % 4])
        rows.append({"id": f"{split}-{n}-impact", "state": state, "label": 1,
                     "question": {"type": "score", "instructions": "How much will it move the price?",
                                  "criteria": LEVELS}})
        rows.append({"id": f"{split}-{n}-grounded", "state": state, "label": "supported",
                     "question": {"type": "choice", "instructions": "Is the claim supported?",
                                  "criteria": shuffled}})
    return rows


def test_a_rate_function_and_a_fixed_value_interface_import(tmp_path: Path) -> None:
    hub = make_hub(tmp_path, score_and_grounded_rows("test", 8), score_and_grounded_rows("calibration", 4))
    imports = write_import(tmp_path / "imports", functions={"impact": {"interface": "rate"},
                                                             "grounded": {"interface": "is_supported",
                                                                          "option_order": "shuffled"}})
    path = run(tmp_path, hub, imports) / "jeff.finance.desk" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    manifest_from_dict(raw, path)
    assert raw["functions"]["impact"]["labels"] == {"source": "fixed", "levels": LEVELS}
    assert raw["functions"]["impact"]["type"] == "score"
    # is_supported needs its values in the interface's order, not listed by key.
    assert list(raw["functions"]["grounded"]["labels"]["fixed"]) == list(GROUNDED)
    assert raw["functions"]["grounded"]["labels"]["fixed"] == GROUNDED


def test_labels_that_are_not_a_fixed_value_interfaces_values_stop_it(tmp_path: Path) -> None:
    imports = write_import(tmp_path / "imports", functions={"company": SHUFFLED_CLASSIFY,
                                                             "kind": {"interface": "equivalent"},
                                                             "urgent": {"interface": "detect"}})
    with pytest.raises(SystemExit, match=r"desk\.kind: .* not the interface's values \['match'"):
        run(tmp_path, make_hub(tmp_path), imports)


CASES = json.loads((Path(__file__).parent / "fixtures" / "jeffhub_helpers.json").read_text())


@pytest.mark.parametrize("case", CASES, ids=[f"{n}-{case['function']}" for n, case in enumerate(CASES)])
def test_the_shared_helpers_give_what_import_jeffhub_gave_before_they_moved(case: dict[str, Any]) -> None:
    notes: list[str] = []
    args = case["args"]
    calls = {"choice_labels": lambda: common.choice_labels(*args[:4], notes, args[4]),
             "question_from_rows": lambda: common.question_from_rows(*args),
             "function_of": lambda: common.function_of(*args, None, None, None)}
    try:
        result = {"output": calls[case["function"]]()}
    except SystemExit as error:
        result = {"exit": str(error)}
    except Exception as error:  # the captured outcome may be an error: compare its type and message
        result = {"error": type(error).__name__, "message": str(error)}
    expected = {key: case[key] for key in ("output", "exit", "error", "message") if key in case}
    assert (json.loads(json.dumps(result)), notes) == (expected, case["notes"])


def test_import_jeffhub_uses_the_shared_helpers() -> None:
    import import_jeffhub

    for name in ("read_rows", "function_of", "choice_labels", "question_from_rows"):
        assert getattr(import_jeffhub, name) is getattr(common, name)


def test_a_numbered_key_is_matched_as_a_whole_string() -> None:
    assert common.NUMBERED.fullmatch("a1") is not None
    assert common.NUMBERED.fullmatch("a1\n") is None  # `$` alone would accept the trailing newline


# --- adapters read from a folder on disk (--adapters-root), bases kept in a local folder, and the row checks -------

def local_root(tmp_path: Path, folder: str = "v14/desk", small_only: bool = False) -> Path:
    """An adapters root holding one adapter folder; with small_only, only its decision_config.json and README.md
    (a copy of the small files), the rest named by a listing."""
    root = tmp_path / "adapters"
    config = {"max_options": 50, "adapter": {"base_checkpoint": {"weights_sha256": {"model.safetensors": BASE_SHA}}}}
    files = {"decision_config.json": json.dumps(config), "README.md": "# Desk news adapter v1.4\n\nReads messages.\n"}
    if not small_only:
        files.update({name: "weights" for name in ADAPTER_FILES if name != "decision_config.json"})
    for name, text in files.items():
        (root / folder).mkdir(parents=True, exist_ok=True)
        (root / folder / name).write_text(text)
    return root


def hub_without_adapters(tmp_path: Path, **rows: Any) -> FakeHub:
    hub = make_hub(tmp_path, **rows)
    for name in (*ADAPTER_FILES, "README.md"):
        hub.drop("teamsuzie/jeff-adapters", "model", f"apps/desk/{name}")
    return hub


def run_local(tmp_path: Path, hub: FakeHub, imports: Path, *extra: str) -> dict[str, Any]:
    out = tmp_path / "out"
    import_private.main(["desk", "--out", str(out), *extra], fetch=hub.fetch, listing=hub.listing, imports=imports)
    return yaml.safe_load((out / "jeff.finance.desk" / "manifest.yaml").read_text())


def test_an_adapter_folder_on_disk_imports_with_local_weights(tmp_path: Path) -> None:
    hub = hub_without_adapters(tmp_path)
    root = local_root(tmp_path)
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports", adapter_path="v14/desk"),
                    "--adapters-root", str(root))
    assert raw["weights"] == {"local": {}}
    assert raw["hub"]["title"] == "Desk news adapter v1.4"
    assert raw["hub"]["source"] == "local adapter folder v14/desk (not on Hugging Face)"
    # the rows still come from jeff-data at `path`; nothing is read from teamsuzie/jeff-adapters
    assert raw["tests"]["internal"] == "hf://datasets/teamsuzie/jeff-data@main/apps/desk/test.jsonl"
    assert not [repo for repo, _, _ in hub.fetched if repo == "teamsuzie/jeff-adapters"]


def test_a_copy_of_the_small_files_imports_with_a_listing_of_the_real_folder(tmp_path: Path) -> None:
    root = local_root(tmp_path, small_only=True)
    listing = tmp_path / "files.txt"
    listing.write_text("".join(f"v14/desk/{name}\n" for name in (*ADAPTER_FILES, "README.md")))
    raw = run_local(tmp_path, hub_without_adapters(tmp_path),
                    write_import(tmp_path / "imports", adapter_path="v14/desk"),
                    "--adapters-root", str(root), "--adapters-listing", str(listing))
    assert raw["weights"] == {"local": {}}


def test_a_listing_that_lacks_a_weight_file_is_refused(tmp_path: Path) -> None:
    root = local_root(tmp_path, small_only=True)
    listing = tmp_path / "files.txt"
    listing.write_text("".join(f"v14/desk/{name}\n" for name in (*ADAPTER_FILES, "README.md")
                               if name != "readout.safetensors"))
    with pytest.raises(SystemExit, match=r"desk: --adapters-root .* v14/desk lacks readout\.safetensors"):
        run_local(tmp_path, hub_without_adapters(tmp_path), write_import(tmp_path / "imports", adapter_path="v14/desk"),
                  "--adapters-root", str(root), "--adapters-listing", str(listing))


def test_a_folder_missing_under_the_root_is_refused(tmp_path: Path) -> None:
    root = local_root(tmp_path)
    with pytest.raises(SystemExit, match=r"desk: --adapters-root .* has no folder v14/other"):
        run_local(tmp_path, hub_without_adapters(tmp_path),
                  write_import(tmp_path / "imports", adapter_path="v14/other"), "--adapters-root", str(root))


def test_a_listing_needs_an_adapters_root(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run_local(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports"),
                  "--adapters-listing", str(tmp_path / "files.txt"))


def test_a_base_kept_in_a_local_folder_is_matched_by_its_recorded_sha(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    bases = tmp_path / "bases.yaml"
    bases.write_text(yaml.safe_dump({"jeff-base": {
        "v1.3": {"repo": "mstrasser/jeff-base", "revision": "v1.3", "released": "2026-10-05"},
        "v1.4": {"path": "/mnt/d/jeff-models/v1.4/v14-final", "model_sha256": "f" * 64, "released": "2026-10-24"}}}))
    monkeypatch.setattr(import_private, "BASES", bases)
    hub = make_hub(tmp_path)
    hub.listed[("mstrasser/jeff-base", "model", "v1.3")]["model.safetensors"] = "a" * 64
    config = {"max_options": 50, "adapter": {"base_checkpoint": {"weights_sha256": {"model.safetensors": "f" * 64}}}}
    hub.put("teamsuzie/jeff-adapters", "model", "apps/desk/decision_config.json", json.dumps(config))
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports"))
    assert raw["base"] == {"id": "jeff-base", "version": "v1.4", "max_options": 50}


def test_a_local_base_without_a_recorded_sha_stops_it(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bases = tmp_path / "bases.yaml"
    bases.write_text(yaml.safe_dump({"jeff-base": {"v1.4": {"path": "/somewhere", "released": "2026-10-24"}}}))
    monkeypatch.setattr(import_private, "BASES", bases)
    with pytest.raises(SystemExit, match=r"base jeff-base v1\.4 is the local folder /somewhere, .* no model_sha256"):
        run_local(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports"))


def test_rows_are_read_at_the_data_revision_the_import_file_names(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    old = rows_for("test", 9)
    for split, rows in (("test", old), ("calibration", rows_for("calibration", 4))):
        target = tmp_path / "hub" / "dataset" / "teamsuzie/jeff-data" / "@abc123" / f"apps/desk/{split}.jsonl"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("".join(json.dumps(row) + "\n" for row in rows))
        hub.listed.setdefault(("teamsuzie/jeff-data", "dataset", "abc123"), {})[f"apps/desk/{split}.jsonl"] = None
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports", data_revision="abc123"))
    assert raw["tests"]["internal"] == "hf://datasets/teamsuzie/jeff-data@abc123/apps/desk/test.jsonl"
    assert raw["tests"]["internal_calibration"] == "hf://datasets/teamsuzie/jeff-data@abc123/apps/desk/calibration.jsonl"
    assert any("revision abc123" in note for note in raw["hub"]["notes"])


def marked(rows: list[dict]) -> list[dict]:
    """Rows as v1.4 data writes them: most with the canonical instructions, some reworded, some with none."""
    for n, row in enumerate(rows):
        kind = ("canonical", "canonical", "variant-3", "none")[n % 4]
        row["source"] = {"instructions": kind}
        if kind == "variant-3":
            row["question"]["instructions"] = f"In other words ({n}): " + row["question"]["instructions"]
        if kind == "none":
            del row["question"]["instructions"]
    return rows


def test_instructions_are_the_text_of_the_rows_marked_canonical(tmp_path: Path) -> None:
    hub = make_hub(tmp_path, marked(rows_for("test", 12)), marked(rows_for("calibration", 6)))
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports"))
    assert {key: body["instructions"] for key, body in raw["functions"].items()} == INSTRUCTIONS
    assert any(note.startswith("company: instructions are the one text of the 6 test rows marked canonical")
               for note in raw["hub"]["notes"])


def test_unmarked_rows_among_marked_ones_must_use_the_canonical_text(tmp_path: Path) -> None:
    rows = marked(rows_for("test", 12))
    del rows[0]["source"]  # the first company row, marked canonical: its text is the canonical text
    raw = run_local(tmp_path, make_hub(tmp_path, rows), write_import(tmp_path / "imports"))
    assert raw["functions"]["company"]["instructions"] == INSTRUCTIONS["company"]
    assert "company: 1 test rows do not mark their instructions (source.instructions); each uses the canonical text " \
           "word for word, so they are rebuilt exactly." in raw["hub"]["notes"]
    rows[0]["question"]["instructions"] = "Which listed company is it?"
    with pytest.raises(SystemExit, match=r"desk\.company: 11 of its 12 test rows mark .* and 1 of those \(e\.g\. "
                                         r"test-000-company\) do not use the canonical text; owner decides"):
        run_local(tmp_path, make_hub(tmp_path, rows), write_import(tmp_path / "imports"))


def test_a_context_field_marked_optional_may_be_absent(tmp_path: Path) -> None:
    rows = json.loads(json.dumps(rows_for("test", 12)))  # each row its own state
    for row in rows[:3]:  # the three questions of the first message
        del row["state"]["watchlist"]
    # rows without a watchlist can only be asked the questions whose labels do not come from it
    rows = [row for row in rows if "watchlist" in row["state"] or not row["id"].endswith("-company")]
    context = [{"key": "watchlist", "description": "The desk's companies.", "required": False}]
    raw = run_local(tmp_path, make_hub(tmp_path, rows), write_import(tmp_path / "imports", context=context))
    assert raw["context"] == [{"key": "watchlist", "description": "The desk's companies.", "required": False}]
    assert any(note.startswith("context watchlist: absent from 2 of the") for note in raw["hub"]["notes"])


def test_a_context_field_marked_optional_that_every_row_has_stops_it(tmp_path: Path) -> None:
    context = [{"key": "watchlist", "description": "The desk's companies.", "required": False}]
    with pytest.raises(SystemExit, match=r"desk: the import file marks context watchlist required: false, but every"):
        run_local(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports", context=context))


def test_a_calibration_row_that_is_also_a_test_row_stops_it(tmp_path: Path) -> None:
    test = rows_for("test", 12)
    calibration = rows_for("calibration", 6) + [dict(test[4], id="calibration-copy-kind")]
    with pytest.raises(SystemExit, match=r"desk: 1 calibration rows ask a test row's state and question"):
        run_local(tmp_path, make_hub(tmp_path, test, calibration), write_import(tmp_path / "imports"))
    with pytest.raises(SystemExit, match=r"desk: 1 row ids are in both the test and the calibration set"):
        run_local(tmp_path, make_hub(tmp_path, test, rows_for("calibration", 6)
                                                    + [dict(test[4], state={**test[4]["state"], "message": "other"})]),
                  write_import(tmp_path / "imports"))


def test_a_row_the_manifest_cannot_describe_stops_it(tmp_path: Path) -> None:
    calibration = rows_for("calibration", 6)
    odd = next(row for row in calibration if row["id"].endswith("-kind"))
    odd["question"]["criteria"] = {**odd["question"]["criteria"], "merger": "A merger."}
    with pytest.raises(SystemExit, match=r"desk: the manifest does not describe 1 of the 18 calibration rows: "
                                         r"\{'fixed options differ from the manifest': 1\}"):
        run_local(tmp_path, make_hub(tmp_path, rows_for("test", 12), calibration), write_import(tmp_path / "imports"))


def test_caller_max_counts_the_calibration_rows_too(tmp_path: Path) -> None:
    calibration = rows_for("calibration", 6)
    wide = next(row for row in calibration if row["id"].endswith("-company"))
    wide["question"]["criteria"] = {**wide["question"]["criteria"], **{f"Z{n}": f"Company Z{n}" for n in range(9)}}
    raw = run_local(tmp_path, make_hub(tmp_path, rows_for("test", 12), calibration), write_import(tmp_path / "imports"))
    assert raw["functions"]["company"]["labels"]["max"] == len(wide["question"]["criteria"]) - 1  # all but `none`
    assert any(note.startswith("company: the calibration rows offer up to") for note in raw["hub"]["notes"])


def test_a_fixed_option_with_another_text_stops_it_naming_the_row(tmp_path: Path) -> None:
    calibration = rows_for("calibration", 6)
    odd = next(row for row in calibration if row["id"].endswith("-kind"))
    odd["question"]["criteria"] = {**odd["question"]["criteria"], "halt": "A pause in trading."}
    with pytest.raises(SystemExit, match=rf"desk: the manifest does not describe 1 of the 18 calibration rows: "
                                         rf"\{{'fixed option texts differ from the manifest': 1\}}; the first is "
                                         rf"{odd['id']} \(fixed option texts differ from the manifest\)"):
        run_local(tmp_path, make_hub(tmp_path, rows_for("test", 12), calibration), write_import(tmp_path / "imports"))


def test_a_rebuilt_row_whose_option_text_differs_stops_it_naming_the_row(tmp_path: Path) -> None:
    calibration = rows_for("calibration", 6)
    odd = next(row for row in calibration if row["id"].endswith("-company"))
    odd["question"]["criteria"] = {**odd["question"]["criteria"], "none": "Nobody on the list."}
    with pytest.raises(SystemExit, match=rf"desk: calibration row {odd['id']} is rebuilt with options .*'none': "
                                         r"'None of them.'.* but it was trained with .*'none': 'Nobody on the list.'"):
        run_local(tmp_path, make_hub(tmp_path, rows_for("test", 12), calibration), write_import(tmp_path / "imports"))


def text_rows(split: str, count: int) -> list[dict[str, Any]]:
    """rows_for's kind and urgent rows with the state as one plain string (the message), as a plain-text adapter's."""
    return [{**row, "state": row["state"]["message"]} for row in rows_for(split, count)
            if not row["id"].endswith("-company")]


TEXT_IMPORT: dict[str, Any] = {
    "context": [], "input": {"key": "message", "description": "One message, as plain text.", "form": "text"},
    "functions": {"kind": SHUFFLED_CLASSIFY, "urgent": {"interface": "detect"}}}


def test_a_plain_text_adapter_imports_with_input_form_text(tmp_path: Path) -> None:
    hub = make_hub(tmp_path, text_rows("test", 12), text_rows("calibration", 6))
    out = run(tmp_path, hub, write_import(tmp_path / "imports", **TEXT_IMPORT))
    path = out / "jeff.finance.desk" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    assert raw["context"] == []
    assert raw["input"] == {"key": "message", "description": "One message, as plain text.", "form": "text"}
    assert manifest_from_dict(raw, path).input.form == "text"


def test_input_form_text_with_named_field_rows_stops_it(tmp_path: Path) -> None:
    rows = [row for row in rows_for("test", 12, ("message",)) if not row["id"].endswith("-company")]
    calibration = [row for row in rows_for("calibration", 6, ("message",)) if not row["id"].endswith("-company")]
    with pytest.raises(SystemExit, match=r"desk: input.form is text, but 36 rows .* not a plain string"):
        run(tmp_path, make_hub(tmp_path, rows, calibration), write_import(tmp_path / "imports", **TEXT_IMPORT))


def test_input_form_fields_with_plain_text_rows_stops_it(tmp_path: Path) -> None:
    hub = make_hub(tmp_path, text_rows("test", 12), text_rows("calibration", 6))
    imports = write_import(tmp_path / "imports", **{**TEXT_IMPORT, "input": {**TEXT_IMPORT["input"], "form": "fields"}})
    with pytest.raises(SystemExit, match=r"desk: .* have a plain-text state, not named fields"):
        run(tmp_path, hub, imports)


@pytest.mark.parametrize(("change", "message"), [
    ({"input": {"key": "message", "description": "One message about a company."}}, "input.form must be one of"),
    ({"input": {"key": "message", "description": "One message.", "form": "words"}}, "input.form must be one of"),
    ({"input": {"key": "message", "description": "One message.", "form": "text"}}, "input.form text .* context must be empty"),
])
def test_an_import_file_without_a_valid_input_form_stops_it(tmp_path: Path, change: dict[str, Any],
                                                            message: str) -> None:
    with pytest.raises(SystemExit, match=rf"desk\.yaml: {message}"):
        run(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports", **change))


def test_the_shared_helper_maps_rows_by_request_shape() -> None:
    """The state_shape criterion through the importers' helper: each function key -> its request shape (the context
    keys and then the input key, or text), as import_private.request_shape_of gives it for an import file."""
    questions = {"alert_action": {"type": "choice"}, "note_action": {"type": "choice"}}
    spec = {"context": [{"key": "organisation", "description": "Who."}],
            "input": {"key": "alert", "description": "The alert.", "form": "fields"},
            "functions": {"alert_action": {"interface": "classify"},
                          "note_action": {"interface": "classify", "context": [],
                                          "input": {"key": "note", "description": "A note.", "form": "text"}}}}
    shapes = {key: import_private.request_shape_of("n", spec, key) for key in questions}
    assert shapes == {"alert_action": ("organisation", "alert"), "note_action": "text"}
    alert = {"id": "r1", "state": {"organisation": "A", "alert": "x"}, "question": {"type": "choice"}}
    note = {"id": "r2", "state": "x", "question": {"type": "choice"}}
    assert common.function_of("n", alert, questions, "state_shape", shapes, None, None) == "alert_action"
    assert common.function_of("n", note, questions, ["question_type", "state_shape"], shapes, None, None) \
        == "note_action"


# --- the import-file format for the v1.4 adapters (2026-10-26): function_by criteria, kinds, id patterns, a function's
# own request shape, fixed labels in a given order with a caller subset, code names ------------------------------------


def two_shape_rows(split: str, count: int) -> list[dict[str, Any]]:
    """rows_for's company and kind rows (state watchlist, message) plus `thread` rows of a second request shape (state
    "thread owner", thread), whose ids end with a number, as some real adapters' rows end with a name rather than the function key."""
    rows = [row for row in rows_for(split, count) if not row["id"].endswith("-urgent")]
    for n in range(count):
        rows.append({"id": f"{split}-{n:03d}-thread-{n}",
                     "state": {"thread owner": f"Owner {n}", "thread": f"{split} {n}"},
                     "question": {"type": "choice", "instructions": "Is the thread about the desk?",
                                  "criteria": {"yes": "It is.", "no": "It is not."}}, "label": "yes"})
    return rows


SHAPES_IMPORT: dict[str, Any] = {
    "function_by": ["id_pattern", "state_shape"],
    "id_patterns": {"company": ".+-company", "kind": ".+-kind", "thread": ".+-thread-[0-9]+"},
    "functions": {"company": SHUFFLED_CLASSIFY, "kind": SHUFFLED_CLASSIFY,
                  "thread": {"interface": "classify",
                             "context": [{"key": "thread owner", "description": "Who started the thread."}],
                             "input": {"key": "thread", "description": "The whole thread.", "form": "fields"}}}}


def test_a_function_with_its_own_request_shape_imports_by_id_pattern_and_state_shape(tmp_path: Path) -> None:
    hub = make_hub(tmp_path, two_shape_rows("test", 12), two_shape_rows("calibration", 6))
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports", **SHAPES_IMPORT))
    assert raw["tests"]["function_by"] == ["id_pattern", "state_shape"]
    assert raw["tests"]["id_patterns"] == SHAPES_IMPORT["id_patterns"]
    thread = raw["functions"]["thread"]
    assert thread["context"] == [{"key": "thread owner", "description": "Who started the thread.", "required": True}]
    assert thread["input"] == {"key": "thread", "description": "The whole thread.", "form": "fields"}
    assert "context" not in raw["functions"]["company"] and "input" not in raw["functions"]["company"]
    manifest = manifest_from_dict(raw, Path("desk.yaml"))
    assert manifest.functions["thread"].context[0].code_name == "thread_owner"


def test_rows_of_a_function_s_own_shape_are_checked_against_it(tmp_path: Path) -> None:
    rows = two_shape_rows("test", 12)
    rows[-1]["state"] = {"owner": "Ann", "thread": "t"}
    with pytest.raises(SystemExit, match=r"desk: .*test-011-thread-11 matches no function: after state_shape"):
        run_local(tmp_path, make_hub(tmp_path, rows, two_shape_rows("calibration", 6)),
                  write_import(tmp_path / "imports", **SHAPES_IMPORT))


@pytest.mark.parametrize(("change", "message"), [
    ({"function_by": ["id_pattern", "id_pattern"]}, "function_by as a list must hold"),
    ({"function_by": ["only"]}, "function_by as a list must hold"),
    ({"function_by": "by_magic"}, "function_by 'by_magic' is not one of"),
    ({"id_patterns": []}, "id_patterns must be a non-empty mapping"),
    ({"function_by": "state_shape"}, "id_patterns goes with the id_pattern criterion"),
    ({"id_patterns": {"kind": ".+-kind", "company": ".+-company", "thread": ".+-thread-[0-9]+"}},
     "id_patterns must name every function, in the functions' order"),
    ({"functions": {**SHAPES_IMPORT["functions"], "thread": {"interface": "classify", "input": {
        "key": "thread", "description": "The whole thread.", "form": "fields"}}}}, "function thread declares input "
                                                                                     "without context"),
    ({"functions": {**SHAPES_IMPORT["functions"], "thread": {**SHAPES_IMPORT["functions"]["thread"], "input": {
        "key": "thread", "description": "The whole thread."}}}}, "functions.thread.input.form must be one of"),
    ({"functions": {**SHAPES_IMPORT["functions"], "kind": {"interface": "classify", "levels": [1]}}},
     "function kind must be"),
    ({"functions": {**SHAPES_IMPORT["functions"], "kind": {"interface": "classify", "option_order": "random"}}},
     "function kind: option_order must be 'shuffled'"),
])
def test_an_import_file_with_a_malformed_new_field_stops_it(tmp_path: Path, change: dict[str, Any],
                                                           message: str) -> None:
    hub = make_hub(tmp_path, two_shape_rows("test", 12), two_shape_rows("calibration", 6))
    with pytest.raises(SystemExit, match=rf"desk\.yaml: {message}"):
        run_local(tmp_path, hub, write_import(tmp_path / "imports", **{**SHAPES_IMPORT, **change}))


def test_kinds_map_source_kind_values_to_functions(tmp_path: Path) -> None:
    def kinded(rows: list[dict]) -> list[dict]:
        for row in rows:
            row["source"] = {"kind": {"company": "which-company", "kind": "event-kind", "urgent": "urgent"}[
                row["id"].rsplit("-", 1)[1]]}
        return rows
    hub = make_hub(tmp_path, kinded(rows_for("test", 12)), kinded(rows_for("calibration", 6)))
    kinds = {"which-company": "company", "event-kind": "kind", "urgent": "urgent"}
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports", function_by="source_kind", kinds=kinds))
    assert raw["tests"]["function_by"] == "source_kind" and raw["tests"]["kinds"] == kinds


def subset_rows(split: str, count: int) -> list[dict[str, Any]]:
    """Rows of one choice function whose five fixed labels are shuffled per row, every third row offering only four."""
    labels = [("ignore", "Ignore it."), ("investigate", "Look into it."), ("escalate", "Escalate it."),
              ("isolate", "Isolate the host."), ("block", "Block it.")]
    rows = []
    for n in range(count):
        offered = labels[n % 5:] + labels[:n % 5]
        if n % 3 == 0:
            offered = [pair for pair in offered if pair[0] != "block"]
        rows.append({"id": f"{split}-{n:03d}", "state": {"watchlist": ["T01"], "message": f"{split} {n}"},
                     "question": {"type": "choice", "instructions": "What do we do?", "criteria": dict(offered)},
                     "label": "ignore"})
    return rows


ORDERED = {"function_by": "only", "functions": {"action": {"interface": "classify", "option_order": "shuffled", "labels": {
    "fixed": ["block", "isolate", "escalate", "investigate", "ignore"], "subset": True}}}}


def test_fixed_labels_in_the_import_files_order_with_a_subset(tmp_path: Path) -> None:
    hub = make_hub(tmp_path, subset_rows("test", 12), subset_rows("calibration", 6))
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports", **ORDERED))
    labels = raw["functions"]["action"]["labels"]
    assert list(labels["fixed"]) == ["block", "isolate", "escalate", "investigate", "ignore"]
    assert labels["fixed"]["isolate"] == "Isolate the host." and labels["subset"] is True
    assert any(note.startswith("action: the labels in the order the import file gives (block, isolate, escalate, "
                               "investigate, ignore); the test rows offer them in 8 different orders (training "
                               "shuffled them), and 4 of the 12 test rows offer only some of them")
               for note in raw["hub"]["notes"])


def test_fixed_labels_every_row_offers_in_one_order_keep_that_order(tmp_path: Path) -> None:
    rows = [{**row, "question": {**row["question"], "criteria": dict(sorted(row["question"]["criteria"].items()))}}
            for row in subset_rows("test", 12) if len(row["question"]["criteria"]) == 5]
    hub = make_hub(tmp_path, rows, subset_rows("calibration", 6))
    with pytest.raises(SystemExit, match=r"desk\.action: every test row offers the labels in the order \['block', "
                                         r"'escalate', 'ignore', 'investigate', 'isolate'\], so that is their order"):
        run_local(tmp_path, hub, write_import(tmp_path / "imports", **ORDERED))


@pytest.mark.parametrize(("labels", "message"), [
    ({"fixed": ["block", "isolate", "escalate", "investigate"], "subset": True}, "lists the labels .* but the test "
                                                                                 "rows offer"),
    ({"fixed": ["block", "isolate", "escalate", "investigate", "ignore"], "subset": False}, "4 test rows offer only "
                                                                                            "some of the labels"),
    ({"fixed": ["block", "block"], "subset": True}, "labels must be"),
])
def test_fixed_labels_the_rows_do_not_bear_out_stop_it(tmp_path: Path, labels: dict[str, Any], message: str) -> None:
    hub = make_hub(tmp_path, subset_rows("test", 12), subset_rows("calibration", 6))
    spec = {**ORDERED, "functions": {"action": {"interface": "classify", "option_order": "shuffled", "labels": labels}}}
    with pytest.raises(SystemExit, match=rf"desk(\.action)?: .*{message}"):
        run_local(tmp_path, hub, write_import(tmp_path / "imports", **spec))


def test_a_field_key_with_spaces_keeps_its_key_and_may_declare_a_code_name(tmp_path: Path) -> None:
    def spaced(rows: list[dict]) -> list[dict]:
        return [{**row, "state": {"watch list": row["state"]["watchlist"], "message": row["state"]["message"]}}
                for row in rows]
    hub = make_hub(tmp_path, spaced(rows_for("test", 12)), spaced(rows_for("calibration", 6)))
    context = [{"key": "watch list", "code_name": "companies", "description": "The desk's companies."}]
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports", context=context))
    assert raw["context"] == [{"key": "watch list", "code_name": "companies", "description": "The desk's companies.",
                               "required": True}]
    assert manifest_from_dict(raw, Path("desk.yaml")).context[0].code_name == "companies"


def test_a_canonical_calibration_row_with_other_instructions_stops_it(tmp_path: Path) -> None:
    calibration = marked(rows_for("calibration", 6))
    odd = next(row for row in calibration if row["source"]["instructions"] == "canonical")
    odd["question"]["instructions"] = "Which company, if any?"
    with pytest.raises(SystemExit, match=rf"desk: calibration row {odd['id']} is rebuilt with the instructions "
                                         r".* but it was trained with 'Which company, if any\?'"):
        run_local(tmp_path, make_hub(tmp_path, marked(rows_for("test", 12)), calibration),
                  write_import(tmp_path / "imports"))


def test_fixed_labels_every_row_offers_in_one_order_are_listed_in_that_order(tmp_path: Path) -> None:
    """Training used one option order in every row (not shuffled): the manifest lists the labels in that order, so the
    prompt shows them as the adapter saw them, not sorted by key."""
    def one_order(rows: list[dict]) -> list[dict]:
        for row in rows:
            if row["id"].endswith("-kind"):
                row["question"]["criteria"] = {"results": "Results.", "halt": "A trading halt.",
                                               "other": "Anything else."}
        return rows
    hub = make_hub(tmp_path, one_order(rows_for("test", 12)), one_order(rows_for("calibration", 6)))
    functions = {"company": SHUFFLED_CLASSIFY, "kind": {"interface": "classify"}, "urgent": {"interface": "detect"}}
    raw = run_local(tmp_path, hub, write_import(tmp_path / "imports", functions=functions))
    assert list(raw["functions"]["kind"]["labels"]["fixed"]) == ["results", "halt", "other"]
    assert not any(note.startswith("kind: every test row has the same") for note in raw["hub"]["notes"])


def test_the_importer_reads_hugging_face_files_through_resolve_and_the_data_mirror(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """hf_fetch goes through jeff.evaluate.resolve: with JEFF_DATA_MIRROR mapping the dataset and revision, a data
    file is the local copy and nothing is downloaded."""
    import huggingface_hub

    def no_download(*args: Any, **options: Any) -> str:
        raise AssertionError(f"downloaded {args} {options}")
    monkeypatch.setattr(huggingface_hub, "hf_hub_download", no_download)
    folder = tmp_path / "jeff-data"
    (folder / "apps/desk").mkdir(parents=True)
    (folder / "apps/desk/test.jsonl").write_text("{}\n")
    monkeypatch.setenv("JEFF_DATA_MIRROR", f"teamsuzie/jeff-data@main={folder}")
    assert import_private.hf_fetch("teamsuzie/jeff-data", "apps/desk/test.jsonl", "dataset", "main") \
        == folder / "apps/desk/test.jsonl"


# Option order: the manifest must keep the one order training used. Every test and calibration row is rebuilt and its
# options compared in order, except for a function whose import file says training shuffled them.

NO_SHUFFLE = {"company": SHUFFLED_CLASSIFY, "kind": {"interface": "classify"}, "urgent": {"interface": "detect"}}


def test_shuffled_options_the_import_file_does_not_declare_stop_it_naming_the_row(tmp_path: Path) -> None:
    hub = make_hub(tmp_path)
    with pytest.raises(SystemExit, match=r"desk: test row test-000-kind is rebuilt with the options in the order "
                                         r"\['halt', 'other', 'results'\], but it was trained in the order \['halt', "
                                         r"'results', 'other'\].*option_order: shuffled"):
        run_local(tmp_path, hub, write_import(tmp_path / "imports", functions=NO_SHUFFLE))


def test_caller_labels_offered_first_at_varying_positions_need_the_declaration_too(tmp_path: Path) -> None:
    functions = {"company": {"interface": "classify"}, "kind": SHUFFLED_CLASSIFY, "urgent": {"interface": "detect"}}
    with pytest.raises(SystemExit, match=r"desk: test row test-001-company is rebuilt with the options in the order "
                                         r"\['none', .*option_order: shuffled"):
        run_local(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports", functions=functions))


def test_a_fixed_value_interface_trained_in_one_other_order_stops_it(tmp_path: Path) -> None:
    """is_supported lists its labels in the interface's order; rows that all offer them in another single order are
    rows the manifest would change, so the import stops instead of reordering them silently."""
    def one_other_order(rows: list[dict]) -> list[dict]:
        for row in rows:
            if row["id"].endswith("-grounded"):
                row["question"]["criteria"] = dict(reversed(list(GROUNDED.items())))
        return rows
    hub = make_hub(tmp_path, one_other_order(score_and_grounded_rows("test", 8)),
                   one_other_order(score_and_grounded_rows("calibration", 4)))
    imports = write_import(tmp_path / "imports", functions={"impact": {"interface": "rate"},
                                                             "grounded": {"interface": "is_supported"}})
    import re
    expected = (f"desk: test row test-0-grounded is rebuilt with the options in the order {list(GROUNDED)}, but it was "
                f"trained in the order {list(reversed(GROUNDED))}")
    with pytest.raises(SystemExit, match=re.escape(expected)):
        run_local(tmp_path, hub, imports)


def test_option_order_shuffled_on_a_function_trained_in_the_manifest_order_stops_it(tmp_path: Path) -> None:
    functions = {"company": SHUFFLED_CLASSIFY, "kind": SHUFFLED_CLASSIFY,
                 "urgent": {"interface": "detect", "option_order": "shuffled"}}
    with pytest.raises(SystemExit, match=r"desk\.urgent: the import file says option_order: shuffled, but every test "
                                         r"and calibration row offers the options in the manifest's order"):
        run_local(tmp_path, make_hub(tmp_path), write_import(tmp_path / "imports", functions=functions))
