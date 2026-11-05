# The engine socket protocol

This describes how a client (an SDK, the server, a CLI) talks to the Jeff engine process over a Unix domain
socket. The schema that every message is checked against lives at `contract/schemas/socket.schema.json`.

## Framing

Every message, in both directions, is sent as one frame:

1. A 4-byte big-endian unsigned integer: the number of bytes in the message that follows (not counting these
   4 bytes).
2. That many bytes, holding the message packed with MessagePack (msgpack).

A frame's packed body may not be larger than 64 MiB (`64 * 1024 * 1024` bytes). Either side that is asked to
send or receives a frame bigger than that refuses it as an invalid request, rather than trying to buffer it.

Frames are read strictly in order on a single connection (each side reads the 4-byte length, then reads
exactly that many more bytes), but a connection may be used for several requests at once — see
"Multiplexing" below.

## The hello exchange

As soon as a connection is open, each side sends a `hello` message:

```json
{"op": "hello", "version": 2}
```

The engine's `hello` also carries the version of the set of adapters it currently has loaded:

```json
{"op": "hello", "version": 2, "adapters_version": 3}
```

Both sides check the other's `version` against the protocol version they themselves speak. If the versions
differ, the side that noticed raises a protocol-mismatch error naming both versions (its own and the one it
was sent) and closes the connection — there is no negotiation or fallback to an older version. This hello
exchange is the only place a message's `id` is not used; a failed handshake is reported with reply id `0`,
before any real request has been given an id.

Protocol versions: 1 was the first; 2 added the base served as adapter `"base"`, `input_tokens` in `predict`
results and `base` in `health` results. A version-1 side and a version-2 side refuse each other at hello, so a
deployment with mixed engines fails at once instead of answering differently.

## Operations

After hello, the client sends request messages and the engine sends back one reply per request. Every
request a client sends has an `op` field naming the operation and an `id` field: a positive integer the
client picks itself, unique among the requests it has outstanding on this connection.

Requests the client can send:

- `installed` — `{"op": "installed", "id": <id>}`: list the adapters the engine currently has loaded.
- `predict` — `{"op": "predict", "id": <id>, "adapter": "<adapter id>", "rows": [...], "deadline_ms": <number>}`:
  run between 1 and 4096 input rows through one named adapter. `deadline_ms` is optional; when given, it
  must be a positive number, and the engine should not spend longer than that satisfying the request.
- `serve` — `{"op": "serve", "id": <id>, "adapters": ["<adapter id>", ...]}`: tell the engine which adapters
  it should have loaded, replacing the current set.
- `reload` — `{"op": "reload", "id": <id>}`: look for adapter folders again and re-apply the adapter list in the
  engine's served file (see "The served file" below). In order:
  1. the engine reads the served file, then scans its adapters folders again (`jeff runtime serve
     --adapters-dir`; see "Adapter folders" below). Any problem — the served file missing or in another format, a
     folder whose manifest is invalid or that cannot be read, two folders with the same adapter ID or alias, a
     folder trained on another base, a found ID also given with `--path`, or a loaded adapter whose folder has
     gone — refuses the `reload`
     (`EngineError` for the served file, `ManifestError` for the folders, naming the adapter or folder) and
     nothing changes;
  2. the adapters a `serve` may name become the ones given on the command line plus every folder just found (a
     found folder replacing a catalogue download of the same adapter);
  3. every loaded adapter that is still in the list and whose files changed since it was loaded (the size or
     modification time of a weight file or its manifest), or whose folder changed (a catalogue download now found
     in a folder), is unloaded and loaded again from its folder;
  4. the list is applied exactly as a `serve` with that list would: adapters in it that are not loaded are
     loaded, and loaded adapters not in it are unloaded.

  `adapters_version` goes up when step 3 loaded anything again, even if the list of loaded adapters is the
  same. An engine started without a served file answers `reload` with an `InvalidRequest`.
- `stats` — `{"op": "stats", "id": <id>}`: ask the engine for its running counters (requests served, timing,
  queue depth, and so on).
- `health` — `{"op": "health", "id": <id>}`: ask whether the engine is up and able to serve requests.

