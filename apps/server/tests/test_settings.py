from pathlib import Path

import pytest

from jeff.errors import InvalidRequest
from jeff_server.settings import ServerSettings

MINIMAL = {"JEFF_ENGINE_SOCKETS": "/tmp/engine-0.sock", "JEFF_MODEL_NAME": "jeff-v1.3"}


def test_required_fields_and_their_defaults() -> None:
    settings = ServerSettings.from_env(MINIMAL)
    assert settings.engine_sockets == (Path("/tmp/engine-0.sock"),)
    assert settings.model_name == "jeff-v1.3"
    assert settings.engine_timeout_s == 30.0
    assert settings.catalogue is None
    assert settings.adapter_dirs == ()
    assert settings.graphs is None
    assert settings.public is False
    assert settings.api_key is None
    assert settings.hub_public_key is None
    assert settings.users_file is None
    assert settings.rate_limit is None
    assert settings.access_log is None
    assert settings.admins == frozenset()
    assert settings.geoip_db is None


def test_several_engine_sockets_colon_separated() -> None:
    settings = ServerSettings.from_env({**MINIMAL, "JEFF_ENGINE_SOCKETS": "/tmp/a.sock:/tmp/b.sock"})
    assert settings.engine_sockets == (Path("/tmp/a.sock"), Path("/tmp/b.sock"))


@pytest.mark.parametrize("missing", ["JEFF_ENGINE_SOCKETS", "JEFF_MODEL_NAME"])
def test_missing_required_field_is_invalid_request(missing: str) -> None:
    env = {key: value for key, value in MINIMAL.items() if key != missing}
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env(env)
    assert missing in str(error.value)


@pytest.mark.parametrize("missing", ["JEFF_ENGINE_SOCKETS", "JEFF_MODEL_NAME"])
def test_missing_required_field_is_reported_before_public_mode_requirements(missing: str) -> None:
    # A server missing a basic required field AND lacking public-mode credentials/rate-limit should report the
    # more fundamental problem (the missing field), not the public-mode one.
    env = {key: value for key, value in MINIMAL.items() if key != missing}
    env = {**env, "JEFF_PUBLIC": "1"}  # public, with no credential and no rate limit either
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env(env)
    assert missing in str(error.value)
    assert "JEFF_PUBLIC" not in str(error.value) and "credential" not in str(error.value)


def test_empty_engine_socket_entry_is_invalid_request() -> None:
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env({**MINIMAL, "JEFF_ENGINE_SOCKETS": "/tmp/a.sock::/tmp/b.sock"})
    assert "JEFF_ENGINE_SOCKETS" in str(error.value)


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number"])
def test_malformed_engine_timeout_is_invalid_request(value: str) -> None:
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env({**MINIMAL, "JEFF_ENGINE_TIMEOUT_S": value})
    assert "JEFF_ENGINE_TIMEOUT_S" in str(error.value)


def test_custom_engine_timeout() -> None:
    settings = ServerSettings.from_env({**MINIMAL, "JEFF_ENGINE_TIMEOUT_S": "5.5"})
    assert settings.engine_timeout_s == 5.5


@pytest.mark.parametrize("value", ["0", "true", "yes", "2"])
def test_malformed_public_is_invalid_request(value: str) -> None:
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env({**MINIMAL, "JEFF_PUBLIC": value})
    assert "JEFF_PUBLIC" in str(error.value)


@pytest.mark.parametrize("value", ["rps-only", "1/", "/4", "x/4", "1/x", "0/4", "1/0", "-1/4", "nan/5", "inf/5", "-inf/5", "NaN/5"])
def test_malformed_rate_limit_is_invalid_request(value: str) -> None:
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env({**MINIMAL, "JEFF_RATE_LIMIT": value})
    assert "JEFF_RATE_LIMIT" in str(error.value)


def test_valid_rate_limit() -> None:
    settings = ServerSettings.from_env({**MINIMAL, "JEFF_RATE_LIMIT": "2.5/10"})
    assert settings.rate_limit == (2.5, 10)


def test_admins_split_on_comma_and_trimmed() -> None:
    settings = ServerSettings.from_env({**MINIMAL, "JEFF_ADMINS": "alice, bob ,,carol"})
    assert settings.admins == frozenset({"alice", "bob", "carol"})


def test_admins_are_lower_cased() -> None:
    """jeff_server.console._is_admin compares a console session's login to this set; normalising it once here,
    rather than at every call site, keeps that comparison a plain membership test."""
    settings = ServerSettings.from_env({**MINIMAL, "JEFF_ADMINS": "Alice@Example.com, BOB@example.com"})
    assert settings.admins == frozenset({"alice@example.com", "bob@example.com"})


def test_admins_of_only_separators_is_invalid_request() -> None:
    with pytest.raises(InvalidRequest, match="JEFF_ADMINS"):
        ServerSettings.from_env({**MINIMAL, "JEFF_ADMINS": ",, ,"})


def test_public_without_any_credential_is_invalid_request() -> None:
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env({**MINIMAL, "JEFF_PUBLIC": "1", "JEFF_RATE_LIMIT": "5/10"})
    assert "credential" in str(error.value)


