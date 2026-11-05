"""The factory's publish step: an adapter published with a manifest and calibration fitted on its own calibration
split, served by the engines and callable through the API's /v1/decide and MCP with no restart; a publish that
fails leaves no folder an engine would find.

Engines here are real engine servers (jeff_runtime.testing.Running over a FakeEngine) with the adapter folder as
their --adapters-dir, reached over their sockets, as the factory worker and the API reach them."""

import dataclasses
import json
import math
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from jeff.binding import bind, build_row
from jeff.catalogue import Catalogue
from jeff.client import Call, Jeff
from jeff.evaluate import published_set, score
from jeff.manifest import load_manifest, manifest_from_dict
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine
from jeff_runtime.engine_server import SERVED_FORMAT, SERVED_VERSION, read_served
from jeff_runtime.testing import Running

from jeff_server.adapter_change import LOCK_NAME, AdapterChangeLock
from jeff_server.app import create_app
from jeff_server.factory import evaluation
from jeff_server.factory.config import FactoryConfig
from jeff_server.factory.publish import serve_published
from jeff_server.factory.manifest import served_question
from jeff_server.factory.runner import Factory
from jeff_server.factory.store import Run, read_json, read_jsonl, write_json, write_jsonl
from jeff_server.router import EngineRouter
from jeff_server.settings import ServerSettings

from factory_testing import fake_scorer, label_key, trading_spec, trained_run, worker_hooks

ADAPTER = "jeff.factory.trading-calls"


def owner_config(root: Path) -> FactoryConfig:
    return FactoryConfig(mode="owner", root=root, teacher_url="u", teacher_key="k", teacher_model="m",
                         trainer=root / "trainer", min_free_gb=16.0)


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    """The first option, sure of it."""
    question = row["question"]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [0.9] + [0.1 / (count - 1)] * (count - 1)


class Engines:
    """One engine server finding adapter folders under `root`, with a served file (none: it refuses a reload)."""

    def __init__(self, tmp_path: Path, root: Path, *, served: bool = True,
                 answer: Any = answers) -> None:
        self.served = tmp_path / "served.json" if served else None
        if self.served is not None:
            self.served.write_text(json.dumps({"format": SERVED_FORMAT, "version": SERVED_VERSION, "adapters": []}))
        self.server = Running(FakeEngine(answer, set()), served_file=self.served, fixed={}, adapter_roots=[root])
        self.path = self.server.path

    def close(self) -> None:
        self.server.close()


@pytest.fixture
def config(tmp_path: Path) -> FactoryConfig:
    return owner_config(tmp_path)


@pytest.fixture
def engines(tmp_path: Path) -> Iterator[Engines]:
    (tmp_path / "adapters").mkdir()
    running = Engines(tmp_path, tmp_path / "adapters")
    yield running
    running.close()


def evaluated(config: FactoryConfig, engines: Engines, wrong_side: bool = False) -> tuple[Factory, Run]:
    """A trained run, calibrated and evaluated through the worker's steps with a fake scorer that is always right
    (with wrong_side, always wrong on the side question, whose options are buy and sell), over `engines`."""
    run, labels = trained_run(config, trading_spec())
    right = fake_scorer(labels)

    def scorer(base: str, adapter: Path, rows: list[dict[str, Any]]) -> dict[str, Any]:
        scored = right(base, adapter, rows)
        for row, logits in zip(rows, scored["logits"], strict=True):
            if wrong_side and set(row["question"].get("criteria", ())) == {"buy", "sell"}:
                logits.reverse()
        return scored

    factory = Factory(config, worker_hooks(config.required_root, (engines.path,), scorer))
    assert factory.step(run, "calibrate") and factory.step(run, "evaluate")
    return factory, run


def folders(root: Path) -> list[str]:
    """The entries of the adapter root, but for the adapter-change lock file publishing takes there."""
    return sorted(path.name for path in root.iterdir() if path.name != LOCK_NAME)


