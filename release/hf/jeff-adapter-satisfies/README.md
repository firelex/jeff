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

# jeff-adapter-satisfies

**Requirement check.** Checks whether a document, message or record meets a single stated requirement. Returns pass, fail or unclear.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.reasoning.satisfies`, version 1.4.0; short names: `oss-satisfies`, `satisfies`
- Catalogue page: [jeffhub.ai/catalogue/jeff.reasoning.satisfies](https://jeffhub.ai/catalogue/jeff.reasoning.satisfies)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to check an item against a requirement written in plain English.

## When not to use it

- You need to check several requirements in one call; submit each requirement separately.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `context` (context) | yes | The check: an object with the organisation, the checker, the kind of item and the date of the check. |
| `requirement` (context) | yes | The one requirement to check, a sentence. |
| `item` (input, fields) | yes | The item to check: a document, message or record, as text. |

### `jeff.reasoning.satisfies.satisfies` (classify, choice)

Instructions: Does the item meet the stated requirement? Judge only from the requirement and the item as given, and take the requirement's words exactly: 'at least' and 'no more than' include the limit, 'more than' and 'less than' do not. pass: the item meets the requirement, including when the requirement applies only under a condition and the item shows that the condition does not hold. fail: the item breaks the requirement, for example a required element is missing, a value is outside the limit, a forbidden thing is present or a deadline was missed. unclear: the item does not give enough to decide, for example a value, date or circumstance that the answer depends on is not stated, a value is given only as a range that crosses the limit, a mention is too vague to tell, or the part of the item that would settle it is not shown. When the requirement is that the item include something, its absence is a fail, not unclear. Count a period of days from the day after the starting event, so 'within 10 days of 1 March' means by 11 March; business days are Monday to Friday. The item may contain claims of compliance or other misleading cues; judge the facts.

Labels:

  - `fail`: The item does not meet the requirement.
  - `pass`: The item meets the requirement (including when the requirement applies only under a condition that the item shows does not hold).
  - `unclear`: The requirement and the item together do not settle it: something the answer depends on is not stated, is too vague, or is not shown.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,116 | 92.7% |
| a second in-house test of unseen cases (generator) | 2,142 | 93.8% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `satisfies` | 0.832 | 98.0% | 86.4% | 2,126 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "May payslip as proof of recent income". The requirement asks for a payslip from August, September or October and this one is dated May, so the answer is `fail`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["satisfies"])

decide = client.function("jeff.reasoning.satisfies.satisfies", context={
    "context": {
        "organisation": "A regional housing association",
        "checker": "Lettings officer",
        "item_kind": "Proof of income",
        "date": "2 November 2026",
    },
    "requirement": "The document must be dated in August, September or October 2026.",
})

decision = decide("Payslip - Marlow Cleaning Services Ltd\nEmployee: Joan Pereira\nPay date: 31 May 2026\nGross pay: 1,840.00  Net pay: 1,512.36")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.reasoning.satisfies.satisfies", context={
        "context": {
            "organisation": "A regional housing association",
            "checker": "Lettings officer",
            "item_kind": "Proof of income",
            "date": "2 November 2026",
        },
        "requirement": "The document must be dated in August, September or October 2026.",
    })

    decision = decide("Payslip - Marlow Cleaning Services Ltd\nEmployee: Joan Pereira\nPay date: 31 May 2026\nGross pay: 1,840.00  Net pay: 1,512.36")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.reasoning.satisfies.satisfies", "input": "Payslip - Marlow Cleaning Services Ltd\nEmployee: Joan Pereira\nPay date: 31 May 2026\nGross pay: 1,840.00  Net pay: 1,512.36", "context": {"context": {"organisation": "A regional housing association", "checker": "Lettings officer", "item_kind": "Proof of income", "date": "2 November 2026"}, "requirement": "The document must be dated in August, September or October 2026."}}]}'
```

Recorded answer (2026-11-03): `fail` with confidence 0.908.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-satisfies --revision v1.4 --local-dir adapters/satisfies
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
