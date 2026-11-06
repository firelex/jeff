---
title: Errors
---
# Errors

Every error Jeff can raise is listed in `contract/errors.yaml` (its server-side name, HTTP status
and meaning), and every SDK maps that name back to the same exception class. An error body over HTTP is
`{"error": {"type", "message", "details"}}`, and every response — success or error — carries an `x-request-id`
header; quote it when asking the server's operator about a failure.

A few entries have no HTTP status at all: they occur only on your machine (no server involved) or on the raw engine
socket, and can never reach an HTTP or channel client.

| Error | HTTP status | Meaning |
|---|---|---|
| `JeffError` | 500 | Base class; never raised directly. |
| `ContractError` | 500 | A document does not match its contract schema. |
| `ManifestError` | 500 | An adapter manifest or the routing tree is invalid. |
| `InvalidReference` | 422 | A function reference is malformed. |
| `UnknownFunction` | 404 | No function or adapter in the catalogue has this ID, alias or version. |
| `NotInstalled` | 424 | The function exists but its adapter is not loaded. |
| `MissingContext` | 422 | Required context fields are missing. |
| `UnknownContext` | 422 | Context fields the function does not have. |
| `MissingLabels` | 422 | The function takes its labels from the caller and none were given. |
| `InvalidLabels` | 422 | Labels are given where they are fixed, or keys collide or are malformed. |
| `TooManyLabels` | 422 | More labels than the function was trained on. |
| `TooManyHandles` | 422 | A channel connection already holds the most bound-function handles it may (1024); release one first. |
| `InterfaceMismatch` | 422 | A function was called through an interface it does not implement. |
| `LowConfidence` | 409 | The decision abstained and on_low_confidence was 'error'; details carry the decision. |
| `EscalationFailed` | 502 | An escalation step failed. |
| `EscalationBudgetExceeded` | 504 | An escalation ran past its budget_ms. |
| `InvalidPolicy` | 422 | on_low_confidence is not valid for this call. |
| `GraphError` | 422 | A decision graph is invalid or cannot run. |
| `RuntimeMissing` | Not sent over HTTP | Local mode needs the jeff-runtime package (Python), or uv to start the engine (Node, Rust, C#, Java). |
| `ProprietaryWeights` | Not sent over HTTP | A proprietary adapter's weights need a licence or a local folder. |
| `CalibrationError` | Not sent over HTTP | Calibration cannot reach the target or has no data. |
| `EngineError` | 500 | The engine failed or broke its contract. |
| `InputTooLong` | 422 | The request is longer than the token limit; it was refused, never truncated. |
| `BackendUnavailable` | 503 | The engine backend cannot run on this machine. |
| `Busy` | 529 | The engine queue is full; retry after the Retry-After seconds. |
| `DeadlineExceeded` | 504 | The request's deadline passed before it ran. |
| `ProtocolMismatch` | Not sent over HTTP | The two ends of the engine socket speak different protocol versions. |
| `NotReady` | 503 | The engine is still starting. |
| `Unauthorised` | 401 | Missing or invalid credentials. |
| `NotLicensed` | 403 | The caller's token does not include this proprietary function's licence. |
| `RateLimited` | 429 | The caller exceeded its rate limit; retry after the Retry-After seconds. |
| `UsageLimitReached` | 402 | The account has used its input-token allowance for the billing period; calls resume when the next period starts. |
| `InvalidRequest` | 422 | The request does not match the API schema. |
| `Conflict` | 409 | The request does not fit the current state of the factory run. |
| `FactoryOff` | 403 | The factory is off on this server. |

`Busy` and `RateLimited` both carry `retry_after_s` in their details: wait that many seconds before trying again.
A `LowConfidence` error's details carry the decision that abstained, so the caller can inspect it even when it
asked for `"error"` rather than `"return"` (see [Escalation](/docs/guides/escalation)).

`UsageLimitReached` is checked when a request first calls the model, so listing functions and binding a function on
the channel still work after the allowance is reached, and a request that starts within the allowance runs to completion. Usage
counts the input tokens of every request the model answered, even when a later part of the same API request fails
(for example, the second function of a batch): those tokens have already been processed.

## Next steps

- [Functions and /v1/decide](/docs/guides/decide): where most of these errors come from.
- [The low-latency channel](/docs/guides/channel): `TooManyHandles`, and the same errors carried without an HTTP status.

