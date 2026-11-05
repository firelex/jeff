"""Private adapters (jeff_server.access), the security review's findings: no error names a private adapter to a caller
who is not the owner (jeff_server.redaction), a channel handle follows the access in force at each call, and an
adapter folder's manifest may not open an adapter wider than the catalogue's manifest it replaces.

The catalogue and callers are test_access.py's: jeff.private.secret is the owner's only, the two routers are the
owner's and acct1's, spam and triage are everyone's."""

import logging
import shutil
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jeff.errors import EngineError, InvalidRequest, ManifestError, NotInstalled
from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine, fake_adapter_folder
from jeff_runtime.testing import Running
from jeff_runtime.wording import loaded

from jeff_server import channel as channel_module
from jeff_server import redaction
from jeff_server.app import create_app
from jeff_server.catalogue_refresh import catalogue_with_folders
from jeff_server.router import EngineRouter
from jeff_server.settings import AccountsSettings, ServerSettings

from accounts_testing import HUB_URL, KEY, OTHER_ACCOUNTS_KEY, OWNER_KEY, SECRET, Hub, answers, bearer
from channel_testing import assert_error, start
from test_access import (ALL_IDS, MCP_HEADERS, PUBLIC_IDS, ROUTER_CALL, ROUTER_IDS, SECRET_ID, private_catalogue)

PRIVATE_IDS = ALL_IDS - PUBLIC_IDS


@pytest.fixture(autouse=True)
def fresh_log_limit() -> Iterator[None]:
    """Each test starts with no reworded error logged yet (redaction logs one per type per minute)."""
    redaction._LOGGED.clear()
    yield
    redaction._LOGGED.clear()


@contextmanager
def deployment(folder: Path, engines: int = 1, adapter_dirs: tuple[Path, ...] = (),
               private_adapters: tuple[str, ...] = (), engine_answers: Any = answers
               ) -> Iterator[tuple[TestClient, FastAPI, list[SocketEngine], Path]]:
    """An app in accounts mode over `engines` engine processes, each serving every adapter, on a fresh copy of the
    private catalogue (returned, so a test can change it)."""
    catalogue = private_catalogue(folder)
    fixed = {adapter: Path("/weights") / adapter for adapter in ALL_IDS}
    servers = [Running(FakeEngine(engine_answers, set(ALL_IDS)), fixed=fixed) for _ in range(engines)]
    sockets = [SocketEngine(server.path, timeout_s=5) for server in servers]
    token_file = folder / "accounts-token"
    token_file.write_text(SECRET + "\n")
    token_file.chmod(0o600)
    accounts = AccountsSettings(url=HUB_URL, token_file=token_file, token=SECRET, max_stale_s=300.0,
                                usage_file=folder / "usage.jsonl", bypass_file=None, bypass=None)
    settings = ServerSettings(engine_sockets=tuple(server.path for server in servers), model_name="jeff-v1.3",
                              catalogue=catalogue, api_key=OWNER_KEY, rate_limit=(1000.0, 8), accounts=accounts,
                              adapter_dirs=adapter_dirs, private_adapters=private_adapters)
    app = create_app(settings, engine=EngineRouter(sockets),
                     accounts_http=httpx.Client(transport=httpx.MockTransport(Hub().handler)))
    try:
        with TestClient(app) as http:
            app.state.accounts.refresh()
            yield http, app, sockets, catalogue
    finally:
        for engine in sockets:
            engine.close()
        for server in servers:
            server.close()


# ---- 1. engines that disagree ----------------------------------------------------------------------------------------

def _get(http: TestClient, path: str, headers: dict[str, str]) -> str:
    """The text of the failure the route answers (an MCP tools/list's JSON-RPC error, or an HTTP error body)."""
    if path == "mcp tools/list":
        response = http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                             headers={**MCP_HEADERS, **headers})
        assert "error" in response.json(), response.text
        return response.text
    response = http.get(path, headers=headers)
    assert response.status_code == 500, response.text
    assert response.json()["error"]["type"] == "EngineError", response.text
    return response.text


