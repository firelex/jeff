"""Who is calling, what they may call, and how often.

Credentials arrive as `Authorization: Bearer <token>`. Four kinds are accepted, each only when configured:

- the static API key (JEFF_API_KEY), compared in constant time: the owner, holding every licence;
- a console session token from POST /ui/login (JEFF_UI_USERS, see jeff_server.users): also the owner;
- a hub-signed JWT (JEFF_HUB_PUBLIC_KEY): EdDSA (Ed25519) only, with `sub` (a non-empty string), `licences` (a list
  of adapter IDs), `iat` and `exp`, lasting no more than 3600 seconds. The server never calls the hub to check one;
- an account key (`jeff_live_...`, accounts mode, jeff_server.accounts): an account on the hub holding every adapter
  licence (ALL_ADAPTERS) but never the owner; the hub playground's key is one too, with its own rate
  (RateLimiter.set_rate) and, with an X-Jeff-Account header, standing in for that account.

With no credential configured the API is open (private use): every caller is the owner. With any configured, every
/v1 route needs one, otherwise 401 Unauthorised with WWW-Authenticate: Bearer.

Licences: a function of an adapter whose manifest has licence.kind "proprietary" answers only for a caller whose
licences include that adapter's ID, otherwise 403 NotLicensed. The check is the authorise hook of jeff.Jeff, called
with every function just before it is decided, so it also covers the leaf a dispatch reaches, escalation steps and
graph nodes; jeff_server.decide gives each request its own view of the shared Jeff carrying that caller's hook.

Rate limits (JEFF_RATE_LIMIT="<rps>/<concurrent>"): each caller has a token bucket refilled at rps per second holding
at most rps requests (at least one, so a rate below one per second still lets one request through), and at most
`concurrent` requests in flight; over either, 429 RateLimited with Retry-After. Callers are told apart by kind and
subject, so a hub token whose sub is "api-key" never shares the API key's bucket.

The in-flight slot is held until the route returns. Endpoints run the planner with run_in_threadpool, which cannot be
cancelled, so a client that disconnects keeps its slot until the engine call it started returns.
"""

import hmac
import math
import threading
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from jeff.catalogue import Catalogue
from jeff.client import Authorise
from jeff.errors import EngineError, InvalidRequest, NotLicensed, RateLimited, Unauthorised
from jeff.manifest import FunctionSpec, Manifest

from jeff_server.access_log import AccessLog
from jeff_server.errors import error_response
from jeff_server.settings import ServerSettings
from jeff_server.users import SignInGuard, UserStore

if TYPE_CHECKING:
    from jeff_server.accounts import Accounts, Meter

MAX_TOKEN_SECONDS = 3600
# How long a caller at its concurrent limit is asked to wait: when one of its requests ends is not known in advance.
CONCURRENT_RETRY_AFTER_S = 1.0
# Buckets of callers that are idle and full are dropped once there are more than this many, to bound memory; the
# scan over every bucket runs once per SWEEP_INTERVAL_S or when the buckets have doubled since the last scan, never
# on every request (see RateLimiter._sweep_due).
MAX_IDLE_BUCKETS = 1024
SWEEP_INTERVAL_S = 60.0


KEY_PREFIX = "jeff_live_"  # every account key, and the playground's (jeff_server.accounts)
ACCOUNT_HEADER = "x-jeff-account"  # the hub playground runs a signed-in subscriber's call as their account


class _EveryAdapter(frozenset[str]):
    """An account's licences: every adapter, open or proprietary, including ones added after the caller was made.
    Not None, which is the owner's: an account never reaches the owner's endpoints."""

    def __contains__(self, item: object) -> bool:
        return isinstance(item, str)

    def __repr__(self) -> str:
        return "ALL_ADAPTERS"


ALL_ADAPTERS: frozenset[str] = _EveryAdapter()


