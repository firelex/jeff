# jeff-sdk: Jeff for Rust

Call Jeff's decision functions from Rust: bind a function, call it, read a typed `Decision`, route a call to the
right function, escalate when Jeff is unsure, and run decision graphs. The crate is async on Tokio (a multi-thread or
a current-thread runtime).

The development notes for the crate; the short README.md is what crates.io shows. It is published on crates.io as
`jeff-sdk` (imported as `jeff_sdk`): `cargo add jeff-sdk`. To depend on a checkout instead, use its path:

```toml
[dependencies]
jeff-sdk = { path = "../jeff/packages/rust" }
```

## Two ways to run

**Local** (the fastest): the crate talks straight to a Jeff engine running on the same machine, over the engine's Unix
socket. There is no server in between. The model runs only in the Python engine, `jeff-runtime`; the crate either finds
or starts it for you, or uses one you start yourself.

With a list of adapters, the crate finds the engine running for this user on this machine, or starts one
(contract/local-engine.md has every rule). Install uv once (`curl -LsSf https://astral.sh/uv/install.sh | sh`), then:

```rust
use jeff_sdk::{Jeff, StartOptions};

let jeff = Jeff::local_adapters(["guard", "triage"], StartOptions::default()).await?;
```

- The engine is `jeff-runtime[mac]` (macOS with Apple silicon) or `jeff-runtime[cuda]` (Linux with an NVIDIA GPU) of
  this crate's own version, run as `uvx --from jeff-runtime[<extra>]==<version> jeff runtime serve ...`. Any other
  machine is refused with `BackendUnavailable`; no uvx (on `PATH`, at `~/.local/bin/uvx` or `~/.cargo/bin/uvx`) is
  `RuntimeMissing`.
- Its folder is `~/Library/Caches/jeff/engine` on macOS and `$XDG_CACHE_HOME/jeff/engine` (or `~/.cache/jeff/engine`)
  on Linux: `engine.sock`, `engine.lock` (held while a program starts the engine) and `engine.log` (the last start's
  output).
- The first start downloads the engine and the base model (about 2 GB, once) and prints one line to standard error.
  The engine runs in its own process group, so it outlives the program and a Ctrl+C meant for the program. It stops
  by itself 5 minutes after the last program using it disconnects (`--exit-when-unused-s 300`).
- A second client, in this program or another, uses the running engine. When it lacks an adapter asked for, the crate
  sends one `serve` request with the adapters it serves plus the missing ones; a refusal is `NotInstalled`.
- `StartOptions { timeout_ms, start_timeout_ms }`: each reply (60,000 ms by default) and everything before the client
  is ready (1,800,000 ms by default). An engine that does not answer within the start timeout is sent SIGTERM (its
  whole process group) and the error shows its log's last lines.

To use exactly an engine you start and manage yourself (proprietary adapters with `--path`, an INT4 base with `--base`,
your own batching settings), start it and give its socket to `Jeff::local`, which never starts anything:

```bash
jeff runtime serve --socket /tmp/jeff.sock --adapter jeff.support.triage \
  --batch-window-ms 0.1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2
```

`--socket`, `--batch-window-ms`, `--max-batch-rows`, `--queue-limit-rows` and `--prepare-workers` are always
required. On an NVIDIA GPU the engine also requires `--max-adapters` and `--prefix-pool-mb` (and refuses to start
without them); on other backends (Apple Silicon, CPU) those two options are refused. On macOS a socket path may be at
most 104 bytes long; keep it short.

