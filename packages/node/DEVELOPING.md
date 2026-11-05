# jeff-sdk: Jeff for Node

Call Jeff's decision functions from Node 22 or later: bind a function, call it, read a typed `Decision`, route a call
to the right function, escalate when Jeff is unsure, and run decision graphs. These are the development notes; the
short README.md is what npm shows. Install it with `npm install jeff-sdk`.

## Two ways to run

**Local** (the fastest): the SDK talks straight to a Jeff engine running on the same machine, over the engine's Unix
socket. There is no server in between. The SDK never starts the engine; start it yourself (the model runs only in the
Python engine) and keep it running with your process manager:

```bash
jeff runtime serve --socket /tmp/jeff.sock --adapter jeff.support.triage \
  --batch-window-ms 2 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2
```

`--socket`, `--batch-window-ms`, `--max-batch-rows`, `--queue-limit-rows` and `--prepare-workers` are always
required. On an NVIDIA GPU the engine also requires `--max-adapters` and `--prefix-pool-mb` (and refuses to start
without them); on other backends (Apple Silicon, CPU) those two options are refused.

```ts
import { Jeff } from "jeff-sdk";

const jeff = await Jeff.local("/tmp/jeff.sock", { timeoutMs: 30000 });
const urgency = await jeff.function("triage.urgency", { context: { company: "A furniture shop.", channel: "email" } });
const decision = await urgency.call("My wardrobe arrived broken");
console.log(decision.value, decision.confidence, decision.abstained);
await jeff.close();
```

If nothing listens on the socket, `Jeff.local` fails with an error that names the socket and this command.

**Remote**: the SDK calls a Jeff server (ours or your own) over HTTP, or over the server's WebSocket channel for lower
latency:

```ts
const jeff = await Jeff.remote("https://api.example.com", process.env.JEFF_TOKEN!, { timeoutMs: 30000 });
const fast = await Jeff.remote("https://api.example.com", () => currentToken(), { timeoutMs: 30000, channel: true });
```

The token is a string, a function returning the current token, or `null` only for a server with no credentials.

`timeoutMs` bounds each phase of a request separately, not the request as a whole: connecting, waiting for the
answer's headers (this phase also covers the time spent sending the request body, so a slow upload and a slow
server both count against it), and each wait for more of the answer's body. A slow connect followed by a fast
answer never adds up to a timeout; a server that is slow in any one phase does. With `channel: true`, the same
`timeoutMs` instead bounds opening the channel, its hello, and the closing handshake, and, for each request, the wait
for its reply, which starts only once its whole message has been sent: a large message on a slow but working link
takes as long as it needs to send. Sending is bounded by stalls instead: if the socket accepts no piece of a message
for `timeoutMs` (the server or the network stopped taking data), the channel ends with `BackendUnavailable` saying so,
and every request waiting on it fails with that error.

Proxies are taken from the environment as the Python SDK takes them: `HTTPS_PROXY`, `HTTP_PROXY` (and `ALL_PROXY` for
HTTP requests) and `NO_PROXY`, lower-case forms first, with the same choice of proxy and the same `NO_PROXY` matching;
`https://` requests and the channel's WebSocket go through a CONNECT tunnel, `http://` requests are forwarded. SOCKS
proxies are refused, and the operating system's own proxy settings are not read.

## What you can do

- `jeff.decide([{ function, input, context, labels }])`: many calls at once, one engine request per adapter.
- `jeff.function(ref, { context, labels, onLowConfidence })`: bind once, then `call(input)` or `many(inputs)`.
- `jeff.classify`, `route`, `rate`, `detect`, `isSupported`, `equivalent`, `requiresEscalation`: call through an
  interface, with `function` to name the function, or without it to let the routers choose (`jeff.dispatch`).
- `onLowConfidence`: `"return"` the unsure decision (the default), `"error"` to raise `LowConfidence`, or
  `escalate([functionStep("triage.sentiment"), llm(askAnLlm), human(askAPerson)], { budgetMs })`. An `llm` or `human`
  step is your own function: it gets a `Question` and returns one of the allowed answers.

  Build any prompt you send to an LLM or show to a person from the question's `stateEntries` and `optionEntries`,
  not from its plain `state` and `options` objects: those are ordinary JavaScript objects, and JavaScript always
  lists a key that looks like a whole number (`"2"`, `"10"`) before every other key, in numeric order, no matter
  where it sat in what the engine sent. `stateEntries` and `optionEntries` are arrays of `[key, value]` pairs that
  keep the original order, so a prompt built from them reads the same options in the same order a person or model
  was shown in Python.

  ```ts
  async function askAnLlm(question: Question): Promise<string> {
    const stateText = (question.stateEntries ?? []).map(([key, value]) => `${key}: ${JSON.stringify(value)}`).join("\n");
    const optionsText = question.optionEntries.map(([key, description]) => `${key}: ${description ?? ""}`).join("\n");
    const answer = await callYourLlm(`${question.instructions}\n\n${stateText}\n\nOptions:\n${optionsText}`);
    return answer; // must be one of question.optionEntries' keys
  }
  ```
