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

# jeff-adapter-contradicts

**Contradiction check.** Checks whether two statements or records conflict. Returns yes, no or unclear when the information is ambiguous.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.reasoning.contradicts`, version 1.4.0; short names: `oss-contradicts`, `contradicts`
- Catalogue page: [jeffhub.ai/catalogue/jeff.reasoning.contradicts](https://jeffhub.ai/catalogue/jeff.reasoning.contradicts)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to identify disagreements between documents, messages or records.

## When not to use it

- You need to check whether evidence supports a claim; use Claim support check.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `setting` (context) | yes | Who is asking: a sentence naming the team and organisation the statements come from. |
| `statement_a` (context) | yes | The first statement or record: an object with `source` (for example record entry, email, report) and `text`. |
| `statement_b` (input, fields) | yes | The second statement or record, in the same form as statement_a. |

### `jeff.reasoning.contradicts.contradicts` (classify, choice)

Instructions: Do these two statements or records conflict? Answer yes when they cannot both be true about the same thing at the same time, whether the conflict is stated outright or follows from what they say. Answer no when both can be true, including when they are about different things or different times, or one only adds detail. Answer unclear only when they conflict under one reasonable reading and not under another, and neither settles which: for example when it is not clear whether they refer to the same item, when a date or quantity can be read two ways, or when it is not clear who holds a role.

Labels:

  - `no`: No: they can both be true.
  - `unclear`: Unclear: whether they conflict depends on something neither one settles.
  - `yes`: Yes: they cannot both be true.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| public benchmarks: 500 rows of SNLI and 500 of VitaminC (public) | 1,000 | 94.5% |
| the in-house test (generator) | 1,931 | 96.4% |
| the in-house test in domains never seen in training (generator) | 1,939 | 96.5% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `contradicts` | 0.662 | 98.0% | 97.4% | 1,933 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Discharge letter against the pharmacy record". The hospital stopped metformin at discharge, but the pharmacy's repeat record still issues it, so the answer is `yes`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["contradicts"])

decide = client.function("jeff.reasoning.contradicts.contradicts", context={
    "setting": "The pharmacy team of a district hospital checks each patient's medicines when they are discharged.",
    "statement_a": {
        "source": "discharge letter, ward 6, 2 November 2026",
        "text": "Medicines on discharge: ramipril 5 mg once daily; atorvastatin 40 mg at night; bisoprolol 2.5 mg once daily. Metformin stopped on admission because of reduced kidney function; do not restart until reviewed by the GP.",
    },
})

decision = decide({
    "source": "community pharmacy repeat prescription record, printed 3 November 2026",
    "text": "Repeat items for this patient: ramipril 5 mg once daily; atorvastatin 40 mg at night; metformin 500 mg twice daily (next issue due 6 November 2026).",
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.reasoning.contradicts.contradicts", context={
        "setting": "The pharmacy team of a district hospital checks each patient's medicines when they are discharged.",
        "statement_a": {
            "source": "discharge letter, ward 6, 2 November 2026",
            "text": "Medicines on discharge: ramipril 5 mg once daily; atorvastatin 40 mg at night; bisoprolol 2.5 mg once daily. Metformin stopped on admission because of reduced kidney function; do not restart until reviewed by the GP.",
        },
    })

    decision = decide({
        "source": "community pharmacy repeat prescription record, printed 3 November 2026",
        "text": "Repeat items for this patient: ramipril 5 mg once daily; atorvastatin 40 mg at night; metformin 500 mg twice daily (next issue due 6 November 2026).",
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.reasoning.contradicts.contradicts", "input": {"source": "community pharmacy repeat prescription record, printed 3 November 2026", "text": "Repeat items for this patient: ramipril 5 mg once daily; atorvastatin 40 mg at night; metformin 500 mg twice daily (next issue due 6 November 2026)."}, "context": {"setting": "The pharmacy team of a district hospital checks each patient'\''s medicines when they are discharged.", "statement_a": {"source": "discharge letter, ward 6, 2 November 2026", "text": "Medicines on discharge: ramipril 5 mg once daily; atorvastatin 40 mg at night; bisoprolol 2.5 mg once daily. Metformin stopped on admission because of reduced kidney function; do not restart until reviewed by the GP."}}}]}'
```

Recorded answer (2026-11-03): `yes` with confidence 0.975.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-contradicts --revision v1.4 --local-dir adapters/contradicts
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
