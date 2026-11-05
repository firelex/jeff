"""Remote mode over the server's low-latency channel (contract/socket.md, "The channel"): Jeff.remote(url, token,
timeout_s=..., channel=True).

One WebSocket to <url>/v1/channel (ws:// for http://, wss:// for https://), compression off. Connecting sends hello
and, when there is a token, auth; then every request is one frame carrying an id, sent from any thread, and one reader
thread hands each reply to the request waiting for that id (the design of jeff.socket_engine.SocketEngine). Each
WebSocket binary message is one frame of jeff.protocol's format.

A bound function (ChannelBoundFunction) is a handle on the server: bound once with `bind` (its function, context,
labels and policy), each call then a `call` or `call_many` frame carrying only the input. A bind answered with
another version of the function's adapter than the local catalogue has raises the ContractError connecting gives. decide, dispatch and
graphs use the `decide`, `dispatch` and `graph` frames. The list of served functions (the adapter-version check at
connect, installed(refresh=True)) is read over HTTP, GET /v1/functions, as with channel=False: the channel has no
operation for it.

Token: over the channel a token callable is called for the token sent with auth, in each refresh window, and for each
GET /v1/functions; not before each request (as it is with channel=False). When the token is a JWT (a hub-signed token),
its `exp` is read from its payload without verifying the signature (base64 and JSON: the server verifies, and the jeff
package does not depend on PyJWT). From REFRESH_BEFORE_S seconds before exp, a refresher thread calls the callable and
sends `refresh` with the token it returns once that token expires later than the channel's current one; while the
callable returns a token that does not (a callable that caches its token and renews it later is fine) or raises, or the
server refuses the refresh, it calls the callable again every REFRESH_RETRY_S seconds until exp. Each failure is kept:
if exp passes without a fresh token, the channel closes with Unauthorised carrying every failure as a note. The callable
must therefore return a later-expiring token before exp. A string token, or a token that is not a JWT (an API key, a
console session token), is never refreshed. The refresh window is computed with this machine's clock and the server
decides expiry with its own: a client clock behind the server's by more than REFRESH_BEFORE_S makes the refresh too
late, so keep both clocks synchronised (NTP).

Sending: frames are sent from the caller's thread, in pieces of at most SEND_CHUNK bytes, and the time each piece is
accepted by the socket is recorded. As over HTTP (where httpx bounds each write), timeout_s bounds each stall, not the
whole upload: a large frame on a slow but working link takes as long as it takes, while a send that has had no bytes
accepted for timeout_s (the server, or the network, stopped taking data) ends the channel with BackendUnavailable. A
watchdog thread checks this, because the WebSocket keepalive cannot (its ping waits for the connection lock the
stalled send holds); it shuts the socket down, which also ends the stalled send, and every waiting request fails with
the same error.

An error raised for a failure the server sent carries no request_id (there is no HTTP response) but the server
connection's id as channel_id (from the failure's details), which the server names in its own log lines.

When the channel closes (a failure frame with id 0, the socket closing, or close()), every request waiting for a reply
fails with that error (BackendUnavailable "the channel closed" when the socket closed without one), and every later
request raises the same error. There is no automatic reconnect: connect again with Jeff.remote."""

import base64
import binascii
import collections
import itertools
import json
import logging
import socket
import threading
import time
import weakref
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import Future, TimeoutError as FutureTimeout
from typing import Any

import websockets.exceptions
import websockets.sync.client

from jeff import contract
from jeff.binding import JSON, Labels
from jeff.catalogue import Catalogue
from jeff.client import BoundFunction, Call
from jeff.decision import Decision
from jeff.errors import (BackendUnavailable, ContractError, DeadlineExceeded, EngineError, InvalidRequest, JeffError,
                         Unauthorised)
from jeff.escalation import Policy
from jeff.protocol import (CHANNEL_PATH, HEADER, MAX_BODY, MAX_FRAME, PREAUTH_MAX_BODY, check_hello, decode, encode,
                           hello, validate_message)
from jeff.remote import (RemoteBoundFunction, RemoteJeff, RestTransport, TokenSource, WirePolicy, _labels_to_wire,
                         check_batch, read_token, version_mismatch)
