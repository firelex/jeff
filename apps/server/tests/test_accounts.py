"""Accounts mode: account keys checked against the hub's key list (fetched, never called per request), refused when
unknown or when the list is too old, the last good list kept through a broken fetch, every adapter licence but none of
the owner's endpoints, one rate-limit bucket per account, and the playground's key with its own rate and
X-Jeff-Account."""

import json
import logging
import re
from pathlib import Path

import httpx
import pytest
from fastapi.routing import APIRoute, APIWebSocketRoute
from fastapi.testclient import TestClient

from jeff_server.accounts import HTTP_TIMEOUT_S, AccountsError, parse_key_list
from jeff_server.mcp_mount import McpRoute

from accounts_testing import (CATALOGUE, HUB_URL, INTERNAL_API, KEY, OTHER_ACCOUNTS_KEY, OWNER_KEY, PLAYGROUND_KEY,
                              SECOND_KEY, SECRET, SPAM_CALL, Hub, accounts_app, bearer, digest, key_list, serving)
from channel_testing import Channel, assert_error, start
from server_testing import proprietary


def decide(http, headers):  # type: ignore[no-untyped-def]
    return http.post("/v1/decide", json={"calls": [SPAM_CALL]}, headers=headers)


def test_an_account_key_is_accepted_as_its_account(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http:
        assert decide(http, bearer(KEY)).status_code == 200
        me = http.get("/ui/me", headers=bearer(KEY)).json()
        assert (me["kind"], me["user"], me["admin"]) == ("account", "acct1", False)


def test_the_hub_is_asked_with_the_shared_secret(tmp_path: Path) -> None:
    hub = Hub()
    with serving(accounts_app(tmp_path, hub)):
        assert hub.authorizations and set(hub.authorizations) == {f"Bearer {SECRET}"}


def test_the_bypass_header_is_sent_on_both_internal_calls_when_the_setting_is_set(tmp_path: Path) -> None:
    hub = Hub()
    with serving(accounts_app(tmp_path, hub, bypass="a-bypass-secret")):
        assert hub.bypass_headers and set(hub.bypass_headers) == {"a-bypass-secret"}


def test_the_bypass_header_is_absent_when_the_setting_is_unset(tmp_path: Path) -> None:
    hub = Hub()
    with serving(accounts_app(tmp_path, hub)):
        assert hub.bypass_headers and set(hub.bypass_headers) == {None}


def test_the_hub_is_asked_only_at_its_internal_api(tmp_path: Path) -> None:
    # Not through serving()/TestClient: that starts the app's own minute loop (jeff_server.accounts.run_every_minute,
    # Task 7), which would call the hub concurrently with this test's own calls. app.state.accounts is built and
    # usable as soon as accounts_app returns, with no app started.
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    app.state.accounts.refresh()
    app.state.accounts.tick()
    assert hub.urls == [f"{INTERNAL_API}/keys", f"{INTERNAL_API}/keys", f"{INTERNAL_API}/usage"]

    # Through serving()/TestClient too, with the minute loop actually running (its own startup and shutdown
    # ticks included): every URL it asks for is still one of the two internal-api paths, never anything else.
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    hub2 = Hub()
    with serving(accounts_app(second_dir, hub2)):
        pass
    assert hub2.urls and all(url in (f"{INTERNAL_API}/keys", f"{INTERNAL_API}/usage") for url in hub2.urls)


def test_the_servers_own_hub_client_checks_tls_and_follows_no_redirect(tmp_path: Path) -> None:
    """Without accounts_http the app builds its client: certificates verified, redirects not followed (a redirect
    would carry the shared secret's request elsewhere), and a timeout."""
    from jeff_server.app import _hub_client
    client = _hub_client()
    assert client.follow_redirects is False
    assert client.timeout == httpx.Timeout(HTTP_TIMEOUT_S)
    import ssl
    context = client._transport._pool._ssl_context  # type: ignore[attr-defined]
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname


def test_an_unknown_account_key_is_refused_saying_where_to_make_one(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http:
        response = decide(http, bearer("jeff_live_" + "z" * 52))
        assert response.status_code == 401
        assert response.json()["error"]["message"] == \
            "This API key is not valid; create one at https://hub.test/account"


def test_before_the_first_fetch_account_keys_are_refused_but_the_owner_key_works(tmp_path: Path) -> None:
    hub = Hub()
    hub.status = 503
    with TestClient(accounts_app(tmp_path, hub)) as http:
        response = decide(http, bearer(KEY))
        assert response.status_code == 401
        assert "has not fetched the key list" in response.json()["error"]["message"]
        assert decide(http, bearer(OWNER_KEY)).status_code == 200


def test_a_stale_key_list_refuses_account_keys_but_not_the_owner_key(tmp_path: Path) -> None:
    app = accounts_app(tmp_path, Hub())
    with serving(app) as http:
        fetched = app.state.accounts.clock()
        app.state.accounts.clock = lambda: fetched + 301
        response = decide(http, bearer(KEY))
        assert response.status_code == 401
        assert "the key list is 301 seconds old" in response.json()["error"]["message"]
        assert decide(http, bearer(OWNER_KEY)).status_code == 200


def test_a_list_exactly_at_the_staleness_limit_still_works(tmp_path: Path) -> None:
    app = accounts_app(tmp_path, Hub())
    with serving(app) as http:
        app.state.accounts.clock = lambda: 1000.0
        app.state.accounts.refresh()
        app.state.accounts.clock = lambda: 1300.0
        assert decide(http, bearer(KEY)).status_code == 200
        app.state.accounts.clock = lambda: 1300.5
        assert decide(http, bearer(KEY)).status_code == 401


@pytest.mark.parametrize("broken", ["status", "not json", "schema", "redirect"])
def test_a_broken_fetch_keeps_the_last_good_list_and_logs_why(tmp_path: Path, broken: str,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        if broken == "status":
            hub.status = 500
        elif broken == "not json":
            hub.keys = b"<html>oops</html>"
        elif broken == "schema":
            hub.keys = {"accounts": [], "keys": [{"hash": "nothex", "account": "acct1"}], "playground": {}}
        else:
            hub.status, hub.location = 302, "https://elsewhere.test/api/internal/v1/keys"
        with caplog.at_level(logging.ERROR, logger="jeff_server"):
            app.state.accounts.tick()
        assert any("internal/v1/keys" in record.getMessage() or "key list" in record.getMessage()
                   for record in caplog.records)
        assert decide(http, bearer(KEY)).status_code == 200
        assert not any("elsewhere.test" in url for url in hub.urls)


def test_a_broken_fetch_does_not_make_the_last_good_list_younger(tmp_path: Path) -> None:
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        fetched = app.state.accounts.clock()
        hub.status = 500
        app.state.accounts.clock = lambda: fetched + 301
        app.state.accounts.tick()
        assert "the key list is 301 seconds old" in decide(http, bearer(KEY)).json()["error"]["message"]


def test_no_key_hash_or_secret_reaches_a_log_line_or_a_reply(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The hub's broken answers, a refusal and a contradicting list: nothing logged or answered carries a key, a key's
    hash or the shared secret, even when the hub wrongly sends a raw key where a hash belongs."""
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    secrets = [SECRET, KEY, PLAYGROUND_KEY, digest(KEY), digest(PLAYGROUND_KEY), digest(KEY)[:12]]
    texts: list[str] = []
    with caplog.at_level(logging.DEBUG), serving(app) as http:
        for body in ({**key_list(), "keys": [{"hash": KEY, "account": "acct1"}]},
                     {**key_list(), "keys": [*key_list()["keys"], {"hash": digest(KEY), "account": "acct1"}]},
                     {**key_list(), "keys": [{"hash": digest(PLAYGROUND_KEY), "account": "acct1"}]},
                     {**key_list(), "keys": [{"hash": digest(KEY), "account": "ghost"}]}):
            hub.keys = body
            app.state.accounts.tick()
        hub.status, hub.keys = 500, {"echo": SECRET, "hash": digest(KEY)}
        app.state.accounts.tick()
        texts.append(decide(http, bearer(KEY + "x")).text)
        texts.append(http.get("/ui/me", headers=bearer(KEY, **{"X-Jeff-Account": "acct2"})).text)
    texts.extend(record.getMessage() for record in caplog.records)
    assert len(caplog.records) >= 5
    assert not [text for text in texts if any(secret in text for secret in secrets)]


def test_a_key_dropped_from_the_list_stops_working_at_the_next_fetch(tmp_path: Path) -> None:
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        assert decide(http, bearer(KEY)).status_code == 200
        hub.keys = {**key_list(), "keys": [entry for entry in key_list()["keys"] if entry["hash"] != digest(KEY)]}
        app.state.accounts.refresh()
        assert decide(http, bearer(KEY)).status_code == 401
        assert decide(http, bearer(SECOND_KEY)).status_code == 200


def test_a_key_dropped_from_the_list_closes_its_open_channel(tmp_path: Path) -> None:
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    with serving(app) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, KEY)
        channel.result({"op": "decide", "id": 2, "calls": [SPAM_CALL]})
        hub.keys = {**key_list(), "keys": [entry for entry in key_list()["keys"] if entry["hash"] != digest(KEY)]}
        app.state.accounts.refresh()
        channel.send({"op": "decide", "id": 3, "calls": [SPAM_CALL]})
        final = channel.receive()
        assert final["id"] == 0
        assert_error(final, "Unauthorised", "This API key is not valid")
        channel.closed()


def test_a_stale_key_list_closes_an_account_keys_open_channel(tmp_path: Path) -> None:
    app = accounts_app(tmp_path, Hub())
    with serving(app) as http, http.websocket_connect("/v1/channel") as ws:
        channel = start(ws, KEY)
        fetched = app.state.accounts.clock()
        app.state.accounts.clock = lambda: fetched + 301
        channel.send({"op": "decide", "id": 2, "calls": [SPAM_CALL]})
        final = channel.receive()
        assert final["id"] == 0
        assert_error(final, "Unauthorised", "the key list is 301 seconds old")
        channel.closed()


def test_an_account_holds_every_adapter_licence(tmp_path: Path) -> None:
    catalogue = proprietary(CATALOGUE, tmp_path, "jeff.safety.spam")
    with serving(accounts_app(tmp_path, Hub(), catalogue=catalogue)) as http:
        assert decide(http, bearer(KEY)).status_code == 200


@pytest.mark.parametrize(("method", "path", "body"), [
    ("PUT", "/v1/adapters/served", {"adapters": ["jeff.safety.spam"]}),
    ("POST", "/v1/adapters/reload", {}),
    ("GET", "/metrics", None),
    ("GET", "/v1/factory/mode", None),
    ("GET", "/ui/access-log", None),
])
def test_an_account_cannot_reach_the_owners_endpoints(tmp_path: Path, method: str, path: str,
                                                      body: dict[str, object] | None) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http:
        response = http.request(method, path, json=body, headers=bearer(KEY))
        assert response.status_code == 403, response.text
        assert response.json()["error"]["type"] == "NotLicensed"


# Every route of the server, by who may use it. A route added to the server fails
# test_every_route_is_sorted_by_who_may_use_it until it is placed here, so whether a customer may reach it is decided.
OWNER_ONLY = {("PUT", "/v1/adapters/served"), ("POST", "/v1/adapters/reload"), ("GET", "/metrics")}
ADMIN_ONLY = {("GET", "/ui/access-log")}  # a console session under a JEFF_ADMINS login, not even the API key
FACTORY_PREFIX = "/v1/factory/"  # every factory route is the owner's
CUSTOMER = {("POST", "/v1/decide"), ("GET", "/v1/functions"), ("GET", "/v1/graphs"), ("POST", "/v1/systemone"),
            ("GET", "/v1/models"), ("GET", "/v1/adapters"), ("GET", "/ui/me"), ("POST", "/ui/events"),
            ("GET", "/ui/examples"), ("GET", "/openapi.json"), ("WEBSOCKET", "/v1/channel"), ("GET", "/mcp"),
            ("POST", "/mcp"), ("DELETE", "/mcp")}
NO_CREDENTIALS = {("POST", "/ui/login"), ("GET", "/health"), ("GET", "/ui"), ("GET", "/ui/"),
                  ("GET", "/ui/assets/{name}"), ("GET", "/")}


def every_route(app) -> set[tuple[str, str]]:  # type: ignore[no-untyped-def]
    """(method, path) of every route, through included routers (FastAPI 0.141 keeps an included router as one route
    holding the original router; every router here is included without a prefix of its own)."""
    found: set[tuple[str, str]] = set()
    for route in getattr(app, "routes", app):
        if hasattr(route, "original_router"):
            found |= every_route(route.original_router.routes)
        elif isinstance(route, APIWebSocketRoute):
            found.add(("WEBSOCKET", route.path))
        elif isinstance(route, (APIRoute, McpRoute)):
            found.update((method, route.path) for method in route.methods if method != "HEAD")
        else:
            raise AssertionError(f"A route of a kind this test does not know: {route!r}")
    return found


def test_every_route_is_sorted_by_who_may_use_it(tmp_path: Path) -> None:
    routes = every_route(accounts_app(tmp_path, Hub()))
    factory = {route for route in routes if route[1].startswith(FACTORY_PREFIX)}
    assert len(factory) > 30
    assert routes - factory == OWNER_ONLY | ADMIN_ONLY | CUSTOMER | NO_CREDENTIALS


def test_no_owner_route_answers_an_account_or_the_playground(tmp_path: Path) -> None:
    """Every owner-only route (adapters, metrics, the access log, each factory route), with an account's key, the
    playground's key, and the playground's key standing in for an account: 403 NotLicensed from the owner check."""
    app = accounts_app(tmp_path, Hub(), rate_limit=(1000.0, 8))
    routes = every_route(app)
    owners = sorted(route for route in routes if route in OWNER_ONLY | ADMIN_ONLY or route[1].startswith(FACTORY_PREFIX))
    callers = {"account": bearer(KEY), "playground": bearer(PLAYGROUND_KEY),
               "playground as acct2": bearer(PLAYGROUND_KEY, **{"X-Jeff-Account": "acct2"})}
    wrong = []
    with serving(app) as http:
        for method, template in owners:
            path = re.sub(r"\{[^}]+\}", "x", template)
            expected = ("Only the server's administrators" if (method, template) in ADMIN_ONLY
                        else "needs the server owner's credentials")
            for name, headers in callers.items():
                response = http.request(method, path, json={} if method in ("POST", "PUT") else None,
                                        headers=headers)
                error = response.json().get("error", {}) if response.status_code != 200 else {}
                if response.status_code != 403 or error.get("type") != "NotLicensed" \
                        or expected not in error.get("message", ""):
                    wrong.append(f"{method} {path} as {name}: {response.status_code} {response.text[:200]}")
    assert not wrong, "\n".join(wrong)


def test_the_owner_refusal_says_whose_credential_it_was(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http:
        message = http.get("/metrics", headers=bearer(KEY)).json()["error"]["message"]
        assert message == ("GET /metrics needs the server owner's credentials (the API key or a console session); "
                           "the account credential for acct1 is a customer's and cannot change or inspect the server")


@pytest.mark.parametrize("path", ["/v1/functions", "/v1/graphs", "/v1/models", "/v1/adapters", "/ui/me",
                                  "/ui/examples", "/openapi.json"])
def test_an_account_reaches_the_customers_routes(tmp_path: Path, path: str) -> None:
    """Past the credentials and every owner check; /v1/models and /v1/adapters then need engine processes, which a
    FakeEngine is not (500 EngineError, as for the owner), so for them only the refusals are ruled out."""
    with serving(accounts_app(tmp_path, Hub())) as http:
        response = http.get(path, headers=bearer(KEY))
        if path in ("/v1/models", "/v1/adapters"):
            assert response.status_code == 500 and response.json()["error"]["type"] == "EngineError"
            assert "FakeEngine" in response.json()["error"]["message"]
        else:
            assert response.status_code == 200, response.text


def test_an_accounts_keys_share_one_rate_limit_bucket(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub(), rate_limit=(1.0, 8))) as http:
        assert decide(http, bearer(KEY)).status_code == 200
        assert decide(http, bearer(SECOND_KEY)).status_code == 429  # the same account
        assert decide(http, bearer(OTHER_ACCOUNTS_KEY)).status_code == 200  # another account


def test_the_playground_key_runs_at_the_lists_rate_not_the_servers(tmp_path: Path) -> None:
    hub = Hub()
    hub.keys = key_list(runs_per_second=1.0)
    with serving(accounts_app(tmp_path, hub, rate_limit=(100.0, 8))) as http:
        assert decide(http, bearer(PLAYGROUND_KEY)).status_code == 200
        response = decide(http, bearer(PLAYGROUND_KEY))
        assert response.status_code == 429 and response.headers["Retry-After"] == "1"
        assert http.get("/ui/me", headers=bearer(OWNER_KEY)).status_code == 200


def test_the_playground_key_with_x_jeff_account_runs_as_that_account(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http:
        me = http.get("/ui/me", headers=bearer(PLAYGROUND_KEY, **{"X-Jeff-Account": "acct2"})).json()
        assert (me["kind"], me["user"]) == ("account", "acct2")
        assert http.get("/ui/me", headers=bearer(PLAYGROUND_KEY)).json()["kind"] == "playground"


def test_x_jeff_account_is_refused_for_an_unknown_account_or_from_any_other_key(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http:
        unknown = http.get("/ui/me", headers=bearer(PLAYGROUND_KEY, **{"X-Jeff-Account": "nobody"}))
        assert unknown.status_code == 401 and "nobody" in unknown.json()["error"]["message"]
        for key in (KEY, OWNER_KEY):
            response = http.get("/ui/me", headers=bearer(key, **{"X-Jeff-Account": "acct2"}))
            assert response.status_code == 401
            assert "only with the hub playground's key" in response.json()["error"]["message"]


def test_x_jeff_account_is_refused_at_mcp_from_an_account_key(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http:
        response = http.post("/mcp", json={}, headers=bearer(KEY, **{"X-Jeff-Account": "acct2"}))
        assert response.status_code == 401
        assert "only with the hub playground's key" in response.json()["error"]["message"]


def test_an_account_key_on_a_server_not_in_accounts_mode_is_refused_saying_so() -> None:
    from jeff_server.auth import Authenticator
    from jeff.errors import Unauthorised
    with pytest.raises(Unauthorised, match="accounts mode is off"):
        Authenticator("the-key", None, None, None).caller(f"Bearer {KEY}", None)
    with pytest.raises(Unauthorised, match="only with the hub playground's key"):
        Authenticator(None, None, None, None).caller(None, "acct1")


def test_the_account_url_in_the_refusal_is_the_hubs(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http:
        assert f"{HUB_URL}/account" in decide(http, bearer("jeff_live_nope")).json()["error"]["message"]


@pytest.mark.parametrize(("change", "named"), [
    (lambda body: body["keys"].append({"hash": body["keys"][0]["hash"], "account": "acct1"}), "twice"),
    (lambda body: body["keys"].append({"hash": body["playground"]["hash"], "account": "acct1"}), "playground"),
    (lambda body: body["keys"].append({"hash": "e" * 64, "account": "ghost"}), "ghost"),
    (lambda body: body["accounts"].append({**body["accounts"][0]}), "twice"),
    (lambda body: body["accounts"][0].update(period_end="2026-11-01T00:00:00Z"), "before it starts"),
    (lambda body: body["accounts"][0].update(account="playground"), "playground"),
    (lambda body: body["playground"].update(period_end="2026-10-01T00:00:00Z"), "before it starts"),
])
def test_a_key_list_that_contradicts_itself_is_refused(change, named: str) -> None:  # type: ignore[no-untyped-def]
    body = key_list()
    change(body)
    with pytest.raises(AccountsError, match=named):
        parse_key_list(body)


def test_a_key_list_that_breaks_the_schema_is_refused_naming_where() -> None:
    body = key_list()
    body["keys"][1]["hash"] = "nothex"
    with pytest.raises(AccountsError, match=r"accounts_keys.*\$\.keys\[1\]\.hash.*pattern"):
        parse_key_list(body)


# --- fix round 1: broken answers never escape tick, the schema's patterns matched whole, the reprs, the channel's
# handshake, the answer's size and deadline, and the list's age from when it was asked for

def fixed_clock(app, value: float) -> None:  # type: ignore[no-untyped-def]
    app.state.accounts.clock = lambda: value


@pytest.mark.parametrize("rps", [b"Infinity", b"-Infinity", b"NaN", b"0", b"-1"])
def test_a_playground_rate_that_is_not_a_finite_positive_number_is_refused_and_changes_nothing(
        tmp_path: Path, rps: bytes, caplog: pytest.LogCaptureFixture) -> None:
    from jeff_server.accounts import PLAYGROUND_RATE_KEY
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        fixed_clock(app, 1000.0)
        app.state.accounts.refresh()
        good = json.dumps(key_list(runs_per_second=1.0)).encode()
        hub.keys = good.replace(b'"runs_per_second": 1.0', b'"runs_per_second": ' + rps)
        assert hub.keys != good
        fixed_clock(app, 1200.0)
        with caplog.at_level(logging.ERROR, logger="jeff_server"):
            app.state.accounts.tick()  # returns: never raises
        assert any("key list" in record.getMessage() for record in caplog.records)
        assert app.state.limiter.rates[PLAYGROUND_RATE_KEY] == 50.0  # the last good list's
        fixed_clock(app, 1300.0)
        assert decide(http, bearer(KEY)).status_code == 200
        fixed_clock(app, 1300.5)  # still the list fetched at 1000: the refused one was never put in force
        assert decide(http, bearer(KEY)).status_code == 401


def test_a_body_nested_too_deep_to_parse_is_refused_and_logged(tmp_path: Path,
                                                               caplog: pytest.LogCaptureFixture) -> None:
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        hub.keys = b"[" * 100_000 + b"]" * 100_000
        with caplog.at_level(logging.ERROR, logger="jeff_server"):
            app.state.accounts.tick()
        assert any("cannot be read as JSON: RecursionError" in record.getMessage() for record in caplog.records)
        assert decide(http, bearer(KEY)).status_code == 200


def test_an_unexpected_failure_in_tick_is_logged_without_its_message_and_does_not_escape(
        tmp_path: Path, caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch) -> None:
    app = accounts_app(tmp_path, Hub())
    with serving(app):
        def broken() -> None:
            raise RuntimeError(f"quoting {digest(KEY)}")
        monkeypatch.setattr(app.state.accounts.book, "report", broken)
        with caplog.at_level(logging.ERROR, logger="jeff_server"):
            app.state.accounts.tick()
        lines = [record.getMessage() for record in caplog.records]
        assert any("report failed with RuntimeError" in line for line in lines)
        assert not any(digest(KEY) in line for line in lines)
        # This test is about tick(), not the last report at shutdown: undo the monkeypatch before serving() exits,
        # so shutdown's own report (jeff_server.app._last_report) sees the real book.report, not the broken stand-in.
        monkeypatch.undo()


@pytest.mark.parametrize(("change", "named"), [
    (lambda body: body["accounts"][0].update(account="acct1\n"), r"accounts\[0\]"),
    (lambda body: body["keys"][0].update(hash=body["keys"][0]["hash"] + "\n"), r"keys\[0\]"),
    (lambda body: body["keys"][0].update(account="acct1\n"), r"keys\[0\]"),
    (lambda body: body["playground"].update(hash=body["playground"]["hash"] + "\n"), "playground"),
    (lambda body: body["accounts"][0].update(period_end="2026-13-99T99:99:99Z"), "not a real time"),
    (lambda body: body["playground"].update(period_start="2026-02-30T00:00:00Z"), "not a real time"),
])
def test_values_outside_the_schemas_patterns_or_impossible_times_are_refused(change, named: str) -> None:  # type: ignore[no-untyped-def]
    body = key_list()
    change(body)
    with pytest.raises(AccountsError, match=named) as refused:
        parse_key_list(body)
    assert "\n" not in str(refused.value) and digest(KEY) not in str(refused.value)


def test_the_key_list_and_a_meter_show_no_hash_in_their_repr(tmp_path: Path) -> None:
    keys = parse_key_list(key_list())
    app = accounts_app(tmp_path, Hub())
    with serving(app):
        meter = app.state.accounts.caller(KEY, None).meter
    for text in (repr(keys), repr(meter)):
        assert digest(KEY) not in text and digest(PLAYGROUND_KEY) not in text and digest(SECOND_KEY) not in text


def test_the_channel_refuses_x_jeff_account_by_name(tmp_path: Path) -> None:
    with serving(accounts_app(tmp_path, Hub())) as http, \
            http.websocket_connect("/v1/channel", headers={"X-Jeff-Account": "acct2"}) as ws:
        channel = Channel(ws)
        channel.hello()
        final = channel.receive()
        assert final["id"] == 0
        assert_error(final, "Unauthorised", "X-Jeff-Account is not accepted on /v1/channel")
        channel.closed()


def test_an_answer_over_the_size_limit_is_refused_by_name(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    from jeff_server.accounts import MAX_ANSWER_BYTES
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    with serving(app) as http:
        hub.keys = b" " * (MAX_ANSWER_BYTES + 1)
        with caplog.at_level(logging.ERROR, logger="jeff_server"):
            app.state.accounts.tick()
        assert any("answered more than 16,777,216 bytes" in record.getMessage() for record in caplog.records)
        assert decide(http, bearer(KEY)).status_code == 200


def test_an_answer_slower_than_the_deadline_is_refused(tmp_path: Path) -> None:
    from jeff_server.accounts import FETCH_DEADLINE_S
    app = accounts_app(tmp_path, Hub())
    with serving(app):
        good = json.dumps(key_list()).encode()
        app.state.accounts.http = httpx.Client(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, content=iter([good[:10], good[10:]]))))
        ticks = iter(range(0, 1000, 20))
        app.state.accounts.clock = lambda: float(next(ticks))
        assert FETCH_DEADLINE_S == 30.0  # sent at 0, deadline 20 + 30; the first chunk at 40, the second at 60
        with pytest.raises(AccountsError, match="took more than 30 seconds"):
            app.state.accounts.refresh()


def test_the_lists_age_counts_from_when_it_was_asked_for(tmp_path: Path) -> None:
    app = accounts_app(tmp_path, Hub())
    with serving(app) as http:
        times = iter([1000.0])
        app.state.accounts.clock = lambda: next(times, 1100.0)  # sent at 1000; everything after, 1100
        app.state.accounts.refresh()
        fixed_clock(app, 1300.5)  # 300.5 seconds after it was asked for, 200.5 after it arrived
        assert "the key list is 300 seconds old" in decide(http, bearer(KEY)).json()["error"]["message"]
