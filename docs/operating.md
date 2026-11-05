# Operating Jeff

The details behind the short [README](../README.md): the SDK in depth, the commands, and how to run the engine, the
server and the rest of the stack yourself. The user guides are on [jeffhub.ai](https://jeffhub.ai/docs/guides/introduction).

## A local decision

Bind a function to a context (fields that stay the same across calls) and a label set (here, the caller's own
support teams), then call it per message:

```python
from jeff import Jeff

TEAMS = {
    "deliveries": "Deliveries: late, lost or damaged deliveries, and delivery bookings",
    "refunds": "Refunds and payments: refunds, charges and invoices",
    "assembly": "Assembly service: booking and complaints about assembly",
    "account": "Account access: logins, passwords and account details",
}
CONTEXT = {"company": "A mid-sized online furniture retailer shipping across Europe.", "channel": "email"}
MESSAGE = ("Subject: Crushed wardrobe\n\nHi, the wardrobe I ordered (order 55120) arrived today and the box was "
           "crushed. Two doors are split. I want a replacement or my money back. This is the second time.\n\nAnna")

jeff = Jeff.local(["triage"])
route = jeff.function("triage.route", context=CONTEXT, labels=TEAMS)
decision = route(MESSAGE)

print(decision.value)       # 'deliveries'
print(decision.confidence)  # 0.998
print(decision.abstained)   # False
```

Run for real on this machine (Apple silicon, MLX backend):

```
value: deliveries
confidence: 0.9983643020262455
abstained: False
timing: {'path': 'mlx', 'model_ms': 73.813}
```

Binding checks the context and labels once and fixes the prompt prefix; every later call on the same `route` object
reuses that binding. On NVIDIA's fast path the prefix is also processed once and cached, so later calls only process
the message; on MLX (as in this run) and on the standard PyTorch path each call is a full model pass.

Context is passed by each field's code name. A manifest keeps every context and input key exactly as its adapter was
trained, spaces included (for example `counterparty name`), because that is what the prompt shows the model; callers
use the field's code name instead: a key that already looks like a name in code (`^[a-z][a-z0-9_]*$`) is its own
code name; any other key is lower-cased, with each run of other characters turned into one underscore
(`counterparty_name`), or the manifest declares a `code_name` when that rule gives no usable name. The SDK's
`context=`, HTTP and channel `context` objects, MCP tool arguments and the console all take code names, and
`GET /v1/functions` lists each field's `code_name` beside its `key`. Passing the exact key with spaces is
`UnknownContext`, which names the code name to use; two fields of one manifest may not share a code name.

An adapter trained on several request shapes (for example ground's `grounding`, which reads `(sources, answer)`,
and `rerank`, which reads a `question` alone) offers each shape as its own function: such a function declares its own `context` and
`input` in the manifest, both together, and they replace the adapter's for that function alone. Binding, the prompt,
`GET /v1/functions`, the MCP tools and `jeff gen` all use each function's own fields; a function that declares
neither takes the adapter's, as before. Test rows find their function by `tests.function_by: state_shape` (the row's
state keys, in order, are exactly the function's context keys and then its input key, or the state is a plain string
for a text input), alone or in a list with `question_type`, `id_suffix`, `source_kind` or `id_pattern`; `tests.kinds`
may map one `source.kind` to a list of functions when that kind holds rows of several shapes (another criterion must
then tell them apart, or the manifest is refused when it loads). `id_pattern` matches the whole row id against each
function's regular expression in `tests.id_patterns`, for row ids that do not end with the function key. A test row that maps to no function,
or to several, raises `ManifestError` naming the row, in `jeff test` and `jeff calibrate` as in the importers.

Plain-text input: an adapter trained on a state that is one plain string, not an object of named fields (v1.4's
knowledge-qa `blank` and `question`, for example), has `input.form: text` in its manifest. The caller's `input` must
then be a string, and Jeff sends it to the model unwrapped, exactly as trained; the input key appears nowhere in the
prompt and only names the input in `GET /v1/functions`, the console and `jeff gen` (`Input = str`). Such a function
has no context fields, and a call that passes context is refused with `UnknownContext`; a routed call (dispatch) to it
drops other adapters' context, as for any function without those fields. Every other input has `input.form: fields`.

Fixed labels with a caller subset: a choice function's fixed labels carry `subset: true` or `false`. With `false` the
caller passes no `labels` and every fixed label is offered. With `true` (v1.4 soc's `action`, trained on rows that
offer four of its five actions) the caller may pass `labels=` as a non-empty list of fixed keys, without repeats, to
offer only those, listed in the manifest's order with their fixed descriptions; leaving `labels=` out offers them all.
A direct call, dispatch, HTTP, the channel, the MCP tools (an optional `labels` array) and the console's checkboxes
all take the subset; a key that is not one of the function's labels is `InvalidLabels`.

## A decision graph

A graph wires functions together with declarative conditions (data, not code), so it runs the same way locally or
through the server. This is the support-intake shape from the design spec (Section 10), using the real `spam` and
`triage` adapters:

```yaml
# support_intake.yaml
graph: jeff.graphs.support_intake
version: 1.0.0
input: message
params: [company, channel, teams]
nodes:
  kind:    { call: jeff.safety.spam.message_type, context: { channel: { param: channel } } }
  team:    { call: jeff.support.triage.route,
             context: { company: { param: company }, channel: { param: channel } }, labels: { param: teams },
             when: { node: kind, is: legitimate } }
  urgency: { call: jeff.support.triage.urgency,
             context: { company: { param: company }, channel: { param: channel } },
             when: { node: team, abstained: false } }
  ticket:  { code: make_ticket, after: [team, urgency] }
outputs: [kind, team, urgency, ticket]
```

```python
jeff = Jeff.local(["triage", "spam"])
result = jeff.run_graph(
    "support_intake.yaml", MESSAGE,
    {"company": CONTEXT["company"], "channel": CONTEXT["channel"], "teams": TEAMS},
    code={"make_ticket": lambda state: {"team": state["nodes"]["team"].value}},
)
print(result.outputs["kind"].value, result.outputs["team"].value, result.outputs["ticket"])
# legitimate deliveries {'team': 'deliveries'}
```