The base model itself is served with no adapter under the adapter id `"base"`: an engine lists it in every
adapter list it sends, a `predict` may name it, a `serve` may list it but never adds or removes it (a `serve`
without it leaves it served), and the served file never lists it. No adapter can have that id (adapter IDs have
three dotted parts).

## Replies

Every request gets exactly one reply, carrying the same `id` the request had, and every reply carries the
engine's current `adapters_version` (a number that goes up whenever the set of loaded adapters changes), so a
client can tell when its list of installed adapters is out of date and ask for it again with `installed`:

- On success: `{"id": <id>, "ok": true, "result": {...}}`, with `adapters_version` inside `result`.
- On failure: `{"id": <id>, "ok": false, "error": {"type": "<error class name>", "message": "<text>", "details": {...}}}`,
  with `adapters_version` inside `details`. `type` is the name of a Jeff error class (for example `Busy`,
  `InvalidRequest`, `DeadlineExceeded`); the receiving side takes `adapters_version` out of `details` and
  reconstructs that same error class with the same message and the remaining details as attributes, so every
  SDK talking to the engine raises the same error for the same failure.

Errors the engine answers a `predict` with include `NotInstalled` (the adapter is not loaded in this engine),
`InvalidRequest` (a malformed row, or more rows than the engine's whole queue holds, which could never be
accepted), `InputTooLong`, `DeadlineExceeded`, and `Busy` (the queue is full right now). A `Busy` error's
details carry `retry_after_s`: the engine's estimate, in seconds, of when there will be room (from how many rows
are queued and how long recent batches took; 1 second when no batch has finished yet).

A message that is valid msgpack but does not match the schema is answered by its `id` when it has one (an
integer of at least 1): that request fails with `InvalidRequest` naming the first problem and the `op`, and the
connection stays open. A message without a usable `id`, a body that is not msgpack, or a broken frame is
answered with a failure carrying id `0`, and the engine closes the connection.

Result shapes:

