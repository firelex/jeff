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

# jeff-adapter-doc-type

**Document classification.** Classifies a document using your list of document types and descriptions, or returns other when none fits.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.retrieval.doc-type`, version 1.4.0; short names: `oss-doc-type`, `doc-type`
- Catalogue page: [jeffhub.ai/catalogue/jeff.retrieval.doc-type](https://jeffhub.ai/catalogue/jeff.retrieval.doc-type)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to sort incoming documents into categories you define.

## When not to use it

- You need to extract document fields; this adapter only identifies the document type.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `sorting_desk` (context) | yes | Where the document arrived: a sentence describing the inbox or team that sorts it. |
| `document` (input, fields) | yes | The document's text. |

### `jeff.retrieval.doc-type.doc_type` (classify, choice)

Instructions: Which type of document is this? Choose one type from the list, using the descriptions. Judge by what the document is and does (its purpose, who sends it to whom, what it asks for or records), not by words it mentions in passing: a quote that says an invoice will follow is still a quote. The title may be missing or worded differently from the type names. When two listed types are similar, the descriptions say how to tell them apart. other: the document is none of the listed types, even when a listed type is similar to it.

Labels:

  - The caller gives the labels (at most 11), each a key and its text.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,000 | 96.9% |
| a second in-house test of unseen cases (generator) | 2,000 | 99.1% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `doc_type` | 0.498 | 97.8% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Quotation for mounting brackets". The document offers a price before any order is placed, so its type is `quote`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["doc-type"])

decide = client.function("jeff.retrieval.doc-type.doc_type", context={
    "sorting_desk": "The shared inbox of a home energy installer's office, where suppliers, customers and partners send documents.",
}, labels={
    "invoice": "A bill for goods or services already supplied, asking for payment",
    "quote": "An offer of a price for goods or services, before any order",
    "purchase_order": "The buyer's order to a supplier, placing an order at agreed prices",
    "delivery_note": "A note listing goods delivered, signed on receipt",
    "other": "None of these types",
})

decision = decide("QUOTATION Q-2291\nTo: Fenwick Home Energy, 12 Quay Street\nFor the supply of 40 outdoor mounting brackets (model MB-6) at 32.00 each: 1,280.00 plus VAT.\nThis quotation is valid for 30 days. An invoice will follow on delivery.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.retrieval.doc-type.doc_type", context={
        "sorting_desk": "The shared inbox of a home energy installer's office, where suppliers, customers and partners send documents.",
    }, labels={
        "invoice": "A bill for goods or services already supplied, asking for payment",
        "quote": "An offer of a price for goods or services, before any order",
        "purchase_order": "The buyer's order to a supplier, placing an order at agreed prices",
        "delivery_note": "A note listing goods delivered, signed on receipt",
        "other": "None of these types",
    })

    decision = decide("QUOTATION Q-2291\nTo: Fenwick Home Energy, 12 Quay Street\nFor the supply of 40 outdoor mounting brackets (model MB-6) at 32.00 each: 1,280.00 plus VAT.\nThis quotation is valid for 30 days. An invoice will follow on delivery.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.retrieval.doc-type.doc_type", "input": "QUOTATION Q-2291\nTo: Fenwick Home Energy, 12 Quay Street\nFor the supply of 40 outdoor mounting brackets (model MB-6) at 32.00 each: 1,280.00 plus VAT.\nThis quotation is valid for 30 days. An invoice will follow on delivery.", "context": {"sorting_desk": "The shared inbox of a home energy installer'\''s office, where suppliers, customers and partners send documents."}, "labels": {"invoice": "A bill for goods or services already supplied, asking for payment", "quote": "An offer of a price for goods or services, before any order", "purchase_order": "The buyer'\''s order to a supplier, placing an order at agreed prices", "delivery_note": "A note listing goods delivered, signed on receipt", "other": "None of these types"}}]}'
```

Recorded answer (2026-11-03): `quote` with confidence 0.997.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-doc-type --revision v1.4 --local-dir adapters/doc-type
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
