# Jeff.Sdk: Jeff for .NET

Call Jeff's decision functions from .NET: bind a function, call it, read a typed `Decision`, route a call to the right
function, escalate when Jeff is unsure, and run decision graphs.

The development notes for the package; the short README.md is what NuGet shows. It is published on NuGet as
`Jeff.Sdk` (`dotnet add package Jeff.Sdk`); to build it from this repository, run `dotnet pack` (see Development). It targets `net8.0`, so it should run on .NET 8 or later, but it has been tested
only on .NET 10 so far.

## Two ways to run

**Local** (the fastest): the SDK talks straight to a Jeff engine running on the same machine, over the engine's Unix
socket. There is no server in between. The SDK never starts the engine; start it yourself (the model runs only in the
Python engine) and keep it running with your process manager:

```bash
jeff runtime serve --socket /tmp/jeff.sock --adapter jeff.support.triage \
  --batch-window-ms 0.1 --max-batch-rows 64 --queue-limit-rows 4096 --prepare-workers 2
```

`--socket`, `--batch-window-ms`, `--max-batch-rows`, `--queue-limit-rows` and `--prepare-workers` are always
required. On an NVIDIA GPU the engine also requires `--max-adapters` and `--prefix-pool-mb` (and refuses to start
without them); on other backends (Apple Silicon, CPU) those two options are refused.

**Choosing the batch window.** The engine waits up to `--batch-window-ms` after a request arrives for others to join
its batch, so every call pays up to that much extra. With one caller at a time, keep it small (0.1 ms, say); with many
callers at once, a longer window lets more of them share one batch.

```csharp
using System.Text.Json.Nodes;
using Jeff.Sdk;

await using var jeff = await JeffClient.LocalAsync("/tmp/jeff.sock", new LocalOptions { Timeout = TimeSpan.FromSeconds(30) });
var urgency = await jeff.FunctionAsync("triage.urgency",
    new CallOptions { Context = new JsonObject { ["company"] = "A furniture shop.", ["channel"] = "email" } });
var decision = await urgency.CallAsync("My wardrobe arrived broken");
Console.WriteLine($"{decision.Value} {decision.Confidence} {decision.Abstained}");
```

If nothing listens on the socket, `LocalAsync` fails with an error that names the socket and the command above.

**Remote**: the SDK calls a Jeff server (ours or your own) over HTTP, or over the server's WebSocket channel for lower
latency:

```csharp
await using var jeff = await JeffClient.RemoteAsync("https://api.example.com", Environment.GetEnvironmentVariable("JEFF_TOKEN"),
    new RemoteOptions { Timeout = TimeSpan.FromSeconds(30) });
await using var fast = await JeffClient.RemoteAsync("https://api.example.com",
    TokenSource.From(async cancellationToken => await CurrentTokenAsync(cancellationToken)),
    new RemoteOptions { Timeout = TimeSpan.FromSeconds(30), Channel = true });
```

The token is a string, a `TokenSource.From` function returning the current token (called before every HTTP request,
and refreshed on the channel before a JWT expires), or `null` only for a server with no credentials. No error message
ever shows a token.

**Timeouts over HTTP.** `Timeout` bounds each phase of a request separately, not the request as a whole: waiting for a
free connection, connecting, waiting for the answer's headers (which also covers sending the request body), and each
wait for more of the answer's body.

**Timeouts on the channel** (`Channel = true`). The same `Timeout` bounds opening the channel, its hello and the
closing handshake. For each request, it bounds the wait for its reply, and that wait starts only once the request's
whole message has been sent: a large message on a slow but working link takes as long as it needs to send. Sending is
bounded by stalls instead: if the socket accepts no piece of a message for `Timeout` (the server or the network stopped
taking data), the channel ends with `BackendUnavailable` saying so, and every request waiting on it fails with that
error. As in the Python SDK, the channel sends a WebSocket ping every 20 seconds, and a pong that has not come 20
seconds after its ping ends the channel with `BackendUnavailable` naming the missing pong. This needs .NET 9 or later
at run time: the .NET 8 `ClientWebSocket` cannot send pings, so on .NET 8 the channel sends none and a server that
stops answering is noticed only by each request's own timeout.

Every asynchronous method also takes a `CancellationToken`: cancelling fails only that call, with
`OperationCanceledException`; the connection goes on serving other calls.

**Proxies.** Proxies are taken from environment variables, as the Python SDK's two libraries read them: httpx for the
HTTP requests and websockets for the channel. `HTTPS_PROXY`, `HTTP_PROXY`, `ALL_PROXY` (HTTP requests only) and
`NO_PROXY`, in upper or lower case (a lower-case one wins), with the same choice of proxy and the same `NO_PROXY`
matching as Python. `https://` requests and the channel's WebSocket go through a CONNECT tunnel (TLS is still checked
end to end), `http://` requests are forwarded. The operating system's own proxy settings are not read: with no proxy
variable set there is no proxy. SOCKS proxies are refused by name, as the Python SDK cannot use them either. No error
message shows a proxy's user name or password.

## What you can do

