"""Jeff.remote(channel=True): bound functions are handles on the server's /v1/channel. A bound function sends one bind
and then only call frames; close() or garbage collection releases its handle; requests from many threads share one
connection; a callable token is refreshed before its exp; a string token expiring with calls pending fails each of
them with Unauthorised; the server going away fails every request with BackendUnavailable. The equivalence with local
mode and with channel=False is in test_remote.py and test_remote_graphs.py (the `mode` fixture).

The server runs in this process (jeff_server.create_app over a FakeEngine, served by a real uvicorn on a free port with
`jeff-server serve`'s WebSocket options)."""

import base64
import collections
import gc
import json
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from jeff import channel_client
from jeff.catalogue import Catalogue
from jeff.channel_client import ChannelTransport, channel_url, token_expiry
from jeff.client import Call, Jeff
from jeff.errors import (BackendUnavailable, ContractError, DeadlineExceeded, EngineError, InvalidRequest, JeffError,
                         LowConfidence, RateLimited, Unauthorised)
from jeff.escalation import escalate, function_step, llm
from jeff.protocol import HEADER, decode, encode
from jeff.remote import RemoteJeff
from jeff.testing import FakeEngine

from jeff_server import channel as channel_module
from jeff_server.app import create_app
from served import (CATALOGUE, CONTEXT, TIMEOUT_S, answers, channel_frames, fake, hub_token, local, serving, settings,
                    without_timing)


class Frames:
    """An ASGI wrapper that records the op of every frame a channel client sends, as the server receives it."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.ops: list[str] = []
        self.state = app.state

    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Any], send: Callable[[Any], Any]) -> None:
        if scope["type"] != "websocket":
            await self.app(scope, receive, send)
            return

        async def recording() -> Any:
            message = await receive()
            if message["type"] == "websocket.receive" and message.get("bytes"):
                self.ops.append(decode(message["bytes"][HEADER.size:])["op"])
            return message

        await self.app(scope, recording, send)

    def take(self) -> list[str]:
        ops, self.ops = self.ops, []
        return ops


@contextmanager
def channel_server(engine: FakeEngine | None = None, **options: Any) -> Iterator[tuple[str, Frames]]:
    frames = Frames(create_app(settings(**options), engine=engine if engine is not None else fake()))
    with serving(frames) as url:
        yield url, frames


def connect(url: str, token: Any = None, timeout_s: float = TIMEOUT_S) -> RemoteJeff:
    return Jeff.remote(url, token, timeout_s=timeout_s, catalogue=Catalogue.from_dir(CATALOGUE), channel=True)


def transport_of(client: RemoteJeff) -> ChannelTransport:
    transport = client.transport
    assert isinstance(transport, ChannelTransport)
    return transport


SPAM = Call("spam.message_type", "x", {"channel": "email"})


class Gate:
    """An engine answer function that waits until the test opens the gate, counting the calls waiting."""

    def __init__(self) -> None:
        self.open, self.waiting = threading.Event(), threading.Semaphore(0)

    def __call__(self, adapter: str, row: dict[str, Any]) -> list[float]:
        self.waiting.release()
        assert self.open.wait(20), "the test never opened the gate"
        return answers(adapter, row)

    def engine(self) -> FakeEngine:
        return FakeEngine(self, {"jeff.safety.spam", "jeff.support.triage"})


def in_threads(count: int, work: Callable[[int], Any]) -> tuple[list[threading.Thread], dict[int, Any]]:
    """Starts `count` threads running work(index); the dict fills with each one's result or JeffError."""
    results: dict[int, Any] = {}

    def run(index: int) -> None:
        try:
            results[index] = work(index)
        except JeffError as error:
            results[index] = error

    threads = [threading.Thread(target=run, args=(index,)) for index in range(count)]
    for thread in threads:
        thread.start()
    return threads, results


