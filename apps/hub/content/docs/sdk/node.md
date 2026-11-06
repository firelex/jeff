---
title: Node SDK
---
# Node SDK

`jeff-sdk` is the Node SDK, for Node 22 or later. It is written in TypeScript and comes with its type declarations.
This page covers details specific to Node; the topic pages ([Connect](/docs/sdk/connect),
[Functions](/docs/sdk/functions), ...) show every feature in every language.

## Install

The package is on npm as `jeff-sdk`, the same name as the Python package:

```bash
npm install jeff-sdk
```

Its source is in the repository [github.com/firelex/jeff](https://github.com/firelex/jeff), in `packages/node`.

To run the models [on your own machine](/docs/sdk/connect#section-on-your-machine-free), install uv once
(`curl -LsSf https://astral.sh/uv/install.sh | sh`, see [docs.astral.sh/uv](https://docs.astral.sh/uv/)) and call
local mode with the adapters you use: `await Jeff.local(["guard"])`. The model runs only in the Jeff engine, which
is written in Python; the Node SDK finds the engine running for you on this machine, or starts it with uv (the
first start downloads about 2 GB, once), and talks to it. It runs on a Mac with Apple silicon or on Linux with an
NVIDIA GPU. To use an engine you start yourself with `jeff runtime serve`, pass its socket instead:
`await Jeff.local(socketPath, { timeoutMs })` ([Using an engine you start yourself](/docs/sdk/connect#section-using-an-engine-you-start-yourself)).

## Idioms

Every method that reaches an engine or a server returns a promise: `await` it. Close the client with
`await jeff.close()` when you are done, and a bound function you no longer need on the channel with
`await bound.close()`. A token function, an escalation step's `ask` function and a graph's code function may each be
async. Names follow JavaScript's conventions: `timeoutMs` for `timeout_s`, `onLowConfidence` for `on_low_confidence`,
`decidedBy` for `decided_by`, `isSupported` for `is_supported`, and so on.

JavaScript lists an object key that looks like a whole number (`"2"`, `"10"`) before every other key. Where that
matters, the SDK provides lists that preserve the original order (a question's `stateEntries` and `optionEntries`) or documents the limitation:
see [the order of probabilities](/docs/sdk/functions#section-the-order-of-probabilities-in-node),
[the question an llm or human step is given](/docs/sdk/escalation#section-the-question-an-llm-or-human-step-is-given)
and [node names that look like numbers](/docs/sdk/graphs#section-node-names-that-look-like-numbers-in-node).

## Errors

Every error is a `JeffError` subclass, exported by `jeff-sdk` with the same name as in the
[list of errors](/docs/sdk/errors) and the same message as the Python SDK. Check its class with `instanceof` (for
example `error instanceof LowConfidence`) or read `error.type`. `error.details` holds the same details as the API's
error body, and `error.requestId` the request ID of the server answer that carried it (through the API; otherwise
`null`).

## Settings

- **Timeouts**: `timeoutMs`, in milliseconds, required by `Jeff.local` with a socket and by `Jeff.remote`: a finite
  number above 0 and at most 2147483647 (about 24.8 days, the longest timer Node keeps), or `InvalidRequest`.
  [What the timeout bounds](/docs/sdk/connect#section-what-the-timeout-bounds) explains the phases. `Jeff.local`
  with a list of adapters takes an optional `StartOptions`, `{ timeoutMs, startTimeoutMs }`: whole numbers of
  milliseconds of at least 1 and at most 2147483647, by default 60,000 and 1,800,000 (30 minutes); see
  [what local mode does](/docs/sdk/connect#section-what-local-mode-does-in-node-rust-c-and-java).
- **Proxies**: `HTTPS_PROXY`, `HTTP_PROXY` (and `ALL_PROXY` for HTTP requests) and `NO_PROXY`, lower-case forms first,
  with the same choice of proxy and the same `NO_PROXY` matching as the Python SDK. SOCKS proxies are refused, and the
  operating system's own proxy settings are not read.
- **Limits**: at most 256 calls or inputs in one request through the API.

## Testing code that uses the SDK

`FakeEngine`, from `jeff-sdk/testing`, is an engine whose answers come from a function you supply; pass it to
`Jeff.create` with a catalogue to test your own code without a model.
