"""/v1/channel: the low-latency channel (contract/socket.md, "The channel"). A WebSocket whose binary messages each carry
one frame of the engine socket's format (a 4-byte big-endian length, then a msgpack map), for services calling Jeff
at high rates: the client authenticates once, binds a function to a handle once, and then sends only each call's
input.

One Connection per WebSocket. It reads frames in order and answers the quick ones itself (auth, refresh, release,
and every refusal: a malformed frame, an unknown handle, a limit reached); bind and every call (call, call_many,
decide, dispatch, graph) run as a task each, whose work (catalogue checks, the planner, the engine) runs on the decide
thread pool (anyio's default thread limiter, sized by jeff_server.app.DECIDE_THREADS), never on the event loop.
Replies are sent as they finish, so they may arrive out of order; each carries its request's id.

Reading a frame: until the connection is authenticated (on a server with credentials) a frame body may be at most
PREAUTH_MAX_BODY bytes, decided from its 4-byte length before anything is unpacked, so a caller without credentials
can never make the server unpack or check a large frame. After that, a frame body up to INLINE_PARSE_MAX bytes is
unpacked and checked on the event loop (a few microseconds); a larger one on the decide thread pool, so the event
loop keeps serving every other connection meanwhile. msgpack itself refuses binary strings and extension types
(values JSON cannot hold), and json.dumps (C code) finds what is left (an empty binary string), so no Python walk
over the message is needed. A frame is checked against the socket schema definition of its own operation only
(DEFINITIONS), and a reply against the definition of its own shape (REPLY_DEFINITIONS), never against the schema's
oneOf of every message.

The answers are /v1/decide's: a call on a handle is answered exactly as /v1/decide answers {"calls": [<the bound
function, context and labels, with this input>], "on_low_confidence": <the bound policy>}, through the same
functions (jeff_server.decide.answer on a jeff_server.decide.caller_view carrying the caller's licences as they are
when the frame arrives), so the caller's licence and whether the adapter is served are checked again on every call.
A handle's calls run on the planner it was bound on; a bind, decide, dispatch or graph frame runs on the planner
current when it arrives (jeff_server.catalogue_refresh, rebuilt when the engines' adapters change). A decide,
dispatch or graph frame is checked as jeff_server.decide.check_body checks an HTTP body (its socket schema entry being
that body's shape with op and id added, see _decide).

Limits per connection: MAX_HANDLES live handles (TooManyHandles), MAX_OUTSTANDING requests sent and not yet answered
(RateLimited, retry_after_s OUTSTANDING_RETRY_AFTER_S), and no request id reused while it is outstanding. Each bind
and call frame (CALLS) also takes a slot of the caller's rate limit (jeff_server.auth.RateLimiter, exactly as one HTTP
request) before it runs, and gives it back just before its reply is sent, so a client that sends its next request on
reading a reply never finds the slot still held.

When the connection ends (the client closes it or goes away, or the server closes it), the handles end, and each
task is cancelled through its own anyio cancel scope: a task still waiting for a decide thread ends at once and gives
back its rate-limit slot; a task whose engine call is running keeps its slot and its thread until that call
returns (within the engine timeout, JEFF_ENGINE_TIMEOUT_S), as on the HTTP API, and its reply is dropped. The
Connection stays in app.state.channels until its last task has ended.

Credentials: on a server with any configured (every public one), the first request after hello must be an `auth`
that succeeds within AUTH_TIMEOUT_S of the connection opening, or the server sends Unauthorised with id 0 and
closes. When the credential expires (Caller.expires), the server stops reading, lets the requests it is working on
finish and send their replies, then sends Unauthorised("the channel's token expired") with id 0 and closes. A
console session is checked again for every bind and call frame, as every HTTP request checks it (UserStore.session):
once it has ended (its login removed, a new secret, or past its end) the server stops reading in the same way and
closes with Unauthorised (SESSION_ENDED), id 0. An account key (accounts mode) is looked up again in the key list for
every bind and call frame in the same way: a key the hub dropped, or a key list too old to trust, closes the channel
with the HTTP API's Unauthorised, id 0."""

import asyncio
import json
import logging
import secrets
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from typing import Any

import anyio
import msgpack  # type: ignore[import-untyped]
from fastapi import APIRouter, WebSocket, WebSocketDisconnect
from starlette.concurrency import run_in_threadpool

from jeff.client import Jeff
from jeff.contract import error_catalogue, first_problem
from jeff.errors import (EngineError, InvalidPolicy, InvalidRequest, JeffError, ProtocolMismatch, RateLimited,
                         TooManyHandles, Unauthorised, UnknownFunction)
