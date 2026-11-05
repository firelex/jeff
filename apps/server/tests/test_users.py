"""Console sign-in (jeff_server.users), ported from jeff-server's ui_users and its tests: scrypt password hashes in the
JEFF_UI_USERS file, signed 30-day session tokens the API accepts like the API key, sign-out-all rotates the secret,
repeated failed sign-ins lock that login and then everyone."""

import json
import os
import stat
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from jeff.errors import ContractError
from jeff.testing import FakeEngine

from jeff_server import auth, users
from jeff_server.app import create_app
from jeff_server.cli import main as cli
from jeff_server.settings import ServerSettings

LOGIN = "menlo@scissero.com"


@pytest.fixture
def users_file(tmp_path: Path) -> Path:
    path = tmp_path / "users.json"
    users.write_file(path, "s" * 64, {LOGIN: users.hash_password("right horse")})
    return path


def app(users_file: Path | None, api_key: str | None = None) -> TestClient:
    settings = ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                              users_file=users_file, api_key=api_key)
    return TestClient(create_app(settings, engine=FakeEngine(lambda adapter, row: [1.0], set())))


def sign_in(client: TestClient) -> str:
    response = client.post("/ui/login", json={"email": " Menlo@Scissero.com", "password": "right horse"})
    assert response.status_code == 200, response.text
    assert response.json()["user"] == LOGIN
    return str(response.json()["token"])


def bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_the_right_password_signs_in_and_the_token_opens_the_api(users_file: Path) -> None:
    with app(users_file) as client:
        token = sign_in(client)
        assert client.get("/v1/graphs", headers=bearer(token)).status_code == 200


@pytest.mark.parametrize("email,password", [(LOGIN, "wrong"), ("dcvc@scissero.com", "right horse")])
def test_a_wrong_login_or_password_is_refused(users_file: Path, email: str, password: str) -> None:
    with app(users_file) as client:
        response = client.post("/ui/login", json={"email": email, "password": password})
    assert response.status_code == 401
    assert response.json()["error"]["type"] == "Unauthorised"
    assert response.json()["error"]["message"] == "Wrong email or password."


def test_the_api_needs_a_token_once_sign_in_is_configured(users_file: Path) -> None:
    with app(users_file) as client:
        token = sign_in(client)
        assert client.get("/v1/graphs").status_code == 401
        assert client.get("/v1/graphs", headers=bearer(f"{token[:-1]}x")).status_code == 401


def test_the_api_key_still_works_beside_sign_in(users_file: Path) -> None:
    with app(users_file, api_key="the-key") as client:
        assert client.get("/v1/graphs", headers=bearer("the-key")).status_code == 200
        assert client.get("/v1/graphs", headers=bearer(sign_in(client))).status_code == 200


def test_removing_a_login_or_new_secret_ends_its_sessions(users_file: Path) -> None:
    with app(users_file) as client:
        token = sign_in(client)
        _, people = users.read_file(users_file)
        users.write_file(users_file, "t" * 64, people)
        os.utime(users_file, (time.time() + 5, time.time() + 5))  # the server rereads the file when its time changes
        assert client.get("/v1/graphs", headers=bearer(token)).status_code == 401
        token = sign_in(client)
        users.write_file(users_file, "t" * 64, {})
        os.utime(users_file, (time.time() + 10, time.time() + 10))
        assert client.get("/v1/graphs", headers=bearer(token)).status_code == 401


def test_sign_out_all_rotates_the_secret_and_ends_every_session(users_file: Path) -> None:
    with app(users_file) as client:
        token = sign_in(client)
        secret, _ = users.read_file(users_file)
        cli(["users", str(users_file), "sign-out-all"])
        os.utime(users_file, (time.time() + 5, time.time() + 5))
        assert users.read_file(users_file)[0] != secret
        assert client.get("/v1/graphs", headers=bearer(token)).status_code == 401
        assert client.get("/v1/graphs", headers=bearer(sign_in(client))).status_code == 200


