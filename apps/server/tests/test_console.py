"""Serving the built console at /ui, and its own JSON endpoints: /ui/me, /ui/events, /ui/access-log and
/ui/examples. The HTML and asset routes need no credentials (the page signs itself in); the four JSON endpoints
carry the same credentials and rate limit as /v1 (jeff_server.auth.V1_DEPENDENCIES)."""

import json
import re
import shutil
from pathlib import Path
from typing import Any

import jsonschema
import pytest
import yaml
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi.testclient import TestClient

from jeff.contract import shipped_dir
from jeff.testing import FakeEngine

from jeff_server import console, users
from jeff_server.app import create_app
from jeff_server.settings import ServerSettings

from server_testing import token

ROOT = Path(__file__).resolve().parents[3]
FIXTURES = ROOT / "packages" / "python" / "tests" / "fixtures"
CATALOGUE = FIXTURES / "catalogue"
FIXTURE_ADAPTERS = {"jeff.safety.spam", "jeff.support.triage"}
LOGIN = "menlo@scissero.com"


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def fake(installed: set[str] = FIXTURE_ADAPTERS) -> FakeEngine:
    return FakeEngine(answers, set(installed))


def settings(**options: Any) -> ServerSettings:
    options.setdefault("catalogue", CATALOGUE)
    return ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3", **options)


def client(engine: FakeEngine | None = None, **options: Any) -> TestClient:
    return TestClient(create_app(settings(**options), engine=engine if engine is not None else fake()))


