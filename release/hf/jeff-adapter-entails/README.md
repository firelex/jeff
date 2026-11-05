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

# jeff-adapter-entails

**Claim support check.** Checks whether supplied documents, messages or records support a claim. Returns supported, unsupported or unclear.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.reasoning.entails`, version 1.4.0; short names: `oss-entails`, `entails`
- Catalogue page: [jeffhub.ai/catalogue/jeff.reasoning.entails](https://jeffhub.ai/catalogue/jeff.reasoning.entails)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to verify a statement against the evidence available before acting on it.

## When not to use it

- The answer requires facts outside the supplied evidence; this adapter uses only the evidence provided.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `setting` (context) | yes | Who is asking: a sentence naming the team and organisation the evidence comes from. |
| `evidence` (context) | yes | The evidence: a list of documents, messages or records, each with `source` and `text`. |
| `claim` (input, fields) | yes | The claim to check against the evidence, one sentence. |

### `jeff.reasoning.entails.entails` (classify, choice)

Instructions: Does the evidence support the claim? Answer supported when the evidence, read plainly, establishes the claim, including what follows from it by simple arithmetic, by comparing dates or by combining two pieces of evidence. Answer unsupported when the evidence contradicts the claim or does not contain what the claim needs. Answer unclear only when the evidence speaks to the claim but leaves it genuinely open: for example a range or vague date that could go either way, sources that disagree with no sign of which is right, a cause that is only suspected or implied, or a person or record the evidence leaves ambiguous.

Labels:

  - `supported`: Supported: the evidence establishes the claim.
  - `unclear`: Unclear: the evidence bears on the claim but leaves it genuinely open.
  - `unsupported`: Unsupported: the evidence contradicts the claim or does not say what it needs.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| public benchmarks: 500 rows of SNLI and 500 of VitaminC (public) | 1,000 | 93.7% |
| the in-house test (generator) | 2,000 | 97.8% |
| the in-house test in domains never seen in training (generator) | 2,000 | 98.5% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `entails` | 0.518 | 98.5% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Supplier's claim of an early, full delivery". The delivery records show the full order arrived before the agreed date, so the claim is `supported`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["entails"])

decide = client.function("jeff.reasoning.entails.entails", context={
    "setting": "The accounts payable team of a mid-sized furniture maker checks supplier claims before paying.",
    "evidence": [
        {
            "source": "purchase order",
            "text": "Purchase order 4471, issued 3 February 2026 to Calder Timber, for 120 oak boards at 18.50 each, delivery by 28 February 2026.",
        },
        {
            "source": "delivery note",
            "text": "Delivery note for purchase order 4471, signed at the loading bay on 26 February 2026. Received 120 oak boards, no damage noted.",
        },
    ],
})

decision = decide("Calder Timber delivered the full order for purchase order 4471 before the agreed date.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.reasoning.entails.entails", context={
        "setting": "The accounts payable team of a mid-sized furniture maker checks supplier claims before paying.",
        "evidence": [
            {
                "source": "purchase order",
                "text": "Purchase order 4471, issued 3 February 2026 to Calder Timber, for 120 oak boards at 18.50 each, delivery by 28 February 2026.",
            },
            {
                "source": "delivery note",
                "text": "Delivery note for purchase order 4471, signed at the loading bay on 26 February 2026. Received 120 oak boards, no damage noted.",
            },
        ],
    })

    decision = decide("Calder Timber delivered the full order for purchase order 4471 before the agreed date.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.reasoning.entails.entails", "input": "Calder Timber delivered the full order for purchase order 4471 before the agreed date.", "context": {"setting": "The accounts payable team of a mid-sized furniture maker checks supplier claims before paying.", "evidence": [{"source": "purchase order", "text": "Purchase order 4471, issued 3 February 2026 to Calder Timber, for 120 oak boards at 18.50 each, delivery by 28 February 2026."}, {"source": "delivery note", "text": "Delivery note for purchase order 4471, signed at the loading bay on 26 February 2026. Received 120 oak boards, no damage noted."}]}}]}'
```

Recorded answer (2026-11-03): `supported` with confidence 0.615.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-entails --revision v1.4 --local-dir adapters/entails
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
