"""The server's settings, built once from the process environment. Every field is explicit: a setting with no
documented default below is required, and a malformed value raises InvalidRequest naming the environment variable
and the form it must take. A variable that is present but empty is refused too, never read as unset: an unset $KEY in
a deploy script gives JEFF_API_KEY="", which must not quietly mean "no key". Nothing here is inferred or silently
substituted.

These settings configure one API worker process. `jeff-server serve --workers N` starts N of them from the same
environment, and each keeps its own rate limits, sign-in lockouts and request counters: JEFF_RATE_LIMIT applies per
worker, so a caller can get up to N times the rate (and N times the concurrent requests) with N workers. Set it
accordingly."""

import math
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from jeff.errors import InvalidRequest

from jeff_server.factory import config as factory_config
from jeff_server.factory.config import FactoryConfig

# /v1/systemone answers to these names for the base, besides JEFF_MODEL_NAME (as jeff-server did), so JEFF_MODEL_NAME
# can be none of them. jeff-qwen3.8-27b is accepted for clients written against v1.0 and v1.1 but not listed by
# /v1/models: the name wrongly suggested a 27B model.
LEGACY_NAME = "jeff-qwen3.8-27b"
BASE_NAMES = ("jeff", "jeff-latest", LEGACY_NAME)

# Accounts mode (jeff_server.accounts): API keys made on the hub, checked against the key list the server fetches
# from JEFF_ACCOUNTS_URL once a minute, with usage counted into JEFF_USAGE_FILE. All four are set, or none.
ACCOUNTS_SETTINGS = ("JEFF_ACCOUNTS_URL", "JEFF_ACCOUNTS_TOKEN_FILE", "JEFF_ACCOUNTS_MAX_STALE_SECONDS",
                     "JEFF_USAGE_FILE")
# JEFF_ACCOUNTS_BYPASS_FILE (optional, needs accounts mode): the file holds Vercel's deployment-protection bypass
# secret for the hub's preview project. When set, the server sends it as the x-vercel-protection-bypass header on
# both internal calls, and only to JEFF_ACCOUNTS_URL. Unset: no header is sent.
# The key list is fetched every 60 seconds (jeff_server.accounts.TICK_S); a staleness limit below two fetches would
# refuse every account key whenever one fetch is a little late.
MIN_STALE_SECONDS = 120.0


@dataclass(frozen=True)
class AccountsSettings:
    url: str              # the hub's base address, https://, no trailing slash
    token_file: Path      # JEFF_ACCOUNTS_TOKEN_FILE, owner-only
    token: str = field(repr=False)  # the shared secret read from token_file at start; never logged or shown
    max_stale_s: float    # JEFF_ACCOUNTS_MAX_STALE_SECONDS
    usage_file: Path      # JEFF_USAGE_FILE
    bypass_file: Path | None = None  # JEFF_ACCOUNTS_BYPASS_FILE, owner-only; None: no bypass header is sent
    bypass: str | None = field(default=None, repr=False)  # the secret read from bypass_file; never logged or shown