ROUTES = ["/v1/functions", "/v1/models", "/v1/adapters", "/ui/examples", "mcp tools/list"]


@pytest.mark.parametrize("path", ROUTES)
def test_engines_that_disagree_are_reported_without_private_adapters_to_a_customer(
        tmp_path: Path, path: str, caplog: pytest.LogCaptureFixture) -> None:
    with deployment(tmp_path, engines=2) as (http, _, engines, _):
        # the second engine stops serving spam, as while PUT /v1/adapters/served goes round one engine at a time
        engines[1].serve(sorted(ALL_IDS - {"jeff.safety.spam"}))
        with caplog.at_level(logging.ERROR, logger="jeff_server"):
            customer = _get(http, path, bearer(OTHER_ACCOUNTS_KEY))
        owner = _get(http, path, bearer(OWNER_KEY))
    assert not [adapter for adapter in PRIVATE_IDS if adapter in customer], customer
    assert "jeff.safety.spam" in customer and "jeff.support.triage" in customer  # the lists are there, cut
    assert all(adapter in owner for adapter in PRIVATE_IDS), owner
    # the full text is in the server's log
    assert any(SECRET_ID in record.getMessage() and "not the owner" in record.getMessage()
               for record in caplog.records)


def test_an_account_sees_the_private_adapters_it_may_use_in_a_disagreement(tmp_path: Path) -> None:
    with deployment(tmp_path, engines=2) as (http, _, engines, _):
        engines[1].serve(sorted(ALL_IDS - {"jeff.safety.spam"}))
        text = _get(http, "/v1/functions", bearer(KEY))
    assert all(router in text for router in ROUTER_IDS) and SECRET_ID not in text


# ---- 2. channel handles follow the access in force -------------------------------------------------------------------

def _set_access(catalogue: Path, adapter: str, access: object) -> None:
    path = catalogue / adapter / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["access"] = access
    path.write_text(yaml.safe_dump(manifest, sort_keys=False))


@pytest.mark.parametrize("access", ["owner", {"accounts": ["acct2"]}], ids=["made owner-only", "acct1 revoked"])
def test_a_handle_whose_adapter_no_longer_admits_the_caller_is_refused_as_unknown_and_dropped(
        tmp_path: Path, access: object) -> None:
    with deployment(tmp_path) as (http, app, _, catalogue):
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws, KEY)
            handle = channel.result({"op": "bind", "id": 2, "function": ROUTER_CALL["function"],
                                     "labels": ROUTER_CALL["labels"]})["handle"]
            call = {"op": "call", "id": 3, "handle": handle, "input": "My wardrobe is broken"}
            assert channel.result(call)["decision"]["value"] in ROUTER_CALL["labels"]
            for router in ROUTER_IDS:
                _set_access(catalogue, router, access)
            app.state.planner.refresh()  # what POST /v1/adapters/reload does after the engines reload
            refused = assert_error(channel.request({**call, "id": 5}), "UnknownFunction")
            unknown = assert_error(channel.request({"op": "decide", "id": 6, "calls": [
                {**ROUTER_CALL, "function": "jeff.support.unknownx.function"}]}), "UnknownFunction")
            assert refused["message"].replace("jeff.support.router", "jeff.support.unknownx") == unknown["message"]
            # the handle is gone
            assert_error(channel.request({**call, "id": 7}), "InvalidRequest", f"holds no handle {handle}")
        # the owner still binds and calls it
        with http.websocket_connect("/v1/channel") as ws:
            owner = start(ws, OWNER_KEY)
            handle = owner.result({"op": "bind", "id": 2, "function": ROUTER_CALL["function"],
                                   "labels": ROUTER_CALL["labels"]})["handle"]
            owner.result({"op": "call", "id": 3, "handle": handle, "input": "My wardrobe is broken"})


