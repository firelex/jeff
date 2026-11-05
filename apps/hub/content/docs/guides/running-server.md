---
title: Running the server
---
# Running the server

`jeff-server` is the HTTP API in front of one or more engine processes. It needs at least one engine already
running, so start that first.

## 1. Start an engine process

The engine loads the base model and the named adapters once and serves decisions over a Unix socket, so several
callers can share one loaded model without each waiting for its own model to load:

```bash
uv run python -m jeff.cli runtime serve --socket /path/e.sock --adapter triage --adapter spam \
    --batch-window-ms 1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 4
```

`--batch-window-ms`, `--max-batch-rows`, `--queue-limit-rows` and `--prepare-workers` are required: how long the
engine waits to collect rows into a batch, how many rows may go into one batch (every adapter's rows together), how
many rows may be queued before a new request is refused as busy, and how many requests are prepared at once as they
arrive (each by a thread with its own tokenizer). On an NVIDIA GPU, `--max-adapters N` is also required: the engine keeps room for N adapters on the
GPU, allocated at start-up, and refuses to load more. So is `--prefix-pool-mb MB`: GPU memory for cached prompt
prefixes, allocated at start-up, which must hold `--max-batch-rows` prefixes of 8,192 tokens at once (7,345 MB for 64
rows; 8,192 MB also holds 266 prefixes of 1,000 tokens). On a Mac or a CPU, leave both out: the command above runs as
is (both flags are refused there). `--adapter` is repeatable; `--path
ID=FOLDER` loads one adapter's weights from a local folder instead of downloading them.

For an engine that keeps running, add `--served-file PATH`, `--adapters-dir DIR` and `--base-version ID@VERSION`. The
served file holds the adapters the engine serves; once it exists the engine starts from it, and `--adapter` is
refused when that file exists. Every adapter folder under an adapters directory (a folder holding `manifest.yaml` and
`adapter_config.json`) may be served, and a reload looks for new or changed folders. `--base-version` names the base,
so an engine can start with no adapters at all. `jeff runtime health --socket /path/e.sock --timeout-s 5` exits 0
once the engine is ready. [Deploying jeff-server](/docs/guides/running) starts all of this with one script.

On an NVIDIA GPU an engine can serve an INT4 base instead of the bfloat16 one: `--base FOLDER` names an INT4 base
folder made by `jeff runtime quantize`. The engine refuses it, at start-up and whenever adapters are served or reloaded, unless the
folder's `gate.json` (written by `jeff runtime gate`) passed for every adapter it would serve, at that adapter's
version; the error names the adapter and the command to run. Run the gate on the GPU type that will serve the base:
`gate.json` records the GPU, but serving does not check it. Each calibrated function also needs its
threshold for that INT4 base (`jeff calibrate <adapter> --base FOLDER`; for an adapter served from an adapters folder,
`jeff calibrate <adapter folder> --base FOLDER`, which writes into that folder's `manifest.yaml`), or it is refused
when it is called.

## 2. Point the server at it

```bash
JEFF_ENGINE_SOCKETS=/path/e.sock JEFF_MODEL_NAME=jeff-v1.3 \
    uv run jeff-server serve --host 127.0.0.1 --port 8000 --workers 1
```

`--host`, `--port` and `--workers` have no default: binding to every interface (`--host 0.0.0.0`) and running more
than one uvicorn worker process are both deliberate choices — `jeff-server
serve` refuses to start without all three, naming whichever is missing. Every other setting comes from the process
environment, read once at start-up, not from `serve`'s own flags, so the same environment configures every worker
identically. A setting with no default below is required; the server refuses to start without it (or, for a
malformed value, at all), naming the variable. A variable that is set but empty is refused too, never read as
unset.

Each worker keeps its own rate limits, sign-in lockouts and request counters, so `JEFF_RATE_LIMIT` applies per
worker: with `--workers N` a caller can get up to N times the rate (and N times the concurrent requests).

## Settings

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `JEFF_ENGINE_SOCKETS` | yes | — | One or more engine socket paths, separated by `:` |
| `JEFF_MODEL_NAME` | yes | — | The base's public name for the older question API and `/v1/models`; no spaces, and not `jeff`, `jeff-latest` or `jeff-qwen3.8-27b` (the base already answers to those) |
| `JEFF_ENGINE_TIMEOUT_S` | no | `30.0` | Seconds to wait for an engine's reply before raising |
| `JEFF_CATALOGUE` | no | the catalogue shipped with `jeff` | A catalogue folder to read instead |
| `JEFF_ADAPTER_DIRS` | required when `JEFF_FACTORY=owner`, else no | none | Folders of adapter folders, separated by `:` (the engines' `--adapters-dir`); their manifests join the catalogue. Adapter changes take a lock file, `.jeff-adapter-change.lock`, in the first folder, shared with the factory worker |
| `JEFF_GRAPHS` | no | none registered | A folder of graph `.yaml` files to register at start-up, served from `GET /v1/graphs` and runnable by id from `POST /v1/decide` |
| `JEFF_PUBLIC` | no | unset (private) | `1` for a server reachable from the internet; then a credential and `JEFF_RATE_LIMIT` are both required. Any other value, `0` included, is refused: leave it unset for a private server |
| `JEFF_API_KEY` | no | none | A static API key; whoever holds it is the owner (every licence, and access to the owner-only endpoints) |
| `JEFF_HUB_PUBLIC_KEY` | no | none | The hub's Ed25519 public key (PEM path), to verify hub-signed caller tokens |
| `JEFF_UI_USERS` | no | none | The console sign-in file (`jeff-server users`) |
| `JEFF_RATE_LIMIT` | required when `JEFF_PUBLIC=1`, else no | no limit | `"<requests per second>/<concurrent requests>"`, per caller and per worker process |
| `JEFF_ACCESS_LOG` | no | nothing logged | File to append one JSON line per `/v1/*` request to |
| `JEFF_ADMINS` | no | none | Comma-separated logins the access log marks as admins |
| `JEFF_GEOIP_DB` | no | none | A GeoIP database for the access log; only usable together with `JEFF_ACCESS_LOG` |

With no credential configured (`JEFF_API_KEY`, `JEFF_HUB_PUBLIC_KEY` and `JEFF_UI_USERS` all unset) the server is
open: every caller is the owner. This is refused outright when `JEFF_PUBLIC=1`.

The adapter builder's settings (`JEFF_FACTORY` and the rest) are on [Deploying jeff-server](/docs/guides/running).

The server needs no restart when an engine restarts: calls in flight fail, and the next call reconnects (at most one
attempt a second while the engine is down). New adapters require no restart either: each API worker rebuilds its list
of functions when the engines report a change (or an engine restarts), so a newly reloaded or published adapter is
callable at once. While a published adapter is switched on one engine at a time, calls to adapters served by every engine
continue to receive answers.

## What it serves

| Endpoint | Purpose |
|---|---|
| `POST /v1/decide` | Decisions — [Functions and /v1/decide](/docs/guides/decide), [Graphs](/docs/guides/graphs) |
| `GET /v1/functions` | Every loaded function |
| `GET /v1/graphs` | Graphs registered from `JEFF_GRAPHS` |
| `GET /v1/adapters`, `PUT /v1/adapters/served`, `POST /v1/adapters/reload` | Which adapters are served (owner-only) |
| `/v1/channel` | [The low-latency channel](/docs/guides/channel) |
| `/mcp` | [MCP](/docs/guides/mcp) |
| `/ui` | The Jeff Console, for the people who run the server |
| `GET /health`, `GET /metrics` | State; Prometheus metrics |

`GET /openapi.json` serves the committed OpenAPI description of every endpoint, request and response shape and
error — the [API reference](/docs/api) page is built from it.

## Next steps

- [Functions and /v1/decide](/docs/guides/decide): the main decision endpoint.
- [Errors](/docs/guides/errors): what a misconfigured or overloaded server answers with.