from jeff.wire import error_from_wire, error_to_wire

log = logging.getLogger("jeff")

# How long before a token's exp the refresher starts asking the token callable for a fresh token, and how often it
# asks again while the token it returns does not expire later (or the callable or the refresh fails).
REFRESH_BEFORE_S = 60.0
REFRESH_RETRY_S = 5.0
# How often the watchdog checks for a send lasting longer than timeout_s (at most; a quarter of timeout_s if shorter).
WATCHDOG_INTERVAL_S = 1.0
# The largest piece of a frame handed to the socket at once: each accepted piece counts as progress.
SEND_CHUNK = 64 * 1024


class _ProgressSocket:
    """The connection's socket, with sendall() sending in pieces of at most SEND_CHUNK bytes and calling `progress`
    after each piece the socket accepts; everything else is the socket's own. (The sync websockets connection writes
    only through sendall.)"""

    def __init__(self, sock: socket.socket, progress: Callable[[], None]) -> None:
        self._sock, self._progress = sock, progress

    def sendall(self, data: bytes) -> None:
        with memoryview(data) as view:
            offset = 0
            while offset < len(view):
                offset += self._sock.send(view[offset:offset + SEND_CHUNK])
                self._progress()

    def __getattr__(self, name: str) -> Any:
        return getattr(self._sock, name)


def channel_url(url: str) -> str:
    """The channel's WebSocket URL for a server URL: ws:// for http://, wss:// for https://, path /v1/channel."""
    for http, ws in (("http://", "ws://"), ("https://", "wss://")):
        if url.startswith(http):
            return ws + url[len(http):].rstrip("/") + CHANNEL_PATH
    raise InvalidRequest(f"The server URL {url!r} must start with http:// or https://")


