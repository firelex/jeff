"""Accounts mode's four settings: all or none, each checked, the shared secret read from its file at start and never
shown; and `jeff-server serve` refusing more than one worker in accounts mode."""

import os
import stat
from pathlib import Path

import pytest

from jeff.errors import InvalidRequest
from jeff_server.cli import main as cli
from jeff_server.settings import ACCOUNTS_SETTINGS, ServerSettings

MINIMAL = {"JEFF_ENGINE_SOCKETS": "/tmp/engine-0.sock", "JEFF_MODEL_NAME": "jeff-v1.3", "JEFF_RATE_LIMIT": "20/8"}


def accounts_env(tmp_path: Path, **changes: str) -> dict[str, str]:
    token = tmp_path / "accounts-token"
    token.write_text("a-test-secret-not-a-real-one\n")
    token.chmod(0o600)
    env = {**MINIMAL, "JEFF_ACCOUNTS_URL": "https://hub.example", "JEFF_ACCOUNTS_TOKEN_FILE": str(token),
           "JEFF_ACCOUNTS_MAX_STALE_SECONDS": "300", "JEFF_USAGE_FILE": str(tmp_path / "usage.jsonl"),
           "JEFF_PRIVATE_ADAPTERS": "jeff.private.secret"}
    env.update(changes)
    return env


def test_accounts_mode_is_off_without_its_settings() -> None:
    assert ServerSettings.from_env(MINIMAL).accounts is None


def test_all_four_settings_turn_it_on_and_the_secret_is_read_from_its_file(tmp_path: Path) -> None:
    accounts = ServerSettings.from_env(accounts_env(tmp_path)).accounts
    assert accounts is not None
    assert accounts.url == "https://hub.example"
    assert accounts.token == "a-test-secret-not-a-real-one"  # one trailing newline removed
    assert accounts.max_stale_s == 300.0
    assert accounts.usage_file == tmp_path / "usage.jsonl"
    assert "a-test-secret" not in repr(accounts)


@pytest.mark.parametrize("left_out", ACCOUNTS_SETTINGS)
def test_any_one_missing_is_refused_naming_it(tmp_path: Path, left_out: str) -> None:
    env = accounts_env(tmp_path)
    del env[left_out]
    with pytest.raises(InvalidRequest, match=left_out):
        ServerSettings.from_env(env)


def test_accounts_mode_without_private_adapters_is_refused_naming_it(tmp_path: Path) -> None:
    """Account keys may call every adapter with no access field, so an unset list would quietly turn the check of
    the manifests' access lines off."""
    env = accounts_env(tmp_path)
    del env["JEFF_PRIVATE_ADAPTERS"]
    with pytest.raises(InvalidRequest, match="Accounts mode needs JEFF_PRIVATE_ADAPTERS"):
        ServerSettings.from_env(env)


@pytest.mark.parametrize(("name", "value", "named"), [
    ("JEFF_ACCOUNTS_URL", "http://hub.example", "https://"),
    ("JEFF_ACCOUNTS_URL", "https://hub.example/", "trailing slash"),
    ("JEFF_ACCOUNTS_MAX_STALE_SECONDS", "60", "at least 120"),
    ("JEFF_ACCOUNTS_MAX_STALE_SECONDS", "soon", "number of seconds"),
    ("JEFF_ACCOUNTS_MAX_STALE_SECONDS", "inf", "number of seconds"),
    ("JEFF_ACCOUNTS_TOKEN_FILE", "/nonexistent/accounts-token", "does not exist"),
    ("JEFF_USAGE_FILE", "/nonexistent/folder/usage.jsonl", "folder"),
])
def test_a_malformed_setting_is_refused_naming_it(tmp_path: Path, name: str, value: str, named: str) -> None:
    with pytest.raises(InvalidRequest, match=named) as raised:
        ServerSettings.from_env(accounts_env(tmp_path, **{name: value}))
    assert name in str(raised.value)


