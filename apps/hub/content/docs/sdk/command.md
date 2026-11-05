---
title: The jeff command
---
# The jeff command

The Python package `jeff-sdk` installs a command, `jeff`, with five subcommands. Use the same command whichever SDK you
call Jeff from: the Node, Rust, C# and Java SDKs have no command of their own, and use `jeff runtime serve` to start
the engine they attach to on your machine. `jeff gen` generates Python only. Each subcommand prints its options with
`--help` (for example `jeff test --help`). When a command fails with a Jeff error, it prints `jeff: ` and the error's message, and exits
with status 1. Invalid command arguments (such as a missing required option) print the usage and exit with
status 2.

| Command | What it does |
| - | - |
| `jeff test` | Run published test sets, or your own cases |
| `jeff calibrate` | Fit per-function thresholds and write them to the catalogue |
| `jeff gen` | Generate typed code for functions or adapters |
| `jeff mcp` | Serve Jeff's functions as MCP tools over stdio |
| `jeff runtime` | The engine process: `serve`, `quantize`, `gate`, `health` |

`jeff test`, `jeff calibrate` and `jeff runtime serve`, `quantize` and `gate` run the models on this machine, so they
need `jeff-runtime` (see [Install](/docs/sdk/install)); `jeff runtime health` only asks a running engine, and does
not. `jeff mcp` needs the `mcp` extra.

## jeff test

Runs functions on this machine against their published test sets, or against cases of your own, and reports each
function's accuracy.

```bash
jeff test guard
jeff test triage.route --sample 200
jeff test triage --mine my-cases.jsonl --save-baseline baseline.json
jeff test triage --mine my-cases.jsonl --against baseline.json
```

| Option | What it does |
| - | - |
| `functions` | Adapters or functions, e.g. `triage` or `triage.route`. |
| `--sample N` | Score a fixed-seed random sample of this many rows per adapter. |
| `--mine FILE` | Your own cases: JSON lines `{id, function, input, context?, labels?, expected}`. |
| `--save-baseline FILE` | With `--mine`: write the versions and each case's result to this file. |
| `--against FILE` | With `--mine`: fail on any case that passed in this baseline and fails now. |
| `--engine-rows` | Send each published row to the engine as written, instead of rebuilding it as your call. |
| `--backend` | `auto` (the default), `mlx`, `torch` or `fast`, as for `Jeff.local`. |
| `--json` | Print each function's report as one line of JSON. |

For a published test set it prints, per function, the accuracy with its 95% interval, how many decisions abstained,
and the median time per decision. With `--mine` it prints how many cases pass and names the failing ones, and exits
with status 1 if any fail. With `--against`, it exits with status 1 when the baseline has a case your file does not
(compare the same cases), for a case that passed in the baseline and fails now, and for a failing case the baseline
does not have; a case that already failed in the baseline is reported but
not counted, and any change in adapter version is reported.

## jeff calibrate

Fits each function's confidence threshold, the lowest confidence at which its answers reach a target precision, and
writes it to the adapter's manifest. Decisions below that threshold then abstain.

```bash
jeff calibrate ./my-adapter --target-precision 0.95
```

| Option | What it does |
| - | - |
| `adapter` | An adapter (every function) or one function, e.g. `triage` or `triage.urgency`; or an adapter folder (a path with a `/`, holding `manifest.yaml` and `adapter_config.json`): every function, written into the folder's `manifest.yaml`. |
| `--catalogue FOLDER` | The catalogue folder to read and write. Required when you name a catalogue adapter or function: the default is the repository's `catalogue/` when jeff runs from a checkout of its source, which a `pip install` does not have. For an adapter folder, the catalogue read for its base (default: the catalogue shipped with jeff), never written. |
| `--target-precision P` | Required. The precision, from 0 to 1, the answers at or above the threshold must reach. |
| `--data URI` | The rows to calibrate on, instead of the calibration set the manifest names. |
| `--split-test` | For an adapter with no calibration set: calibrate on half of its test set (seed 0), and report accuracy with `jeff test` on the other half only. |
| `--backend` | `auto`, `mlx`, `torch` or `fast`. |
| `--base FOLDER` | An INT4 base folder (`jeff runtime quantize`): fit the functions' thresholds on it, leaving out the calibration rows its quantiser saw, and write them as their `calibration_by_format` entries for its `format_id`. |

