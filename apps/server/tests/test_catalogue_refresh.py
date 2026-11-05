"""The API's catalogue includes the adapter folders under JEFF_ADAPTER_DIRS and is rebuilt when the engines' adapters
change: at once in the worker that ran POST /v1/adapters/reload, and in every other worker on its next request that
sees the engines' new adapters_version.

Engines here are real engine processes' servers (jeff_runtime.testing.Running over a FakeEngine, with the adapters
dir as --adapters-dir) reached through SocketEngine and the EngineRouter, as in production."""

import json
import shutil
import time
from collections.abc import Iterator
from contextlib import closing
from pathlib import Path
from typing import Any

import pytest
import yaml
from fastapi.testclient import TestClient

from jeff.errors import EngineError, ManifestError
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine
from jeff_runtime.engine_server import SERVED_FORMAT, SERVED_VERSION
from jeff_runtime.testing import Running

from jeff_server import catalogue_refresh
from jeff_server.app import create_app
from jeff_server.router import EngineRouter
from jeff_server.settings import ServerSettings

from channel_testing import start

ROOT = Path(__file__).resolve().parents[3]
ROUTERS = ROOT / "packages" / "python" / "tests" / "fixtures" / "routers"
CATALOGUE = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
FIXED = {adapter: Path("/weights") / adapter for adapter in CATALOGUE}
SPAM_MANIFEST = yaml.safe_load((ROUTERS / "jeff.safety.spam" / "manifest.yaml").read_text())


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def adapter_folder(root: Path, adapter_id: str, alias: str, version: str = "1.0.0") -> Path:
    """An adapter folder as an engine finds one: manifest.yaml (the spam fixture's, under another ID and alias, its
    weights the folder itself) and adapter_config.json."""
    folder = root / adapter_id
    folder.mkdir(parents=True)
    manifest = {**SPAM_MANIFEST, "id": adapter_id, "aliases": [alias], "version": version,
                "weights": {"local": {}}}
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest))
    (folder / "adapter_config.json").write_text("{}")
    return folder


def served_file(path: Path, adapters: set[str]) -> Path:
    path.write_text(json.dumps({"format": SERVED_FORMAT, "version": SERVED_VERSION, "adapters": sorted(adapters)}))
    return path


def call(adapter_id: str) -> dict[str, Any]:
    return {"calls": [{"function": f"{adapter_id}.message_type", "input": "WIN NOW", "context": {"channel": "email"}}]}


class Deployment:
    """Engine processes started with `root` as their adapters dir and a served file each, and API apps (workers)
    over them, each with its own SocketEngines, as `jeff-server serve --workers N` runs them."""

    def __init__(self, tmp_path: Path, root: Path, served: set[str], count: int = 2) -> None:
        self.root = root
        self.files = [served_file(tmp_path / f"served-{index}.json", served) for index in range(count)]
        self.servers = [Running(FakeEngine(answers, set(served)), served_file=file, fixed=FIXED, adapter_roots=[root])
                        for file in self.files]
        self.sockets: list[SocketEngine] = []

    def app(self) -> TestClient:
        engines = [SocketEngine(server.path, timeout_s=5) for server in self.servers]
        self.sockets.extend(engines)
        settings = ServerSettings(engine_sockets=tuple(server.path for server in self.servers),
                                  model_name="jeff-v1.3", catalogue=ROUTERS, adapter_dirs=(self.root,))
        return TestClient(create_app(settings, engine=EngineRouter(engines)))

    def serve(self, adapters: set[str]) -> None:
        """What each engine's served file lists (read by the next reload)."""
        for file in self.files:
            served_file(file, adapters)

    def close(self) -> None:
        for engine in self.sockets:
            engine.close()
        for server in self.servers:
            server.close()


@pytest.fixture
def deployment(tmp_path: Path) -> Iterator[Deployment]:
    root = tmp_path / "adapters"
    adapter_folder(root, "jeff.test.first", "first")
    running = Deployment(tmp_path, root, CATALOGUE | {"jeff.test.first"})
    yield running
    running.close()


def function_ids(http: TestClient) -> set[str]:
    response = http.get("/v1/functions")
    assert response.status_code == 200, response.text
    return {function["id"] for function in response.json()["functions"]}


