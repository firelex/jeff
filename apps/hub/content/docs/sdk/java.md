---
title: Java SDK
---
# Java SDK

`ai.jeffhub:jeff-sdk` is the Java SDK (its Java package is `ai.jeff.sdk`). It compiles for Java 21 or later. This page covers details specific to Java; the
topic pages ([Connect](/docs/sdk/connect), [Functions](/docs/sdk/functions), ...) show every feature in every
language.

## Install

The package is on Maven Central as `ai.jeffhub:jeff-sdk`. With Maven:

```xml illustrative
<dependency>
  <groupId>ai.jeffhub</groupId>
  <artifactId>jeff-sdk</artifactId>
  <version>1.4.1</version>
</dependency>
```

With Gradle:

```kotlin illustrative
implementation("ai.jeffhub:jeff-sdk:1.4.1")
```

It brings two libraries with it: Jackson's streaming parser and generator, `jackson-core` 2.22.4 (not
`jackson-databind`), and `org.yaml:snakeyaml` 2.7. The jar also carries a copy of the catalogue. Its source is in the
repository [github.com/firelex/jeff](https://github.com/firelex/jeff), in `packages/java`.

To run the models [on your own machine](/docs/sdk/connect#section-on-your-machine-free), install uv once
(`curl -LsSf https://astral.sh/uv/install.sh | sh`, see [docs.astral.sh/uv](https://docs.astral.sh/uv/)) and call
local mode with the adapters you use: `Jeff.local(List.of("guard"))`. The model runs only in the Jeff engine, which
is written in Python; the Java SDK finds the engine running for you on this machine, or starts it with uv (the
first start downloads about 2 GB, once), and talks to it. It runs on a Mac with Apple silicon or on Linux with an
NVIDIA GPU. To use an engine you start yourself with `jeff runtime serve`, pass its socket instead:
`Jeff.local(socketPath, LocalOptions.timeoutMs(ms))` ([Using an engine you start yourself](/docs/sdk/connect#section-using-an-engine-you-start-yourself)).

## Idioms

Every method blocks until it has its answer, as in the Python SDK, and each has an `...Async` twin returning a
`CompletableFuture` (`callAsync`, `manyAsync`, `decideAsync`, `classifyAsync`, `runGraphAsync`, ...), which runs the
call on a virtual thread. `Async.join(future)` waits for one and throws its `JeffError` directly. Many threads may
share one `Jeff`: one local client holds one connection to the engine, and their requests are multiplexed on it, so
the engine can batch them. A client and a bound function are `AutoCloseable`: use them in try-with-resources. No
thread the SDK starts keeps the JVM alive.

<!-- snippet: sdk/java-1.java -->

Order matters to the model (it reads options and fields in the order it was trained on), so give caller labels and
any map inside an input as a `LinkedHashMap` (or another map that keeps an order). A `HashMap` or `Map.of(...)` with
two or more entries keeps no order and is refused with an error naming its class, never sent in a random order.
Context may be any map: it is bound in the manifest's field order. Whole numbers may be `Integer` or `Long`; every
decision and graph result holds `Long`. Options are built by chaining: `CallOptions.none().context(...).labels(...)
.onLowConfidence(...)`, `RemoteOptions.timeoutMs(ms).withChannel(true)`. An `llm` or `human` step is an `Ask` (a
function from a `Question` to an option key), and a code node a `CodeFunction`.

## Errors

Every error is an unchecked `JeffError` subclass nested in `JeffError` and named as in [Errors](/docs/sdk/errors)
(`JeffError.NotInstalled`, `JeffError.LowConfidence`, ...), with the same message and `details()` the Python SDK gives
for the same failure; `type()` is its name. `requestId()` is the ID the server gave the request that failed, or
`null` for an error raised in your own program. The Javadoc of each method names the errors it raises; a typed getter
such as `retryAfterS()` throws a `ContractError` naming the field when a server sent an error without it, without
inventing a value. No error message, note or `toString()` includes a token.

## Settings

- **Timeouts**: `timeoutMs`, in milliseconds (`LocalOptions.timeoutMs(ms)`, `RemoteOptions.timeoutMs(ms)`), above 0,
  else `InvalidRequest`. Over HTTP it bounds waiting for a free connection, connecting (running out is
  `BackendUnavailable`), waiting for the answer's headers and each wait for more of its body (`DeadlineExceeded`);
  `close()` ends every request still in flight. Over the channel a request also waits at most `timeoutMs` for its
  turn to be written, else `DeadlineExceeded`, and it is never sent. Local mode with a list of adapters takes an
  optional `StartOptions.defaults().withTimeoutMs(ms).withStartTimeoutMs(ms)`, each a whole number of milliseconds
  of at least 1, by default 60,000 and 1,800,000 (30 minutes); see
  [what local mode does](/docs/sdk/connect#section-what-local-mode-does-in-node-rust-c-and-java).
- **Proxies**: HTTP requests as httpx chooses them (`HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY`, each also in lower case,
  which wins, and `NO_PROXY`; `*` turns every proxy off). A user name and password in the proxy URL become a
  `Proxy-Authorization` header. The channel as the websockets library chooses: for `wss://` the first of
  `WSS_PROXY`, `SOCKS_PROXY`, `HTTPS_PROXY`; for `ws://` the first of `WS_PROXY`, `SOCKS_PROXY`, `HTTPS_PROXY`,
  `HTTP_PROXY` (`ALL_PROXY` is not read). A SOCKS proxy is refused by name. The operating system's own proxy settings
  are not read: set the variables instead.
- **Limits**: any value the SDK copies, writes as JSON or prints is at most 9,998 levels deep; a message to the engine
  socket at most about 1,024 (`InvalidRequest` before anything is sent); YAML at most 512 (`ManifestError`), and its
  aliases may expand to at most 1,000,000 values; an HTTP answer's body at most 64 MiB, as sent and once its `gzip` or
  `deflate` layers are undone (`BackendUnavailable`, and the client is closed); schema checks at most 500 inside one
  another (`ContractError`, or `GraphError` for a graph). A `Float` and a string holding a lone UTF-16 surrogate are
  refused when a row is packed.

## Testing code that uses the SDK

`ai.jeff.sdk.testing.FakeEngine` is an engine whose answers come from a function you supply; pass it to
`Jeff.create` with a catalogue to test your own code without a model.