def test_a_published_adapter_has_a_valid_manifest_and_is_served(config: FactoryConfig, engines: Engines,
                                                                 tmp_path: Path) -> None:
    factory, run = evaluated(config, engines)
    assert factory.step(run, "publish"), run.state["steps"]["publish"]
    folder = tmp_path / "adapters" / "trading-calls"
    assert folders(tmp_path / "adapters") == ["trading-calls"]
    raw = yaml.safe_load((folder / "manifest.yaml").read_text())
    manifest = manifest_from_dict(raw, folder / "manifest.yaml")
    assert (manifest.id, manifest.version, manifest.aliases) == (ADAPTER, "1.0.0", ("trading-calls",))
    assert raw["licence"] == {"kind": "proprietary", "adapter": "owner"} and raw["weights"] == {"local": {}}
    assert raw["base"] == {"id": "jeff-base", "version": "v1.3", "max_options": 26}
    assert [(field.key, field.required) for field in manifest.context] == [("desk", True), ("asset_classes", True)]
    assert (manifest.input.key, manifest.input.form) == ("request", "fields")
    assert {key: (spec.interface, spec.type) for key, spec in manifest.functions.items()} == {
        "function": ("classify", "choice"), "side": ("classify", "choice"), "urgent": ("detect", "noul")}
    assert manifest.functions["function"].labels.fixed == {"place_order": "Buy or sell", "cancel_order": "Cancel",
                                                           "none": "Not a trading instruction"}
    assert manifest.functions["urgent"].labels.fixed == {"false": "Not urgent", "true": "Urgent"}
    calibration = folder / "calibration.jsonl"
    for key, spec in manifest.functions.items():
        assert spec.calibration is not None and spec.calibration.data == str(calibration)
        assert spec.calibration.target_precision == 0.95 and spec.calibration.precision_at_threshold == 1.0
    rows = [json.loads(line) for line in calibration.read_text().splitlines()]
    assert sum(spec.calibration.rows for spec in manifest.functions.values()  # type: ignore[union-attr]
               ) == len(rows) == len((run.data / "calibration.jsonl").read_text().splitlines())
    assert raw["hub"]["notes"] == ["side is asked only when function is place_order (only when the function is "
                                   "place_order); the caller decides when to ask it."]
    assert raw["tests"] == {"published": None, "calibration": None, "internal": str(folder / "test.jsonl"),
                            "internal_calibration": str(calibration), "function_by": "id_suffix"}
    # The calibration split was scored again as Jeff sends it: the canonical instruction of each question.
    served = read_json(run.folder / "calibration-served-logits.json")
    assert len(served["logits"]) == len(rows)
    assert raw["routes_to"] == []
    assert (folder / "example.json").exists() and (folder / "data_card.json").exists()

    published = read_json(run.folder / "publish.json")
    assert published["function"] == f"{ADAPTER}.function" and published["answer"]["value"] == "place_order"
    assert published["served"] == {str(engines.path): [ADAPTER]}
    assert engines.served is not None and read_served(engines.served) == [ADAPTER]
    assert run.state["status"] == "published"


def test_the_api_answers_the_published_function_with_no_restart(config: FactoryConfig, engines: Engines,
                                                                tmp_path: Path) -> None:
    socket = SocketEngine(engines.path, timeout_s=5)
    settings = ServerSettings(engine_sockets=(engines.path,), model_name="jeff-v1.3",
                              adapter_dirs=(tmp_path / "adapters",))
    try:
        with TestClient(create_app(settings, engine=EngineRouter([socket]))) as http:
            factory, run = evaluated(config, engines)
            assert factory.step(run, "publish"), run.state["steps"]["publish"]
            call = {"function": f"{ADAPTER}.function", "input": "buy 100 shares now",
                    "context": {"desk": "Rates", "asset_classes": "equity"}}
            response = http.post("/v1/decide", json={"calls": [call]})
            assert response.status_code == 200, response.text
            decision = response.json()["decisions"][0]
            assert decision["value"] == "place_order" and decision["decided_by"] == f"{ADAPTER}.function@1.0.0"
            assert decision["calibrated"] is True
            tools = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                              headers={"Accept": "application/json, text/event-stream",
                                       "MCP-Protocol-Version": "2025-11-25"})
            assert tools.status_code == 200, tools.text
            names = [tool["name"] for tool in tools.json()["result"]["tools"]]
            assert "factory_trading_calls_function" in names, names
    finally:
        socket.close()


def test_a_question_id_that_clashes_after_lower_casing_is_refused_before_copying(
        config: FactoryConfig, engines: Engines, tmp_path: Path) -> None:
    factory, run = evaluated(config, engines)
    spec = run.spec
    spec["questions"][1]["id"] = "Function"
    write_json(run.folder / "spec.json", spec)
    assert not factory.step(run, "publish")
    error = run.state["steps"]["publish"]["error"]
    assert error == ("ManifestError: Questions 'function' and 'Function' both become the function key 'function' "
                     "(lower case, other characters as '_'); rename one of them")
    assert folders(tmp_path / "adapters") == []
    assert engines.served is not None and read_served(engines.served) == []