@dataclass(frozen=True)
class ServerSettings:
    # Required: no default.
    engine_sockets: tuple[Path, ...]
    model_name: str
    # Optional, with the defaults below.
    engine_timeout_s: float = 30.0
    catalogue: Path | None = None
    # JEFF_ADAPTER_DIRS: folders (separated by ':') whose adapter folders join the catalogue, as the engines'
    # --adapters-dir (jeff_server.catalogue_refresh). Each must exist. Unset: none.
    adapter_dirs: tuple[Path, ...] = ()
    graphs: Path | None = None
    public: bool = False
    api_key: str | None = None
    hub_public_key: Path | None = None
    users_file: Path | None = None
    # JEFF_RATE_LIMIT="<requests per second>/<concurrent requests>", per caller (jeff_server.auth.RateLimiter): a token
    # bucket refilled at the rate, holding a burst of max(rate, 1) requests (at least one, so a rate below one per
    # second still admits a request), and at most <concurrent requests> in flight. Unset: no limit (private use).
    # The limit is kept per worker process: with `serve --workers N` a caller can get up to N times this.
    rate_limit: tuple[float, int] | None = None
    # JEFF_ACCESS_LOG: the access log file (jeff_server.access_log); unset, nothing is logged. It trusts the last
    # X-Forwarded-For entry as the client's address, so run behind a proxy that sets that header.
    access_log: Path | None = None
    # JEFF_ADMINS: logins (lower-cased, comma-separated) that may read the console's access log
    # (jeff_server.console) and are reported as admins by GET /ui/me. Only a console session's own login is ever
    # checked against this set -- an "admin" here means nothing for a hub token or the API key.
    admins: frozenset[str] = frozenset()
    geoip_db: Path | None = None
    # JEFF_FACTORY and the other factory variables (jeff_server.factory.config). Unset: the factory is off. A public
    # server refuses every one of them except JEFF_FACTORY=off.
    factory: FactoryConfig = field(default_factory=FactoryConfig.off)
    # Accounts mode (ACCOUNTS_SETTINGS above); None: off.
    accounts: AccountsSettings | None = None
    # JEFF_PRIVATE_ADAPTERS: adapter IDs (comma-separated) that must be private on this server: at startup and at
    # every catalogue rebuild they must be exactly the catalogue's adapters (with the adapter folders') that have an
    # access field, or the catalogue is refused (jeff_server.access.check_private_adapters). Required in accounts
    # mode; elsewhere unset (()) checks nothing.
    private_adapters: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "ServerSettings":
        # The two settings with no default at all are checked first, so a server missing both a required field
        # and a public-mode requirement reports the more basic problem.
        engine_sockets = _engine_sockets(env)
        model_name = model_name_from_env(env)
        engine_timeout_s = _engine_timeout_s(env)
        catalogue = catalogue_from_env(env)
        adapter_dirs = adapter_dirs_from_env(env)
        graphs = _optional_path(env, "JEFF_GRAPHS")

        public = _public(env)
        rate_limit = _rate_limit(env)
        api_key = _value(env, "JEFF_API_KEY")
        hub_public_key = _optional_path(env, "JEFF_HUB_PUBLIC_KEY")
        users_file = _optional_path(env, "JEFF_UI_USERS")
        accounts = _accounts(env, rate_limit)
        private_adapters = _private_adapters(env)
        if accounts is not None and not private_adapters:
            raise InvalidRequest(
                "Accounts mode needs JEFF_PRIVATE_ADAPTERS: account keys may call every adapter that has no access "
                "field, so the server must be told which adapters are private (the IDs of every adapter whose "
                "manifest has an access field, comma-separated) and refuses a catalogue that does not agree")

        if public and not (api_key or hub_public_key or users_file or accounts):
            raise InvalidRequest(
                "JEFF_PUBLIC=1 needs a credential: set JEFF_API_KEY, JEFF_HUB_PUBLIC_KEY, JEFF_UI_USERS or the "
                "accounts settings; a public server must not be open")
        if public and rate_limit is None:
            raise InvalidRequest(
                "JEFF_PUBLIC=1 needs JEFF_RATE_LIMIT=\"<requests per second>/<concurrent requests>\"; "
                "a public server must not be unlimited")
        factory = _factory(env, public)
        if factory.mode == "owner" and not adapter_dirs:
            raise InvalidRequest("JEFF_FACTORY=owner needs JEFF_ADAPTER_DIRS: the factory worker publishes into its "
                                 "first folder, the API's catalogue reads it, and both take the adapter-change lock "
                                 "there")

        return cls(
            engine_sockets=engine_sockets,
            model_name=model_name,
            engine_timeout_s=engine_timeout_s,
            catalogue=catalogue,
            adapter_dirs=adapter_dirs,
            graphs=graphs,
            public=public,
            api_key=api_key,
            hub_public_key=hub_public_key,
            users_file=users_file,
            rate_limit=rate_limit,
            access_log=_optional_path(env, "JEFF_ACCESS_LOG"),
            admins=_admins(env),
            geoip_db=_optional_path(env, "JEFF_GEOIP_DB"),
            factory=factory,
            accounts=accounts,
            private_adapters=private_adapters,
        )


