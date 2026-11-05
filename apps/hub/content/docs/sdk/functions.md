---
title: Functions
---
# Functions

A Jeff **function** is a small trained model that answers one kind of question about its input with a typed value
and a calibrated confidence. This page shows how to call one and how to read what it returns. For an explanation of functions and
how they are trained, see [How Jeff works](/docs/guides/how-it-works); every function in the catalogue has a
[catalogue page](/catalogue) listing its context, its input and its answers.

These pages use Python's names. Each SDK follows its own language's conventions: `on_low_confidence` is
`onLowConfidence` in Node and Java, `on_low_confidence` in Rust, and `OnLowConfidence` in C#; `decided_by` is
`decidedBy`, `decided_by` and `DecidedBy`; and so on. Every method that reaches an engine or a server returns a
promise in Node and a future in Rust, and is `...Async` in C#; Java blocks, and has an `...Async` twin of each.

## Naming a function

A call names its function by a **reference**:

- the full function ID, `jeff.<domain>.<adapter>.<function>`, for example `jeff.safety.guard.attempt`;
- the adapter's alias and the function's key, for example `guard.attempt`;
- either of these followed by `@` and a version, for example `guard.attempt@1.4` (any leading part of the adapter's
  version matches).

An adapter with a single function can be named without the function's key. A reference that is not of one of these
forms raises `InvalidReference`; one that names nothing in the catalogue raises `UnknownFunction`. The catalogue
answers these questions without a server or a model:

<!-- snippet: sdk/functions-2 -->

## Binding a function

`client.function(ref, *, context=None, labels=None, on_low_confidence="return")` returns a `BoundFunction`: the
function with its context and labels fixed. Everything that can be checked is checked here, once (the reference,
the context fields, the labels and the policy), so invalid arguments fail during binding rather than on the first call. Each
call then adds only the input.

- `context` is an object of the function's context fields, each given by its **code name**: the field's key, or for a
  key with spaces or other characters a Python name cannot contain, the key written as a name (`counterparty_name` for
  `counterparty name`). A required field left out raises `MissingContext`; a field the function does not have raises
  `UnknownContext`. A function whose input is plain text takes no context at all.
- `labels` is only for a function whose answers you supply or choose from: an object mapping each label key to a description,
  or a list of keys. `MissingLabels`, `InvalidLabels` and `TooManyLabels` say what is wrong with them.
  [Functions and /v1/decide](/docs/guides/decide#section-calling-a-function) sets out which form each kind of function
  takes. In Java, give labels as a `LinkedHashMap` (or another map that keeps its order): a `Map.of` with two or more
  entries keeps no order, and is refused.
- `on_low_confidence` says what happens when a decision abstains: see [Escalation](/docs/sdk/escalation).

A bound function is called with one input and returns one `Decision` (`call(input)` in every language but Python,
where the bound function is called directly). `many(inputs)` decides a list of inputs together and returns their
decisions in the same order; through the API that is one request, for at most 256 inputs (more raises
`InvalidRequest` before anything is sent). `close()` releases the bound function's server handle, which exists
only over the channel; in Python it can also be used as a `with` block, in C# disposed with
`await using`, in Java closed with try-with-resources.

<!-- snippet: sdk/functions-1 -->

## Several functions in one batch

`client.decide(calls)` takes a list of calls, each naming its own function, input, context and labels, and returns
one `Decision` per call, in order. Calls to the same adapter are run together. Through the API the whole list is one
request (at most 256 calls); an empty list returns an empty list.

<!-- snippet: sdk/functions-3 -->

A call is a plain, immutable value: `Call(function, input, context=None, labels=None)` in Python, Rust, C# and
Java (in Rust, `Call::new(function, input)` with `.with_context(...)` and `.with_labels(...)`), and a plain object
`{ function, input, context, labels }` in Node. `decide` applies no `on_low_confidence` policy: an abstained decision
is returned as it is.

## The Decision

Every function, shorthand, graph node and escalation returns a `Decision`, with these fields:

| Field | Type | What it is |
| - | - | - |
| `value` | a string, a boolean or a whole number | The answer: a label key (classify, route, is_supported, equivalent), true or false (detect, requires_escalation), or the index of a level, from 0 (rate). |
| `confidence` | a number, or none | How likely `value` is to be right, from 0 to 1. None when a person or a language model answered during escalation. |
| `abstained` | boolean | True when `confidence` is below the threshold the function was calibrated with, or the answer is one the function marks as abstaining. |
| `calibrated` | boolean | Whether the function has a calibrated threshold on the model that answered. |
| `interface` | string | The interface the function implements (see [Interfaces](/docs/sdk/interfaces)). Java names it `interfaceName()`. |
| `function`, `version` | string | The function's full ID and its adapter's version. |
| `probabilities` | a map of label key to number | The probability of each possible answer, by label key (`"true"` and `"false"` for a yes-or-no function, `"0"`, `"1"`, ... for the levels of a rating). Empty when a person or a language model answered. |
| `score` | a number, or none | For a rating, the expected level: each level's index weighted by its probability. None otherwise. |
| `route` | a list of `RouteStep` | The router steps taken to reach this function, when it was reached through routing; empty otherwise. Each `RouteStep` has `router`, `chose` and `confidence`. |
| `decided_by` | string | Who answered: the function and its version (`jeff.safety.guard.attempt@1.4.0`), or `"llm"` or `"human"` after escalation. |
| `escalations` | a list of `EscalationRecord` | The earlier attempts when the decision was escalated, each with `decided_by`, `value`, `confidence` and `reason`. |
| `timing` | a map | How long the decision took, as the engine reports it. |
| `evidence_refs` | a list of strings | References into the input that support the decision, when the function gives them. |

The decision's JSON form is the object returned by `POST /v1/decide` (the fields are those of
[Functions and /v1/decide](/docs/guides/decide#section-each-decision-s-fields)): `decision.to_json()` writes it and
`Decision.from_json(data)` reads one back (`toJSON` and `fromJSON` in Node, `ToJson` and `FromJson` in C#, `toJson`
and `fromJson` in Java). Rust's `to_json_text()` writes exactly the text Python's `json.dumps(decision.to_json())`
writes.

### The order of probabilities in Node

Every SDK keeps `probabilities` in the function's own option order, except one detail of Node: there it is a plain
JavaScript object, and JavaScript always lists a key that looks like a whole number (`"2"`, `"10"`) before every
other key, in numeric order, regardless of when it was added. So when a function's labels include such a key, as a rating's
levels `"0"`, `"1"`, ... do, iterating the object (`Object.keys`, `for...in`, `JSON.stringify`) may not list the
answers in option order. The values are the same; only the order of the keys differs. When the order matters, read it
from the function's labels in the catalogue.

## Next steps

- [Interfaces](/docs/sdk/interfaces): the shorthand methods, one per kind of answer.
- [Escalation](/docs/sdk/escalation): what to do when a decision abstains.
- [Errors](/docs/sdk/errors): every error these calls can raise.
