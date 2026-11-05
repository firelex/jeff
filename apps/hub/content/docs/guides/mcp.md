---
title: MCP
---
# MCP

Jeff's functions can be called as MCP (Model Context Protocol) tools, so any MCP-aware agent can use them through
the standard MCP interface. The server mounts these tools at `/mcp` (streamable HTTP), behind the same bearer
credentials and rate limiter as `/v1`.

## The tools

There is one tool per function this server can currently call — every function of every installed adapter your token may call — named from its function
ID with the leading `jeff.` removed and every `.` and `-` replaced with `_`: `jeff.support.triage.route` becomes
`support_triage_route`. Each tool's description comes from the function's manifest: its instructions, its interface,
its context and input fields, and what its labels mean. Its input schema takes:

- `input` (required)
- `context` (required when the manifest requires a context field)
- `labels` (only for a function whose caller supplies or chooses the labels)
- `on_low_confidence`: `"return"` (the default) or `"error"`, as in `POST /v1/decide`

Two further tools are always available:

- `jeff_dispatch` — route an input through the router functions to the function that should answer it (the
  `dispatch` shape, see [Functions and /v1/decide](/docs/guides/decide)).
- `jeff_list_functions` — the same listing as `GET /v1/functions`.

## Calling a tool

A tool call's arguments are checked for their shape (input present, context and labels in the forms
`/v1/decide` takes), then bound exactly as a call to `/v1/decide` would be — so a bad context or label set is
refused with the same error type (`MissingContext`, `UnknownContext`, `MissingLabels`, `InvalidLabels`,
`TooManyLabels`) a direct `/v1/decide` call would get.

A tool call returns the whole `Decision` — `abstained` included, so the agent can see when Jeff is not confident
enough to act — as both the tool's text and its structured content. A `JeffError` becomes a tool error whose text
starts with the error's type (for example `NotLicensed: ...`), rather than an exception the agent's host has to
unwrap.

Every POST to `/mcp` — whatever MCP message it carries — counts as one request against the caller's rate limit, so a
fresh client's first tool call typically uses 3 to 4 requests from that budget: initialising the MCP session, the
call itself, and the SDK's own `tools/list`, which most MCP clients run once before calling a tool they have not
seen listed.

## Serving the same tools over stdio

For a local agent that prefers stdio to HTTP, `jeff mcp` serves the same tools without a server in
between (install the package with its extra first: `pip install "jeff-sdk[mcp]"`):

```bash
jeff mcp --server {{origin}} --token-env JEFF_API_KEY --timeout-s 30
# or straight against an engine process, with no server in between:
jeff mcp --socket /path/e.sock --timeout-s 30
```

`--server` and `--socket` are mutually exclusive. `--token-env NAME` names an environment variable holding the
server's bearer token, so the token itself never appears in the process list; it is read again on every request, so
a refreshed token is picked up, and an unset variable fails that request with `Unauthorised` naming it. Leave
`--token-env` out for a server with no credentials, and leave it out entirely with `--socket` (an engine socket
takes no token).

## Next steps

- [Functions and /v1/decide](/docs/guides/decide): the same calls, without MCP.
- [Errors](/docs/guides/errors): the error catalogue a `JeffError` tool error's text names.