from jeff.escalation import Escalate, FunctionStep, check_policy
from jeff.protocol import (CHANNEL_PATH, HEADER, MAX_BODY, PREAUTH_MAX_BODY, check_hello, encode, fail, hello, ok,
                           validate_message)

from jeff_server.access_log import AccessLog
from jeff_server.auth import ACCOUNT_HEADER, OPEN_CALLER, Authenticator, Caller, RateLimiter
from jeff_server.catalogue_refresh import CatalogueRefresher
from jeff_server.decide import (RegisteredGraph, answer, caller_view, check_interface, plan_body, policy_from_wire,
                                refuse_sdk_only_steps)
from jeff_server.operations import RequestCounts
from jeff_server.redaction import as_seen_by

log = logging.getLogger("jeff_server")

# How long a new connection has to send its hello and, on a server with credentials, a successful auth.
AUTH_TIMEOUT_S = 10.0
MAX_HANDLES = 1024
MAX_OUTSTANDING = 256
OUTSTANDING_RETRY_AFTER_S = 1.0
# Frame bodies up to this size are unpacked and checked on the event loop; larger ones on the decide thread pool.
INLINE_PARSE_MAX = 64 * 1024
# Every task a connection starts is named with this prefix (tests look for tasks left behind by it).
TASK_NAME = "jeff-channel"
CHANNEL_ID_BYTES = 4  # a connection id of 8 hex characters
EXPIRED = "the channel's token expired"
SESSION_ENDED = ("the channel's console session has ended (its login was removed, the users file was given a new "
                 "secret, or the session expired); sign in again")

ENGINE_OPS = frozenset({"installed", "predict", "serve", "reload", "stats", "health"})
# Each channel operation and the socket schema definition its frames must match.
DEFINITIONS = {"auth": "auth", "refresh": "auth", "bind": "bind", "call": "call", "call_many": "call_many",
               "release": "release", "decide": "decide", "dispatch": "dispatch", "graph": "graph"}
# The operations that may carry an escalation policy (refused here when a step can only run in the SDK).
WITH_POLICY = frozenset({"bind", "decide", "dispatch", "graph"})
# The operations that count against the caller's rate limit, as one HTTP request each.
CALLS = frozenset({"bind", "call", "call_many", "decide", "dispatch", "graph"})

# WebSocket close codes: 1008 (policy violation) for credentials, 1002 (protocol error) for anything else.
CLOSE_UNAUTHORISED = 1008
CLOSE_PROTOCOL = 1002
# How a connection is counted in /metrics when the server ends it other than by a close frame: 1001 (going away)
# when it is shutting down, 1011 (internal error) for an unexpected exception.
CLOSE_SHUTDOWN = 1001
CLOSE_INTERNAL = 1011

router = APIRouter()


@router.websocket(CHANNEL_PATH)
async def channel(websocket: WebSocket) -> None:
    await websocket.accept()
    await Connection(websocket).run()


@dataclass(frozen=True)
class Handle:
    """A bound function: its full ID, the context and labels it was bound with (None when not given) and its policy
    in wire form. Every call on it is answered on the caller's view of the planner in force when the call arrives
    (jeff_server.access), never the one it was bound on: its function and each of its escalation steps must still
    be there. One that is not (the caller may no longer use its adapter, or the adapter has left the catalogue)
    drops the handle, and the call is answered exactly as a fresh bind naming it would be (Connection._call_work)."""
    function: str
    context: Mapping[str, Any] | None
    labels: Any
    policy: str | Mapping[str, Any]

    def body(self, inputs: list[Any]) -> dict[str, Any]:
        """The /v1/decide body these inputs are answered as."""
        calls = []
        for item in inputs:
            call: dict[str, Any] = {"function": self.function, "input": item}
            if self.context is not None:
                call["context"] = self.context
            if self.labels is not None:
                call["labels"] = self.labels
            calls.append(call)
        return {"calls": calls, "on_low_confidence": self.policy}


@dataclass(frozen=True)
class Frame:
    """A request frame read off the connection: its id and either the checked message (`op` set) or the error that
    refuses this request only (`op` None)."""
    id: int
    op: str | None
    message: dict[str, Any]
    refused: JeffError | None = None