def _value(env: Mapping[str, str], name: str) -> str | None:
    """The variable's value, or None when it is unset. Present but empty is an error, never "unset"."""
    value = env.get(name)
    if value == "":
        raise InvalidRequest(f"{name} is set but empty; set a value or unset it")
    return value


def _engine_sockets(env: Mapping[str, str]) -> tuple[Path, ...]:
    value = _value(env, "JEFF_ENGINE_SOCKETS")
    if value is None:
        raise InvalidRequest(
            "JEFF_ENGINE_SOCKETS is required: one or more engine socket paths separated by ':'")
    parts = value.split(":")
    if any(not part for part in parts):
        raise InvalidRequest(f"JEFF_ENGINE_SOCKETS={value!r} has an empty path; separate paths with a single ':'")
    return tuple(Path(part) for part in parts)


def model_name_from_env(env: Mapping[str, str]) -> str:
    """JEFF_MODEL_NAME, checked; also read by `jeff-server factory worker`, which checks a published adapter's names
    against it as the API does at startup."""
    value = _value(env, "JEFF_MODEL_NAME")
    if value is None:
        raise InvalidRequest("JEFF_MODEL_NAME is required: the base's public name, for example jeff-v1.3")
    if any(character.isspace() for character in value) or value in BASE_NAMES:
        raise InvalidRequest(f"JEFF_MODEL_NAME={value!r}: use a name without spaces that is not one of "
                             f"{', '.join(BASE_NAMES)}")
    return value


def catalogue_from_env(env: Mapping[str, str]) -> Path | None:
    """JEFF_CATALOGUE (None: the catalogue shipped with jeff); also read by `jeff-server factory worker`."""
    return _optional_path(env, "JEFF_CATALOGUE")


def adapter_dirs_from_env(env: Mapping[str, str]) -> tuple[Path, ...]:
    """JEFF_ADAPTER_DIRS, checked; also read by `jeff-server factory worker`, which publishes into the first folder."""
    value = _value(env, "JEFF_ADAPTER_DIRS")
    if value is None:
        return ()
    parts = value.split(":")
    if any(not part for part in parts):
        raise InvalidRequest(f"JEFF_ADAPTER_DIRS={value!r} has an empty path; separate folders with a single ':'")
    for part in parts:
        if not Path(part).is_dir():
            raise InvalidRequest(f"JEFF_ADAPTER_DIRS names {part}, which is not a folder; create it or leave it out")
    return tuple(Path(part) for part in parts)


def _factory(env: Mapping[str, str], public: bool) -> FactoryConfig:
    """As jeff-server: a public server runs without the factory, which spends teacher-model money and trains on the
    GPU, so it belongs on a private server."""
    given = factory_config.given(env)
    if public and given:
        raise InvalidRequest(f"JEFF_PUBLIC=1 runs without the adapter factory; unset {', '.join(sorted(given))}")
    return FactoryConfig.from_env(env)


def _engine_timeout_s(env: Mapping[str, str]) -> float:
    value = _value(env, "JEFF_ENGINE_TIMEOUT_S")
    if value is None:
        return 30.0
    try:
        timeout = float(value)
    except ValueError as error:
        raise InvalidRequest(f"JEFF_ENGINE_TIMEOUT_S={value!r}; use a number of seconds above 0") from error
    if timeout <= 0:
        raise InvalidRequest(f"JEFF_ENGINE_TIMEOUT_S={value!r}; use a number of seconds above 0")
    return timeout


