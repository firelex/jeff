"""Accounts mode (jeff_server.settings.AccountsSettings): API keys made on the hub for paying accounts.

Once a minute (TICK_S) the server fetches the hub's list of usable keys (GET <JEFF_ACCOUNTS_URL>/api/internal/v1/keys,
contract schema accounts_keys) and posts its usage totals back (POST <JEFF_ACCOUNTS_URL>/api/internal/v1/usage, schema
accounts_usage), each with the shared secret from JEFF_ACCOUNTS_TOKEN_FILE as a bearer token. The secret goes only to
those two addresses: JEFF_ACCOUNTS_URL is https:// (the settings refuse anything else), the server's client verifies
the hub's certificate and never follows a redirect (one is an error like any other unexpected answer); an answer
larger than MAX_ANSWER_BYTES or slower than FETCH_DEADLINE_S is refused. When JEFF_ACCOUNTS_BYPASS_FILE is set, both
calls also carry Vercel's deployment-protection bypass secret from that file as x-vercel-protection-bypass, for a
JEFF_ACCOUNTS_URL that is itself a Vercel preview behind Deployment Protection; unset, no such header is sent. The
hub is never
called on a request's path: a call with an account key is checked against the last good list in memory. A fetch or
report that fails is logged with its reason and tried again at the next tick; the last good list stays in force until
it is older than JEFF_ACCOUNTS_MAX_STALE_SECONDS, after which every account key is refused (the owner's API key keeps
working).

A key (jeff_live_ + base32) is looked up by its SHA-256 hash. It gives Caller("account", <account id>, ALL_ADAPTERS):
every adapter's licence, none of the owner's endpoints, one rate-limit bucket per account. The playground's key gives
Caller("playground", "playground", ALL_ADAPTERS) at the list's runs_per_second, with no allowance; with an
X-Jeff-Account header naming a usable account, the playground's call runs as that account (the hub's playground for
a signed-in subscriber).

No key, no key hash (not even part of one) and never the shared secret goes into a log line, an error or a reply: a
problem in the key list is named by its place in the list and its account.

Each such caller carries a Meter, for the period in force (in_force: after a period's end, the next one is assumed
until the key list brings it). jeff_server.decide.caller_view gives each request a view whose engine (MeteredEngine)
checks the allowance at the request's first engine request (UsageLimitReached at or past it) and counts the input
tokens of every engine reply; an engine request that ran is counted even when a later part of the same API request
fails. Callers with a hub-signed token (JEFF_HUB_PUBLIC_KEY) carry no meter: such tokens are for the owner's own
services only, never for customers."""

import copy
import hashlib
import hmac
import json
import logging
import math
import re
import threading
import traceback
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import cache
from typing import Any

import anyio
import anyio.to_thread
import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match

from jeff import contract
from jeff.client import Jeff
from jeff.engine import Engine, EngineResult
from jeff.errors import EngineError, InvalidRequest, Unauthorised, UsageLimitReached

from jeff_server.auth import ALL_ADAPTERS, Caller, RateLimiter
from jeff_server.settings import AccountsSettings
from jeff_server.usage import UsageBook, utc_text

log = logging.getLogger("jeff_server")

PLAYGROUND = "playground"
PLAYGROUND_RATE_KEY = f"playground:{PLAYGROUND}"  # Caller("playground", PLAYGROUND, ...).rate_key
TICK_S = 60.0
HTTP_TIMEOUT_S = 10.0  # each connect, read and write of one request to the hub
# The whole of one request to the hub, from sending it to the last byte of its answer: checked between the chunks of
# the answer, so a hub dripping bytes is cut off within FETCH_DEADLINE_S plus one HTTP_TIMEOUT_S read.
FETCH_DEADLINE_S = 30.0
# The largest answer read from the hub. A key is about 110 bytes of JSON and an account about 130, so this holds well
# over 100,000 accounts with ten keys each.
MAX_ANSWER_BYTES = 16 * 1024 * 1024
TIME_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
KEYS_PATH = "/api/internal/v1/keys"
USAGE_PATH = "/api/internal/v1/usage"
# The schema's patterns, matched against the whole value: jsonschema matches a pattern with re.search, whose $ also
# matches before a final newline, so "acct1\n" passes the schema alone.
ACCOUNT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")
KEY_HASH = re.compile(r"[0-9a-f]{64}")


class AccountsError(Exception):
    """The hub's internal API could not be reached, or answered something unusable. Logged by Accounts.tick, which
    tries again at the next tick; never sent to a client."""


