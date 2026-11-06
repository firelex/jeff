---
title: C# SDK
---
# C# SDK

`Jeff.Sdk` is the C# SDK. It targets `net8.0`, so it runs on .NET 8 or later, and has been tested on .NET 10. This
page covers details specific to C#; the topic pages ([Connect](/docs/sdk/connect),
[Functions](/docs/sdk/functions), ...) show every feature in every language.

## Install

`Jeff.Sdk` is on NuGet:

```bash
dotnet add package Jeff.Sdk
```

Its source is in the repository [github.com/firelex/jeff](https://github.com/firelex/jeff), in `packages/dotnet`.

To run the models [on your own machine](/docs/sdk/connect#section-on-your-machine-free), install uv once
(`curl -LsSf https://astral.sh/uv/install.sh | sh`, see [docs.astral.sh/uv](https://docs.astral.sh/uv/)) and call
local mode with the adapters you use: `await JeffClient.LocalAsync(new[] { "guard" })`. The model runs only in the Jeff engine, which
is written in Python; the C# SDK finds the engine running for you on this machine, or starts it with uv (the
first start downloads about 2 GB, once), and talks to it. It runs on a Mac with Apple silicon or on Linux with an
NVIDIA GPU. To use an engine you start yourself with `jeff runtime serve`, pass its socket instead:
`JeffClient.LocalAsync(socketPath, new LocalOptions { Timeout })` ([Using an engine you start yourself](/docs/sdk/connect#section-using-an-engine-you-start-yourself)).

## Idioms

Every method that reaches an engine or a server is asynchronous (`FunctionAsync`, `CallAsync`, `DecideAsync`,
`RunGraphAsync`, ...) and takes a `CancellationToken`: cancelling fails only that call, with
`OperationCanceledException`, and the connection continues serving other calls. The client and a bound function are
`IAsyncDisposable`: `await using` them. On the channel, disposing a bound function releases its handle (one you drop
without disposing is released by its finalizer, before the channel's next request).

<!-- snippet: sdk/csharp-1.cs -->

JSON values are `System.Text.Json.Nodes` nodes (a context or labels is a `JsonObject`); the SDK copies every node you
pass it, so you may reuse a context object for many calls. Every ordered map preserves its order, even for keys that
look like numbers. An `Llm` or `Human` step is an `Ask` delegate, `(Question, CancellationToken) => Task<string?>`, and
a code node a `CodeFunction`. The SDK starts no threads of its own, and every `await` inside it uses
`ConfigureAwait(false)`, so blocking on a call from a UI thread does not deadlock (prefer `await` anyway).

## Errors

Every error is a `JeffError` subclass named as in [Errors](/docs/sdk/errors) (`NotInstalled`, `LowConfidence`, ...),
with the same message and `Details` the Python SDK gives for the same mistake; `Type` is its name. `RequestId` is the
ID the server gave the request that failed, or `null` for an error raised in your own program. Typed properties such
as `RateLimited.RetryAfterS` and `NotInstalled.FunctionId` throw a `ContractError` naming the field when a server sent
an error without it; they never make up a value. No error message includes a token.

## Settings

- **Timeouts**: `Timeout`, a `TimeSpan` (`new LocalOptions { Timeout = ... }`, `new RemoteOptions { Timeout = ... }`),
  positive and at most 2,147,483,647 milliseconds (about 24.8 days), else `InvalidRequest`. Local mode with a list
  of adapters takes an optional `new StartOptions { Timeout = ..., StartTimeout = ... }`, each a whole number of
  milliseconds of at least 1, by default 1 minute and 30 minutes; see
  [what local mode does](/docs/sdk/connect#section-what-local-mode-does-in-node-rust-c-and-java).
- **The channel's keepalive** needs .NET 9 or later at run time: the .NET 8 `ClientWebSocket` cannot send pings, so on
  .NET 8 the channel sends none and a server that stops answering is noticed only by each request's own timeout.
- **Proxies**: `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` (HTTP requests only) and `NO_PROXY`, in upper or lower case (a
  lower-case one wins, and an empty lower-case one removes the proxy), with the same choice and the same `NO_PROXY`
  matching as Python. TLS is still checked end to end through a CONNECT tunnel. The operating system's own proxy
  settings are not read: with no proxy variable set there is no proxy. SOCKS proxies are refused by name.
- **Limits**: values nested past a fixed depth are refused, naming the limit: 512 levels for MessagePack, 512 for
  YAML, 1,000 for JSON read from a server or a file, and 256 for graph documents. An HTTP answer body is refused past
  64 MiB, as sent and after each `gzip` or `deflate` layer is undone (`BackendUnavailable`, and the client is closed).
  A manifest or graph file whose YAML aliases expand to more than 1,000,000 values is refused with `ManifestError`.

Two failures on the channel have no caller to report to, and the SDK writes them to standard error (`Console.Error`):
a failed release of a bound function you dropped without disposing it, and a failed token refresh that the channel
will try again. Redirect `Console.Error` to route them elsewhere.

## Testing code that uses the SDK

`Jeff.Sdk.Testing.FakeEngine` is an engine whose answers come from a function you supply; pass it to
`JeffClient.CreateAsync` with a catalogue to test your own code without a model.
