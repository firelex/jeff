---
title: Escalation
---
# Escalation

A decision **abstains** when its confidence is below the threshold its function was calibrated with: the function
still gives its most likely answer, but is not sure enough for you to act on it alone. `on_low_confidence` says what
happens then. Set it when you bind a function, call a shorthand, dispatch, or run a graph (per node).

| Policy | What happens to an abstained decision |
| - | - |
| `"return"` (the default) | It is returned as it is, with `abstained` set to true. |
| `"error"` | `LowConfidence` is raised; its `decision` holds the decision. |
| `escalate(step, ...)` | Each step is tried in order until one gives an answer that does not abstain. |

A decision that does not abstain is returned as it is, whatever the policy. Each language expresses the policies in its own
way:

| | Return | Error | Escalate |
| - | - | - | - |
| Python | `"return"` | `"error"` | `escalate(step, ..., budget_ms=None)` |
| Node | `"return"` | `"error"` | `escalate([step, ...], { budgetMs })` |
| Rust | `Policy::Return` | `Policy::Error` | `escalate(vec![step, ...], Some(budget_ms))?` |
| C# | `Policy.Return` | `Policy.Error` | `Policy.Escalate([step, ...], budget)` |
| Java | `Policy.RETURN` | `Policy.ERROR` | `Policy.escalate(List.of(step, ...), budgetMs)` |

<!-- snippet: sdk/escalation-2 -->

## Steps

`escalate` takes one or more steps; passing no steps raises `InvalidPolicy`. Its budget limits the combined duration
of all steps (in milliseconds, or a `TimeSpan` in C#), and `EscalationBudgetExceeded` is raised when they take longer.

| Step | Who answers |
| - | - |
| `function_step(ref)` | Another function, named by any reference: for example a larger or more specialised one. It must implement the same interface as the function that abstained, and is called with the same input, context and labels. |
| `llm(ask)` | Your own language model. `ask` is a function you write: it is given a `Question` and returns one of its option keys. |
| `human(ask)` | A person. `ask` is the same kind of function, for example one that places the question in a review queue and waits for the answer. |
| `remote(client)` | Another Jeff: any client that decides calls, such as one made with `Jeff.remote` for another server. |

The steps are `functionStep`, `llm`, `human` and `remote` in Node; `function_step`, `llm`, `human` and `remote_step`
in Rust; `Steps.Function`, `Steps.Llm`, `Steps.Human` and `Steps.Remote` in C#; `Step.function`, `Step.llm`,
`Step.human` and `Step.remote` in Java. An `ask` function may be async in Node, Rust and C#.

Policies are values, so you can build them once and reuse them:

<!-- snippet: sdk/escalation-1 -->

Before anything is decided, every function step is checked: its function must be in the catalogue, installed, and
implement the right interface, or the call fails at once (`InvalidPolicy`, `NotInstalled` or `InterfaceMismatch`).
A step that fails while running (it raises, cannot be reached, or answers with a key that is not one of the options)
raises `EscalationFailed`, whose `step` names it; the abstained decision is not returned after a failed
step.

## The question an llm or human step is given

A `Question` has four fields:

- `function`: the full ID of the function that abstained;
- `instructions`: that function's instructions, the question it was trained to answer;
- `state`: what the model read. For most functions, an object holding the context fields and the input under their
  keys; for a function whose input is plain text, that text itself;
- `options`: the allowed answers, each key with its description (or none). For a yes-or-no function the keys are
  `"true"` and `"false"`; for a rating, `"0"`, `"1"`, and so on, one per level.

`ask` returns one of those keys, as a string; any other answer raises `EscalationFailed`. Build any prompt in the
order the model reads the fields and options. Node, Rust, C# and Java also give them as lists, `state_entries` and
`option_entries` (`stateEntries` and `optionEntries`, `StateEntries` and `OptionEntries`), which keep that order. In
Node, use those lists rather than `state` and `options`: those two are plain JavaScript objects, and JavaScript lists a
key that looks like a whole number (`"2"`, `"10"`) before every other key, regardless of its position in the data the engine sent.

<!-- snippet: sdk/escalation-3 -->

## The escalated decision

The decision returned after escalation is the answer of the step that did not abstain. Its `decided_by` names that
step (the function and its version, or `"llm"` or `"human"`), and `escalations` holds one `EscalationRecord` for each
attempt that abstained before it, the original decision first, each with `decided_by`, `value`, `confidence` and
`reason`. An answer from an `llm` or `human` step has no `confidence`, `calibrated` false and no `probabilities`. If
every step abstains, the last step's abstained decision is returned, with the attempts before it in `escalations`.

## Where the steps run

On your machine every step runs in your process. Through the API, `"return"`, `"error"` and an escalation consisting only of
function steps are sent with the request and run on the server. A policy with an `llm`, `human` or `remote` step runs
in your process instead, on the abstained decision the server returned, because only your process can call those
functions. See [Escalation](/docs/guides/escalation) for the same policies over HTTP.

## Next steps

- [Errors](/docs/sdk/errors): `LowConfidence`, `EscalationFailed`, `EscalationBudgetExceeded` and `InvalidPolicy`.
- [Decision log](/docs/sdk/decision-log): keep a record of every decision, escalated or not (Python only).
