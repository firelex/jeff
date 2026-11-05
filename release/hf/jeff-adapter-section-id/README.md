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

# jeff-adapter-section-id

**Document section classification.** Identifies the type of document section a passage belongs to, using your list of section types and descriptions. Returns none when no type fits.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.retrieval.section-id`, version 1.4.0; short names: `oss-section-id`, `section-id`
- Catalogue page: [jeffhub.ai/catalogue/jeff.retrieval.section-id](https://jeffhub.ai/catalogue/jeff.retrieval.section-id)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You have split documents into passages and need to classify each passage by section type.

## When not to use it

- Your documents already have reliable headings that code can read directly.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `document` (context) | yes | What the document is: its kind and parties, one line. |
| `passage` (input, fields) | yes | The passage's text. |

### `jeff.retrieval.section-id.section` (classify, choice)

Instructions: Which section of the document is this passage from? Choose the section kind whose main purpose the passage serves, using the descriptions. Judge by its content, not by topics it mentions in passing: a termination clause that mentions unpaid fees is still about termination. Headings may be missing, numbered or worded differently from the section names. none: the passage serves none of the listed section kinds (for example a kind of section that is not on the list), even when a listed kind is similar.

Labels:

  - The caller gives the labels (at most 11), each a key and its text.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| CUAD, a public benchmark of real contract paragraphs (public) | 1,168 | 90.9% |
| the in-house test (generator) | 2,000 | 99.8% |
| the in-house test in domains never seen in training (generator) | 2,000 | 99.6% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `section` | 0.533 | 99.8% | 100.0% | 2,000 |

Caveats:

- The CUAD rows were kept only where an LLM agreed with the label, which removes the hardest real passages.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Ending the agreement on notice". The passage is about ending the agreement, so it comes from the `termination` section.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["section-id"])

decide = client.function("jeff.retrieval.section-id.section", context={
    "document": "An installation services agreement between a home energy installer and a housing developer.",
}, labels={
    "scope": "What work the installer will do, and where",
    "payment": "Prices, invoicing and when payment is due",
    "termination": "How and when either party may end the agreement",
    "liability": "Who is responsible for loss or damage, and the limits",
    "none": "None of these sections",
})

decision = decide("Either party may end this agreement on 60 days' written notice. If the developer ends it, any invoices then unpaid fall due at once.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.retrieval.section-id.section", context={
        "document": "An installation services agreement between a home energy installer and a housing developer.",
    }, labels={
        "scope": "What work the installer will do, and where",
        "payment": "Prices, invoicing and when payment is due",
        "termination": "How and when either party may end the agreement",
        "liability": "Who is responsible for loss or damage, and the limits",
        "none": "None of these sections",
    })

    decision = decide("Either party may end this agreement on 60 days' written notice. If the developer ends it, any invoices then unpaid fall due at once.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.retrieval.section-id.section", "input": "Either party may end this agreement on 60 days'\'' written notice. If the developer ends it, any invoices then unpaid fall due at once.", "context": {"document": "An installation services agreement between a home energy installer and a housing developer."}, "labels": {"scope": "What work the installer will do, and where", "payment": "Prices, invoicing and when payment is due", "termination": "How and when either party may end the agreement", "liability": "Who is responsible for loss or damage, and the limits", "none": "None of these sections"}}]}'
```

Recorded answer (2026-11-03): `termination` with confidence 1.0.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-section-id --revision v1.4 --local-dir adapters/section-id
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