- `predict` → `{"probabilities": [[<float>, ...], ...], "timing": {...}, "input_tokens": [<int>, ...],
  "adapters_version": <int>}`: one row of probabilities per input row, plus timing information and the adapters
  version the batch actually ran with. `input_tokens` is each input row's prompt length in tokens, in row order; an
  engine that does not count tokens leaves the field out (the server's `/v1/systemone`, which reports their sum, then
  refuses the request). The timing (see "How requests are batched" below; for a request split over several
  micro-batches, numbers are those of its last one, and lists cover all its rows):
  - `queue_ms`: the time from the request's arrival until the micro-batch holding its first rows was formed (waiting
    only, no model time);
  - `batch_rows`: the rows of the micro-batch it ran in, every adapter's together;
  - `model_ms`: the wall time of the model work its rows were part of;
  - `path`: `"fast"` (the NVIDIA fast path), `"standard"` (PyTorch; with `reason`, why the fast path does not take
    the request: `"images"`, or more new tokens than the largest bucket), `"mlx"`, or the test engine's `"fake"`.
  - `format`: the weight format of the base the engine serves, `"bf16"` (bfloat16) or `"int4-g64"` (4-bit integers
    with one bfloat16 scale per 64 inputs, made from a bfloat16 source base); for `"int4-g64"` also `format_id`, the
    name of that exact INT4 base, for example `"jeff-base@v1.3+int4-g64-gptq.3f9a1c2e"` (the source base's id and
    version, the format, the quantisation method, and the first 8 hex digits of the sha256 of its `int4.safetensors`).
    One engine serves one format, decided by its base folder.
  - On the fast path, for all of the micro-batch's fast rows together (every request in the micro-batch gets the
    same values; they count other requests' rows too): `prefix_misses` (new prompt prefixes processed),
    `prefix_hits` (rows whose prefix was cached), `prefix_shared` (rows whose new prefix an earlier row of the
    micro-batch brought; the three add up to the micro-batch's fast rows), `prefix_ms`, `prefix_passes` and
    `passes`; and per row of the request, as lists in row order: `new_tokens` (the row's tokens after its cached
    prefix, before padding to its bucket), `pass_rows` (the rows in the row's pass), `bucket` (its pass's new-token
    bucket) and `adapters_in_pass` (how many adapters that pass mixed).
- `installed`, `serve`, `reload` → `{"adapters": ["<adapter id>", ...], "adapters_version": <int>}`.
- `stats` → `{"queued_rows", "batches", "rows", "busy", "deadline_exceeded", "model_ms_p50", "model_ms_p99",
  "mean_batch_rows", "adapters_version"}`; `queued_rows` counts only rows still waiting, not rows in the batch
  running now; `batches` counts micro-batches (engine calls), and `model_ms_p50`/`model_ms_p99` are over the wall
  time of the last 1,000 of them.
- `health` → `{"status": "ready" | "broken", "backend", "adapters", "adapters_version", "base": {"id", "version",
  "checkpoint", "max_options", "format"}}`, with `"format_id"` in `base` too for an INT4 base (`base`: the base's id
  and version, as adapter manifests name it, where the base model was loaded from, the most options a choice question
  may have on it, and its weight format and INT4 name as in the `predict` timing above), plus `"cause"` (the error, as
  text) when the status is `"broken"`: the engine's model thread has failed and nothing more will run. `jeff runtime
  serve` then closes the socket and exits with status 1, so a supervisor can restart it.

These result shapes are not fixed by the schema beyond being an object.

### The probabilities of a predict

Each row of a `predict` result is a probability distribution over that row's options, in the order the question
lists them: for a choice question one probability per criteria key, for a score question one per level (lowest
first), and for a yes/no (`noul`) question two, the probability of false then the probability of true. The engine
computes every row the same way: the adapter's readout logits in float32, the options past the row's count masked,
divided by the adapter's temperature, a softmax, and the row cut to its own option count (the masked options are
exactly zero). A row is therefore valid only when:

1. it has exactly as many probabilities as the question has options;
2. every probability is a finite number from 0 to 1 inclusive (no NaN, no infinity, nothing negative, nothing above
   1);
3. their sum (summed in float64, as Python's `sum` does) is within 0.0001 of 1. A float32 softmax over at most 255
   options is off by at most about 255 × 2⁻²⁴ (about 1.5e-5); the engine's own tests hold it to 0.0001.

Every SDK refuses a row that breaks any of these with `EngineError` (the engine broke its contract), naming the
function, the values the engine returned and what was expected; it never rescales, clips or otherwise repairs one.
The first rule broken is the one named: the count, then the values (listing the ones outside 0 to 1), then the
sum. A valid row is divided by its sum before the decision is made, so its probabilities sum to 1 exactly as far
as float64 allows. contract/conformance/decisions.json holds a case for each refusal; because JSON has no NaN or
infinities, a probability there that is not finite is written as the string `"NaN"`, `"Infinity"` or
`"-Infinity"`.

### How the engine writes a row's numbers for the model

The engine turns each row into the text the model reads: the state (and any criteria description that is not plain
text) is written as JSON. A number means its value, never the way it was written: `189`, `189.0`, `189.00` and
`1.89e2` are the same number, and JavaScript, for one, cannot tell them apart. Whatever the transport (a MessagePack
integer or float on this socket, a JSON number written either way over HTTP or the channel), the engine therefore
writes every number by its value alone:

1. a number with no fractional part is written as a whole number, in plain digits with no decimal point or exponent:
   `189.0` is written `189`, `-3.0` is written `-3`, `-0.0` is written `0` and `1e21` is written
   `1000000000000000000000`;
2. any other finite number is written as Python's `json.dumps` writes that float: the shortest text that reads back
   as the same value (`189.5`, `0.1`, `1.5e-07`);
3. `true` and `false` stay `true` and `false`, and text is never changed: a string holding `"189.00"` is written as
   that string.

So the same call gives the same model text, and the same answer, from every SDK. The whole-number form is the one
training used for counts and other integer values, which are most of the whole numbers in the training data; the
training rows also hold a few amounts written as floats with `.0` (for example `46840.0`), which the engine now
writes without it. contract/conformance/rendering.json holds the cases: each lists rows, as the JSON text a client
sent, that must all give exactly the model text the case gives.

## How requests are batched

The engine prepares each `predict` as it arrives, on one of `jeff runtime serve --prepare-workers` threads, each with
its own tokenizer: it checks the rows (an error here fails that request alone, with the typed error) and, on the
fast path, splits and tokenises each row with its adapter's own prompt layout, so the thread that runs the model never
tokenises a row. Prepared requests wait in one queue. Requests that arrive together may be queued in a different
order than they arrived, since several are prepared at once.

The model thread then forms a micro-batch: it waits up to `--batch-window-ms` after the oldest queued request for
`--max-batch-rows` rows to gather, drops the requests whose deadline has passed (`DeadlineExceeded`), and takes at
most `--max-batch-rows` rows, every adapter's together. A request larger than the room left is split: its first rows
go into this micro-batch and the rest wait for later ones, and its answer is sent once all its rows have run. Room is
shared out in this order:

1. Requests already part-way through (some of their rows have run) share half of the micro-batch, `--max-batch-rows`
   divided by 2 rounded down and at least 1 row, in arrival order. So a large request keeps getting at least that
   share of every micro-batch until it is done: a steady stream of new requests, on its adapter or any other, cannot
   starve it.
2. Requests not started yet then fill the room left, in arrival order.
3. Room still left goes to the part-way requests again.

The micro-batch runs as one engine call. On the fast path every row runs together whatever its adapter: each row's
prompt prefix is made sure of in the prefix cache first (new prefixes processed in batched passes), then the rows are
grouped by new-token bucket (32, 64, 128, 256, 512 or 1,024 tokens, the smallest that holds the row) and each group is
run in passes of up to 64 rows (16 at 1,024 tokens, 32 at 512); each request's answers come back in its own row order.
Rows that run on the standard path (images, or more than 1,024 new tokens) and every row on MLX run as one group per
adapter, one group after another. A request whose adapter was removed, or loaded again (a `serve` or `reload`),
after it arrived gets `NotInstalled` alone and does not run: it was prepared for the adapter as it was (its prompt
layout and option limit), so it is sent again rather than answered by the new weights. A
typed error of one standard or MLX group fails that group's requests; one of the fast path fails every fast-path
request of the micro-batch; any other failure fails every request of the micro-batch.

## The served file

An engine started with a served file (`jeff runtime serve --served-file <path>`) writes the adapter list there
after every fully successful `serve` (or `reload`), and `reload` reads it back. The file is exactly:

```json
{"format": "jeff-engine-served", "version": 1, "adapters": ["<adapter id>", ...]}
```

A file at that path in any other format (for example jeff-server's own `served.json`) is refused with an
`EngineError` naming the file — at startup, on `serve` before anything changes, and on `reload` — so it is never
read or overwritten as the engine's. `reload` with no file there is an `EngineError`.

The served file is where the engine starts from. When it exists at startup, the engine loads the adapters it
lists, and `--adapter` is refused ("two sources for the starting adapters"). When it does not exist, the engine
loads the `--adapter` ones and writes the file with them once they are loaded. The base is the one the starting
adapters are trained on; an empty list (no file and no `--adapter`, or a file listing nothing) needs
`--base-version <base id>@<version>` (for example `jeff-base@v1.3`), and the engine then serves only the base.
`--base-version` given with adapters must match their base.

## Adapter folders

`jeff runtime serve --adapters-dir <folder>` (repeatable) adds every adapter folder under that folder, at any
depth, to the adapters a `serve` may name. A folder is an adapter when it holds both `manifest.yaml` and
`adapter_config.json`; the manifest's `id` is the adapter's ID, and its weights are the files in that folder (a
manifest written for such a folder may say `weights: {local: {}}`). Folders whose names start with `.` are
skipped, with everything under them, so an adapter copied in under a dot name and then renamed into place is never
seen half-copied. Symlinked folders are followed. A folder that does not exist or cannot be read, a folder reached
twice (a symlink loop, or two links or roots to one folder), an invalid manifest, two folders with the same ID or
sharing an alias, an ID also given with `--path`, or an adapter trained on a base other than the loaded one stops
the engine at startup with a `ManifestError` naming it; on `reload` the same problems refuse the `reload`. At
startup a found adapter's alias that is the catalogue's name for another adapter is refused too. An adapter in the
catalogue that is also found in a folder is loaded from the folder (the engine's own copy), at startup or by the
first `reload` after the folder appears. Folders are looked at only at startup
and on `reload`, never while answering a `predict`.

## An INT4 base and its gate

An engine started with `jeff runtime serve --base <INT4 base folder>` serves that INT4 base only for adapters its
gate passed. The folder's `gate.json` (written by `jeff runtime gate`) must be the gate of this exact base (its
`format_id` and the sha256 of its `int4.safetensors`), must have passed as a whole, and must hold a passing entry for
each adapter served at that adapter's version (`<adapter id>@<version>`: a found folder's from its own manifest, any
other's from the catalogue). This is checked at startup (the base alone included) and on every `serve` and `reload`,
after the checks above and before anything changes, reading `gate.json` again each time; a refusal is an
`EngineError` naming each adapter, why, and the `jeff runtime gate` command to run, and nothing changes. An engine
serving a bfloat16 base has no such check.

## Multiplexing

A client may have more than one request outstanding on the same connection at once (for example, several
`predict` calls queued while earlier ones are still running). The engine is not required to answer requests
in the order they were sent — a reply for a later request may arrive before the reply for an earlier one.
Clients must match each reply to its request by `id`, not by arrival order, and must not reuse an `id` that
is still outstanding on that connection.

## The 64 MiB frame limit

`MAX_FRAME = 64 * 1024 * 1024` bytes is a hard ceiling on the packed size of a single frame, in either
direction. It exists so that a corrupt or malicious length header can never make either side allocate an
unbounded buffer. A message that would pack larger than this (for example, a `predict` request with very
large rows) is refused as an invalid request before it is sent; a frame header that announces a length over
this limit is refused the same way as soon as it is read, before any attempt is made to read the body.

## The channel

jeff-server's low-latency channel is a WebSocket at `/v1/channel` that speaks this protocol to remote clients, with
the operations below added. It is for services that call Jeff at high rates over the network: the client
authenticates once, binds a function once, and then sends only each call's input.

### Messages

Each WebSocket **binary** message carries exactly one frame in the format above (the 4-byte big-endian length,
then the msgpack body), and the length must equal the rest of the message. A frame's body may not be larger than
50 MiB (`50 * 1024 * 1024` bytes, the HTTP API's body limit); a WebSocket message longer than that frame is refused
by the WebSocket server itself, which closes the connection with close code 1009 (message too big) before reading
it. Until the connection is authenticated (on a server with credentials), a frame's body may be at most 64 KiB
(`64 * 1024` bytes; a `hello` or an `auth` is far smaller), decided from the frame's 4-byte length before anything
is unpacked. A text message, a message that is not exactly one frame, a header announcing a body over the limit, a
body that is not msgpack, a body holding a value JSON cannot hold (a binary string or a msgpack extension type,
timestamps included, or a map key that is not a string), a message without a usable `id`, or a request reusing the
`id` of a request still outstanding is answered with a failure carrying id `0` (`InvalidRequest`), and the server
closes the connection. A message that does not match its operation's shape but has a usable `id` fails only that
request (`InvalidRequest` naming the first problem), as on the engine socket. Whenever the server closes the connection after a failure carrying id `0`, it uses WebSocket
close code 1008 (policy violation) for `Unauthorised` and 1002 (protocol error) for anything else.

### Opening, authenticating, refreshing

1. The client sends `hello` (`{"op": "hello", "version": 2}`) and the server answers with its own `hello`
   (`{"op": "hello", "version": 2}`, without `adapters_version`). A different version is refused with a
   `ProtocolMismatch` failure carrying id `0`, and the connection is closed.
2. On a server with credentials configured (every public server), the client then sends
   `{"op": "auth", "id": <id>, "token": "<token>"}` with any credential `Authorization: Bearer` accepts on the HTTP
   API (the API key, a console session token, or a hub-signed token); the reply is `{}`. Until an `auth` succeeds,
   every other request fails with `Unauthorised`, without its shape being checked (a failed `auth` too, and the
   client may try again). If no `auth`
   has succeeded within 10 seconds of the connection opening, the server sends an `Unauthorised` failure carrying
   id `0` and closes the connection. On a server without credentials, `auth` is optional (any token is accepted, as
   every caller is the owner). On every server, a connection that sends no `hello` within those 10 seconds gets an
   `InvalidRequest` failure carrying id `0` and is closed.
   A second `auth` on one connection is refused with `InvalidRequest`; use `refresh`.
3. `{"op": "refresh", "id": <id>, "token": "<token>"}` replaces the connection's token with a new one for the same
   caller (the same kind of credential and the same subject, for a hub token its `sub`); the reply is `{}`. The new
   token's licences apply to every later request, including calls on handles bound before. A token for another
   caller fails with `Unauthorised`, after which the server sends an `Unauthorised` failure carrying id `0` and
   closes the connection. A token that is not valid fails with `Unauthorised` and the connection keeps the token
   it had. A request already running when a `refresh` arrives keeps the licences it arrived with. Clients refresh
   well before the token's `exp` (the Python SDK does so 60 seconds before): a `refresh` the server has not read
   when `exp` passes is never read (see 4).
4. When the connection's token expires (a hub token's `exp`, a console session's end; the API key does not
   expire), the server stops reading requests, lets every request it is already working on finish and send its
   reply, then sends `Unauthorised("the channel's token expired")` with id `0` and closes the connection. A client
   that sent requests the server had not read by then gets no reply for them and should fail them with that error.
5. A console session token is checked again for every `bind` and call request (`call`, `call_many`, `decide`,
   `dispatch`, `graph`), exactly as every HTTP request checks it: once the session has ended on the server (its
   login removed from the users file, the file given a new secret with `sign-out-all`, or the session past its
   end), the request is not answered; the server stops reading, lets the requests it is already working on finish
   and send their replies, then sends an `Unauthorised` failure carrying id `0` and closes the connection. An API
   key and a hub-signed token are not checked again (neither can be revoked before its `exp`).

### Operations

Every request after `hello` has an `id` (unique among the client's outstanding requests on this connection) and
gets one reply, `{"id", "ok": true, "result"}` or `{"id", "ok": false, "error"}`, exactly as on the engine socket.
Replies may arrive in any order. Errors are the same typed errors (`contract/errors.yaml`) the HTTP API answers
with, carried in the error's `type`, `message` and `details` (there is no HTTP status); a `RateLimited` or `Busy`
error carries `retry_after_s` in its details. A channel reply has no request id (there is no HTTP response); instead
each connection has an id of its own (8 hex characters), which every failure the server sends on it (the failure
carrying id `0` included) carries in its details as `channel_id`, and which the server names in its log lines and
the connection's access-log entry. The engine socket's own operations (`installed`, `predict`, `serve`,
`reload`, `stats`, `health`) are refused on the channel with `InvalidRequest`, and the engine socket refuses the
channel's operations the same way.

- `bind` — `{"op": "bind", "id", "function": "<function reference>", "context"?, "labels"?, "on_low_confidence"?}`
  → `{"handle": <int>, "function": "<the function's full ID>", "version": "<its adapter's version>"}`. `context`,
  `labels` and `on_low_confidence` take the forms of a `/v1/decide` call and policy (`on_low_confidence`: `"return"`
  (the default), `"error"`, or `{"escalate": [<function references>], "budget_ms"?}`; LLM, human and remote steps
  run in the SDK and are refused here, as on `/v1/decide`). The function, the caller's licence for it, whether its
  adapter is served, the context, the labels and the policy are all checked here, with the errors `/v1/decide`
  gives for the same call. Handles are numbered from 1 on each connection and are never reused on it.
- `call` — `{"op": "call", "id", "handle", "input"}` → `{"decision": <Decision>}`: exactly what `/v1/decide`
  answers for `{"calls": [<the bound function, context and labels, with this input>], "on_low_confidence": <the
  bound policy>}`, errors included (an abstained decision under `"error"` is a `LowConfidence` error carrying the
  decision).
- `call_many` — `{"op": "call_many", "id", "handle", "inputs": [...]}` (1 to 256 inputs) → `{"decisions": [...]}`,
  in input order.
- `release` — `{"op": "release", "id", "handle"}` → `{}`. Releasing ends the handle; a request already sent on it
  still gets its answer. A handle the connection does not hold (never bound, or released) fails with
  `InvalidRequest` naming it, for `call`, `call_many` and `release` alike. Handles end with the connection.
- `decide` — `{"op": "decide", "id", "calls": [...], "on_low_confidence"?}` → `{"decisions": [...]}`: the
  `/v1/decide` calls shape (1 to 256 calls), for calls without a handle.
- `dispatch` — `{"op": "dispatch", "id", "dispatch": {...}, "on_low_confidence"?}` → `{"decision": <Decision>}`: the
  `/v1/decide` dispatch shape.
- `graph` — `{"op": "graph", "id", "graph": {"id"} | {"inline"}, "input", "params", "on_low_confidence_by_node"?}`
  → `{"graph_result": <GraphResult>}`: the `/v1/decide` graph shape.

Every request on a handle, and every `decide`, `dispatch` and `graph`, checks the caller's licences as they are at
that moment (a `refresh` may have removed one, giving `NotLicensed`) and whether the function's adapter is served
then (a `PUT /v1/adapters/served` from anyone may have removed it, giving `NotInstalled`); the connection stays open
for the other handles. Every call on one handle builds the same prompt prefix, so the server's engine router sends
them all to the same engine process, which keeps that prefix cached.

### Limits

- At most 1,024 live handles per connection: one more `bind` fails with `TooManyHandles`.
- At most 256 outstanding requests per connection (sent and not yet answered): one more fails at once with
  `RateLimited`, `retry_after_s` 1.
- Each `bind`, `call`, `call_many`, `decide`, `dispatch` and `graph` request counts against the caller's rate limit
  (`JEFF_RATE_LIMIT`) exactly as one HTTP request does: it takes a slot before it runs (or fails with `RateLimited`
  and its `retry_after_s`), and gives it back when its reply is sent; if the connection closes first, when the engine
  call it started returns, as on the HTTP API (at once if it had not started one yet).
- `bind` counts as one request because its checks ask the engine which adapters are served, as a `/v1/decide`
  call does; `auth`, `refresh` and `release` do not count against the rate limit. Every request counts as
  outstanding until it is answered.
- At most 256 inputs per `call_many` and 256 calls per `decide`, as on `/v1/decide`; a frame's body at most 50 MiB.
- Memory: the server stops reading from a connection while one whole message waits to be read by the channel, so
  one connection holds at most the frame being read, one waiting frame and one arriving, each at most one frame of
  50 MiB (`jeff-server serve` runs uvicorn with `ws_max_size` 4 bytes + 50 MiB, `ws_max_queue` 1, and its
  websockets-sansio implementation). WebSocket compression (permessage-deflate) is not offered: a compressed
  message can be a thousand times smaller on the wire than in memory, so one read from the network could hold
  several whole messages; without it, every byte the server holds arrived over the network. Channel frames are
  msgpack, which compresses little anyway. The WebSocket server's message limit is one setting for the whole server, so
  the 64 KiB limit before auth is the channel's own check of each frame's length, made before the frame is
  unpacked.

A connection that closes (or drops) ends its handles at once. Each request it had outstanding is dropped: one that
had not started its engine call gives back its rate-limit slot at once; one whose engine call is running keeps its
slot until that call returns, as on the HTTP API, and its answer is discarded. When the server shuts down, it waits
for the requests still running on every connection (at most the engine timeout, `JEFF_ENGINE_TIMEOUT_S`) before it
closes its engine connections, then cancels any left and logs how many there were.

### Access log and metrics

Each connection writes one access-log entry (`JEFF_ACCESS_LOG`, path `/v1/channel`): when it is authenticated
(status 200, with the caller's kind and subject; at once on a server without credentials), or, when it ends without
having been authenticated, at its end (status 401). The entry's detail is `WEBSOCKET <caller kind>` (`WEBSOCKET`
alone when not authenticated), then `channel <the connection's id>`, then the number of failed `auth` attempts when
there were any (for example `WEBSOCKET hub, channel 1f2e3d4c, 1 failed auth`). `/metrics`
counts each connection once it has ended in `jeff_http_requests_total` under `route="/v1/channel"`, with its
WebSocket close code as the status: the client's own close code when it closed the connection, 1008 or 1002 when
the server closed it after a failure carrying id `0`, 1001 when the server was shutting down.