def token_expiry(token: str) -> float | None:
    """The `exp` of a JWT (header.payload.signature), read from its payload without verifying the signature; None for
    a token that is not a JWT (an API key, a console session token), which this side cannot see expire. A JWT whose
    payload cannot be read or has no numeric exp is Unauthorised: the server would refuse it too."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)))
    except (binascii.Error, ValueError) as error:
        raise Unauthorised(f"The token is shaped like a JWT, but its payload is not base64-encoded JSON: {error}") \
            from error
    expires = payload.get("exp") if isinstance(payload, dict) else None
    if not isinstance(expires, (int, float)) or isinstance(expires, bool):
        raise Unauthorised(f"The token is a JWT without a numeric exp (got {expires!r}); the server needs one")
    return float(expires)


def _copy(error: JeffError) -> JeffError:
    """The same error (class, message, attributes, notes) as a new instance, chained from the original: each thread
    raises its own instance, never one shared by every thread."""
    copy = error_from_wire(error_to_wire(error))
    for note in getattr(error, "__notes__", ()):
        copy.add_note(note)
    copy.__cause__ = error
    return copy


class ChannelTransport:
    """The Transport (jeff.remote.Transport) over the channel, plus the handle operations bind, call, call_many and
    release. Thread-safe: requests from any thread are multiplexed by id on one WebSocket. GET /v1/functions goes over
    HTTP (a RestTransport with the same URL, token and timeout)."""

    def __init__(self, url: str, token: TokenSource, timeout_s: float) -> None:
        self.ws_url = channel_url(url)
        self._rest = RestTransport(url, token, timeout_s)  # also checks the URL and the token's type
        self.url, self.timeout_s, self._token = self._rest.url, timeout_s, token
        self._send_lock = threading.Lock()
        self._pending_lock = threading.Lock()
        self._pending: dict[int, Future[dict[str, Any]]] = {}
        self._unawaited: set[int] = set()  # ids of releases sent for garbage-collected bound functions
        self._abandoned: set[int] = set()  # ids of requests whose caller stopped waiting (DeadlineExceeded)
        self._releases: collections.deque[int] = collections.deque()
        self._ids = itertools.count(1)
        self._closed: JeffError | None = None
        self._refresh_failures: list[str] = []
        self._sending = False  # a send is in progress
        self._progress_at = 0.0  # time.monotonic() when the send in progress last had bytes accepted
        self._stop = threading.Event()
        self._reader: threading.Thread | None = None
        self._refresher: threading.Thread | None = None
        self._watchdog: threading.Thread | None = None
        try:
            self._ws = self._connect()
            self._ws.socket = _ProgressSocket(self._ws.socket, self._progressed)  # type: ignore[assignment]
        except BaseException:
            self._rest.close()
            raise
        try:
            self._handshake()
            self._reader = threading.Thread(target=self._reader_loop, name=f"jeff-channel {self.ws_url}", daemon=True)
            self._reader.start()
            self._watchdog = threading.Thread(target=self._watch_sends, name=f"jeff-channel-watchdog {self.ws_url}",
                                              daemon=True)
            self._watchdog.start()
            expires = self._authenticate()
            if expires is not None:
                self._refresher = threading.Thread(target=self._refresh_loop, args=(expires,),
                                                   name=f"jeff-channel-refresh {self.ws_url}", daemon=True)
                self._refresher.start()
        except BaseException:
            self.close()
            raise

    # ---- opening

    def _connect(self) -> websockets.sync.client.ClientConnection:
        # The token also goes on the handshake as Authorization: Bearer. The Jeff server ignores it there (it checks
        # the auth message sent after hello), but a proxy in front of the server, such as the gateway that serves it
        # under /jeff/, opens the WebSocket only for a request that carries credentials.
        headers = {} if self._token is None else {"Authorization": f"Bearer {self._current_token()}"}
        try:
            # legacy=True: the connection itself, closed by close(), rather than one for a with block only.
            connection = websockets.sync.client.connect(self.ws_url, compression=None, open_timeout=self.timeout_s,
                                                        close_timeout=self.timeout_s, additional_headers=headers,
                                                        max_size=HEADER.size + MAX_FRAME, legacy=True)
            assert isinstance(connection, websockets.sync.client.ClientConnection)
            return connection
        except websockets.exceptions.InvalidStatus as error:
            raise EngineError(f"The Jeff server at {self.url} refused to open the channel {self.ws_url}: HTTP status "
                              f"{error.response.status_code}") from error
        except TimeoutError as error:
            raise BackendUnavailable(f"Cannot reach the Jeff server at {self.url}: opening {self.ws_url} took more "
                                     f"than {self.timeout_s} s") from error
        except (OSError, websockets.exceptions.InvalidHandshake) as error:
            raise BackendUnavailable(f"Cannot reach the Jeff server at {self.url} ({self.ws_url}): "
                                     f"{type(error).__name__}: {error}") from error

    def _handshake(self) -> None:
        """Sends hello and reads the server's, before the reader thread starts (bounded by timeout_s)."""
        try:
            self._ws.send(encode(hello(), definition="hello"))
            data = self._ws.recv(timeout=self.timeout_s)
        except TimeoutError as error:
            raise BackendUnavailable(f"No hello from the channel {self.ws_url} within {self.timeout_s} s") from error
        except websockets.exceptions.ConnectionClosed as error:
            raise BackendUnavailable(f"{self.ws_url}: the channel closed during the hello ({_closing(error)})") \
                from error
        message = self._message(data)
        if message.get("ok") is False:
            raise error_from_wire(message["error"])
        check_hello(message)
        validate_message(message, f"the hello of {self.ws_url}", definition="hello")

    def _authenticate(self) -> float | None:
        """Sends auth when there is a token; the token's exp when the token is a callable's JWT (to refresh)."""
        if self._token is None:
            return None
        token = self._current_token()
        self._request({"op": "auth", "token": token}, limit=PREAUTH_MAX_BODY)
        return token_expiry(token) if callable(self._token) else None

    def _current_token(self) -> str:
        """The token now; called only when the channel has one (auth, and the refresher of a callable's token)."""
        token = read_token(self._token, self.url)
        assert token is not None
        return token

    # ---- reading

    def _message(self, data: str | bytes) -> dict[str, Any]:
        """The checked message one WebSocket message carries (exactly one frame)."""
        if isinstance(data, str):
            raise EngineError(f"The channel {self.ws_url} sent a text message; it carries binary frames only")
        if len(data) < HEADER.size:
            raise EngineError(f"The channel {self.ws_url} sent a message of {len(data)} bytes, shorter than a frame "
                              "header")
        (length,) = HEADER.unpack_from(data)
        if length != len(data) - HEADER.size:
            raise EngineError(f"The channel {self.ws_url} sent a message that is not exactly one frame: its header "
                              f"announces {length} bytes and the message carries {len(data) - HEADER.size}")
        try:
            return decode(data[HEADER.size:])
        except InvalidRequest as error:
            raise EngineError(f"The channel {self.ws_url} sent a frame that does not match the socket schema: "
                              f"{error}") from error

    def _reader_loop(self) -> None:
        error: JeffError
        try:
            while True:
                try:
                    data = self._ws.recv()
                except websockets.exceptions.ConnectionClosed as closed:
                    error = BackendUnavailable(f"{self.ws_url}: the channel closed ({_closing(closed)})")
                    error.__cause__ = closed
                    break
                message = self._message(data)
                id_ = message["id"]
                if id_ == 0 and message["ok"] is False:
                    # A failure of the whole connection (the token expired, a refresh for another caller, a frame
                    # the server could not read): the server closes the channel after it.
                    error = error_from_wire(message["error"])
                    break
                with self._pending_lock:
                    future = self._pending.pop(id_, None)
                    unawaited = id_ in self._unawaited
                    abandoned = id_ in self._abandoned
                    self._unawaited.discard(id_)
                    self._abandoned.discard(id_)
                if future is not None:
                    future.set_result(message)
                elif unawaited:
                    if not message["ok"]:
                        log.error("Releasing handle of a garbage-collected bound function on %s failed: %s",
                                  self.ws_url, message["error"])
                elif not abandoned:
                    # A reply to an id this side never sent, or one already answered: the two sides disagree about
                    # what is outstanding, so no later reply can be trusted to be matched right.
                    error = EngineError(f"The channel {self.ws_url} sent a reply for id {id_}, which no request is "
                                        "waiting for; the channel is closed")
                    break
                # An abandoned id is a late reply to a request whose caller raised DeadlineExceeded: dropped.
        except JeffError as failure:
            error = failure
        except Exception as failure:  # noqa: BLE001 - every way the reader can die ends the channel with a typed
            # error naming what went wrong, never an unobserved thread crash
            error = EngineError(f"The channel {self.ws_url} failed: {type(failure).__name__}: {failure}")
            error.__cause__ = failure
        self._shutdown(error)

    def _shutdown(self, error: JeffError) -> None:
        """Ends the channel once: the first error wins; every pending request fails with its own copy of it."""
        with self._pending_lock:
            if self._closed is None:
                for failure in self._refresh_failures:
                    error.add_note(failure)
                self._closed = error
            pending, self._pending = list(self._pending.values()), {}
            self._unawaited.clear()
            self._abandoned.clear()
        self._stop.set()
        for future in pending:
            future.set_exception(_copy(self._closed))
        self._ws.close()

    # ---- requests

    def _send(self, frame: bytes) -> None:
        with self._send_lock:
            self._progress_at = time.monotonic()
            self._sending = True
            try:
                self._ws.send(frame)
            finally:
                self._sending = False

    def _progressed(self) -> None:
        self._progress_at = time.monotonic()

    def _watch_sends(self) -> None:
        """Ends the channel when a send has had no bytes accepted for timeout_s (see the module docstring)."""
        interval = min(WATCHDOG_INTERVAL_S, self.timeout_s / 4)
        while not self._stop.wait(interval):
            if self._sending and time.monotonic() - self._progress_at > self.timeout_s:
                error = BackendUnavailable(f"{self.ws_url}: the socket accepted no bytes of a frame for "
                                           f"{self.timeout_s} s (the server or the network stopped taking data); the "
                                           "channel closed")
                # The socket first: the stalled send holds the connection's lock, which closing the WebSocket needs.
                try:
                    self._ws.socket.shutdown(socket.SHUT_RDWR)
                except OSError as failure:
                    error.add_note(f"Shutting the socket down failed: {failure}")
                self._shutdown(error)
                return

    def _sent_or_closed(self, id_: int, frame: bytes, ids: set[int] | None = None) -> None:
        """Sends a registered request's frame. On any failure the request is unregistered (from _pending, or from
        `ids`) before raising: a closed connection as the channel's error (BackendUnavailable "the channel closed"
        when the reader has not recorded one yet), anything else as it is."""
        try:
            self._send(frame)
        except BaseException as failure:
            with self._pending_lock:
                self._pending.pop(id_, None)
                if ids is not None:
                    ids.discard(id_)
                closed = self._closed
            if not isinstance(failure, websockets.exceptions.ConnectionClosed):
                raise
            if closed is not None:
                raise _copy(closed) from failure
            raise BackendUnavailable(f"{self.ws_url}: the channel closed ({_closing(failure)})") from failure

    def _register(self, message: dict[str, Any], limit: int) -> tuple[int, bytes]:
        id_ = next(self._ids)
        frame = encode({**message, "id": id_})
        if len(frame) - HEADER.size > limit:
            raise InvalidRequest(f"A {message['op']!r} frame of {len(frame) - HEADER.size} bytes is over the "
                                 f"channel's limit of {limit} bytes"
                                 + (" before auth" if limit == PREAUTH_MAX_BODY else " (50 MiB)"))
        return id_, frame

    def _request(self, message: dict[str, Any], limit: int = MAX_BODY) -> dict[str, Any]:
        """Sends one request and waits for its reply (at most timeout_s); its result, or the error its failure
        names."""
        self._send_releases()
        id_, frame = self._register(message, limit)
        future: Future[dict[str, Any]] = Future()
        with self._pending_lock:
            if self._closed is not None:
                raise _copy(self._closed)
            self._pending[id_] = future
        self._sent_or_closed(id_, frame)
        try:
            reply = future.result(self.timeout_s)
        except FutureTimeout as error:
            with self._pending_lock:
                if self._pending.pop(id_, None) is not None:
                    self._abandoned.add(id_)
            raise DeadlineExceeded(f"The channel {self.ws_url} did not answer {message['op']!r} within "
                                   f"{self.timeout_s} s; the server may still have run the request") from error
        if not reply["ok"]:
            raise error_from_wire(reply["error"])
        result: dict[str, Any] = reply["result"]
        return result

    def _decide(self, message: dict[str, Any], key: str) -> Any:
        """A request answered as /v1/decide answers: its result checked against decide_response, holding `key`."""
        result = self._request(message)
        if not contract.is_valid("decide_response", result) or key not in result:
            raise ContractError(f"The answer of {self.ws_url} to {message['op']!r} does not match "
                                f"contract/schemas/decide_response with {key!r}: "
                                f"{contract.first_problem('decide_response', result) or sorted(result)}")
        return result[key]

    def _send_releases(self) -> None:
        """Sends `release` for each garbage-collected bound function (queued by release_later) without waiting for
        the reply; the reader logs a failure."""
        while True:
            try:
                handle = self._releases.popleft()  # atomic: two threads draining never take one handle twice
            except IndexError:
                return  # the queue is empty
            id_, frame = self._register({"op": "release", "handle": handle}, MAX_BODY)
            with self._pending_lock:
                if self._closed is not None:
                    return  # the handles ended with the connection
                self._unawaited.add(id_)
            self._sent_or_closed(id_, frame, self._unawaited)

    # ---- the Transport

    def decide_calls(self, calls: list[dict[str, Any]], policy: WirePolicy) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = self._decide({"op": "decide", "calls": calls, "on_low_confidence": policy},
                                                       "decisions")
        if len(decisions) != len(calls):
            raise ContractError(f"{self.ws_url} answered {len(decisions)} decisions for {len(calls)} calls")
        return decisions

    def dispatch(self, body: dict[str, Any]) -> dict[str, Any]:
        decision: dict[str, Any] = self._decide({"op": "dispatch", **body}, "decision")
        return decision

    def graph(self, body: dict[str, Any]) -> dict[str, Any]:
        result: dict[str, Any] = self._decide({"op": "graph", **body}, "graph_result")
        return result

    def functions(self) -> dict[str, Any]:
        return self._rest.functions()

    # ---- handles

    def bind(self, function: str, context: Mapping[str, Any] | None, labels: Any,
             policy: WirePolicy) -> tuple[int, str]:
        """Binds `function` (its full ID) with this context, labels (wire form) and policy; the handle's number and
        the version of the function's adapter the server serves."""
        message: dict[str, Any] = {"op": "bind", "function": function, "on_low_confidence": policy}
        if context is not None:
            message["context"] = dict(context)
        if labels is not None:
            message["labels"] = labels
        result = self._request(message)
        handle, version = result.get("handle"), result.get("version")
        if not (type(handle) is int and handle >= 1) or result.get("function") != function \
                or not isinstance(version, str):
            raise ContractError(f"{self.ws_url} answered bind {function!r} with {result!r}; expected a handle (a "
                                f"whole number from 1) for {function!r} and its adapter's version")
        return handle, version

    def call(self, handle: int, input: JSON) -> dict[str, Any]:
        decision: dict[str, Any] = self._decide({"op": "call", "handle": handle, "input": input}, "decision")
        return decision

    def call_many(self, handle: int, inputs: Sequence[JSON]) -> list[dict[str, Any]]:
        decisions: list[dict[str, Any]] = self._decide({"op": "call_many", "handle": handle, "inputs": list(inputs)},
                                                       "decisions")
        if len(decisions) != len(inputs):
            raise ContractError(f"{self.ws_url} answered {len(decisions)} decisions for {len(inputs)} inputs")
        return decisions

    def release(self, handle: int) -> None:
        self._request({"op": "release", "handle": handle})

    def release_later(self, handle: int) -> None:
        """Queues a release, sent before the next request: safe from a garbage-collector finalizer on any thread
        (it takes no lock and sends nothing). Nothing to do once the channel is closed: its handles ended with it."""
        if self._closed is None:
            self._releases.append(handle)

    @property
    def closed(self) -> bool:
        return self._closed is not None

    # ---- refreshing

    def _refresh_loop(self, expires: float) -> None:
        """From REFRESH_BEFORE_S seconds before each token's exp (and at least REFRESH_RETRY_S after the last
        successful refresh, so a callable minting tokens shorter-lived than REFRESH_BEFORE_S is not called in a tight
        loop), asks the token callable for a token expiring later and sends `refresh` with it; each failure (the
        callable raised, its token does not expire later, the server refused the refresh) is kept and the callable
        asked again every REFRESH_RETRY_S seconds. If exp passes first, the channel closes with Unauthorised carrying
        every failure as a note."""
        earliest = 0.0  # no refresh before this time: REFRESH_RETRY_S after the last successful one
        while not self._stop.wait(max(0.0, expires - REFRESH_BEFORE_S - time.time(), earliest - time.time())):
            while True:
                try:
                    renewed = self._refresh(expires)
                    break
                except Exception as error:  # noqa: BLE001 - kept, and carried by the closing error if exp passes
                    if self._stop.is_set():
                        return
                    failure = (f"Refreshing the channel's token (exp {expires:g}) at {time.time():.1f} failed: "
                               f"{type(error).__name__}: {error}")
                    self._refresh_failures.append(failure)
                    log.warning("%s: %s", self.ws_url, failure)
                remaining = expires - time.time()
                if remaining <= 0:
                    self._shutdown(Unauthorised(
                        f"The channel's token expired at {expires:g} before the token callable returned one "
                        f"expiring later ({len(self._refresh_failures)} attempts failed; see the notes); the "
                        "channel is closed"))
                    return
                if self._stop.wait(min(REFRESH_RETRY_S, remaining)):
                    return
            self._refresh_failures.clear()
            expires, earliest = renewed, time.time() + REFRESH_RETRY_S

    def _refresh(self, expires: float) -> float:
        """One refresh attempt: the callable's token, sent with `refresh` when it expires after `expires`; its exp."""
        token = self._current_token()
        renewed = token_expiry(token)
        if renewed is None or renewed <= expires:
            raise Unauthorised(f"the token callable returned a token expiring at {renewed}, not after the channel's "
                               f"current token (exp {expires:g})")
        self._request({"op": "refresh", "token": token})
        return renewed

    # ---- closing

    def close(self) -> None:
        self._shutdown(InvalidRequest(f"This remote client of {self.url} is closed; connect again with Jeff.remote"))
        for thread in (self._reader, self._refresher, self._watchdog):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=self.timeout_s)
        self._rest.close()


