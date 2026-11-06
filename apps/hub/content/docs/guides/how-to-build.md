---
title: How to build with Jeff
---
# How to build with Jeff

Jeff is a software primitive, not an agent. Your program keeps the plan, the loop and the side effects; it calls a
Jeff function, as it calls any other function, when it meets messy input and needs a judgement about it. It never decides what to do next on its
own, and it is fast enough to be called on every request.

## Where Jeff sits

| | Rules in code | A language-model agent | Code that calls Jeff |
| - | - | - | - |
| What runs the workflow | Your code | The model | Your code |
| Reads unstructured text | No | Yes | Yes |
| What comes back | Exact values | Prose to parse | One of the function's own answers, with probabilities |
| Time per decision | Microseconds | Seconds | Milliseconds |
| Can be unit-tested | Yes | With difficulty | Yes: the same call gives the same probabilities |

## A worked design: a support inbox

A shop receives a few thousand customer emails a day. Some are spam, most need one of four teams, and a few need a
person straight away. The following intake workflow uses open functions
from the [catalogue](/catalogue).

### Start with what code can already decide

Blocked senders and empty messages are exact checks. Do them in code first, and send
the message to Jeff only when it passes these checks:

```python
if email.sender in blocked_senders:
    drop(email)
elif not email.body.strip():
    drop(email)
else:
    check_with_jeff(email)
```

### Bind each function once

Each function is bound to its context once, when the program starts; every message then costs one call.

```python
import os

import jeff

TEAMS = {
    "deliveries": "Deliveries: late, lost or damaged deliveries, and delivery bookings",
    "refunds": "Refunds and payments: refunds, charges and invoices",
    "assembly": "Assembly service: booking and complaints about assembly",
    "account": "Account access: logins, passwords and account details",
}
CONTEXT = {"company": "A mid-sized online furniture retailer shipping across Europe.", "channel": "email"}

client = jeff.Jeff.remote("{{origin}}", token=os.environ["JEFF_API_KEY"], timeout_s=30)
is_spam = client.function("jeff.safety.spam.is_spam", context={"channel": "email"})
route = client.function("jeff.support.triage.route", context=CONTEXT, labels=TEAMS)
urgency = client.function("jeff.support.triage.urgency", context=CONTEXT)
needs_human = client.function("jeff.support.triage.needs_human", context=CONTEXT)
```

`TEAMS` are your own labels: the routing function was trained to choose between whatever teams the caller lists,
always with one more option listed first, `other` ("Not for any of these teams"). Its decision's `value` is one of
your keys or `other`, so `other` cannot be one of your own keys. The other functions have fixed answers. The context field names
(`company`, `channel`) are the ones each function's catalogue page lists.

### Ask small questions, not one big one

"What should we do with this email?" combines several judgements into one answer that is hard to check. Ask each question
separately: is it spam, which team should handle it, how urgent is it, does a person need to act now. Each function answers separately; none sees
another's answer. To send them all in one request, use `client.decide` with one `jeff.Call` per function, or a
[graph](/docs/guides/graphs), which can also skip the triage calls when the message is spam.

### Turn the decisions into an action in code

```python
def check_with_jeff(email):
    text = f"Subject: {email.subject}\n\n{email.body}"
    if is_spam(text).value:
        return drop(email)
    team, level, person = route(text), urgency(text), needs_human(text)
    if team.abstained or person.abstained or team.value == "other":
        return send_to_supervisor(email)       # Jeff is not sure enough, or no team fits: a person decides
    if person.value or level.value >= 3:       # levels 3 and 4: within hours, immediately
        return page_on_call(team.value, email)
    return queue(team.value, email)
```

`urgency` is a `rate` function: its `value` is the position of its most likely level, from 0 ("No action needed") to
4 ("Handle immediately"), and its `score` is the expected level, a number between them. `abstained` is set when a
decision's confidence falls below the threshold its function was calibrated with.

Instead of handling `abstained` yourself, you can give a function an `on_low_confidence` policy: raise an error, or
hand the input on to another function, a language model or a person. See [Escalation](/docs/guides/escalation).

### Set thresholds from your own data

The functions' own thresholds are a starting point. With a few hundred past emails and their actual destinations, you
can compare `confidence` with what happened and choose where your code should stop trusting a decision, and
whether a stricter cut-off suits a costly action (paging someone) better than a less costly one.

## Test it like code

The same call on the same adapter version gives the same probabilities, apart from tiny rounding differences on
GPUs. That makes decisions testable: keep a set of real inputs with the answers you expect, run them in CI against
the versions you deploy, and fail the build when the answers change for the worse. The Python package's `jeff test`
command does this on your own machine with the engine package (`pip install jeff-sdk jeff-runtime`). Name the
adapters or functions the cases use, and pass the cases as JSON lines, one object per case with `id`, `function`,
`input`, `expected` and, where the function takes them, `context` and `labels`:

```bash
jeff test spam triage --mine cases.jsonl --save-baseline baseline.json   # print how many cases pass; save the results
jeff test spam triage --mine cases.jsonl --against baseline.json         # compare a later run with them
```

Without `--against`, the command fails when any case fails. With `--against`, it fails when a case that passed in
the baseline fails now, when a case that is not in the baseline fails, or when a case in the baseline is missing
from this run; a case that already failed in the baseline and still fails is reported but does not count as a failure. It also prints
any change of adapter version since the baseline.

## Worked examples

Four small systems built this way, each step a live call in every language, on your machine or through the API:

- [A support agent, end to end](/docs/guides/worked-support-agent): checks around every tool call of a shop's
  assistant, and the program that wires them together.
- [A shared inbox that sorts itself](/docs/guides/worked-inbox): who handles each message, what to do and how soon.
- [A retrieval pipeline that knows when not to answer](/docs/guides/worked-retrieval): an assistant that says "I
  don't know" when its documents do not hold the answer.
- [CI failures routed to the right person](/docs/guides/worked-ci): each failed run sorted by cause, and its issue
  routed to a team.
