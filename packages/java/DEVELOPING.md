# Jeff for Java

Call Jeff's decision functions from Java 21 or later: bind a function, call it, read a typed `Decision`, route a call to
the right function, escalate when Jeff is unsure, and run decision graphs. The package is `ai.jeff.sdk` (and
`ai.jeff.sdk.testing` for `FakeEngine`). It behaves as the Python SDK does: the same results, the same errors, the
same messages.

The development notes for the package; the short README.md is what Maven Central shows. It is published on Maven
Central as `ai.jeffhub:jeff-sdk`. To build it from the repository, run `./gradlew build` in `packages/java`; the jar
is written to `build/libs/jeff-sdk-1.4.0.jar`. It compiles for Java 21 (`--release 21`) and needs these libraries on the class path:
jackson-core 2.22.4 (Jackson's streaming reader and writer only, not databind) and SnakeYAML 2.7.

## Two ways to run

**Local** (the fastest): the SDK talks straight to a Jeff engine running on the same machine, over the engine's Unix
socket. There is no server in between. `Jeff.local(List.of("guard"))` (or with `StartOptions`) finds the engine
running for this user on this machine or starts one through uvx (`jeff-runtime` of the SDK's own version, the version
read from the build-generated resource `ai/jeff/sdk/version.txt`), as contract/local-engine.md specifies; it needs uv
installed once (and, on Linux, `setsid` from util-linux). Java cannot start a process in its own process group, so
the engine is started through the contract's shell script: `/bin/bash` with job control on macOS, `/bin/sh` and
`setsid` on Linux. The socket form below never starts anything: start the engine yourself (the model runs
only in the Python engine) and keep it running with your process manager:

```bash
jeff runtime serve --socket /tmp/jeff.sock --adapter jeff.support.triage \
  --batch-window-ms 2 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2
```

`--socket`, `--batch-window-ms`, `--max-batch-rows`, `--queue-limit-rows` and `--prepare-workers` are always
required. On an NVIDIA GPU the engine also requires `--max-adapters` and `--prefix-pool-mb` (and refuses to start
without them); on other backends (Apple Silicon, CPU) those two options are refused. `--batch-window-ms` is how long
the engine waits after the first queued request for others to join its batch: with one caller nothing joins, so a
short window (`0.1`) waits least; with many concurrent callers a longer one batches more rows per model run.

```java
import ai.jeff.sdk.*;
import java.util.Map;

try (Jeff jeff = Jeff.local("/tmp/jeff.sock", LocalOptions.timeoutMs(30000))) {
    BoundFunction urgency = jeff.function("triage.urgency",
        CallOptions.none().context(Map.of("company", "A furniture shop.", "channel", "email")));
    Decision decision = urgency.call("My wardrobe arrived broken");
    System.out.println(decision.value() + " " + decision.confidence() + " " + decision.abstained());
}
```

If nothing listens on the socket, `Jeff.local` fails with an `EngineError` that names the socket and the command that
starts an engine. `timeoutMs` bounds the engine's hello and each reply. One `Jeff` holds one connection, shared by every
thread that uses it: their requests are multiplexed on it, so the engine can batch them. When the engine closes the
connection, the next call makes one fresh connection (at most one attempt per second); a call that was in flight
fails and is never sent again.

**Remote**: the SDK calls a Jeff server (ours or your own) over HTTP, or over the server's WebSocket channel for lower
latency:

```java
RemoteJeff jeff = Jeff.remote("https://api.example.com", System.getenv("JEFF_TOKEN"), RemoteOptions.timeoutMs(30000));
RemoteJeff fast = Jeff.remote("https://api.example.com", () -> currentToken(),
    RemoteOptions.timeoutMs(30000).withChannel(true));
```

The token is a string, a function returning the current token (called before every HTTP request, and on the channel
when its token is about to expire), or `TokenSource.none()` only for a server with no credentials. No error message,
note or `toString()` ever shows a token. Connecting reads `GET /v1/functions` once and refuses (`ContractError`) a
server that serves another version of an adapter the client's catalogue has; pass your own catalogue with
`RemoteOptions.withCatalogue`.

### Timeouts

Over HTTP, `timeoutMs` bounds each phase of a request on its own, as httpx's `Timeout` does for the Python SDK:
waiting for a free connection, connecting (TCP and TLS; running out is `BackendUnavailable`), waiting for the answer's
headers once the request is being sent, and each wait for more of the answer's body (running out of either is
`DeadlineExceeded`). A slow connect followed by a fast answer never adds up to a timeout. A request is never sent
twice, redirects are never followed, and `close()` ends every request still in flight.