def test_the_functions_of_an_adapter_folder_are_listed(deployment: Deployment) -> None:
    with deployment.app() as http:
        assert "jeff.test.first.message_type" in function_ids(http)
        assert http.post("/v1/decide", json=call("jeff.test.first")).status_code == 200


def test_a_reload_adds_a_dropped_in_folder_in_this_worker_at_once(deployment: Deployment) -> None:
    with deployment.app() as http:
        adapter_folder(deployment.root, "jeff.test.second", "second")
        deployment.serve(CATALOGUE | {"jeff.test.first", "jeff.test.second"})
        response = http.post("/v1/adapters/reload")
        assert response.status_code == 200, response.text
        listed = {adapter["id"]: adapter["served"] for adapter in response.json()["adapters"]}
        assert listed["jeff.test.second"] is True
        assert {"jeff.test.first.message_type", "jeff.test.second.message_type"} <= function_ids(http)
        response = http.post("/v1/decide", json=call("second"))
        assert response.status_code == 200, response.text
        assert response.json()["decisions"][0]["decided_by"] == "jeff.test.second.message_type@1.0.0"


def test_another_worker_sees_the_new_function_after_its_next_request(deployment: Deployment) -> None:
    with deployment.app() as first, deployment.app() as second:
        assert "jeff.test.second.message_type" not in function_ids(second)
        adapter_folder(deployment.root, "jeff.test.second", "second")
        deployment.serve(CATALOGUE | {"jeff.test.first", "jeff.test.second"})
        assert first.post("/v1/adapters/reload").status_code == 200
        # The second worker's next request carries the engines' new adapters_version back to it; the request
        # after that runs on the rebuilt catalogue.
        assert second.post("/v1/decide", json=call("first")).status_code == 200
        assert "jeff.test.second.message_type" in function_ids(second)
        assert second.post("/v1/decide", json=call("second")).status_code == 200


def test_a_broken_folder_refuses_the_reload_and_the_old_functions_still_answer(deployment: Deployment) -> None:
    with deployment.app() as http:
        broken = deployment.root / "jeff.test.broken"
        broken.mkdir()
        (broken / "manifest.yaml").write_text("id: jeff.test.broken\nversion: [not a version\n")
        (broken / "adapter_config.json").write_text("{}")
        response = http.post("/v1/adapters/reload")
        assert response.status_code == 500, response.text
        error = response.json()["error"]
        assert error["type"] == "ManifestError", error
        assert "jeff.test.broken" in error["message"]
        assert "jeff.test.first.message_type" in function_ids(http)
        assert http.post("/v1/decide", json=call("first")).status_code == 200


def test_a_folder_whose_manifest_has_no_description_refuses_the_reload(deployment: Deployment) -> None:
    """GET /v1/adapters lists each adapter with its one-line description (hub.summary) as written: a manifest
    without one is refused when the catalogue is built, before any engine changes, never listed with a made-up or
    empty description."""
    with deployment.app() as http:
        folder = adapter_folder(deployment.root, "jeff.test.undescribed", "undescribed")
        manifest = yaml.safe_load((folder / "manifest.yaml").read_text())
        del manifest["hub"]
        (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest))
        deployment.serve(CATALOGUE | {"jeff.test.first", "jeff.test.undescribed"})
        response = http.post("/v1/adapters/reload")
        assert response.status_code == 500, response.text
        error = response.json()["error"]
        assert error["type"] == "ManifestError", error
        assert "jeff.test.undescribed" in error["message"] and "hub.summary" in error["message"]
        assert "jeff.test.undescribed" not in {entry["id"] for entry in http.get("/v1/adapters").json()["adapters"]}


