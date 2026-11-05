"""The usage book of accounts mode (jeff_server.accounts): each account's input tokens per billing period, in memory
and in JEFF_USAGE_FILE, a JSON Lines file.

Every change appends one line holding the new TOTAL for that account and period, {"account", "period_start",
"input_tokens"}, and flushes it; the last line for a pair is its total. On start the file is read and rewritten
(compacted) with one line per pair, keeping each account's KEPT_PERIODS latest periods (the current one and the one
before, which the hub shows). The book also keeps when each key was last used, in memory only: the hub keeps the last
time it was told.

A line that is not a usage entry stops the server naming the file and the line: a count is never dropped or guessed.
A last line with no newline is what a crash in the middle of a write leaves; it is refused too, with a message saying
so (the previous line for that account still holds its total before the interrupted request).

Thread-safe: one lock guards the totals, the file and the last-used times. One process only: the book holds an
exclusive lock (flock) on <file>.lock from before it reads the file until it is closed, and a second process given
the same file is refused at start, naming it (its compaction would replace the file under the first, whose later
counts would then be lost). The lock is on a file beside it because compaction replaces the usage file itself."""

import fcntl
import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import IO, Any

from jeff.errors import InvalidRequest

KEPT_PERIODS = 2
TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
ACCOUNT = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def utc_text(moment: datetime) -> str:
    """A moment as the internal API writes times: UTC, to the second, with a Z."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class UsageBook:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._process_lock = _lock_against_other_processes(path)
        try:
            totals = _read(path) if path.exists() else {}
            self._totals: dict[tuple[str, str], int] = _kept(totals)
            _rewrite(path, self._totals)
            self._file = path.open("a", encoding="utf-8")
        except BaseException:
            self._process_lock.close()  # the book was not opened: its lock is given back, and the error goes on
            raise
        self._last_used: dict[str, str] = {}

    def total(self, account: str, period_start: str) -> int:
        """Input tokens counted for this account in the period starting at period_start; 0 when none were (an account
        that has made no call this period has used nothing)."""
        with self._lock:
            return self._totals.get((account, period_start), 0)

    def add(self, account: str, period_start: str, tokens: int) -> int:
        if tokens < 0:
            raise ValueError(f"UsageBook.add was given a negative count {tokens} for {account}: a server bug")
        with self._lock:
            pair = (account, period_start)
            total = self._totals.get(pair, 0) + tokens
            # Written first: a write that fails (a full disk) raises with the total in memory as the file holds it.
            self._file.write(json.dumps({"account": account, "period_start": period_start,
                                         "input_tokens": total}) + "\n")
            self._file.flush()
            new_period = pair not in self._totals
            self._totals[pair] = total
            if new_period:
                self._totals = _kept(self._totals)
            return total

    def touch(self, key_hash: str, when: str) -> None:
        with self._lock:
            self._last_used[key_hash] = when

    def report(self) -> dict[str, Any]:
        """The usage report the hub is sent (contract schema accounts_usage): totals, sorted, never deltas."""
        with self._lock:
            return {"accounts": [{"account": account, "period_start": period, "input_tokens": total}
                                 for (account, period), total in sorted(self._totals.items())],
                    "keys": [{"hash": key_hash, "last_used": when}
                             for key_hash, when in sorted(self._last_used.items())]}

    def close(self) -> None:
        with self._lock:
            self._file.close()
            self._process_lock.close()  # closing the file gives the flock back


def _lock_against_other_processes(path: Path) -> IO[str]:
    lock = path.with_name(path.name + ".lock").open("a", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise InvalidRequest(f"JEFF_USAGE_FILE {path} is in use by another jeff-server process (it holds "
                             f"{path.name}.lock); give each server its own usage file, or stop the other one") from None
    return lock


def _kept(totals: dict[tuple[str, str], int]) -> dict[tuple[str, str], int]:
    """Each account's KEPT_PERIODS latest periods (period starts compare as text: they all have one form)."""
    periods: dict[str, list[str]] = {}
    for account, period in totals:
        periods.setdefault(account, []).append(period)
    keep = {(account, period) for account, starts in periods.items() for period in sorted(starts)[-KEPT_PERIODS:]}
    return {pair: total for pair, total in totals.items() if pair in keep}


def _read(path: Path) -> dict[tuple[str, str], int]:
    totals: dict[tuple[str, str], int] = {}
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(keepends=True), start=1):
        if not line.endswith("\n"):
            raise InvalidRequest(
                f"{path} line {number} is incomplete (it has no newline at its end): the server stopped while "
                "writing it. Look at the line, remove it, and start again; the line before it for the same account "
                "holds the total before the request that was being written")
        try:
            entry = json.loads(line)
        except json.JSONDecodeError as error:
            raise InvalidRequest(f"{path} line {number} is not JSON ({error}); fix or remove the line") from error
        if not _is_entry(entry):
            raise InvalidRequest(f"{path} line {number} is not a usage entry {{\"account\", \"period_start\" "
                                 f"(YYYY-MM-DDTHH:MM:SSZ), \"input_tokens\" (a whole number, 0 or more)}}: "
                                 f"{line.strip()[:200]}")
        totals[(entry["account"], entry["period_start"])] = entry["input_tokens"]
    return totals


def _is_entry(entry: object) -> bool:
    if not isinstance(entry, dict) or set(entry) != {"account", "period_start", "input_tokens"}:
        return False
    account, period, tokens = entry["account"], entry["period_start"], entry["input_tokens"]
    return (isinstance(account, str) and ACCOUNT.match(account) is not None
            and isinstance(period, str) and TIME.match(period) is not None
            and isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0)


def _rewrite(path: Path, totals: dict[tuple[str, str], int]) -> None:
    """The compacted file, written beside it and moved into place, so a crash here leaves the old file whole."""
    temporary = path.with_name(path.name + ".compacting")
    with temporary.open("w", encoding="utf-8") as file:
        for (account, period), total in sorted(totals.items()):
            file.write(json.dumps({"account": account, "period_start": period, "input_tokens": total}) + "\n")
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)