def test_an_engine_refusing_the_reload_leaves_no_visible_folder_and_is_named(config: FactoryConfig,
                                                                            tmp_path: Path) -> None:
    root = tmp_path / "adapters"
    root.mkdir()
    refusing = Engines(tmp_path, root, served=False)
    try:
        factory, run = evaluated(config, refusing)
        assert not factory.step(run, "publish")
    finally:
        refusing.close()
    error = run.state["steps"]["publish"]["error"]
    assert error.startswith(f"EngineError: The engine at {refusing.path} refused to reload: InvalidRequest: this "
                            "engine has no served file")
    # The clean-up's own reload is refused too, and noted after the cause.
    assert f"The engine at {refusing.path} could not reload after the folder was moved" in error
    (failed,) = folders(root)
    assert failed.startswith(".trading-calls.failed-")
    assert (root / failed / "manifest.yaml").exists()
    rolled_back = [event for event in run.events() if event["kind"] == "publish.rolled_back"]
    assert rolled_back[0]["moved_to"] == failed and len(rolled_back[0]["notes"]) == 1
    assert not (run.folder / "publish.json").exists()


def test_a_failed_example_decision_takes_the_adapter_back_out(config: FactoryConfig, tmp_path: Path) -> None:
    root = tmp_path / "adapters"
    root.mkdir()
    broken = Engines(tmp_path, root, answer=lambda adapter, row: [1.0])  # one probability for three options
    try:
        factory, run = evaluated(config, broken)
        assert not factory.step(run, "publish")
        client = SocketEngine(broken.path, timeout_s=5)
        try:
            assert ADAPTER not in client.installed(refresh=True)
        finally:
            client.close()
    finally:
        broken.close()
    assert run.state["steps"]["publish"]["error"] == (
        f"EngineError: {ADAPTER}.function: the engine returned 1 probabilities for 3 options")
    assert [name.startswith(".trading-calls.failed-") for name in folders(root)] == [True]
    assert broken.served is not None and read_served(broken.served) == []
    assert [event["notes"] for event in run.events() if event["kind"] == "publish.rolled_back"] == [[]]


def test_an_existing_folder_of_that_name_is_refused(config: FactoryConfig, engines: Engines, tmp_path: Path) -> None:
    factory, run = evaluated(config, engines)
    (tmp_path / "adapters" / "trading-calls").mkdir()
    assert not factory.step(run, "publish")
    assert run.state["steps"]["publish"]["error"] == (
        f"Conflict: {tmp_path / 'adapters' / 'trading-calls'} already exists; an adapter of that name is already in "
        "the folder")
    assert folders(tmp_path / "adapters") == ["trading-calls"]
    assert engines.served is not None and read_served(engines.served) == []


def test_an_adapter_named_as_the_base_is_refused_before_copying(config: FactoryConfig, engines: Engines,
                                                               tmp_path: Path) -> None:
    """The worker runs the API's startup check on JEFF_MODEL_NAME too, so no API worker refuses the folder later."""
    factory, run = evaluated(config, engines)
    assert factory.hooks is not None
    factory.hooks = dataclasses.replace(factory.hooks, model_name="trading-calls")  # the adapter's alias
    assert not factory.step(run, "publish")
    error = run.state["steps"]["publish"]["error"]
    assert error.startswith("InvalidRequest: JEFF_MODEL_NAME=trading-calls is also the name of the adapter "
                            f"{ADAPTER}"), error
    assert folders(tmp_path / "adapters") == []
    assert engines.served is not None and read_served(engines.served) == []


def test_a_function_no_threshold_calibrates_stops_the_publish(config: FactoryConfig, engines: Engines,
                                                             tmp_path: Path) -> None:
    factory, run = evaluated(config, engines, wrong_side=True)
    assert not factory.step(run, "publish")
    assert run.state["steps"]["publish"]["error"].startswith(
        "CalibrationError: Question side: No threshold reaches precision 0.95")
    assert folders(tmp_path / "adapters") == []


def test_a_run_trained_on_plain_text_requests_is_refused_before_copying(config: FactoryConfig, engines: Engines,
                                                                        tmp_path: Path) -> None:
    spec = trading_spec()
    spec["family"]["fields"] = []
    run, labels = trained_run(config, spec)
    assert isinstance(read_jsonl(run.data / "test.jsonl")[0]["state"], str)
    factory = Factory(config, worker_hooks(config.required_root, (engines.path,), fake_scorer(labels)))
    assert factory.step(run, "calibrate") and factory.step(run, "evaluate")
    assert not factory.step(run, "publish")
    assert run.state["steps"]["publish"]["error"] == (
        "ManifestError: trading-calls cannot be published as functions: the adapter was trained on a plain-text "
        "request (its family has no named fields), but the factory only publishes adapters trained on named fields, "
        'so Jeff would send {"request": <text>}, a different prompt from the one the adapter was trained on. Give the '
        "family at least one field and train again")
    assert folders(tmp_path / "adapters") == []
    assert not (run.folder / "calibration-served-logits.json").exists()  # refused before any scoring