Over the channel, `timeoutMs` bounds opening it (lookup, TCP, a proxy's tunnel, TLS and the upgrade together), its
hello, and the closing handshake. A request's own timeout starts once its message has been sent: it bounds the wait for
its reply (`DeadlineExceeded`, and a late reply is dropped), as in the Python SDK. Before that, a request waits at most
`timeoutMs` for its turn to be written (`DeadlineExceeded`, and it is never sent). Its upload may take as long as a slow
but working link needs; when the socket accepts nothing of the message for `timeoutMs`, that send stall ends the whole
channel with `BackendUnavailable` naming the stall, and every request waiting on it fails.

The channel sends a WebSocket ping every 20 seconds, as the Python SDK's websockets client does; a pong that does not
come within 20 seconds of its ping ends the channel with `BackendUnavailable`. The channel does not reconnect: connect
again with `Jeff.remote`.

### Proxies

Requests to a Jeff server go through the proxy the environment variables name, exactly as the Python SDK chooses it:

- **HTTP** (as httpx with `trust_env`): `HTTPS_PROXY` for `https://` servers, `HTTP_PROXY` for `http://` ones,
  `ALL_PROXY` for both, each also in lower case (the lower-case spelling wins), and `NO_PROXY` for the hosts that
  bypass it (`*` turns every proxy off). An `http://` server is reached through the proxy by forwarding, an
  `https://` one through a `CONNECT` tunnel. A user name and password in the proxy URL become a
  `Proxy-Authorization` header.
- **The WebSocket channel** (as websockets with `proxy=True`): for `wss://` the first of `WSS_PROXY`, `SOCKS_PROXY`,
  `HTTPS_PROXY`; for `ws://` the first of `WS_PROXY`, `SOCKS_PROXY`, `HTTPS_PROXY`, `HTTP_PROXY` (`ALL_PROXY` is not
  read), with `NO_PROXY` as Python's urllib reads it. Both go through a `CONNECT` tunnel. A SOCKS proxy is refused by
  name (the Python SDK does not install SOCKS support either).

The operating system's own proxy settings (macOS or Windows system settings) are not read, unlike the Python SDK,
which reads them when no variable is set: set the variables instead. `GET /v1/functions` always goes over HTTP, with
the HTTP rule.

## Blocking and asynchronous

Every method blocks until it has its answer, as in the Python SDK, and each has an `...Async` twin returning a
`CompletableFuture` (`decideAsync`, `callAsync`, `manyAsync`, `classifyAsync`, `dispatchAsync`, `runGraphAsync`, ...),
which runs the call on a virtual thread. `Async.join(future)` waits for one and throws its `JeffError` as itself. Many
threads may share one `Jeff`.

No thread the SDK starts keeps the JVM alive (they are daemon or virtual threads): an idle connection does not hold
it open.

## What you can do

- `jeff.decide(List.of(new Call(function, input, context, labels)))`: many calls at once, one engine request per
  adapter. No policy is applied; each decision comes back as it is.
- `jeff.function(ref, CallOptions.none().context(...).labels(...).onLowConfidence(...))`: bind once, then
  `call(input)` or `many(inputs)`. An input may be any JSON value, `null` included.
- `jeff.classify`, `route`, `rate`, `detect`, `isSupported`, `equivalent`, `requiresEscalation`: call through an
  interface, naming the function (`jeff.rate(input, "triage.urgency", options)`) or without it to let the routers
  choose (`jeff.rate(input, options)`, or `jeff.dispatch(input, "rate", options)`).
- `onLowConfidence`: `Policy.RETURN` the unsure decision (the default), `Policy.ERROR` to raise `LowConfidence`, or
  `Policy.escalate(List.of(Step.function("triage.sentiment"), Step.llm(this::askAnLlm), Step.human(this::askAPerson)), 5000L)`
  (the last argument, optional, is the whole escalation's budget in milliseconds). An `llm` or `human` step is your own
  function: it gets a `Question` and returns one of its option keys. The question's `state()` and `options()` keep the
  order the model reads them (also as `stateEntries()` and `optionEntries()`):

  ```java
  String askAnLlm(Question question) {
      StringBuilder prompt = new StringBuilder(question.instructions()).append("\n\n");
      if (question.stateEntries() == null) prompt.append(question.state()).append("\n");
      else for (var field : question.stateEntries()) prompt.append(field.getKey()).append(": ").append(field.getValue()).append("\n");
      prompt.append("\nOptions:\n");
      for (var option : question.optionEntries()) prompt.append(option.getKey()).append(": ").append(option.getValue()).append("\n");
      return callYourLlm(prompt.toString()); // must be one of question.options()' keys
  }
  ```
- `jeff.runGraph(graph, input, params, code, policies)`: a `Graph` (`Graphs.load(path)`, `Graphs.fromMap(document,
  where)`), a graph file's `Path`, or a document `Map`; code nodes are your own `CodeFunction`s. A `RemoteJeff` also
  runs a graph registered on the server by id (`runServerGraph`).
- `Jeff.create(engine, catalogue)`: a client over any `Engine`, such as `ai.jeff.sdk.testing.FakeEngine`, whose
  answers come from your own function, for tests of code that uses the SDK with no model.

Every error is a `JeffError` subclass named as in `contract/errors.yaml` (`JeffError.NotInstalled`,
`JeffError.LowConfidence`, ...), unchecked, with the same message and `details()` the Python SDK gives for the same
failure. The Javadoc of each method names the errors it raises.

## Behaviour

The SDK follows the Python SDK exactly: `contract/conformance/` holds what the Python SDK does (rows sent to the
engine, decisions, socket conversations, server exchanges), and this package's tests replay all of it, byte for byte
where bytes are recorded.

- Order matters to the model (it reads options and fields in the order it was trained on), so give caller labels
  and any map inside an input as a `LinkedHashMap` (or another map that keeps an order). A `HashMap` or `Map.of(...)`
  with two or more entries keeps no order and is refused with an error naming its class, never sent in a random order.
  Context may be any map: it is bound in the manifest's field order.
- Whole numbers may be `Integer` or `Long`; every decision and graph result holds `Long`. A `Float` (Python has no
  32-bit float) and a string holding a lone UTF-16 surrogate (it has no UTF-8 form) are refused when the row is packed.
- Nothing here depends on whether a key looks like a number: Java keeps every map's order, so graphs whose node names
  look like numbers are read, run and written back like any other.
- `Jeff.local` does no adapter version check on connect: the engine's `installed` reply carries adapter ids only, no
  versions (the Python SDK's `Jeff.connect` does none either), unlike `Jeff.remote`.

### Nesting and size limits

Values nested deeply (lists and maps inside one another), and inputs that would grow far larger once read, are refused
with an error that names the limit, never with a stack overflow or by filling the memory, whatever the calling
thread's stack size:

- **Any value** the SDK copies, writes as JSON or prints in a message: at most 9,998 levels, as deep as CPython's
  `json` and `repr()` go.
- **A message to the engine socket** is packed with MessagePack, which (as msgpack-python) packs at most 1,025 levels
  and reads at most 1,024. The row and the message around it take a few of those levels, so a deeper input is refused
  with `InvalidRequest` before anything is sent.
- **YAML** (manifests and graph files): at most 512 levels, refused with `ManifestError`.
- **Numbers in a manifest** must be finite: a `.nan`, `.inf` or `-.inf` anywhere is refused with `ManifestError`
  naming its place and value (contract/conformance/manifests.json). A threshold of NaN would turn calibration off
  without a word, since no confidence is below it.
- **YAML aliases**: the values a document's aliases add once each is copied out (an alias stands for the whole value
  it names; mapping keys are not counted, as in the Rust and C# SDKs) are at most 1,000,000, so a few hundred bytes of nested aliases cannot fill the memory; past that the file
  is refused with `ManifestError`. Any number of aliases within that is read, as PyYAML reads them.
- **An HTTP answer's body**: at most 64 MiB (67,108,864 bytes, the same in every Jeff SDK), both as sent and once its
  `gzip` or `deflate` encoding (any number of layers) is undone, so a few KiB of compressed data cannot fill the
  memory. Past that the request fails with `BackendUnavailable` naming the limit and the client is closed (connect
  again with `Jeff.remote`), as the C# SDK does. httpx sets no limit; a Jeff answer is far smaller (a `/v1/decide`
  answer of 1,000 calls is a few MiB).
- **Schema checks** (manifests, graphs, server answers): at most 500 checks inside one another, refused with
  `ContractError` (`GraphError` for a graph); that is about 170 nested graph conditions, where the Python SDK stops
  with a `RecursionError`.

## Development

```bash
cd packages/java
export JAVA_HOME=/opt/homebrew/opt/openjdk@27/libexec/openjdk.jdk/Contents/Home
./gradlew syncShipped     # copy contract/ and catalogue/ into src/main/resources/jeff-shipped (a test fails when stale)
./gradlew test            # unit and conformance tests, offline
./gradlew javadoc         # the API documentation; any doclint warning fails it
./gradlew build           # all of the above, and the jar in build/libs/
```

The public surface is pinned in `src/test/resources/public-api.txt`: a change to it fails `SurfaceTest` until the file
is updated with it.

The golden cases (`contract/golden/step1.json`) need a real engine with the weights of the base the shipped manifests
name, so `GoldenSlowTest` is skipped, saying why, unless `JEFF_TEST_SOCKET` names the socket of a running
`jeff runtime serve`. With it, every golden case of the adapters that engine serves must reproduce:

```bash
JEFF_TEST_SOCKET=/tmp/jeff.sock ./gradlew test --tests ai.jeff.sdk.GoldenSlowTest
```