@dataclass(frozen=True)
class Account:
    id: str
    allowance: int | None  # input tokens per period; None for the playground, which has no allowance
    period_start: str      # YYYY-MM-DDTHH:MM:SSZ
    period_end: str


@dataclass(frozen=True)
class KeyList:
    accounts: Mapping[str, Account]
    keys: Mapping[str, str] = field(repr=False)  # key hash -> account id; hashes never in a repr
    playground_hash: str = field(repr=False)
    playground: Account
    playground_rps: float


@cache
def _schema() -> Draft202012Validator:
    return Draft202012Validator(contract.load_schema("accounts_keys"))


def _schema_problem(body: object) -> str | None:
    """Where the key list first breaks its schema and which rule, without quoting the value there (it may be a key's
    hash, or a raw key the hub sent by mistake); None when it matches."""
    problem = best_match(_schema().iter_errors(body))
    if problem is None:
        return None
    if problem.validator == "required":  # the message names the missing property, a name from the schema
        return f"at {problem.json_path}: {problem.message}"
    return f"at {problem.json_path}: the value breaks the schema's {problem.validator!r} rule"


def _time(name: str, which: str, text: str) -> datetime:
    try:
        return datetime.strptime(text, TIME_FORMAT)
    except ValueError:
        raise AccountsError(f"The hub's key list gives {name} a {which} that is not a real time "
                            "(YYYY-MM-DDTHH:MM:SSZ, UTC)") from None


def _period(name: str, start: str, end: str) -> None:
    if _time(name, "period_end", end) <= _time(name, "period_start", start):
        raise AccountsError(f"The hub's key list gives {name} a period that ends ({end}) before it starts ({start})")


def parse_key_list(body: object) -> KeyList:
    problem = _schema_problem(body)
    if problem is not None:
        raise AccountsError(f"The hub's key list does not match contract/schemas/accounts_keys.schema.json {problem}")
    assert isinstance(body, dict)  # the schema's root is an object
    accounts: dict[str, Account] = {}
    for place, entry in enumerate(body["accounts"]):
        account = Account(entry["account"], entry["allowance"], entry["period_start"], entry["period_end"])
        if not ACCOUNT_ID.fullmatch(account.id):  # the id itself is not quoted: it may hold a newline
            raise AccountsError(f"The hub's key list has an account id (accounts[{place}]) with characters outside "
                                f"{ACCOUNT_ID.pattern}")
        if account.id == PLAYGROUND:
            raise AccountsError("The hub's key list has an account named 'playground', the playground's own name")
        if account.id in accounts:
            raise AccountsError(f"The hub's key list names account {account.id} twice")
        _period(f"account {account.id}", account.period_start, account.period_end)
        accounts[account.id] = account
    raw = body["playground"]
    _period("the playground", raw["period_start"], raw["period_end"])
    if not KEY_HASH.fullmatch(raw["hash"]):
        raise AccountsError(f"The hub's key list gives the playground a hash with characters outside "
                            f"{KEY_HASH.pattern}")
    rps = raw["runs_per_second"]
    # json reads Infinity and NaN, which the schema's exclusiveMinimum lets through
    if isinstance(rps, bool) or not isinstance(rps, (int, float)) or not math.isfinite(rps) or rps <= 0:
        raise AccountsError("The hub's key list gives the playground a runs_per_second that is not a finite number "
                            "above 0")
    keys: dict[str, str] = {}
    for place, entry in enumerate(body["keys"]):
        key_hash, owner = entry["hash"], entry["account"]
        if not KEY_HASH.fullmatch(key_hash):
            raise AccountsError(f"The hub's key list has a hash (keys[{place}]) with characters outside "
                                f"{KEY_HASH.pattern}")
        if owner not in accounts:
            if not ACCOUNT_ID.fullmatch(owner):
                raise AccountsError(f"The hub's key list has a key (keys[{place}]) whose account id has characters "
                                    f"outside {ACCOUNT_ID.pattern}")
            raise AccountsError(f"The hub's key list has a key (keys[{place}]) of account {owner}, which is not "
                                "among its accounts")
        if key_hash == raw["hash"]:
            raise AccountsError(f"The hub's key list gives account {owner} a key (keys[{place}]) whose hash is the "
                                "playground's")
        if key_hash in keys:
            raise AccountsError(f"The hub's key list has the hash of keys[{place}] (account {owner}) twice")
        keys[key_hash] = owner
    playground = Account(PLAYGROUND, None, raw["period_start"], raw["period_end"])
    return KeyList(accounts, keys, raw["hash"], playground, float(rps))


