---
title: Deploying jeff-server
---
# Deploying jeff-server

> This page describes running this server as a whole: its engines, the API and the adapter factory. [Running the server](/docs/guides/running-server) starts one engine and the API by hand.

A deployment consists of three kinds of process. One **engine** per GPU loads the base checkpoint and the served
[adapters](/docs/guides/adapters) once and answers decisions over a Unix socket. The **API** (`jeff-server serve`) handles
HTTP requests in front of every engine, and serves the Jeff Console at `/ui` and the OpenAPI description at `/openapi.json`. The
**factory worker** (`jeff-server factory worker`) runs the builds started from the adapter builder in the Jeff
Console, on a server that builds adapters.

## Install and start

```bash
uv sync                                                       # the engine, the API and the console
cp deploy/private.env.example ~/jeff.env && chmod 600 ~/jeff.env
$EDITOR ~/jeff.env                                            # fill in every blank, or delete the line; JEFF_PORT=8000
deploy/run.sh ~/jeff.env
curl http://127.0.0.1:8000/health
```

`deploy/run.sh` starts each process in its own tmux session, logging to `~/<session>.log`: the engines first, then,
once every engine answers `jeff runtime health`, the API, then, once the API answers `/health`, the factory worker. It
fails, naming the session and its log, if a process stops or does not answer in time. It refuses to start over an
instance that is already running, and it never kills anything. The tmux server never sees a key: the env file's
settings reach each process through the env file alone. `deploy/stop.sh ~/jeff.env` ends the
instance's sessions by name. `deploy/README.md` in the repository covers the session names and the requirements.

`GET /health` answers `"ready"` when every engine is up and they agree on what they serve, `"degraded"` when some
engine is down but the others agree, and `"broken"` when no engine is ready or they disagree. While the engines
disagree (a published adapter is switched on one engine at a time), calls to adapters every engine serves are still
answered.

## Settings

All settings are environment variables; `deploy/run.sh` reads them from the env file and ignores `JEFF_*` variables
in the calling shell. A value the server does not understand, or one that is set but empty, stops it at start-up
with a message naming the variable; nothing is silently replaced by a default.

The engines (`deploy/run.sh` turns these into `jeff runtime serve` flags):

