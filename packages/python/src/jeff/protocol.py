"""The engine socket protocol (contract/socket.md): each frame is a 4-byte big-endian length, then a msgpack map.
Replies carry the request id and may arrive in any order."""

import asyncio
import struct
from collections.abc import Callable, Mapping
from typing import Any

import msgpack  # type: ignore[import-untyped]

from jeff import contract
from jeff.contract import SchemaName
from jeff.errors import ContractError, EngineError, InvalidRequest, JeffError, ProtocolMismatch
from jeff.wire import error_to_wire

PROTOCOL_VERSION = 2  # 2: the base served as adapter "base", and input_tokens in predict results
MAX_FRAME = 64 * 1024 * 1024
HEADER = struct.Struct(">I")

# The server's low-latency channel (contract/socket.md, "The channel"), shared by the server (jeff_server.channel) and
# the client (jeff.channel_client): its path, the largest frame body it reads (the HTTP API's body limit, 50 MiB) and
# the largest before the connection is authenticated (a hello or an auth is far smaller).
CHANNEL_PATH = "/v1/channel"
MAX_BODY = 50 * 1024 * 1024
PREAUTH_MAX_BODY = 64 * 1024


# The socket schema's definition for each request op (contract/schemas/socket.schema.json).
_OP_DEFINITIONS = {"installed": "simple", "reload": "simple", "stats": "simple", "health": "simple",
                   "predict": "predict", "serve": "serve", "auth": "auth", "refresh": "auth", "bind": "bind",
                   "call": "call", "call_many": "call_many", "release": "release", "decide": "decide",
                   "dispatch": "dispatch", "graph": "graph"}


def socket_definition(message: object) -> str | None:
    """The one $defs entry of the socket schema a message can match, read from its tag (its `op`, or for a reply
    its `ok`): every alternative of the schema's top-level oneOf is a $ref to one entry, each entry fixes its tag and
    allows no other fields, so a message matches the oneOf exactly when it matches this entry. Checking one entry is
    about ten times quicker than the oneOf. None when the tag names no entry: the whole schema is then checked, and
    refuses the message as before."""
    if not isinstance(message, Mapping):
        return None
    if "op" in message:
        op = message["op"]
        if not isinstance(op, str):
            return None
        if op == "hello":
            return "engine_hello" if "adapters_version" in message else "hello"
        return _OP_DEFINITIONS.get(op)
    ok = message.get("ok")
    if ok is True:
        return "ok_reply"
    if ok is False:
        return "failure_reply"
    return None


def validate_message(message: object, where: str, schema: SchemaName = "socket",
                     definition: str | None = None) -> dict[str, Any]:
    """The message as a dict if it matches `schema` (or, with `definition`, that one of its $defs); InvalidRequest
    (with every problem listed) if not. A socket message without a `definition` is checked against the entry its tag
    names (socket_definition), which accepts exactly what the whole schema accepts."""
    if schema == "socket" and definition is None:
        definition = socket_definition(message)
    try:
        contract.validate(schema, message, where, definition)
    except ContractError as error:
        raise InvalidRequest(str(error)) from error
    return dict(message)  # type: ignore[call-overload]


def encode(message: Mapping[str, Any], schema: SchemaName = "socket", definition: str | None = None) -> bytes:
    """One frame: the message checked against `schema` (contract/schemas/<schema>.schema.json), or against one of
    its $defs when `definition` names the shape the sender knows the message has (quicker than the schema's oneOf
    of every shape), packed with msgpack, behind its 4-byte length. Raises InvalidRequest for a message that does
    not match, holds a value msgpack cannot pack (a set, an integer over 64 bits, ...), or packs larger than
    64 MiB."""
    checked = validate_message(dict(message), f"outgoing {schema} message", schema, definition)
    try:
        payload = msgpack.packb(checked, use_bin_type=True)
    except (TypeError, OverflowError, ValueError) as error:
        raise InvalidRequest(f"The outgoing {schema} message cannot be packed with msgpack: "
                             f"{type(error).__name__}: {error}") from error
    if len(payload) > MAX_FRAME:
        raise InvalidRequest(f"Socket message of {len(payload)} bytes is over the 64 MiB limit")
    return HEADER.pack(len(payload)) + payload


def unpack(payload: bytes) -> object:
    """A frame body unpacked with msgpack, not yet checked against any schema."""
    try:
        return msgpack.unpackb(payload, raw=False)
    except (msgpack.UnpackException, ValueError) as error:
        raise InvalidRequest(f"Socket frame is not valid msgpack: {error}") from error


def decode(payload: bytes, schema: SchemaName = "socket") -> dict[str, Any]:
    return validate_message(unpack(payload), f"incoming {schema} message", schema)


def _length(header: bytes) -> int:
    (length,) = HEADER.unpack(header)
    if length > MAX_FRAME:
        raise InvalidRequest(f"Socket frame of {length} bytes is over the 64 MiB limit")
    return length


def read_frame(read_exactly: Callable[[int], bytes], schema: SchemaName = "socket") -> dict[str, Any]:
    try:
        header = read_exactly(HEADER.size)
        return decode(read_exactly(_length(header)), schema)
    except EOFError as error:
        raise EngineError("The engine socket closed") from error


async def read_body_async(reader: asyncio.StreamReader) -> bytes | None:
    """The next frame's body, not yet unpacked or checked (None at a clean end of stream), for a reader that must
    see the message's id before deciding how to refuse a message that does not match its schema."""
    try:
        header = await reader.readexactly(HEADER.size)
    except asyncio.IncompleteReadError as error:
        if error.partial:
            raise InvalidRequest("Socket closed in the middle of a frame header") from error
        return None
    length = _length(header)
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as error:
        raise InvalidRequest(
            f"Socket closed in the middle of a frame body (got {len(error.partial)} of {length} bytes)"
        ) from error


async def read_frame_async(reader: asyncio.StreamReader, schema: SchemaName = "socket") -> dict[str, Any] | None:
    body = await read_body_async(reader)
    return None if body is None else decode(body, schema)


def hello(adapters_version: int | None = None) -> dict[str, Any]:
    message: dict[str, Any] = {"op": "hello", "version": PROTOCOL_VERSION}
    if adapters_version is not None:
        message["adapters_version"] = adapters_version
    return message


def check_hello(message: Mapping[str, Any]) -> None:
    if message.get("op") != "hello":
        raise ProtocolMismatch(f"Expected a hello, got {message.get('op')!r}")
    if message.get("version") != PROTOCOL_VERSION:
        raise ProtocolMismatch(f"This side speaks protocol {PROTOCOL_VERSION}, the other side "
                               f"{message.get('version')}; upgrade the older one")


def ok(id: int, result: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": id, "ok": True, "result": dict(result)}


def fail(id: int, error: JeffError) -> dict[str, Any]:
    return {"id": id, "ok": False, "error": error_to_wire(error)}
