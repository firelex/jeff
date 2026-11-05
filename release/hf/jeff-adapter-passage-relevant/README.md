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
- retrieval
---

# jeff-adapter-passage-relevant

**Passage relevance.** Checks whether a passage answers a question about the specified subject and time period. Returns yes, partly or no.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.retrieval.passage-relevant`, version 1.4.0; short names: `oss-passage-relevant-v2`, `passage-relevant`
- Catalogue page: [jeffhub.ai/catalogue/jeff.retrieval.passage-relevant](https://jeffhub.ai/catalogue/jeff.retrieval.passage-relevant)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Search results may cover the right topic but the wrong company, product or year.

## When not to use it

- You need to compare several passages; use Passage selection and answer verification.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `source` (context) | yes | Where the passage comes from: the collection's name and a description of what it holds. |
| `question` (context) | yes | The question to answer. |
| `passage` (input, fields) | yes | The retrieved passage's text. |

### `jeff.retrieval.passage-relevant.relevant` (classify, choice)

Instructions: Does the passage answer the question? Check who or what the passage is about (the named person, organisation, site, product or case), the period or date it covers, and every detail the question asks for. yes: the passage states every asked detail for the right subject and period (other content around it does not matter). partly: the passage is about the right subject and period and states at least one of the asked details but not all of them; for a question about several periods, it covers only some of them. no: the passage is about a different subject (even one with a similar name) or a different period, states none of the asked details (only related facts), or only says that the asked detail is not known or not decided yet.

Labels:

  - `no`: No: the passage is about a different subject or period, states none of the asked details (only related facts), or only says the detail is not known yet.
  - `partly`: Partly: the passage is about the right subject and period and states at least one asked detail, but not all of them (or covers only some of the asked periods).
  - `yes`: Yes: the passage states every detail the question asks for, for the right subject and period.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| SQuAD 2.0, a public question-answering benchmark with human labels (public) | 2,000 | 83.9% |
| the in-house test (generator) | 2,000 | 99.5% |
| the in-house test in domains never seen in training (generator) | 2,000 | 99.1% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `relevant` | 0.521 | 99.5% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Older model's warranty terms". The passage is about the Aster 1 while the question asks about the Aster 2, so the answer is `no`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["passage-relevant"])

decide = client.function("jeff.retrieval.passage-relevant.relevant", context={
    "source": "warranty_terms: the warranty terms for each heat pump and boiler a home energy installer sells",
    "question": "How long is the warranty on an Aster 2 heat pump installed after March 2026?",
})

decision = decide("Aster 1 heat pumps carry a five-year parts and labour warranty from the date of installation, provided the unit is serviced every year by an approved engineer.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.retrieval.passage-relevant.relevant", context={
        "source": "warranty_terms: the warranty terms for each heat pump and boiler a home energy installer sells",
        "question": "How long is the warranty on an Aster 2 heat pump installed after March 2026?",
    })

    decision = decide("Aster 1 heat pumps carry a five-year parts and labour warranty from the date of installation, provided the unit is serviced every year by an approved engineer.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.retrieval.passage-relevant.relevant", "input": "Aster 1 heat pumps carry a five-year parts and labour warranty from the date of installation, provided the unit is serviced every year by an approved engineer.", "context": {"source": "warranty_terms: the warranty terms for each heat pump and boiler a home energy installer sells", "question": "How long is the warranty on an Aster 2 heat pump installed after March 2026?"}}]}'
```

Recorded answer (2026-11-03): `no` with confidence 0.993.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-passage-relevant --revision v1.4 --local-dir adapters/passage-relevant
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
