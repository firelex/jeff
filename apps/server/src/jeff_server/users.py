"""Console sign-in, ported unchanged in behaviour from jeff-server's ui_users: the JSON file named by JEFF_UI_USERS
holds {"secret": ..., "users": {login: scrypt password hash}}. A sign-in returns a session token signed with the
secret, valid for 30 days; the API accepts it like the API key. A new secret (`sign-out-all`) ends every session, and
removing a login ends that login's sessions. Repeated failed sign-ins lock that login, and then everyone, for a while.

    jeff-server users ~/.jeff/ui-users.json add menlo@scissero.com    # prints a new random password once
    jeff-server users ~/.jeff/ui-users.json remove menlo@scissero.com
    jeff-server users ~/.jeff/ui-users.json list
    jeff-server users ~/.jeff/ui-users.json sign-out-all               # new secret: every session token stops working

Differences from ui_users, none in behaviour: the file's path comes from ServerSettings.users_file (JEFF_UI_USERS)
rather than being read from the environment on every check; a malformed file or password hash is a ContractError
naming it (ui_users raised ValueError); the server reads the file once at startup, so a missing or malformed file
stops it starting; the failed sign-in counts live in a SignInGuard per app (jeff-server kept them in a module dict), which counts
an attempt as a failure under its lock before the password is checked, so parallel guesses cannot exceed the limit."""

import argparse
import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

from jeff.errors import ContractError, RateLimited

N, R, P = 2**14, 8, 1  # scrypt cost: ~16 MB and a few tens of milliseconds per check
SESSION_SECONDS = 30 * 24 * 3600  # a sign-in lasts 30 days

# Failed sign-ins in the last LOCKOUT_SECONDS, per login and in total. A login with LOGIN_FAILURES failures, or a
# server with TOTAL_FAILURES across all logins, refuses further attempts until the oldest failure is that old.
LOCKOUT_SECONDS, LOGIN_FAILURES, TOTAL_FAILURES = 15 * 60, 5, 30


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=N, r=R, p=P)
    return f"scrypt${N}${R}${P}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    parts = stored.split("$")
    if len(parts) != 6 or parts[0] != "scrypt":
        raise ContractError(f"Unknown password hash {parts[0]!r}: expected scrypt$N$r$p$salt$digest, as "
                            "jeff-server users writes it")
    _, n, r, p, salt, digest = parts
    actual = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p))
    return hmac.compare_digest(actual, bytes.fromhex(digest))


def read_file(path: Path) -> tuple[str, dict[str, str]]:
    """The signing secret and the login -> password hash map."""
    try:
        content = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ContractError(f"{path} is not valid JSON ({error}): make it with jeff-server users") from error
    secret, users = (content.get("secret"), content.get("users")) if isinstance(content, dict) else (None, None)
    if not isinstance(secret, str) or len(secret) < 32 or not isinstance(users, dict) or not all(
            isinstance(k, str) and isinstance(v, str) for k, v in users.items()):
        raise ContractError(f"{path} must be {{\"secret\": <at least 32 characters>, \"users\": {{login: password "
                            "hash}}: make it with jeff-server users")
    return secret, users


def write_file(path: Path, secret: str, users: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"secret": secret, "users": users}, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)


