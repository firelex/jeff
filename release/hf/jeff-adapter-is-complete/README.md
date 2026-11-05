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
- reasoning
---

# jeff-adapter-is-complete

**Document completeness check.** Checks a set of documents against a checklist. Returns complete, incomplete or unclear, accounting for missing, expired, unsigned or incorrectly attributed documents.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.reasoning.is-complete`, version 1.4.0; short names: `oss-is-complete`, `is-complete`
- Catalogue page: [jeffhub.ai/catalogue/jeff.reasoning.is-complete](https://jeffhub.ai/catalogue/jeff.reasoning.is-complete)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to assess whether a document submission meets a checklist.

## When not to use it

- You need an itemised list of missing documents; this adapter assesses the submission as a whole.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `context` (context) | yes | The review: an object with the organisation, the reviewer, the kind of pack, its purpose and the date of the review. |
| `checklist` (context) | yes | The checklist: a list of required documents and conditions (some only for some cases). |
| `submission` (input, fields) | yes | The pack received: an object with the case (name, reference) and the documents received, each described (kind, date, pages, signatures). |

### `jeff.reasoning.is-complete.is_complete` (classify, choice)

Instructions: Is the pack complete against the checklist? Go through every checklist line that applies to this case. A required document counts only if it is the kind of document listed (not a similar one), is about the subject of the case, has all its pages, is signed where the checklist asks for a signature, has not expired on the date of the review, and was issued within the period the checklist allows. A conditional line applies only when its circumstance holds for the case. complete: every applicable line is met (extra documents do not matter). incomplete: at least one applicable line is not met. unclear: no line is clearly unmet, but at least one cannot be decided from what is shown, for example a needed date, signature or page count is not visible, a document's kind cannot be told, or the case details do not say whether a conditional line's circumstance holds while its document is absent. Cover notes and labels may be misleading; judge the documents themselves.

Labels:

  - `complete`: Every document the checklist requires for this case is in the pack and acceptable.
  - `incomplete`: At least one required document is missing or not acceptable (wrong kind, about someone or something else, expired or too old, missing pages, or unsigned).
  - `unclear`: The pack and the case details do not settle it: something the answer depends on cannot be seen or is not stated.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 1,974 | 96.8% |
| a second in-house test of unseen cases (generator) | 1,932 | 95.7% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `is_complete` | 0.789 | 98.0% | 96.7% | 1,941 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Bakery's business loan pack". The bakery has two directors but the pack holds photo identification for only one, so it is `incomplete`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["is-complete"])

decide = client.function("jeff.reasoning.is-complete.is_complete", context={
    "context": {
        "organisation": "A small business lender",
        "reviewer": "Loan operations analyst",
        "pack_kind": "Business loan application",
        "purpose": "Check the application pack is complete before credit review",
        "review_date": "2 November 2026",
    },
    "checklist": [
        "Signed loan application form (all pages)",
        "Business bank statements for the last three months",
        "Photo identification of every director",
        "Latest annual accounts (only for businesses trading more than two years)",
    ],
})

decision = decide({
    "case": {
        "name": "Ferngrove Bakery Ltd",
        "reference": "APP-20931",
        "trading_since": "March 2025",
        "directors": [
            "Priya Natarajan",
            "Tom Okafor",
        ],
    },
    "documents_received": [
        {
            "kind": "Loan application form",
            "date": "28 October 2026",
            "pages": "4 of 4",
            "signatures": "signed by Priya Natarajan",
        },
        {
            "kind": "Business bank statements",
            "date": "July, August and September 2026",
            "pages": "complete",
        },
        {
            "kind": "Passport",
            "date": "valid until 2031",
            "about": "Priya Natarajan",
        },
    ],
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.reasoning.is-complete.is_complete", context={
        "context": {
            "organisation": "A small business lender",
            "reviewer": "Loan operations analyst",
            "pack_kind": "Business loan application",
            "purpose": "Check the application pack is complete before credit review",
            "review_date": "2 November 2026",
        },
        "checklist": [
            "Signed loan application form (all pages)",
            "Business bank statements for the last three months",
            "Photo identification of every director",
            "Latest annual accounts (only for businesses trading more than two years)",
        ],
    })

    decision = decide({
        "case": {
            "name": "Ferngrove Bakery Ltd",
            "reference": "APP-20931",
            "trading_since": "March 2025",
            "directors": [
                "Priya Natarajan",
                "Tom Okafor",
            ],
        },
        "documents_received": [
            {
                "kind": "Loan application form",
                "date": "28 October 2026",
                "pages": "4 of 4",
                "signatures": "signed by Priya Natarajan",
            },
            {
                "kind": "Business bank statements",
                "date": "July, August and September 2026",
                "pages": "complete",
            },
            {
                "kind": "Passport",
                "date": "valid until 2031",
                "about": "Priya Natarajan",
            },
        ],
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.reasoning.is-complete.is_complete", "input": {"case": {"name": "Ferngrove Bakery Ltd", "reference": "APP-20931", "trading_since": "March 2025", "directors": ["Priya Natarajan", "Tom Okafor"]}, "documents_received": [{"kind": "Loan application form", "date": "28 October 2026", "pages": "4 of 4", "signatures": "signed by Priya Natarajan"}, {"kind": "Business bank statements", "date": "July, August and September 2026", "pages": "complete"}, {"kind": "Passport", "date": "valid until 2031", "about": "Priya Natarajan"}]}, "context": {"context": {"organisation": "A small business lender", "reviewer": "Loan operations analyst", "pack_kind": "Business loan application", "purpose": "Check the application pack is complete before credit review", "review_date": "2 November 2026"}, "checklist": ["Signed loan application form (all pages)", "Business bank statements for the last three months", "Photo identification of every director", "Latest annual accounts (only for businesses trading more than two years)"]}}]}'
```

Recorded answer (2026-11-03): `incomplete` with confidence 0.965.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-is-complete --revision v1.4 --local-dir adapters/is-complete
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
