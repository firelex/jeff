"""A Jeff server for remote-mode tests: jeff_server.create_app over a FakeEngine, served by a real uvicorn on a free
port in this process, and the helpers to reach it (a RemoteJeff, a request counter). Mode is how a test reaches it: over
HTTP (channel=False) or over the channel (channel=True); the `mode` fixture (conftest.py) runs a test both ways."""

import hashlib
import json
import shutil
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import jwt
import uvicorn
import yaml
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jeff.catalogue import Catalogue
from jeff.channel_client import ChannelTransport
from jeff.client import Jeff
from jeff.decision import Decision
from jeff.protocol import HEADER, decode
from jeff.remote import RemoteJeff
from jeff.testing import FakeEngine

from jeff_server.app import create_app
from jeff_server.cli import websocket_options
from jeff_server.settings import ServerSettings

FIXTURES = Path(__file__).resolve().parent / "fixtures"
CATALOGUE = FIXTURES / "catalogue"
ROUTERS = FIXTURES / "routers"
GRAPH = FIXTURES / "graphs" / "support_intake.yaml"
CONTEXT = {"company": "A furniture shop.", "channel": "email"}
TEAMS = {"deliveries": "Deliveries", "refunds": "Refunds"}
PARAMS: dict[str, Any] = {"company": "A furniture shop.", "channel": "email", "teams": TEAMS}
FIXTURE_ADAPTERS = {"jeff.safety.spam", "jeff.support.triage"}
ROUTED = {"jeff.core.router", "jeff.support.router", "jeff.support.triage", "jeff.safety.spam"}
UNSURE = [0.0, 0.5, 0.5, 0.0, 0.0]  # below triage.urgency's calibration threshold 0.6
TIMEOUT_S = 10.0

CALLS: list[dict[str, Any]] = [
    {"function": "triage.route", "input": "My wardrobe arrived broken", "context": CONTEXT, "labels": TEAMS},
    {"function": "jeff.support.triage.urgency", "input": "It is on fire", "context": CONTEXT},
    {"function": "spam.message_type", "input": "WIN NOW", "context": {"channel": "email"}},
    {"function": "triage.needs_human", "input": "Call me back", "context": CONTEXT},
]


def answers(adapter: str, row: dict[str, Any]) -> list[float]:
    """The first option wins, except for a score question: confident (level 2 at 0.9) unless the input contains
    "unsure"."""
    question = row["question"]
    if question["type"] == "score":
        return UNSURE if "unsure" in json.dumps(row["state"]) else [0.0, 0.0, 0.9, 0.1, 0.0]
    count = len(question["criteria"]) if "criteria" in question else 2
    return [1.0] + [0.0] * (count - 1)


def fake(installed: set[str] = FIXTURE_ADAPTERS,
         answer: Callable[[str, dict[str, Any]], list[float]] = answers) -> FakeEngine:
    return FakeEngine(answer, set(installed))


def settings(catalogue: Path = CATALOGUE, **options: Any) -> ServerSettings:
    return ServerSettings(engine_sockets=(Path("/tmp/engine-0.sock"),), model_name="jeff-v1.3", catalogue=catalogue,
                          **options)


def local(engine: FakeEngine, catalogue: Path = CATALOGUE) -> Jeff:
    return Jeff(engine, Catalogue.from_dir(catalogue))


def hub_keys(tmp_path: Path) -> tuple[Ed25519PrivateKey, Path]:
    """A hub key pair: the private key (to sign tokens) and the public key's PEM file (JEFF_HUB_PUBLIC_KEY). The
    `keys` fixture (conftest.py) makes one per test."""
    private = Ed25519PrivateKey.generate()
    path = tmp_path / "hub.pub.pem"
    path.write_bytes(private.public_key().public_bytes(serialization.Encoding.PEM,
                                                       serialization.PublicFormat.SubjectPublicKeyInfo))
    return private, path


def hub_token(private: Ed25519PrivateKey, licences: list[str] | None = None, ttl_s: int = 600) -> str:
    """A hub-signed token for alice holding `licences`, lasting ttl_s seconds."""
    now = int(time.time())
    return jwt.encode({"sub": "alice", "licences": licences if licences is not None else [], "iat": now,
                       "exp": now + ttl_s}, private, algorithm="EdDSA")


def proprietary(tmp_path: Path, adapter: str) -> Path:
    """A copy of the fixture catalogue in which `adapter` is proprietary."""
    catalogue = tmp_path / "proprietary"
    shutil.copytree(CATALOGUE, catalogue)
    path = catalogue / adapter / "manifest.yaml"
    manifest = yaml.safe_load(path.read_text())
    manifest["licence"] = {"kind": "proprietary", "adapter": "Commercial"}
    manifest["weights"] = {"proprietary": {"repo": "jeffhub/private", "path": adapter}}
    path.write_text(yaml.safe_dump(manifest))
    return catalogue