def _optional_path(env: Mapping[str, str], name: str) -> Path | None:
    value = _value(env, name)
    return Path(value) if value is not None else None


def _public(env: Mapping[str, str]) -> bool:
    value = _value(env, "JEFF_PUBLIC")
    if value is None:
        return False
    if value != "1":
        raise InvalidRequest(
            f"JEFF_PUBLIC={value!r}; use 1 for a server reachable from the internet, or leave it unset")
    return True


def _rate_limit(env: Mapping[str, str]) -> tuple[float, int] | None:
    value = _value(env, "JEFF_RATE_LIMIT")
    if value is None:
        return None
    rps_text, sep, concurrent_text = value.partition("/")
    if not sep:
        raise InvalidRequest(f"JEFF_RATE_LIMIT={value!r}; use '<requests per second>/<concurrent requests>'")
    try:
        rps = float(rps_text)
        concurrent = int(concurrent_text)
    except ValueError as error:
        raise InvalidRequest(
            f"JEFF_RATE_LIMIT={value!r}; use '<requests per second>/<concurrent requests>'") from error
    if not math.isfinite(rps):
        raise InvalidRequest(f"JEFF_RATE_LIMIT={value!r}; the rate must be a finite number of requests per second")
    if rps <= 0 or concurrent <= 0:
        raise InvalidRequest(f"JEFF_RATE_LIMIT={value!r}; both the rate and the concurrency limit must be above 0")
    return rps, concurrent


# An adapter ID (contract/schemas/manifest.schema.json, id).
ADAPTER_ID = re.compile(r"jeff(\.[a-z0-9][a-z0-9-]*){2}")


def _private_adapters(env: Mapping[str, str]) -> tuple[str, ...]:
    """JEFF_PRIVATE_ADAPTERS: adapter IDs, comma-separated, each once. A value with no ID once separators are
    removed, an entry that is not an adapter ID (an alias is not one) or an ID listed twice is refused."""
    value = _value(env, "JEFF_PRIVATE_ADAPTERS")
    if value is None:
        return ()
    ids = [part.strip() for part in value.split(",") if part.strip()]
    if not ids:
        raise InvalidRequest(f"JEFF_PRIVATE_ADAPTERS={value!r} has no adapter ID once separators are removed; list "
                             "at least one, or unset it")
    wrong = [item for item in ids if not ADAPTER_ID.fullmatch(item)]
    if wrong:
        raise InvalidRequest(f"JEFF_PRIVATE_ADAPTERS names {', '.join(wrong)}, which is not an adapter ID "
                             "(jeff.<domain>.<name>; an alias is not accepted)")
    twice = sorted({item for item in ids if ids.count(item) > 1})
    if twice:
        raise InvalidRequest(f"JEFF_PRIVATE_ADAPTERS names {', '.join(twice)} more than once")
    return tuple(ids)


def _admins(env: Mapping[str, str]) -> frozenset[str]:
    """Logins, lower-cased and stripped once here (not at every place that checks membership, such as
    jeff_server.console._is_admin) so that check is a plain `in`. A value that is set but reduces to no logins at
    all once separators are removed (",,," or similar) is refused, the same way any other present-but-meaningless
    setting is, rather than quietly behaving as if JEFF_ADMINS were never set."""
    value = _value(env, "JEFF_ADMINS")
    if value is None:
        return frozenset()
    admins = frozenset(part.strip().lower() for part in value.split(",") if part.strip())
    if not admins:
        raise InvalidRequest(f"JEFF_ADMINS={value!r} has no logins once separators are removed; list at least "
                             "one login, or unset it")
    return admins