@pytest.mark.parametrize("content", ["", "\n", "two words\n", "secret\n\n"])
def test_an_empty_or_malformed_secret_file_is_refused_without_showing_it(tmp_path: Path, content: str) -> None:
    env = accounts_env(tmp_path)
    Path(env["JEFF_ACCOUNTS_TOKEN_FILE"]).write_text(content)
    with pytest.raises(InvalidRequest, match="JEFF_ACCOUNTS_TOKEN_FILE") as raised:
        ServerSettings.from_env(env)
    assert "two words" not in str(raised.value)


def test_a_secret_file_other_users_can_read_is_refused(tmp_path: Path) -> None:
    env = accounts_env(tmp_path)
    Path(env["JEFF_ACCOUNTS_TOKEN_FILE"]).chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP)
    with pytest.raises(InvalidRequest, match="chmod 600"):
        ServerSettings.from_env(env)


def test_the_bypass_file_is_optional_and_off_by_default(tmp_path: Path) -> None:
    accounts = ServerSettings.from_env(accounts_env(tmp_path)).accounts
    assert accounts is not None
    assert accounts.bypass_file is None and accounts.bypass is None


def test_a_bypass_file_sets_the_secret_read_from_it(tmp_path: Path) -> None:
    bypass_file = tmp_path / "accounts-bypass"
    bypass_file.write_text("a-bypass-secret-not-a-real-one\n")
    bypass_file.chmod(0o600)
    accounts = ServerSettings.from_env(
        accounts_env(tmp_path, JEFF_ACCOUNTS_BYPASS_FILE=str(bypass_file))).accounts
    assert accounts is not None
    assert accounts.bypass_file == bypass_file
    assert accounts.bypass == "a-bypass-secret-not-a-real-one"
    assert "a-bypass-secret" not in repr(accounts)


@pytest.mark.parametrize("content", ["", "\n", "two words\n", "secret\n\n"])
def test_a_bad_bypass_file_is_refused_naming_it(tmp_path: Path, content: str) -> None:
    bypass_file = tmp_path / "accounts-bypass"
    bypass_file.write_text(content)
    bypass_file.chmod(0o600)
    env = accounts_env(tmp_path, JEFF_ACCOUNTS_BYPASS_FILE=str(bypass_file))
    with pytest.raises(InvalidRequest, match="JEFF_ACCOUNTS_BYPASS_FILE") as raised:
        ServerSettings.from_env(env)
    assert "two words" not in str(raised.value)


def test_a_missing_bypass_file_is_refused_naming_it(tmp_path: Path) -> None:
    env = accounts_env(tmp_path, JEFF_ACCOUNTS_BYPASS_FILE="/nonexistent/accounts-bypass")
    with pytest.raises(InvalidRequest, match="JEFF_ACCOUNTS_BYPASS_FILE") as raised:
        ServerSettings.from_env(env)
    assert "does not exist" in str(raised.value)


def test_the_bypass_file_is_refused_without_accounts_mode() -> None:
    env = {**MINIMAL, "JEFF_ACCOUNTS_BYPASS_FILE": "/nonexistent/accounts-bypass"}
    with pytest.raises(InvalidRequest, match="JEFF_ACCOUNTS_BYPASS_FILE") as raised:
        ServerSettings.from_env(env)
    assert "accounts mode" in str(raised.value)


def test_accounts_mode_needs_a_rate_limit(tmp_path: Path) -> None:
    env = accounts_env(tmp_path)
    del env["JEFF_RATE_LIMIT"]
    with pytest.raises(InvalidRequest, match="JEFF_RATE_LIMIT"):
        ServerSettings.from_env(env)


def test_accounts_mode_is_a_credential_for_a_public_server(tmp_path: Path) -> None:
    settings = ServerSettings.from_env(accounts_env(tmp_path, JEFF_PUBLIC="1"))
    assert settings.public and settings.accounts is not None


def test_serve_refuses_more_than_one_worker_in_accounts_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn
    monkeypatch.setattr(uvicorn, "run", lambda *args, **options: pytest.fail("uvicorn must not start"))
    monkeypatch.setattr(os, "environ", accounts_env(tmp_path))
    with pytest.raises(SystemExit, match="--workers 1"):
        cli(["serve", "--host", "127.0.0.1", "--port", "8123", "--workers", "2"])
