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
pip install jeff-sdk                   # Python
npm install jeff-sdk                   # Node
cargo add jeff-sdk                     # Rust
dotnet add package Jeff.Sdk            # C#
```

For Java, add `ai.jeffhub:jeff-sdk:1.4.0` to your Maven or Gradle build ([Java](/docs/sdk/java)). The source of
every package is in the repository [github.com/firelex/jeff](https://github.com/firelex/jeff).

An SDK package is all you need to call a Jeff server [through the API](/docs/sdk/connect#section-through-the-api).
Each also includes a copy of the catalogue: the manifest for every open adapter, describing each function, the
context it takes and the answers it can return.

## On your machine

The models run only in the Jeff engine, the Python package `jeff-runtime`. To run them
[on your own machine](/docs/sdk/connect#section-on-your-machine-free), install it with the Python SDK:

| You want to | Install |
| - | - |
| Call a Jeff server from Python | `pip install jeff-sdk` |
| Serve Jeff's functions to an AI agent as MCP tools (`jeff mcp`) | `pip install "jeff-sdk[mcp]"` |
| Run the models on your own machine, from any language | `pip install jeff-sdk jeff-runtime` |

`jeff-runtime` selects its backend automatically: on an NVIDIA GPU it uses its fast path, on Apple silicon it uses MLX, and
anywhere else it uses PyTorch on the CPU. Two `jeff-runtime` extras provide the dependencies for the first two backends:

```bash
pip install jeff-sdk "jeff-runtime[mac]"    # Apple silicon: MLX
pip install jeff-sdk "jeff-runtime[cuda]"   # NVIDIA GPU: the fast path's kernels
```

Python loads the engine into its own process with `Jeff.local`. Without `jeff-runtime`, `Jeff.local` raises
`RuntimeMissing`; on Apple silicon without the `mac` extra, it raises `BackendUnavailable`, naming the extra to
install. Node, Rust, C# and Java attach to an engine you start with `jeff runtime serve`, which comes with the Python
packages (see [Connect](/docs/sdk/connect#section-on-your-machine-free)).

The Python package also includes the `jeff` command (see [The jeff command](/docs/sdk/command)).

## Check that it works

This check needs no server or model: it reads the catalogue included with the package.

<!-- snippet: sdk/install-1 -->

## Next steps

- [Connect](/docs/sdk/connect): reach a server, or run the models on your own machine.
- [Functions](/docs/sdk/functions): bind a function and read its decisions.
- [Quick start](/docs/guides/quickstart): one call with `curl` and from Python, end to end.