def joined(threads: list[threading.Thread], timeout: float = 8.0) -> None:
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(max(0.0, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads), "a request hung"


def handles(frames: Frames) -> list[int]:
    """How many handles each open channel connection holds."""
    return [len(connection.handles) for connection in frames.state.channels]


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


# --- URLs and tokens --------------------------------------------------------------------------------------------------

def test_the_channel_url_follows_the_server_url() -> None:
    assert channel_url("http://127.0.0.1:8790") == "ws://127.0.0.1:8790/v1/channel"
    assert channel_url("https://jeff.example/") == "wss://jeff.example/v1/channel"
    with pytest.raises(InvalidRequest, match="http:// or https://"):
        channel_url("ftp://jeff.example")


def test_a_tokens_expiry_is_read_without_verifying_it() -> None:
    signed = jwt.encode({"sub": "a", "exp": 1234567890}, "a secret of at least thirty-two bytes", algorithm="HS256")
    assert token_expiry(signed) == 1234567890.0
    assert token_expiry("an-api-key") is None and token_expiry("session.token") is None
    payload = base64.urlsafe_b64encode(json.dumps({"sub": "a"}).encode()).decode().rstrip("=")
    with pytest.raises(Unauthorised, match="exp"):
        token_expiry(f"e30.{payload}.sig")
    with pytest.raises(Unauthorised, match="base64"):
        token_expiry("a.!!!.c")


# --- handles ----------------------------------------------------------------------------------------------------------

def test_a_bound_function_sends_one_bind_then_only_call_frames() -> None:
    expected = local(fake()).function("triage.urgency", context=CONTEXT)
    with channel_server() as (url, frames), connect(url) as client:
        assert frames.take() == ["hello"]
        bound = client.function("triage.urgency", context=CONTEXT)
        got = [bound("It is on fire"), bound("unsure"), *bound.many(["x", "unsure"])]
        assert frames.take() == ["bind", "call", "call", "call_many"]
    assert got == [expected("It is on fire"), expected("unsure"), *expected.many(["x", "unsure"])]


def test_close_releases_the_handle() -> None:
    with channel_server() as (url, frames), connect(url) as client:
        bound = client.function("triage.urgency", context=CONTEXT)
        assert handles(frames) == [1]
        bound.close()
        assert frames.take()[-1] == "release" and handles(frames) == [0]
        with pytest.raises(InvalidRequest, match="closed"):
            bound("x")
        bound.close()  # already released: nothing more is sent
        with client.function("triage.urgency", context=CONTEXT) as managed:
            assert managed("x").value == 2
        assert frames.take() == ["bind", "call", "release"] and handles(frames) == [0]


def test_a_garbage_collected_bound_function_is_released_before_the_next_request() -> None:
    with channel_server() as (url, frames), connect(url) as client:
        client.function("triage.urgency", context=CONTEXT)
        gc.collect()
        assert frames.take()[-1] == "bind"
        client.decide([Call("spam.message_type", "WIN NOW", {"channel": "email"})])
        wait_until(lambda: handles(frames) == [0])
        assert frames.take() == ["release", "decide"]


def test_a_bind_answered_with_another_adapter_version_raises_contract_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """The server's adapter changed after connecting: the bind's reply names another version than the local
    catalogue's manifest, so the bound function would map labels by the wrong manifest. It raises the ContractError
    connecting gives, and the handle the server made is released before the next request."""
    with channel_server() as (url, frames), connect(url) as client:
        real = channel_module._bind

        def other_version(shared: Any, caller: Any, message: Any) -> Any:
            handle, result = real(shared, caller, message)
            return handle, {**result, "version": "9.9.9"}

        monkeypatch.setattr(channel_module, "_bind", other_version)
        local_version = client.catalogue.adapter("jeff.support.triage").version
        with pytest.raises(ContractError, match=rf"jeff.support.triage: the server serves version 9.9.9, this "
                                                rf"client's catalogue has {local_version}"):
            client.function("triage.urgency", context=CONTEXT)
        assert handles(frames) == [1]
        client.decide([SPAM])
        wait_until(lambda: handles(frames) == [0])


def test_an_error_the_channel_sends_carries_the_connections_id() -> None:
    """Channel errors have no request id; each carries the server connection's id as channel_id, the error that
    closes the channel too (raised by every later request)."""
    with channel_server() as (url, frames), connect(url) as client:
        [connection] = frames.state.channels
        with pytest.raises(LowConfidence) as refused:  # the server's answer to the bound "error" policy
            client.function("triage.urgency", context=CONTEXT, on_low_confidence="error")("unsure")
        assert refused.value.channel_id == connection.id  # type: ignore[attr-defined]
        assert getattr(refused.value, "request_id", None) is None
        # A second hello: the server closes the channel with an InvalidRequest failure carrying id 0.
        transport_of(client)._ws.send(encode({"op": "hello", "version": 2}))
        with pytest.raises(InvalidRequest) as closed:
            wait_until(lambda: transport_of(client).closed)
            client.decide([SPAM])
        assert closed.value.channel_id == connection.id  # type: ignore[attr-defined]


def test_the_bind_carries_the_policy_the_server_runs_and_an_sdk_policy_is_bound_as_return() -> None:
    with channel_server() as (url, frames), connect(url) as client:
        sent: list[dict[str, Any]] = []
        transport = client.transport
        assert isinstance(transport, ChannelTransport)
        channel_frames(transport, sent.append)
        client.function("triage.urgency", context=CONTEXT, on_low_confidence=escalate(function_step("triage.urgency")))
        asked: list[Any] = []

        def ask(question: Any) -> str:
            asked.append(question)
            return "3"

        bound = client.function("triage.urgency", context=CONTEXT, on_low_confidence=escalate(llm(ask)))
        decision = bound("unsure")
    assert [frame["on_low_confidence"] for frame in sent if frame["op"] == "bind"] == [
        {"escalate": ["jeff.support.triage.urgency"]}, "return"]
    assert all(frame["function"] == "jeff.support.triage.urgency" for frame in sent if frame["op"] == "bind")
    assert decision.decided_by == "llm" and decision.value == 3 and len(asked) == 1


def test_calls_from_eight_threads_share_the_channel_and_each_gets_its_own_answer() -> None:
    inputs = [f"message {index}" + (" unsure" if index % 3 == 0 else "") for index in range(8 * 20)]
    expected_bound = local(fake()).function("triage.urgency", context=CONTEXT)
    expected = {item: without_timing(expected_bound(item)) for item in inputs}
    got: dict[str, Any] = {}
    failures: list[BaseException] = []
    with channel_server() as (url, frames), connect(url) as client:
        bound = client.function("triage.urgency", context=CONTEXT)

        def work(chunk: list[str]) -> None:
            try:
                for item in chunk:
                    got[item] = without_timing(bound(item))
            except BaseException as error:  # noqa: BLE001 - reported by the assertion below
                failures.append(error)

        threads = [threading.Thread(target=work, args=(inputs[index::8],)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=30)
        assert not any(thread.is_alive() for thread in threads)
        assert len(frames.state.channels) == 1
    assert failures == [] and got == expected


# --- tokens -----------------------------------------------------------------------------------------------------------

def test_a_callable_token_is_refreshed_before_its_exp_and_the_channel_outlives_it(
        keys: tuple[Ed25519PrivateKey, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    private, public = keys
    monkeypatch.setattr(channel_client, "REFRESH_BEFORE_S", 2.5)
    issued: list[str] = []

    def token() -> str:
        issued.append(hub_token(private, ttl_s=4))
        return issued[-1]

    with channel_server(hub_public_key=public) as (url, frames), connect(url, token) as client:
        first = jwt.decode(issued[-1], options={"verify_signature": False})["exp"]
        bound = client.function("spam.message_type", context={"channel": "email"})
        wait_until(lambda: time.time() > first + 0.5, timeout=10)
        assert bound("WIN NOW").value
        assert client.decide([Call("spam.message_type", "x", {"channel": "email"})])[0].value
        ops = frames.take()
    assert ops[:3] == ["hello", "auth", "bind"] and "refresh" in ops


def test_a_client_dropped_without_close_closes_its_channel_and_stops_refreshing(
        keys: tuple[Ed25519PrivateKey, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    """Nothing the transport's threads hold keeps the ChannelJeff alive: once the last reference to it goes, the
    channel closes, its threads end and the token callable is no longer called."""
    private, public = keys
    monkeypatch.setattr(channel_client, "REFRESH_BEFORE_S", 4.0)
    monkeypatch.setattr(channel_client, "REFRESH_RETRY_S", 0.2)
    issued: list[str] = []

    def token() -> str:
        issued.append(hub_token(private, ttl_s=4))  # inside the refresh window at once: refreshed every 0.2 s
        return issued[-1]

    with channel_server(hub_public_key=public) as (url, frames):
        client = connect(url, token)
        transport = transport_of(client)
        wait_until(lambda: len(issued) >= 2)
        del client
        gc.collect()
        wait_until(lambda: transport.closed and not frames.state.channels)
        threads = [transport._reader, transport._refresher, transport._watchdog]
        assert not any(thread is not None and thread.is_alive() for thread in threads)
        calls = len(issued)
        time.sleep(0.6)
        assert len(issued) == calls


def test_a_string_token_expiring_with_calls_pending_fails_each_with_unauthorised(
        keys: tuple[Ed25519PrivateKey, Path]) -> None:
    """Review focus 1, client side: the calls the server is running when the token expires get their answers, the
    calls it never read fail with Unauthorised once it closes the channel, and so does every later call; nothing
    hangs."""
    private, public = keys
    gate, running = threading.Event(), threading.Semaphore(0)

    def gated(adapter: str, row: dict[str, Any]) -> list[float]:
        running.release()
        assert gate.wait(20), "the test never opened the gate"
        return answers(adapter, row)

    short = hub_token(private, ttl_s=2)
    expires = jwt.decode(short, options={"verify_signature": False})["exp"]
    results: dict[str, Any] = {}
    try:
        with channel_server(FakeEngine(gated, {"jeff.safety.spam", "jeff.support.triage"}),
                            hub_public_key=public) as (url, _), connect(url, short) as client:
            bound = client.function("spam.message_type", context={"channel": "email"})

            def call(name: str) -> None:
                try:
                    results[name] = bound(name).value
                except JeffError as error:
                    results[name] = error

            early = [threading.Thread(target=call, args=(f"early {index}",)) for index in range(3)]
            for thread in early:
                thread.start()
            for _ in early:
                assert running.acquire(timeout=5)
            wait_until(lambda: time.time() > expires + 0.3, timeout=5)
            late = [threading.Thread(target=call, args=(f"late {index}",)) for index in range(3)]
            for thread in late:
                thread.start()
            time.sleep(0.2)  # the late frames are on the wire before the gate opens
            gate.set()
            for thread in early + late:
                thread.join(timeout=8)
            assert not any(thread.is_alive() for thread in early + late)
            with pytest.raises(Unauthorised, match="the channel's token expired"):
                bound("after")
    finally:
        gate.set()
    assert all(results[f"early {index}"] == "legitimate" for index in range(3)), results
    for index in range(3):
        error = results[f"late {index}"]
        assert isinstance(error, Unauthorised) and "the channel's token expired" in str(error), results


def test_a_wrong_token_is_unauthorised_and_leaves_no_thread_behind() -> None:
    before = threading.active_count()
    with channel_server(api_key="the-key") as (url, _):
        with pytest.raises(Unauthorised):
            connect(url, "the-kez")
        with connect(url, "the-key") as client:
            assert client.decide([Call("spam.message_type", "x", {"channel": "email"})])[0].value
    wait_until(lambda: threading.active_count() <= before)


class Handshakes:
    """An ASGI wrapper that records the Authorization header of every WebSocket handshake the server receives."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.authorization: list[str | None] = []

    async def __call__(self, scope: dict[str, Any], receive: Callable[[], Any], send: Callable[[Any], Any]) -> None:
        if scope["type"] == "websocket":
            headers = dict(scope["headers"])
            value = headers.get(b"authorization")
            self.authorization.append(value.decode() if value is not None else None)
        await self.app(scope, receive, send)


def test_the_handshake_carries_the_token_as_a_bearer_header_for_a_proxy_in_front() -> None:
    """The server authenticates the channel by the auth message; the same token also goes on the handshake, so a
    proxy that opens WebSockets only for requests with credentials (the gateway) lets the channel through."""
    with_key = Handshakes(create_app(settings(api_key="the-key"), engine=fake()))
    with serving(with_key) as url:
        with connect(url, "the-key") as client:
            assert client.decide([SPAM])[0].value
    assert with_key.authorization == ["Bearer the-key"]
    open_server = Handshakes(create_app(settings(), engine=fake()))
    with serving(open_server) as url:
        with connect(url) as client:
            assert client.decide([SPAM])[0].value
    assert open_server.authorization == [None]


# --- closing ----------------------------------------------------------------------------------------------------------

def test_close_closes_the_channel_and_later_requests_raise_invalid_request() -> None:
    with channel_server() as (url, frames):
        client = connect(url)
        bound = client.function("triage.urgency", context=CONTEXT)
        client.close()
        wait_until(lambda: len(frames.state.channels) == 0)
        with pytest.raises(InvalidRequest, match="closed"):
            bound("x")  # the handle ended with the connection
        with pytest.raises(InvalidRequest, match="closed"):
            client.decide([Call("spam.message_type", "x", {"channel": "email"})])
        bound.close()  # nothing to release: the handles ended with the connection


def test_the_server_going_away_fails_every_request_with_backend_unavailable() -> None:
    with channel_server() as (url, _):
        client = connect(url)
        bound = client.function("triage.urgency", context=CONTEXT)
    try:
        transport = client.transport
        assert isinstance(transport, ChannelTransport)
        wait_until(lambda: transport.closed, timeout=10)
        for attempt in (lambda: bound("x"), lambda: client.decide([Call("spam.message_type", "x", {"channel": "e"})])):
            with pytest.raises(BackendUnavailable, match="the channel closed"):
                attempt()
    finally:
        client.close()


# --- races, refresh retries, stray replies, failed and stalled sends, closes, limits ----------------------------------

class RacingQueue(collections.deque):  # type: ignore[type-arg]
    """A release queue whose emptiness check waits for a second thread, so two threads both see it non-empty."""

    def __init__(self) -> None:
        super().__init__()
        self.barrier = threading.Barrier(2, timeout=5)

    def __bool__(self) -> bool:
        seen = len(self) > 0
        self.barrier.wait()
        return seen


def test_two_threads_draining_one_queued_release_send_it_once() -> None:
    with channel_server() as (url, frames), connect(url) as client:
        transport = transport_of(client)
        bound = client.function("triage.urgency", context=CONTEXT)
        bound._finalizer.detach()  # type: ignore[attr-defined]
        transport._releases = RacingQueue()
        transport._releases.append(bound._handle)  # type: ignore[attr-defined]
        threads, results = in_threads(2, lambda index: transport._send_releases())
        joined(threads)
        assert results == {0: None, 1: None}
        wait_until(lambda: handles(frames) == [0])
        assert frames.take().count("release") == 1


def renewing(private: Ed25519PrivateKey, ttl_s: int, renew_within_s: float) -> Callable[[], str]:
    """A token callable that caches its token and only gets a new one within `renew_within_s` of its exp (a real
    client renewing 30 s before exp, scaled down)."""
    cached: list[str] = []

    def token() -> str:
        if not cached or token_expiry(cached[-1]) - time.time() < renew_within_s:  # type: ignore[operator]
            cached.append(hub_token(private, ttl_s=ttl_s))
        return cached[-1]

    return token


def test_a_caching_token_callable_is_asked_again_until_it_renews_and_the_channel_stays_open(
        keys: tuple[Ed25519PrivateKey, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    private, public = keys
    monkeypatch.setattr(channel_client, "REFRESH_BEFORE_S", 2.5)
    monkeypatch.setattr(channel_client, "REFRESH_RETRY_S", 0.2)
    token = renewing(private, ttl_s=4, renew_within_s=1.2)
    with channel_server(hub_public_key=public) as (url, frames), connect(url, token) as client:
        first = token_expiry(token())
        assert first is not None
        wait_until(lambda: time.time() > first + 0.5, timeout=10)
        assert client.decide([SPAM])[0].value
        assert not transport_of(client).closed and "refresh" in frames.take()


def test_a_token_callable_that_never_renews_closes_the_channel_at_exp_with_every_failure(
        keys: tuple[Ed25519PrivateKey, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    private, public = keys
    monkeypatch.setattr(channel_client, "REFRESH_BEFORE_S", 2.0)
    monkeypatch.setattr(channel_client, "REFRESH_RETRY_S", 0.3)
    fixed = hub_token(private, ttl_s=3)
    with channel_server(hub_public_key=public) as (url, _), connect(url, lambda: fixed) as client:
        wait_until(lambda: transport_of(client).closed, timeout=8)
        with pytest.raises(Unauthorised) as raised:
            client.decide([SPAM])
    notes = getattr(raised.value, "__notes__", [])
    assert len(notes) >= 2 and all("not after the channel's current token" in note for note in notes), notes


def test_a_late_reply_to_a_timed_out_request_is_dropped_and_the_channel_stays_open() -> None:
    def slow(adapter: str, row: dict[str, Any]) -> list[float]:
        if "slow" in json.dumps(row["state"]):
            time.sleep(1.0)
        return answers(adapter, row)

    with channel_server(FakeEngine(slow, {"jeff.safety.spam", "jeff.support.triage"})) as (url, _), \
            connect(url, timeout_s=0.5) as client:
        with pytest.raises(DeadlineExceeded, match="may still have run"):
            client.decide([Call("spam.message_type", "slow", {"channel": "email"})])
        time.sleep(1.0)  # the late reply arrives meanwhile
        assert client.decide([SPAM])[0].value and not transport_of(client).closed


def test_a_reply_for_an_id_nobody_sent_ends_the_channel_with_engine_error() -> None:
    with channel_server() as (url, _), connect(url) as client:
        transport = transport_of(client)
        transport._send(encode({"op": "decide", "id": 10**6, "calls": [{"function": "spam.message_type",
                                                                        "input": "x", "context": {"channel": "e"}}]}))
        wait_until(lambda: transport.closed)
        with pytest.raises(EngineError, match="no request is waiting"):
            client.decide([SPAM])


def test_a_send_that_fails_unregisters_its_request_and_raises_the_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    with channel_server() as (url, _), connect(url) as client:
        transport = transport_of(client)

        def broken(frame: bytes) -> None:
            raise RuntimeError("the send broke")

        monkeypatch.setattr(transport._ws, "send", broken)
        with pytest.raises(RuntimeError, match="the send broke"):
            client.decide([SPAM])
        assert transport._pending == {}
        monkeypatch.undo()
        assert client.decide([SPAM])[0].value


class Proxy:
    """A TCP proxy in front of the server whose client-to-server direction can be paused (it stops reading, so the
    client's socket buffers fill and its send blocks) or throttled to `rate` bytes a second."""

    def __init__(self, upstream: str) -> None:
        self.upstream = int(upstream.rsplit(":", 1)[1])
        self.paused = threading.Event()
        self.rate: float | None = None
        self.listener = socket.socket()
        self.listener.bind(("127.0.0.1", 0))
        self.listener.listen(16)
        self.url = f"http://127.0.0.1:{self.listener.getsockname()[1]}"
        self.sockets: list[socket.socket] = []
        threading.Thread(target=self._accept, daemon=True).start()

    def _accept(self) -> None:
        while True:
            try:
                client, _ = self.listener.accept()
            except OSError:
                return  # the listener was closed
            client.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 32 * 1024)
            server = socket.create_connection(("127.0.0.1", self.upstream))
            self.sockets += [client, server]
            threading.Thread(target=self._pump, args=(client, server, True), daemon=True).start()
            threading.Thread(target=self._pump, args=(server, client, False), daemon=True).start()

    def _pump(self, source: socket.socket, target: socket.socket, upward: bool) -> None:
        try:
            while True:
                while upward and self.paused.is_set():
                    time.sleep(0.01)
                data = source.recv(16384 if upward and self.rate else 65536)
                if not data:
                    break
                target.sendall(data)
                if upward and self.rate:
                    time.sleep(len(data) / self.rate)
        except OSError:
            pass  # one side closed: the other is shut down below
        for side in (source, target):
            try:
                side.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass  # already closed

    def close(self) -> None:
        self.paused.clear()
        self.listener.close()
        for side in self.sockets:
            side.close()


@contextmanager
def proxied() -> Iterator[Proxy]:
    with channel_server() as (url, _):
        proxy = Proxy(url)
        try:
            yield proxy
        finally:
            proxy.close()


def test_a_send_with_no_bytes_accepted_for_the_timeout_ends_the_channel() -> None:
    with proxied() as proxy:
        client = connect(proxy.url, timeout_s=1.0)
        bound = client.function("spam.message_type", context={"channel": "email"})
        proxy.paused.set()
        started = time.monotonic()
        big, results = in_threads(1, lambda index: bound("x" * (20 * 1024 * 1024)))
        time.sleep(0.2)  # the big frame is filling the socket buffers
        small, small_results = in_threads(3, lambda index: bound("hi"))
        joined(big + small)
        stalled = time.monotonic() - started
        closing = time.monotonic()
        client.close()
        assert time.monotonic() - closing < 2.0
    for error in [results[0], *small_results.values()]:
        assert isinstance(error, BackendUnavailable) and "accepted no bytes of a frame for 1.0 s" in str(error), error
    assert 1.0 <= stalled < 4.0


def test_a_frame_on_a_slow_but_steady_link_takes_as_long_as_it_needs() -> None:
    with proxied() as proxy:
        proxy.rate = 2 * 1024 * 1024  # 2 MiB/s
        with connect(proxy.url, timeout_s=1.0) as client:
            bound = client.function("spam.message_type", context={"channel": "email"})
            started = time.monotonic()
            assert bound("x" * (6 * 1024 * 1024)).value == "legitimate"
            assert time.monotonic() - started > 2.5  # several times timeout_s


@pytest.mark.parametrize("timeout_s", [0, -1.0, float("inf"), float("nan"), True])
def test_a_timeout_that_is_not_a_positive_finite_number_is_refused(timeout_s: Any) -> None:
    for channel in (False, True):
        with pytest.raises(InvalidRequest, match="timeout_s"):
            Jeff.remote("http://127.0.0.1:1", None, timeout_s=timeout_s, channel=channel)


def test_a_callable_minting_a_short_token_on_every_call_is_not_refreshed_in_a_tight_loop(
        keys: tuple[Ed25519PrivateKey, Path], monkeypatch: pytest.MonkeyPatch) -> None:
    private, public = keys
    monkeypatch.setattr(channel_client, "REFRESH_RETRY_S", 0.5)  # REFRESH_BEFORE_S stays 60: always in the window

    def token() -> str:
        now = time.time()
        return jwt.encode({"sub": "alice", "licences": [], "iat": now, "exp": now + 3.5}, private,
                          algorithm="EdDSA")

    with channel_server(hub_public_key=public) as (url, frames), connect(url, token) as client:
        time.sleep(2.2)
        assert client.decide([SPAM])[0].value
        refreshes = frames.take().count("refresh")
    assert 3 <= refreshes <= 6, refreshes


def test_requests_pending_when_the_socket_closes_raise_backend_unavailable() -> None:
    gate = Gate()
    try:
        with channel_server(gate.engine()) as (url, _), connect(url) as client:
            transport = transport_of(client)
            bound = client.function("spam.message_type", context={"channel": "email"})
            threads, results = in_threads(3, lambda index: bound(f"pending {index}"))
            for _ in threads:
                assert gate.waiting.acquire(timeout=5)
            transport._ws.socket.shutdown(socket.SHUT_RDWR)
            joined(threads)
            for index in range(3):
                assert isinstance(results[index], BackendUnavailable), results
                assert "the channel closed" in str(results[index])
            with pytest.raises(BackendUnavailable, match="the channel closed"):
                bound("later")
            gate.open.set()  # the server's shutdown waits for the engine calls still running
    finally:
        gate.open.set()


def test_the_257th_outstanding_request_is_rate_limited() -> None:
    gate = Gate()
    try:
        with channel_server(gate.engine()) as (url, _), connect(url) as client:
            bound = client.function("spam.message_type", context={"channel": "email"})
            threads, results = in_threads(257, lambda index: bound(f"call {index}").value)
            wait_until(lambda: any(isinstance(result, RateLimited) for result in results.values()), timeout=8)
            gate.open.set()
            joined(threads)
    finally:
        gate.open.set()
    limited = [result for result in results.values() if isinstance(result, RateLimited)]
    assert len(limited) == 1 and limited[0].retry_after_s == 1
    assert sorted(result for result in results.values() if not isinstance(result, RateLimited)) == ["legitimate"] * 256
