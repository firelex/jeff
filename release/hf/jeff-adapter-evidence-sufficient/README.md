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

# jeff-adapter-evidence-sufficient

**Evidence sufficiency check.** Checks whether retrieved passages provide enough evidence to answer a question. Returns sufficient, insufficient or conflicting.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.retrieval.evidence-sufficient`, version 1.4.0; short names: `oss-evidence-sufficient-v2`, `evidence-sufficient`
- Catalogue page: [jeffhub.ai/catalogue/jeff.retrieval.evidence-sufficient](https://jeffhub.ai/catalogue/jeff.retrieval.evidence-sufficient)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your retrieval system needs to decide whether to answer, search further or flag conflicting evidence.

## When not to use it

- You need to generate an answer; this adapter only assesses the evidence.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `question` (context) | yes | The question to answer. |
| `passages` (input, fields) | yes | The retrieved passages, a list of texts. |

### `jeff.retrieval.evidence-sufficient.sufficient` (classify, choice)

Instructions: Can the question be answered from these passages alone? Use only what the passages state about the right subject and period: facts about a different subject (even one with a similar name) or a different period do not count, and a passage saying a detail is not known yet does not give it. The answer may need two or more passages together, for example one passage names the related site, supplier or person and another gives its figure. sufficient: together the passages state every asked detail for the right subject and period, and they do not disagree about it. insufficient: at least one asked detail, or a link needed to reach it, is not stated for the right subject and period. conflicting: two passages give different values for the same asked detail for the same subject and period, and nothing in the passages says which one is right.

Labels:

  - `conflicting`: Conflicting: two passages give different values for the same asked detail for the same subject and period, and nothing says which is right.
  - `insufficient`: Insufficient: at least one asked detail, or a link needed to reach it, is not stated for the right subject and period.
  - `sufficient`: Sufficient: together the passages state every asked detail for the right subject and period, and they do not disagree about it.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| HotpotQA, a public question-answering benchmark with human labels (public) | 2,000 | 93.0% |
| the in-house test (generator) | 2,000 | 98.4% |
| the in-house test in domains never seen in training (generator) | 2,000 | 98.5% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `sufficient` | 0.499 | 99.4% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Three warranty passages". One of the passages holds the answer, so the evidence is `sufficient` and no further search is needed.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["evidence-sufficient"])

decide = client.function("jeff.retrieval.evidence-sufficient.sufficient", context={
    "question": "How long is the warranty on an Aster 2 heat pump installed on or after 1 March 2026?",
})

decision = decide([
    "Aster 1 heat pumps carry a five-year parts and labour warranty from the date of installation, provided the unit is serviced every year by an approved engineer.",
    "For Aster 2 units installed on or after 1 March 2026 the warranty is extended to seven years, parts and labour, with the same annual service condition.",
    "Our boilers come with a two-year warranty as standard.",
])

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.retrieval.evidence-sufficient.sufficient", context={
        "question": "How long is the warranty on an Aster 2 heat pump installed on or after 1 March 2026?",
    })

    decision = decide([
        "Aster 1 heat pumps carry a five-year parts and labour warranty from the date of installation, provided the unit is serviced every year by an approved engineer.",
        "For Aster 2 units installed on or after 1 March 2026 the warranty is extended to seven years, parts and labour, with the same annual service condition.",
        "Our boilers come with a two-year warranty as standard.",
    ])

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.retrieval.evidence-sufficient.sufficient", "input": ["Aster 1 heat pumps carry a five-year parts and labour warranty from the date of installation, provided the unit is serviced every year by an approved engineer.", "For Aster 2 units installed on or after 1 March 2026 the warranty is extended to seven years, parts and labour, with the same annual service condition.", "Our boilers come with a two-year warranty as standard."], "context": {"question": "How long is the warranty on an Aster 2 heat pump installed on or after 1 March 2026?"}}]}'
```

Recorded answer (2026-11-03): `sufficient` with confidence 0.991.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-evidence-sufficient --revision v1.4 --local-dir adapters/evidence-sufficient
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
