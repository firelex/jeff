---
title: Errors
---
# Errors

Every error the SDK raises is a `JeffError`, and every message names what is missing or wrong. Through the API, an
error returned by the server is raised as the same error, with the same message, as a call on your machine would
raise. Every SDK has the same errors, with the same names, messages and details:

| | The errors | How to tell them apart |
| - | - | - |
| Python | Subclasses of `jeff.JeffError` (`jeff.UnknownFunction`, ...) | `except jeff.UnknownFunction` |
| Node | Subclasses of `JeffError` from `jeff-sdk` | `error instanceof UnknownFunction`, or `error.type` |
| Rust | One `JeffError` type | `error.kind() == ErrorType::UnknownFunction`; `error.type_name()` gives the name |
| C# | Subclasses of `Jeff.Sdk.JeffError` | `catch (UnknownFunction error)`, or `error.Type` |
| Java | Unchecked subclasses of `JeffError`, nested in it | `catch (JeffError.UnknownFunction error)`, or `error.type()` |

<!-- snippet: sdk/errors-1 -->

## Details worth reading

Each error also carries the same details as the API's error body (`details`; `details()` in Rust and Java, `Details`
in C#), and typed accessors for the useful ones. An accessor whose detail the server did not send raises an error
naming it; it does not invent a value.

- `request_id` (every error): the ID the server gave the request that failed (its `x-request-id`), or none for an
  error raised in your process. Quote it when you ask the server's owner about a failure.
- `retry_after_s` (`Busy`, `RateLimited`): how many seconds to wait before trying again.
- `decision` (`LowConfidence`): the decision that abstained.
- `step` (`EscalationFailed`): the escalation step that failed.
- `function_id` (`NotInstalled`, `MissingContext`, `MissingLabels`), `fields` (`MissingContext`) and `adapter_id`
  (`ProprietaryWeights`): what the error is about.
- `account`, `used`, `allowance`, `period_end` (`UsageLimitReached`): whose allowance ran out, the input tokens used
  and allowed this billing period, and when the next period starts (ISO 8601, UTC).

Each language uses its own naming conventions: `requestId` and `retryAfterS` in Node and Java, `request_id()` and
`retry_after_s()` in Rust, `RequestId` and `RetryAfterS` in C#.

<!-- snippet: sdk/errors-2 -->

## Every error

The table uses Python's argument names (`timeout_s` is `timeoutMs` in Node and Java, `timeout_ms` in Rust and
`Timeout` in C#). Errors about `Jeff.local` loading the models in your process are raised only by the Python SDK.