def test_a_failed_rebuild_in_another_worker_is_a_manifest_error_and_its_old_functions_still_answer(
        deployment: Deployment) -> None:
    with deployment.app() as first, deployment.app() as second:
        function_ids(second)
        adapter_folder(deployment.root, "jeff.test.second", "second")
        deployment.serve(CATALOGUE | {"jeff.test.first", "jeff.test.second"})
        assert first.post("/v1/adapters/reload").status_code == 200
        # Only the second worker's view of the folders breaks (an alias clash with the catalogue's spam).
        manifest = deployment.root / "jeff.test.second" / "manifest.yaml"
        manifest.write_text(manifest.read_text().replace("- second", "- spam"))
        assert second.post("/v1/decide", json=call("first")).status_code == 200  # learns the new version
        response = second.post("/v1/decide", json=call("first"))
        assert response.status_code == 500, response.text
        error = response.json()["error"]
        assert error["type"] == "ManifestError", error
        assert "spam" in error["message"]
        # Logged once and kept: the next requests answer from the catalogue this worker had.
        assert second.post("/v1/decide", json=call("first")).status_code == 200
        assert "jeff.test.second.message_type" not in function_ids(second)


def test_a_decision_with_nothing_changed_reads_no_folder(deployment: Deployment,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    with deployment.app() as http:
        http.post("/v1/decide", json=call("first"))  # the engines' versions are known from here on
        scans: list[object] = []
        real_scan = catalogue_refresh.scan

        def counted(roots: Any) -> Any:
            scans.append(roots)
            return real_scan(roots)

        monkeypatch.setattr(catalogue_refresh, "scan", counted)
        asked: list[Path] = []
        real_installed_now = SocketEngine._installed_now

        def counted_installed(engine: SocketEngine, timeout_s: float | None = None) -> tuple[int, set[str]]:
            asked.append(engine.path)
            return real_installed_now(engine, timeout_s)

        monkeypatch.setattr(SocketEngine, "_installed_now", counted_installed)
        for _ in range(20):
            assert http.post("/v1/decide", json=call("first")).status_code == 200
        assert scans == []
        assert asked == []  # no `installed` asked of any engine either


def fake_settings(root: Path) -> ServerSettings:
    return ServerSettings(engine_sockets=(Path("/unused.sock"),), model_name="jeff-v1.3", catalogue=ROUTERS,
                          adapter_dirs=(root,))


def test_a_catalogue_id_found_in_a_folder_takes_the_folders_manifest(tmp_path: Path) -> None:
    """As the engine does: it serves the adapter from the folder, so the API answers from that folder's manifest."""
    adapter_folder(tmp_path, "jeff.safety.spam", "spam", version="1.4.0")
    with TestClient(create_app(fake_settings(tmp_path), engine=FakeEngine(answers, set(CATALOGUE)))) as http:
        listed = {adapter["id"]: adapter for adapter in http.get("/v1/functions").json()["functions"]}
        assert listed["jeff.safety.spam.message_type"]["adapter_version"] == "1.4.0"


def test_a_found_alias_naming_another_catalogue_adapter_stops_startup(tmp_path: Path) -> None:
    adapter_folder(tmp_path, "jeff.test.clash", "spam")
    with pytest.raises(ManifestError, match="spam"):
        with TestClient(create_app(fake_settings(tmp_path), engine=FakeEngine(answers, set(CATALOGUE)))):
            pass


def mcp_tools(http: TestClient) -> set[str]:
    response = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                         headers={"Accept": "application/json, text/event-stream",
                                  "MCP-Protocol-Version": "2025-11-25"})
    assert response.status_code == 200, response.text
    return {tool["name"] for tool in response.json()["result"]["tools"]}


def test_the_mcp_tools_follow_a_rebuilt_catalogue(deployment: Deployment) -> None:
    with deployment.app() as http:
        before = mcp_tools(http)
        adapter_folder(deployment.root, "jeff.test.second", "second")
        deployment.serve(CATALOGUE | {"jeff.test.first", "jeff.test.second"})
        assert http.post("/v1/adapters/reload").status_code == 200
        added = mcp_tools(http) - before
        assert len(added) == 1 and "second" in next(iter(added)), added


def test_a_channel_handle_is_answered_on_the_rebuilt_catalogue_as_new_binds_are(deployment: Deployment) -> None:
    """A handle's calls run on the catalogue in force when they arrive (so its access and escalation steps are
    checked again: jeff_server.channel.Handle), not the one it was bound on."""
    with deployment.app() as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws)
        bound = channel.result({"op": "bind", "id": 1, "function": "first.message_type",
                                "context": {"channel": "email"}})
        assert bound["version"] == "1.0.0"
        manifest = deployment.root / "jeff.test.first" / "manifest.yaml"
        manifest.write_text(manifest.read_text().replace("version: 1.0.0", "version: 2.0.0"))
        assert http.post("/v1/adapters/reload").status_code == 200
        answer = channel.result({"op": "call", "id": 2, "handle": bound["handle"], "input": "WIN NOW"})
        assert answer["decision"]["decided_by"] == "jeff.test.first.message_type@2.0.0"
        rebound = channel.result({"op": "bind", "id": 3, "function": "first.message_type",
                                  "context": {"channel": "email"}})
        assert rebound["version"] == "2.0.0"