def usage_limit_reached(account: Account, used: int) -> UsageLimitReached:
    if account.allowance is None:
        raise ValueError(f"usage_limit_reached for {account.id}, which has no allowance: a server bug")
    end = datetime.strptime(account.period_end, TIME_FORMAT)
    return UsageLimitReached(f"This account has used its {account.allowance:,} input tokens for the period ending "
                             f"{end:%Y-%m-%d %H:%M} UTC; calls resume then",
                             account.id, used, account.allowance, account.period_end)


def in_force(account: Account, now: datetime) -> Account:
    """The account's period at `now`. Billing periods are contiguous (each starts when the one before ends), so when
    `now` has reached the end of the period the key list gives and the list has not yet brought the next one, the
    next is taken to be [period_end, period_end + the same length), with the same allowance, until the key list
    says otherwise: an account that used its allowance is admitted again as soon as its period ends. The start, which
    keys the usage book, is the next period's real start; only the assumed end can differ from the hub's (months
    differ in length), and the next key list corrects it."""
    start = datetime.strptime(account.period_start, TIME_FORMAT).replace(tzinfo=UTC)
    end = datetime.strptime(account.period_end, TIME_FORMAT).replace(tzinfo=UTC)
    if now < end:
        return account
    length = end - start  # the key list refuses a period that does not end after it starts, so this is above 0
    while now >= end:
        start, end = end, end + length
    return Account(account.id, account.allowance, utc_text(start), utc_text(end))


@dataclass(frozen=True)
class Meter:
    """One caller's usage: its account with the period in force when the call arrived (in_force; so a call that
    crosses a period's end counts in the period it started in), and the key it came with (None for the playground's
    calls)."""
    book: UsageBook
    account: Account
    key_hash: str | None = field(repr=False)
    now: Callable[[], datetime]

    def touch(self) -> None:
        """Notes the key as used (the hub shows each key's last use), for any request, whether or not it reaches
        the model."""
        if self.key_hash is not None:
            self.book.touch(self.key_hash, utc_text(self.now()))

    def check(self) -> None:
        """Before a call's first engine request: refuses the call (UsageLimitReached) when the account has used its
        allowance this period. A call admitted here runs to its end, even past the allowance."""
        if self.account.allowance is None:
            return
        used = self.book.total(self.account.id, self.account.period_start)
        if used >= self.account.allowance:
            raise usage_limit_reached(self.account, used)

    def count(self, tokens: int) -> None:
        self.book.add(self.account.id, self.account.period_start, tokens)

    def view(self, jeff: Jeff) -> Jeff:
        """A view of `jeff` (already the caller's licence view, for one request) whose engine checks the allowance
        at the request's first engine request and counts every reply's input tokens. A request that never asks the
        model (a listing, a bind, one refused before deciding) is never refused for the allowance. A shallow copy, as
        Jeff.with_authorise makes: the shared Jeff is never changed."""
        self.touch()
        metered = copy.copy(jeff)
        metered.engine = MeteredEngine(jeff.engine, self)
        return metered


# Engine methods that would reach the model without passing through MeteredEngine.predict: refused on a metered view
# rather than forwarded uncounted (no door of the server calls them on a request's path).
UNMETERED = frozenset({"predict_many", "prepare"})


class MeteredEngine:
    """The engine of one request's view, counting on its caller's meter. The request's first predict is checked
    against the allowance (Meter.check) before the engine sees a row; every later predict of the same request (the
    rest of a batch, a router's next level, a graph's next node, an escalation step) is admitted, so a call admitted
    at its start runs to its end. Every reply's input tokens are counted, also when a later part of the same request
    then fails: the engine did that work. A reply without a count for each row, or with a count that is not a whole
    number of 0 or more, is refused (EngineError): the call cannot be counted, so it is not served (never a guessed
    count). Everything else is the inner engine's."""

    def __init__(self, inner: Engine, meter: Meter) -> None:
        self.inner, self.meter = inner, meter
        self.base_format = inner.base_format
        self._admitted = False
        self._lock = threading.Lock()

    def installed(self, refresh: bool = False) -> set[str]:
        return self.inner.installed(refresh)

    def predict(self, adapter_id: str, rows: list[dict[str, Any]], **options: Any) -> EngineResult:
        with self._lock:
            if not self._admitted:
                self.meter.check()
                self._admitted = True
        result = self.inner.predict(adapter_id, rows, **options)
        counts = result.input_tokens
        if counts is None:
            raise EngineError(f"The engine reply for {adapter_id} carries no input_tokens, so the call cannot be "
                              "counted; run the engines from a jeff-runtime that reports them")
        if len(counts) != len(rows) or not all(type(count) is int and count >= 0 for count in counts):
            raise EngineError(f"The engine reply for {adapter_id} gives {len(counts)} input token counts for "
                              f"{len(rows)} rows, or a count that is not a whole number of 0 or more, so the call "
                              "cannot be counted")
        self.meter.count(sum(counts))
        return result

    def __getattr__(self, name: str) -> Any:
        if name in UNMETERED:
            raise AttributeError(f"MeteredEngine does not offer {name}: it would reach the model without counting "
                                 "the caller's input tokens; call predict")
        return getattr(self.inner, name)


