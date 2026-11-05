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

# jeff-adapter-extract-check

**Extracted value validation.** Checks an extracted field and value against the source document. Identifies correct values, incorrect values, wrong-field errors, unsupported values and ambiguity.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.retrieval.extract-check`, version 1.4.0; short names: `oss-extract-check`, `extract-check`
- Catalogue page: [jeffhub.ai/catalogue/jeff.retrieval.extract-check](https://jeffhub.ai/catalogue/jeff.retrieval.extract-check)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to validate fields extracted by another tool or model before using them.

## When not to use it

- You need to extract the value; this adapter only validates a value already supplied.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `document` (context) | yes | The document's text. |
| `extracted` (input, fields) | yes | The extracted value to check: an object with `field` (the field's name) and `value`. |

### `jeff.retrieval.extract-check.check` (classify, choice)

Instructions: Is the extracted value supported by the document? Find where the document states the extracted field and compare the values. A value written in another format still matches (dates written differently, amounts with or without currency or separators, a name in another order or without a company suffix). correct: the document states this value for this field. wrong_value: the document states this field with a different value, and the extracted value is not what it gives for another field. wrong_field: the extracted value appears in the document but for a different field (for example the tax amount given as the total, or the issue date given as the due date). not_in_document: the document does not state this field at all; do not work it out from other values. ambiguous: the document states this field more than once with different values, and the extracted value matches one of them.

Labels:

  - `ambiguous`: Ambiguous: the document states this field more than once with different values, and the extracted value matches one of them.
  - `correct`: Correct: the document states this value for this field (in this or another format).
  - `not_in_document`: Not in the document: the document does not state this field at all.
  - `wrong_field`: Wrong field: the extracted value appears in the document, but for a different field (for example the tax amount given as the total).
  - `wrong_value`: Wrong value: the document states this field, but with a different value, and the extracted value is not what the document gives for another field.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,000 | 99.6% |
| a second in-house test of unseen cases (generator) | 2,000 | 98.8% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `check` | 0.483 | 99.1% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Invoice total checked against the invoice". The extracted figure is in the document but is the VAT, not the total, so the answer is `wrong_field`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["extract-check"])

decide = client.function("jeff.retrieval.extract-check.check", context={
    "document": "INVOICE 7781\nIssued: 14 October 2026    Due: 13 November 2026\nFenwick Home Energy, 12 Quay Street\nAster 2 heat pump, supplied and installed: 8,400.00\nVAT (5%): 420.00\nTotal due: 8,820.00",
})

decision = decide({
    "field": "total",
    "value": "420.00",
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.retrieval.extract-check.check", context={
        "document": "INVOICE 7781\nIssued: 14 October 2026    Due: 13 November 2026\nFenwick Home Energy, 12 Quay Street\nAster 2 heat pump, supplied and installed: 8,400.00\nVAT (5%): 420.00\nTotal due: 8,820.00",
    })

    decision = decide({
        "field": "total",
        "value": "420.00",
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.retrieval.extract-check.check", "input": {"field": "total", "value": "420.00"}, "context": {"document": "INVOICE 7781\nIssued: 14 October 2026    Due: 13 November 2026\nFenwick Home Energy, 12 Quay Street\nAster 2 heat pump, supplied and installed: 8,400.00\nVAT (5%): 420.00\nTotal due: 8,820.00"}}]}'
```

Recorded answer (2026-11-03): `wrong_field` with confidence 0.991.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-extract-check --revision v1.4 --local-dir adapters/extract-check
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