@dataclass(frozen=True)
class Caller:
    kind: Literal["open", "api-key", "console", "hub", "account", "playground"]
    subject: str
    licences: frozenset[str] | None  # None: the owner, holding every licence
    # When the credential stops being valid, in seconds since the epoch: a hub token's exp, a console session's end;
    # None for the API key, account keys and the open caller, which do not expire. The channel (jeff_server.channel)
    # closes a connection whose credential has expired; an HTTP request is checked once, when it arrives.
    expires: float | None = None
    # An account's or the playground's usage meter (jeff_server.accounts.Meter): checks the allowance and counts
    # input tokens. None for every other kind.
    meter: "Meter | None" = field(default=None, compare=False, repr=False)

    @property
    def rate_key(self) -> str:
        return f"{self.kind}:{self.subject}"

    @property
    def log_user(self) -> str | None:
        """Who this caller is in the access log, one scheme for every entry: a console session by its bare login
        (as its sign-in is logged, so a user's sign-ins, API calls and views share one People card); a hub token
        or the API key as "<kind>:<subject>", so a hub sub never merges with a console login of the same text;
        an "open" caller (no credentials configured at all) as None, not signed in."""
        if self.kind == "open":
            return None
        if self.kind == "console":
            return self.subject
        return f"{self.kind}:{self.subject}"


OPEN_CALLER = Caller("open", "anonymous", None)
API_KEY_CALLER = Caller("api-key", "api-key", None)


def load_hub_public_key(path: Path) -> Ed25519PublicKey:
    if not path.is_file():
        raise InvalidRequest(f"JEFF_HUB_PUBLIC_KEY={path} does not exist; point it at the hub's Ed25519 public key "
                             "(PEM), or make a development pair with jeff-server token keygen")
    try:
        key = serialization.load_pem_public_key(path.read_bytes())
    except ValueError as error:
        raise InvalidRequest(f"JEFF_HUB_PUBLIC_KEY={path} is not a PEM public key: {error}") from error
    if not isinstance(key, Ed25519PublicKey):
        raise InvalidRequest(f"JEFF_HUB_PUBLIC_KEY={path} is a {type(key).__name__}; hub tokens are signed with "
                             "Ed25519, so it must be an Ed25519 public key")
    return key


class Authenticator:
    def __init__(self, api_key: str | None, users: UserStore | None, hub_key: Ed25519PublicKey | None,
                 accounts: "Accounts | None") -> None:
        self.api_key, self.users, self.hub_key, self.accounts = api_key, users, hub_key, accounts

    @classmethod
    def from_settings(cls, settings: ServerSettings, accounts: "Accounts | None") -> "Authenticator":
        return cls(settings.api_key,
                   UserStore(settings.users_file) if settings.users_file is not None else None,
                   load_hub_public_key(settings.hub_public_key) if settings.hub_public_key is not None else None,
                   accounts)

    @property
    def configured(self) -> bool:
        return (self.api_key is not None or self.users is not None or self.hub_key is not None
                or self.accounts is not None)

    def caller(self, authorization: str | None, on_behalf: str | None) -> Caller:
        """The caller for this Authorization header; on_behalf is the request's X-Jeff-Account header (None when
        absent), accepted only with the hub playground's key."""
        scheme, _, given = (authorization or "").partition(" ")
        if on_behalf is not None and not (self.accounts is not None and scheme.lower() == "bearer"
                                          and given.startswith(KEY_PREFIX)):
            raise Unauthorised("X-Jeff-Account is accepted only with the hub playground's key")
        if not self.configured:
            return OPEN_CALLER
        if scheme.lower() != "bearer" or not given:
            raise Unauthorised("This server needs credentials: send Authorization: Bearer <API key, account key, "
                               "console session token or hub-signed token>")
        if self.api_key is not None and hmac.compare_digest(given.encode(), self.api_key.encode()):
            if on_behalf is not None:
                raise Unauthorised("X-Jeff-Account is accepted only with the hub playground's key")
            return API_KEY_CALLER
        if given.startswith(KEY_PREFIX):
            if self.accounts is None:
                raise Unauthorised("This server does not accept account keys: accounts mode is off "
                                   "(JEFF_ACCOUNTS_URL is not set)")
            return self.accounts.caller(given, on_behalf)
        if given.count(".") == 2:  # header.payload.signature: a JWT (a console session token has one dot)
            return self._hub_caller(given)
        if self.users is not None:
            session = self.users.session(given)
            if session is not None:
                login, expires = session
                return Caller("console", login, None, expires)
        raise Unauthorised("The credentials are not valid on this server: not its API key, and not a current "
                           "console session token")

    def _hub_caller(self, token: str) -> Caller:
        if self.hub_key is None:
            raise Unauthorised("This server does not accept hub-signed tokens (JEFF_HUB_PUBLIC_KEY is not set)")
        try:
            claims: dict[str, Any] = jwt.decode(token, self.hub_key, algorithms=["EdDSA"],
                                                options={"require": ["exp", "iat", "sub"]})
        except jwt.InvalidTokenError as error:
            raise Unauthorised(f"The hub-signed token is not valid: {error}") from error
        sub, licences, issued, expires = claims["sub"], claims.get("licences"), claims["iat"], claims["exp"]
        if not isinstance(sub, str) or not sub:
            raise Unauthorised("The hub-signed token's sub must be a non-empty string")
        if not isinstance(licences, list) or not all(isinstance(item, str) for item in licences):
            raise Unauthorised("The hub-signed token needs licences: a list of adapter IDs (empty for none)")
        # PyJWT accepts iat and exp given as numeric strings; the lifetime is computed here, so insist on numbers.
        if not all(isinstance(value, (int, float)) and not isinstance(value, bool) for value in (issued, expires)):
            raise Unauthorised("The hub-signed token's iat and exp must be numbers (seconds since the epoch)")
        if expires - issued > MAX_TOKEN_SECONDS:
            raise Unauthorised(f"The hub-signed token lasts {expires - issued} seconds; at most {MAX_TOKEN_SECONDS} "
                               "are accepted")
        return Caller("hub", sub, frozenset(licences), float(expires))


