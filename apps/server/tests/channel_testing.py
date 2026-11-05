"""Helpers for the channel's tests (test_channel.py and others that open /v1/channel): a test client over
Starlette's TestClient WebSocket, the check of a failure reply, and opening a channel (hello, then auth if given)."""

from typing import Any

import anyio

from jeff.protocol import HEADER, PROTOCOL_VERSION, decode, encode


class Channel:
    """A test client over Starlette's TestClient WebSocket: sends frames, reads frames (never waiting forever)."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws

    def send(self, message: dict[str, Any]) -> None:
        self.ws.send_bytes(encode(message))

    def send_raw(self, body: bytes) -> None:
        self.ws.send_bytes(HEADER.pack(len(body)) + body)

    def message(self, timeout: float = 5.0) -> dict[str, Any]:
        async def receive() -> dict[str, Any]:
            with anyio.fail_after(timeout):
                return dict(await self.ws._send_rx.receive())
        return self.ws.portal.call(receive)  # type: ignore[no-any-return]

    def receive(self, timeout: float = 5.0) -> dict[str, Any]:
        message = self.message(timeout)
        assert message["type"] == "websocket.send", message
        data: bytes = message["bytes"]
        (length,) = HEADER.unpack(data[:HEADER.size])
        assert length == len(data) - HEADER.size
        return decode(data[HEADER.size:])

    def receive_all(self, count: int) -> dict[int, dict[str, Any]]:
        replies = [self.receive() for _ in range(count)]
        by_id = {reply["id"]: reply for reply in replies}
        assert len(by_id) == count
        return by_id

    def request(self, message: dict[str, Any]) -> dict[str, Any]:
        self.send(message)
        reply = self.receive()
        assert reply["id"] == message["id"], reply
        return reply

    def result(self, message: dict[str, Any]) -> dict[str, Any]:
        reply = self.request(message)
        assert reply["ok"] is True, reply
        return dict(reply["result"])

    def error(self, message: dict[str, Any], error_type: str, *named: str) -> dict[str, Any]:
        return assert_error(self.request(message), error_type, *named)

    def closed(self) -> None:
        message = self.message()
        assert message["type"] == "websocket.close", message

    def hello(self) -> dict[str, Any]:
        self.send({"op": "hello", "version": PROTOCOL_VERSION})
        return self.receive()


def assert_error(reply: dict[str, Any], error_type: str, *named: str) -> dict[str, Any]:
    """The failure's error, checked, without the connection id every channel failure carries in its details (so it
    can be compared with the HTTP API's error for the same request)."""
    assert reply["ok"] is False, reply
    error = reply["error"]
    assert error["type"] == error_type, error
    for text in named:
        assert text in error["message"], error["message"]
    details = dict(error["details"])
    assert isinstance(details.pop("channel_id"), str), error
    return {**error, "details": details}


def start(ws: Any, token: str | None = None) -> Channel:
    channel = Channel(ws)
    assert channel.hello() == {"op": "hello", "version": PROTOCOL_VERSION}
    if token is not None:
        assert channel.result({"op": "auth", "id": 1, "token": token}) == {}
    return channel
