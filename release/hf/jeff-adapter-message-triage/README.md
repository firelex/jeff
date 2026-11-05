---
license: apache-2.0
base_model: mstrasser/jeff-base
base_model_relation: adapter
library_name: peft
pipeline_tag: text-classification
language:
- en
tags:
- jeff
- lora
- decision-model
- calibration
- support
---

# jeff-adapter-message-triage

**Shared inbox triage.** Classifies an inbox message, determines the required action and urgency, and selects the responsible team using your message categories, routing table and rules.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.support.message-triage`, version 1.4.0; short names: `oss-message-triage`, `message-triage`
- Catalogue page: [jeffhub.ai/catalogue/jeff.support.message-triage](https://jeffhub.ai/catalogue/jeff.support.message-triage)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- A shared inbox receives different types of messages, and you can define its categories, teams and handling rules.

## When not to use it

- You need to write a reply; this adapter only classifies and routes the message.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `recipient` (context) | yes | Whose inbox it is: an object with the recipient's name, address, role and organisation. |
| `message_kinds` (context) | yes | The recipient's own sorting list: an object of message kind -> description. |
| `routing_table` (context) | yes | Who handles what: a list of entries, each with key, name, address and the topics it handles. |
| `routing_rules` (context) | yes | Rules that override the routing table, a list of sentences. |
| `now` (context) | yes | The current date and time (for example 'Monday 8 February 2027, 12:15'). |
| `message` (input, fields) | yes | The newest message: an object with channel, from, to, cc, received, subject and body (it may quote or forward earlier messages). |

### `jeff.support.message-triage.route_to` (route, choice)

Instructions: Who should deal with this message? Choose the routing table entry whose responsibilities cover the topic of the newest message (for a forwarded message, the forwarded content), or self if it is a topic the recipient deals with personally. A routing rule that matches the message overrides the topic. Choose the entry that should deal with it even if it is already in the To or Cc, or if the message only informs.

Labels:

  - The caller gives the labels (at most 8), each a key and its text.

### `jeff.support.message-triage.action_needed` (classify, choice)

Instructions: What must the recipient do with this message? Go by the newest message. ignore_spam: unsolicited bulk advertising, a scam or a phishing attempt, whatever it claims. Otherwise first decide who deals with it (the routing table entry for its topic, unless a routing rule matches the message, or the recipient for topics they deal with). If that is a team or person in the routing table: forward when they are not already in the To or Cc of the message, fyi_only when they are. If it is the recipient: fyi_only when the message only informs and asks nothing; reply when the sender needs only a written answer, decision or confirmation; do_task when the recipient must do something beyond writing an answer, such as preparing or sending a document, changing a booking or record, fixing something, or approving or paying in a system.

Labels:

  - `do_task`: Do a task: the recipient deals with it and must do something beyond writing an answer (prepare or send a document, change a booking or record, fix something, approve or pay in a system).
  - `forward`: Forward: a team or person in the routing table should deal with it and is not already in the To or Cc of the message.
  - `fyi_only`: For information only: nothing is asked of the recipient: the message only informs, or the team or person who should deal with it is already in the To or Cc.
  - `ignore_spam`: Ignore as spam: unsolicited bulk advertising, a scam or a phishing attempt.
  - `reply`: Reply: the recipient deals with it, and the sender needs only a written answer, decision or confirmation.

### `jeff.support.message-triage.intent` (classify, choice)

Instructions: Which kind of message is this, using the recipient's own sorting list (the options)? Judge the newest message; earlier messages in the thread are context only. For a forwarded message, judge the forwarded content that is being passed on. Go by what the message actually asks or says, not by its subject line, which may be out of date or misleading.

Labels:

  - The caller gives the labels (at most 10), each a key and its text.

### `jeff.support.message-triage.urgency` (classify, choice)

Instructions: How soon must this message be acted on, judged against the current date and time given? now: something is broken, stopped or blocked right now, or the action is needed within the next two hours. today: needed before the end of today but more than two hours from now. this_week: needed after today and within the next seven days. no_deadline: no deadline stated or implied, or the deadline is more than seven days away. Spam, and messages that only inform and ask nothing of anyone, are no_deadline. A request addressed to someone else keeps its own deadline. Go by the facts: a polite or relaxed tone does not lower urgency, and words like 'urgent' in a subject line or a disclaimer do not raise it.

Labels:

  - `no_deadline`: No deadline: no deadline stated or implied, a deadline more than seven days away, or nothing needs doing.
  - `now`: Now: something is broken, stopped or blocked right now, or the action is needed within the next two hours.
  - `this_week`: This week: needed after today and within the next seven days.
  - `today`: Today: needed before the end of today, but more than two hours from now.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| CLINC150, a public intent benchmark (it asks the intent question only) (public) | 1,000 | 98.0% |
| the in-house test (generator) | 2,000 | 96.7% |
| the in-house test in domain families never seen in training (generator) | 2,000 | 97.0% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `route_to` | 0.502 | 96.9% | 100.0% | 520 |
| `action_needed` | 0.497 | 95.2% | 100.0% | 540 |
| `intent` | 0.443 | 95.4% | 100.0% | 540 |
| `urgency` | 0.486 | 98.2% | 100.0% | 400 |

Caveats:

- Training and test rows were kept only where a blind LLM check agreed with the label, so these scores are probably inflated.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Supplier invoice to the office manager". A supplier's invoice belongs to the finance team, who are not yet on the email, so the action is `forward`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["message-triage"])

decide = client.function("jeff.support.message-triage.action_needed", context={
    "recipient": {
        "name": "Lena Brandt",
        "address": "lena.brandt@hollowayarch.example",
        "role": "Office manager",
        "organisation": "Holloway Architects",
    },
    "message_kinds": {
        "invoice": "A supplier's bill or a question about one",
        "booking": "Meeting room, travel or event bookings",
        "it_problem": "Something broken with computers, phones or the network",
        "newsletter": "Bulk mail and marketing",
    },
    "routing_table": [
        {
            "key": "finance",
            "name": "Finance team",
            "address": "finance@hollowayarch.example",
            "topics": "Supplier invoices, payments, expenses",
        },
        {
            "key": "it",
            "name": "IT support",
            "address": "it@hollowayarch.example",
            "topics": "Computers, phones, printers, network and accounts",
        },
    ],
    "routing_rules": [
        "Anything about a client project's fees goes to the partner on the project, never to finance.",
    ],
    "now": "Monday 2 November 2026, 09:40",
})

decision = decide({
    "channel": "email",
    "from": "billing@greenleafplants.example",
    "to": "lena.brandt@hollowayarch.example",
    "cc": "",
    "received": "Monday 2 November 2026, 08:55",
    "subject": "Invoice GL-5520 for October plant care",
    "body": "Hello Lena, please find attached our invoice GL-5520 for October's plant care at your Bristol office, 240.00. Could you arrange payment within 30 days? Kind regards, Greenleaf Plants",
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.support.message-triage.action_needed", context={
        "recipient": {
            "name": "Lena Brandt",
            "address": "lena.brandt@hollowayarch.example",
            "role": "Office manager",
            "organisation": "Holloway Architects",
        },
        "message_kinds": {
            "invoice": "A supplier's bill or a question about one",
            "booking": "Meeting room, travel or event bookings",
            "it_problem": "Something broken with computers, phones or the network",
            "newsletter": "Bulk mail and marketing",
        },
        "routing_table": [
            {
                "key": "finance",
                "name": "Finance team",
                "address": "finance@hollowayarch.example",
                "topics": "Supplier invoices, payments, expenses",
            },
            {
                "key": "it",
                "name": "IT support",
                "address": "it@hollowayarch.example",
                "topics": "Computers, phones, printers, network and accounts",
            },
        ],
        "routing_rules": [
            "Anything about a client project's fees goes to the partner on the project, never to finance.",
        ],
        "now": "Monday 2 November 2026, 09:40",
    })

    decision = decide({
        "channel": "email",
        "from": "billing@greenleafplants.example",
        "to": "lena.brandt@hollowayarch.example",
        "cc": "",
        "received": "Monday 2 November 2026, 08:55",
        "subject": "Invoice GL-5520 for October plant care",
        "body": "Hello Lena, please find attached our invoice GL-5520 for October's plant care at your Bristol office, 240.00. Could you arrange payment within 30 days? Kind regards, Greenleaf Plants",
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.support.message-triage.action_needed", "input": {"channel": "email", "from": "billing@greenleafplants.example", "to": "lena.brandt@hollowayarch.example", "cc": "", "received": "Monday 2 November 2026, 08:55", "subject": "Invoice GL-5520 for October plant care", "body": "Hello Lena, please find attached our invoice GL-5520 for October'\''s plant care at your Bristol office, 240.00. Could you arrange payment within 30 days? Kind regards, Greenleaf Plants"}, "context": {"recipient": {"name": "Lena Brandt", "address": "lena.brandt@hollowayarch.example", "role": "Office manager", "organisation": "Holloway Architects"}, "message_kinds": {"invoice": "A supplier'\''s bill or a question about one", "booking": "Meeting room, travel or event bookings", "it_problem": "Something broken with computers, phones or the network", "newsletter": "Bulk mail and marketing"}, "routing_table": [{"key": "finance", "name": "Finance team", "address": "finance@hollowayarch.example", "topics": "Supplier invoices, payments, expenses"}, {"key": "it", "name": "IT support", "address": "it@hollowayarch.example", "topics": "Computers, phones, printers, network and accounts"}], "routing_rules": ["Anything about a client project'\''s fees goes to the partner on the project, never to finance."], "now": "Monday 2 November 2026, 09:40"}}]}'
```

