---
title: Python SDK
---
# Python SDK

`jeff-sdk` is the Python SDK, for Python 3.13 or later, imported as `jeff`. This page covers details specific to
Python; the topic pages ([Connect](/docs/sdk/connect), [Functions](/docs/sdk/functions), ...) show every feature in
every language.

## Install

```bash
pip install jeff-sdk                   # call a Jeff server
pip install jeff-sdk jeff-runtime      # also run the models in your own process
```

Both are on PyPI; their source is in the repository
[github.com/firelex/jeff](https://github.com/firelex/jeff). [Install](/docs/sdk/install) lists the extras (`mcp`, and `jeff-runtime`'s `mac` and `cuda`).

## What only Python has

The engine is written in Python, so the Python package offers a few features the other SDKs do not:

- `Jeff.local(adapters)` runs the models in your own process; the other SDKs attach to an engine process
  ([Connect](/docs/sdk/connect#section-python-in-your-own-process)).
- A [decision log](/docs/sdk/decision-log), `DecisionLog`.
- An `authorise` function on a client made from your own engine, and `with_authorise` ([Engines](/docs/sdk/engines)).
- The `jeff` command: `jeff test`, `jeff calibrate`, `jeff gen`, `jeff mcp` and `jeff runtime`
  ([The jeff command](/docs/sdk/command)).

## Idioms

Every call blocks until it receives an answer; there is no async API. A remote client and a bound function are context
managers: leave the `with` block, or call `close()`, to release the connection or the channel handle. A client made
with `Jeff.local` holds the models in your process and needs no closing. A bound function is called directly,
`guard(text)`, rather than with a `call` method.

## Errors

Every error is a subclass of `jeff.JeffError`, importable from `jeff` (`jeff.UnknownFunction`, `jeff.LowConfidence`,
...), with `request_id` and the typed attributes [Errors](/docs/sdk/errors) lists.

## Settings

- **Timeouts**: `timeout_s`, in seconds, a finite number above 0, required by `Jeff.remote` and `Jeff.connect`.
  [What the timeout bounds](/docs/sdk/connect#section-what-the-timeout-bounds) explains the phases.
- **Proxies**: HTTP requests use httpx's rules (`trust_env`): `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` and
  `NO_PROXY`, lower-case forms first; when none is set, the operating system's own proxy settings are used. The
  channel uses the websockets library's rules. SOCKS proxies are refused.
- **Limits**: at most 256 calls or inputs in one request through the API; at most 1,024 bound-function handles on one
  channel connection (`TooManyHandles`).
