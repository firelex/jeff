"""A Jeff server for the console's Playwright smoke test (apps/server/ui/e2e/smoke.spec.ts).

Starts the real FastAPI app (jeff_server.app.create_app), serving the committed console build at /ui, in front of a
real engine process (jeff_runtime.testing.Running) over a fake model (jeff.testing.FakeEngine) -- real enough that
/health, /v1/adapters and PUT /v1/adapters/served behave exactly as they do with jeff_runtime.LocalEngine, but with
no model weights loaded. Picks its own free port and writes a console sign-in file (jeff_server.users) with one
admin login and one ordinary ("member") login. The factory is off (JEFF_FACTORY=off) with a factory folder holding one
finished recorded run (tests/fixtures/console-factory, copied so the test never writes into the repository), so the
factory page shows its replay and the "building is switched off" message.

Prints one line of JSON to stdout before serving -- {"url", "usersFile", "admin": {"email", "password"}, "member":
{"email", "password"}} -- which apps/server/ui/playwright.config.ts's global setup reads to point the browser at
this server and sign in. Stops on SIGTERM: uvicorn installs its own SIGINT/SIGTERM handlers when run this way (see
uvicorn.Server.serve's capture_signals()), so no signal handling of this script's own is needed; the `finally`
block below then closes the engine connection and the engine process, and removes the temporary users/access-log
files.

Run directly, not through pytest:

    uv run python apps/server/tests/console_server.py
"""

import json
import secrets
import shutil
import tempfile
from pathlib import Path
from typing import Any

import uvicorn

from jeff.socket_engine import SocketEngine
from jeff.testing import FakeEngine
from jeff_runtime.testing import Running

from jeff_server import users
from jeff_server.app import create_app
from jeff_server.cli import websocket_options
from jeff_server.factory.config import FactoryConfig
from jeff_server.router import EngineRouter
from jeff_server.settings import ServerSettings

from server_testing import free_port

ROOT = Path(__file__).resolve().parents[3]
CATALOGUE = ROOT / "packages" / "python" / "tests" / "fixtures" / "catalogue"
ADAPTERS = {"jeff.safety.spam", "jeff.support.triage"}
AVAILABLE = {adapter: Path("/weights") / adapter for adapter in ADAPTERS}  # dummy paths: FakeEngine never reads them
FACTORY_FIXTURE = Path(__file__).resolve().parent / "fixtures" / "console-factory"


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    """The first option wins, so every call the smoke test runs gets a confident decision, never an abstain."""
    question = row["question"]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def main() -> None:
    workdir = Path(tempfile.mkdtemp(prefix="jeff-console-e2e-"))
    running = Running(FakeEngine(answers, set(ADAPTERS)), fixed=AVAILABLE)
    socket_engine = SocketEngine(running.path, timeout_s=10.0)
    try:
        admin_email, admin_password = "owner@example.com", secrets.token_urlsafe(12)
        member_email, member_password = "member@example.com", secrets.token_urlsafe(12)
        users_file = workdir / "users.json"
        users.write_file(users_file, secrets.token_hex(32), {
            admin_email: users.hash_password(admin_password),
            member_email: users.hash_password(member_password),
        })

        factory_dir = workdir / "factory"
        shutil.copytree(FACTORY_FIXTURE, factory_dir)
        settings = ServerSettings(
            engine_sockets=(running.path,), model_name="jeff-console-e2e", catalogue=CATALOGUE,
            users_file=users_file, admins=frozenset({admin_email}), access_log=workdir / "access.jsonl",
            factory=FactoryConfig(mode="off", root=factory_dir))
        app = create_app(settings, engine=EngineRouter([socket_engine]))

        port = free_port()
        credentials = {
            "url": f"http://127.0.0.1:{port}",
            "usersFile": str(users_file),
            "admin": {"email": admin_email, "password": admin_password},
            "member": {"email": member_email, "password": member_password},
        }
        print(json.dumps(credentials), flush=True)

        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning", **websocket_options())
    finally:
        socket_engine.close()
        running.close()
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