def add_second(deployment: Deployment) -> None:
    adapter_folder(deployment.root, "jeff.test.second", "second")
    deployment.serve(CATALOGUE | {"jeff.test.first", "jeff.test.second"})


def test_another_workers_first_request_for_the_new_function_succeeds(deployment: Deployment) -> None:
    """A request for a function the worker's catalogue does not have never reaches an engine, so it asks the
    engines' versions itself, finds the change, and is planned again on the rebuilt catalogue."""
    with deployment.app() as first, deployment.app() as second:
        add_second(deployment)
        assert first.post("/v1/adapters/reload").status_code == 200
        response = second.post("/v1/decide", json=call("second"))
        assert response.status_code == 200, response.text
        assert response.json()["decisions"][0]["decided_by"] == "jeff.test.second.message_type@1.0.0"


def test_unknown_names_ask_the_engines_at_most_once_a_second(deployment: Deployment,
                                                             monkeypatch: pytest.MonkeyPatch) -> None:
    with deployment.app() as http:
        asked: list[Path] = []
        real = SocketEngine.installed_version

        def counted(engine: SocketEngine, timeout_s: float | None = None) -> tuple[int, set[str]]:
            asked.append(engine.path)
            return real(engine, timeout_s)

        monkeypatch.setattr(SocketEngine, "installed_version", counted)
        for _ in range(2):
            response = http.post("/v1/decide", json=call("jeff.test.nowhere"))
            assert response.status_code == 404, response.text
            assert response.json()["error"]["type"] == "UnknownFunction"
        assert sorted(asked) == sorted(server.path for server in deployment.servers)  # one check: each engine once


def test_a_rebuild_the_engines_cannot_answer_is_one_engine_error_and_the_old_functions_still_answer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One engine, so the version the failed rebuild records is the whole deployment's."""
    root = tmp_path / "adapters"
    adapter_folder(root, "jeff.test.first", "first")
    deployment = Deployment(tmp_path, root, CATALOGUE | {"jeff.test.first"}, count=1)
    try:
        with deployment.app() as first, deployment.app() as second:
            add_second(deployment)
            assert first.post("/v1/adapters/reload").status_code == 200
            router = second.app.state.engine  # type: ignore[attr-defined]

            def unreachable() -> str | None:  # the rebuild asks every engine; deciding reads the cached lists
                raise EngineError("The engine at e.sock closed the connection (this test)")

            monkeypatch.setattr(router, "disagreement", unreachable)
            assert second.post("/v1/decide", json=call("first")).status_code == 200  # learns the new version
            response = second.post("/v1/decide", json=call("first"))
            assert response.status_code == 500, response.text
            error = response.json()["error"]
            assert error["type"] == "EngineError" and "this test" in error["message"], error
            response = second.post("/v1/decide", json=call("first"))
            assert response.status_code == 200, response.text
    finally:
        deployment.close()


