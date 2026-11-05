# Deploying Jeff

`deploy/run.sh <env file>` starts one Jeff instance in tmux from this checkout's `.venv`; `deploy/stop.sh <env file>`
stops it. Every setting comes from the env file. Settings named `JEFF_*` in the calling shell are ignored.

This plan does not touch datigator. The live jeff-server there keeps running as it is. Moving datigator to this server
is the cutover in the step 2 server design (Section 9), and
each step of it needs the owner's approval first.

## The two instances

| | Private (`private.env.example`) | Public (`public.env.example`) |
|---|---|---|
| Who | the owner: console, access log, admins | the internet, behind the reverse proxy |
| Credentials | `JEFF_API_KEY`, `JEFF_UI_USERS` | `JEFF_HUB_PUBLIC_KEY` (hub tokens), `JEFF_API_KEY` |
| Limits | none | `JEFF_PUBLIC=1` and `JEFF_RATE_LIMIT` (both required by the server) |
| Factory | `JEFF_FACTORY=owner`: the API's build endpoints and the factory worker | none: a public server refuses every factory setting |

Both can run on one machine side by side. They must not share a session prefix, a socket folder, a served-file folder,
a port or a GPU. They can share the adapter folders.

Set one up like this:

```bash
cp deploy/private.env.example ~/jeff-private.env
chmod 600 ~/jeff-private.env          # it holds keys
$EDITOR ~/jeff-private.env            # fill in the blanks
mkdir -p ~/jeff-private/sockets ~/jeff-private/served ~/jeff-private/factory
deploy/run.sh ~/jeff-private.env
curl http://127.0.0.1:8795/health
```

The server refuses any setting that is present but empty. Fill in every blank key, or delete the line.

## What run.sh starts

With `JEFF_SESSION_PREFIX=jeff-private` and `JEFF_GPUS=0,1`:

| Session | Process |
|---|---|
| `jeff-private-engine-0`, `jeff-private-engine-1` | `jeff runtime serve` with `CUDA_VISIBLE_DEVICES=<gpu>`, socket `<JEFF_SOCKET_DIR>/engine-<gpu>.sock`, served file `<JEFF_SERVED_FILE_DIR>/served-<gpu>.json`, an `--adapters-dir` for each folder in `JEFF_ADAPTER_DIRS`, `--base-version`, `--prepare-workers <JEFF_PREPARE_WORKERS>`, `--max-adapters <JEFF_MAX_ADAPTERS>` and `--prefix-pool-mb <JEFF_PREFIX_POOL_MB>`, and `--base <JEFF_BASE_FOLDER>` when it is set |
| `jeff-private-api` | `jeff-server serve --host --port --workers`, with `JEFF_ENGINE_SOCKETS` set to every engine's socket |
| `jeff-private-factory` | `jeff-server factory worker --engine-socket ...` with `CUDA_VISIBLE_DEVICES=<JEFF_FACTORY_GPU>` (only with `JEFF_FACTORY=owner`) |

On a Mac, `JEFF_GPUS=mlx` starts a single engine, `<prefix>-engine-mlx`, without `CUDA_VISIBLE_DEVICES`.

`JEFF_PREPARE_WORKERS` is required for every engine, the MLX one too, and passed to each as `--prepare-workers`: how
many requests one engine prepares at once as they arrive, each by a thread with its own copy of the tokenizer.
Preparing checks each request (its rows, its options, its length in tokens) on every backend, and on a GPU also splits
and tokenises each row, so the GPU thread never waits for the CPU (the examples use 4).

`JEFF_MAX_ADAPTERS` is required for engines on GPUs and passed to each as `--max-adapters`: the most adapters one
engine holds at once. The fast backend allocates GPU room for that many at start (about 34 MB each) and refuses to
load more. With `JEFF_GPUS=mlx` it is refused, because the MLX engine has no such limit.

`JEFF_PREFIX_POOL_MB` is required for engines on GPUs too, and passed to each as `--prefix-pool-mb`: the GPU memory,
in MB, each engine allocates at start for cached prompt prefixes. It must hold `JEFF_MAX_BATCH_ROWS` prefixes of
8,192 tokens at once, or the engine refuses to start (7,345 MB for 64 rows); the examples' 8,192 MB also holds 266
prefixes of 1,000 tokens. With `JEFF_GPUS=mlx` it is refused, because the MLX engine has no prefix pool.