class Connection:
    def __init__(self, websocket: WebSocket) -> None:
        state = websocket.app.state
        self.websocket = websocket
        # The connection's id: in the server's log lines, its access-log entry and the details of every failure it
        # sends (channel_id), so a client's error can be matched to the server's records.
        self.id = secrets.token_hex(CHANNEL_ID_BYTES)
        self.auth: Authenticator = state.auth
        self.limiter: RateLimiter | None = state.limiter
        self.planner: CatalogueRefresher = state.planner
        self.graphs: Mapping[str, RegisteredGraph] = state.graphs
        self.registry: set[Connection] = state.channels
        self.access_log: AccessLog | None = state.access_log
        self.request_counts: RequestCounts = state.request_counts
        self.logged = False  # whether this connection's access-log entry has been written
        self.failed_auths = 0
        # The WebSocket close code the connection ended with: the server's (CLOSE_UNAUTHORISED, CLOSE_PROTOCOL) or
        # the client's, from its close frame; counted in /metrics when the connection ends.
        self.close_code: int | None = None
        # None until an auth succeeds on a server with credentials; the owner from the start on one without.
        self.caller: Caller | None = None if self.auth.configured else OPEN_CALLER
        self.auth_received = False
        self.token: str | None = None  # the credential the caller was authenticated with (auth, then refresh)
        self.handles: dict[int, Handle] = {}
        self.next_handle = 1
        self.outstanding: set[int] = set()  # the ids of the requests started and not yet answered
        self.tasks: dict[asyncio.Task[None], anyio.CancelScope] = {}
        self.closed = False
        self.send_lock = asyncio.Lock()
        self.loop = asyncio.get_running_loop()
        self.opened = self.loop.time()

    # ---- the connection's life

    async def run(self) -> None:
        self.registry.add(self)
        if self.caller is not None:  # a server without credentials: every caller is the owner from the start
            self._log_access(200)
        try:
            reason = await self._serve()
            if reason is not None:
                await self._close(reason)
        except asyncio.CancelledError:
            self._closed_with(CLOSE_SHUTDOWN)
            raise
        except BaseException:
            self._closed_with(CLOSE_INTERNAL)
            raise
        finally:
            self._end()
            if not self.logged:
                self._log_access(401)
            if self.close_code is None:
                raise EngineError("A channel connection ended without a close code: a server bug")
            self.request_counts.add(CHANNEL_PATH, self.close_code)

    def _closed_with(self, code: int) -> None:
        """Records the close code, unless the connection already has one (the first close is the one that
        happened)."""
        if self.close_code is None:
            self.close_code = code

    def _log_access(self, status: int) -> None:
        """The connection's one access-log entry: written when it is authenticated (200; at once on a server without
        credentials), or when it ends without having been (401), with the caller's kind and subject, the client's
        address and how many auth attempts failed."""
        self.logged = True
        if self.access_log is None:
            return
        caller = self.caller
        client = self.websocket.client
        detail = f"WEBSOCKET {caller.kind}" if caller is not None else "WEBSOCKET"
        detail += f", channel {self.id}"
        if self.failed_auths:
            detail += f", {self.failed_auths} failed auth"
        self.access_log.record_request(
            "api", user=caller.log_user if caller is not None else None,
            headers=self.websocket.headers, peer=client.host if client else None, path=CHANNEL_PATH, status=status,
            detail=detail)

    def _end(self) -> None:
        """Drops the handles and cancels every task through its cancel scope: one waiting for a decide thread ends
        at once, one whose engine call is running ends (giving back its rate-limit slot and its thread) when that
        call returns. The Connection leaves app.state.channels when its last task has ended. Synchronous, so it
        completes even when the connection's own coroutine is being cancelled."""
        self.closed = True
        self.handles.clear()
        for scope in self.tasks.values():
            scope.cancel()
        if not self.tasks:
            self.registry.discard(self)

    def _task_ended(self, task: asyncio.Task[None]) -> None:
        self.tasks.pop(task, None)
        if self.closed and not self.tasks:
            self.registry.discard(self)

    async def _serve(self) -> JeffError | None:
        """Reads frames until the client goes away (None) or the connection must close (the error to close
        with)."""
        greeting = await self._receive(self.opened + AUTH_TIMEOUT_S)
        if greeting is None:
            return None
        if isinstance(greeting, TimeoutError):
            return InvalidRequest(f"The channel received no hello within {AUTH_TIMEOUT_S:g} seconds of opening")
        try:
            _check_greeting(self._body(greeting))
        except (InvalidRequest, ProtocolMismatch) as error:
            return error
        await self._send(hello())
        if ACCOUNT_HEADER in self.websocket.headers:
            # X-Jeff-Account lets the hub playground's key stand in for an account on the HTTP API and /mcp only;
            # refused here by name rather than ignored, so a client never believes it runs as that account.
            return Unauthorised(f"X-Jeff-Account is not accepted on {CHANNEL_PATH}: a channel runs as the caller its "
                                "auth token names; send it to the HTTP API or /mcp instead")
        while True:
            message = await self._receive(self._deadline())
            if isinstance(message, TimeoutError):
                if self.caller is None:
                    return Unauthorised(f"The channel was not authenticated within {AUTH_TIMEOUT_S:g} seconds of "
                                        "opening: send auth {token} after hello")
                await self._finish_in_flight()
                return Unauthorised(EXPIRED)
            if message is None:
                return None
            try:
                body = self._body(message)
                authenticated = self.caller is not None
                frame = read_frame(body, authenticated) if len(body) <= INLINE_PARSE_MAX else \
                    await run_in_threadpool(read_frame, body, authenticated)
            except InvalidRequest as error:
                return error
            if frame.id in self.outstanding:
                return InvalidRequest(f"Request id {frame.id} is already outstanding on this channel; a client must "
                                      "not reuse an id until its reply has arrived")
            if frame.refused is not None:
                await self._send(fail(frame.id, frame.refused))
                continue
            reason = await self._handle(frame)
            if reason is not None:
                return reason

    def _deadline(self) -> float | None:
        """When to stop waiting for the next frame (event-loop time): the auth timeout until authenticated, then
        the credential's expiry, if it has one."""
        if self.caller is None:
            return self.opened + AUTH_TIMEOUT_S
        if self.caller.expires is None:
            return None
        return self.loop.time() + (self.caller.expires - time.time())

    async def _receive(self, deadline: float | None) -> dict[str, Any] | TimeoutError | None:
        """The next WebSocket message, a TimeoutError (returned, not raised) once `deadline` passes, or None when
        the client has gone away. A message the timeout interrupts is never needed: the connection closes."""
        try:
            async with asyncio.timeout_at(deadline):
                message = await self.websocket.receive()
        except TimeoutError as error:
            return error
        if message["type"] == "websocket.disconnect":
            self._closed_with(int(message["code"]))
            return None
        return dict(message)

    def _body(self, message: Mapping[str, Any]) -> bytes:
        """The frame body of a WebSocket message, which must be binary and exactly one frame, and no larger than
        the connection allows now (PREAUTH_MAX_BODY until authenticated, MAX_BODY after)."""
        data = message.get("bytes")
        if data is None:
            raise InvalidRequest("The channel carries binary WebSocket messages only, each one frame; got a text "
                                 "message")
        if len(data) < HEADER.size:
            raise InvalidRequest(f"A channel message of {len(data)} bytes is shorter than a frame header "
                                 f"({HEADER.size} bytes)")
        (length,) = HEADER.unpack_from(data)
        if self.caller is None and length > PREAUTH_MAX_BODY:
            raise InvalidRequest(f"A channel frame of {length} bytes before auth: until the channel is "
                                 f"authenticated a frame may be at most {PREAUTH_MAX_BODY} bytes")
        if length > MAX_BODY:
            raise InvalidRequest(f"A channel frame of {length} bytes is over the 50 MiB limit ({MAX_BODY} "
                                 "bytes)")
        if length != len(data) - HEADER.size:
            raise InvalidRequest(f"A channel message must hold exactly one frame: its header announces {length} "
                                 f"bytes and the message carries {len(data) - HEADER.size}")
        return bytes(data[HEADER.size:])

    async def _finish_in_flight(self) -> None:
        if self.tasks:
            await asyncio.wait(set(self.tasks))

    async def _close(self, error: JeffError) -> None:
        await self._send(fail(0, error))
        if self.closed:
            return
        self.closed = True
        code = CLOSE_UNAUTHORISED if isinstance(error, Unauthorised) else CLOSE_PROTOCOL
        self._closed_with(code)
        try:
            await self.websocket.close(code)
        except WebSocketDisconnect:
            return  # the client went away first: there is nobody left to close for

    async def _send(self, message: dict[str, Any]) -> None:
        """Writes are serialised. A message to a client that has gone away is dropped (there is nobody to tell). A
        reply that cannot be encoded (larger than the socket protocol's 64 MiB) is replaced by an EngineError
        failure for the same id, so the client is never left waiting. Every failure carries the connection's id in
        its details (channel_id)."""
        async with self.send_lock:
            if self.closed:
                return
            try:
                payload = encode(self._tagged(message), definition=_definition_of(message))
            except InvalidRequest as error:
                log.error("Could not encode the reply to request %s on channel %s (%s); sending a failure instead",
                          message.get("id"), self.id, error)
                payload = encode(self._tagged(fail(message.get("id", 0),
                                                   EngineError(f"the server could not encode its reply: {error}"))),
                                 definition="failure_reply")
            try:
                await self.websocket.send_bytes(payload)
            except WebSocketDisconnect as gone:
                self._closed_with(gone.code)
                self.closed = True

    def _tagged(self, message: dict[str, Any]) -> dict[str, Any]:
        """A failure with the connection's id added to its error's details; any other message as it is."""
        if message.get("ok") is not False:
            return message
        error = message["error"]
        return {**message, "error": {**error, "details": {**error["details"], "channel_id": self.id}}}

    # ---- one request

    async def _handle(self, frame: Frame) -> JeffError | None:
        """Answers or starts one request; returns an error when the connection must close with it."""
        id_, op, message = frame.id, frame.op, frame.message
        if op == "auth":
            await self._authenticate(id_, message["token"])
            return None
        assert self.caller is not None  # read_frame refuses every other op before auth
        if op == "refresh":
            return await self._refresh(id_, message["token"])
        if op == "release":
            await self._release(id_, message["handle"])
            return None
        assert op is not None
        ended = self._credential_ended()
        if ended is not None:
            await self._finish_in_flight()
            return ended
        await self._start(id_, op, message, self.caller)
        return None

    def _credential_ended(self) -> Unauthorised | None:
        """Why the credential this connection was authenticated with no longer holds, or None while it does.
        Checked for every bind and call, as every HTTP request checks it, so a credential ended on the server stops
        the channel as it stops the HTTP API:
        - a console session: ended when its login is removed from the users file, the file is given a new secret,
          or the session is past its end (an HMAC and the users file's cached modification time; SESSION_ENDED);
        - an account key or the playground's (accounts mode): looked up again in the key list in force (a dictionary
          lookup), so a key the hub dropped, or a key list too old to trust, ends it with the HTTP API's message.
          The caller is replaced by the one the current list gives, so a call counts in the period it arrives in.
        Other credentials cannot be revoked: an API key is compared once, and a hub token lasts until its exp (the
        deadline in _deadline)."""
        if self.caller is None:
            return None
        if self.caller.kind == "console":
            assert self.auth.users is not None and self.token is not None
            return Unauthorised(SESSION_ENDED) if self.auth.users.session(self.token) is None else None
        if self.caller.kind in ("account", "playground"):
            assert self.auth.accounts is not None and self.token is not None
            try:
                current = self.auth.accounts.caller(self.token, None)
            except Unauthorised as ended:
                return ended
            if (current.kind, current.subject) != (self.caller.kind, self.caller.subject):
                return Unauthorised(f"the channel's key now belongs to {current.kind} {current.subject!r}, not "
                                    f"{self.caller.kind} {self.caller.subject!r}; the channel is closed")
            self.caller = current
        return None

    async def _authenticate(self, id_: int, token: str) -> None:
        if self.auth_received:
            await self._send(fail(id_, InvalidRequest("This channel is already authenticated; send refresh {token} "
                                                      "to replace its token")))
            return
        try:
            caller = self.auth.caller(f"Bearer {token}", None)  # X-Jeff-Account is refused at the hello
        except Unauthorised as error:
            self.failed_auths += 1
            await self._send(fail(id_, error))
            return
        self.caller, self.auth_received, self.token = caller, True, token
        if not self.logged:
            self._log_access(200)
        await self._send(ok(id_, {}))

    async def _refresh(self, id_: int, token: str) -> JeffError | None:
        assert self.caller is not None
        try:
            caller = self.auth.caller(f"Bearer {token}", None)
        except Unauthorised as refused:
            await self._send(fail(id_, refused))
            return None
        if (caller.kind, caller.subject) != (self.caller.kind, self.caller.subject):
            error = Unauthorised(f"refresh must keep the channel's caller: it belongs to {self.caller.kind} "
                                 f"{self.caller.subject!r}, and the new token to {caller.kind} {caller.subject!r}; "
                                 "the channel is closed")
            await self._send(fail(id_, error))
            return error
        self.caller, self.token = caller, token
        await self._send(ok(id_, {}))
        return None

    async def _release(self, id_: int, number: int) -> None:
        if number not in self.handles:
            await self._send(fail(id_, _no_handle(number)))
            return
        del self.handles[number]
        await self._send(ok(id_, {}))

    async def _start(self, id_: int, op: str, message: dict[str, Any], caller: Caller) -> None:
        """Checks the limits, then starts the request's task. A refusal is answered at once."""
        work: Callable[[], Awaitable[dict[str, Any]]]
        if op in ("call", "call_many"):
            handle = self.handles.get(message["handle"])
            if handle is None:
                await self._send(fail(id_, _no_handle(message["handle"])))
                return
            inputs = [message["input"]] if op == "call" else message["inputs"]
            work = self._call_work(caller, message["handle"], handle, inputs, many=op == "call_many")
        elif op == "bind":
            if len(self.handles) >= MAX_HANDLES:
                await self._send(fail(id_, _too_many_handles()))
                return
            work = self._bind_work(caller, message)
        else:
            work = self._decide_work(caller, {key: value for key, value in message.items() if key not in ("op", "id")})
        if len(self.outstanding) >= MAX_OUTSTANDING:
            await self._send(fail(id_, RateLimited(
                f"This channel already has {MAX_OUTSTANDING} requests outstanding, the most one connection may "
                "have; wait for replies before sending more", retry_after_s=OUTSTANDING_RETRY_AFTER_S)))
            return
        slot = caller.rate_key if op in CALLS and self.limiter is not None else None
        if slot is not None:
            assert self.limiter is not None
            try:
                self.limiter.acquire(slot)
            except RateLimited as error:
                await self._send(fail(id_, error))
                return
        self.outstanding.add(id_)
        scope = anyio.CancelScope()
        task = asyncio.create_task(self._answer(id_, work, slot, scope), name=f"{TASK_NAME} {op} {id_}")
        self.tasks[task] = scope
        task.add_done_callback(self._task_ended)

    async def _answer(self, id_: int, work: Callable[[], Awaitable[dict[str, Any]]], slot: str | None,
                      scope: anyio.CancelScope) -> None:
        """Runs one request inside its cancel scope and sends its reply. The rate-limit slot is given back once,
        just before the reply is sent, or when the task ends without sending one (cancelled when the connection
        ended: at once while waiting for a thread, after the engine call returns once it runs); the request stops
        counting as outstanding once its reply has been sent."""
        holding = slot is not None

        def give_back_slot() -> None:
            nonlocal holding
            if holding:
                holding = False
                assert self.limiter is not None and slot is not None
                self.limiter.release(slot)

        try:
            with scope:
                try:
                    reply = ok(id_, await work())
                except JeffError as error:
                    reply = fail(id_, _as_sent(self._as_seen(error, id_), id_, self.id))
                except Exception as error:  # noqa: BLE001 - every request gets a typed reply, as an HTTP request
                    # gets a 500 EngineError; never an exception left for asyncio to report with nobody awaiting it
                    log.exception("Answering request %s on channel %s failed", id_, self.id)
                    wrapped = EngineError(f"{type(error).__name__}: {error}")
                    wrapped.__cause__ = error
                    reply = fail(id_, self._as_seen(wrapped, id_))
                give_back_slot()
                await self._send(reply)
        finally:
            give_back_slot()
            self.outstanding.discard(id_)

    def _as_seen(self, error: JeffError, id_: int) -> JeffError:
        """A caller who is not the owner gets an error naming no private adapter and no engine socket path
        (jeff_server.redaction), as over HTTP."""
        return as_seen_by(error, self.caller, lambda: self.planner.built.catalogue,
                          lambda: self.planner.settings.engine_sockets, f"request {id_} on channel {self.id}")

    # ---- the work of each request (the planner runs on the decide thread pool)

    def _bind_work(self, caller: Caller, message: dict[str, Any]) -> Callable[[], Awaitable[dict[str, Any]]]:
        async def work() -> dict[str, Any]:
            shared = await self.planner.ready()
            handle, result = await run_in_threadpool(self.planner.retry_unknown, shared,
                                                     lambda jeff: _bind(jeff, caller, message))
            if self.closed:  # the connection ended while this bind ran: its handles are gone, and so is this one
                raise EngineError("The channel closed before the bind finished; no handle was kept")
            # Checked again now: other binds may have finished while this one ran.
            if len(self.handles) >= MAX_HANDLES:
                raise _too_many_handles()
            number = self.next_handle
            self.next_handle += 1
            self.handles[number] = handle
            return {"handle": number, **result}
        return work

    def _call_work(self, caller: Caller, number: int, handle: Handle, inputs: list[Any],
                   many: bool) -> Callable[[], Awaitable[dict[str, Any]]]:
        async def work() -> dict[str, Any]:
            view = caller_view(await self.planner.ready(), caller)
            try:
                _check_handle(view, handle)
            except (UnknownFunction, InvalidPolicy):
                # Its function or an escalation step is no longer in this caller's view: the handle goes, and the
                # call is refused as a bind naming it would be now.
                if self.handles.get(number) is handle:
                    del self.handles[number]
                raise
            answered = await run_in_threadpool(answer, view, self.graphs, handle.body(inputs))
            decisions = answered["decisions"]
            return {"decisions": decisions} if many else {"decision": decisions[0]}
        return work

    def _decide_work(self, caller: Caller, body: dict[str, Any]) -> Callable[[], Awaitable[dict[str, Any]]]:
        async def work() -> dict[str, Any]:
            shared = await self.planner.ready()
            return await run_in_threadpool(self.planner.retry_unknown, shared,
                                           lambda jeff: _decide(jeff, caller, self.graphs, body))
        return work