def test_a_handle_still_admitted_keeps_answering_after_a_rebuild(tmp_path: Path) -> None:
    with deployment(tmp_path) as (http, app, _, catalogue):
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws, KEY)
            handle = channel.result({"op": "bind", "id": 2, "function": ROUTER_CALL["function"],
                                     "labels": ROUTER_CALL["labels"]})["handle"]
            for router in ROUTER_IDS:
                _set_access(catalogue, router, {"accounts": ["acct1", "acct2"]})
            app.state.planner.refresh()
            channel.result({"op": "call", "id": 3, "handle": handle, "input": "My wardrobe is broken"})


# ---- 3. a folder's manifest may not widen the catalogue's access ---------------------------------------------------

def _folder_copy(root: Path, catalogue: Path, adapter: str, access: object) -> Path:
    """An adapter folder under `root` holding the catalogue's manifest of `adapter` with `access` (None: no access
    field)."""
    folder = fake_adapter_folder(root / adapter, adapter)
    manifest = yaml.safe_load((catalogue / adapter / "manifest.yaml").read_text())
    manifest.pop("access", None)
    if access is not None:
        manifest["access"] = access
    (folder / "manifest.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False))
    return folder


@pytest.mark.parametrize(("adapter", "access", "named"), [
    (SECRET_ID, None, "no access field (everyone) instead of access: owner"),
    (SECRET_ID, {"accounts": ["acct1"]}, "access: {accounts: [acct1]} instead of access: owner"),
    ("jeff.support.router", {"accounts": ["acct1", "acct2"]},
     "access: {accounts: [acct1, acct2]} instead of access: {accounts: [acct1]}"),
    ("jeff.support.router", None, "no access field (everyone) instead of access: {accounts: [acct1]}")])
def test_a_folder_manifest_wider_than_the_catalogues_is_refused_naming_both(tmp_path: Path, adapter: str,
                                                                            access: object, named: str) -> None:
    catalogue = private_catalogue(tmp_path)
    root = tmp_path / "adapters"
    folder = _folder_copy(root, catalogue, adapter, access)
    with pytest.raises(ManifestError) as raised:
        catalogue_with_folders(catalogue, (root,))
    message = str(raised.value)
    assert named in message and adapter in message
    assert str(folder / "manifest.yaml") in message and str(catalogue / adapter / "manifest.yaml") in message


@pytest.mark.parametrize(("adapter", "access"), [
    (SECRET_ID, "owner"), ("jeff.support.router", {"accounts": ["acct1"]}), ("jeff.core.router", "owner"),
    ("jeff.safety.spam", None), ("jeff.support.triage", None)])
def test_a_folder_manifest_as_narrow_or_narrower_is_accepted(tmp_path: Path, adapter: str, access: object) -> None:
    catalogue = private_catalogue(tmp_path)
    root = tmp_path / "adapters"
    _folder_copy(root, catalogue, adapter, access)
    catalogue_with_folders(catalogue, (root,))


def test_a_wider_folder_manifest_stops_the_server_starting(tmp_path: Path) -> None:
    catalogue = private_catalogue(tmp_path)
    root = tmp_path / "adapters"
    _folder_copy(root, catalogue, SECRET_ID, None)
    settings = ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                              catalogue=catalogue, api_key=OWNER_KEY, adapter_dirs=(root,))
    with pytest.raises(ManifestError, match="wider access"):
        with TestClient(create_app(settings, engine=FakeEngine(answers, set(ALL_IDS)))):
            pass


def test_a_wider_folder_manifest_stops_a_reload_and_the_old_catalogue_stays(tmp_path: Path) -> None:
    root = tmp_path / "adapters"
    root.mkdir()
    with deployment(tmp_path, adapter_dirs=(root,)) as (http, app, _, catalogue):
        _folder_copy(root, catalogue, SECRET_ID, None)
        with pytest.raises(ManifestError, match="wider access"):
            app.state.planner.refresh()
        response = http.get("/v1/functions", headers=bearer(OTHER_ACCOUNTS_KEY))
        assert response.status_code == 200 and SECRET_ID not in response.text


