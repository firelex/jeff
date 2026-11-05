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

To run the models [on your own machine](/docs/sdk/connect#section-on-your-machine-free) you also need the Jeff engine,
which is written in Python: install `jeff-sdk` and `jeff-runtime` as described in [Install](/docs/sdk/install), and
start it with `jeff runtime serve`. The model runs only in the engine; the Node SDK talks to it.

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

- **Timeouts**: `timeoutMs`, in milliseconds, required by `Jeff.local` and `Jeff.remote`: a finite number above 0 and
  at most 2147483647 (about 24.8 days, the longest timer Node keeps), or `InvalidRequest`.
  [What the timeout bounds](/docs/sdk/connect#section-what-the-timeout-bounds) explains the phases.
- **Proxies**: `HTTPS_PROXY`, `HTTP_PROXY` (and `ALL_PROXY` for HTTP requests) and `NO_PROXY`, lower-case forms first,
  with the same choice of proxy and the same `NO_PROXY` matching as the Python SDK. SOCKS proxies are refused, and the
  operating system's own proxy settings are not read.
- **Limits**: at most 256 calls or inputs in one request through the API.

## Testing code that uses the SDK

`FakeEngine`, from `jeff-sdk/testing`, is an engine whose answers come from a function you supply; pass it to
`Jeff.create` with a catalogue to test your own code without a model.