# ---- reading frames (runs on the event loop for small bodies, on the decide thread pool for large ones)

def _refuse_extension(code: int, data: bytes) -> object:
    raise ValueError(f"msgpack extension type {code}")


# How an empty binary string is packed (bin 8, 16 and 32 with length 0): the one value JSON cannot hold that
# msgpack's own limits (max_bin_len=0, max_ext_len=0) let through.
EMPTY_BINARY = (b"\xc4\x00", b"\xc5\x00\x00", b"\xc6\x00\x00\x00\x00")


def _unpack(body: bytes) -> object:
    """A frame body unpacked, holding only values JSON can hold. msgpack refuses a binary string longer than 0
    bytes and every extension type (timestamps included) while unpacking. An empty binary string is looked for in
    the packed bytes (a C search, microseconds even for 50 MiB); those bytes can also occur inside other values (the
    number 196 followed by 0), so only when they are found is the unpacked message checked exactly, with json.dumps
    (C code). Either refusal is like a body that is not msgpack: the request cannot be read, so it has no id to
    answer by."""
    try:
        raw = msgpack.unpackb(body, raw=False, max_bin_len=0, max_ext_len=0, ext_hook=_refuse_extension)
    except (msgpack.UnpackException, ValueError) as error:
        raise InvalidRequest(f"A channel frame is not valid msgpack, or holds a binary string or an extension type "
                             f"(channel messages carry only values JSON can hold): {error}") from error
    if not any(marker in body for marker in EMPTY_BINARY):
        return raw
    try:
        json.dumps(raw)
    except TypeError as error:
        raise InvalidRequest(f"A channel frame holds a value JSON cannot hold ({error}); channel messages carry "
                             "only values JSON can hold") from error
    except RecursionError as error:
        raise InvalidRequest("A channel frame is nested too deeply to read") from error
    return raw


