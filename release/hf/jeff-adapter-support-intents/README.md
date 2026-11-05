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

# jeff-adapter-support-intents

**Customer request classification.** Identifies the request in a customer or assistant-user message, choosing from a fixed list such as cancel_order or track_refund.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.support.support-intents`, version 1.4.0; short names: `support-intents`
- Catalogue page: [jeffhub.ai/catalogue/jeff.support.support-intents](https://jeffhub.ai/catalogue/jeff.support.support-intents)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to classify requests in an online shop chat or voice assistant.
- Messages are short and in English.

## When not to use it

- You need to identify every request in a message containing several; this adapter selects one.
- Your inputs are long emails or tickets; use Support ticket triage.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `service` (context) | yes | One short phrase about the service the message is sent to. Training used three, one per data set, for example "Customer support chat of an online shop". |
| `message` (input, fields) | yes | The customer's or user's message as received. |

### `jeff.support.support-intents.intent` (classify, choice)

Instructions: What does the customer want? Choose the request that best matches what the customer is asking for in their message.

Labels:

  - The caller gives the labels (at most 64), each a key and its text.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| public intent data sets: Bitext customer support, HWU64 and SNIPS (public) | 5,577 | 96.4% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `intent` | 0.290 | 96.5% | 100.0% | 999 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Cancel boots before they ship". The boots have not shipped yet, so the customer's request is `cancel_order`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["support-intents"])

decide = client.function("jeff.support.support-intents.intent", context={
    "service": "Customer support chat of an online shop",
}, labels={
    "cancel_order": "Cancel an order that has not been delivered yet",
    "track_order": "Find out where an order is",
    "change_address": "Change the delivery address of an order",
    "return_item": "Return an item that was delivered",
    "track_refund": "Find out the status of a refund",
})

decision = decide("hi, i changed my mind about the boots i ordered yesterday, can i still stop the order before it ships?")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.support.support-intents.intent", context={
        "service": "Customer support chat of an online shop",
    }, labels={
        "cancel_order": "Cancel an order that has not been delivered yet",
        "track_order": "Find out where an order is",
        "change_address": "Change the delivery address of an order",
        "return_item": "Return an item that was delivered",
        "track_refund": "Find out the status of a refund",
    })

    decision = decide("hi, i changed my mind about the boots i ordered yesterday, can i still stop the order before it ships?")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.support.support-intents.intent", "input": "hi, i changed my mind about the boots i ordered yesterday, can i still stop the order before it ships?", "context": {"service": "Customer support chat of an online shop"}, "labels": {"cancel_order": "Cancel an order that has not been delivered yet", "track_order": "Find out where an order is", "change_address": "Change the delivery address of an order", "return_item": "Return an item that was delivered", "track_refund": "Find out the status of a refund"}}]}'
```

Recorded answer (2026-11-03): `cancel_order` with confidence 0.999.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-support-intents --revision v1.4 --local-dir adapters/support-intents
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
