import asyncio
import io
import struct

import msgpack
import pytest

from jeff import contract
from jeff.contract import first_problem
from jeff.errors import Busy, EngineError, InvalidRequest, ProtocolMismatch
from jeff.protocol import (MAX_FRAME, PROTOCOL_VERSION, check_hello, decode, encode, fail, hello, ok, read_body_async,
                           read_frame, read_frame_async, unpack, validate_message)

PREDICT = {"op": "predict", "id": 7, "adapter": "jeff.safety.spam",
           "rows": [{"state": {"channel": "sms", "message": "hi"}, "question": {"type": "noul", "instructions": "?"}}]}


def reader(data: bytes):
    buffer = io.BytesIO(data)

    def read_exactly(n: int) -> bytes:
        chunk = buffer.read(n)
        if len(chunk) < n:
            raise EOFError
        return chunk
    return read_exactly


def test_round_trip() -> None:
    assert read_frame(reader(encode(PREDICT))) == PREDICT


def test_frame_is_length_then_msgpack() -> None:
    frame = encode(hello())
    (length,) = struct.unpack(">I", frame[:4])
    assert length == len(frame) - 4 and msgpack.unpackb(frame[4:]) == {"op": "hello", "version": PROTOCOL_VERSION}


def test_invalid_message_is_refused_on_both_sides() -> None:
    with pytest.raises(InvalidRequest, match="adapter"):
        encode({"op": "predict", "id": 1, "rows": []})
    bad = msgpack.packb({"op": "predict", "id": 1, "adapter": 3, "rows": []})
    with pytest.raises(InvalidRequest):
        decode(bad)


def test_oversized_frame_is_refused() -> None:
    with pytest.raises(InvalidRequest, match="64 MiB"):
        read_frame(reader(struct.pack(">I", MAX_FRAME + 1)))


def test_closed_socket_is_an_engine_error() -> None:
    with pytest.raises(EngineError, match="closed"):
        read_frame(reader(b""))


def test_hello_mismatch_names_both_versions() -> None:
    with pytest.raises(ProtocolMismatch, match=f"{PROTOCOL_VERSION}.*99|99.*{PROTOCOL_VERSION}"):
        check_hello({"op": "hello", "version": 99})


def test_replies() -> None:
    assert ok(3, {"adapters": [], "adapters_version": 0})["ok"] is True
    body = fail(3, Busy("full", 2.5))
    assert body == {"id": 3, "ok": False, "error": {"type": "Busy", "message": "full",
                                                     "details": {"retry_after_s": 2.5}}}
    encode(body)


def test_async_reader() -> None:
    async def run() -> object:
        stream = asyncio.StreamReader()
        stream.feed_data(encode(PREDICT))
        stream.feed_eof()
        first = await read_frame_async(stream)
        second = await read_frame_async(stream)
        return first, second
    assert asyncio.run(run()) == (PREDICT, None)


def test_async_reader_eof_mid_body_is_an_invalid_request() -> None:
    async def run() -> None:
        stream = asyncio.StreamReader()
        stream.feed_data(struct.pack(">I", 10) + b"abc")
        stream.feed_eof()
        await read_frame_async(stream)
    with pytest.raises(InvalidRequest, match="3 of 10"):
        asyncio.run(run())


# ---- final review fixes


def test_a_value_msgpack_cannot_pack_is_an_invalid_request_naming_the_problem() -> None:
    with_a_set = {**PREDICT, "rows": [{"state": {"tags": {"a", "b"}}, "question": {"type": "noul"}}]}
    with pytest.raises(InvalidRequest, match="msgpack.*set"):
        encode(with_a_set)
    too_big = {**PREDICT, "rows": [{"state": {"n": 2 ** 70}, "question": {"type": "noul"}}]}
    with pytest.raises(InvalidRequest, match="msgpack.*OverflowError.*Integer"):
        encode(too_big)


def test_encode_and_decode_take_the_schema_to_check_against() -> None:
    frame = encode(hello(), schema="socket")
    assert decode(frame[4:], schema="socket") == hello()
    with pytest.raises(InvalidRequest, match="manifest schema"):
        decode(frame[4:], schema="manifest")
    with pytest.raises(InvalidRequest, match="manifest schema"):
        encode(hello(), schema="manifest")


def test_read_body_async_returns_the_raw_body_for_the_caller_to_check() -> None:
    async def run() -> object:
        stream = asyncio.StreamReader()
        stream.feed_data(encode(PREDICT))
        stream.feed_eof()
        return await read_body_async(stream), await read_body_async(stream)
    body, end = asyncio.run(run())
    assert unpack(body) == PREDICT and end is None


@pytest.mark.parametrize(("message", "named"), [
    ({**PREDICT, "deadline_ms": 0}, "deadline_ms"),
    ({"op": "predict", "id": 4, "adapter": "a"}, "'rows' is a required property"),
    ({"op": "stats", "id": 4, "extra": 1}, "'extra' was unexpected"),
    ({"op": "nope", "id": 3}, "'nope' is not one of"),
])
def test_first_problem_names_the_field_not_every_alternative(message: dict, named: str) -> None:
    problem = first_problem("socket", message)
    assert named in problem and "\n" not in problem and len(problem) < 300