def _check_greeting(body: bytes) -> None:
    raw = _unpack(body)
    if not isinstance(raw, dict):
        raise InvalidRequest(f"The channel's first frame must be a hello map; got a {type(raw).__name__}")
    check_hello(raw)
    validate_message(raw, "the channel's hello", definition="hello")


def read_frame(body: bytes, authenticated: bool) -> Frame:
    """The request in a frame body. Raises InvalidRequest when the connection must close (the body cannot be read,
    or has no usable id); a request it can answer by id but refuses comes back as a Frame with `refused` set. Until
    the connection is `authenticated`, every request but an auth is refused with Unauthorised before its shape is
    checked, so a caller without credentials never makes the server check a request."""
    raw = _unpack(body)
    id_ = raw.get("id") if isinstance(raw, dict) else None
    if not (type(id_) is int and id_ >= 1):
        op = raw.get("op") if isinstance(raw, dict) else None
        what = f"an {op[:100]!r} message" if isinstance(op, str) else f"a {type(raw).__name__}"
        raise InvalidRequest(f"A channel message needs an id (a whole number from 1); got {what} without one")
    assert isinstance(raw, dict)
    op = raw.get("op")
    if not authenticated and op != "auth":
        return Frame(id_, None, raw, Unauthorised("This channel is not authenticated yet: send auth {token} first"))
    try:
        return Frame(id_, op, _checked(op, raw)) if isinstance(op, str) else Frame(id_, None, raw, _not_an_op(op))
    except InvalidRequest as error:
        return Frame(id_, None, raw, error)