Before the first decision the runner checks the whole graph against the catalogue (every function known, installed
and bound to its params; every policy valid). It then batches every node whose inputs are ready into one call to
`decide` (so independent branches share model passes, and on the fast path cached prefixes), evaluates the `when`
conditions, and repeats until every node has run or been skipped. `result.to_json()` gives the result as the JSON
document of `contract/schemas/graph_result.schema.json`.

## Escalation on low confidence

Nothing escalates by default — `on_low_confidence` must be asked for. This run (real model, same message) actually
hits this path: `triage.urgency` abstains on this message (confidence below its calibrated threshold), so the
`human` step runs and its answer becomes the Decision:

```python
from jeff.escalation import escalate, human

def ask_a_person(question):
    # show question.instructions, question.options and question.state to someone, return their chosen key
    return next(iter(question.options))

decision = jeff.rate(
    MESSAGE, function="triage.urgency", context=CONTEXT,
    on_low_confidence=escalate(human(ask_a_person)),
)
print(decision.decided_by)    # 'human'
print(decision.escalations)   # (EscalationRecord(decided_by='jeff.support.triage.urgency@1.3.0', value=3,
                               #  confidence=0.4345150382971283,
                               #  reason='abstained (confidence 0.435 < threshold 0.771902)'),)
```

`question.state` is what the model read: an object of named fields (the context and the input) for most functions,
and the plain input text itself for a function whose input is plain text (`input.form: text`).

A step that fails (unreachable, times out, answers outside the label set) raises — the low-confidence answer is
never returned as if nothing had happened. `escalate(step, step, ..., budget_ms=...)` tries each step in order;
`remote(...)` and `llm(...)` are the other two step kinds (Section 8 of the spec).

## Commands

```bash
uv run jeff test triage.route --sample 20     # score a random sample of the published test set
uv run jeff gen triage.route --out jeff_types.py   # generate a typed Python module from the manifest
uv run jeff calibrate triage --target-precision 0.95   # fit per-function thresholds (illustrative: triage is
                                                        # already calibrated in this repo; see results/ below)
uv run jeff runtime quantize ... / jeff runtime gate ...   # make and gate an INT4 base: "INT4 bases (NVIDIA)" below
```

`jeff calibrate` takes an adapter (every function) or one function (`triage.urgency`), and writes into the
repository's `catalogue/` when run from a checkout; elsewhere pass `--catalogue PATH`. It also takes an adapter folder
(a path with a `/`, holding `manifest.yaml` and `adapter_config.json`, as `JEFF_ADAPTER_DIRS` serves): then it
calibrates every function, loads the weights from the folder and writes into the folder's own `manifest.yaml`, the
manifest a server reads in place of the catalogue's. A function that already has a calibration block is refused rather
than refitted.