| Variable | Default | What it does |
| - | - | - |
| `JEFF_SESSION_PREFIX` | required | The tmux session names: `<prefix>-engine-<gpu>`, `<prefix>-api`, `<prefix>-factory` |
| `JEFF_GPUS` | required | One engine per GPU index, for example `0,1`; `mlx` for one engine on a Mac |
| `JEFF_SOCKET_DIR` | required | Where the engines' sockets go (`engine-<gpu>.sock`); keep the path short |
| `JEFF_SERVED_FILE_DIR` | required | Where each engine keeps the list of adapters it serves (`served-<gpu>.json`). An engine starts from its list, so switching an adapter on or off survives a restart |
| `JEFF_ADAPTER_DIRS` | required | The folders of adapter folders, separated by `:`. Every folder under them holding a `manifest.yaml` and an `adapter_config.json` may be served; the API reads the same manifests, and the builder publishes into the first folder. Two adapters with the same ID stop the engine, naming both folders |
| `JEFF_BASE_VERSION` | required | The base to load, for example `jeff-base@v1.3`. Every adapter found must be trained on it |
| `JEFF_START_ADAPTERS` | unset | Adapters to serve on the very first start, separated by commas, before any served list exists. Unset: the engine starts with the base alone, and adapters are switched on later |
| `JEFF_BASE_FOLDER` | unset | An INT4 base folder (`jeff runtime quantize`) that every GPU engine serves instead of the bfloat16 base. Refused unless its `gate.json` (`jeff runtime gate`) passed for every adapter served; refused with `JEFF_GPUS=mlx`. Unset: the bfloat16 base |
| `JEFF_ENGINE_START_S` | required | How long each engine may take to load before `run.sh` gives up |
| `JEFF_API_START_S` | required | How long the API may take to answer `/health` before `run.sh` gives up |
| `JEFF_BATCH_WINDOW_MS`, `JEFF_MAX_BATCH_ROWS`, `JEFF_QUEUE_LIMIT_ROWS` | required | How long an engine waits to gather a batch, the most rows in one batch (all adapters' rows together; 64 on a GPU, where a pass holds at most 64 rows), and the most rows queued before a request is refused as busy |
| `JEFF_PREPARE_WORKERS` | required | How many requests one engine prepares at once as they arrive (checked, and on a GPU split and tokenised), each on a thread with its own copy of the tokenizer. Required for every engine, the MLX one too; the examples use 4 |
| `JEFF_MAX_ADAPTERS` | required on GPUs | The most adapters one GPU engine holds at once. It reserves GPU room for that many at start-up (about 34 MB each) and refuses to load more. Refused with `JEFF_GPUS=mlx` |
| `JEFF_PREFIX_POOL_MB` | required on GPUs | The GPU memory, in MB, each GPU engine reserves at start-up for cached prompt prefixes. It must hold `JEFF_MAX_BATCH_ROWS` prefixes of 8,192 tokens (7,345 MB for 64 rows), or the engine refuses to start; the examples use 8,192. Refused with `JEFF_GPUS=mlx` |
| `JEFF_HOST`, `JEFF_PORT`, `JEFF_WORKERS` | required | The API's address, port and number of worker processes |

The API (the full list is on [Running the server](/docs/guides/running-server)):

| Variable | Default | What it does |
| - | - | - |
| `JEFF_MODEL_NAME` | required | The base's name in the older question API and `/v1/models`, for example `jeff-v1.3` |
| `JEFF_API_KEY` | unset | The owner's key: every licence, switching adapters, and the adapter builder |
| `JEFF_UI_USERS` | unset | The console's sign-in file, made with `jeff-server users <file> add <login>` (it prints the password). A signed-in session counts as the owner. Sessions last 30 days; `jeff-server users <file> sign-out-all` ends them all |
| `JEFF_HUB_PUBLIC_KEY` | unset | The hub's public key, to accept the tokens the hub signs for customers |
| `JEFF_PUBLIC` | unset | `1` for a server reachable from the internet: `/health` shows only the status to callers who are not signed in, adapters cannot be switched or reloaded, the adapter builder is refused, and a credential and `JEFF_RATE_LIMIT` are required |
| `JEFF_RATE_LIMIT` | no limit | `"<requests per second>/<concurrent requests>"` per caller, kept by each API worker process |
| `JEFF_ACCESS_LOG`, `JEFF_ADMINS`, `JEFF_GEOIP_DB` | unset | The access log: a JSON-lines file of every sign-in, console visit and API call, with the user, the address and (with an IP location database such as DB-IP's free City Lite) the city and country. The logins in `JEFF_ADMINS` (comma-separated) see it on the console's Access log page |
| `JEFF_ENGINE_TIMEOUT_S` | `30.0` | How long the API waits for an engine's answer |

### Adapter builder

The adapter builder in the Jeff Console trains new adapters on this server's base. It is off unless you turn it on.

| Variable | What it does |
| - | - |
| `JEFF_FACTORY` | `off` (default: only recorded replays are shown) or `owner` (builds adapters) |
| `JEFF_FACTORY_DIR` | Folder for build runs, replays and the job queue, on a local disk (not a network share) |
| `JEFF_TEACHER_URL`, `JEFF_TEACHER_KEY`, `JEFF_TEACHER_MODEL` | An OpenAI-compatible model that writes the training data. With the builder off, these three still offer description checks and "Try it" previews |
| `JEFF_TRAINER` | The folder of the pinned trainer install (`.venv/bin/jeff-train` and `TRAINER_COMMIT`) |
| `JEFF_FACTORY_MIN_FREE_GB` | GPU memory, in GB, that must be free before training starts (required: there is no default) |
| `JEFF_FACTORY_GPU` | The one GPU index the factory worker trains on (`deploy/run.sh` sets it as the worker's `CUDA_VISIBLE_DEVICES`); required with `JEFF_FACTORY=owner`, `mlx` on a Mac, where it cannot train |

There is no separate builder key: build actions need the owner, meaning the API key or a signed-in console session.
The API only queues builds; the factory worker runs them, one at a time, and `deploy/run.sh` starts it when
`JEFF_FACTORY=owner`. Started by hand, it is `jeff-server factory worker --engine-socket <socket>` (once per engine),
with the same settings (`JEFF_MODEL_NAME` and `JEFF_ADAPTER_DIRS` included), and it needs the `factory` extra of
`jeff-server`. The trainer it starts gets no key: only the paths, the Hugging Face cache settings and the GPU. The API's
adapter changes and the worker's publishing operations share a lock file, `.jeff-adapter-change.lock`, in the first folder of
`JEFF_ADAPTER_DIRS`, which must be on a local disk. When no worker is running, the Make an
adapter page says so. A finished build is published into the first folder of `JEFF_ADAPTER_DIRS` with its own
manifest and calibration, enabled on every engine, and callable at once through `/v1/decide`, MCP and the
playground, with no restart.

## Batching and busy engines

Each engine gathers requests into batches: it waits up to `JEFF_BATCH_WINDOW_MS` for rows, runs at most
`JEFF_MAX_BATCH_ROWS` in one pass, and keeps at most `JEFF_QUEUE_LIMIT_ROWS` waiting. A request that would queue more
gets `529` with `Retry-After`. With several GPUs, each call goes to the engine chosen by its function, context and
labels, so repeated calls reuse that engine's cached prompt.

An engine restarted on its own needs no API restart: calls in flight fail, and the next call reconnects. Adapter
folders are read only when an engine starts or reloads, never while it answers.

## Backends and speed

| Backend | Hardware | Reads | Typical decision time (0.8B base) |
| - | - | - | - |
| PyTorch, fast path | NVIDIA GPU | Text and images | About 22 ms (RTX PRO 6000) |
| MLX | Apple silicon | Text | About 80 ms (M4 Max) |
| PyTorch | CPU | Text and images | Not yet measured |

The engine picks the backend itself: the fast path when an NVIDIA GPU is present, MLX on Apple silicon (install the
`mac` extra of `jeff-runtime`), PyTorch otherwise. On MLX the rows of a batch run one after another, so keep
`JEFF_MAX_BATCH_ROWS` small there.

## Security

- Set `JEFF_API_KEY` or `JEFF_UI_USERS` whenever the server listens on anything but `127.0.0.1`. With no credential
  at all, every caller is the owner.
- Customers get hub-signed tokens (`JEFF_HUB_PUBLIC_KEY`), which cannot switch adapters or use the builder.
- `/health` and `/ui` are open; `/openapi.json` and every `/v1` endpoint need a credential once one is configured.
- Five failed sign-ins for one login (or thirty in total) within 15 minutes lock sign-in for a while.
- Request bodies are not logged or stored; the access log records the user, the address, the path and the status.
- Env files hold keys: keep them outside the repository, readable only by you (`chmod 600`).

## Behind a proxy

Terminate TLS at a reverse proxy (nginx, Caddy). Pass through the `x-request-id` header so clients can quote a request id,
and allow WebSocket upgrades for [the channel](/docs/guides/channel).