def _not_an_op(op: object) -> InvalidRequest:
    return InvalidRequest(f"A channel message's op must be one of {', '.join(DEFINITIONS)}; got {op!r}"[:300])


def _checked(op: str, raw: dict[str, Any]) -> dict[str, Any]:
    if op in ENGINE_OPS:
        raise InvalidRequest(f"{op!r} is an engine socket operation; the channel does not serve it (its "
                             f"operations: {', '.join(DEFINITIONS)})")
    if op not in DEFINITIONS:
        raise InvalidRequest(f"{op[:100]!r} is not a channel operation; expected one of {', '.join(DEFINITIONS)}")
    if op in WITH_POLICY:
        refuse_sdk_only_steps(raw)
    try:
        try:
            return validate_message(raw, "incoming channel message", definition=DEFINITIONS[op])
        except InvalidRequest as error:
            raise InvalidRequest(f"{op!r} request does not match the socket schema: "
                                 f"{first_problem('socket', raw, DEFINITIONS[op])}") from error
    except RecursionError as error:
        raise InvalidRequest(f"The {op!r} request is nested too deeply to check") from error


def _definition_of(message: Mapping[str, Any]) -> str:
    """The socket schema definition an outgoing message is checked against: its own shape."""
    if "op" in message:
        return "hello"
    return "ok_reply" if message["ok"] is True else "failure_reply"


