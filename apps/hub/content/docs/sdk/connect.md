---
title: Connect
---
# Connect

A Jeff client makes decisions using either models on your own machine or a Jeff server over the network.
Everything else in the SDK (binding functions, the interface shorthands, graphs, escalation) works the same way on
both. The code on these pages is available in every SDK language: choose yours in the tabs, and your choice carries
over to every page.

| | On your machine (free) | Through the API |
| - | - | - |
| Python | `Jeff.local(adapters)`, or `Jeff.connect(socket, timeout_s=...)` | `Jeff.remote(url, token, timeout_s=...)` |
| Node | `Jeff.local(adapters, options?)`, or `Jeff.local(socket, { timeoutMs })` | `Jeff.remote(url, token, { timeoutMs })` |
| Rust | `Jeff::local_adapters(adapters, StartOptions::default())`, or `Jeff::local(socket, LocalOptions::new(ms))` | `Jeff::remote(url, token, RemoteOptions::new(ms))` |
| C# | `JeffClient.LocalAsync(adapters, options?)`, or `JeffClient.LocalAsync(socket, new LocalOptions { Timeout })` | `JeffClient.RemoteAsync(url, token, new RemoteOptions { Timeout })` |
| Java | `Jeff.local(adapters)`, `Jeff.local(adapters, options)`, or `Jeff.local(socket, LocalOptions.timeoutMs(ms))` | `Jeff.remote(url, token, RemoteOptions.timeoutMs(ms))` |