class Accounts:
    def __init__(self, settings: AccountsSettings, book: UsageBook, limiter: RateLimiter, http: httpx.Client,
                 clock: Callable[[], float], now: Callable[[], datetime]) -> None:
        self.settings, self.book, self.limiter, self.http = settings, book, limiter, http
        self.clock, self.now = clock, now  # clock: monotonic seconds, for the list's age; now: UTC, for last-used
        self._lock = threading.Lock()
        self._list: KeyList | None = None
        self._fetched_at: float | None = None

    def _headers(self) -> dict[str, str]:
        """Both internal calls go to self.settings.url alone (KEYS_PATH and USAGE_PATH, joined with it in _ask), so
        the bypass header, when the setting is set, never reaches anywhere else."""
        headers = {"Authorization": f"Bearer {self.settings.token}"}
        if self.settings.bypass is not None:
            headers["x-vercel-protection-bypass"] = self.settings.bypass
        return headers

    def _ask(self, method: str, path: str, body: Any | None) -> tuple[httpx.Response, bytes]:
        """One request to the hub's internal API and its whole answer, read in chunks: refused (AccountsError) past
        FETCH_DEADLINE_S from sending, or past MAX_ANSWER_BYTES, or on any HTTP failure. Never quotes the answer."""
        url = f"{self.settings.url}{path}"
        deadline = self.clock() + FETCH_DEADLINE_S
        chunks: list[bytes] = []
        size = 0
        try:
            with self.http.stream(method, url, headers=self._headers(), json=body) as response:
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > MAX_ANSWER_BYTES:
                        raise AccountsError(f"{method} {url} answered more than {MAX_ANSWER_BYTES:,} bytes, the "
                                            "most this server reads from the hub")
                    if self.clock() > deadline:
                        raise AccountsError(f"{method} {url} took more than {FETCH_DEADLINE_S:g} seconds to answer")
                    chunks.append(chunk)
        except httpx.HTTPError as error:
            raise AccountsError(f"{method} {url} failed: {type(error).__name__}: {error}") from error
        return response, b"".join(chunks)

    def refresh(self) -> None:
        """Fetches the key list and puts it in force; any failure raises AccountsError and leaves the last good list
        (its age, and the playground's rate) as it was. The list's age counts from when the request was sent."""
        sent = self.clock()
        response, content = self._ask("GET", KEYS_PATH, None)
        url = f"{self.settings.url}{KEYS_PATH}"
        if response.status_code != 200:  # the body is not quoted: it could hold anything, hashes included
            raise AccountsError(f"GET {url} answered {response.status_code} {response.reason_phrase}, not 200 with "
                                "the key list")
        try:
            body = json.loads(content)
        except (ValueError, RecursionError) as error:  # RecursionError: a body nested too deep to parse
            raise AccountsError(f"GET {url} answered a body that cannot be read as JSON: "
                                f"{type(error).__name__}") from None
        keys = parse_key_list(body)
        try:
            self.limiter.set_rate(PLAYGROUND_RATE_KEY, keys.playground_rps)  # before the swap: a refusal changes nothing
        except InvalidRequest as error:
            raise AccountsError(f"GET {url}: the playground's rate cannot be used: {error}") from error
        with self._lock:
            self._list, self._fetched_at = keys, sent

    def report(self) -> None:
        url = f"{self.settings.url}{USAGE_PATH}"
        response, _ = self._ask("POST", USAGE_PATH, self.book.report())
        if response.status_code not in (200, 204):
            raise AccountsError(f"POST {url} answered {response.status_code} {response.reason_phrase}, not 200 "
                                "or 204")

    def tick(self) -> None:
        """One minute's work: fetch the key list, then report usage. Never raises: either step failing is logged with
        its reason and tried again at the next tick (the spec's rule); meanwhile the last good list stays in force
        until it is too old. A failure that is not an AccountsError is a server bug: it is logged with its type and
        where it was raised, but not its message, which may quote the hub's answer."""
        for step in (self.refresh, self.report):
            self._tick_step(step)

    def _tick_step(self, step: Callable[[], None]) -> None:
        """One step of tick (refresh or report), logged and never raised past here -- split out of tick() so
        run_every_minute can run refresh and report as two separate anyio.to_thread.run_sync calls, with an await
        point between them where a cancellation lands before the next step starts, rather than only after a whole
        tick (refresh and report together) has run."""
        try:
            step()
        except AccountsError as error:
            log.error("Accounts mode: %s; trying again in %g seconds", error, TICK_S)
        except Exception as error:  # the minute loop must outlive one bad answer (the spec: retry next tick)
            frame = traceback.extract_tb(error.__traceback__)[-1]
            log.error("Accounts mode: %s failed with %s at %s:%d (a server bug; its message is not logged, as it "
                      "may quote the hub's answer); trying again in %g seconds", step.__name__,
                      type(error).__name__, frame.filename.rsplit("/", 1)[-1], frame.lineno, TICK_S)

    def _current(self) -> KeyList:
        with self._lock:
            keys, fetched_at = self._list, self._fetched_at
        if keys is None or fetched_at is None:
            raise Unauthorised("Account keys cannot be checked: the server has not fetched the key list from the "
                               "hub since it started")
        age = self.clock() - fetched_at
        if age > self.settings.max_stale_s:
            raise Unauthorised(f"Account keys cannot be checked: the key list is {age:.0f} seconds old")
        return keys

    def caller(self, token: str, on_behalf: str | None) -> Caller:
        keys = self._current()
        now = self.now()
        key_hash = hashlib.sha256(token.encode()).hexdigest()
        if hmac.compare_digest(key_hash, keys.playground_hash):
            if on_behalf is None:
                return Caller("playground", PLAYGROUND, ALL_ADAPTERS,
                              meter=Meter(self.book, in_force(keys.playground, now), None, self.now))
            if on_behalf not in keys.accounts:
                raise Unauthorised(f"X-Jeff-Account names {on_behalf[:64]!r}, which is not an account that may "
                                   "use keys now")
            return Caller("account", on_behalf, ALL_ADAPTERS,
                          meter=Meter(self.book, in_force(keys.accounts[on_behalf], now), None,
                                      self.now))
        if on_behalf is not None:
            raise Unauthorised("X-Jeff-Account is accepted only with the hub playground's key")
        # Looked up by the SHA-256 of the presented token, so how long the lookup takes depends only on that hash,
        # which the caller cannot steer towards a stored one: it tells nothing about any stored key.
        owner = keys.keys.get(key_hash)
        if owner is None:
            raise Unauthorised(f"This API key is not valid; create one at {self.settings.url}/account")
        account = in_force(keys.accounts[owner], now)
        return Caller("account", account.id, ALL_ADAPTERS, meter=Meter(self.book, account, key_hash, self.now))

    def close(self) -> None:
        """Closes the hub client and the usage book, the book even if closing the client raises, so the usage
        file's lock (UsageBook's own flock) is always given back."""
        try:
            self.http.close()
        finally:
            self.book.close()