- `jeff.runGraph(graph, input, params, code, policies)`: a graph file, document or `Graph`; code nodes are your own
  functions.

Every error is a `JeffError` subclass named as in `contract/errors.yaml`, with the same message and `details` the
Python SDK gives for the same failure.

## Behaviour

The SDK follows the Python SDK exactly: `contract/conformance/` holds what the Python SDK does (rows sent to the
engine, decisions, socket conversations, server exchanges), and this package's tests replay all of it.

`Jeff.local` does no adapter version check on connect: the engine's `installed` reply carries adapter ids only, no
versions (the Python SDK's `Jeff.connect` does none either), unlike `Jeff.remote`, which refuses a server serving a
different version of a catalogue manifest.

Two limits follow from JavaScript's own object key order, both only when a label or node name looks like a whole
number (`"2"`, `"10"`), never for ordinary names:

- `Decision.probabilities` is a plain object keyed by option label. When the labels include a number-like one,
  iterating the object (`Object.keys`, `for...in`, `JSON.stringify`) lists that label before the others, in numeric
  order, which is not necessarily the option order Python reports. The values are unaffected; only the key order of
  this one object is.
- `graphToDict` (writing a `Graph` back out as a document) refuses when the graph has a node whose name looks like
  a number and reordering it to the front, as a plain object would, would change the graph: it raises `GraphError`
  naming the node. Such a graph can still be read and run (`loadGraph`, which reads a YAML document into a `Map`
  that keeps real order), just not written back by this SDK. `RemoteJeff.runGraph` sends such a graph inline over
  HTTP by calling `graphToDict` on it (unless the graph has a code node or an escalation step the server cannot
  run, when it runs node by node here instead), so a graph with a number-like node name that runs fine locally
  (`Jeff.local`) is refused with the same `GraphError` when run through `Jeff.remote`.

Limits that turn a broken or hostile server or file into a named error instead of exhausting the memory:

- **An HTTP answer's body**: at most 64 MiB (67,108,864 bytes), both as sent (whatever its framing: a body with no
  `Content-Length`, or an endless one, is given up at the limit) and once each `gzip` or `deflate` layer of its
  `Content-Encoding` is undone, so a few kilobytes of stacked gzip cannot decode to gigabytes. Past it the request
  fails with `BackendUnavailable` naming the limit, and the client is closed. As httpx, requests ask for
  `gzip, deflate` and the SDK undoes those two itself (deflate zlib-wrapped or raw), with Node's asynchronous zlib,
  so the event loop keeps running while any number of layers is decoded; `br`, `zstd`,
  `x-gzip` and unknown codings are left as they are. A body that is not its coding is an `EngineError` ("... could
  not be read: DecodingError: Error -3 while decompressing data: ..."). httpx sets no limit; a Jeff answer is far
  smaller (a `/v1/decide` answer of 1,000 calls is a few MiB).
- **YAML aliases** (manifests and graph files): the values a document's aliases add once each is copied out are at
  most 1,000,000; past that the file is refused with `ManifestError` ("... the aliases expand to more than 1,000,000
  values; write the repeated parts out or use fewer aliases"). The value is built in one walk of the parsed document
  (the yaml package's own `toJS`, which takes time growing with the square of the number of aliases, is not used).
  Refused by line too, as PyYAML and the other SDKs refuse them: a mapping key that is not a string, and an alias
  naming a collection it is inside. Scalars are read as PyYAML's safe_load reads them: `y` and `n` are strings and
  `yes`/`no`/`on`/`off` booleans; `08`, `019`, `1e5` and `1.0e5` are strings (`010` is octal 8, `1.0e+5` a float);
  `.inf` and `.nan` are `Infinity` and `NaN`; an integer past 2**53 is a `bigint`, exact, and written exactly in a
  request to a Jeff server, but a manifest or graph holding one is refused (`ManifestError`, `GraphError`) naming the
  field; a manifest number that is not finite (`.nan`, `.inf`) is refused before its schema check with
  `ManifestError` naming its place and value, as every SDK words it (contract/conformance/manifests.json); a merge key's pairs come before the mapping's own. A tag PyYAML's safe loader cannot
  construct (`!foo`, `!!binary`, `!!set`, ...) and a bare `=` as a value are refused with `ManifestError`.
- **The channel's keepalive**: as the Python SDK's websockets client, the channel pings the server every 20 s; if no
  pong comes within 20 s of a ping being sent, the channel ends with `BackendUnavailable` ("... the server sent no
  pong within 20 s of a keepalive ping; the channel closed") and every request waiting on it fails with that error.

## Development

```bash
pnpm --filter jeff-sdk sync        # copy contract/ and catalogue/ into shipped/ (a test fails when it is stale)
pnpm --filter jeff-sdk test        # unit and conformance tests, offline
pnpm --filter jeff-sdk test:slow   # golden cases against a real engine (see test/golden.slow.test.ts)
pnpm --filter jeff-sdk bench -- --help   # latency benchmark (see bench/latency.ts)
pnpm --filter jeff-sdk build       # dist/ with type declarations
```
