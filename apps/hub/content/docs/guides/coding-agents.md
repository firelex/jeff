---
title: Jeff with coding agents
---
# Jeff with coding agents

Jeff cannot serve as the underlying model for Claude Code, Cursor, Codex or other coding agents. It does
not chat, write code or stream text; each function returns one typed decision. Coding agents can use
Jeff's functions as tools and refer to these docs when writing code that calls Jeff.

## Give the agent Jeff's functions as tools (MCP)

Jeff exposes its functions as MCP (Model Context Protocol) tools, one tool per function, plus `jeff_dispatch` (let
Jeff pick the function) and `jeff_list_functions` (the same listing as `GET /v1/functions`). Any MCP-aware agent can
then call the prompt injection guard on a piece of text without knowing Jeff's API. Each tool returns
the whole decision, `abstained` included, so the agent can identify low-confidence decisions. There are two ways to connect.

**Over HTTP.** The Jeff server itself serves the tools at `{{origin}}/mcp` (streamable HTTP), behind the same
bearer token as `/v1/decide`. Add it to your agent as a remote MCP server with the header
`Authorization: Bearer <your token>`.

**Over stdio, with `jeff mcp`.** For an agent that starts its MCP servers as local commands, the `jeff` command
serves the same tools and calls the server for each one. It is included in the Python package `jeff-sdk` with its `mcp` extra:

```bash
pip install "jeff-sdk[mcp]"
```

Then add it to the agent's MCP settings. Most agents accept a block like this:

```json
{
  "mcpServers": {
    "jeff": {
      "command": "jeff",
      "args": ["mcp", "--server", "{{origin}}", "--token-env", "JEFF_API_KEY", "--timeout-s", "30"],
      "env": {"JEFF_API_KEY": "<your token>"}
    }
  }
}
```

`--token-env` names the environment variable that holds the token, so the token never appears in the list of
running processes. `--timeout-s` is required: it bounds how long each call may take. [MCP](/docs/guides/mcp)
describes the tools, their arguments and their errors in full.

## Give the agent the docs

When the agent writes code that calls Jeff, point it at:

- this site's pages, starting with the [Quick start](/docs/guides/quickstart) and
  [Functions and /v1/decide](/docs/guides/decide);
- the OpenAPI description of every endpoint, request, response and error, at `{{origin}}/openapi.json` (also shown
  as the [API reference](/docs/api));
- the [catalogue](/catalogue) page of each function it will call: its context fields, its input and its answers.

## When Jeff is worth reaching for

An agent or application can use Jeff to:

- check text before a model acts on it (a prompt injection, a request it must not follow);
- pick where something goes (which tool, which team, which queue) and know how confident the choice is;
- put something on a scale (how urgent, how risky) and compare it with a threshold;
- avoid asking a chat model to "answer in JSON" and checking whether it did: a function can only return its own
  answers.

> **No agent skill yet.** An installable skill that teaches coding agents the Jeff API is on the
> [roadmap](/docs/guides/gaps).
