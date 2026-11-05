"""Helpers for the accounts-mode tests: a stand-in for the hub's internal API (httpx.MockTransport), key lists, and
an app in accounts mode over a FakeEngine."""

import hashlib
import json
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from jeff.engine import Engine
from jeff.testing import FakeEngine

from jeff_server.app import create_app
from jeff_server.settings import AccountsSettings, ServerSettings

ROOT = Path(__file__).resolve().parents[3]
CATALOGUE = ROOT / "packages" / "python" / "tests" / "fixtures" / "catalogue"
HUB_URL = "https://hub.test"
INTERNAL_API = f"{HUB_URL}/api/internal/v1"
SECRET = "a-test-secret-not-a-real-one"
OWNER_KEY = "the-owners-key"
KEY = "jeff_live_" + "a" * 52
SECOND_KEY = "jeff_live_" + "d" * 52
OTHER_ACCOUNTS_KEY = "jeff_live_" + "b" * 52
PLAYGROUND_KEY = "jeff_live_" + "c" * 52
NOV, DEC = "2026-11-03T10:00:00Z", "2026-12-03T10:00:00Z"
SPAM_CALL = {"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "email"}}


def digest(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_list(allowance: int = 1_000_000, period_start: str = NOV, period_end: str = DEC,
             runs_per_second: float = 50.0) -> dict[str, Any]:
    """acct1 holds KEY and SECOND_KEY, acct2 holds OTHER_ACCOUNTS_KEY; the playground has PLAYGROUND_KEY."""
    return {"accounts": [{"account": "acct1", "allowance": allowance, "period_start": period_start,
                          "period_end": period_end},
                         {"account": "acct2", "allowance": allowance, "period_start": period_start,
                          "period_end": period_end}],
            "keys": [{"hash": digest(KEY), "account": "acct1"}, {"hash": digest(SECOND_KEY), "account": "acct1"},
                     {"hash": digest(OTHER_ACCOUNTS_KEY), "account": "acct2"}],
            "playground": {"hash": digest(PLAYGROUND_KEY), "runs_per_second": runs_per_second,
                           "period_start": "2026-11-01T00:00:00Z", "period_end": "2026-12-01T00:00:00Z"}}


class Hub:
    """The hub's internal API: GET /api/internal/v1/keys answers `keys` (a dict as JSON, or bytes as they are) with
    `status` (and `location`, when set, as its Location header); POST /api/internal/v1/usage records each report.
    Every request's URL, Authorization header and x-vercel-protection-bypass header are kept."""

    def __init__(self) -> None:
        self.keys: dict[str, Any] | bytes = key_list()
        self.status = 200
        self.location: str | None = None
        self.report_status = 204
        self.reports: list[dict[str, Any]] = []
        self.urls: list[str] = []
        self.authorizations: list[str | None] = []
        self.bypass_headers: list[str | None] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        self.authorizations.append(request.headers.get("authorization"))
        self.bypass_headers.append(request.headers.get("x-vercel-protection-bypass"))
        if request.method == "GET" and request.url.path == "/api/internal/v1/keys":
            headers = {"Location": self.location} if self.location is not None else {}
            if isinstance(self.keys, bytes):
                return httpx.Response(self.status, content=self.keys, headers=headers)
            return httpx.Response(self.status, json=self.keys, headers=headers)
        if request.method == "POST" and request.url.path == "/api/internal/v1/usage":
            self.reports.append(json.loads(request.content))
            return httpx.Response(self.report_status)
        return httpx.Response(404, json={"error": "no such route"})


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    question = row["question"]
    if question["type"] == "score":
        return [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def fake_engine() -> FakeEngine:
    return FakeEngine(answers, {"jeff.safety.spam", "jeff.support.triage"})


def accounts_app(tmp_path: Path, hub: Hub, *, rate_limit: tuple[float, int] = (100.0, 8),
                 catalogue: Path = CATALOGUE, engine: Engine | None = None,
                 bypass: str | None = None) -> FastAPI:
    token_file = tmp_path / "accounts-token"
    token_file.write_text(SECRET + "\n")
    token_file.chmod(0o600)
    bypass_file: Path | None = None
    if bypass is not None:
        bypass_file = tmp_path / "accounts-bypass"
        bypass_file.write_text(bypass + "\n")
        bypass_file.chmod(0o600)
    accounts = AccountsSettings(url=HUB_URL, token_file=token_file, token=SECRET, max_stale_s=300.0,
                                usage_file=tmp_path / "usage.jsonl", bypass_file=bypass_file, bypass=bypass)
    settings = ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3",
                              catalogue=catalogue, api_key=OWNER_KEY, rate_limit=rate_limit, accounts=accounts)
    app = create_app(settings, engine=engine if engine is not None else fake_engine(),
                     accounts_http=httpx.Client(transport=httpx.MockTransport(hub.handler)))
    app.state.test_hub = hub  # serving() below: so it can wait for the automatic loop's first tick
    return app


@contextmanager
def serving(app: FastAPI) -> Iterator[TestClient]:
    """The app running, with the key list fetched once (a manual refresh, so the list is in force even if the
    automatic loop's own first tick happens to run late). Also waits for that automatic first tick to report to
    the hub (jeff_server.accounts.run_every_minute, started by create_app in accounts mode) before yielding: once
    it has, the loop is asleep until the next minute and no longer touches Accounts.clock or the hub, so a test
    that patches accounts.clock or hub.status/hub.report_status right after this yields never races it."""
    with TestClient(app) as http:
        app.state.accounts.refresh()
        hub = app.state.test_hub
        deadline = time.monotonic() + 5.0
        while not hub.reports:
            if time.monotonic() > deadline:
                raise AssertionError("the automatic minute loop never reported to the hub")
            time.sleep(0.01)
        yield http


def bearer(value: str, **headers: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}", **headers}