def bearer(value: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def assert_error(response: Any, status: int, error_type: str, *named: str) -> dict[str, Any]:
    assert response.status_code == status, response.text
    body = response.json()
    error = body["error"]
    assert error["type"] == error_type, error
    for text in named:
        assert text in error["message"], error["message"]
    return dict(error)


@pytest.fixture
def built_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A temporary ui/ folder with a built index.html, one hashed asset, a dotfile, and a secret file outside
    assets/ (ui/secret.txt) plus a symlink inside assets/ pointing at it -- real escape attempts, not just
    shaped names -- as `pnpm --filter jeff-console build` would produce plus an attacker's own additions.
    Monkeypatches the module-level UI_FOLDER so every route reads from it."""
    folder = tmp_path / "ui"
    (folder / "assets").mkdir(parents=True)
    (folder / "index.html").write_text('<html><body><script src="./assets/app.a1b2c3.js"></script></body></html>',
                                       encoding="utf-8")
    (folder / "assets" / "app.a1b2c3.js").write_text("console.log('jeff console')", encoding="utf-8")
    (folder / "assets" / ".hidden").write_text("dotfile", encoding="utf-8")
    (folder / "secret.txt").write_text("the secret install detail", encoding="utf-8")
    (folder / "assets" / "out.js").symlink_to(Path("..") / "secret.txt")
    monkeypatch.setattr(console, "UI_FOLDER", folder)
    return folder


@pytest.fixture
def empty_ui(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An empty ui/ folder -- no index.html -- so "the console is not built" is tested against a folder this test
    controls, not against whatever apps/server/src/jeff_server/ui/ happens to hold in this checkout (the real,
    committed build)."""
    folder = tmp_path / "ui"
    folder.mkdir()
    monkeypatch.setattr(console, "UI_FOLDER", folder)
    return folder


@pytest.fixture
def users_file(tmp_path: Path) -> Path:
    path = tmp_path / "users.json"
    users.write_file(path, "s" * 64, {LOGIN: users.hash_password("right horse")})
    return path


def sign_in(http: TestClient) -> str:
    response = http.post("/ui/login", json={"email": LOGIN, "password": "right horse"})
    assert response.status_code == 200, response.text
    return str(response.json()["token"])


# --- serving the console and its assets -----------------------------------------------------------------------------

def test_the_console_and_its_assets_are_served(built_ui: Path) -> None:
    with client() as http:
        page = http.get("/ui/")
        assert page.status_code == 200 and "./assets/" in page.text
        assert page.headers["cache-control"] == "no-cache"
        asset = page.text.split('src="./assets/')[1].split('"')[0]
        asset_response = http.get(f"/ui/assets/{asset}")
        assert asset_response.status_code == 200
        assert asset_response.headers["cache-control"] == "public, max-age=31536000, immutable"
        assert asset_response.text == "console.log('jeff console')"


CONSOLE_CSP = "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'"


def test_the_console_page_carries_a_content_security_policy_and_nosniff(built_ui: Path) -> None:
    with client() as http:
        for path in ("/ui", "/ui/"):  # /ui redirects to /ui/
            page = http.get(path)
            assert page.status_code == 200
            assert page.headers["content-security-policy"] == CONSOLE_CSP
            assert page.headers["x-content-type-options"] == "nosniff"
        asset = http.get("/ui/assets/app.a1b2c3.js")
        assert asset.status_code == 200
        assert asset.headers["x-content-type-options"] == "nosniff"


def test_loading_the_console_page_is_logged_as_an_anonymous_console_visit(built_ui: Path, tmp_path: Path) -> None:
    log = tmp_path / "access.jsonl"
    with client(access_log=log) as http:
        assert http.get("/ui/", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 200
        assert http.get("/ui/").status_code == 200
        assert http.get("/ui/assets/app.a1b2c3.js").status_code == 200  # an asset is not a visit
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(entry["event"], entry["user"], entry["ip"], entry["path"], entry["status"]) for entry in entries] == [
        ("console", None, "203.0.113.7", "/ui/", 200), ("console", None, "testclient", "/ui/", 200)]


def test_the_committed_build_references_only_assets_it_has_and_serves_them() -> None:
    """The real build under apps/server/src/jeff_server/ui (console.UI_FOLDER, not a temporary folder): every
    ./assets/ file its index.html names exists there, is a flat name the asset route accepts, and is served. Every
    name is relative, so the console also works under a proxy's path prefix (the gateway's /jeff/ui/)."""
    page = (console.UI_FOLDER / "index.html").read_text(encoding="utf-8")
    referenced = re.findall(r'(?:src|href)="\./assets/([^"]+)"', page)
    assert referenced, "the committed index.html names no ./assets/ files"
    assert not re.findall(r'(?:src|href)="/', page), "index.html names an absolute path"
    for asset in (console.UI_FOLDER / "assets").iterdir():
        if asset.suffix in {".js", ".css"}:
            text = asset.read_text(encoding="utf-8")
            assert "/ui/assets/" not in text, f"{asset.name} names an absolute /ui/assets/ path"
    for name in referenced:
        assert console.ASSET_NAME.fullmatch(name), name
        assert (console.UI_FOLDER / "assets" / name).is_file(), name
    with client() as http:
        assert http.get("/ui/").text == page
        for name in referenced:
            assert http.get(f"/ui/assets/{name}").status_code == 200, name


def test_the_console_is_not_built_is_404_with_the_message(empty_ui: Path) -> None:
    """empty_ui, not the real apps/server/src/jeff_server/ui/: that folder holds the committed build, which would
    make a test relying on "whatever this checkout happens to have" pass or fail by accident."""
    with client() as http:
        for path in ("/ui", "/ui/"):  # /ui redirects to /ui/
            page = http.get(path)
            assert_error(page, 404, "InvalidRequest", "pnpm --filter jeff-console build")
            assert page.headers["x-request-id"]


def test_a_missing_asset_is_404(built_ui: Path) -> None:
    with client() as http:
        response = http.get("/ui/assets/missing.js")
    assert_error(response, 404, "InvalidRequest", "No such console asset")


@pytest.mark.parametrize("name", [
    "..%2Fsecret.txt",   # an encoded slash: must not escape the assets folder
    ".hidden",           # a real dotfile that exists in the fixture -- refused by shape, not just "missing"
    "sub%2Fpath.js",      # an encoded nested path
    "sub/path.js",       # a literal nested path (no route even matches it; still 404)
    "missing.js",        # a plain missing file
    "a.js%00",           # an embedded NUL byte, decoded before this route ever sees it
    "x" * 300,           # far longer than ASSET_NAME allows
])
def test_asset_path_escape_attempts_are_404(built_ui: Path, name: str) -> None:
    """Review Focus 4: nothing outside ui/assets/ is ever served, whatever shape the name takes, and the response
    never leaks this process's install path (the giveaway an unguarded Path.resolve()/is_file() call would raise
    on a NUL byte or an over-long name)."""
    with client() as http:
        response = http.get(f"/ui/assets/{name}")
    assert response.status_code == 404
    assert str(built_ui) not in response.text


def test_a_symlink_inside_assets_pointing_outside_it_is_404_and_never_leaks_the_target(built_ui: Path) -> None:
    """The one escape a name-shape check alone cannot catch: "out.js" is a valid flat filename and the file it
    names genuinely exists, but resolving it leads outside assets/ (built_ui's fixture made it a symlink to
    ../secret.txt). Removing the is_relative_to check in jeff_server.console.console_asset would serve it; this
    test is written to fail if that check is ever removed."""
    with client() as http:
        response = http.get("/ui/assets/out.js")
    assert response.status_code == 404
    assert "the secret install detail" not in response.text


# --- GET / -----------------------------------------------------------------------------------------------------------

def test_a_public_server_redirects_the_root_to_the_console() -> None:
    """Review Focus 5: a public server always has a credential and a rate limit configured (ServerSettings
    refuses one without), so both are supplied here."""
    with client(public=True, api_key="the-key", rate_limit=(100.0, 10)) as http:
        response = http.get("/", follow_redirects=False)
    assert response.status_code in (302, 303, 307, 308)
    assert response.headers["location"] == "ui/"  # relative: lands on <prefix>/ui/ behind a proxy's path prefix


def test_ui_without_a_slash_redirects_relatively_to_the_page(built_ui: Path) -> None:
    """The page names its assets relatively (./assets/...), so it must be read at an address ending in /ui/; the
    redirect's Location is relative, so behind the gateway (/jeff/ui) it resolves to /jeff/ui/."""
    with client() as http:
        response = http.get("/ui", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"] == "ui/"


def test_a_private_server_claims_no_behaviour_for_the_root() -> None:
    with client() as http:
        assert http.get("/").status_code == 404


# --- /ui/me, sign-in and the rate limiter -----------------------------------------------------------------------------

def test_ui_me_needs_credentials_like_v1_when_any_are_configured() -> None:
    with client(api_key="the-key") as http:
        assert_error(http.get("/ui/me"), 401, "Unauthorised")
        assert http.get("/ui/me", headers=bearer("the-key")).status_code == 200


def test_ui_me_for_a_console_user_an_admin_and_a_hub_token(
        users_file: Path, keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    with client(users_file=users_file, admins=frozenset({LOGIN}), hub_public_key=public) as http:
        console_session = sign_in(http)
        me = http.get("/ui/me", headers=bearer(console_session)).json()
        assert me == {"user": LOGIN, "kind": "console", "admin": True}

        hub_caller = http.get("/ui/me", headers=bearer(token(private, sub="alice"))).json()
        assert hub_caller == {"user": "alice", "kind": "hub", "admin": False}


def test_ui_me_is_not_admin_without_jeff_admins(users_file: Path) -> None:
    with client(users_file=users_file) as http:
        session = sign_in(http)
        assert http.get("/ui/me", headers=bearer(session)).json()["admin"] is False


def test_a_hub_token_whose_sub_equals_an_admin_login_is_still_not_admin(
        users_file: Path, keys: tuple[Ed25519PrivateKey, Path]) -> None:
    """Admin status is a console-session question only: a hub token's sub happening to equal a login JEFF_ADMINS
    lists must not borrow that login's privilege -- a hub token is a customer's credential, never the owner's."""
    private, public = keys
    with client(users_file=users_file, admins=frozenset({LOGIN}), hub_public_key=public) as http:
        hub_caller = http.get("/ui/me", headers=bearer(token(private, sub=LOGIN))).json()
        assert hub_caller == {"user": LOGIN, "kind": "hub", "admin": False}
        refused = http.get("/ui/access-log", headers=bearer(token(private, sub=LOGIN)))
        assert_error(refused, 403, "NotLicensed", "JEFF_ADMINS")


CONSOLE_ME_RESPONSE_SCHEMA = yaml.safe_load((ROOT / "contract" / "openapi.yaml").read_text())[
    "components"]["schemas"]["ConsoleMeResponse"]


def test_ui_me_responses_validate_against_their_openapi_schema(
        users_file: Path, keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    with client(users_file=users_file, admins=frozenset({LOGIN}), hub_public_key=public, api_key="the-key") as http:
        console_admin = http.get("/ui/me", headers=bearer(sign_in(http))).json()
        hub_caller = http.get("/ui/me", headers=bearer(token(private, sub="alice"))).json()
        api_key_caller = http.get("/ui/me", headers=bearer("the-key")).json()
    with client() as http:  # nothing configured at all: every caller is the open, anonymous one
        open_caller = http.get("/ui/me").json()
    by_kind = {"console": console_admin, "hub": hub_caller, "api-key": api_key_caller, "open": open_caller}
    for kind, body in by_kind.items():
        jsonschema.validate(body, CONSOLE_ME_RESPONSE_SCHEMA)
        assert body["kind"] == kind
    assert open_caller["user"] == "anonymous" and open_caller["admin"] is False


# --- /ui/events --------------------------------------------------------------------------------------------------

def test_events_are_written_to_the_access_log(users_file: Path, tmp_path: Path) -> None:
    log = tmp_path / "access.jsonl"
    with client(users_file=users_file, access_log=log) as http:
        session = sign_in(http)
        response = http.post("/ui/events", json={"view": "playground"}, headers=bearer(session))
    assert response.status_code == 200 and response.json() == {"logged": True}
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    view_entries = [entry for entry in entries if entry["event"] == "view"]
    assert len(view_entries) == 1
    assert view_entries[0]["user"] == LOGIN and view_entries[0]["detail"] == "playground"


def test_events_with_no_access_log_configured_say_so_but_still_answer(users_file: Path) -> None:
    with client(users_file=users_file) as http:
        session = sign_in(http)
        response = http.post("/ui/events", json={"view": "home"}, headers=bearer(session))
    assert response.status_code == 200 and response.json() == {"logged": False}


def test_an_event_with_an_unknown_field_is_422() -> None:
    with client() as http:
        assert_error(http.post("/ui/events", json={"view": "x", "extra": 1}), 422, "InvalidRequest")


@pytest.mark.parametrize("view", ["", "Has Spaces", "UPPER", "under_score", "x" * 101])
def test_an_invalid_view_name_is_422(view: str) -> None:
    with client() as http:
        assert_error(http.post("/ui/events", json={"view": view}), 422, "InvalidRequest")


def test_a_hub_caller_and_a_console_login_sharing_a_subject_do_not_merge_in_the_log(
        users_file: Path, tmp_path: Path, keys: tuple[Ed25519PrivateKey, Path]) -> None:
    private, public = keys
    log = tmp_path / "access.jsonl"
    with client(users_file=users_file, admins=frozenset({LOGIN}), hub_public_key=public, access_log=log) as http:
        session = sign_in(http)
        http.post("/ui/events", json={"view": "home"}, headers=bearer(session))
        http.post("/ui/events", json={"view": "home"}, headers=bearer(token(private, sub=LOGIN)))
        report = http.get("/ui/access-log", headers=bearer(session)).json()
    # A console user is logged by the bare login, a hub caller as "hub:<sub>": the two never share a card.
    people = {person["user"]: person for person in report["people"]}
    assert set(people) == {LOGIN, f"hub:{LOGIN}"}
    assert people[LOGIN]["sign_ins"] == 1 and people[f"hub:{LOGIN}"]["sign_ins"] == 0


def test_a_console_users_sign_in_api_calls_and_views_land_on_one_people_card(
        users_file: Path, tmp_path: Path) -> None:
    log = tmp_path / "access.jsonl"
    with client(users_file=users_file, admins=frozenset({LOGIN}), access_log=log) as http:
        session = sign_in(http)
        assert http.get("/v1/functions", headers=bearer(session)).status_code == 200
        http.post("/ui/events", json={"view": "home"}, headers=bearer(session))
        report = http.get("/ui/access-log", headers=bearer(session)).json()
    assert [person["user"] for person in report["people"]] == [LOGIN]
    events = sorted(entry["event"] for entry in report["entries"])
    assert events == ["api", "signin", "view"]
    assert all(entry["user"] == LOGIN for entry in report["entries"])


def test_api_key_callers_are_logged_by_kind_and_subject(tmp_path: Path) -> None:
    log = tmp_path / "access.jsonl"
    with client(api_key="the-key", access_log=log) as http:
        assert http.get("/v1/functions", headers=bearer("the-key")).status_code == 200
        http.post("/ui/events", json={"view": "home"}, headers=bearer("the-key"))
    entries = [json.loads(line) for line in log.read_text().splitlines()]
    assert [(entry["event"], entry["user"]) for entry in entries] == [("api", "api-key:api-key"), ("view", "api-key:api-key")]


# --- /ui/access-log -----------------------------------------------------------------------------------------------

def test_access_log_is_for_admins_only(users_file: Path, tmp_path: Path) -> None:
    """Review Focus 5."""
    log = tmp_path / "access.jsonl"
    with client(users_file=users_file, access_log=log, admins=frozenset({LOGIN})) as http:
        session = sign_in(http)
        http.post("/ui/events", json={"view": "home"}, headers=bearer(session))
        assert http.get("/ui/access-log", headers=bearer(session)).status_code == 200

    other_login = "someone-else@scissero.com"
    users.write_file(users_file, "s" * 64, {LOGIN: users.hash_password("right horse"),
                                            other_login: users.hash_password("another horse")})
    with client(users_file=users_file, access_log=log, admins=frozenset({LOGIN})) as http:
        response = http.post("/ui/login", json={"email": other_login, "password": "another horse"})
        not_admin = response.json()["token"]
        refused = http.get("/ui/access-log", headers=bearer(not_admin))
        assert_error(refused, 403, "NotLicensed", "JEFF_ADMINS")


def test_access_log_without_credentials_configured_is_422_invalid_request_message(users_file: Path) -> None:
    """An admin login with no JEFF_ACCESS_LOG set: there is nothing to read."""
    with client(users_file=users_file, admins=frozenset({LOGIN})) as http:
        session = sign_in(http)
        assert_error(http.get("/ui/access-log", headers=bearer(session)), 422, "InvalidRequest", "JEFF_ACCESS_LOG")


@pytest.mark.parametrize("limit", [0, -1, console.ACCESS_LOG_MAX + 1])
def test_access_log_limit_outside_bounds_is_422_not_silently_clamped(
        users_file: Path, tmp_path: Path, limit: int) -> None:
    log = tmp_path / "access.jsonl"
    with client(users_file=users_file, access_log=log, admins=frozenset({LOGIN})) as http:
        session = sign_in(http)
        assert_error(http.get(f"/ui/access-log?limit={limit}", headers=bearer(session)), 422, "InvalidRequest")


def test_access_log_limit_within_bounds_returns_at_most_that_many_entries(users_file: Path, tmp_path: Path) -> None:
    log = tmp_path / "access.jsonl"
    with client(users_file=users_file, access_log=log, admins=frozenset({LOGIN})) as http:
        session = sign_in(http)
        for _ in range(3):
            http.post("/ui/events", json={"view": "x"}, headers=bearer(session))
        assert len(http.get("/ui/access-log?limit=1", headers=bearer(session)).json()["entries"]) == 1
        full = http.get(f"/ui/access-log?limit={console.ACCESS_LOG_MAX}", headers=bearer(session)).json()
        assert len(full["entries"]) == full["total"]


def test_a_burst_on_a_ui_json_route_is_429(keys: tuple[Ed25519PrivateKey, Path]) -> None:
    """The four JSON endpoints share jeff_server.auth.V1_DEPENDENCIES' rate limiter with /v1; a public server
    always has one configured (ServerSettings refuses one without)."""
    private, public = keys
    with client(public=True, hub_public_key=public, rate_limit=(1.0, 5)) as http:
        caller = bearer(token(private, sub="alice"))
        assert http.get("/ui/me", headers=caller).status_code == 200
        assert_error(http.get("/ui/me", headers=caller), 429, "RateLimited", "alice")


# --- /ui/examples ------------------------------------------------------------------------------------------------

GOLDEN = json.loads((shipped_dir() / "contract" / "golden" / "step1.json").read_text())["cases"]


def golden_names(served: set[str]) -> set[str]:
    engine = fake(served)
    with client(engine) as http:
        response = http.get("/ui/examples")
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"examples"}
    for example in body["examples"]:
        assert set(example) == {"name", "call"}  # golden cases only: no adapter folder here
    return {example["call"]["function"] for example in body["examples"]}


def test_examples_list_only_functions_the_server_serves_now() -> None:
    assert golden_names({"jeff.safety.spam"}) == {"jeff.safety.spam.message_type"}
    assert golden_names({"jeff.safety.spam", "jeff.support.triage"}) == {
        "jeff.safety.spam.message_type", "jeff.support.triage.route", "jeff.support.triage.urgency",
        "jeff.support.triage.needs_human"}
    assert golden_names(set()) == set()


def test_an_example_s_call_matches_the_golden_case_exactly() -> None:
    engine = fake({"jeff.safety.spam"})
    with client(engine) as http:
        body = http.get("/ui/examples").json()
    [example] = body["examples"]
    [case] = [case for case in GOLDEN if case["call"]["function"] == "jeff.safety.spam.message_type"]
    assert example["call"] == case["call"] and example["name"] == case["name"]


def catalogue_with_examples(tmp_path: Path, adapter: str, examples: list[dict[str, Any]]) -> Path:
    """The fixture catalogue copied to tmp_path, with a playground-examples.json beside jeff.safety.spam's manifest."""
    target = tmp_path / "catalogue"
    shutil.copytree(CATALOGUE, target)
    (target / "jeff.safety.spam" / console.EXAMPLES_FILE).write_text(
        json.dumps({"adapter": adapter, "examples": examples}), encoding="utf-8")
    return target


SPAM_EXAMPLES = [
    {"name": "promotion · Sale today", "call": {"function": "jeff.safety.spam.message_type", "input": "Sale today",
                                                 "context": {}, "labels": None},
     "expected": "promotion", "source": "test row s1"},
    {"name": "spam · Win a prize", "call": {"function": "jeff.safety.spam.is_spam", "input": "Win a prize",
                                             "context": {}, "labels": None},
     "expected": True, "source": "test row s2"},
]


def test_examples_add_an_adapter_folder_s_playground_examples_after_the_golden_cases(tmp_path: Path) -> None:
    catalogue = catalogue_with_examples(tmp_path, "jeff.safety.spam", SPAM_EXAMPLES)
    with client(fake({"jeff.safety.spam"}), catalogue=catalogue) as http:
        body = http.get("/ui/examples").json()
    golden = [{"name": case["name"], "call": case["call"]} for case in GOLDEN
              if case["call"]["function"] == "jeff.safety.spam.message_type"]
    # is_spam is not a function of the fixture manifest, so the server does not serve it: left out
    [first, _] = SPAM_EXAMPLES
    assert body["examples"] == [*golden, {"name": first["name"], "call": first["call"], "expected": "promotion"}]


def test_examples_leave_out_an_unserved_adapter_s_playground_examples(tmp_path: Path) -> None:
    catalogue = catalogue_with_examples(tmp_path, "jeff.safety.spam", SPAM_EXAMPLES)
    with client(fake({"jeff.support.triage"}), catalogue=catalogue) as http:
        body = http.get("/ui/examples").json()
    assert body["examples"] and all("expected" not in example for example in body["examples"])


def test_examples_refuse_a_playground_examples_file_naming_another_adapter(tmp_path: Path) -> None:
    catalogue = catalogue_with_examples(tmp_path, "jeff.support.triage", SPAM_EXAMPLES)
    with client(fake({"jeff.safety.spam"}), catalogue=catalogue) as http, \
            pytest.raises(ValueError, match="examples for jeff.support.triage, not for jeff.safety.spam"):
        http.get("/ui/examples")


def test_examples_needs_credentials_like_v1() -> None:
    with client(api_key="the-key") as http:
        assert_error(http.get("/ui/examples"), 401, "Unauthorised")
        assert http.get("/ui/examples", headers=bearer("the-key")).status_code == 200