# ---- the work of each request

def _bind(shared: Jeff, caller: Caller, message: Mapping[str, Any]) -> tuple[Handle, dict[str, Any]]:
    """Checks a bind as /v1/decide checks the same call, in the same order: the function, the policy against its
    interface, the caller's licence, whether its adapter is served, the context and the labels."""
    jeff = caller_view(shared, caller)
    policy = message.get("on_low_confidence", "return")
    function = jeff.catalogue.function(message["function"])
    check_policy(jeff, function.interface, policy_from_wire(policy))
    jeff.function(function.id, context=message.get("context"), labels=message.get("labels"),
                  on_low_confidence=policy_from_wire(policy))
    manifest = jeff.catalogue.manifest_of(function)
    handle = Handle(function.id, message.get("context"), message.get("labels"), policy)
    return handle, {"function": function.id, "version": manifest.version}


def _check_handle(view: Jeff, handle: Handle) -> None:
    """A handle's function and each function step of its policy are in `view` (the caller's view of the planner in
    force). UnknownFunction for the function, or the InvalidPolicy check_policy gives a step it cannot find, exactly
    as a bind of the same function and policy on `view` would be refused (_bind)."""
    function = view.catalogue.function(handle.function)
    policy = policy_from_wire(handle.policy)
    if isinstance(policy, Escalate):
        for step in policy.steps:
            if isinstance(step, FunctionStep):
                try:
                    view.catalogue.function(step.ref)
                except UnknownFunction:
                    check_policy(view, function.interface, policy)  # raises the refusal a bind gives
                    raise
    return None


