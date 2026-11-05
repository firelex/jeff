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

# jeff-adapter-ground

**Passage selection and answer verification.** Selects the passage that best answers a question and checks whether a generated answer is supported by its sources. These are separate functions.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.retrieval.ground`, version 1.4.0; short names: `ground`
- Catalogue page: [jeffhub.ai/catalogue/jeff.retrieval.ground](https://jeffhub.ai/catalogue/jeff.retrieval.ground)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your retrieval pipeline needs to select the best passage or determine that none answers the question.
- You need to verify a generated answer against its sources before displaying it.

## When not to use it

- You need to generate an answer or identify the specific unsupported claim; this adapter returns a selection or verdict.
- You need an assessment based on outside knowledge; this adapter uses only the supplied sources.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `sources` (context) | yes | The source passages the answer was written from, each with a short label such as "Document A: Title" or "Source 1". |
| `answer` (input, fields) | yes | The answer to check. |

### `jeff.retrieval.ground.grounding` (is_supported, choice)

Instructions: Is the answer supported by the sources? Judge only from the sources, not from outside knowledge.

Labels:

  - `supported`: Everything the answer claims is stated in or follows directly from the sources
  - `partly_supported`: Some claims are supported, but at least one is not in the sources
  - `contradicted`: The sources state something that conflicts with the answer
  - `unsupported`: The answer's main claim is not in the sources at all

### `jeff.retrieval.ground.rerank` (classify, choice)

Instructions: Which passage answers the question? If none of them does, choose none.

This function takes its own fields:

| Field | Required | What it holds |
|---|---|---|
| `question` (input, fields) | yes | The question to find an answering passage for; the passages are the options, each as p1, p2, ... with its text. |

Labels:

  - The caller gives the labels (at most 41), each a key and its text.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| passages from public data sets (SQuAD 2.0, HotpotQA, FEVER) and LLM-written documents, held out by source document, with LLM-made changes for the partly supported, contradicted and unsupported cases (mixed) | 4,160 | 96.6% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `grounding` | 0.634 | 98.0% | 98.1% | 720 |
| `rerank` | 0.797 | 98.0% | 91.0% | 720 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Heat pump warranty without a service". Of three warranty clauses, only the one on missed annual services says what happens to the warranty, so the pick is `p2`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["ground"])

decide = client.function("jeff.retrieval.ground.rerank", labels={
    "p1": "4.1 Aster 2 heat pumps installed on or after 1 March 2026 carry a seven-year parts and labour warranty from the date of installation, subject to clause 4.3.",
    "p2": "4.3 The warranty remains valid only if the unit is serviced at least once every 12 months by a Fenwick-approved engineer. If a service is missed, the warranty ends on the date the service was due and cannot be reinstated.",
    "p3": "6.2 To book a service, call 0161 496 0123 or use the customer portal. Services are available Monday to Saturday, 8:00 to 18:00.",
    "none": "None of the passages answers the question.",
})

decision = decide("Does the warranty on my Aster 2 heat pump still apply if I skip the annual service?")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.retrieval.ground.rerank", labels={
        "p1": "4.1 Aster 2 heat pumps installed on or after 1 March 2026 carry a seven-year parts and labour warranty from the date of installation, subject to clause 4.3.",
        "p2": "4.3 The warranty remains valid only if the unit is serviced at least once every 12 months by a Fenwick-approved engineer. If a service is missed, the warranty ends on the date the service was due and cannot be reinstated.",
        "p3": "6.2 To book a service, call 0161 496 0123 or use the customer portal. Services are available Monday to Saturday, 8:00 to 18:00.",
        "none": "None of the passages answers the question.",
    })

    decision = decide("Does the warranty on my Aster 2 heat pump still apply if I skip the annual service?")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.retrieval.ground.rerank", "input": "Does the warranty on my Aster 2 heat pump still apply if I skip the annual service?", "labels": {"p1": "4.1 Aster 2 heat pumps installed on or after 1 March 2026 carry a seven-year parts and labour warranty from the date of installation, subject to clause 4.3.", "p2": "4.3 The warranty remains valid only if the unit is serviced at least once every 12 months by a Fenwick-approved engineer. If a service is missed, the warranty ends on the date the service was due and cannot be reinstated.", "p3": "6.2 To book a service, call 0161 496 0123 or use the customer portal. Services are available Monday to Saturday, 8:00 to 18:00.", "none": "None of the passages answers the question."}}]}'
```

Recorded answer (2026-11-03): `p2` with confidence 1.0.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-ground --revision v1.4 --local-dir adapters/ground
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