Recorded answer (2026-11-03): `forward` with confidence 0.99.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-message-triage --revision v1.4 --local-dir adapters/message-triage
```

## Files

| File | What it is |
|---|---|
| `adapter_model.safetensors`, `adapter_config.json` | The LoRA weights and their configuration |
| `readout.safetensors` | The adapter's readout over the answer codes |
| `decision_config.json` | Answer codes, temperature, prompt layout and the base it was trained on (the SHA-256 of the base's weights, which the engine checks) |
| `manifest.yaml` | The adapter's manifest: its functions, labels, fields and calibration, as the SDKs read it |
| `test.jsonl` | The held-out test set the numbers above come from |
| `calibration.jsonl` | The calibration set the thresholds come from |
| `LICENSE` | The Apache License 2.0, for the Qwen3.5-0.8B notice below |

## Licence

Adapter licence: **Apache-2.0**

**Qwen3.5-0.8B notice:** these weights were modified from [Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B) by the Jeff project: jeff-base is a fine-tune of Qwen3.5-0.8B, and this adapter was trained on top of it. Qwen3.5-0.8B is Copyright 2026 Alibaba Cloud and licensed under the Apache License, Version 2.0; a copy of that licence is in [`LICENSE`](LICENSE).

Jeff is an independent project, not affiliated with or endorsed by the makers of Qwen.