Naming an adapter is refused if any of its functions already has a threshold: name one uncalibrated function
instead, or set the calibrated functions' calibration to `null` in the manifest to fit them again.
When a function cannot reach the target, it is named, the others are still written, and the command exits with
status 1 (`CalibrationError`).

## jeff gen

Generates typed Python code for functions or adapters, so that a change to a function's labels or fields breaks your
type checks instead of your production code.

```bash
jeff gen guard triage.route --out jeff_types.py
```

| Option | What it does |
| - | - |
| `refs` | Adapters or functions, e.g. `triage` or `triage.route`. |
| `--lang` | Target language (only `python` exists in this version). |
| `--out FILE` | Required. File to write the generated code to. |

The file contains one class per function, named from its ID (`SafetyGuardAttempt` for `jeff.safety.guard.attempt`),
with its `id`, the type of its `Value` (for example `bool`, or a `Literal` of its label keys), its `Context` (a
`TypedDict` of its context fields by code name, each marked required or not), its `Input`, and a `value(decision)`
method that returns a decision's value as that type, raising `TypeError` for a decision of another function or a
value outside the type.

## jeff mcp

Serves one MCP tool per function, plus `jeff_dispatch` and `jeff_list_functions`, over stdio, for a coding agent or
any other MCP client. It needs the `mcp` extra: `pip install "jeff-sdk[mcp]"`.

```bash
jeff mcp --server {{origin}} --token-env JEFF_API_KEY --timeout-s 30
```

| Option | What it does |
| - | - |
| `--server URL` | A Jeff server's URL, e.g. `http://127.0.0.1:8000`. |
| `--socket PATH` | The engine's Unix socket (`jeff runtime serve --socket`). Give either `--server` or `--socket`. |
| `--token-env NAME` | With `--server`: the environment variable holding the server's bearer token (leave out for a server with no credentials). It is read before every request, so the token never appears in the process list. |
| `--timeout-s S` | Required. How long to wait for each answer from the engine or server, in seconds. |
| `--catalogue FOLDER` | The catalogue folder to read (default: the catalogue shipped with jeff). |

The tools, their names and what they answer are described in [MCP](/docs/guides/mcp); setting them up in a coding
agent is in [Jeff with coding agents](/docs/guides/coding-agents).

## jeff runtime serve

Serves decisions over a Unix socket: one engine process that loads the base model and adapters once, batches
requests, and answers the local client of every SDK (`Jeff.connect` in Python), `jeff mcp --socket` and the Jeff
server.

```bash
jeff runtime serve --socket /tmp/jeff-engine.sock --adapter guard \
  --batch-window-ms 5 --max-batch-rows 64 --queue-limit-rows 1024 --prepare-workers 2
```

| Option | What it does |
| - | - |
| `--socket PATH` | Required. Unix socket path to listen on. |
| `--adapter ID` | An adapter to load and serve at startup (repeatable); only when the served file does not exist yet (once it does, the engine starts from its list). |
| `--adapters-dir DIR` | A folder to find adapter folders in, at any depth (repeatable): each one found may be served, and `reload` looks again. |
| `--base-version ID@VERSION` | The base to load, e.g. `jeff-base@v1.3`: needed to start with no adapters; otherwise it must match the starting adapters' base. |
| `--path ID=FOLDER` | Load this adapter's weights from FOLDER instead of downloading them (repeatable). |
| `--catalogue FOLDER` | The catalogue folder to read (default: the catalogue shipped with jeff). |
| `--served-file FILE` | Read and write the served adapter list here, for `serve` and `reload`; when it exists, the engine starts from its list. |
| `--batch-window-ms MS` | Required. How long the first queued request waits for others to join its batch; above 0. |
| `--max-batch-rows N` | Required. The maximum number of rows in one batch; only on an NVIDIA GPU (the fast backend), the prefix pool (`--prefix-pool-mb`) must hold this many prefixes of 8,192 tokens at once, or the engine refuses to start. |
| `--queue-limit-rows N` | Required. The maximum number of rows that may wait in the queue; at least `--max-batch-rows`. A request that would queue more is refused with `Busy`. |
| `--backend` | `auto` (the default), `mlx`, `torch` or `fast`. |
| `--max-adapters N` | The maximum number of adapters the engine holds at once (alongside the base); required on the fast backend (NVIDIA), which allocates room for them at start, and refused on the others. |
| `--prefix-pool-mb MB` | Only on an NVIDIA GPU: GPU memory, in MB, for cached prompt prefixes, allocated at start; required on the fast backend (NVIDIA) and refused on the others. It must hold `--max-batch-rows` prefixes of 8,192 tokens: 7345 for 64 rows (8192 holds 266 prefixes of 1,000 tokens). |
| `--prepare-workers N` | Required, on every backend. How many requests are prepared at once on arrival (checked, and on the fast backend tokenised), each by a thread with its own copy of the tokeniser. |
| `--base FOLDER` | Serve this INT4 base folder (`jeff runtime quantize`) of the adapters' base instead of the bfloat16 base; refused unless its `gate.json` (`jeff runtime gate`) passes for every adapter served, at start and on every serve and reload. |
| `--max-tokens N` | The longest prompt, in tokens, the engine accepts (8,192 unless given); a longer one is refused with `InputTooLong`. |