On an INT4 base (`jeff runtime quantize`) a calibrated function decides with its own threshold for those exact weights:
`jeff calibrate triage --base <INT4 base folder> --target-precision 0.95` fits it on the calibration set minus the
rows the quantiser saw (listed in the folder's `quantization.json`) and writes it under the function's
`calibration_by_format`, keyed by the folder's `format_id`, refusing a format the function already has. An engine
serving an INT4 base refuses to bind a calibrated function that has no entry for its `format_id`, naming that command;
it never falls back to the bfloat16 threshold.

Test and calibration sets are named in manifests as `hf://datasets/<owner>/<repo>@<revision>/<file>` and downloaded
on first use. To read private data from a local copy instead, offline, set `JEFF_DATA_MIRROR` to
`<owner>/<repo>@<revision>=<local folder>` (several mappings separated by `;`), for example
`JEFF_DATA_MIRROR=teamsuzie/jeff-data@main=/mnt/d/jeff-data`. A URI of exactly that dataset and revision is then read
from `<local folder>/<file>` by `jeff test`, `jeff calibrate`, `jeff runtime quantize --calibrate-on` and
`jeff runtime gate`, which then run fully offline. Nothing is downloaded, and a file missing from the mirror is an
error naming the local path and the URI. The private importer (`scripts/import_private.py`) reads its test and
calibration rows from the mirror too, but it is not offline: it still lists files on Hugging Face (the data folder,
to check that both files are there; the base repos in `catalogue/bases.yaml`; and the adapter folders unless
`--adapters-root` is given). The same dataset at another revision (a pinned commit) is not mapped and is downloaded as
before, so offline it fails. A malformed value is refused when it is read, naming the expected form. Manifests keep
their `hf://` paths. What was read from a mirror is recorded beside the `hf://` path, since a mirror is a copy of a
revision such as `main` that may since have moved: `jeff calibrate` (and the gate's refit) add `data_mirror`
(`path`, the local file, and `sha256`, its SHA-256) to each calibration block, and `jeff runtime quantize` adds
`data_mirror` per adapter to its `calibration` record in `quantization.json` and to each entry of
`not_in_test_sets`. Without a mirror nothing is added.

`jeff gen` writes one class per function; its `Context` (a TypedDict of the context fields by code name) and `Input`
types describe what to bind and pass, and its `value(decision)` raises `TypeError` — deliberately, as a type check in
your code — when the decision comes from another function or holds a value outside the generated type.

`jeff test --mine cases.jsonl` runs your own labelled cases and exits non-zero on any failure, so it works in CI.
Add `--save-baseline b.json` to record the adapter versions and each case's result, then `--against b.json` on a
later run to fail only on cases that used to pass and now don't (this replaces a `jeff.lock`-style pinned file). A
case that already failed in the baseline and still fails is reported, not counted.

## Engine process

`jeff runtime serve` loads the base model and the named adapters once and serves decisions over a Unix socket, so
several callers share one loaded model instead of each paying its own load time:

```bash
uv run python -m jeff.cli runtime serve --socket /path/e.sock --adapter triage --adapter spam \
    --batch-window-ms 1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 4
```

The four scheduler flags are required, with no defaults:
- `--batch-window-ms`: how long the engine waits to collect rows into a micro-batch.
- `--max-batch-rows`: the most rows in one micro-batch, every adapter's rows together. 64 suits an NVIDIA GPU (the
  largest pass the fast path records); keep it small on MLX (below).
- `--queue-limit-rows`: how many rows may wait before a new request is refused as busy.
- `--prepare-workers`: how many requests are prepared at once as they arrive, each by a thread with its own tokenizer
  (every backend; 4 in the examples).

On an NVIDIA GPU (the fast backend) two more are required, and other backends refuse them:
- `--max-adapters N`: the engine stacks every adapter on the GPU, in room for N adapters allocated at start (about
  34 MB each), and refuses to load more.
- `--prefix-pool-mb MB`: the GPU memory for cached prompt prefixes, allocated at start. It must hold
  `--max-batch-rows` prefixes of 8,192 tokens at once (7,345 MB for 64 rows); 8,192 MB also holds 266 prefixes of
  1,000 tokens.

`--adapter` is repeatable; `--path ID=FOLDER` loads one adapter's weights from a local folder instead of downloading
them, and `--catalogue`, `--backend` and `--max-tokens` are also available (see `jeff runtime serve --help`).

How the fast backend batches:
- Each request is prepared when it arrives, on a preparation thread: checked, its prompt split where the part that
  changes per request begins (by its own adapter's prompt layout), and the new part tokenised.
- The GPU thread takes up to `--max-batch-rows` waiting rows, of any adapters, as one micro-batch. Requests already
  part-way through are served first, then new ones in arrival order (`contract/socket.md`, "How requests are
  batched").
- Each row's fixed part (its prefix) is looked up in the prefix pool; the micro-batch's new prefixes are processed
  together in batched prefix passes.
- The rows are grouped by new-token count (buckets of 32 to 1,024 tokens) into passes of up to 64 rows. A pass is one
  CUDA graph replay in which every row has its own adapter and its own prefix.
- A lone request runs in a pass of one: on the RTX PRO 6000, 2.4 to 3.9 ms per call (wall-clock p50, copies and
  synchronisation included) at 32 to 256 new tokens,
  faster than the single-row path it replaced. Under load, passes of 64 mixed rows answer
  about 3,200 rows/s at 32 new tokens, 1,850 at 64 and 930 at 128 (`results/step2b1/throughput.json`), against 335
  to 415 rows/s one row at a time.
- Rows with images or more than 1,024 new tokens go to the standard path (a PyTorch forward pass), decided at
  preparation.
- At start, every fast engine (`Jeff.local` on a GPU too) records its 75 CUDA graphs: about 25 s and 2.8 GB of GPU
  memory.

A long-running engine keeps its served adapters in a served file and finds adapter folders on disk:

```bash
uv run python -m jeff.cli runtime serve --socket /path/e.sock --served-file /path/served.json \
    --adapters-dir /path/adapters --base-version jeff-base@v1.3 \
    --batch-window-ms 1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 4
```

- `--served-file PATH` is where the engine writes the adapters it serves after every change, and where it starts
  from: when the file exists, the engine loads the adapters it lists, and `--adapter` beside it is refused. When it
  does not exist yet, the engine starts from `--adapter` and writes the file once they are loaded.
- `--adapters-dir DIR` (repeatable) adds every adapter folder under `DIR`, at any depth, to what the engine may serve.
  A folder is an adapter when it holds `manifest.yaml` and `adapter_config.json`; folders whose names start with `.`
  are skipped. Two folders with the same ID, an invalid manifest or an adapter trained on another base stop the
  engine, naming the folders. An adapter that is both in the catalogue and in a folder is loaded from the folder.
- `--base-version ID@VERSION` names the base to load. It is needed to start with no adapters at all (the engine then
  serves the base alone), and otherwise must match the starting adapters' base.
- The `reload` operation (`POST /v1/adapters/reload` on the server) looks for adapter folders again, loads again any
  served adapter whose files changed, and re-applies the served file. Folders are read only at startup and on
  `reload`, never while answering. The exact rules are in `contract/socket.md` ("The served file", "Adapter
  folders").

`jeff runtime health --socket /path/e.sock --timeout-s 5` prints an engine's health as JSON and exits 0 only when it
is ready (1 when it is broken or nothing listens there), so scripts can wait for an engine to finish loading.

Connect to it with `jeff.connect`, which returns an ordinary `Jeff`:

```python
import jeff

j = jeff.connect("/path/e.sock", timeout_s=30)
decision = j.function("triage.route", context=CONTEXT, labels=TEAMS)(MESSAGE)
```

Batching rewards the NVIDIA fast path, where many rows run through the GPU in one pass. MLX (Apple silicon) runs the
rows of a batch one after another, and every request in a batch gets its answer only when the whole batch is done,
so on MLX a large `--max-batch-rows` adds latency (a one-row request waits for every row batched with it) without
adding throughput; keep it small there. The wire protocol (framing, the `hello` exchange, multiplexing many in-flight requests on one connection,
typed errors) is `contract/socket.md`. The HTTP API in front of it is below.

## INT4 bases (NVIDIA)

An engine on an NVIDIA GPU can serve an INT4 base instead of the bfloat16 one. An INT4 base keeps the 186 matrices of
the text decoder as 4-bit integers with one bfloat16 scale per 64 inputs (264 MB instead of 995 MB; the folder is
0.98 GB instead of 1.71 GB); the embedding, the vision tower, the readout and every adapter stay bfloat16. It is
chosen for latency at small batches, where reading the weights dominates, not for memory: an INT4 engine keeps a
bfloat16 copy for the standard path and the large passes beside the packed integers, so it takes about 0.3 GB more GPU
memory than a bfloat16 one. Each CUDA graph uses the INT4 kernel or cuBLAS on the bfloat16 copy, whichever it timed
faster at start, so that an INT4 engine is never slower than its own bfloat16 copy: that is the design's intent, not
yet measured (the GPU benchmark of step 2b-2 is pending). bfloat16 stays the default; INT4 is a choice per
deployment, made only after its gate passes.

Four steps, run on the GPU type that will serve (the gate records the GPU; serving does not check it):

```bash
# 1. quantise (GPTQ; 5 minutes for the 0.8B base on the RTX PRO 6000), on 64 calibration rows of each adapter named
#    (never test rows); writes a new folder whose format_id names these exact weights
uv run jeff runtime quantize --base /models/jeff-base-v1.3 --method gptq --rows-per-adapter 64 \
    --calibrate-on triage --calibrate-on spam --out /models/jeff-base-v1.3-int4-gptq
# 2. gate it against the bfloat16 base for every adapter it will serve (estimated at about an hour for nine
#    adapters); writes gate.json into the INT4 folder and exits 1 if the gate fails
uv run jeff runtime gate --base /models/jeff-base-v1.3-int4-gptq --reference /models/jeff-base-v1.3 \
    --adapters triage spam
# 3. fit each calibrated function's threshold for this INT4 base (writes calibration_by_format into the manifest):
#    a catalogue adapter writes into the catalogue's manifest; an adapter folder (as JEFF_ADAPTER_DIRS serves)
#    into the folder's own manifest.yaml
uv run jeff calibrate triage --base /models/jeff-base-v1.3-int4-gptq --target-precision 0.95
uv run jeff calibrate /srv/jeff-adapters/spam --base /models/jeff-base-v1.3-int4-gptq --target-precision 0.95
# 4. serve it
uv run python -m jeff.cli runtime serve --socket /path/e.sock --base /models/jeff-base-v1.3-int4-gptq \
    --adapter triage --adapter spam --batch-window-ms 1 --max-batch-rows 64 --queue-limit-rows 4096 \
    --prepare-workers 4 --max-adapters 32 --prefix-pool-mb 8192
```

- `--calibrate-on` and `--adapters` take catalogue IDs or aliases (with `--path ID=FOLDER` for local weights) or
  adapter folders. Only `--method gptq` is built: AWQ + GPTQ measured worse on triage (design Section 1).
- **The gate** (`jeff runtime gate`, step 2b-2 design Section 6) measures both bases in full, each in its own engine
  as served: accuracy on every published test row in engine mode (each row's own state and question) and in template
  mode (rows rebuilt as calls, as `jeff test` does), on the same rows for both bases; the fast path against the
  standard path on the INT4 base, on up to 1,000 test rows per adapter; every calibrated function's threshold
  refitted for the INT4 base; and the quantiser's determinism (the first two layers quantised twice, byte for byte).
  It passes when the mean accuracy change over the adapters is at least -0.3 points and no adapter's is below -0.5,
  in each mode; the fast path makes the standard path's choice on at least 99% of each adapter's rows (a row whose
  top two standard-path options are exactly equal agrees either way); every refit reaches its target precision; and
  the two quantiser runs are identical. `gate.json` records every number (per adapter and function: rows, accuracy
  in both bases, the change, agreement, calibration error in both bases, the refitted thresholds), the method, the
  GPU and driver, and the code versions. A gate is recorded once per INT4 folder; an existing `gate.json` is never
  overwritten (move it aside to gate again).
- **Serving checks the gate.** `jeff runtime serve --base` refuses an INT4 base, at start and on every `serve` and
  `reload`, unless its `gate.json` is this exact base's (its `format_id` and `int4.safetensors` hash) and passed, with
  a passing entry for every adapter it would serve at that adapter's version. The error names the adapter and the
  command to run. An adapter added later needs the gate run again with it (and every other adapter served), which
  may require a new INT4 base with its rows included (design Risk 2).
- **Thresholds per format.** A calibrated function served on an INT4 base decides with its `calibration_by_format`
  entry for that base's `format_id` (see "Commands" above); without one it is refused, never answered with the
  bfloat16 threshold. Temperatures are not refitted (design Section 5).
- `--max-adapters` and `--prefix-pool-mb` mean the same on an INT4 base and need no change.

## Running the server

`jeff-server` (the `apps/server` package) is the HTTP API in front of one or more engine processes. Start an engine
process first (see "Engine process" above), then point `jeff-server serve` at its socket:

```bash
uv run python -m jeff.cli runtime serve --socket /path/e.sock --adapter triage --adapter spam \
    --batch-window-ms 1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 4 &

JEFF_ENGINE_SOCKETS=/path/e.sock JEFF_MODEL_NAME=jeff-v1.3 \
    uv run jeff-server serve --host 127.0.0.1 --port 8000 --workers 1
```

`--host`, `--port` and `--workers` all have no default: binding to every interface (`--host 0.0.0.0`) or running
more than one uvicorn worker process (each one opens its own connections to the engine process(es), rather than
sharing one) are both deliberate choices, not something to fall into silently — `jeff-server serve` on its own
fails loudly, naming whichever of the three is missing. Every other setting — which engine sockets to use, the
model's public name, credentials, rate limits, and so on — comes from the process environment, read once at startup
by `ServerSettings.from_env` (`apps/server/src/jeff_server/settings.py`), not from `serve`'s own flags, so the same
environment configures every worker identically. A setting with no default below is required; the server refuses to
start without it (or, for a malformed value, at all), naming the variable. A variable that is set but empty (for
example `JEFF_API_KEY=""` from an unset `$KEY` in a deploy script) is refused too, never read as unset: set a value
or unset it.

Each worker keeps its own rate limits, sign-in lockouts and request counters. `JEFF_RATE_LIMIT` therefore applies
per worker: with `--workers N` a caller can get up to N times the rate (and N times the concurrent requests), so set
it accordingly. `/metrics` labels the API's own request counts with `worker` (the worker's process id).

| Variable | Required | Default | Meaning |
|---|---|---|---|
| `JEFF_ENGINE_SOCKETS` | yes | — | One or more engine socket paths, separated by `:` |
| `JEFF_MODEL_NAME` | yes | — | The base's public name for `/v1/systemone` and `/v1/models`; no spaces, and not `jeff`, `jeff-latest` or `jeff-qwen3.8-27b` (the base already answers to those) |
| `JEFF_ENGINE_TIMEOUT_S` | no | `30.0` | Seconds to wait for an engine's reply before raising |
| `JEFF_CATALOGUE` | no | the catalogue shipped with `jeff` | A catalogue folder to read instead |
| `JEFF_ADAPTER_DIRS` | required when `JEFF_FACTORY=owner`, else no | none | Folders of adapter folders, separated by `:`, the same ones the engines get as `--adapters-dir`; each adapter's `manifest.yaml` joins the catalogue (a found manifest replaces a catalogue manifest with the same ID). Each folder must exist. The factory worker publishes into the first one |
| `JEFF_GRAPHS` | no | none registered | A folder of graph `.yaml` files to register at startup, served from `GET /v1/graphs` and runnable by id from `POST /v1/decide` |
| `JEFF_PUBLIC` | no | unset (private) | `1` for a server reachable from the internet; then a credential and `JEFF_RATE_LIMIT` are both required. Any other value, `0` included, is refused: leave it unset for a private server |
| `JEFF_API_KEY` | no | none | A static API key; whoever holds it is the owner (every licence, and access to the owner-only endpoints) |
| `JEFF_HUB_PUBLIC_KEY` | no | none | The hub's Ed25519 public key (PEM path), to verify hub-signed caller tokens |
| `JEFF_UI_USERS` | no | none | The console sign-in file (`jeff-server users`) |
| `JEFF_RATE_LIMIT` | required when `JEFF_PUBLIC=1`, else no | no limit | `"<requests per second>/<concurrent requests>"`, per caller and per worker process (with `--workers N`, up to N times this per caller) |
| `JEFF_ACCESS_LOG` | no | nothing logged | File to append one JSON line per `/v1/*` request to |
| `JEFF_ADMINS` | no | none | Comma-separated logins the access log marks as admins |
| `JEFF_GEOIP_DB` | no | none | A GeoIP database for the access log; only usable together with `JEFF_ACCESS_LOG` |
| `JEFF_ACCOUNTS_URL` | all four or none (accounts mode) | — | The hub's origin; its internal API is fetched at `/api/internal/v1/keys` and `/api/internal/v1/usage`, `https://` only |
| `JEFF_ACCOUNTS_TOKEN_FILE` | all four or none (accounts mode) | — | Path to the shared secret sent as that API's bearer token, `chmod 600`, kept outside every repo |
| `JEFF_ACCOUNTS_MAX_STALE_SECONDS` | all four or none (accounts mode) | — | Account keys are refused once the fetched key list is older than this |
| `JEFF_USAGE_FILE` | all four or none (accounts mode) | — | The usage counters (JSON Lines), kept across restarts; the server reads and compacts it at startup |

The adapter factory has its own settings (`JEFF_FACTORY`, `JEFF_FACTORY_DIR`, the teacher and the trainer); see
"The adapter factory" below.

With no credential configured (`JEFF_API_KEY`, `JEFF_HUB_PUBLIC_KEY` and `JEFF_UI_USERS` all unset) the server is
open: every caller is the owner. This is refused outright when `JEFF_PUBLIC=1`.

**A restarted engine.** The server does not need restarting when an engine restarts. Requests in flight when an
engine's connection drops fail with `EngineError` and are never resent. The next request to that engine reconnects
(connect, the `hello` exchange, and a fresh list of what it serves), and `/health` is ready again. While the engine is
still down, a failed attempt is remembered for one second: requests in that second fail at once with an error naming
the attempt and its age, and the next one after it tries again. Nothing is added to a request while the connection is
healthy. `/metrics` counts reconnects per engine as `jeff_engine_reconnects_total{socket="<path>"}`, and each one
is logged as a warning.

**New adapters without a restart.** Each API worker builds its catalogue at startup from `JEFF_CATALOGUE` and the
manifests in `JEFF_ADAPTER_DIRS`, and builds it again when the engines' `adapters_version` changes (an adapter is
switched on or off, or reloaded) or an engine restarts (counted by its reconnect: a restarted engine counts its
versions from 0 again). The check is one comparison per request, against the versions the engines already sent;
folders are read only when something changed. `POST /v1/adapters/reload` rebuilds the catalogue of the worker
that answers it at once, after the engines have reloaded; the other workers rebuild on their next request after the
version changes. A request for a function this worker does not know asks the engines for their versions (at most once
a second per worker) and is planned again if they changed, so a newly published adapter is never refused for long.
A rebuild that fails (a broken manifest, an engine down) fails the request that triggered it, names the cause, and
leaves the previous catalogue answering. Engines that disagree on what they serve are not a failure: the factory's
publish changes them one at a time. The previous catalogue keeps answering, a request for an adapter every engine
serves is answered, and the rebuild runs again once the last engine has changed.

A decision, the same as the local example above, over HTTP:

```bash
curl -s http://localhost:8000/v1/decide \
  -H 'content-type: application/json' \
  -d '{"calls": [{"function": "triage.route", "input": "My wardrobe arrived broken",
                  "context": {"company": "A furniture shop.", "channel": "email"},
                  "labels": {"deliveries": "Deliveries", "refunds": "Refunds"}}]}'
# {"decisions": [{"function": "jeff.support.triage.route", "value": "deliveries", "confidence": 0.998, ...}]}
```

Add `-H "authorization: Bearer <token or API key>"` once a credential is configured. `POST /v1/decide` also takes
`dispatch` (routed decisions) and `graph` request shapes, and `on_low_confidence` as locally — see "Escalation on
low confidence" above and Section 5 of the server design spec for the exact JSON escalation form. `GET
/openapi.json` serves the committed OpenAPI description of every endpoint, request/response shape and error; `GET
/health` reports whether each engine process is reachable and what it serves, and needs no credentials (a public
server answers only the bare status, with no detail, to an unsigned caller).

## Remote mode

`Jeff.remote` gives Python code the same surface as `Jeff.local` — binding, labels, policies and escalation all work
exactly as locally — except that every decision is made by calling a Jeff server instead of an engine in this
process:

```python
import jeff

remote = jeff.Jeff.remote("http://localhost:8000", token=None, timeout_s=30.0)
decision = remote.function("triage.route", context=CONTEXT, labels=TEAMS)(MESSAGE)
remote.close()  # or use it as a context manager: `with jeff.Jeff.remote(...) as remote:`
```

`token` is the server's bearer credential: a string (an API key or a hub-signed token), a callable returning one (so
it can be refreshed), or `None` — only for a server with no credentials configured. `timeout_s` is required and must
be a finite number above 0; it bounds how long each request may take. Connecting reads `GET /v1/functions` once and
checks every adapter the server serves against this process's own catalogue (the one `jeff` ships with, unless you
pass `catalogue=`): a version mismatch raises `ContractError` naming the adapter and both versions, rather than
mapping that adapter's labels with the wrong manifest. Point `catalogue=` at the server's own manifests (for example
a checkout of its `catalogue/` folder) to connect to a server running another version.
`remote.engine.installed(refresh=True)` reads the served list again, for example after a `PUT /v1/adapters/served`
elsewhere changes it.

Each operation costs one request to the server: `decide`, a bound function's call or `many()`, an interface
shorthand (`rate`, `classify`, ...), `dispatch`, and a graph with no code nodes. `on_low_confidence` policies of
`"return"`, `"error"` or `escalate(function_step(...))` run on the server, in the same request; a policy with an
`llm(...)`, `human(...)` or `remote(...)` step runs here instead, on the decision the server returns (only this
process holds those callables), which costs one further request per escalation step that actually runs. A graph
with no code nodes runs as one request, entirely on the server; a graph with any code node runs stage by stage from
this process instead — one request per stage that has model nodes, with each stage's code run here — because the
code can only run where it was defined. Every error is the same `jeff.errors` class local mode would raise, with
the same message, carrying the HTTP response's `x-request-id` as its `request_id` attribute.

One request carries at most 256 calls: `decide` with more than 256 calls, or a bound function's `many()` with more
than 256 inputs, raises `InvalidRequest` naming the limit before sending anything (it is never split into several
requests behind your back; send batches of at most 256). `decide([])` and `many([])` answer `[]` without a request,
as in local mode.

Pass `channel=True` to use the server's low-latency WebSocket channel (`/v1/channel`) instead of REST — one
long-lived connection, authenticated once, where a bound function becomes a *handle* on the server rather than a
request on every call:

```python
with jeff.Jeff.remote("http://localhost:8000", token=get_token, timeout_s=10.0, channel=True) as remote:
    route = remote.function("triage.route", context=CONTEXT, labels=TEAMS)  # one `bind` frame
    decision = route(MESSAGE)                                              # one `call` frame
```

A connection holds at most 1,024 live handles and 256 outstanding requests at once; one more of either fails at
once (`TooManyHandles`, `RateLimited`). When `token` is a callable, it is called for the token sent with `auth`, again
from 60 seconds before that token's `exp` (retried every 5 seconds, until a token expiring later is returned or
`exp` passes), and again for each `GET /v1/functions` — never before every call, as it is with `channel=False`. The
refresh window is measured against this machine's clock while the server checks expiry with its own, so keep both
clocks synchronised (NTP): a client clock running behind the server's by more than 60 seconds makes every refresh
arrive too late. A send that the socket accepts no bytes of for `timeout_s` (the server or the network stopped
taking data) closes the channel with `BackendUnavailable`, and every request waiting for a reply fails with that
error. There is no automatic reconnect on any failure — connect again with `Jeff.remote`.

Errors over the channel carry no request id (`request_id` is `None`: there is no HTTP response); instead each
connection has an id of its own, a short hex string, which every error the client raises for a failure the channel
sent carries as its `channel_id` attribute, and which the server names in its log lines and in the details of
those errors — quote it when asking the server's operator about a failure.

## MCP

Jeff's functions can also be called as MCP tools, so any MCP-aware agent can use them without learning `jeff`'s own
API. There is one tool per function the server can call now (every function of an adapter it serves, proprietary
ones included: calling one without its licence is a tool error `NotLicensed: ...`), named from its function ID with
the leading
`jeff.` removed and every `.` and `-` replaced with `_` (`jeff.support.triage.route` becomes `support_triage_route`),
plus two of Jeff's own: `jeff_dispatch` (route an input through the router functions to the function that should
answer it) and `jeff_list_functions` (the same listing as `GET /v1/functions`). Every tool call answers with the
whole `Decision` — `abstained` included, so the agent can see when Jeff is not confident enough to act — as both
the tool's text and its structured content. A `JeffError` becomes a tool error whose text starts with the error's type
(for example `NotLicensed: ...`) rather than an exception the agent's host has to unwrap.

A Jeff server mounts these tools at `/mcp` (streamable HTTP), behind the same bearer credentials and rate limiter as
`/v1`: every POST to `/mcp` — whatever MCP message it carries — counts as one request against the caller's rate
limit, so a fresh client's first tool call typically spends 3 to 4 requests of that budget (initialising the MCP
session, the call itself, and the SDK's own `tools/list`, which most MCP clients run once before calling a tool they
have not seen listed).

To serve the same tools over stdio, for a local agent (install the extra first: `pip install 'jeff[mcp]'`):

```bash
uv run jeff mcp --server http://localhost:8000 --token-env JEFF_TOKEN --timeout-s 30
# or, straight against an engine process (see "Engine process" above), with no server in between:
uv run jeff mcp --socket /path/e.sock --timeout-s 30
```

`--server` and `--socket` are mutually exclusive. `--token-env NAME` names an environment variable holding the
server's bearer token (so the token itself never appears in the process list); it is read again on every request,
so a token refreshed in that variable is picked up, and a variable that is unset when a request is made fails that
request with `Unauthorised` naming it. Leave it out for a server with no credentials, and leave it out entirely with
`--socket` (an engine socket takes no token).

## The console

`jeff-console` (`apps/server/ui`, a pnpm workspace package) is a small web console served by `jeff-server` itself at
`/ui`: sign in, browse every function the server serves, run one from a generated form or raw JSON and see its
`Decision`, switch adapters on and off, build adapters and watch recorded builds (the Factory page, owners only; see
"The adapter factory" below), read the access log (administrators only), browse the API reference (read
live from the server's own `GET /openapi.json`), and read the documentation. The built console is committed at
`apps/server/src/jeff_server/ui/` (`index.html` plus `assets/`) rather than built at deploy time, so `jeff-server
serve` needs nothing beyond that checkout to serve it.

Build it (from the repository root, with the pnpm workspace installed):

```bash
pnpm install --frozen-lockfile
pnpm --filter jeff-console build
```

This runs a type check (`tsc --noEmit`) then `vite build`, writing straight into `apps/server/src/jeff_server/ui/`
(`vite.config.ts` fixes `build.outDir` there and empties it first). Commit the result together with whatever source
change produced it -- `apps/server/tests/test_console_build.py` (a `slow` test, excluded from the default `uv run
pytest`, same as the model tests; run it with `uv run pytest -m slow apps/server/tests/test_console_build.py`)
rebuilds it into a temporary folder and fails if the committed build differs.

Develop against a running `jeff-server` (see "Running the server" above) instead of a static build:

```bash
JEFF_URL=http://127.0.0.1:8000 pnpm --filter jeff-console dev
```

The dev server runs on port 5190 and proxies `/v1`, `/health`, `/openapi.json` and the console's own JSON endpoints
(`/ui/login`, `/ui/me`, `/ui/events`, `/ui/examples`, `/ui/access-log`) to `JEFF_URL`; sign in with a login from
that server's `JEFF_UI_USERS` file.

Test:

```bash
pnpm test                               # every workspace package's unit tests (vitest); the hub's light ones only
pnpm --filter jeff-hub test:all         # before merging hub work: every hub unit test (light and heavy) and its e2e
pnpm typecheck                          # every workspace package's tsc --noEmit
pnpm --filter jeff-console exec playwright install chromium   # once, before the first e2e run
pnpm --filter jeff-console e2e          # the browser smoke test (Playwright, Chromium)
```

The hub's unit tests are two vitest projects (`apps/hub/vitest.config.ts`): `light`, which `pnpm test` runs in a few
seconds, and `heavy` (`pnpm --filter jeff-hub test:heavy`): the files that each start a Postgres database in memory
(PGlite), spawn child processes or render every page of the site, which spike the CPU. `test:all` runs both, then the
browser checks, with one worker each; nothing is left out of it.

The smoke test (`apps/server/ui/e2e/smoke.spec.ts`) drives the committed build in a real browser against a real
`jeff-server` over a fake engine (`apps/server/tests/console_server.py`, started by the suite's own Playwright
global setup, on its own free port, with its own temporary `JEFF_UI_USERS` file and two accounts): it signs in,
visits every page, runs a function and sees a `Decision`, switches an adapter off and on, opens a documentation
page, checks that only an administrator sees the access log, and checks that removing a signed-in user from the
users file mid-session sends the console back to sign-in with the server's own message on its next call. Each
page's screenshot, at 1440x900, is committed under `results/console/` for a quick visual review without running
the suite.

## The adapter factory

The factory builds a new adapter from a plain description of a decision: a teacher model (any OpenAI-compatible
endpoint) turns the description into questions and writes training data, the trainer (`jeff-train` from the training
repository) trains an adapter on the engines' base, and the result is calibrated, evaluated and published onto this
server. It is ported from jeff-server, with three changes: build steps run in a separate worker process, a published
adapter gets a manifest, and the build endpoints use the server's own owner credentials.

**Settings** (read by both `jeff-server serve` and the worker; a public server refuses all of them except
`JEFF_FACTORY=off`):

| Variable | Meaning |
|---|---|
| `JEFF_FACTORY` | `owner` (builds adapters) or `off`; required as soon as any variable below is set, and unset means off |
| `JEFF_FACTORY_DIR` | Folder for runs, previews, replays, description checks, the job queue and the worker's heartbeat; on a local disk (it is locked with `flock`) |
| `JEFF_TEACHER_URL`, `JEFF_TEACHER_KEY`, `JEFF_TEACHER_MODEL` | The teacher model: an OpenAI-compatible base URL, its key and its model name |
| `JEFF_TRAINER` | The folder of the pinned trainer install (`.venv/bin/jeff-train` and `TRAINER_COMMIT`); owner mode only |
| `JEFF_FACTORY_MIN_FREE_GB` | GPU memory, in GB, that must be free before training starts; owner mode only, with no default |

In owner mode every one of them is required. With the factory off, the recorded replays in `JEFF_FACTORY_DIR` are still
shown, and with the three teacher settings description checks and previews are offered too. There is no separate
build key: every `/v1/factory/*` route needs the owner's credentials (the API key or a console session); a hub token
gets 403 `NotLicensed`, and a build route on a server with the factory off gets 403 `FactoryOff`.

**The API** (`/v1/factory/*`, every route in `GET /openapi.json`) keeps jeff-server's routes and bodies: `mode`,
`description-checks`, `previews`, `runs` (start, import questions, edit and approve the spec, run a step or the rest,
the review gate, test sets, events as a server-sent stream, save as a replay), `testsets`, `references` and
`replays`, plus `GET /v1/factory/worker`, which says whether a worker is running (its heartbeat is at most 15 s old)
and which job it holds. A request that does not fit the run's state is 409 `Conflict`; an unknown run, test set,
reference or replay is 422 `InvalidRequest`.

**The worker** runs every step of a build run (`interpret`, `data_prep`, `generate`, `checks`, `train`, `calibrate`,
`evaluate`, `independent_test`, `publish`); the API only queues them, so restarting the API leaves a running step
alone:

```bash
JEFF_FACTORY=owner JEFF_FACTORY_DIR=... JEFF_ADAPTER_DIRS=/path/adapters JEFF_MODEL_NAME=jeff-v1.3 ... \
    uv run jeff-server factory worker --engine-socket /path/e.sock    # repeat --engine-socket for each engine
```

It reads the same environment as `jeff-server serve` and needs `JEFF_FACTORY=owner`, `JEFF_ADAPTER_DIRS` and
`JEFF_MODEL_NAME` (a published adapter may not take the base's name, as the API checks at startup). An API with
`JEFF_FACTORY=owner` needs `JEFF_ADAPTER_DIRS` too: the API's adapter changes and the worker's publish take turns on
an `flock` on `.jeff-adapter-change.lock` in its first folder (keep it on a local disk). The trainer the worker starts
gets a minimal environment with no key, and runs on the worker's `CUDA_VISIBLE_DEVICES` (`deploy/run.sh` sets it from
`JEFF_FACTORY_GPU`). It takes jobs one at a time, oldest first, across every run. Only one worker runs per factory folder; a second is refused,
naming the first one's process. The engines must already be running: the worker trains on the base they serve (read
from their health, which must agree, with the checkpoint a folder on this machine). At start it fails every step a
stopped worker left running ("the worker stopped during <step>", and the run is marked unclean), and it refuses to
start while a trainer from a stopped worker is still alive; it never kills anything itself. It needs jeff-server's
`factory` extra, which adds `jeff-runtime`, `torch` and `transformers` (`uv sync --package jeff-server --extra
factory`; in this checkout's development install they are already there).

**Previews** interpret a description into questions without generating data or training. They are teacher calls
only, so they run in the API process, at most four at once per API process (a fifth is 409 `Conflict`), and they work
with the factory off when the teacher settings are given. A description check is one synchronous teacher call in the
API.

**Publishing.** The `publish` step turns the trained run into an adapter this server serves, callable through
`Jeff`, `POST /v1/decide`, the channel and MCP, not only through `/v1/systemone`:

- The adapter's ID is `jeff.factory.<spec name>`, with alias `<spec name>`, version `1.0.0`, licence proprietary to the
  owner and weights `local` (the folder itself). Its context is the spec's family fields, all required, and its input
  is the live field. Each question becomes one function, keyed by its question ID in lower case with every other
  character replaced by `_`: a choice question is `classify`, a yes-or-no question is `detect`, a score is `rate`, with
  the trained wording as fixed labels. A question asked only after another answer is published as an ordinary
  function, with its condition in the manifest's `hub.notes`.
- Each function's calibration threshold is fitted at 0.95 precision on the run's own calibration split, scored with the
  requests exactly as Jeff sends them. A function no threshold reaches stops the publish.
- The folder is staged as `.<name>.publishing/` in the first `JEFF_ADAPTER_DIRS` folder (dot folders are never
  discovered), with `manifest.yaml`, `example.json`, `data_card.json` and its own `test.jsonl` and
  `calibration.jsonl`, checked against the catalogue the API would build, then renamed to `<name>/`. Every engine is
  sent `reload` and then `serve` with the new adapter added, and the example is decided through `Jeff` over the engine
  sockets. If anything fails after the rename, the engines stop serving it and the folder is moved to
  `.<name>.failed-<time>`, so it is never discovered half-published. The API workers pick the new adapter up on their
  next request, with no restart (see "New adapters without a restart" above).
- A run trained on plain text (a family with no named fields, such as a run started by importing questions) is
  refused at publish, with a `ManifestError` that says why: the factory only publishes adapters trained on named
  fields, and for this one Jeff would send a different prompt from the one it was trained on.

## Deployment

`deploy/` starts a whole instance in tmux from this checkout's `.venv`: one engine per GPU (or one MLX engine on a
Mac), the API, and with `JEFF_FACTORY=owner` the factory worker.

```bash
cp deploy/private.env.example ~/jeff-private.env && chmod 600 ~/jeff-private.env   # then fill in the blanks
deploy/run.sh ~/jeff-private.env
deploy/stop.sh ~/jeff-private.env
```

Every setting comes from the env file (`JEFF_*` variables in the calling shell are ignored). Each engine starts with
its own served file, an `--adapters-dir` for each folder of `JEFF_ADAPTER_DIRS` and `--base-version`; on a first start,
when no served file exists yet, it gets the optional `JEFF_START_ADAPTERS`. The optional `JEFF_BASE_FOLDER` gives every
GPU engine `--base`, to serve a gated INT4 base. run.sh waits for each engine with `jeff
runtime health` before it starts the API, refuses to start over sessions or engines that are already running, and
never kills anything; stop.sh ends the instance's sessions by exact name. `deploy/README.md` has the private and public
instances, the session names and logs, and the requirements. Moving datigator to this server is the cutover of the
server design spec (Section 9), and each step of it needs the owner's approval.


## From jeff-server's client

Code written against `jeff-server`'s own Python client can switch to this package without changing anything else:

```python
from jeff.systemone import Client

jeff = Client("http://localhost:8765", model="jeff-latest")
picked = jeff.choose("My parcel arrived crushed.", {"refunds": "Refunds", "parcels": "Damaged or lost parcels"},
                     "Which team should handle this?")
picked.key, picked.probability           # ("parcels", 0.91)
```

`jeff.systemone` is `jeff-server`'s client, ported into this package unchanged, calling the legacy `/v1/systemone`
API — same methods, same arguments, same behaviour. Its exceptions (`jeff.systemone.Unauthorised`,
`jeff.systemone.InvalidRequest`, and so on) are their own class hierarchy, separate from `jeff.errors`, so import
them from `jeff.systemone` rather than from `jeff` or `jeff.errors`, or you will catch the wrong class.

## Results

- [`results/step1-accuracy.md`](results/step1-accuracy.md) — the 13 shipped adapters, each checked against its
  published accuracy.
- [`results/step1-calibration.md`](results/step1-calibration.md) — per-function calibration thresholds and coverage.

In short: the 13 adapters reproduce their published accuracy; all 23 functions are calibrated at 0.95 precision;
`emotion` keeps only 7.8% coverage at that target (its raw accuracy is the weakest of the 13, so most of its
decisions abstain once calibrated this strictly).

