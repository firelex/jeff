---
title: The low-latency channel
---
# The low-latency channel

There are three ways to access the same engine:

| Access method | For | Per-call cost |
|---|---|---|
| REST/JSON (`/v1/decide`) | Callers using any language or tool | HTTP and JSON, auth and binding checks on every request |
| **The channel** | Services calling Jeff at high rates over the network | One connection, one authentication step, a small binary frame per call |
| On your machine (free): in process, or the engine's own socket | Callers requiring minimum overhead | No network |

The channel is a WebSocket at `/v1/channel`, carrying the same length-prefixed msgpack frames as the engine's own
Unix-socket protocol (`contract/socket.md`), extended with the operations below. Each WebSocket message carries
exactly one such frame: a 4-byte big-endian length, then the msgpack body.

## Opening a connection

1. The client sends `{"op": "hello", "version": 2}`; the server replies with the same message. A mismatched version closes the
   connection.
2. On a server with credentials configured, the client sends `{"op": "auth", "id": <id>, "token": "<token>"}` — any
   credential `Authorization: Bearer` accepts on the HTTP API. Until `auth` succeeds, every other request fails
   with `Unauthorised`. A connection with no successful `auth` within 10 seconds is closed. On a server without
   credentials, `auth` is optional.
3. `{"op": "refresh", "id", "token"}` replaces the connection's token before it expires (the Python SDK does this
   60 seconds before `exp`). When a token expires, the server finishes the requests it is already running, sends an
   `Unauthorised` failure, and closes the connection — there is no automatic reconnect; connect again with
   `Jeff.remote(..., channel=True)` (see [Through the API](/docs/guides/remote)).

## Binding a function once, then calling it by handle

```
bind   {op: "bind", id, function, context?, labels?, on_low_confidence?}  -> {handle, function, version}
call   {op: "call", id, handle, input}                                   -> {decision}
call_many {op: "call_many", id, handle, inputs}  (1-256 inputs)           -> {decisions}
release {op: "release", id, handle}                                      -> {}
```

`bind` checks the function, the caller's licence for it, whether its adapter is served, the context, the labels and
the policy once — the same checks and errors as `POST /v1/decide` for the same call. The handle is pinned to one
engine process, so every `call` on it reuses the same cached prompt prefix. A connection holds at most 1,024 live
handles (`TooManyHandles` on the next `bind`) and at most 256 outstanding requests at once (`RateLimited` on the
next one); handles expire when the connection closes.

`decide`, `dispatch` and `graph` are also available directly on the channel, for calls without a handle, taking the
same shapes as `POST /v1/decide`:

```
decide   {op: "decide", id, calls, on_low_confidence?}                       -> {decisions}
dispatch {op: "dispatch", id, dispatch, on_low_confidence?}                  -> {decision}
graph    {op: "graph", id, graph, input, params, on_low_confidence_by_node?} -> {graph_result}
```

## Errors

Every reply is `{"id", "ok": true, "result"}` or `{"id", "ok": false, "error": {"type", "message", "details"}}` —
the same typed errors as the HTTP API, since there is no HTTP response to carry an `x-request-id`. Each connection
instead has its own short id (`channel_id`, in every error's details), recorded in the server's logs.

## Next steps

- [Through the API](/docs/guides/remote): `Jeff.remote(url, token, timeout_s=..., channel=True)`, which speaks this protocol.
- [Errors](/docs/guides/errors): including `TooManyHandles`, which only the channel can raise.

