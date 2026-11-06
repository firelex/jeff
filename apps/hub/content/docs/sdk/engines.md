---
title: Engines
---
# Engines

An **engine** turns a function's prompt into probabilities. In Python, `Jeff.local` loads one into your
process; in every SDK, the local client talks to one in another process (`Jeff.connect` in Python), and `Jeff.remote`
asks a server. To run Jeff on top of an engine of your own (in a test, or in a service that manages the model
itself), build the client directly from it:

| | Make a client from an engine | The engine type |
| - | - | - |
| Python | `jeff.Jeff(engine, catalogue, log=None, authorise=None)` | anything with the members of the `Engine` protocol |
| Node | `await Jeff.create(engine, catalogue)` | an object with the members of the `Engine` interface |
| Rust | `Jeff::new(Arc::new(engine), catalogue).await?` | a type implementing the `Engine` trait (with the `async-trait` crate) |
| C# | `await JeffClient.CreateAsync(engine, catalogue)` | a class implementing `IEngine` |
| Java | `Jeff.create(engine, catalogue)` | a class implementing `Engine` |

The arguments, using Python's names:

- `engine`: anything that follows the `Engine` protocol below.
- `catalogue`: the `Catalogue` to bind calls with; `jeff.Catalogue.shipped()` is the one that comes with the package,
  and `jeff.Catalogue.from_dir(path)` reads a folder of manifests (a `bases.yaml` and one folder per adapter).
- `log`: a [`DecisionLog`](/docs/sdk/decision-log), or `None`.
- `authorise`: a function called with each function's description just before it is decided; it raises (for example
  `NotLicensed`) to refuse that function, and the engine then never sees it. `client.with_authorise(authorise)`
  gives a copy of the client with another such function, sharing the same engine.

`log` and `authorise` are available only in Python. Each SDK also has a ready-made test engine whose answers come from a function
you supply, `FakeEngine` (in `jeff-sdk/testing` for Node, `jeff_sdk::testing` for Rust, `Jeff.Sdk.Testing` for C#, `ai.jeff.sdk.testing` for
Java), to test your own code without a model.

## The Engine protocol

An `Engine` has (in each language's own names: `baseFormat`, `installed`, `predict` in Node and Java; `base_format`
in Rust; `BaseFormat`, `InstalledAsync`, `PredictAsync` in C#):

- `base_format`: the base model's weight format, read once when the client is created: `{"format": "bf16"}` for the base
  as released, or for an INT4 base its format and `format_id`. It determines which calibrated thresholds the functions
  use.
- `installed(refresh=False)`: the set of adapter IDs it has loaded and ready. An engine that keeps a copy of another
  process's list asks that process first when `refresh` is `True`.
- `predict(adapter_id, rows)`: an `EngineResult` for a list of rows of one adapter. Each row is an object with the
  `state` the model reads and the `question` (the function's type, instructions and options).

An `EngineResult` holds `probabilities` (one list per row, over the row's options in order), `timing` (an object containing
whatever timing information the engine reports), and optionally `input_tokens` (each row's prompt length) and `row_timing` (timing that
differs per row).

Every engine also serves the base model with no adapter, under the adapter ID `"base"`.

This stand-in engine gives every row the same probabilities. It demonstrates the whole protocol;
the client built on it binds and checks calls exactly as with a real engine:

<!-- snippet: sdk/engines-1 -->

## SocketEngine

This section and the next cover the Python package. The other SDKs have a `SocketEngine` too, which their local
client builds (on the engine local mode finds or starts, or on the socket you give), but not `ServingEngine`: only
the engine in `jeff-runtime` serves.

`SocketEngine(path, *, timeout_s)` is the `Engine` that `Jeff.connect` builds: it talks to an engine process started
with `jeff runtime serve --socket <path>`, over that Unix socket. `timeout_s` is how long to wait for each answer.
Several threads may use it at once. In addition to the protocol, it has `health(timeout_s=None)` and `stats(timeout_s=None)`,
which ask the engine for its health and its counters, and `close()`, after which every call fails.

If the engine process closes the connection (for example, after a restart), the call in flight fails and is never sent again;
the next call makes one new connection. Connecting raises `EngineError` when nothing is listening at `path`, and
`ProtocolMismatch` when the engine speaks another version of the socket protocol.

## ServingEngine

`ServingEngine` is the larger protocol a server needs from its engine, so it can check requests as they arrive,
batch them, and change adapters while it keeps serving. On top of `Engine` it has:

| Member | What it does |
| - | - |
| `backend` | Which backend is running (`"fast"`, `"mlx"` or `"torch"` for the engine in `jeff-runtime`). |
| `base_checkpoint` | Where the base model was loaded from. |
| `max_adapters` | The maximum number of adapters it holds at once alongside the base, or `None` for no limit. |
| `base_max_options` | The maximum number of options a choice question may have on the base model. |
| `check(adapter_id, rows)` | Checks rows exactly as `predict` would, without running the model: `NotInstalled`, `InvalidRequest` or `InputTooLong`. |
| `prepare(adapter_id, rows)` | `check`, then the work to do before the model runs (such as tokenising), returning a `Prepared`. |
| `predict_many(groups)` | One batch: a list of (adapter ID, `Prepared`) groups, and one `EngineResult` or error per group, in order. |
| `add_adapter(adapter_id, path)`, `remove_adapter(adapter_id)` | Load or unload an adapter while the others keep serving. |

A `Prepared` holds a request's checked `rows`, and the engine's own preparation of them: `items` (one per row, or
`None`), `route` (the engine's name for how the rows will run) and `generation` (which load of the adapter the rows
were prepared for, so rows prepared before a reload are refused).

`jeff-runtime`'s engine follows `ServingEngine`; the [Jeff server](/docs/guides/running-server) and
`jeff runtime serve` are built on it.

## Next steps

- [Connect](/docs/sdk/connect): the ready-made ways to reach an engine.
- [Deploying jeff-server](/docs/guides/running): engines, batching and backends in production.