def free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@contextmanager
def serving(app: Any) -> Iterator[str]:
    """`app` served by uvicorn on a free port in a thread, with the WebSocket options `jeff-server serve` uses;
    stopped (and checked stopped) on exit."""
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning",
                                           **websocket_options()))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            raise RuntimeError(f"uvicorn did not start on port {port}")
        time.sleep(0.01)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "uvicorn did not stop"


@contextmanager
def jeff_server(engine: FakeEngine, catalogue: Path = CATALOGUE, **options: Any) -> Iterator[str]:
    with serving(create_app(settings(catalogue, **options), engine=engine)) as url:
        yield url


@contextmanager
def remote(url: str, token: Any = None, catalogue: Path = CATALOGUE, channel: bool = False) -> Iterator[RemoteJeff]:
    with Jeff.remote(url, token, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(catalogue),
                     channel=channel) as client:
        yield client


def channel_frames(transport: ChannelTransport, record: Callable[[dict[str, Any]], None]) -> None:
    """Calls `record` with every frame (decoded) the transport sends, before sending it."""
    send = transport._send

    def recording(frame: bytes) -> None:
        record(decode(frame[HEADER.size:]))
        send(frame)

    transport._send = recording  # type: ignore[method-assign]


def http_client(client: RemoteJeff) -> httpx.Client:
    """The httpx client a RemoteJeff's HTTP requests go through (over the channel: GET /v1/functions only)."""
    transport = client.transport
    rest = transport._rest if isinstance(transport, ChannelTransport) else transport
    return rest.client  # type: ignore[attr-defined, no-any-return]


class Requests:
    """Counts the requests a RemoteJeff sends: each HTTP request as "<method> <path>" (an httpx event hook), each
    channel frame as its op."""

    def __init__(self, client: RemoteJeff) -> None:
        self.sent: list[str] = []
        http_client(client).event_hooks["request"].append(self._record)
        if isinstance(client.transport, ChannelTransport):
            channel_frames(client.transport, lambda frame: self.sent.append(frame["op"]))

    def _record(self, request: httpx.Request) -> None:
        self.sent.append(f"{request.method} {request.url.path}")

    def take(self) -> list[str]:
        sent, self.sent = self.sent, []
        return sent


class Bodies:
    """The bodies of the decide requests a RemoteJeff sends: each POST /v1/decide body, or each decide, dispatch and
    graph frame without its op and id (exactly a /v1/decide body)."""

    def __init__(self, client: RemoteJeff) -> None:
        self.sent: list[dict[str, Any]] = []
        if isinstance(client.transport, ChannelTransport):
            channel_frames(client.transport, self._frame)
        else:
            http_client(client).event_hooks["request"].append(self._request)

    def _request(self, request: httpx.Request) -> None:
        if request.url.path == "/v1/decide":
            self.sent.append(json.loads(request.content))

    def _frame(self, frame: dict[str, Any]) -> None:
        if frame["op"] in ("decide", "dispatch", "graph"):
            self.sent.append({key: value for key, value in frame.items() if key not in ("op", "id")})


ONE_DECIDE = ["POST /v1/decide"]


@dataclass(frozen=True)
class Mode:
    """How a test reaches the server: over HTTP or over the channel."""
    channel: bool

    def remote(self, url: str, token: Any = None, catalogue: Path = CATALOGUE) -> Any:
        return remote(url, token, catalogue, self.channel)

    def connect(self, url: str, token: Any = None, catalogue: Path = CATALOGUE) -> RemoteJeff:
        return Jeff.remote(url, token, timeout_s=TIMEOUT_S, catalogue=Catalogue.from_dir(catalogue),
                           channel=self.channel)

    def sent(self, op: str) -> list[str]:
        """What Requests records for one request of channel operation `op`: that op over the channel; over HTTP one
        POST /v1/decide, or nothing for a bind (binding is local)."""
        if self.channel:
            return [op]
        return [] if op == "bind" else ONE_DECIDE

    @property
    def request_ids(self) -> bool:
        """Whether errors carry a request id (an HTTP response's x-request-id; channel replies have none)."""
        return not self.channel


def without_timing(decision: Decision) -> dict[str, Any]:
    data = decision.to_json()
    del data["timing"]
    return data


def hashed(adapter: str, row: dict[str, Any]) -> list[float]:
    """Probabilities that depend on the whole row, so a request that reached the engine differently would differ."""
    question = row["question"]
    count = len(question["criteria"]) if "criteria" in question else 2
    digest = hashlib.sha256(json.dumps([adapter, row], sort_keys=True).encode()).digest()
    weights = [1.0 + digest[index % len(digest)] for index in range(count)]
    return [weight / sum(weights) for weight in weights]  # summing to 1, as the engine's softmax does
