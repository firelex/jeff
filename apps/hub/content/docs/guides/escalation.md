---
title: Escalation
---
# Escalation

A decision **abstains** (`abstained: true`) when its confidence falls below the function's calibrated threshold.
Nothing escalates automatically — escalation must be requested through `on_low_confidence`, for every request shape (`calls`, `dispatch`,
`graph`) and, for a graph, per node with `on_low_confidence_by_node`. If omitted, an abstained decision is simply
returned with `abstained: true`, so the caller can decide what to do with it.

## The three policies

- **`"return"`** (the default): hand back the abstained decision as it is.
- **`"error"`**: raise `LowConfidence` (409) instead of returning the decision; the error's details carry the
  decision that abstained.
- **Escalate**: try other functions in order until one does not abstain.

## Escalating through other functions

Over HTTP, escalation is given as JSON:

```json
{"escalate": ["triage.urgency", "jeff.support.triage.urgency.v2"], "budget_ms": 5000}
```

`escalate` is a list of function references (an adapter alias plus a function key, or a full function ID — the same
form a call's own `function` field takes), each one implementing the same interface as the function that abstained,
tried in order until one answers without abstaining; at most 256 steps. `budget_ms` is optional; it limits the total
time for all steps combined. Before anything is decided, each referenced function is checked — it must be
registered and installed, implement the same interface and be licensed to the caller — exactly as a call's own
`function` field is checked. The returned decision's `decided_by` names whichever function actually answered, and
its `escalations` list carries one `{decided_by, value, confidence, reason}` record per step that was tried,
including the one that abstained first.

```bash
curl -s {{origin}}/v1/decide \
  -H "Authorization: Bearer <token>" -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "triage.urgency", "input": "...", "context": {"...": "..."}}],
       "on_low_confidence": {"escalate": ["triage.urgency.v2"], "budget_ms": 5000}}'
```

Sending anything that is not a function-reference string in `escalate` — `"llm"`, `"human"`, `"remote"`, or any
other value — is refused with `InvalidRequest` naming the step; the server never silently drops or ignores it.

## Escalating to an LLM, a person or another server

The `jeff` Python package's escalation steps also include asking an LLM, a person, or another `Jeff` entirely —
`jeff.escalation.llm(ask)`, `jeff.escalation.human(ask)`, `jeff.escalation.remote(client)` — but **the server
cannot run these steps itself**: there is nothing on the server side to call the caller's own LLM or show a question
to the caller's own staff. [Through the API](/docs/guides/remote), these three step kinds run inside the SDK, on the
abstained decision the server returns, costing one further request to the server per escalation step that actually
runs. A `human` or `llm` step is given a `Question` (the function's `instructions`, its `options`, and the
state the model read: an object of named fields for most functions, and the plain input text itself for a function
whose input is plain text, `input.form: text`) and must return one of the option keys:

```python
from jeff.escalation import escalate, human

def ask_a_person(question):
    # show question.instructions, question.options and question.state to someone; return their chosen key
    return next(iter(question.options))

decision = jeff.rate(
    message, function="triage.urgency", context=context,
    on_low_confidence=escalate(human(ask_a_person)),
)
print(decision.decided_by)    # 'human'
print(decision.escalations)   # (EscalationRecord(decided_by='jeff.support.triage.urgency@1.3.0', value=3, ...),)
```

A step raises an error if it is unreachable, times out or returns a value outside the function's label set;
the low-confidence answer is never returned silently as if nothing had gone wrong.

## Next steps

- [Functions and /v1/decide](/docs/guides/decide): the three request shapes `on_low_confidence` attaches to.
- [Through the API](/docs/guides/remote): where `llm`, `human` and `remote` escalation steps actually run.
- [Errors](/docs/guides/errors): `LowConfidence`, `EscalationFailed` and `EscalationBudgetExceeded`.