def _accounts(env: Mapping[str, str], rate_limit: tuple[float, int] | None) -> AccountsSettings | None:
    given = [name for name in ACCOUNTS_SETTINGS if _value(env, name) is not None]
    bypass_file_value = _value(env, "JEFF_ACCOUNTS_BYPASS_FILE")
    if not given:
        if bypass_file_value is not None:
            raise InvalidRequest(f"JEFF_ACCOUNTS_BYPASS_FILE needs accounts mode: set {', '.join(ACCOUNTS_SETTINGS)} "
                                 "too, or unset JEFF_ACCOUNTS_BYPASS_FILE")
        return None
    missing = [name for name in ACCOUNTS_SETTINGS if name not in given]
    if missing:
        raise InvalidRequest(f"Accounts mode needs all of {', '.join(ACCOUNTS_SETTINGS)}; set "
                             f"{', '.join(missing)} too, or unset {', '.join(given)}")
    url = env["JEFF_ACCOUNTS_URL"]
    if not url.startswith("https://"):
        raise InvalidRequest(f"JEFF_ACCOUNTS_URL={url!r}; the hub's internal API is served only over https://, "
                             "for example https://jeffhub.ai")
    if url.endswith("/"):
        raise InvalidRequest(f"JEFF_ACCOUNTS_URL={url!r}; give it without a trailing slash")
    if rate_limit is None:
        raise InvalidRequest("Accounts mode needs JEFF_RATE_LIMIT: each account's keys share one bucket of it, and "
                             "the playground runs at most its concurrent requests at once")
    bypass_file = Path(bypass_file_value) if bypass_file_value is not None else None
    bypass = _accounts_secret_file(bypass_file, "JEFF_ACCOUNTS_BYPASS_FILE",
                                   "Vercel's deployment-protection bypass secret for the hub's preview project") \
        if bypass_file is not None else None
    return AccountsSettings(url=url, token_file=Path(env["JEFF_ACCOUNTS_TOKEN_FILE"]),
                            token=_accounts_token(Path(env["JEFF_ACCOUNTS_TOKEN_FILE"])),
                            max_stale_s=_max_stale(env["JEFF_ACCOUNTS_MAX_STALE_SECONDS"]),
                            usage_file=_usage_file(Path(env["JEFF_USAGE_FILE"])),
                            bypass_file=bypass_file, bypass=bypass)


def _accounts_secret_file(path: Path, name: str, contents: str) -> str:
    """The shared secret held alone in `path`: its text without one trailing newline. Messages never include it."""
    if not path.is_file():
        raise InvalidRequest(f"{name}={path} does not exist; write {contents} there")
    if path.stat().st_mode & 0o077:
        raise InvalidRequest(f"{name}={path} can be read by other users; chmod 600 it")
    text = path.read_text(encoding="utf-8")
    secret = text[:-1] if text.endswith("\n") else text
    if not secret or any(character.isspace() for character in secret):
        raise InvalidRequest(f"{name}={path} must hold the secret alone on one line (it is empty, "
                             "or has spaces or more lines)")
    return secret


def _accounts_token(path: Path) -> str:
    """The shared secret: the file's text without one trailing newline. Messages never include it."""
    return _accounts_secret_file(path, "JEFF_ACCOUNTS_TOKEN_FILE",
                                 "the hub's internal API secret (docs/release/accounts-go-live.md)")


def _max_stale(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError as error:
        raise InvalidRequest(f"JEFF_ACCOUNTS_MAX_STALE_SECONDS={value!r}; use a number of seconds") from error
    if not math.isfinite(seconds):
        raise InvalidRequest(f"JEFF_ACCOUNTS_MAX_STALE_SECONDS={value!r}; use a finite number of seconds")
    if seconds < MIN_STALE_SECONDS:
        raise InvalidRequest(f"JEFF_ACCOUNTS_MAX_STALE_SECONDS={value!r}; use at least {MIN_STALE_SECONDS:g}, two "
                             "of the key list's 60-second fetches")
    return seconds


def _usage_file(path: Path) -> Path:
    if not path.parent.is_dir():
        raise InvalidRequest(f"JEFF_USAGE_FILE={path}: its folder {path.parent} does not exist; create it first")
    return path