def test_the_access_of_every_private_adapter_is_logged_at_startup_and_after_a_rebuild(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    expected = ("Private adapters in the catalogue: jeff.core.router: the owner and account(s) acct1; "
                "jeff.private.secret: the owner only; jeff.support.router: the owner and account(s) acct1")
    with caplog.at_level(logging.INFO, logger="jeff_server"):
        with deployment(tmp_path) as (_, app, _, _):
            assert [record.getMessage() for record in caplog.records].count(expected) == 1
            app.state.planner.refresh()
            assert [record.getMessage() for record in caplog.records].count(expected) == 2


# ---- 4. a failed rebuild ---------------------------------------------------------------------------------------------

def test_a_failed_rebuild_names_its_cause_only_to_the_owner(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    with deployment(tmp_path) as (http, _, engines, catalogue):
        broken = catalogue / SECRET_ID / "manifest.yaml"
        broken.write_text(broken.read_text().replace("access: owner", "access: everyone"))

        def after_a_change(headers: dict[str, str]) -> dict[str, Any]:
            served = sorted(engines[0].installed(refresh=True))
            engines[0].serve(served[:-1])  # a change of the engines' adapters: the next request rebuilds
            engines[0].serve(served)
            response = http.get("/v1/functions", headers=headers)
            assert response.status_code >= 400, response.text
            return dict(response.json()["error"])

        with caplog.at_level(logging.ERROR, logger="jeff_server"):
            customer = after_a_change(bearer(OTHER_ACCOUNTS_KEY))
        owner = after_a_change(bearer(OWNER_KEY))
    assert customer["type"] == owner["type"] == "ManifestError"
    assert SECRET_ID not in customer["message"] and "access" not in customer["message"]
    assert "server's log names the cause" in customer["message"]
    assert SECRET_ID in owner["message"]
    assert any(SECRET_ID in record.getMessage() for record in caplog.records)


# ---- round 2: a handle's escalation steps, a withdrawn adapter -----------------------------------------------------

SPAM_BIND = {"op": "bind", "id": 2, "function": "spam.message_type", "context": {"channel": "email"},
             "on_low_confidence": {"escalate": ["secret.message_type"]}}


def _refused_like_a_fresh_bind(channel: Any, call: dict[str, Any], bind: dict[str, Any]) -> dict[str, Any]:
    """The call is refused exactly as binding the same function and policy is now, and the handle is gone."""
    refused = channel.request(call)
    assert refused["ok"] is False, refused
    fresh = channel.request({**bind, "id": call["id"] + 100})
    assert fresh["ok"] is False, fresh
    assert assert_error(refused, fresh["error"]["type"]) == assert_error(fresh, fresh["error"]["type"])
    assert_error(channel.request({**call, "id": call["id"] + 200}), "InvalidRequest",
                 f"holds no handle {call['handle']}")
    return refused


@pytest.mark.parametrize("access", ["owner", {"accounts": ["acct2"]}], ids=["made owner-only", "acct1 revoked"])
def test_a_handle_whose_escalation_step_no_longer_admits_the_caller_is_dropped(tmp_path: Path, access: object) -> None:
    with deployment(tmp_path) as (http, app, _, catalogue):
        _set_access(catalogue, SECRET_ID, {"accounts": ["acct1"]})
        app.state.planner.refresh()
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws, KEY)
            handle = channel.result(SPAM_BIND)["handle"]
            call = {"op": "call", "id": 3, "handle": handle, "input": "WIN NOW"}
            channel.result(call)
            _set_access(catalogue, SECRET_ID, access)
            app.state.planner.refresh()
            refused = _refused_like_a_fresh_bind(channel, {**call, "id": 4}, SPAM_BIND)
            assert refused["error"]["type"] == "InvalidPolicy" and "No adapter 'secret'" in refused["error"]["message"]


@pytest.mark.parametrize("key", [KEY, OWNER_KEY], ids=["acct1", "owner"])
def test_a_handle_whose_adapter_left_the_catalogue_while_still_served_is_dropped(tmp_path: Path, key: str) -> None:
    with deployment(tmp_path) as (http, app, _, catalogue):
        _set_access(catalogue, SECRET_ID, {"accounts": ["acct1"]})
        app.state.planner.refresh()
        with http.websocket_connect("/v1/channel") as ws:
            channel = start(ws, key)
            bind = {"op": "bind", "id": 2, "function": "secret.message_type", "context": {"channel": "email"}}
            handle = channel.result(bind)["handle"]
            call = {"op": "call", "id": 3, "handle": handle, "input": "WIN NOW"}
            channel.result(call)
            shutil.rmtree(catalogue / SECRET_ID)  # the engines still serve it
            app.state.planner.refresh()
            # the handle holds the function's full ID, so the refusal names it so
            refused = _refused_like_a_fresh_bind(channel, {**call, "id": 4},
                                                 {**bind, "function": f"{SECRET_ID}.message_type"})
            assert refused["error"]["type"] == "UnknownFunction"


# ---- round 2: JEFF_PRIVATE_ADAPTERS ------------------------------------------------------------------------------------

def test_private_adapters_is_read_as_a_list_of_adapter_ids() -> None:
    base = {"JEFF_ENGINE_SOCKETS": "/tmp/engine-0.sock", "JEFF_MODEL_NAME": "jeff-v1.3"}
    read = ServerSettings.from_env({**base, "JEFF_PRIVATE_ADAPTERS": f" {SECRET_ID}, jeff.core.router ,"})
    assert read.private_adapters == (SECRET_ID, "jeff.core.router")
    assert ServerSettings.from_env(base).private_adapters == ()
    for value, named in ((",, ", "no adapter ID"), ("secret", "not an adapter ID"),
                         (f"{SECRET_ID},{SECRET_ID}", "more than once")):
        with pytest.raises(InvalidRequest, match=named):
            ServerSettings.from_env({**base, "JEFF_PRIVATE_ADAPTERS": value})


def _starting(tmp_path: Path, required: tuple[str, ...]) -> None:
    catalogue = private_catalogue(tmp_path)
    settings = ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                              catalogue=catalogue, api_key=OWNER_KEY, private_adapters=required)
    with TestClient(create_app(settings, engine=FakeEngine(answers, set(ALL_IDS)))):
        pass


