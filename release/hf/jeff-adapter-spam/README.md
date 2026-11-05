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
- safety
---

# jeff-adapter-spam

**Spam and phishing detection.** Detects spam or phishing in text messages and emails. A separate function classifies each message as legitimate, spam or phishing.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.safety.spam`, version 1.4.0; short names: `spam`
- Catalogue page: [jeffhub.ai/catalogue/jeff.safety.spam](https://jeffhub.ai/catalogue/jeff.safety.spam)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need spam and phishing probabilities for incoming text messages or emails.
- You need to distinguish advertising spam from scams seeking personal details, passwords or money.

## When not to use it

- You need to inspect email headers, links or attachments; this adapter reads only the subject and readable body.
- You need a definitive blocking decision without human review; use the prediction as one input to that decision.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `channel` (context) | yes | Where the message came from: "sms" or "email", as in training. |
| `message` (input, fields) | yes | The text of the message as received. For an email, its subject and readable body (no other headers, no attachments). |

### `jeff.safety.spam.is_spam` (detect, noul)

Instructions: Is this message spam or phishing? Answer yes if it is unwanted bulk or advertising, or a scam trying to get personal details or money; answer no if it is a normal message.

Labels:

  - `false`: The message is a normal message: not unwanted bulk or advertising, and not a scam.
  - `true`: The message is spam (unwanted bulk or advertising) or phishing (a scam trying to get personal details, passwords or money).

### `jeff.safety.spam.message_type` (classify, choice)

Instructions: What kind of message is this: a legitimate message, spam (unwanted bulk or advertising), or phishing (a scam trying to get personal details, passwords or money)?

Labels:

  - `legitimate`: A normal message from a person or a genuine organisation, not spam and not phishing.
  - `phishing`: A scam message that tries to trick the reader into giving personal details, passwords or money, often by pretending to be a bank, company or authority and asking them to click a link or call a number.
  - `spam`: Unwanted bulk or advertising message (for example prizes, offers, premium-rate services), but not trying to steal personal or account details.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| real messages from public spam and phishing collections (SMS and email) (public) | 3,897 | 98.4% |
| the test set of the earlier version (public) | 3,603 | 98.3% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `is_spam` | 0.516 | 99.2% | 100.0% | 1,113 |
| `message_type` | 0.426 | 98.1% | 100.0% | 832 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Parcel redelivery fee text". A message asking for a small fee through an unknown link is after money or details, so it is `phishing`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["spam"])

decide = client.function("jeff.safety.spam.message_type", context={
    "channel": "sms",
})

decision = decide("Your parcel could not be delivered. Pay the 1.99 redelivery fee within 24 hours here: http://parcel-redeliver.example/pay")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.safety.spam.message_type", context={
        "channel": "sms",
    })

    decision = decide("Your parcel could not be delivered. Pay the 1.99 redelivery fee within 24 hours here: http://parcel-redeliver.example/pay")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.safety.spam.message_type", "input": "Your parcel could not be delivered. Pay the 1.99 redelivery fee within 24 hours here: http://parcel-redeliver.example/pay", "context": {"channel": "sms"}}]}'
```

Recorded answer (2026-11-03): `phishing` with confidence 0.911.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-spam --revision v1.4 --local-dir adapters/spam
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
