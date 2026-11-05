"""The minute loop: the key list is fetched and usage reported as the server starts and then every tick, a failing
tick is logged and the next one runs, and shutdown sends a last report. Fix round 1 (review): shutdown order
(channels finish, then the engines close, then accounts mode's loop stops, its last report is sent, and it closes),
a cancelled loop awaited rather than left to overlap the last report, and a startup failure still closing the
accounts."""

import json
import logging
import threading
import time
from pathlib import Path

import anyio
import httpx
import pytest
from fastapi.testclient import TestClient

from jeff import contract
from jeff_server import app as app_module
from jeff_server.accounts import run_every_minute
from jeff_server.usage import UsageBook

from accounts_testing import KEY, OWNER_KEY, SPAM_CALL, Hub, accounts_app, bearer
from channel_testing import start
from test_channel import URGENCY, GatedEngine, wait_until


def test_the_server_fetches_the_list_as_it_starts(tmp_path: Path) -> None:
    hub = Hub()
    with TestClient(accounts_app(tmp_path, hub)) as http:
        for _ in range(50):  # the first tick runs on a worker thread right after start
            if http.post("/v1/decide", json={"calls": [SPAM_CALL]}, headers=bearer(KEY)).status_code == 200:
                break
            time.sleep(0.02)
        else:
            pytest.fail("the key list was never fetched")


def test_shutdown_sends_a_last_valid_report_with_the_totals(tmp_path: Path) -> None:
    hub = Hub()
    app = accounts_app(tmp_path, hub)
    with TestClient(app) as http:
        app.state.accounts.refresh()
        assert http.post("/v1/decide", json={"calls": [SPAM_CALL]}, headers=bearer(KEY)).status_code == 200
    last = hub.reports[-1]
    assert contract.is_valid("accounts_usage", last)
    assert last["accounts"][0]["account"] == "acct1" and last["accounts"][0]["input_tokens"] > 0
    assert len(last["keys"]) == 1


def test_a_failing_tick_is_logged_and_the_next_one_still_runs(tmp_path: Path,
                                                              caplog: pytest.LogCaptureFixture) -> None:
    hub = Hub()
    hub.status = 500
    hub.report_status = 503
    app = accounts_app(tmp_path, hub)
    calls_before = len(hub.authorizations)

    async def three_ticks() -> None:
        with anyio.move_on_after(0.35):
            await run_every_minute(app.state.accounts, 0.1, anyio.CapacityLimiter(1))

    with caplog.at_level(logging.ERROR, logger="jeff_server"):
        anyio.run(three_ticks)
    assert len(hub.authorizations) - calls_before >= 6  # at least three ticks, each a fetch and a report
    assert any("answered 500" in record.getMessage() for record in caplog.records)
    assert any("answered 503" in record.getMessage() for record in caplog.records)