PRIVATE_LIST = (SECRET_ID, "jeff.core.router", "jeff.support.router")


def test_listed_private_adapters_that_are_private_start(tmp_path: Path) -> None:
    _starting(tmp_path, PRIVATE_LIST)


def test_a_private_adapter_left_off_the_list_stops_the_server_starting(tmp_path: Path) -> None:
    """The list and the manifests' access lines agree exactly: one with an access line but not listed is refused,
    naming it and its manifest file."""
    unlisted = r"does not list adapters whose manifest has an access field: jeff\.support\.router \(.*/manifest\.yaml\)"
    with pytest.raises(ManifestError, match=unlisted) as raised:
        _starting(tmp_path, (SECRET_ID, "jeff.core.router"))
    assert "lists adapters without" not in str(raised.value) and "not in the catalogue" not in str(raised.value)


@pytest.mark.parametrize(("required", "named"), [
    (("jeff.safety.spam",), r"without an access field, so open to everyone: jeff\.safety\.spam \(.*jeff\.safety\.spam/"
                            r"manifest\.yaml\)"),
    (("jeff.nothing.here", SECRET_ID), r"not in the catalogue or any adapter folder: jeff\.nothing\.here")])
def test_a_listed_private_adapter_that_is_open_or_absent_stops_the_server_starting(
        tmp_path: Path, required: tuple[str, ...], named: str) -> None:
    with pytest.raises(ManifestError, match=named):
        _starting(tmp_path, required)