def test_a_reload_whose_rebuild_fails_says_the_engines_have_changed(deployment: Deployment,
                                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    with deployment.app() as http:
        add_second(deployment)

        def broken(settings: ServerSettings) -> Any:
            raise ManifestError("the folders changed between the check and the rebuild (this test)")

        # Only the rebuild after the reload; the check before it (jeff_server.operations) still passes.
        monkeypatch.setattr(catalogue_refresh, "build_catalogue", broken)
        response = http.post("/v1/adapters/reload")
        assert response.status_code == 500, response.text
        error = response.json()["error"]
        assert error["type"] == "ManifestError", error
        assert "reload was applied on every engine" in error["message"] and "this test" in error["message"]
        assert http.post("/v1/decide", json=call("first")).status_code == 200


def test_a_broken_folder_refuses_a_served_change_with_no_engine_changed(deployment: Deployment) -> None:
    with deployment.app() as http:
        before = [set(server.server._snapshot[1]) for server in deployment.servers]
        broken = deployment.root / "jeff.test.broken"
        broken.mkdir()
        (broken / "manifest.yaml").write_text("id: jeff.test.broken\nversion: [not a version\n")
        (broken / "adapter_config.json").write_text("{}")
        response = http.put("/v1/adapters/served", json={"adapters": sorted(CATALOGUE)})
        assert response.status_code == 500, response.text
        assert response.json()["error"]["type"] == "ManifestError"
        assert [set(server.server._snapshot[1]) for server in deployment.servers] == before


def test_an_engine_whose_version_is_not_known_yet_causes_no_rebuild(deployment: Deployment) -> None:
    with deployment.app() as http:
        planner = http.app.state.planner  # type: ignore[attr-defined]
        engine = http.app.state.engine.engines[0]  # type: ignore[attr-defined]
        built = planner.current()
        engine._snapshot = (-1, engine._snapshot[1])  # as just after a reconnect, before its first reply
        assert planner.current() is built


def test_another_workers_first_bind_or_tool_call_for_the_new_function_succeeds(deployment: Deployment) -> None:
    with deployment.app() as first, deployment.app() as second, deployment.app() as third:
        before = mcp_tools(first)
        add_second(deployment)
        assert first.post("/v1/adapters/reload").status_code == 200
        (tool,) = mcp_tools(first) - before
        with second.websocket_connect("/v1/channel") as ws:
            bound = start(ws).result({"op": "bind", "id": 1, "function": "second.message_type",
                                      "context": {"channel": "email"}})
            assert bound["function"] == "jeff.test.second.message_type"
        response = third.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                            "params": {"name": tool, "arguments": {
                                                "input": "WIN NOW", "context": {"channel": "email"}}}},
                              headers={"Accept": "application/json, text/event-stream",
                                       "MCP-Protocol-Version": "2025-11-25"})
        assert response.status_code == 200, response.text
        result = response.json()["result"]
        assert not result.get("isError"), result
        assert result["structuredContent"]["decided_by"] == "jeff.test.second.message_type@1.0.0"


def change_engine(server: Running, adapter: str) -> None:
    """What the factory's publish does to one engine, over a connection of its own: reload (finding the new folder),
    then serve `adapter` beside what it serves."""
    with closing(SocketEngine(server.path, timeout_s=5)) as engine:
        engine.serve(sorted(engine.reload() | {adapter}))


def calls_on_every_engine(http: TestClient, adapter: str) -> list[int]:
    """Status codes of decisions spread over the engines (each context is another binding, hashed to an engine)."""
    return [http.post("/v1/decide", json={"calls": [{"function": f"{adapter}.message_type", "input": "WIN NOW",
                                                     "context": {"channel": f"email-{index}"}}]}).status_code
            for index in range(8)]


def test_engines_changed_one_at_a_time_keep_answering_and_the_new_function_follows(deployment: Deployment) -> None:
    """The factory's publish changes the engines one at a time. Between the two, a decision for a function every
    engine serves is answered (never an "engines disagree" 500), and once both have changed the new function is
    callable."""
    with deployment.app() as http:
        assert http.post("/v1/decide", json=call("first")).status_code == 200
        adapter_folder(deployment.root, "jeff.test.second", "second")
        change_engine(deployment.servers[0], "jeff.test.second")
        assert calls_on_every_engine(http, "jeff.test.first") == [200] * 8
        change_engine(deployment.servers[1], "jeff.test.second")
        assert calls_on_every_engine(http, "jeff.test.first") == [200] * 8
        response = http.post("/v1/decide", json=call("second"))
        assert response.status_code == 200, response.text
        assert response.json()["decisions"][0]["decided_by"] == "jeff.test.second.message_type@1.0.0"