def _closing(closed: websockets.exceptions.ConnectionClosed) -> str:
    frame = closed.rcvd
    if frame is None:
        return "no close frame received"
    return f"WebSocket close code {frame.code}" + (f": {frame.reason}" if frame.reason else "")


class ChannelBoundFunction(RemoteBoundFunction):
    """A bound function that is a handle on the channel: bound here (context, labels, policy checked) and then on the
    server with one `bind`; each call is one `call` frame, many() one `call_many`. A policy the server can run is
    bound with the handle; one with an llm, human or remote step is bound as "return" and run here on each decision.
    close() releases the handle (waiting for the server's reply); a bound function that is garbage-collected while
    the channel is open has its handle released before the channel's next request."""

    def __init__(self, jeff: "ChannelJeff", call_template: Call, on_low_confidence: Policy) -> None:
        super().__init__(jeff, call_template, on_low_confidence)
        self._transport = jeff.transport
        self._wire = jeff._wire_policy(on_low_confidence)
        labels = None if call_template.labels is None else _labels_to_wire(call_template.labels)
        self._handle, version = self._transport.bind(self._bound.function.id, call_template.context, labels,
                                                     self._wire if self._wire is not None else "return")
        manifest = self._bound.manifest
        if version != manifest.version:
            # The adapter changed on the server since connecting: the handle is released before the next request.
            self._transport.release_later(self._handle)
            raise version_mismatch(manifest.id, version, manifest.version)
        self._finalizer = weakref.finalize(self, self._transport.release_later, self._handle)
        self._finalizer.atexit = False

    def _live(self) -> int:
        if not self._finalizer.alive:
            raise InvalidRequest(f"This bound function of {self._bound.function.id} is closed; bind it again with "
                                 "jeff.function")
        return self._handle

    def __call__(self, input: JSON) -> Decision:
        decision = Decision.from_json(self._transport.call(self._live(), input))
        if self._wire is not None:
            return decision
        return self._jeff._apply(self._call(input), decision, self._policy)

    def many(self, inputs: Sequence[JSON]) -> list[Decision]:
        handle = self._live()
        if not inputs:
            return []
        check_batch(len(inputs), "inputs")
        decisions = [Decision.from_json(item) for item in self._transport.call_many(handle, inputs)]
        if self._wire is not None:
            return decisions
        return [self._jeff._apply(self._call(item), decision, self._policy)
                for item, decision in zip(inputs, decisions, strict=True)]

    def close(self) -> None:
        """Releases the handle. Nothing to release once the channel is closed: its handles ended with it."""
        if self._finalizer.detach() is not None and not self._transport.closed:
            self._transport.release(self._handle)