def test_a_listed_private_adapter_that_lost_its_access_stops_a_rebuild(tmp_path: Path) -> None:
    with deployment(tmp_path, private_adapters=PRIVATE_LIST) as (http, app, _, catalogue):
        path = catalogue / SECRET_ID / "manifest.yaml"
        path.write_text(path.read_text().replace("access: owner\n", ""))
        with pytest.raises(ManifestError, match=rf"open to everyone: {SECRET_ID} \({path}\)"):
            app.state.planner.refresh()
        response = http.get("/v1/functions", headers=bearer(OTHER_ACCOUNTS_KEY))
        assert response.status_code == 200 and SECRET_ID not in response.text  # the old catalogue stays


def test_a_listed_private_adapter_known_only_from_an_adapter_folder_must_carry_access(tmp_path: Path) -> None:
    """As on datigator: the adapter has no catalogue manifest, only its folder's."""
    catalogue = private_catalogue(tmp_path)
    shutil.rmtree(catalogue / SECRET_ID)
    root = tmp_path / "adapters"
    folder = fake_adapter_folder(root / "secret", "jeff.private.folder")
    settings = ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                              catalogue=catalogue, api_key=OWNER_KEY, adapter_dirs=(root,),
                              private_adapters=("jeff.private.folder", *sorted(ROUTER_IDS)))
    with pytest.raises(ManifestError, match=rf"open to everyone: jeff\.private\.folder \({folder}/manifest\.yaml\)"):
        with TestClient(create_app(settings, engine=FakeEngine(answers, set(ALL_IDS)))):
            pass


# ---- round 2: engine errors that list adapters, and socket paths ---------------------------------------------------

def not_loaded(adapter: str, row: dict[str, Any]) -> list[float]:
    """The engine's refusal, in the runtime's own wording (jeff_runtime.wording.loaded, which LocalEngine uses), so
    a change to that wording breaks this test rather than the cut."""
    raise NotInstalled(adapter, f"not loaded in this engine {loaded(ALL_IDS)}")


