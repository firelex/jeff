---
title: Rust SDK
---
# Rust SDK

`jeff-sdk` is the Rust SDK, imported as `jeff_sdk`. It is async and runs on Tokio (a multi-thread or a current-thread
runtime), and needs Rust 1.99.1 or later (edition 2024). This page covers details specific to Rust; the topic
pages ([Connect](/docs/sdk/connect), [Functions](/docs/sdk/functions), ...) show every feature in every language.

## Install

The crate is on crates.io as `jeff-sdk`:

```bash
cargo add jeff-sdk serde_json tokio --features tokio/macros,tokio/rt-multi-thread
```

or, in your own project's `Cargo.toml`:

```toml illustrative
[dependencies]
jeff-sdk = "1.4"
serde_json = "1"
tokio = { version = "1", features = ["macros", "rt-multi-thread"] }
async-trait = "0.1"   # only to implement the Engine trait yourself
```

Its source is in the repository [github.com/firelex/jeff](https://github.com/firelex/jeff), in `packages/rust`.

To run the models [on your own machine](/docs/sdk/connect#section-on-your-machine-free), install uv once
(`curl -LsSf https://astral.sh/uv/install.sh | sh`, see [docs.astral.sh/uv](https://docs.astral.sh/uv/)) and call
local mode with the adapters you use: `Jeff::local_adapters(["guard"], StartOptions::default()).await?`. The model runs only in the Jeff engine, which
is written in Python; the Rust SDK finds the engine running for you on this machine, or starts it with uv (the
first start downloads about 2 GB, once), and talks to it. It runs on a Mac with Apple silicon or on Linux with an
NVIDIA GPU. To use an engine you start yourself with `jeff runtime serve`, pass its socket instead:
`Jeff::local(socket_path, LocalOptions::new(ms))` ([Using an engine you start yourself](/docs/sdk/connect#section-using-an-engine-you-start-yourself)).

## Idioms

Every method that reaches an engine or a server is async and returns a `jeff_sdk::Result`: `.await?` it. A `Jeff`
and a `BoundFunction` are cheap handles: clone them to share one connection between tasks. End the connection with
`jeff.close().await`; on the channel, `bound.close().await?` releases a bound function's handle (dropping it releases
it before the channel's next request).

<!-- snippet: sdk/rust-1.rs -->

JSON values are `serde_json` values, and the crate builds `serde_json` with `preserve_order`, so every ordered map
preserves its order: rows, a decision's `probabilities` (in option order), questions and graph nodes keep the manifest's,
the caller's or the document's order, even for keys that look like numbers. Context is a `jeff_sdk::Context` (a JSON map),
labels a `Labels` (`Labels::from_json`). Options are structs with defaults: `CallOptions { context, labels,
on_low_confidence, ..CallOptions::default() }`, and `ShorthandOptions` with `function`. An `llm` or `human` step and a
code node are your own async functions (`code_function(|call| async move { ... })`). Names follow Rust's conventions:
`timeout_ms` for `timeout_s`, `Policy::Error` for `on_low_confidence="error"`, and so on.

## Errors

All errors use one type, `JeffError`. Its `kind()` is one of `ErrorType`'s variants, one for each error in
[Errors](/docs/sdk/errors) (`ErrorType::RateLimited`, ...), and `type_name()` gives its name; the message and
`details()` are those the Python SDK gives for the same mistake. `request_id()` is the ID the server gave the request
that failed, or `None` for an error raised in your program. The typed getters (`function_id()`, `fields()`,
`retry_after_s()`, `step()`, `adapter_id()` and `decision()`) return an error naming the field when an error's details
lack it; they never make up a value. A token never appears in a message or in `Debug` output.

## Settings

- **Timeouts**: `timeout_ms`, in milliseconds (`LocalOptions::new(ms)`, `RemoteOptions::new(ms)`): a whole number
  above 0 and at most 68,719,476,734 (about 2.2 years, the longest timer Tokio keeps), else `InvalidRequest`. Over
  the channel, a request also waits at most `timeout_ms` for its turn to be written behind earlier messages, else
  `DeadlineExceeded`, and it is never sent. `Jeff::local_adapters` takes `StartOptions { timeout_ms,
  start_timeout_ms }` (both `u64` milliseconds of at least 1; `StartOptions::default()` gives 60,000 and 1,800,000,
  30 minutes); see [what local mode does](/docs/sdk/connect#section-what-local-mode-does-in-node-rust-c-and-java).
- **Proxies**: HTTP requests as httpx chooses them: `HTTPS_PROXY` for `https://` servers, `HTTP_PROXY` for `http://`
  ones, `ALL_PROXY` for both, and `NO_PROXY` matched as httpx matches it (`*` turns every proxy off; `example.com`
  bypasses that host and its subdomains; `.example.com` only the subdomains). The channel as the websockets library
  chooses: for `wss://` `WSS_PROXY`, then `SOCKS_PROXY`, then `HTTPS_PROXY`; for `ws://` `WS_PROXY`, `SOCKS_PROXY`,
  `HTTPS_PROXY`, then `HTTP_PROXY` (`ALL_PROXY` is not read), always through a CONNECT tunnel. A lower-case variable
  wins over any other spelling, and an empty lower-case one removes the proxy. Only `http://` proxies are supported:
  an `https://` or SOCKS proxy these rules select is refused, naming it. The operating system's own proxy settings
  are not read.
- **Limits**: a MessagePack message (from the engine socket or the channel) that nests more than 256 arrays and maps
  is refused before it is read; an HTTP response is read as JSON with a limit of 128 levels of nesting, and at most 64 MiB
  (as sent and once each `gzip` or `deflate` layer is undone), else `BackendUnavailable` and the client is closed. An
  engine answer whose probabilities are broken is refused, never rescaled. Manifests and graph files are read as
  Python's `yaml.safe_load` reads them, except that what JSON cannot hold (a date and time, `.inf`, `.nan`, an integer
  beyond 64 bits), explicit tags, merge keys, several documents in one file and aliases that expand to more than
  1,000,000 values are refused with `ManifestError`.

## Testing code that uses the SDK

`jeff_sdk::testing::FakeEngine` is an engine whose answers come from a function you supply; pass it to `Jeff::new`
with a catalogue to test your own code without a model.