def _decide(shared: Jeff, caller: Caller, graphs: Mapping[str, RegisteredGraph],
            body: dict[str, Any]) -> dict[str, Any]:
    """A decide, dispatch or graph frame without its op and id: exactly a /v1/decide body, already checked as
    jeff_server.decide.check_body checks one, but for the dispatch interface (done here): read_frame refused its
    llm, human and remote steps, and its socket schema entry is one of decide_request's three shapes with op and id
    added (each additionalProperties false), so the frame matching its entry is the body matching decide_request."""
    check_interface(body)
    return plan_body(shared, caller, graphs, body)


def _no_handle(number: int) -> InvalidRequest:
    return InvalidRequest(f"This channel holds no handle {number} (never bound on this connection, or released)")


def _too_many_handles() -> TooManyHandles:
    return TooManyHandles(f"This channel already holds {MAX_HANDLES} handles, the most one connection may; release "
                          "one first")


def _as_sent(error: JeffError, id_: int, channel_id: str) -> JeffError:
    """The error as a door sends it: one the catalogue says is never sent (status null) is a server bug, sent as an
    EngineError and logged, as the HTTP API does (jeff_server.errors)."""
    spec = error_catalogue().get(type(error).__name__)
    if spec is not None and spec.status is not None:
        return error
    log.error("%s has status null in the error catalogue (never sent by a server) but was raised answering request "
              "%s on channel %s; this is a server bug: %s", type(error).__name__, id_, channel_id, error)
    return EngineError(f"{type(error).__name__}: {error}")