| Error | Raised when | What to do |
| - | - | - |
| `JeffError` | Never raised itself: the base class of every error below. | Catch it to handle any Jeff error in one place. |
| `ContractError` | A document does not match its schema, or `Jeff.remote` finds the server serving another version of an adapter than your catalogue holds. | Update `jeff-sdk` (its catalogue) to match the server, or pass the matching catalogue in `catalogue=`. |
| `ManifestError` | An adapter manifest is invalid or does not fit the base or the interfaces; `Jeff.local` is given a single string, adapters on different bases, or a `paths=` key that is not one of its adapters. | Fix the manifest or arguments as the message explains. |
| `InvalidReference` | A function reference is malformed. | Use `jeff.<domain>.<adapter>[.<function>]` or `<alias>[.<function>]`, optionally with `@<version>`. |
| `UnknownFunction` | No function or adapter in the catalogue has this ID, alias or version; an adapter with several functions is named without one; a dispatch has no router to start from. | Check the name on the adapter's [catalogue page](/catalogue), or name the function with `function=`. |
| `NotInstalled` | The function exists, but its adapter is not loaded by the engine or server; in local mode in Node, Rust, C# or Java, the engine already running does not serve an adapter you named and refused to load it. | On your machine, add the adapter to `Jeff.local`'s list, or serve it with the engine's `--adapter` (for an engine you start yourself; when programs need different adapters at once, start one engine with all of them and give its socket); through the API, ask the server's owner to load it. |
| `MissingContext` | A required context field was not given. | Pass every field the message lists, in `context=`. |
| `UnknownContext` | A context field the function does not have was given, a field was given by its key instead of its code name, or a plain-text function was given context. | Use the code names the message lists; give a plain-text function no context. |
| `MissingLabels` | The function takes its labels from you and none were given. | Pass `labels=`. |
| `InvalidLabels` | Labels were given to a function whose labels are fixed, or the keys repeat, collide or are malformed. | Follow the message: leave `labels=` out, or pass the form the function takes. |
| `TooManyLabels` | More labels than the function was trained on. | Shortlist the labels first, or split the question. |
| `TooManyHandles` | A channel connection has reached its limit of 1,024 bound-function handles. | Close bound functions you no longer use. |
| `InterfaceMismatch` | A function was called through an interface it does not implement, or an escalation `function_step` implements another interface. | Call the method the message names, or pick a step of the same interface. |
| `LowConfidence` | A decision abstained under the policy `"error"`. | Read `error.decision`, and act as your policy needs. |
| `EscalationFailed` | An escalation step failed: it raised, could not be reached, or answered with a key that is not one of the options. | Fix the step `error.step` names; the cause is chained to the error. |
| `EscalationBudgetExceeded` | The escalation steps took longer than `budget_ms`. | Raise `budget_ms`, or use faster steps. |
| `InvalidPolicy` | `on_low_confidence` is not `"return"`, `"error"` or an `escalate(...)`; `escalate()` has no steps or a step names an unknown function; a graph registered on the server is given an `llm`, `human` or `remote` step. | Give a valid policy. |
| `GraphError` | A graph document is invalid (schema, unknown nodes, undeclared params, a cycle), or a run is missing params or code functions, or a node's call cannot be bound. | Fix the graph or the arguments to `run_graph` as the message says. |
| `RuntimeMissing` | `Jeff.local` without `jeff-runtime` (Python), local mode in Node, Rust, C# or Java without uv (`uvx` is not on `PATH`, nor in `~/.local/bin` or `~/.cargo/bin`), or `jeff mcp` without the `mcp` extra. | `pip install jeff-runtime`; install uv with `curl -LsSf https://astral.sh/uv/install.sh \| sh`; or `pip install "jeff-sdk[mcp]"`. |
| `ProprietaryWeights` | `Jeff.local` is asked for an adapter whose weights are not open, and no folder was given for it (local mode in Node, Rust, C# and Java takes no folder). | Pass the folder of its weights in `paths=` (Python), start an engine yourself with `jeff runtime serve --path` and give its socket, or call it through the API. |
| `CalibrationError` | `jeff calibrate` cannot reach the target precision or has no data. | Give it data (`--data`, `--split-test`) or another target. |
| `EngineError` | The engine failed or broke its contract; only on an NVIDIA GPU, `Jeff.local` on the fast backend without `prefix_pool_mb` (or with it on another backend); nothing is listening at the engine's socket; in local mode in Node, Rust, C# or Java, the engine it started exited or did not answer within the start timeout, or `HOME` is not set. | Read the message; for `prefix_pool_mb`, see [Connect](/docs/sdk/connect#section-only-on-an-nvidia-gpu-the-prefix-pool); for an engine local mode started, read the log the message names (`engine.log`). |
| `InputTooLong` | The prompt is longer than the engine's token limit. It is refused, never cut short. | Send a shorter input or context. |
| `BackendUnavailable` | The requested backend cannot run on this machine: no NVIDIA GPU for `"fast"`, or MLX not installed for `"mlx"`; local mode in Node, Rust, C# or Java on a machine that is neither a Mac with Apple silicon nor Linux with an NVIDIA GPU. Through the API: the server or the proxy (named, any password hidden) could not be reached or refused the connection, or the channel's socket accepted no part of a message for the timeout. | Install the extra the message names, or pick another backend (or, for local mode, use a Jeff server through the API); for a server or proxy, check the address and your network, and connect again. |
| `Busy` | The engine's queue is full. | Wait `error.retry_after_s` seconds and try again. |
| `DeadlineExceeded` | The server or channel did not answer within `timeout_s`, or the request's deadline passed before it ran. | Retry, or give a longer `timeout_s`. |
| `ProtocolMismatch` | The two ends of the engine socket speak different protocol versions. | Use the same version of `jeff-sdk` and `jeff-runtime` on both ends. |
| `NotReady` | The engine is still starting. | Try again shortly. |
| `Unauthorised` | The token is missing or invalid, or (over the channel) it expired. | Send a valid token; see [Connect](/docs/sdk/connect#section-tokens). |
| `NotLicensed` | Your token does not include this proprietary function's licence. | Ask the server's owner for a token that includes it. |
| `RateLimited` | You exceeded your rate limit. | Wait `error.retry_after_s` seconds and try again. |
| `UsageLimitReached` | Your account has used its 50 million input tokens for this billing period. | Wait until `period_end`, when the allowance resets; see your usage on [your account page](https://jeffhub.ai/account). |
| `InvalidRequest` | The request does not match the API's schema; more than 256 calls or inputs in one request; `timeout_s` is not a number above 0; a proxy setting that cannot be used (a SOCKS proxy, an address that is not a URL). | Fix the request as the message explains; split a large batch yourself. |
| `Conflict` | Not raised by decision calls: a request to the server's adapter-building endpoints that does not fit the current state of the build. | Not needed for deciding. |
| `FactoryOff` | Not raised by decision calls: the server's adapter-building endpoints are switched off. | Not needed for deciding. |

The same errors over HTTP, with their status codes, are in [Errors](/docs/guides/errors).

## Next steps

- [Escalation](/docs/sdk/escalation): the policies behind `LowConfidence` and the escalation errors.
- [Connect](/docs/sdk/connect): timeouts and tokens.