def test_an_engines_list_of_loaded_adapters_is_cut_for_a_customer(tmp_path: Path) -> None:
    body = {"calls": [{"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "email"}}]}
    with deployment(tmp_path, engine_answers=not_loaded) as (http, _, _, _):
        customer = http.post("/v1/decide", json=body, headers=bearer(OTHER_ACCOUNTS_KEY)).json()["error"]
        owner = http.post("/v1/decide", json=body, headers=bearer(OWNER_KEY)).json()["error"]
    assert customer["type"] == owner["type"] == "NotInstalled"
    assert loaded(PUBLIC_IDS) in customer["message"], customer
    assert customer["details"] == owner["details"]  # the same error, with another message
    assert loaded(ALL_IDS) in owner["message"], owner


def test_engine_socket_paths_are_cut_for_a_customer(tmp_path: Path) -> None:
    with deployment(tmp_path, engines=2) as (http, app, engines, _):
        engines[1].serve(sorted(ALL_IDS - {"jeff.safety.spam"}))
        customer = _get(http, "/v1/functions", bearer(OTHER_ACCOUNTS_KEY))
        owner = _get(http, "/v1/functions", bearer(OWNER_KEY))
        paths = [str(path) for path in app.state.settings.engine_sockets]
    assert not [path for path in paths if path in customer] and "engine 0=" in customer and "engine 1=" in customer
    assert all(path in owner for path in paths)


def test_a_socket_path_that_begins_another_is_never_replaced_inside_it() -> None:
    sockets = [Path("/run/jeff/engine.sock"), Path("/run/jeff/engine.sock2")]
    shown = redaction.as_seen_by(EngineError("down: /run/jeff/engine.sock2 and /run/jeff/engine.sock"), None,
                                 lambda: None, lambda: sockets, "a test")  # type: ignore[arg-type, return-value]
    assert str(shown) == "down: engine 1 and engine 0"
    reversed_order = redaction.as_seen_by(EngineError("down: /run/jeff/engine.sock2"), None, lambda: None,
                                          lambda: sockets[::-1], "a test")  # type: ignore[arg-type, return-value]
    assert str(reversed_order) == "down: engine 0"


def test_a_reworded_error_is_logged_once_per_type_per_minute_with_a_count(
        monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture) -> None:
    clock = [1000.0]
    monkeypatch.setattr(redaction.time, "monotonic", lambda: clock[0])
    catalogue: Any = None  # never read: the errors below name no list of loaded adapters
    sockets = [Path("/tmp/jeff-engine-7.sock")]

    def send(error: Any) -> None:
        redaction.as_seen_by(error, None, lambda: catalogue, lambda: sockets, "a test")

    with caplog.at_level(logging.ERROR, logger="jeff_server"):
        for _ in range(3):
            send(EngineError("engine /tmp/jeff-engine-7.sock is down"))
        send(InvalidRequest("asked /tmp/jeff-engine-7.sock"))
        clock[0] += 61
        send(EngineError("engine /tmp/jeff-engine-7.sock is down"))
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 3, lines
    assert "EngineError" in lines[0] and "more" not in lines[0]
    assert "InvalidRequest" in lines[1]
    assert "(2 more EngineError reworded since the last such line, not logged)" in lines[2]


# ---- review: an unexpected exception on the channel is reworded as over HTTP ---------------------------------------

def test_an_unexpected_exception_on_the_channel_is_cut_for_a_customer(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A non-Jeff exception raised answering a channel call is sent as an EngineError naming it; a caller who is
    not the owner gets it with the engine socket paths and the private adapters of a loaded list cut, as over
    HTTP."""
    with deployment(tmp_path) as (http, app, _, _):
        [socket] = [str(path) for path in app.state.settings.engine_sockets]

        def broken(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError(f"the engine at {socket} broke {loaded(ALL_IDS)}")

        monkeypatch.setattr(channel_module, "answer", broken)
        sent = {}
        for name, key in (("customer", OTHER_ACCOUNTS_KEY), ("owner", OWNER_KEY)):
            with http.websocket_connect("/v1/channel") as ws:
                channel = start(ws, key)
                handle = channel.result({"op": "bind", "id": 2, "function": "spam.message_type",
                                         "context": {"channel": "email"}})["handle"]
                sent[name] = assert_error(channel.request({"op": "call", "id": 3, "handle": handle,
                                                           "input": "WIN NOW"}), "EngineError", "RuntimeError")
    customer, owner = sent["customer"]["message"], sent["owner"]["message"]
    assert socket not in customer and "engine 0" in customer, customer
    assert loaded(PUBLIC_IDS) in customer and SECRET_ID not in customer, customer
    assert socket in owner and loaded(ALL_IDS) in owner, owner


# ---- review: GET /health is worded for a customer as errors are ----------------------------------------------------

def test_health_shows_a_customer_no_socket_path_and_no_private_adapter_in_a_cause(tmp_path: Path) -> None:
    with deployment(tmp_path, engines=2) as (http, app, engines, _):
        paths = [str(path) for path in app.state.settings.engine_sockets]
        broken = {"status": "broken", "backend": "fake", "adapters_version": 1, "adapters": sorted(ALL_IDS),
                  "cause": f"EngineError: the engine at {paths[1]} failed {loaded(ALL_IDS)}"}
        engines[1].health = lambda timeout_s: broken  # type: ignore[method-assign]
        customer = http.get("/health", headers=bearer(OTHER_ACCOUNTS_KEY)).json()
        owner = http.get("/health", headers=bearer(OWNER_KEY)).json()
    assert [engine["socket"] for engine in customer["engines"]] == ["engine 0", "engine 1"]
    assert customer["engines"][1]["cause"] == f"EngineError: the engine at engine 1 failed {loaded(PUBLIC_IDS)}"
    assert not [text for text in [*paths, SECRET_ID, *ROUTER_IDS] if text in str(customer)], customer
    assert customer["checkpoint"] is not None and "/" not in customer["checkpoint"]
    assert [engine["socket"] for engine in owner["engines"]] == paths
    assert owner["engines"][1]["cause"] == broken["cause"]
