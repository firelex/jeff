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

# jeff-adapter-triage

**Support ticket triage.** Routes a support ticket or email to a team and assesses urgency, sentiment and the need for human review.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.support.triage`, version 1.4.0; short names: `triage`
- Catalogue page: [jeffhub.ai/catalogue/jeff.support.triage](https://jeffhub.ai/catalogue/jeff.support.triage)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Each ticket, email or chat message needs a team assignment, urgency rating and human-review decision.
- You want to use your own team list, with a short description of each team.

## When not to use it

- You need to write a reply; this adapter returns decisions without generating text.
- The decision depends on facts outside the message, such as order history; include them in the request or handle them in code.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `company` (context) | yes | One sentence about the organisation the message is sent to. |
| `channel` (context) | yes | Where the message came from, for example email, web form, chat, app review, social media reply or phone transcript. |
| `message` (input, fields) | yes | The ticket or email as received, including subject line, quoted thread and signature. |

### `jeff.support.triage.route` (route, choice)

Instructions: Which team should handle this message? If it covers several issues, choose the team for the most important one.

Labels:

  - The caller gives the labels (at most 35), each a key and its text.
  - Always offered first: `other`: Not for any of these teams

### `jeff.support.triage.urgency` (rate, score)

Instructions: How urgently does this need a response?

Labels:

  - 0: No action needed
  - 1: Can wait a few days
  - 2: Handle within a day
  - 3: Handle within hours
  - 4: Handle immediately

### `jeff.support.triage.sentiment` (rate, score)

Instructions: How does the customer feel?

Labels:

  - 0: Very negative, angry or distressed
  - 1: Negative
  - 2: Neutral
  - 3: Positive
  - 4: Very positive

### `jeff.support.triage.needs_human` (requires_escalation, noul)

Instructions: Does this need a person to act on it now, rather than an automatic reply? Answer yes for complaints that could escalate, legal or safety issues, or requests an automatic system cannot resolve.

Labels:

  - A yes/no question: the answer is `true` or `false`.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 7,256 | 91.2% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `route` | 0.435 | 95.1% | 100.0% | 367 |
| `urgency` | 0.787 | 95.0% | 59.9% | 367 |
| `sentiment` | 0.687 | 95.0% | 88.0% | 367 |
| `needs_human` | 0.544 | 98.9% | 100.0% | 367 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Wrong jacket size in the parcel". A wrong item to exchange is a matter for the `orders` team.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["triage"])

decide = client.function("jeff.support.triage.route", context={
    "company": "An online shop selling outdoor clothing and camping gear across Europe.",
    "channel": "email",
}, labels={
    "orders": "Orders, deliveries, returns and exchanges",
    "payments": "Payments, refunds and invoices",
    "product": "Product questions, sizing advice and stock",
    "account": "Customer accounts, passwords and newsletters",
})

decision = decide("Subject: Wrong item in my parcel\nHi, my order 88213 arrived today but the parcel had a large Alpine shell jacket instead of the medium I ordered. Please exchange it and send the medium before Saturday. Thanks, Marta")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.support.triage.route", context={
        "company": "An online shop selling outdoor clothing and camping gear across Europe.",
        "channel": "email",
    }, labels={
        "orders": "Orders, deliveries, returns and exchanges",
        "payments": "Payments, refunds and invoices",
        "product": "Product questions, sizing advice and stock",
        "account": "Customer accounts, passwords and newsletters",
    })

    decision = decide("Subject: Wrong item in my parcel\nHi, my order 88213 arrived today but the parcel had a large Alpine shell jacket instead of the medium I ordered. Please exchange it and send the medium before Saturday. Thanks, Marta")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.support.triage.route", "input": "Subject: Wrong item in my parcel\nHi, my order 88213 arrived today but the parcel had a large Alpine shell jacket instead of the medium I ordered. Please exchange it and send the medium before Saturday. Thanks, Marta", "context": {"company": "An online shop selling outdoor clothing and camping gear across Europe.", "channel": "email"}, "labels": {"orders": "Orders, deliveries, returns and exchanges", "payments": "Payments, refunds and invoices", "product": "Product questions, sizing advice and stock", "account": "Customer accounts, passwords and newsletters"}}]}'
```

Recorded answer (2026-11-03): `orders` with confidence 1.0.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-triage --revision v1.4 --local-dir adapters/triage
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