The batch window is how long the engine waits to gather requests into one batch. With one caller at a time it only
adds latency, so use a short one, `--batch-window-ms 0.1`: the Node SDK measured about 0.84 ms of overhead per call
(round trip minus the model's own time) at 0.1 ms, against 2.9 ms at 2 ms. With many concurrent callers a longer
window, such as 2 ms, lets the engine batch them and raises throughput.

```rust
use jeff_sdk::{CallOptions, Jeff, LocalOptions};
use serde_json::json;

#[tokio::main]
async fn main() -> jeff_sdk::Result<()> {
    let jeff = Jeff::local("/tmp/jeff.sock", LocalOptions::new(30_000)).await?;
    let context = json!({"company": "A furniture shop.", "channel": "email"}).as_object().unwrap().clone();
    let urgency = jeff.function("triage.urgency", CallOptions { context: Some(context), ..Default::default() }).await?;
    let decision = urgency.call(json!("My wardrobe arrived broken")).await?;
    println!("{:?} {:?} {}", decision.value, decision.confidence, decision.abstained);
    jeff.close().await;
    Ok(())
}
```

If nothing listens on the socket, `Jeff::local` fails with an `EngineError` that names the socket and the command
above. If the engine restarts, the next call reconnects once (at most one attempt per second); a call that was
waiting when it went away fails and is never sent again.

**Remote**: the crate calls a Jeff server (ours or your own) over HTTP, or over the server's WebSocket channel for
lower latency:

```rust
use jeff_sdk::{Jeff, RemoteOptions, TokenSource};

let jeff = Jeff::remote("https://api.example.com", TokenSource::fixed(std::env::var("JEFF_TOKEN").unwrap()), RemoteOptions::new(30_000)).await?;
let fast = Jeff::remote("https://api.example.com", TokenSource::function(|| async { Ok(current_token().await) }),
    RemoteOptions { channel: true, ..RemoteOptions::new(30_000) }).await?;
```

The token is a fixed string, a function returning the current token (called before every HTTP request; on the channel,
at connect and before the token expires), or `TokenSource::None` only for a server with no credentials. No error
message and no `Debug` output ever shows a token.

`Jeff::remote` compares the catalogue calls are bound with (the one shipped with the crate unless you pass
`RemoteOptions { catalogue: Some(...), .. }`) with what the server serves, and refuses a server that serves another
version of an adapter with a `ContractError` naming the adapter and both versions: labels would otherwise be mapped by
the wrong manifest. Use a crate whose shipped catalogue matches the server's, or pass the server's manifests.

### Timeouts

`timeout_ms` bounds each phase of a request separately, not the request as a whole: connecting, waiting for the
answer's headers (this phase also covers sending the request), and each wait for more of the answer's body. A slow
connect followed by a fast answer never adds up to a timeout; a server that is slow in any one phase does. A redirect
is never followed.

With `channel: true`, the same `timeout_ms` instead bounds opening the channel (connecting, TLS and the WebSocket
upgrade together), its hello and the closing handshake, and, for each request:

- its turn to be written behind earlier messages: at most `timeout_ms`, else `DeadlineExceeded`, and it is never sent;
- its upload: as long as it needs on a slow but working link. Sending is bounded by stalls instead: if the socket
  accepts no bytes of a message for `timeout_ms` (the server or the network stopped taking data), the channel ends
  with `BackendUnavailable` saying so, and every request waiting on it fails with that error;
- the wait for its reply: at most `timeout_ms`, starting only once its whole message has been sent, else
  `DeadlineExceeded`. This is the Python SDK's rule.

A request whose caller stops waiting before its turn is never sent.

The channel also keeps itself alive as the Python SDK's does: it pings the server every 20 s, and if no pong comes
within 20 s of a ping being sent, the channel ends with `BackendUnavailable` ("... the server sent no pong within 20 s
of a keepalive ping; the channel closed") and every request waiting on it fails with that error.

### Proxies

Proxies are taken from the environment as the Python SDK takes them: HTTP requests as httpx does (`trust_env=True`),
the channel as the websockets library does (`proxy=True`).

- HTTP requests: `HTTPS_PROXY` for `https://` servers, `HTTP_PROXY` for `http://` ones, `ALL_PROXY` for both, and
  `NO_PROXY` matched as httpx matches it (`*` turns every proxy off; `example.com` bypasses that host and its
  subdomains; `.example.com` only the subdomains; an address or `localhost` only that host; an entry with `://` is a
  URL pattern). An `https://` server is reached through a CONNECT tunnel; an `http://` one by sending each request to
  the proxy.
- The channel: for `wss://` `WSS_PROXY`, then `SOCKS_PROXY`, then `HTTPS_PROXY`; for `ws://` `WS_PROXY`,
  `SOCKS_PROXY`, `HTTPS_PROXY`, then `HTTP_PROXY` (`ALL_PROXY` is not read), with urllib's `NO_PROXY` rule. The
  channel always goes through a CONNECT tunnel.
- A lower-case variable wins over any other spelling of its name, and an empty lower-case one removes the proxy. Under
  CGI (`REQUEST_METHOD` set) an upper-case `HTTP_PROXY` is ignored.
- Only `http://` proxies are supported: an `https://` or SOCKS proxy the rules select is refused by name. The
  operating system's own proxy settings (which Python reads on macOS and Windows) are not read.

The local mode uses a Unix socket and no proxy.

### Message size and nesting

A MessagePack message (from the engine socket or the server's channel) that nests more than 256 arrays and maps inside
one another is refused by name ("... the value nests more than 256 arrays and maps inside one another") before it is
decoded, so no message can overflow the stack of the task reading it. No message of the contract nests anywhere near
that deep. The byte `0xc1`, which MessagePack never uses, is refused too. An HTTP answer is JSON, read with a limit of
128 levels; a deeper one is reported as an `EngineError` saying the server answered with a body that is not JSON.

An HTTP answer's body is at most 64 MiB (67,108,864 bytes), both as sent (whatever its framing, a body with no
`Content-Length` included) and once each `gzip` or `deflate` layer of its `Content-Encoding` is undone, so a few
kilobytes of stacked gzip cannot decode to gigabytes. Past that the request fails with `BackendUnavailable` naming the
limit, and the client is closed. As the Python SDK's httpx, requests ask for `gzip, deflate` and the answer is decoded
(by zlib, so a body that is not its coding is an `EngineError` with zlib's message); codings httpx does not know are
passed over. httpx sets no limit; a Jeff answer is far smaller (a `/v1/decide` answer of 1,000 calls is a few MiB).

## What you can do

- `jeff.decide(&[Call::new(function, input).with_context(...).with_labels(...)])`: many calls at once, one engine
  request per adapter.
- `jeff.function(reference, CallOptions { context, labels, on_low_confidence })`: bind once, then `call(input)` or
  `many(inputs)`. On the channel a bound function is a handle on the server; `close()` releases it, and dropping it
  releases it before the channel's next request.
- `jeff.classify`, `route`, `rate`, `detect`, `is_supported`, `equivalent`, `requires_escalation`: call through an
  interface, with `ShorthandOptions { function: Some(...), .. }` to name the function, or without it to let the
  routers choose (`jeff.dispatch`).
- `on_low_confidence`: `Policy::Return` (the default) returns the unsure decision, `Policy::Error` raises
  `LowConfidence`, and `escalate(vec![function_step("triage.sentiment"), llm(ask_an_llm), human(ask_a_person)],
  Some(budget_ms))` tries each step in turn. An `llm` or `human` step is your own async function: it gets a
  `Question` and returns one of its option keys. `question.state_entries()` and `question.option_entries()` list the
  fields and options in the order the model reads them; build any prompt from them in that order.

  ```rust
  use jeff_sdk::{BoxError, Question, llm};

  let ask = llm(|question: Question| async move {
      let state: Vec<String> = question.state_entries().unwrap_or_default().iter().map(|(key, value)| format!("{key}: {value}")).collect();
      let options: Vec<String> = question.option_entries().iter().map(|(key, description)| format!("{key}: {}", description.unwrap_or(""))).collect();
      let prompt = format!("{}\n\n{}\n\nOptions:\n{}", question.instructions, state.join("\n"), options.join("\n"));
      Ok::<String, BoxError>(call_your_llm(&prompt).await) // must be one of the option keys
  });
  ```
- `jeff.run_graph(graph, input, params, code, policies)`: a graph file (`PathBuf`), document (`Json`) or `Graph`;
  code nodes are your own async functions (`code_function(|call| async move { ... })`). `run_server_graph` runs a
  graph registered on a Jeff server.
- `decision.to_json()` returns the decision as the contract writes it, as a `Result`: a NaN or infinite number (only
  a decision built by hand can hold one) is a `ContractError` naming the field, since JSON has no value for it.
  `decision.to_json_text()` gives exactly the text the Python SDK's `json.dumps(decision.to_json())` writes
  (separators `", "` and `": "`, non-ASCII escaped, floats as Python prints them, `NaN`, `Infinity` and `-Infinity`
  for those numbers).

Every error is a `JeffError` whose `kind()` is one of `ErrorType`'s variants, one per entry of `contract/errors.yaml`,
with the same message and `details()` the Python SDK gives for the same failure. Typed getters
(`function_id()`, `fields()`, `retry_after_s()`, `step()`, `adapter_id()`, `decision()`) return an error naming the
field when the details lack it, never a made-up value.

Lower-level modules are public for tools built on the SDK: `contract` (the schemas and error catalogue), `manifest`,
`binding`, `routing`, `protocol` (the engine socket's frames), `msgpack`, `pyformat` (Python-style text), `yaml` and
`testing`.
With the `testing` feature, `local_engine` is public too: local mode's engine start with an `Environment` the test
gives (HOME, PATH, XDG_CACHE_HOME, the platform, where printed lines go) instead of the process's, and its pure parts,
which tests/conformance_local_engine.rs checks against contract/conformance/local_engine.json. tests/local_engine.rs
runs it against a fake `uvx` (a shell script on the test's PATH) and the stub engine of tests/support.

## Testing code that uses the SDK

`jeff_sdk::testing::FakeEngine` is an engine whose answers come from a function you supply; pass it to `Jeff::new`
with a catalogue to test your own code without a model. `testing::GoldenCase` and `testing::golden_problems` compare a
decision with a recorded golden case.

`testing::pin_proxy_environment` makes every connection opened afterwards in the process pick its proxy from a list
you give instead of the process's environment (`Some(Vec::new())` connects directly whatever is set). It exists only
with the crate's `testing` feature, because it changes how every connection in the process picks its proxy and a
normal build must not be able to call it by mistake. Turn it on for tests only:

```toml
[dev-dependencies]
jeff-sdk = { path = "../jeff/packages/rust", features = ["testing"] }
```

`testing::pin_channel_keepalive(Some((interval_ms, timeout_ms)))`, also only with the `testing` feature, makes every
channel opened afterwards ping at that interval and give up after that timeout, for tests of the keepalive that
cannot wait 40 s.

## Behaviour

The crate follows the Python SDK exactly: `contract/conformance/` holds what the Python SDK does (rows sent to the
engine, decisions, socket conversations, server exchanges), and this crate's tests replay all of it.

Every map that carries an order keeps it: rows, criteria, `Decision::probabilities` (option order), questions, and
graph nodes keep the manifest's, the caller's or the document's order, including keys that look like numbers
(`"2"`, `"10"`), so the limits the Node SDK documents for such keys do not apply here.

`Jeff::local` does no adapter version check on connect: the engine's `installed` reply carries adapter ids only, no
versions (the Python SDK's `Jeff.connect` does none either), unlike `Jeff::remote`, which refuses a server serving a
different version of a catalogue manifest.

An engine reply whose probabilities are broken (the wrong count, a value that is not a finite number between 0 and 1,
or a sum away from 1) is refused with an error naming the values; it is never rescaled.

YAML (manifests, graph files) is read as PyYAML's `safe_load` reads it, except for these refusals, each a
`ManifestError` naming the file and the reason:

- what JSON cannot hold: a datetime (a bare `YYYY-MM-DD` date is kept as its text), a mapping key that is not a
  string, and `.inf` or `.nan` ("not a finite number"; in a manifest, as the Python SDK words it there, by its
  place: "<path>: /functions/is_spam/calibration/threshold is nan, not a finite number, which JSON cannot hold; write
  a finite number", contract/conformance/manifests.json), and an integer beyond 64 bits;
- an explicit tag, a merge key (`<<`) and the value key (`=`);
- more than one document in the file;
- flow collections (`[...]`, `{...}`) nested more than 255 deep (the parser's "recursion limit exceeded");
- aliases that expand to more than 1,000,000 values. PyYAML shares an aliased value; this crate copies it, so without
  the limit a few hundred bytes of nested aliases could take gigabytes of memory.

Quote a scalar to keep it a plain string.

## Development

```bash
packages/rust/scripts/sync-shipped.sh          # copy contract/ and catalogue/ into shipped/ (a test fails when it is stale)
cd packages/rust
cargo test                                     # unit, integration and conformance tests, offline
cargo test --test golden -- --ignored          # golden cases against a real engine (see tests/golden.rs)
```

The golden test needs `JEFF_ENGINE_SOCKET` (the socket of a running `jeff runtime serve`) and `JEFF_GOLDEN_ADAPTERS`
(the comma-separated adapter ids that engine serves whose cases to check); a missing one fails the run. Without
`--ignored` it is reported as ignored. On a Mac with Apple Silicon the engine runs on MLX from the repository root:

```bash
jeff runtime serve --socket /tmp/jeff.sock --adapter jeff.safety.spam --adapter jeff.support.triage \
  --batch-window-ms 0.1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2
JEFF_ENGINE_SOCKET=/tmp/jeff.sock JEFF_GOLDEN_ADAPTERS=jeff.safety.spam,jeff.support.triage \
  cargo test --test golden -- --ignored
```

The crate's tests never use the machine's proxy variables: every test that is not about proxies pins an empty proxy
environment, and the suite passes with `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` or `WS_PROXY` set.

Homebrew's `rustup` is keg-only: put `/opt/homebrew/opt/rustup/bin` on your `PATH` first. `rust-toolchain.toml` pins
the toolchain.
