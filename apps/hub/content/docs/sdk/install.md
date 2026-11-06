---
title: Install
---
# Install

Jeff has SDKs in five languages. They share the same functions, decisions, errors and error messages. Choose your
language in the tabs of any example on these pages, and every page will show code in that language.

| Language | Package | Needs | More |
| - | - | - | - |
| Python | `jeff-sdk` (imported as `jeff`) | Python 3.13 or later | [Python](/docs/sdk/python) |
| Node | `jeff-sdk` | Node 22 or later | [Node](/docs/sdk/node) |
| Rust | `jeff-sdk` (imported as `jeff_sdk`) | Rust 1.99.1 or later, Tokio | [Rust](/docs/sdk/rust) |
| C# | `Jeff.Sdk` | .NET 8 or later | [C#](/docs/sdk/csharp) |
| Java | `ai.jeffhub:jeff-sdk` (Java package `ai.jeff.sdk`) | Java 21 or later | [Java](/docs/sdk/java) |

The packages are published on PyPI, npm, crates.io, NuGet and Maven Central:

```bash
# Python
pip install jeff-sdk                         # through the API
pip install jeff-sdk "jeff-runtime[mac]"     # on your machine: Apple silicon
pip install jeff-sdk "jeff-runtime[cuda]"    # on your machine: NVIDIA GPU

# Node, Rust, C# and Java: the package, and uv once to run on your machine
curl -LsSf https://astral.sh/uv/install.sh | sh
npm install jeff-sdk                                                              # Node
cargo add jeff-sdk serde_json tokio --features tokio/macros,tokio/rt-multi-thread  # Rust
dotnet add package Jeff.Sdk                                                       # C#
implementation("ai.jeffhub:jeff-sdk:1.4.1")                                       # Java (Gradle)
```

For Maven, the coordinates are `ai.jeffhub:jeff-sdk:1.4.1` ([Java](/docs/sdk/java)). Rust's examples also use
`serde_json` and the Tokio runtime, hence the extra crates. The source of
every package is in the repository [github.com/firelex/jeff](https://github.com/firelex/jeff).

An SDK package is all you need to call a Jeff server [through the API](/docs/sdk/connect#section-through-the-api).
Each also includes a copy of the catalogue: the manifest for every open adapter, describing each function, the
context it takes and the answers it can return.

## On your machine

The models run only in the Jeff engine, the Python package `jeff-runtime`. How you get it
[on your own machine](/docs/sdk/connect#section-on-your-machine-free) depends on the language.

**Node, Rust, C# and Java**: install uv once, a tool that runs Python programs
([docs.astral.sh/uv](https://docs.astral.sh/uv/)), then the SDK package as above:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Local mode (`Jeff.local` with a list of adapters, in each language's own form) then finds the engine running for you
on this machine or starts it with uv, downloading the engine and the base model (about 2 GB) the first time. You do
not install `jeff-runtime` yourself. It runs on a Mac with Apple silicon or on Linux with an NVIDIA GPU; without uv,
local mode raises `RuntimeMissing`, and on any other machine `BackendUnavailable`. To start and manage an engine
yourself instead, install `jeff-runtime` as for Python below and see
[Using an engine you start yourself](/docs/sdk/connect#section-using-an-engine-you-start-yourself).

**Python**: install the engine with the Python SDK:

| You want to | Install |
| - | - |
| Call a Jeff server from Python | `pip install jeff-sdk` |
| Serve Jeff's functions to an AI agent as MCP tools (`jeff mcp`) | `pip install "jeff-sdk[mcp]"` |
| Run the models on your own machine (or start an engine yourself, for any language) | `pip install jeff-sdk jeff-runtime` |

`jeff-runtime` selects its backend automatically: on an NVIDIA GPU it uses its fast path, on Apple silicon it uses MLX, and
anywhere else it uses PyTorch on the CPU. Two `jeff-runtime` extras provide the dependencies for the first two backends:

```bash
pip install jeff-sdk "jeff-runtime[mac]"    # Apple silicon: MLX
pip install jeff-sdk "jeff-runtime[cuda]"   # NVIDIA GPU: the fast path's kernels
```

Python loads the engine into its own process with `Jeff.local`. Without `jeff-runtime`, `Jeff.local` raises
`RuntimeMissing`; on Apple silicon without the `mac` extra, it raises `BackendUnavailable`, naming the extra to
install.

The Python package also includes the `jeff` command (see [The jeff command](/docs/sdk/command)).

## Check that it works

This check needs no server or model: it reads the catalogue included with the package.

<!-- snippet: sdk/install-1 -->

## Next steps

- [Connect](/docs/sdk/connect): reach a server, or run the models on your own machine.
- [Functions](/docs/sdk/functions): bind a function and read its decisions.
- [Quick start](/docs/guides/quickstart): one call with `curl` and from Python, end to end.
