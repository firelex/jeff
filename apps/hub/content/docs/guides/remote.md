---
title: Through the API
---
# Through the API

`jeff.Jeff.remote(url, token, timeout_s=...)` gives Python code the same interface as running a model in this process
(`Jeff.local`) — binding a function, labels, escalation policies, dispatch and graphs all work the same way —
except that every decision is made by calling a Jeff server instead of an engine in this process:

```python
import jeff

remote = jeff.Jeff.remote("http://localhost:8000", token=None, timeout_s=30.0)
decision = remote.function("triage.route", context=context, labels=teams)(message)
remote.close()  # or: with jeff.Jeff.remote(...) as remote:
```

`token` is the server's bearer credential: a string (an API key or a hub-signed token), a callable returning one
(called before each request over plain HTTP, so a refreshed token is picked up), or `None` — only for a server with
no credentials configured. `timeout_s` is required and bounds how long each request may take. Connecting reads
`GET /v1/functions` once and checks every function the server exposes against this process's own catalogue (or the
one `catalogue=` points at): a version mismatch raises `ContractError` naming the function and both versions, rather
than binding with the wrong manifest.

## What costs a request

Each operation costs one request to the server: `decide`, a bound function's call or `many()`, an interface
shorthand (`classify`, `route`, `rate`, `detect`, `is_supported`, `equivalent`, `requires_escalation`), `dispatch`, and a graph with no code nodes. A policy of
`"return"`, `"error"` or `escalate(function_step(...))` runs on the server, in the same request. A policy with
an `llm(...)`, `human(...)` or `remote(...)` step runs in this process instead, on the abstained decision the server
returned — only this process holds those callables — which costs one further request per escalation step that
actually runs (see [Escalation](/docs/guides/escalation)).

A graph with no code nodes runs as one request, entirely on the server; a graph with any code node runs stage by
stage from this process instead — one request per stage that has model nodes, with each stage's code node run here,
because the code can only run where it was defined (see [Graphs](/docs/guides/graphs)).

Every error uses the same `jeff.errors` class and message as a call on your machine, and carries the HTTP
response's `x-request-id` as its `request_id` attribute.

One request carries at most 256 calls: `decide` with more than 256 calls, or a bound function's `many()` with more
than 256 inputs, raises `InvalidRequest` naming the limit before sending anything — it is never silently split into
several requests. `decide([])` and `many([])` answer `[]` without a request, as on your machine.

## The low-latency channel

Pass `channel=True` to use the server's WebSocket channel (`/v1/channel`, see
[The low-latency channel](/docs/guides/channel)) instead of REST — one long-lived, authenticated connection, where a bound
function becomes a *handle* on the server rather than a request on every call:

```python
with jeff.Jeff.remote("{{origin}}", token=get_token, timeout_s=10.0, channel=True) as remote:
    route = remote.function("triage.route", context=context, labels=teams)  # one `bind` frame
    decision = route(message)                                              # one `call` frame
```

When `token` is callable, over the channel it is called for the token sent with `auth`, again from 60 seconds
before that token's `exp` (retried every 5 seconds, until a token expiring later is returned, or `exp` passes), and
again for each `GET /v1/functions` — never before every call, as with `channel=False`. Keep this machine's clock
synchronised with the server's: the refresh window is measured against this machine's clock, while the server
checks expiry with its own.

## Next steps

- [The low-latency channel](/docs/guides/channel): the wire protocol `channel=True` uses.
- [MCP](/docs/guides/mcp): calling functions as tools instead of through this SDK.
- [Escalation](/docs/guides/escalation): the `llm`, `human` and `remote` steps that run in this process.