def rename_question(run: Run, old: str, new: str) -> None:
    """Give question `old` the ID `new` in the run's spec and in every row of its data (id suffix and source)."""
    spec = run.spec
    for question in spec["questions"]:
        if question["id"] == old:
            question["id"] = new
        for condition in question.get("requires", []):
            if condition["question"] == old:
                condition["question"] = new
    write_json(run.folder / "spec.json", spec)
    for path in run.data.glob("*.jsonl"):
        rows = read_jsonl(path)
        for row in rows:
            if row.get("source", {}).get("question_id") == old:
                row["source"]["question_id"] = new
                row["id"] = row["id"][:-len(old)] + new
        write_jsonl(path, rows)


def rename_field(run: Run, old: str, new: str) -> dict[str, Any]:
    """Give family field `old` the name `new` in the run's spec and in every row's state (same order); returns each
    row's label by label_key again, for fake_scorer."""
    spec = run.spec
    for field in spec["family"]["fields"]:
        if field["name"] == old:
            field["name"] = new
    write_json(run.folder / "spec.json", spec)
    labels: dict[str, Any] = {}
    for path in run.data.glob("*.jsonl"):
        rows = read_jsonl(path)
        for row in rows:
            row["state"] = {new if key == old else key: value for key, value in row["state"].items()}
            if "label" in row:
                labels[label_key(row)] = row["label"]
        write_jsonl(path, rows)
    return labels


@pytest.mark.parametrize(("field", "code_name"), [("asset__classes", "asset__classes"), ("company_", "company_"),
                                                  ("Asset classes", "asset_classes")])
def test_a_family_field_is_published_and_its_example_decided_by_its_code_name(
        config: FactoryConfig, engines: Engines, tmp_path: Path, field: str, code_name: str) -> None:
    """A field name that already is a code name keeps it unchanged; any other is called by its derived code name.
    Publishing decides the example through Jeff (decide_example), so it must pass the field by its code name."""
    run, _ = trained_run(config, trading_spec())
    labels = rename_field(run, "asset_classes", field)
    factory = Factory(config, worker_hooks(config.required_root, (engines.path,), fake_scorer(labels)))
    assert factory.step(run, "calibrate") and factory.step(run, "evaluate")
    assert factory.step(run, "publish"), run.state["steps"]["publish"]
    manifest = load_manifest(tmp_path / "adapters" / "trading-calls" / "manifest.yaml")
    assert [(item.key, item.code_name) for item in manifest.context] == [("desk", "desk"), (field, code_name)]
    assert read_json(run.folder / "publish.json")["answer"]["value"] == "place_order"


def test_jeff_test_maps_every_row_of_a_question_id_with_a_capital_and_a_dot(config: FactoryConfig, engines: Engines,
                                                                           tmp_path: Path) -> None:
    run, labels = trained_run(config, trading_spec())
    rename_question(run, "side", "Order.Side")
    factory = Factory(config, worker_hooks(config.required_root, (engines.path,), fake_scorer(labels)))
    assert factory.step(run, "calibrate") and factory.step(run, "evaluate")
    assert factory.step(run, "publish"), run.state["steps"]["publish"]
    manifest = load_manifest(tmp_path / "adapters" / "trading-calls" / "manifest.yaml")
    assert set(manifest.functions) == {"function", "order_side", "urgent"}
    raw_rows = read_jsonl(run.data / "test.jsonl")
    assert any(row["id"].endswith(":Order.Side") for row in raw_rows)

    # What `jeff test` runs on a manifest's test set (jeff.cli._published): every row rebuilt as a call, none skipped.
    chosen = published_set(manifest, "test")
    assert chosen.skipped == {} and len(chosen.rows) == len(raw_rows)
    assert {row.function for row in chosen.rows} == {f"{ADAPTER}.{key}" for key in manifest.functions}
    jeff = Jeff(FakeEngine(answers, {ADAPTER}), Catalogue.shipped().replacing([manifest]))
    reports = {report.function: report for report in score(jeff, chosen.rows)}
    assert set(reports) == {f"{ADAPTER}.{key}" for key in manifest.functions}
    assert sum(report.n for report in reports.values()) == len(raw_rows)
    calibration = published_set(manifest, "calibration")
    assert calibration.skipped == {} and len(calibration.rows) == len(read_jsonl(run.data / "calibration.jsonl"))