def check_adapter_licence(caller: Caller, manifest: Manifest, what: str) -> None:
    """403 NotLicensed unless the caller may use the adapter: the owner holds every licence, an open adapter needs
    none, a proprietary one needs its ID in the caller's licences. `what` names what was called (a function ID,
    or a /v1/systemone model)."""
    if caller.licences is None or manifest.licence_kind != "proprietary" or manifest.id in caller.licences:
        return
    raise NotLicensed(f"{what} belongs to {manifest.id}, a proprietary adapter, and the token for "
                      f"{caller.subject} does not include its licence; ask the hub for a token listing "
                      f"{manifest.id} in licences")


def licence_check(caller: Caller, catalogue: Catalogue) -> Authorise | None:
    """The jeff.Jeff authorise hook for this caller: None for the owner, who holds every licence."""
    if caller.licences is None:
        return None

    def authorise(function: FunctionSpec) -> None:
        check_adapter_licence(caller, catalogue.manifest_of(function), function.id)

    return authorise


class RateLimiter:
    """Per caller: a token bucket (rps per second, holding at most max(rps, 1)) and a count of requests in flight
    (at most `concurrent`). Thread-safe: one lock guards both."""

    def __init__(self, rps: float, concurrent: int, clock: Callable[[], float] = time.monotonic) -> None:
        if isinstance(rps, bool) or not isinstance(rps, (int, float)) or not math.isfinite(rps) or rps <= 0:
            raise InvalidRequest(f"RateLimiter needs a finite rate above 0 requests per second; got {rps!r}")
        if isinstance(concurrent, bool) or not isinstance(concurrent, int) or concurrent <= 0:
            raise InvalidRequest(f"RateLimiter needs a whole number of concurrent requests above 0; got {concurrent!r}")
        self.rps, self.concurrent, self.clock = float(rps), concurrent, clock
        self._last_sweep: float | None = None
        self._size_after_sweep = 0
        self.burst = max(rps, 1.0)
        self.buckets: dict[str, tuple[float, float]] = {}  # key -> (tokens, when last refilled)
        self.in_flight: dict[str, int] = {}
        self.rates: dict[str, float] = {}  # per-key rates set with set_rate
        self._lock = threading.Lock()

    def set_rate(self, key: str, rps: float) -> None:
        """Gives one caller its own rate instead of the server's (the playground: the key list's runs_per_second,
        across every visitor); its bucket holds max(rps, 1). The concurrent limit stays the server's."""
        if isinstance(rps, bool) or not isinstance(rps, (int, float)) or not math.isfinite(rps) or rps <= 0:
            raise InvalidRequest(f"RateLimiter.set_rate needs a finite rate above 0 requests per second; got {rps!r}")
        with self._lock:
            self.rates[key] = float(rps)

    def _rate(self, key: str) -> tuple[float, float]:
        """(rate, burst) for this key: its own when set_rate gave it one, otherwise the server's."""
        rps = self.rates[key] if key in self.rates else self.rps
        return rps, max(rps, 1.0)

    def acquire(self, key: str) -> None:
        """Admit one request for `key`, or raise RateLimited saying how long to wait. Every admitted request must be
        released exactly once."""
        with self._lock:
            now = self.clock()
            if self._sweep_due(now):
                self._last_sweep = now
                self._drop_idle(now)
                self._size_after_sweep = len(self.buckets)
            rps, burst = self._rate(key)
            tokens, last = self.buckets.get(key, (burst, now))
            tokens = min(burst, tokens + (now - last) * rps)
            self.buckets[key] = (tokens, now)
            subject = key.partition(":")[2]
            if self.in_flight.get(key, 0) >= self.concurrent:
                raise RateLimited(f"{subject} already has {self.concurrent} request(s) in flight, the most this "
                                  "server allows per caller; wait for one to finish",
                                  retry_after_s=CONCURRENT_RETRY_AFTER_S)
            if tokens < 1.0:
                raise RateLimited(f"{subject} is over this server's rate limit of {rps:g} request(s) per "
                                  "second", retry_after_s=(1.0 - tokens) / rps)
            self.buckets[key] = (tokens - 1.0, now)
            self.in_flight[key] = self.in_flight.get(key, 0) + 1

    def release(self, key: str) -> None:
        with self._lock:
            count = self.in_flight.get(key, 0)
            if count <= 0:
                raise EngineError(f"RateLimiter.release({key!r}) without a matching acquire: a server bug")
            if count == 1:
                del self.in_flight[key]
            else:
                self.in_flight[key] = count - 1

    def _sweep_due(self, now: float) -> bool:
        """Over the cap, a sweep is due once per SWEEP_INTERVAL_S, and also whenever the buckets have doubled since
        the last sweep left them (counting at least the cap): a sweep scans n buckets only after about n/2 new ones
        were added, so its cost is amortised O(1) per request, and the dict stays within about twice the larger of
        the cap and the callers that are not idle."""
        size = len(self.buckets)
        if size <= MAX_IDLE_BUCKETS:
            return False
        if self._last_sweep is None or now - self._last_sweep >= SWEEP_INTERVAL_S:
            return True
        return size > 2 * max(self._size_after_sweep, MAX_IDLE_BUCKETS)

    def _drop_idle(self, now: float) -> None:
        for key, (tokens, last) in list(self.buckets.items()):
            rps, burst = self._rate(key)
            if key not in self.in_flight and tokens + (now - last) * rps >= burst:
                del self.buckets[key]