def test_version_2_refuses_a_version_1_peer() -> None:
    """Version 2 added the base served as adapter "base" and input_tokens in predict results; an engine and a client
    from before that refuse each other at hello."""
    assert PROTOCOL_VERSION == 2
    with pytest.raises(ProtocolMismatch, match="speaks protocol 2, the other side 1"):
        check_hello({"op": "hello", "version": 1})


SAMPLES: list[object] = [
    {"op": "hello", "version": 2}, {"op": "hello", "version": 2, "adapters_version": 0},
    {"op": "hello", "version": 2, "adapters_version": -1}, {"op": "hello", "version": 2, "extra": 1},
    {"op": "hello"}, {"op": "installed", "id": 1}, {"op": "stats", "id": 0}, {"op": "health", "id": 1, "x": 1},
    PREDICT, {**PREDICT, "deadline_ms": 0}, {**PREDICT, "rows": []}, {"op": "serve", "id": 2, "adapters": ["a"]},
    {"op": "serve", "id": 2, "adapters": [""]}, {"op": "auth", "id": 1, "token": "t"}, {"op": "refresh", "id": 1},
    {"op": "bind", "id": 1, "function": "f"}, {"op": "call", "id": 1, "handle": 1, "input": None},
    {"op": "call", "id": 1, "handle": 1}, {"op": "call_many", "id": 1, "handle": 1, "inputs": []},
    {"op": "release", "id": 1, "handle": 1}, {"op": "decide", "id": 1, "calls": [{"function": "f", "input": 1}]},
    {"op": "dispatch", "id": 1, "dispatch": {"input": 1, "interface": "rate"}},
    {"op": "graph", "id": 1, "graph": {"id": "g"}, "input": 1, "params": {}},
    {"id": 1, "ok": True, "result": {}}, {"id": 0, "ok": False, "error": {"type": "T", "message": "m", "details": {}}},
    {"id": 1, "ok": True, "result": []}, {"id": 1, "ok": 1, "result": {}}, {"id": 1, "ok": 0, "error": {}},
    {"id": 1, "ok": False, "error": {"type": "T", "message": "m"}}, {"op": "stats", "id": 1, "ok": True},
    {"id": 1, "ok": True, "result": {}, "op": "stats"}, {"op": "nope", "id": 1}, {"op": [1], "id": 1},
    {"op": None, "id": 1}, {"id": 1}, {}, [1, 2], "hello", None, 3,
]


@pytest.mark.parametrize("message", SAMPLES)
def test_checking_the_entry_a_message_names_accepts_exactly_what_the_whole_schema_accepts(message: object) -> None:
    """validate_message checks a socket message against the one $defs entry its tag names (socket_definition); it
    must accept and refuse exactly what the schema's oneOf of every shape does."""
    whole = contract.is_valid("socket", message)
    try:
        validate_message(message, "sample")
        quick = True
    except InvalidRequest:
        quick = False
    assert quick == whole, message


def test_every_alternative_of_the_socket_schema_is_one_tagged_closed_entry() -> None:
    """What makes socket_definition exact: each top-level alternative is a $ref to one entry, every entry allows no
    other fields, and its tag (op, or ok for a reply) is fixed."""
    schema = contract.load_schema("socket")
    referenced = []
    for alternative in schema["oneOf"]:
        assert set(alternative) == {"$ref"}, alternative
        referenced.append(alternative["$ref"].removeprefix("#/$defs/"))
    for name in referenced:
        entry = schema["$defs"][name]
        assert entry["additionalProperties"] is False, name
        tag = entry["properties"].get("op") or entry["properties"]["ok"]
        assert "const" in tag or "enum" in tag, name


def _tag_values(entry: dict) -> tuple[str, set]:
    """An entry's tag field and the values it allows."""
    field = "op" if "op" in entry["properties"] else "ok"
    tag = entry["properties"][field]
    return field, set(tag["enum"]) if "enum" in tag else {tag["const"]}


def test_entries_sharing_a_tag_value_can_never_both_match() -> None:
    """Two entries allowing the same tag value (hello and engine_hello) must each require a field the other does not
    allow, so no message matches both; then checking the entry socket_definition names is exact."""
    schema = contract.load_schema("socket")
    entries = {alternative["$ref"].removeprefix("#/$defs/"): schema["$defs"][alternative["$ref"].removeprefix("#/$defs/")]
               for alternative in schema["oneOf"]}
    names = sorted(entries)
    for index, first in enumerate(names):
        for second in names[index + 1:]:
            (field_a, values_a), (field_b, values_b) = _tag_values(entries[first]), _tag_values(entries[second])
            if field_a != field_b or not values_a & values_b:
                continue
            a, b = entries[first], entries[second]
            separated = any(key not in b["properties"] for key in a["required"]) or \
                any(key not in a["properties"] for key in b["required"])
            assert separated, (first, second)


def test_the_op_table_names_the_entry_whose_tag_allows_each_op() -> None:
    """socket_definition's table (_OP_DEFINITIONS) maps every op the schema allows, besides hello, to the one entry
    whose tag allows it, and nothing else."""
    from jeff.protocol import _OP_DEFINITIONS
    schema = contract.load_schema("socket")
    expected = {}
    for alternative in schema["oneOf"]:
        name = alternative["$ref"].removeprefix("#/$defs/")
        field, values = _tag_values(schema["$defs"][name])
        if field == "op":
            for op in values - {"hello"}:
                assert op not in expected, op
                expected[op] = name
    assert _OP_DEFINITIONS == expected