def test_the_router_answers_what_every_engine_serves_while_they_differ(deployment: Deployment) -> None:
    engines = [SocketEngine(server.path, timeout_s=5) for server in deployment.servers]
    router = EngineRouter(engines)
    try:
        before = CATALOGUE | {"jeff.test.first"}
        adapter_folder(deployment.root, "jeff.test.second", "second")
        engines[0].serve(sorted(engines[0].reload() | {"jeff.test.second"}))  # the router's cache of engine 0
        assert router.installed() == before  # engine 1 really serves less: the intersection
        with pytest.raises(EngineError, match="disagree"):
            router.installed(refresh=True)
        assert router.disagreement() is not None
        change_engine(deployment.servers[1], "jeff.test.second")  # behind the router's back: its cache is stale
        assert engines[1].installed() == before
        assert router.installed() == before | {"jeff.test.second"}  # the lists differed, so every engine was asked
        assert engines[1].installed() == before | {"jeff.test.second"}
        assert router.disagreement() is None
    finally:
        router.close()


def test_a_restarted_engine_with_other_adapters_at_the_same_version_is_noticed(tmp_path: Path) -> None:
    """An engine that never changed is at adapters_version 0; restarted with another folder and served file it is at
    0 again. The reconnect alone moves the planner's key, so the new function is callable with no reload."""
    root = tmp_path / "adapters"
    adapter_folder(root, "jeff.test.first", "first")
    deployment = Deployment(tmp_path, root, CATALOGUE | {"jeff.test.first"}, count=1)
    try:
        with deployment.app() as http:
            assert http.post("/v1/decide", json=call("first")).status_code == 200
            engine = http.app.state.engine.engines[0]  # type: ignore[attr-defined]
            old = deployment.servers[0]
            old.close()
            for _ in range(500):  # the SocketEngine's reader notices the closed connection a moment later
                if engine._closed is not None:
                    break
                time.sleep(0.01)
            adapter_folder(root, "jeff.test.second", "second")
            deployment.serve(CATALOGUE | {"jeff.test.first", "jeff.test.second"})
            deployment.servers[0] = Running(FakeEngine(answers, CATALOGUE | {"jeff.test.first", "jeff.test.second"}),
                                            served_file=deployment.files[0], fixed=FIXED, adapter_roots=[root],
                                            path=old.path)
            assert http.post("/v1/decide", json=call("first")).status_code == 200  # reconnects
            assert engine.adapters_version == 0 and engine.reconnects == 1
            response = http.post("/v1/decide", json=call("second"))
            assert response.status_code == 200, response.text
            assert response.json()["decisions"][0]["decided_by"] == "jeff.test.second.message_type@1.0.0"
    finally:
        deployment.close()


def test_the_adapter_listing_says_whether_each_function_has_a_threshold_for_the_served_base(tmp_path: Path) -> None:
    """catalogue_refresh.adapter_entries, built once per catalogue: on a bfloat16 base a function is calibrated when
    it has a calibration block; on an INT4 base only when it has a calibration_by_format entry for exactly that
    base's format_id. Only adapters trained for the served base are listed."""
    root = tmp_path / "catalogue"
    shutil.copytree(ROUTERS, root)
    path = root / "jeff.support.triage" / "manifest.yaml"
    raw = yaml.safe_load(path.read_text())
    raw["functions"]["urgency"]["calibration_by_format"] = {"jeff-base@v1.3+int4-g64-gptq.0123abcd": {
        "data": "fixture", "rows": 98, "excluded": "none", "target_precision": 0.9, "threshold": 0.8,
        "precision_at_threshold": 0.92, "coverage_at_threshold": 0.7}}
    path.write_text(yaml.safe_dump(raw))
    catalogue = catalogue_refresh.catalogue_with_folders(root, ())

    def calibrated(format_id: str | None) -> dict[str, bool]:
        entries = dict(catalogue_refresh.adapter_entries(catalogue, ("jeff-base", "v1.3"), format_id))
        return {function["id"]: function["calibrated"] for function in entries["jeff.support.triage"]["functions"]}

    assert calibrated(None) == {"jeff.support.triage.route": False, "jeff.support.triage.urgency": True,
                                "jeff.support.triage.needs_human": False}
    assert calibrated("jeff-base@v1.3+int4-g64-gptq.0123abcd")["jeff.support.triage.urgency"] is True
    assert calibrated("jeff-base@v1.3+int4-g64-gptq.feedf00d")["jeff.support.triage.urgency"] is False
    assert catalogue_refresh.adapter_entries(catalogue, ("jeff-base", "v1.4"), None) == ()