async def authenticated(request: Request) -> Caller:
    """The caller, or 401 Unauthorised. A dependency: every /v1 route has it (through V1_DEPENDENCIES), and any
    other route that must be signed in (/openapi.json) adds it with Depends(authenticated). Async, so it runs on
    the event loop rather than taking a thread from the pool that slow predictions may have filled: the check is
    quick (a key comparison, a signature check, a session lookup in the cached users file)."""
    auth: Authenticator = request.app.state.auth
    caller = auth.caller(request.headers.get("authorization"), request.headers.get(ACCOUNT_HEADER))
    request.state.caller = caller
    return caller


async def limited(request: Request, caller: Caller = Depends(authenticated)) -> AsyncGenerator[Caller]:
    """The caller, admitted by the rate limiter (when one is configured) for as long as the route runs. The
    in-flight slot is released in `finally`, so a route that raises frees it too. A client that disconnects does
    not free it early: the planner's engine call runs in run_in_threadpool, which cannot be cancelled, so the route
    (and the slot) ends only when that call returns; a cancellation that does reach the route passes through the
    same `finally`."""
    limiter: RateLimiter | None = request.app.state.limiter
    if limiter is None:
        yield caller
        return
    limiter.acquire(caller.rate_key)
    try:
        yield caller
    finally:
        limiter.release(caller.rate_key)


