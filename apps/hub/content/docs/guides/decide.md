---
title: Functions and /v1/decide
---
# Functions and /v1/decide

Jeff answers questions with **functions**: small, trained models that turn a piece of text (or an image) into a
typed, calibrated decision — a chosen label with a probability for every option, never free text. A function has an
ID such as `jeff.support.triage.route`, belongs to one adapter, and implements an **interface** (`classify`, `route`,
`rate`, `detect`, `is_supported`, `equivalent` or `requires_escalation`) that fixes the shape of its answer.
`GET /v1/functions` lists every function this server currently has loaded, with its interface, its context and input
fields, its labels and its calibration.

Every decision goes through one HTTP call: `POST /v1/decide`. The request body is exactly one of three shapes —
`calls`, `dispatch` or `graph` — and the server's `on_low_confidence` setting says what happens when a decision is
not confident enough to act on (see [Escalation](/docs/guides/escalation)). This page covers `calls`, the shape for calling
named functions directly, and `dispatch`, the shape for letting Jeff choose which function should answer. Running a
decision graph is covered on [its own page](/docs/guides/graphs).

## Calling a function

A `calls` request contains up to 256 calls, each naming a function and an input. The results follow the same order:

```bash
curl -s {{origin}}/v1/decide \
  -H "Authorization: Bearer <token>" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "triage.route", "input": "My wardrobe arrived broken",
                  "context": {"company": "A furniture shop.", "channel": "email"},
                  "labels": {"deliveries": "Deliveries", "refunds": "Refunds"}}]}'
```

```json
{"decisions": [{"function": "jeff.support.triage.route", "value": "deliveries", "confidence": 0.998, "..."}]}
```

`function` may be the function's full ID, an adapter alias plus its function key (`"triage.route"`), or any other
form the catalogue accepts as a reference to the same function; `InvalidReference` is raised for anything malformed.
`context` is an object mapping context-field code names to values — each field's `code_name` in `GET /v1/functions` (the
manifest's key itself unless it has spaces or other characters that are not allowed in a code name: `counterparty_name` for
the key `counterparty name`, which the prompt shows exactly as trained). Leave it out (or send `null`) when the
function takes none. A function whose listed `input.form` is `text` was trained on plain text alone: its `input` must
be a string, which the model sees unwrapped, and it takes no context (`UnknownContext`). `labels` is only for a
function whose labels come from the caller, or that offers a subset of its own labels: an object mapping label keys to
descriptions (`null` for no description), or a list of label keys choosing a subset of the function's own labels. A
function whose listed labels are `fixed` takes no `labels` unless its listing says `subset: true` (v1.4 soc's
`action`, for example): it then takes a non-empty list of its fixed keys, without repeats, and offers only those, in
the order of its fixed labels with their fixed descriptions; leaving `labels` out offers them all. A function that is not loaded on this server raises `NotInstalled` (424); one that
does not exist in the catalogue raises `UnknownFunction` (404).

A request with more than 256 calls, or with no calls at all, is refused as `InvalidRequest` before anything runs —
`calls` must have at least one entry.

## Letting Jeff choose: dispatch

When the caller does not know which function should answer, `dispatch` routes the input through a tree of trained
router functions (starting from `jeff.core.router` unless another `router` is named) down to the function that
should handle it, then answers with that function's own decision:

```json
{"dispatch": {"input": "My wardrobe arrived broken", "interface": "route",
               "context": {"company": "A furniture shop.", "channel": "email"},
               "labels": {"deliveries": "Deliveries", "refunds": "Refunds"}}}
```

```json
{"decision": {"value": "deliveries", "route": [{"router": "jeff.core.router", "chose": "jeff.support.triage.route", "confidence": 0.97}], "..."}}
```

`interface` restricts dispatch to functions that implement it; a router step that reaches a function implementing
another interface fails with `InterfaceMismatch`. The returned decision's `route` field lists every router step that
was taken to get there, each with the router's own ID, what it chose, and its confidence in that choice.

## Each decision's fields

Every decision — whether from `calls`, `dispatch` or a graph node — has the same shape
(`contract/schemas/decision.schema.json`):

| Field | Meaning |
|---|---|
| `value` | The chosen label, score or boolean |
| `confidence` | How confident the function is in `value` (0 to 1), or `null` for a function with no calibrated threshold |
| `abstained` | `true` when confidence fell below the function's calibrated threshold — see [Escalation](/docs/guides/escalation) |
| `calibrated` | Whether this function has a calibration threshold at all |
| `interface`, `function`, `version` | Which interface answered, the function's full ID (its adapter's own ID, the versioned one even when you named it by an alias), and its adapter's version |
| `probabilities` | Every label's own probability (for choice- and detect-shaped answers) |
| `score` | The numeric score (for rate-shaped answers), or `null` |
| `route` | The router steps taken to reach this function, empty outside `dispatch` |
| `decided_by` | The function (and version) that produced `value` — the escalation step that answered, if any escalated |
| `escalations` | Every escalation attempt made before `decided_by` answered |
| `timing` | How long the decision took, engine-side |
| `evidence_refs` | References into the input that support the decision, when the function provides them |

## Pinning a version

Some adapters carry the base they were trained for in their ID, such as the open router
(`jeff.routing.router-open-v14`). Each also answers to the same ID without the version, `jeff.routing.router-open`,
and the docs use that name. It follows the newest such adapter only while its interface stays the same: when a new
base is released, the name moves to the new base's adapter only if that adapter takes the same context, input and
labels, so your code does not change; one that changes any of them gets a new name, and the old name stays where it
was. To keep the adapter unchanged, send the versioned ID (`jeff.routing.router-open-v14.specialist`), which names
that one adapter for as long as the server serves it. Either way, the decision's `function` and `decided_by` name the
versioned function that ran, so they change when the name moves. A version pin on the unversioned name
(`jeff.routing.router-open.specialist@1.4`) works only until the name moves, and is then refused with
`UnknownFunction`; pin the versioned ID instead. `GET /v1/functions` lists every other name of each function under
`aliases`, and `GET /v1/adapters` each adapter's aliases. See
[The swarm](/docs/guides/the-swarm#section-pinning-a-version) for the router's names.

## Next steps

- [Escalation](/docs/guides/escalation): what `on_low_confidence` can do about an abstained decision.
- [Graphs](/docs/guides/graphs): wiring several functions together in one request.
- [Errors](/docs/guides/errors): every error `/v1/decide` can answer with.