class ChannelJeff(RemoteJeff):
    """A RemoteJeff over the channel: everything as RemoteJeff, with bound functions as handles. A ChannelJeff
    dropped without close() closes its channel when it is garbage-collected (a weakref finalizer), so its threads
    end and the token callable stops being called: the transport and its threads hold no reference to it."""

    transport: ChannelTransport

    def __init__(self, transport: ChannelTransport, catalogue: Catalogue, url: str) -> None:
        super().__init__(transport, catalogue, url)
        self._finalizer = weakref.finalize(self, transport.close)
        self._finalizer.atexit = False  # the threads are daemons: nothing to wait for at interpreter exit

    def close(self) -> None:
        self._finalizer()  # closes the transport once, however often close() is called

    def function(self, ref: str, *, context: Mapping[str, JSON] | None = None,
                 labels: Labels | None = None, on_low_confidence: Policy = "return") -> BoundFunction:
        return ChannelBoundFunction(self, Call(ref, None, context, labels), on_low_confidence)


def connect(url: str, token: TokenSource, timeout_s: float, catalogue: Catalogue) -> ChannelJeff:
    transport = ChannelTransport(url, token, timeout_s)
    try:
        return ChannelJeff(transport, catalogue, transport.url)
    except BaseException:
        transport.close()
        raise