def test_a_channel_call_in_flight_at_shutdown_is_counted_before_the_book_closes(
        tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """Important 1 (review): shutdown must finish the channel's running calls, and let their tokens be counted,
    before accounts mode's usage book closes and its last report is sent -- never the other way around. A call
    still waiting on the engine when shutdown starts (its gate opens only once the TestClient's `with` block has
    already begun closing) must still have its tokens in the usage file and in the last report, with no error
    logged (the old order closed the book first, so the call's own jeff_server.usage.UsageBook.add raised writing
    to a closed file, and those tokens were lost)."""
    engine = GatedEngine()
    hub = Hub()
    app = accounts_app(tmp_path, hub, engine=engine)
    timer = threading.Timer(0.5, engine.gate.set)
    try:
        with caplog.at_level(logging.ERROR, logger="jeff_server"):
            with TestClient(app) as http:
                app.state.accounts.refresh()
                with http.websocket_connect("/v1/channel") as ws:
                    channel = start(ws, KEY)
                    handle = channel.result({"op": "bind", "id": 2, **URGENCY})["handle"]
                    channel.send({"op": "call", "id": 3, "handle": handle, "input": "x"})
                    wait_until(lambda: engine.waiting == 1)
                # The websocket is closed, but its call's task keeps running (jeff_server.app._finish_channels)
                # until the engine answers; the gate only opens once shutdown, triggered by the `with` block below
                # ending, is already under way.
                timer.start()
        assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    finally:
        engine.gate.set()  # in case the test failed before the timer fired, so no thread is left waiting on it
    entries = [json.loads(line) for line in (tmp_path / "usage.jsonl").read_text().splitlines()]
    assert any(entry["account"] == "acct1" and entry["input_tokens"] > 0 for entry in entries)
    last = hub.reports[-1]
    assert last["accounts"] and last["accounts"][0]["account"] == "acct1" and last["accounts"][0]["input_tokens"] > 0


class SlowFirstReport(Hub):
    """The hub's internal API, whose first POST (the loop's first report) is slow, so it is still on the wire
    when shutdown starts."""

    def __init__(self) -> None:
        super().__init__()
        self.n = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            self.n += 1
            if self.n == 1:
                time.sleep(0.5)
        return super().handler(request)


def test_a_slow_report_in_flight_at_shutdown_never_races_the_last_one(tmp_path: Path,
                                                                      caplog: pytest.LogCaptureFixture) -> None:
    """Important 2 (review): after cancelling the loop, shutdown awaits it (anyio's to_thread.run_sync does not
    abandon a step already running on its worker thread), so a report already on the wire when shutdown starts
    always lands before shutdown's own last report -- never after it, and never concurrently with it (which would
    risk a false 'server bug' log from touching the closed book or client, and an out-of-order report at the hub)."""
    hub = SlowFirstReport()
    app = accounts_app(tmp_path, hub)
    with caplog.at_level(logging.ERROR, logger="jeff_server"):
        with TestClient(app) as http:
            # n is incremented before the sleep, so this catches the loop's first tick while its report is still
            # on the wire (with an empty snapshot), rather than guessing how long that takes under load.
            wait_until(lambda: hub.n >= 1)
            app.state.accounts.refresh()
            assert http.post("/v1/decide", json={"calls": [SPAM_CALL]}, headers=bearer(KEY)).status_code == 200
        # TestClient.__exit__ triggers shutdown while that first report is still in flight.
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
    assert len(hub.reports) == 2  # the stale tick's (empty) report, then shutdown's own (complete) one
    last = hub.reports[-1]
    assert last["accounts"] and last["accounts"][0]["account"] == "acct1" and last["accounts"][0]["input_tokens"] > 0


def test_a_startup_failure_still_closes_the_accounts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Important 4 (review): when something started after the accounts object is built fails (here, load_graphs,
    which runs after the usage book is already open), the usage book and the hub client are still closed, so a
    server that failed to start never leaves the usage file's lock held for the next attempt."""
    def boom(_: Path | None) -> None:
        raise RuntimeError("graphs broken")
    monkeypatch.setattr(app_module, "load_graphs", boom)
    app = accounts_app(tmp_path, Hub())
    with pytest.raises(RuntimeError, match="graphs broken"), TestClient(app):
        pass
    accounts = app.state.accounts
    assert accounts.book._file.closed
    assert accounts.http.is_closed
    # The usage file's lock was given back: a fresh UsageBook on the same file can open it.
    UsageBook(accounts.settings.usage_file).close()


def test_a_loop_bug_does_not_tear_down_the_serving_app(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Important A (review): a bug in run_every_minute itself (not this shutdown's own cancellation) must not
    reach the task group that also holds the host task (suspended at _serve()'s yield, serving requests) --
    a task group cancels its whole cancel_scope, host task included, the moment any child task raises. That would
    finish the channels, close the engines and close the book and the client while the ASGI server keeps accepting
    and serving requests on this same, now half-closed app. The loop must instead die alone (already logged by
    run_every_minute's own log.exception): the server keeps serving normally -- the book and the client stay open,
    and an owner-key request still works -- until it is actually asked to stop, and only then is the bug raised,
    bare, never wrapped in a BaseExceptionGroup."""
    async def crashing_loop(accounts: object, tick_s: float, limiter: object) -> None:
        await anyio.sleep(0.05)
        raise RuntimeError("a bug in the loop")
    monkeypatch.setattr(app_module, "run_every_minute", crashing_loop)
    app = accounts_app(tmp_path, Hub())
    with pytest.raises(RuntimeError, match="a bug in the loop"):
        with TestClient(app) as http:
            time.sleep(0.2)  # the loop has crashed well before this
            assert not app.state.accounts.book._file.closed
            assert not app.state.accounts.http.is_closed
            assert http.get("/ui/me", headers=bearer(OWNER_KEY)).status_code == 200