def test_calibration_confidence_is_the_one_jeff_compares_with_the_threshold(config: FactoryConfig,
                                                                           engines: Engines, tmp_path: Path) -> None:
    """The calibration rows are scored as Jeff sends them, and evaluate_logits' confidence at the adapter's
    temperature is the confidence Jeff.decide gives for an engine applying that temperature (softmax of logits over
    temperature, as jeff_runtime's engines do)."""
    factory, run = evaluated(config, engines)
    assert factory.step(run, "publish"), run.state["steps"]["publish"]
    manifest = load_manifest(tmp_path / "adapters" / "trading-calls" / "manifest.yaml")
    temperature = 1.7
    def logits_of(question: dict[str, Any]) -> list[float]:
        return [2.0, -1.0, 0.5][:len(question["criteria"])] if question["type"] == "choice" else [0.3, -0.4]

    def engine_answers(adapter: str, row: dict[str, Any]) -> list[float]:
        values = logits_of(row["question"])
        exponents = [math.exp(value / temperature) for value in values]
        return [value / sum(exponents) for value in exponents]

    jeff = Jeff(FakeEngine(engine_answers, {ADAPTER}), Catalogue.shipped().replacing([manifest]))
    context = {"desk": "Rates", "asset_classes": "equity"}
    spec = run.spec
    for question in spec["questions"]:
        function = manifest.functions[question["id"]]
        binding = bind(manifest, function, context, None, calibration=function.calibration)
        sent = build_row(binding, "buy 100 now")
        assert sent["question"] == served_question(question)  # what the publish step scored
        decision = jeff.decide([Call(function.id, "buy 100 now", context)])[0]
        label = evaluation.options(sent["question"])[0]
        row = {"id": "r", "state": sent["state"], "question": sent["question"], "label": label, "target": label}
        (prediction,) = evaluation.evaluate_logits([row], [logits_of(sent["question"])], temperature)
        assert decision.confidence == pytest.approx(prediction["confidence"])
        assert decision.value == prediction["prediction"]


def test_a_failed_rename_leaves_no_staging_folder(config: FactoryConfig, engines: Engines, tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    factory, run = evaluated(config, engines)
    rename = Path.rename

    def refuse(self: Path, target: Any) -> Path:
        if self.name == ".trading-calls.publishing":
            raise OSError("the disk refused the rename")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", refuse)
    assert not factory.step(run, "publish")
    assert run.state["steps"]["publish"]["error"] == "OSError: the disk refused the rename"
    assert folders(tmp_path / "adapters") == []


def test_the_workers_serve_waits_while_the_api_changes_adapters(engines: Engines, tmp_path: Path) -> None:
    """The API holds the adapter-change lock during PUT /v1/adapters/served and POST /v1/adapters/reload; the
    worker's reload-then-serve of a published adapter waits for it (an flock, so across processes too), so the API's
    change is never overwritten by a serve computed before it."""
    root = tmp_path / "adapters"
    folder = root / "jeff.test.waiting"
    folder.mkdir()
    spam = Path(__file__).resolve().parents[4] / "packages/python/tests/fixtures/routers/jeff.safety.spam"
    manifest = {**yaml.safe_load((spam / "manifest.yaml").read_text()), "id": "jeff.test.waiting",
                "aliases": ["waiting"], "weights": {"local": {}}}
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (folder / "adapter_config.json").write_text("{}")
    hooks = worker_hooks(tmp_path, (engines.path,), fake_scorer({}))
    socket = SocketEngine(engines.path, timeout_s=5)
    settings = ServerSettings(engine_sockets=(engines.path,), model_name="jeff-v1.3", adapter_dirs=(root,))
    outcome: list[object] = []
    try:
        with TestClient(create_app(settings, engine=EngineRouter([socket]))) as http:
            lock: AdapterChangeLock = http.app.state.adapter_change_lock  # type: ignore[attr-defined]
            assert lock.path == root / LOCK_NAME
            with lock.held():
                worker = threading.Thread(target=lambda: outcome.append(serve_published(hooks, "jeff.test.waiting")))
                worker.start()
                time.sleep(0.5)
                assert worker.is_alive() and outcome == []
                assert "jeff.test.waiting" not in socket.installed(refresh=True)  # not even reloaded yet
            worker.join(10)
            assert outcome == [{str(engines.path): ["jeff.test.waiting"]}]
    finally:
        socket.close()
