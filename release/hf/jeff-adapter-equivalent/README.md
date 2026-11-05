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

# jeff-adapter-equivalent

**Same meaning check.** Checks whether two items have the same meaning within a defined scope, allowing for differences in wording, units and format. Returns yes, no or unclear.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.reasoning.equivalent`, version 1.4.0; short names: `oss-equivalent`, `equivalent`
- Catalogue page: [jeffhub.ai/catalogue/jeff.reasoning.equivalent](https://jeffhub.ai/catalogue/jeff.reasoning.equivalent)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to compare records or statements expressed differently, such as names, amounts or dates in different formats.

## When not to use it

- You need to determine which item is better or more complete; this adapter only checks equivalence.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `setting` (context) | yes | Who is asking: a sentence naming the team and organisation. |
| `scope` (context) | yes | What counts when comparing: a sentence saying which details matter (for example every fact, number, date and name). |
| `item_a` (context) | yes | The first item: a text (a rule, sentence, record or value). |
| `item_b` (input, fields) | yes | The second item, to compare with item_a within the scope. |

### `jeff.reasoning.equivalent.equivalent` (classify, choice)

Instructions: Do these two items mean the same thing, within the stated scope? Answer yes when every detail that matters under the scope is the same, even if the wording, the order of things whose order does not matter, the units or the formats differ. Answer no when at least one detail that matters differs, or is in one item but not the other. Answer unclear only when sameness depends on something neither item settles, such as a date or a time of day that can be read two ways, or a number given without the unit or sign that decides what it means.

Labels:

  - `no`: No: they differ in a detail that matters.
  - `unclear`: Unclear: sameness depends on something neither item settles.
  - `yes`: Yes: they mean the same within the scope.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| PAWS, a public paraphrase benchmark (public) | 1,000 | 93.4% |
| the in-house test (generator) | 2,000 | 98.5% |
| the in-house test in domains never seen in training (generator) | 1,931 | 97.8% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `equivalent` | 0.499 | 98.4% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Two listings of the same paperback". Every fact the scope names agrees between the two book listings, so the answer is `yes`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["equivalent"])

decide = client.function("jeff.reasoning.equivalent.equivalent", context={
    "setting": "The data team of an online bookshop compares supplier listings before merging duplicate products.",
    "scope": "Every fact that identifies the edition counts (title, author, format, page count and year); wording and punctuation do not.",
    "item_a": "The Lighthouse Keeper's Daughter by Ana Rivas, paperback, 312 pages, published 2021.",
})

decision = decide("Ana Rivas, The Lighthouse Keeper's Daughter (paperback edition, 2021), 312 pp.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.reasoning.equivalent.equivalent", context={
        "setting": "The data team of an online bookshop compares supplier listings before merging duplicate products.",
        "scope": "Every fact that identifies the edition counts (title, author, format, page count and year); wording and punctuation do not.",
        "item_a": "The Lighthouse Keeper's Daughter by Ana Rivas, paperback, 312 pages, published 2021.",
    })

    decision = decide("Ana Rivas, The Lighthouse Keeper's Daughter (paperback edition, 2021), 312 pp.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.reasoning.equivalent.equivalent", "input": "Ana Rivas, The Lighthouse Keeper'\''s Daughter (paperback edition, 2021), 312 pp.", "context": {"setting": "The data team of an online bookshop compares supplier listings before merging duplicate products.", "scope": "Every fact that identifies the edition counts (title, author, format, page count and year); wording and punctuation do not.", "item_a": "The Lighthouse Keeper'\''s Daughter by Ana Rivas, paperback, 312 pages, published 2021."}}]}'
```

Recorded answer (2026-11-03): `yes` with confidence 0.971.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-equivalent --revision v1.4 --local-dir adapters/equivalent
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