def _signature(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class UserStore:
    """The users file, reread whenever its modification time changes so tokens check fast and a new secret or a
    removed login takes effect at once. Reading it here, at startup, makes a missing or malformed file stop the
    server rather than fail its first sign-in."""

    def __init__(self, path: Path) -> None:
        if not path.is_file():
            raise ContractError(f"JEFF_UI_USERS={path} does not exist: make it with "
                                f"jeff-server users {path} add <login>")
        self.path = path
        self._lock = threading.Lock()
        self._cache: tuple[float, str, dict[str, str]] | None = None
        self._current()

    def _current(self) -> tuple[str, dict[str, str]]:
        with self._lock:
            modified = self.path.stat().st_mtime
            if self._cache is None or self._cache[0] != modified:
                self._cache = (modified, *read_file(self.path))
            return self._cache[1], self._cache[2]

    def sign_in(self, login: str, password: str) -> str | None:
        """A session token when the login exists and the password matches, otherwise None."""
        secret, users = self._current()
        login = login.strip().lower()
        stored = users.get(login)
        if stored is None:
            hash_password(password)  # same work as a real check, so a wrong login takes as long as a wrong password
            return None
        if not verify_password(password, stored):
            return None
        content = {"user": login, "expires": int(time.time()) + SESSION_SECONDS}
        payload = base64.urlsafe_b64encode(json.dumps(content).encode()).decode()
        return f"{payload}.{_signature(secret, payload)}"

    def session_user(self, token: str) -> str | None:
        """The login a session token belongs to, or None when it is forged, expired, or its login has been
        removed."""
        session = self.session(token)
        return session[0] if session is not None else None

    def session(self, token: str) -> tuple[str, float] | None:
        """The login a session token belongs to and when the session ends (seconds since the epoch), or None when
        it is forged, expired, or its login has been removed."""
        secret, users = self._current()
        payload, _, signature = token.partition(".")
        # compared as bytes: a header can carry non-ASCII text, which compare_digest refuses as str
        if not signature or not hmac.compare_digest(signature.encode(), _signature(secret, payload).encode()):
            return None
        content = json.loads(base64.urlsafe_b64decode(payload))
        if content["expires"] < time.time() or content["user"] not in users:
            return None
        return str(content["user"]), float(content["expires"])


class SignInGuard:
    """Counts failed sign-ins per login over the last LOCKOUT_SECONDS; thread-safe (sign-ins run in the
    threadpool)."""

    def __init__(self) -> None:
        self.failed: dict[str, list[float]] = {}
        self._lock = threading.Lock()

    def attempt(self, login: str, now: float) -> None:
        """Counts a sign-in attempt for `login` as a failure before its password is checked, or raises RateLimited,
        with how long to wait, when this login or the whole server already has too many recent failures. Checking
        and counting under one lock means parallel guesses cannot get more password checks than the limit; a
        successful sign-in then clears the login's failures (success)."""
        with self._lock:
            for name in list(self.failed):
                self.failed[name] = [moment for moment in self.failed[name] if now - moment < LOCKOUT_SECONDS]
                if not self.failed[name]:
                    del self.failed[name]
            mine = self.failed.get(login, [])
            every = sorted(moment for moments in self.failed.values() for moment in moments)
            for moments, limit in ((mine, LOGIN_FAILURES), (every, TOTAL_FAILURES)):
                if len(moments) >= limit:
                    wait = int(LOCKOUT_SECONDS - (now - moments[-limit])) + 1
                    raise RateLimited(f"Too many failed sign-ins. Try again in {(wait + 59) // 60} minutes.",
                                      retry_after_s=wait)
            self.failed.setdefault(login, []).append(now)

    def success(self, login: str) -> None:
        with self._lock:
            self.failed.pop(login, None)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("file", type=Path, help="the users file; point jeff-server at it with JEFF_UI_USERS")
    commands = parser.add_subparsers(dest="users_command", required=True)
    add = commands.add_parser("add", help="add a login, or give an existing one a new password; prints the password")
    add.add_argument("login")
    remove = commands.add_parser("remove", help="remove a login")
    remove.add_argument("login")
    commands.add_parser("list", help="list the logins")
    commands.add_parser("sign-out-all", help="new signing secret: every session token stops working")


def run(args: argparse.Namespace) -> None:
    path: Path = args.file.expanduser()
    secret, users = read_file(path) if path.exists() else (secrets.token_hex(32), {})
    if args.users_command == "list":
        print("\n".join(sorted(users)))
        return
    if args.users_command == "sign-out-all":
        write_file(path, secrets.token_hex(32), users)
        return
    login = args.login.strip().lower()
    if args.users_command == "add":
        password = secrets.token_urlsafe(12)
        users[login] = hash_password(password)
        write_file(path, secret, users)
        print(f"{login}\t{password}")
    else:
        if login not in users:
            raise SystemExit(f"{login} is not in {path}")
        del users[login]
        write_file(path, secret, users)