def test_public_without_rate_limit_is_invalid_request() -> None:
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env({**MINIMAL, "JEFF_PUBLIC": "1", "JEFF_API_KEY": "secret"})
    assert "JEFF_RATE_LIMIT" in str(error.value)


@pytest.mark.parametrize("credential_env", ["JEFF_API_KEY", "JEFF_HUB_PUBLIC_KEY", "JEFF_UI_USERS"])
def test_public_with_a_credential_and_a_rate_limit_is_accepted(credential_env: str) -> None:
    value = "secret" if credential_env == "JEFF_API_KEY" else "/tmp/some-path"
    settings = ServerSettings.from_env({**MINIMAL, "JEFF_PUBLIC": "1", "JEFF_RATE_LIMIT": "5/10",
                                        credential_env: value})
    assert settings.public is True


@pytest.mark.parametrize("name", ["jeff v1", "jeff", "jeff-latest", "jeff-qwen3.8-27b"])
def test_the_model_name_has_no_spaces_and_is_not_one_of_the_bases_other_names(name: str) -> None:
    """As jeff-server's JEFF_MODEL_NAME: /v1/systemone answers to it and to jeff, jeff-latest and
    jeff-qwen3.8-27b, so it can be none of those."""
    with pytest.raises(InvalidRequest, match="JEFF_MODEL_NAME"):
        ServerSettings.from_env({**MINIMAL, "JEFF_MODEL_NAME": name})


def test_adapter_dirs_are_colon_separated_folders(tmp_path: Path) -> None:
    first, second = tmp_path / "a", tmp_path / "b"
    first.mkdir()
    second.mkdir()
    settings = ServerSettings.from_env({**MINIMAL, "JEFF_ADAPTER_DIRS": f"{first}:{second}"})
    assert settings.adapter_dirs == (first, second)


def test_an_adapter_dir_that_does_not_exist_is_invalid_request(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="JEFF_ADAPTER_DIRS names .*nowhere, which is not a folder"):
        ServerSettings.from_env({**MINIMAL, "JEFF_ADAPTER_DIRS": f"{tmp_path}:{tmp_path / 'nowhere'}"})


def test_an_empty_adapter_dir_entry_is_invalid_request(tmp_path: Path) -> None:
    with pytest.raises(InvalidRequest, match="JEFF_ADAPTER_DIRS=.* has an empty path"):
        ServerSettings.from_env({**MINIMAL, "JEFF_ADAPTER_DIRS": f"{tmp_path}::{tmp_path}"})


EVERY_VARIABLE = ["JEFF_ENGINE_SOCKETS", "JEFF_MODEL_NAME", "JEFF_ENGINE_TIMEOUT_S", "JEFF_CATALOGUE", "JEFF_GRAPHS",
                  "JEFF_ADAPTER_DIRS",
                  "JEFF_PUBLIC", "JEFF_RATE_LIMIT", "JEFF_API_KEY", "JEFF_HUB_PUBLIC_KEY", "JEFF_UI_USERS",
                  "JEFF_ACCESS_LOG", "JEFF_ADMINS", "JEFF_GEOIP_DB", "JEFF_PRIVATE_ADAPTERS",
                  "JEFF_FACTORY", "JEFF_FACTORY_DIR", "JEFF_TEACHER_URL", "JEFF_TEACHER_KEY", "JEFF_TEACHER_MODEL",
                  "JEFF_TRAINER", "JEFF_FACTORY_MIN_FREE_GB",
                  "JEFF_ACCOUNTS_URL", "JEFF_ACCOUNTS_TOKEN_FILE", "JEFF_ACCOUNTS_MAX_STALE_SECONDS",
                  "JEFF_USAGE_FILE", "JEFF_ACCOUNTS_BYPASS_FILE"]


@pytest.mark.parametrize("name", EVERY_VARIABLE)
def test_a_variable_set_to_the_empty_string_is_invalid_request_not_unset(name: str) -> None:
    """An unset $KEY in a deploy script gives JEFF_API_KEY="", which must not quietly mean "no key" (an open
    server): every variable present but empty is refused, naming it. Covers each kind: required values, numbers,
    flags, the rate limit, secrets, optional paths and lists."""
    with pytest.raises(InvalidRequest) as error:
        ServerSettings.from_env({**MINIMAL, name: ""})
    assert f"{name} is set but empty" in str(error.value)
    assert "set a value or unset it" in str(error.value)


def test_every_variable_the_settings_read_is_covered_by_the_empty_value_test() -> None:
    """from_env must read no JEFF_* variable that the empty-value test above does not try."""
    read: list[str] = []

    class Recording(dict[str, str]):
        def get(self, key: str, default: object = None) -> object:  # type: ignore[override]
            read.append(key)
            return super().get(key, default)

        def __contains__(self, key: object) -> bool:
            read.append(str(key))
            return super().__contains__(key)

        def __getitem__(self, key: str) -> str:
            read.append(key)
            return super().__getitem__(key)

    ServerSettings.from_env(Recording(MINIMAL))
    assert set(read) == set(EVERY_VARIABLE)
