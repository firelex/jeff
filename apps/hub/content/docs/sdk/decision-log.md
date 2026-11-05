---
title: Decision log
---
# Decision log

A `DecisionLog` writes one line of JSON for every decision a client makes to a specified file. Logging is off unless
you pass a log. Use it to audit what was decided, or to collect the cases where a function abstained and a person had to
answer.

**Only the Python SDK has a decision log.** In Node, Rust, C# and Java, write
each `Decision` you get back (its JSON form, `toJSON`, `to_json`, `ToJson` or `toJson`) to a log of your own.

`DecisionLog(path, include_input=False)` appends to the file at `path`, creating its folder if needed. Pass it as
`log=` to `Jeff.local` or `Jeff.connect` (see [Connect](/docs/sdk/connect)):

<!-- snippet: sdk/decision-log-1.local.py -->

Calls through the API do not accept a log: the server makes those decisions.

## What each line holds

| Field | What it is |
| - | - |
| `time` | When the line was written, in UTC (ISO 8601). |
| `function`, `version` | The function's full ID and its adapter's version. |
| `input_sha256` | The SHA-256 of the call's input, context and labels together, so the same call always gives the same hash without the log storing what was asked. |
| `value`, `confidence`, `abstained`, `calibrated` | As in the [Decision](/docs/sdk/functions#section-the-decision). |
| `route`, `escalations`, `decided_by` | As in the Decision: how the function was reached, and who answered. |
| `input` | The input itself, only with `include_input=True`. |

Every decision is written, including each one a graph node, a router or an escalation `function_step` makes. A
decision that is escalated is written twice: first as the function answered it (abstained), then as the final answer from
escalation, with its `escalations`.

## Next steps

- [Escalation](/docs/sdk/escalation): the policies whose answers the log records.
- [Engines](/docs/sdk/engines): a client made from an engine of your own takes `log=` too.