Settings are checked before anything is downloaded or loaded, so an invalid setting fails immediately with a message
that names it.

Only on an NVIDIA GPU, add `--max-adapters` and `--prefix-pool-mb`. On a Mac or a CPU, leave them out: the command
above runs as is. See [Only on an NVIDIA GPU: the prefix pool](/docs/sdk/connect#section-only-on-an-nvidia-gpu-the-prefix-pool).

## jeff runtime quantize

Writes the INT4 base of a bfloat16 base (NVIDIA): a smaller copy of the base model that serves the same adapters.

| Option | What it does |
| - | - |
| `--base FOLDER` | Required. The bfloat16 base folder to quantise. |
| `--method gptq` | Required. The quantisation method (only GPTQ is built). |
| `--calibrate-on ID\|FOLDER` | Required, repeatable. An adapter whose calibration rows the quantiser uses: a catalogue ID or alias, or an adapter folder (a path with a `/`, holding `manifest.yaml`); the rows are read from the calibration data its manifest names, never its test rows. |
| `--calibrate-on-rows ID\|FOLDER=JSONL` | Draw this adapter's quantiser rows from JSONL (its training rows: `id`, `state` and `question` per line) instead of its calibration set (repeatable); when given, it must name every `--calibrate-on` adapter. The rows are a seeded sample, and none may be in the adapter's test or calibration set. |
| `--rows-per-adapter N` | Required. Calibration rows per adapter (the v1.3 INT4 base used 64). |
| `--path ID=FOLDER` | Load this catalogue adapter's weights from FOLDER instead of downloading them (repeatable). |
| `--catalogue FOLDER` | The catalogue folder to read (default: the catalogue shipped with jeff). |
| `--out FOLDER` | Required. The new INT4 base folder (must not exist). |

## jeff runtime gate

Gates an INT4 base for serving: accuracy, agreement, calibration and determinism against its bfloat16 base (NVIDIA).
It writes `gate.json` into the INT4 base folder, and exits with status 1 if the gate does not pass.

| Option | What it does |
| - | - |
| `--base FOLDER` | Required. The INT4 base folder (`jeff runtime quantize`); `gate.json` is written into it. |
| `--reference FOLDER` | Required. The bfloat16 base folder it was quantised from. |
| `--adapters ID\|FOLDER ...` | Required. The adapters to gate (every adapter this base will serve): catalogue IDs or aliases, or adapter folders (holding `manifest.yaml`). |
| `--path ID=FOLDER` | A catalogue adapter's weights folder, instead of downloading its open weights (repeatable). |
| `--catalogue FOLDER` | The catalogue folder to read (default: the catalogue shipped with jeff). |

## jeff runtime health

Checks the health of a running engine once. When the engine answers that it is ready, it prints the reply as JSON and
exits with status 0; otherwise it prints `jeff: The engine at <socket> ...` (that it is broken and why, or the
status it answered) and exits with status 1, so a start-up script can wait for the engine to be ready.

| Option | What it does |
| - | - |
| `--socket PATH` | Required. The engine's Unix socket (`jeff runtime serve --socket`). |
| `--timeout-s S` | Required. How long to wait for the engine to connect and answer, in seconds. |

## Next steps

- [Connect](/docs/sdk/connect#section-on-your-machine-free): use an engine started with `jeff runtime serve` from
  any SDK.
- [Deploying jeff-server](/docs/guides/running): engines and the server in production.