`JEFF_BASE_FOLDER` is optional. Set, it names an INT4 base folder (made by `jeff runtime quantize` from the base in
`JEFF_BASE_VERSION`) that every GPU engine serves instead of the bfloat16 base, passed to each as `--base`. An engine
refuses it at start, and refuses any `serve` or `reload` (the console's and the factory's adapter changes included),
unless the folder's `gate.json`, written by `jeff runtime gate`, passed for every adapter it would serve, at that
adapter's version; the error names the adapter and the command to run. Run the gate on the GPU type the engines
serve on: `gate.json` records the GPU, but serving does not check it. Each calibrated function also needs a
threshold for that INT4 base: `jeff calibrate <adapter> --base <folder>` for a catalogue adapter (written into the
catalogue's manifest), or `jeff calibrate <adapter folder> --base <folder>` for an adapter under `JEFF_ADAPTER_DIRS`
(written into that folder's own `manifest.yaml`, the one the server reads). With `JEFF_GPUS=mlx` it is refused: INT4
needs the fast backend. Unset, the engines serve the bfloat16 base, the default. An INT4 engine takes about
0.3 GB more GPU memory than a bfloat16 one, not less (it keeps the packed INT4 weights beside a bfloat16 copy), so
`JEFF_MAX_ADAPTERS` and `JEFF_PREFIX_POOL_MB` stay as they are.

`JEFF_MAX_BATCH_ROWS` caps one micro-batch, every adapter's rows together. A GPU engine answers it in passes of up
to 64 rows (one CUDA graph replay each, rows of every adapter and prefix mixed), so 64 is the useful most there; a
lone request still runs alone, at batch-1 latency. A GPU engine records its 75 CUDA graphs at start, which adds about
25 s to its start (well inside the examples' `JEFF_ENGINE_START_S=600`) and reserves about 2.8 GB of GPU memory
beyond the base, the adapters and the prefix pool.

`JEFF_FACTORY_GPU` is required with `JEFF_FACTORY=owner` and refused without it. It names the one GPU the factory
worker trains on, as its `CUDA_VISIBLE_DEVICES`. It may be a GPU an engine uses too: training checks that
`JEFF_FACTORY_MIN_FREE_GB` is free first. On a Mac write `mlx`; the factory cannot train there, but it can interpret
descriptions and generate data. The trainer the worker starts gets a minimal environment: `PATH`, `HOME`, the locale,
the temporary folder, the Hugging Face cache and offline settings (never a token), `CUDA_VISIBLE_DEVICES` and a few
PyTorch settings. It never sees `JEFF_API_KEY`, `JEFF_TEACHER_KEY` or any other key.

The script starts the engines first. Before it starts the API, it waits until each engine answers `jeff runtime health`.
It allows each engine up to `JEFF_ENGINE_START_S` seconds, because loading the base can take tens of seconds. Then it
starts the API and waits up to `JEFF_API_START_S` seconds until `curl -fsS http://<JEFF_HOST>:<JEFF_PORT>/health`
answers. An API listening on every address (`0.0.0.0` or `::`) is asked on the loopback address (`127.0.0.1` or
`::1`). Only then does it start the factory worker and return 0. If a session ends while it waits, or the time runs out,
run.sh fails with a message naming the engine or the API and its log. It leaves running whatever it has already
started; stop that with stop.sh.

The served file holds the adapters each engine served last, and an engine starts from it once it exists. When it does
not exist yet (a first start), the engine starts with the adapters in the optional `JEFF_START_ADAPTERS` (separated by
commas). If that setting is empty too, the engine starts with the base alone, and adapters are switched on later from
the console or the API.

Each process runs through `deploy/session.sh`. That script reads the env file again, so keys never appear on a command
line. Every `tmux` command run.sh and stop.sh issue runs with the `JEFF_*` variables and any Hugging Face token
(`HF_TOKEN`, `HUGGING_FACE_HUB_TOKEN`) removed from its environment. The first one starts the tmux server, which keeps
that environment for as long as it runs, so the tmux server never holds a key. It appends the process's output to `~/<session>.log` and also shows it in the session (`tmux attach -t
<session>`). Each start writes a `===` line with the time and the command to the log.

## Stopping

```bash
deploy/stop.sh ~/jeff-private.env
```

stop.sh runs `tmux kill-session` on each of the instance's sessions by exact name: the factory worker first, then the
API, then the engines. It reports any session that is not running. Never use `pkill` or `kill` by pattern: on datigator,
`pkill -f` once matched the tmux server and killed every session on the machine.

run.sh never stops or kills anything itself. It refuses to start if any session of the instance already exists ("stop it
first with deploy/stop.sh"). It also refuses if an engine outside these sessions already answers at one of the
instance's sockets.

An engine stopped this way leaves its socket file behind. The next engine to start at that path finds that nothing
answers there and removes the file.

## Accounts mode

`public.env.example` has four more settings, `JEFF_ACCOUNTS_URL`, `JEFF_ACCOUNTS_TOKEN_FILE`,
`JEFF_ACCOUNTS_MAX_STALE_SECONDS` and `JEFF_USAGE_FILE`: all or none. Set, the server fetches its keys from the hub
named by `JEFF_ACCOUNTS_URL` and counts and reports customers' usage instead of serving only the owner and hub-signed
tokens. The token file holds the hub's internal API secret (a shared bearer token) and must never be in a repo --
keep it outside this checkout, like the env file itself. The server must run with `JEFF_WORKERS=1` in accounts mode,
so usage is counted in one process. The usage file must be kept across restarts (losing it loses the counts the hub
has not yet seen); backing it up is optional, since the hub keeps the last totals it was reported.
See `docs/release/accounts-go-live.md` for the full go-live procedure.

An optional fifth setting, `JEFF_ACCOUNTS_BYPASS_FILE`, is for when `JEFF_ACCOUNTS_URL` is itself a Vercel preview
behind Vercel's Deployment Protection: the file holds the project's protection-bypass secret, read with the same
checks as the token file (exists, chmod 600, one line, not empty), and the server sends it as the
`x-vercel-protection-bypass` header on both internal calls, only to `JEFF_ACCOUNTS_URL`. It needs the four accounts
settings above (accounts mode must be on); unset, no such header is sent, and the secret is never logged.

## Private adapters

An adapter whose manifest has `access` (`owner`, or `{accounts: [...]}`) is served only to the owner, or to the owner
and those hub accounts; every other caller is answered as if the server did not have it. The server reads `access` from the manifests it loads, so the manifests on the serving machine must carry
it: on datigator, the copies in `~/jeff-v14/adapters/<alias>/manifest.yaml` of every adapter that is not open (the
IDs `JEFF_PRIVATE_ADAPTERS` lists) need the line `access: owner`, as their manifests in
`results/v14-manifests` now have (a copy staged before this change does not). Add it there (or stage the manifests
again), then reload the adapters (`POST /v1/adapters/reload`) or restart the server; only with the owner's approval.
Without the line the adapter is served to every caller, as before. A server refuses to start, or to rebuild its
catalogue, when an open router routes to a private adapter, or when an adapter folder's manifest is more open than the
catalogue manifest it replaces.

On datigator those adapters are known only from their adapter folders, so nothing would notice a copy that lost the
line. `JEFF_PRIVATE_ADAPTERS` closes that: a comma-separated list of adapter IDs (not aliases) that must be private.
At startup and at every catalogue rebuild (a reload, a change of the served adapters) the server refuses, naming each
ID and its manifest file, when a listed adapter is not in the catalogue or its adapter folders, or its manifest has no
`access` field, or when an adapter whose manifest has an `access` field is not listed: the list and the manifests
must agree exactly, so adding a private adapter means adding its ID here too. In accounts mode (`JEFF_ACCOUNTS_URL`
set) the setting is required, and the server refuses to start without it, since account keys may call every adapter
that has no `access` field. The owner's list of every adapter that is not open is `deploy/private-adapters.txt`, one ID
per line, which the public copy of this repository leaves out; set it, comma-separated, on any server that serves them. Each build also
logs every private adapter with its access (`Private adapters in the catalogue: ...`), which is the quick check after
a deploy.

## Requirements

- curl, for the API's `/health` check.
- tmux 3.0 or later. run.sh gives `new-session` the command as separate arguments, and older versions do not take it
  that way.
- `.venv` in this checkout, with `jeff`, `jeff-server` and `jeff-runtime`. jeff-runtime needs its `mac` extra (Apple
  silicon) or its `cuda` extra (NVIDIA). On the Mac, `uv sync` installs all of this. The factory worker also needs
  jeff-server's `factory` extra, which adds jeff-runtime, torch and transformers.
- These folders must already exist: `JEFF_SOCKET_DIR`, `JEFF_SERVED_FILE_DIR` and every folder in `JEFF_ADAPTER_DIRS`.
  run.sh does not create them.
- `JEFF_FACTORY_DIR` must be on a local disk, not a network share. The factory's job queue and its stores are locked
  with `flock`, and `flock` is not reliable over NFS or SMB.
- The same goes for the first folder in `JEFF_ADAPTER_DIRS`. The API's adapter changes (`PUT /v1/adapters/served`,
  `POST /v1/adapters/reload`) and the factory worker's publish take one `flock` there, on
  `.jeff-adapter-change.lock`, so an API change never lands between the worker's reload of an engine and its serve.
  An API with `JEFF_FACTORY=owner` therefore needs `JEFF_ADAPTER_DIRS`. Adapter discovery skips the file: it looks
  only at folders, and skips hidden ones.
- A Unix socket path must stay under about 100 bytes (104 on macOS), so keep `JEFF_SOCKET_DIR` short.