# Every /v1 router is built with these. scope="function" ends `limited` when the route function returns, before the
# response is sent, so a client sending its next request on receiving this response never finds the slot still held.
V1_DEPENDENCIES = [Depends(limited, scope="function")]


def request_caller(request: Request) -> Caller:
    """The caller `authenticated` stored on this request."""
    caller: Caller = request.state.caller
    return caller


# --- console sign-in -----------------------------------------------------------------------------------------------

login_router = APIRouter()


class SignIn(BaseModel):
    email: str
    password: str


@login_router.post("/ui/login", response_model=None)
def console_login(details: SignIn, request: Request) -> dict[str, str] | JSONResponse:
    """Checks a console sign-in against the JEFF_UI_USERS file and returns a session token, which the API accepts in
    place of the API key. Repeated failures lock sign-in for a while (429 RateLimited with Retry-After)."""
    users: UserStore | None = request.app.state.auth.users
    if users is None:
        # No bearer token would help here, so this 401 carries no WWW-Authenticate challenge.
        return error_response(request, Unauthorised(
            "Console sign-in is not configured on this server: set JEFF_UI_USERS to a users file (make one with "
            "jeff-server users <file> add <login>)"), challenge=False)
    guard: SignInGuard = request.app.state.sign_in_guard
    login = details.email.strip().lower()
    guard.attempt(login, time.time())  # counted as a failure until the password matches
    token = users.sign_in(login, details.password)
    if token is None:
        _log_sign_in(request, "signin-failed", login, 401)
        raise Unauthorised("Wrong email or password.")
    guard.success(login)
    _log_sign_in(request, "signin", login, 200)
    return {"user": login, "token": token}


def _log_sign_in(request: Request, event: str, login: str, status: int) -> None:
    access_log: AccessLog | None = request.app.state.access_log
    if access_log is not None:
        access_log.record_request(event, user=login, headers=request.headers,
                                  peer=request.client.host if request.client else None, path=request.url.path,
                                  status=status)


# --- development tokens (until the hub exists) ---------------------------------------------------------------------

def keygen(folder: Path) -> None:
    """Writes private.pem (owner-only) and public.pem, an Ed25519 pair, into `folder`; never overwrites."""
    private_path, public_path = folder / "private.pem", folder / "public.pem"
    existing = [str(path) for path in (private_path, public_path) if path.exists()]
    if existing:
        raise SystemExit(f"{', '.join(existing)} already exist(s); choose another --out folder")
    folder.mkdir(parents=True, exist_ok=True)
    private = Ed25519PrivateKey.generate()
    private_path.touch(mode=0o600)
    private_path.write_bytes(private.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
    public_path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,
                                                              serialization.PublicFormat.SubjectPublicKeyInfo))


def issue(key_path: Path, sub: str, licences: list[str], ttl_s: int) -> str:
    if not 1 <= ttl_s <= MAX_TOKEN_SECONDS:
        raise SystemExit(f"--ttl-s {ttl_s}: a token lasts from 1 to {MAX_TOKEN_SECONDS} seconds")
    if not sub:
        raise SystemExit("--sub must not be empty")
    private = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    if not isinstance(private, Ed25519PrivateKey):
        raise SystemExit(f"{key_path} is a {type(private).__name__}, not an Ed25519 private key; make one with "
                         "jeff-server token keygen")
    now = int(time.time())
    return jwt.encode({"sub": sub, "licences": licences, "iat": now, "exp": now + ttl_s}, private, algorithm="EdDSA")