async def run_every_minute(accounts: Accounts, tick_s: float, limiter: anyio.CapacityLimiter) -> None:
    """Accounts.refresh then Accounts.report, each its own anyio.to_thread.run_sync call (never abandon_on_cancel:
    a step already running on its worker thread always finishes) on a worker thread of `limiter` (the operations
    threads, so slow predictions never hold it up), now and then every tick_s seconds, until cancelled. Refresh and
    report are two separate calls, not one call to Accounts.tick, so a cancellation lands at the await point between
    them -- before the next step starts -- rather than only after a whole tick has run; the caller (jeff_server.app)
    awaits this task after cancelling it, so a step already in flight is never still running once that wait returns.
    Each step logs its own failures (Accounts._tick_step) and never raises; anything else raised here is a bug and
    ends the loop, logged, after which the key list goes stale and account keys are refused."""
    try:
        while True:
            await anyio.to_thread.run_sync(accounts._tick_step, accounts.refresh, limiter=limiter)
            await anyio.to_thread.run_sync(accounts._tick_step, accounts.report, limiter=limiter)
            await anyio.sleep(tick_s)
    except Exception:
        log.exception("Accounts mode: the minute loop stopped; account keys will be refused once the key list is "
                      "%g seconds old", accounts.settings.max_stale_s)
        raise