- `DecideAsync([new Call(function, input, context, labels)])`: many calls at once, one engine request per adapter.
- `FunctionAsync(reference, new CallOptions { Context, Labels, OnLowConfidence })`: bind once, then `CallAsync(input)`
  or `ManyAsync(inputs)`. On the channel a bound function is a handle on the server; dispose it to release the handle
  (one you drop is released by its finalizer, before the channel's next request).
- `ClassifyAsync`, `RouteAsync`, `RateAsync`, `DetectAsync`, `IsSupportedAsync`, `EquivalentAsync`,
  `RequiresEscalationAsync`: call through an interface, with `ShorthandOptions.Function` to name the function, or without
  it to let the routers choose (`DispatchAsync`).
- `OnLowConfidence`: `Policy.Return` (the default) returns the unsure decision, `Policy.Error` raises `LowConfidence`,
  `Policy.Escalate([Steps.Function("triage.sentiment"), Steps.Llm(AskAnLlm), Steps.Human(AskAPerson)], budget)` tries
  each step in turn. An `Llm` or `Human` step is your own function: it gets a `Question` and returns one of the option
  keys. Build a prompt from the question's `StateEntries` and `OptionEntries`: they keep the order the model reads
  (and the Python SDK shows), keys that look like numbers included.

  ```csharp
  async Task<string?> AskAnLlm(Question question, CancellationToken cancellationToken)
  {
      var state = question.StateEntries is { } fields
          ? string.Join("\n", fields.Select(entry => $"{entry.Key}: {entry.Value?.ToJsonString()}"))
          : question.State.GetValue<string>(); // a text input: the state is the plain input string
      var options = string.Join("\n", question.OptionEntries.Select(entry => $"{entry.Key}: {entry.Value}"));
      return await CallYourLlmAsync($"{question.Instructions}\n\n{state}\n\nOptions:\n{options}", cancellationToken); // one of the option keys
  }
  ```
- `RunGraphAsync(graph, input, parameters, code, policies)`: a `Graph`, a graph file's path or a graph document; code
  nodes are your own functions (`CodeFunction`).

Every error is a `JeffError` subclass named as in `contract/errors.yaml` (`NotInstalled`, `LowConfidence`, ...), with
the same message and `Details` the Python SDK gives for the same failure; typed properties such as
`NotInstalled.FunctionId` throw a `ContractError` naming the field when a server sent an error without it.

## Behaviour

The SDK follows the Python SDK exactly: `contract/conformance/` holds what the Python SDK does (rows sent to the engine,
decisions, socket conversations, server exchanges), and this package's tests replay all of it, comparing key order and
int versus float as well as values. JSON values are `System.Text.Json.Nodes` nodes; the SDK copies every node you pass
it, so you may reuse a context object for many calls or keep it in your own documents.

`LocalAsync` does no adapter version check on connect: the engine's `installed` reply carries adapter IDs only, no
versions (the Python SDK's `Jeff.connect` does none either), unlike `RemoteAsync`, which refuses a server serving a
different version of a catalogue manifest.

**Nesting limits.** Values nested inside one another (objects and lists within objects and lists) are refused, with an
error that says so, past a fixed depth, so that a hostile or broken value cannot exhaust the stack:

- MessagePack (what goes to and comes from the engine and the channel): 512 levels, writing and reading.
- YAML (manifests and `bases.yaml`): 512 levels.
- JSON read from a server or a file: 1,000 levels.
- Decision graph documents: 256 levels (the Python SDK itself stops at about 166 levels of nested `not` conditions, so
  every graph Python reads is accepted).

No real manifest, row or graph comes near these limits.

**Size limits.** An HTTP answer body is refused past 64 MiB, both as sent and after each `Content-Encoding` layer
(gzip, deflate) is undone, so a few kilobytes of stacked gzip cannot decode to gigabytes: the call fails with
`BackendUnavailable` naming the limit, and the client is closed (the channel likewise ends on a message past 64 MiB).
YAML aliases are copied where PyYAML shares them, so a document whose aliases expand to more than 1,000,000 values is
refused with `ManifestError`. A number in a manifest must be finite: a `.nan`, `.inf` or `-.inf` anywhere is refused
with `ManifestError` naming its place and value (contract/conformance/manifests.json), since a threshold of NaN would
turn calibration off without a word.

The SDK starts no threads of its own: an engine or channel you forget to dispose never keeps your process from
exiting, and a call you `await` is what keeps it waiting. Every `await` inside the SDK uses `ConfigureAwait(false)`,
so blocking on a call from a UI thread does not deadlock (prefer `await` anyway).

Two failures on the channel have no caller to report to, and the SDK writes them to standard error (`Console.Error`),
where the Python SDK logs them: a failed release of a bound function you dropped without disposing it, and a failed
token refresh that the channel will try again. Redirect `Console.Error` to route them elsewhere.

## Development

```bash
packages/dotnet/scripts/sync-shipped.sh                  # copy contract/ and catalogue/ into shipped/ (a test fails when it is stale)
uv run --package jeff python packages/dotnet/scripts/record-python-catalogue.py   # after a catalogue change: re-record the Python SDK's catalogue answers the tests compare with
dotnet test packages/dotnet/Jeff.Sdk.sln                  # unit and conformance tests, offline (the golden test is skipped)
JEFF_ENGINE_SOCKET=/tmp/jeff.sock JEFF_GOLDEN_ADAPTERS=jeff.compliance.aml,jeff.voice.nav \
  dotnet test packages/dotnet/test/Jeff.Sdk.GoldenTests   # golden cases against a running engine (needs the weights)
dotnet pack packages/dotnet/src/Jeff.Sdk -o <folder>      # a local .nupkg; never pushed anywhere
```

The golden test (`test/Jeff.Sdk.GoldenTests`) checks `contract/golden/step1.json` against a real engine. Without
`JEFF_ENGINE_SOCKET` it is skipped, and the skip reason names the variable; with it, `JEFF_GOLDEN_ADAPTERS` must list
adapters the engine serves that have golden cases, or the test fails saying which.