`adapters` is a list of adapter IDs or aliases, for example `["guard"]`. The first form on your machine is the usual
one; the socket form uses an engine you start yourself (see
[Using an engine you start yourself](#section-using-an-engine-you-start-yourself)).

Close the client when you are done: a `with` block in Python, `await jeff.close()` in Node, `jeff.close().await` in
Rust, `await using` in C#, try-with-resources in Java. Each language's page says more:
[Python](/docs/sdk/python), [Node](/docs/sdk/node), [Rust](/docs/sdk/rust), [C#](/docs/sdk/csharp),
[Java](/docs/sdk/java).

## On your machine (free)

The models run only in the Jeff engine, which is written in Python. In Python, `Jeff.local` loads the engine into
your own process. Every other SDK talks to an engine process on the same machine, over its Unix socket, with no
server in between, and starts that engine itself. You need only uv, a tool that runs Python programs, installed once
([docs.astral.sh/uv](https://docs.astral.sh/uv/)):

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Then install the SDK package and call local mode with the adapters your program uses. The examples on these pages
use the `guard`, `triage` and `spam` adapters; this one uses the guard (in Python, the same code loads the guard into
your own process):

<!-- snippet: sdk/connect-4 -->

### What local mode does in Node, Rust, C# and Java

- **It finds or starts the engine.** There is one engine per user and machine, listening on `engine.sock` in your
  cache folder: `~/Library/Caches/jeff/engine` on macOS, and `$XDG_CACHE_HOME/jeff/engine` (or
  `~/.cache/jeff/engine` when `XDG_CACHE_HOME` is not set) on Linux. If an engine is listening there, the SDK uses
  it. If not, it starts one with uv: `uvx --from "jeff-runtime[mac]==<version>" jeff runtime serve ...` (`cuda` in
  place of `mac` on an NVIDIA GPU), where `<version>` is the SDK's own version, so the engine is always the one the
  SDK was tested with. You never install `jeff-runtime` yourself.
- **The first start prints one line and downloads about 2 GB, once.** The line, on standard error, is
  `Jeff: starting the Jeff engine for jeff.safety.guard; the first time, this downloads the engine and the base model
  (about 2 GB, once). Its log: ...`. The engine's output goes to `engine.log` in the same folder. Later starts only
  load the model.
- **Every program of yours shares it.** A second program, or a second client in the same program, uses the same
  engine and never starts another. While one program is starting the engine, the others wait for it (and say so on
  standard error).
- **It stops by itself.** An engine that local mode started stops 5 minutes after the last program using it closes
  its client or exits, so a program run again soon finds the model still loaded. The SDK never stops an engine
  itself (an engine you start by hand runs until you stop it).
- **It runs on a Mac with Apple silicon, or on Linux with an NVIDIA GPU** (including WSL 2 on Windows). On any other
  machine local mode refuses at once with `BackendUnavailable`, before looking for anything: use a Jeff server
  [through the API](#section-through-the-api) instead.

Name every adapter your program uses in the one call. If the running engine does not serve one of them (another
program started it with other adapters), the SDK asks it to load the missing ones; an engine can usually load only
the adapters it was started with, so this often fails with `NotInstalled`. When programs that run at the same time
need different adapters, start one engine yourself with all of them and give its socket (below).

Two options, both in milliseconds and both optional, bound the waits (C# takes them as `TimeSpan`s):

| Option | Node, Java | Rust | C# | Default | What it bounds |
| - | - | - | - | - | - |
| Reply timeout | `timeoutMs` | `timeout_ms` | `Timeout` | 60,000 (1 minute) | The engine's first answer, and each reply. |
| Start timeout | `startTimeoutMs` | `start_timeout_ms` | `StartTimeout` | 1,800,000 (30 minutes) | Everything before the client is ready: waiting for another program that is starting the engine, and starting it (the first start includes the download). |

Node takes them as `{ timeoutMs, startTimeoutMs }` (`StartOptions`), Rust as `StartOptions { timeout_ms,
start_timeout_ms }`, C# as `new StartOptions { Timeout, StartTimeout }`, and Java as
`StartOptions.defaults().withTimeoutMs(ms).withStartTimeoutMs(ms)`. A value that is not a whole number of
milliseconds of at least 1 is refused with `InvalidRequest`.

What can go wrong, besides the errors of any call:

| Error | When |
| - | - |
| `RuntimeMissing` | uv is not installed: `uvx` is not on `PATH`, nor in `~/.local/bin` or `~/.cargo/bin`. The message gives the command that installs it. |
| `RuntimeMissing` (C# and Java on Linux) | The `setsid` command is missing (it comes with util-linux, present on most Linux systems); the engine is started in a session of its own through it. The message says what to install. |
| `BackendUnavailable` | The machine is neither a Mac with Apple silicon nor Linux with an NVIDIA GPU (an Intel Mac, a Linux machine without an NVIDIA GPU, Windows outside WSL 2). |
| `NotInstalled` | The running engine does not serve an adapter you named and refused to load it. |
| `ProprietaryWeights` | An adapter's weights are not open, so local mode cannot start an engine for it: start one yourself with `--path` and give its socket. |
| `EngineError` | The engine exited while starting, or did not answer within the start timeout (local mode then stops it); the message names `engine.log` and shows its last lines. Also when `HOME` is not set. |
| `ManifestError` | The list of adapters is empty, or its adapters are trained on different bases. |
| `UnknownFunction` | An adapter name is not in the catalogue. |

### Using an engine you start yourself

The socket form (`Jeff.local(socket, ...)` and its equivalents in the table above, `Jeff.connect` in Python) uses
exactly the engine listening on that socket. It never starts an engine, never looks in the engine folder and never
needs uv. Use it to run an engine with proprietary adapters (`--path`), an INT4 base (`--base`), your own batching
settings, or adapters for several programs at once, and keep it running with your process manager. Start it with
`jeff runtime serve`, which comes with the Python package `jeff-runtime` (see [Install](/docs/sdk/install)):

```bash
jeff runtime serve --socket /tmp/jeff.sock --adapter guard --adapter triage --adapter spam \
  --batch-window-ms 0.1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2
```

This engine serves the three adapters the examples on these pages use; give `/tmp/jeff.sock` to the client in place
of the list of adapters.

These `jeff runtime serve` options are always required:

| Option | What it does |
| - | - |
| `--socket PATH` | The Unix socket the engine listens on; give the same path to the client. On macOS a socket path may be at most 104 bytes long, so keep it short. |
| `--batch-window-ms MS` | How long the first queued request waits for others to join its batch; above 0. See [Choosing the batch window](#section-choosing-the-batch-window). |
| `--max-batch-rows N` | The maximum number of rows in one batch. |
| `--queue-limit-rows N` | The most rows that may wait in the queue; at least `--max-batch-rows`. A request that would queue more is refused with `Busy`. |
| `--prepare-workers N` | The number of incoming requests prepared at once. |

Name each adapter to serve with `--adapter` (repeatable). [jeff runtime serve](/docs/sdk/command#section-jeff-runtime-serve)
lists every option. On an NVIDIA GPU the engine needs two more, `--max-adapters` and `--prefix-pool-mb`: see
[Only on an NVIDIA GPU: the prefix pool](#section-only-on-an-nvidia-gpu-the-prefix-pool).

If nothing listens on the socket, connecting fails with `EngineError`, naming the socket and the `jeff runtime serve`
command that starts an engine there. If the engine stops later, a call that was waiting fails and is never sent
again; the next call fails with `EngineError` too, and the client connects again on a later call once the engine is
back. Reconnection attempts are limited to one per second; while the engine is down, callers are notified
immediately. Connecting does not check adapter versions: the engine reports which adapters it serves, not their versions. Use the same catalogue
in the engine and in your program.

### Python in your own process

`Jeff.local(adapters, *, catalogue=None, paths=None, backend="auto", log=None, prefix_pool_mb=None, base=None)` loads
the base model and the adapters you name into this process. It needs `jeff-runtime`. The first run downloads the
weights into the Hugging Face cache; after that, no network is needed. Only the Python SDK can do this; the others
talk to an engine process, as above.

| Argument | What it is |
| - | - |
| `adapters` | A list of adapter IDs or aliases, for example `["guard"]` or `["jeff.safety.guard"]`. A single string is refused with `ManifestError`. All the adapters must be trained on the same base. |
| `catalogue` | The `Catalogue` to read the adapters' manifests from. Leave it out to use the one that comes with the package. |
| `paths` | Adapter ID or alias to the folder of its weights, for adapters you have on disk. An adapter whose weights are not open must be given here, or `Jeff.local` raises `ProprietaryWeights`. |
| `backend` | `"auto"` (the default), `"fast"`, `"mlx"` or `"torch"`. Auto picks `"fast"` when an NVIDIA GPU is present, `"mlx"` on Apple silicon, and `"torch"` otherwise. Naming a backend this machine cannot run raises `BackendUnavailable`. |
| `prefix_pool_mb` | Only on an NVIDIA GPU: the GPU memory, in MB, for cached prompt prefixes. See [the prefix pool](#section-only-on-an-nvidia-gpu-the-prefix-pool). |
| `log` | A `DecisionLog` to write every decision to (see [Decision log](/docs/sdk/decision-log)). |
| `base` | A folder holding a base model to load instead of the adapters' base from the catalogue, for example an INT4 base made with `jeff runtime quantize`. |

`Jeff.connect(socket_path, *, timeout_s, catalogue=None, log=None)` is the Python form of the other SDKs' socket
form: it attaches to an engine started with `jeff runtime serve`, so several programs can share one loaded model.
`jeff.connect` is another name for it. It needs a running engine, so this example is illustrative:

```python illustrative
import jeff

client = jeff.connect("/tmp/jeff.sock", timeout_s=30)
...
client.engine.close()  # the connection to the engine
```

[Engines](/docs/sdk/engines) describes the `SocketEngine` that `Jeff.connect` builds.

### Only on an NVIDIA GPU: the prefix pool

On a Mac or a CPU, leave this out: the example above runs as is.

On a machine with an NVIDIA GPU, Jeff runs on its fast backend, which keeps the prompt prefixes it has already
processed (each function's instructions and your bound context) in a pool of GPU memory set aside at start. You must
specify the size of that pool. It must be a whole number of MB, at least 116 (enough for one prefix of 8,192 tokens);
2048 holds 66 prefixes of 1,000 tokens. On any other backend, setting it is refused with `EngineError`, since there is
no such pool.

- In Python, pass `prefix_pool_mb` to `Jeff.local`. Without it, `Jeff.local` raises `EngineError` before downloading
  anything.
- Local mode in Node, Rust, C# and Java passes them itself when it starts the engine on an NVIDIA GPU
  (`--max-adapters` the larger of 8 and the number of adapters, `--prefix-pool-mb 7345`).
- For an engine you start with `jeff runtime serve` (the socket form, and Python's `Jeff.connect`), pass
  `--max-adapters` and `--prefix-pool-mb`; the engine refuses to start without them. The pool must hold
  `--max-batch-rows` prefixes of 8,192 tokens: 7345 for 64 rows.

This example needs an NVIDIA GPU, so it is illustrative and not run by the checks of these pages:

```python illustrative
import jeff

client = jeff.Jeff.local(["guard"], prefix_pool_mb=2048)
```

```bash
jeff runtime serve --socket /tmp/jeff.sock --adapter guard --max-adapters 4 --prefix-pool-mb 7345 \
  --batch-window-ms 0.1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2
```

### Choosing the batch window

The engine waits `--batch-window-ms` after the first request arrives, so that requests arriving together are decided
in one batch. That wait is added to every decision, so keep it small unless many requests really do arrive together.
Local mode starts its engine with 0.1; choose another window only for an engine you start yourself.

Measured on an Apple M4 Max with MLX, with one caller on its own machine deciding one call at a time (the model itself
took about 28.6 ms per decision), the time the Node SDK and the engine added on top of the model was 0.84 ms at the
median (1.14 ms at the 95th percentile) with `--batch-window-ms 0.1`, and 2.92 ms at the median with
`--batch-window-ms 2`.

For one caller, or a few, use a small window such as 0.1. A larger window helps only when many callers decide at
the same time on a GPU machine, where rows in one batch are decided together. On Apple silicon it does not help: MLX
decides the rows of a batch one at a time, so a longer wait only adds delay.

## Through the API

`Jeff.remote` connects to the Jeff server at a URL, ours or one you run yourself:

<!-- snippet: sdk/connect-1 -->

It takes:

- the server's address, for example `{{origin}}`;
- the bearer token: a string, a function that returns the current token, or none at all (only for a server that has
  no credentials set up);
- a timeout, which is required: how long each phase of a request may take (see
  [What the timeout bounds](#section-what-the-timeout-bounds)). Python takes it in seconds (`timeout_s`); Node, Rust
  and Java in milliseconds; C# as a `TimeSpan`. A timeout that is not above 0 is refused with `InvalidRequest`;
- optionally a catalogue, whose manifests calls are bound with (the one that comes with the package when left out),
  and whether to use the [low-latency channel](#section-the-low-latency-channel).

Connecting reads the server's list of functions (`GET /v1/functions`) once, and refuses with `ContractError` a server
that serves a different version of an adapter than your catalogue holds, naming the adapter and both versions, so a
call is never bound with the wrong manifest. Use a package whose catalogue matches the server's, or pass the server's
manifests as the catalogue.

### Tokens

Keep the token out of your code: read it from an environment variable or a secret store. No error message ever shows
a token. When the token is a function, it is called before every HTTP request, so a token you refresh is used from the
next request on:

<!-- snippet: sdk/connect-2 -->

### What the timeout bounds

The timeout bounds each phase of a request separately, not the request as a whole. Over HTTP the phases are:

1. connecting to the server (C# and Java also count waiting for a free connection as a phase);
2. waiting for the answer's headers, which also covers sending the request body, so a slow upload and a slow server
   both count against it;
3. each wait for more of the answer's body.

A slow connect followed by a fast answer never adds up to a timeout; a server that is slow in any one phase does. A
connect that takes too long raises `BackendUnavailable`; a server that does not answer in time raises
`DeadlineExceeded` (the server may still have run the request). A request is never sent twice, and a redirect is
never followed.

### The low-latency channel

With the channel on, the client keeps one authenticated WebSocket connection open, and a bound function becomes a
handle on the server. Close a bound function you no longer need to release its handle; closing the client ends the
channel, and with it every handle.

<!-- snippet: sdk/connect-3 -->

Over the channel, the timeout bounds opening the channel, the hello exchange, and the closing handshake, and, for each
request, the wait for its reply, which starts only once its whole message has been sent: a large message on a slow
but working link takes as long as it needs to send. Sending is bounded by stalls instead: if the socket accepts no
piece of a message for the timeout (the server or network has stopped accepting data), the channel ends with
`BackendUnavailable` saying so, and every request waiting on it fails with that error.

The channel pings the server every 20 seconds, and ends with `BackendUnavailable` when no answer comes within 20
seconds of a ping (C# needs .NET 9 or later for this; see [C#](/docs/sdk/csharp)). It does not reconnect automatically; create
a new connection. A token function is not called before every call: it is called when the connection authenticates, again from
shortly before that token expires, and for each listing of the server's functions.
[Through the API](/docs/guides/remote) explains exactly what each operation costs in requests and how tokens are
refreshed; [The low-latency channel](/docs/guides/channel) describes the protocol.

### Proxies

Every SDK takes its proxy from the environment, as the Python SDK does: `HTTPS_PROXY` for `https://` servers,
`HTTP_PROXY` for `http://` ones, `ALL_PROXY` for both, and `NO_PROXY` for the hosts that bypass it, with lower-case forms taking
precedence. `https://` requests and the channel's WebSocket go through a CONNECT tunnel, `http://` requests are forwarded.
SOCKS proxies are refused. A proxy setting that cannot be used (a SOCKS proxy, an address that is not a URL) raises
`InvalidRequest`; a proxy that refuses the connection or cannot be reached raises `BackendUnavailable`, naming the
proxy (with any password in its address hidden). On your machine the SDK uses a Unix socket and no proxy.

The languages differ in two details: only Python reads the operating system's own proxy settings when no variable is
set, and Rust and Java choose the channel's proxy as the Python websockets library does (`WSS_PROXY` or `WS_PROXY`
first). Each language's page has its exact rules.

## Mistakes caught at once

Arguments are checked before any request, connection or download:

<!-- snippet: sdk/connect-5 -->

## Next steps

- [Functions](/docs/sdk/functions): bind a function and read its decisions.
- [Running the server](/docs/guides/running-server): run your own Jeff server to call through its API.