def test_an_expired_session_is_refused(users_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(users, "SESSION_SECONDS", -1)
    with app(users_file) as client:
        token = sign_in(client)
        assert client.get("/v1/graphs", headers=bearer(token)).status_code == 401


def test_a_console_caller_carries_when_its_session_ends(users_file: Path) -> None:
    """The channel closes a connection whose credential has expired, so a console caller says when its session
    ends; the API key never expires."""
    store = users.UserStore(users_file)
    before = time.time()
    token = store.sign_in(LOGIN, "right horse")
    assert token is not None
    caller = auth.Authenticator("the-key", store, None, None).caller(f"Bearer {token}", None)
    assert caller.kind == "console" and caller.subject == LOGIN
    assert caller.expires is not None and before + users.SESSION_SECONDS - 1 <= caller.expires
    assert caller.expires <= time.time() + users.SESSION_SECONDS
    assert auth.Authenticator("the-key", store, None, None).caller("Bearer the-key", None).expires is None


def test_sign_in_without_a_users_file_says_how_to_configure_it() -> None:
    with app(None, api_key="the-key") as client:
        response = client.post("/ui/login", json={"email": "a@b.c", "password": "x"})
    assert response.status_code == 401
    assert "JEFF_UI_USERS" in response.json()["error"]["message"]


def test_repeated_failed_sign_ins_lock_that_login_then_everyone(users_file: Path) -> None:
    with app(users_file) as client:
        for _ in range(users.LOGIN_FAILURES):
            assert client.post("/ui/login", json={"email": LOGIN, "password": "guess"}).status_code == 401
        locked = client.post("/ui/login", json={"email": LOGIN, "password": "right horse"})
        assert locked.status_code == 429 and int(locked.headers["retry-after"]) > 0
        assert locked.json()["error"]["type"] == "RateLimited"
        guard: users.SignInGuard = client.app.state.sign_in_guard  # type: ignore[attr-defined]
        guard.failed.clear()
        guard.failed.update({f"x{i}@a.b": [time.time()] for i in range(users.TOTAL_FAILURES)})
        assert client.post("/ui/login", json={"email": "dcvc@scissero.com", "password": "x"}).status_code == 429


def test_parallel_wrong_passwords_cannot_exceed_the_limit(users_file: Path) -> None:
    """Each attempt is counted under the guard's lock before its password is checked, so 30 guesses at once get at
    most LOGIN_FAILURES password checks; the rest are locked out."""
    statuses: list[int] = []
    lock = threading.Lock()
    start = threading.Barrier(30)
    with app(users_file) as client:
        def guess() -> None:
            start.wait()
            status = client.post("/ui/login", json={"email": LOGIN, "password": "guess"}).status_code
            with lock:
                statuses.append(status)

        threads = [threading.Thread(target=guess) for _ in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
    assert statuses.count(401) == users.LOGIN_FAILURES
    assert statuses.count(429) == 30 - users.LOGIN_FAILURES


def test_sign_in_without_a_users_file_does_not_ask_for_a_bearer_token() -> None:
    with app(None, api_key="the-key") as client:
        response = client.post("/ui/login", json={"email": "a@b.c", "password": "x"})
    assert response.status_code == 401 and "WWW-Authenticate" not in response.headers


def test_a_success_clears_that_logins_failures(users_file: Path) -> None:
    with app(users_file) as client:
        for _ in range(users.LOGIN_FAILURES - 1):
            client.post("/ui/login", json={"email": LOGIN, "password": "guess"})
        sign_in(client)
        assert LOGIN not in client.app.state.sign_in_guard.failed  # type: ignore[attr-defined]


def test_failures_older_than_the_lockout_window_are_forgotten() -> None:
    guard = users.SignInGuard()
    long_ago = time.time() - users.LOCKOUT_SECONDS - 1
    guard.failed[LOGIN] = [long_ago] * users.LOGIN_FAILURES
    now = time.time()
    guard.attempt(LOGIN, now)  # does not raise; counts this attempt only
    assert guard.failed[LOGIN] == [now]


# --- the users file ------------------------------------------------------------------------------------------------

def test_password_hashes_are_scrypt_and_salted() -> None:
    first, second = users.hash_password("pw"), users.hash_password("pw")
    assert first.startswith(f"scrypt${users.N}${users.R}${users.P}$") and first != second
    assert users.verify_password("pw", first) and not users.verify_password("other", first)


def test_an_unknown_hash_scheme_is_a_contract_error() -> None:
    with pytest.raises(ContractError, match="scrypt"):
        users.verify_password("pw", "bcrypt$1$2$3$00$00")


def test_the_users_file_is_written_owner_only(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "users.json"
    users.write_file(path, "s" * 64, {})
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


@pytest.mark.parametrize("content", [{"secret": "short", "users": {}}, {"users": {}}, {"secret": "s" * 64},
                                     {"secret": "s" * 64, "users": {"a": 1}}, ["not", "a", "mapping"]])
def test_a_malformed_users_file_is_a_contract_error_naming_it(tmp_path: Path, content: object) -> None:
    path = tmp_path / "users.json"
    path.write_text(json.dumps(content))
    with pytest.raises(ContractError, match=str(path)):
        users.read_file(path)


def test_a_missing_or_malformed_users_file_stops_the_server_starting(tmp_path: Path) -> None:
    with pytest.raises(ContractError, match="JEFF_UI_USERS"):
        app(tmp_path / "missing.json")
    (tmp_path / "bad.json").write_text("{")
    with pytest.raises(ContractError, match="bad.json"):
        app(tmp_path / "bad.json")


def test_the_users_command_adds_lists_and_removes_logins(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "users.json"
    cli(["users", str(path), "add", " Menlo@Scissero.com"])
    login, password = capsys.readouterr().out.strip().split("\t")
    assert login == LOGIN
    store = users.UserStore(path)
    token = store.sign_in(LOGIN, password)
    assert token is not None and store.session_user(token) == LOGIN
    cli(["users", str(path), "list"])
    assert capsys.readouterr().out.strip() == LOGIN
    cli(["users", str(path), "remove", LOGIN])
    assert users.read_file(path)[1] == {}
    with pytest.raises(SystemExit, match="is not in"):
        cli(["users", str(path), "remove", LOGIN])
