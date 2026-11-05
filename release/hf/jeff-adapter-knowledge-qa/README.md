---
license: cc-by-nc-4.0
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
- knowledge
---

# jeff-adapter-knowledge-qa

**Multiple-choice general knowledge.** Selects an answer to a multiple-choice question about science, common sense or general knowledge.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.knowledge.knowledge-qa`, version 1.4.0; short names: `knowledge-qa-v2`, `knowledge-qa`
- Catalogue page: [jeffhub.ai/catalogue/jeff.knowledge.knowledge-qa](https://jeffhub.ai/catalogue/jeff.knowledge.knowledge-qa)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You have a multiple-choice question with a defined set of answer options.

## When not to use it

- The answer depends on private documents or recent events; supply the relevant facts or use a retrieval adapter.
- You plan commercial use; the adapter is licensed CC BY-NC 4.0 (non-commercial use only), because its training data includes benchmarks licensed for non-commercial use only.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `question` (input, text) | yes | A question or statement to complete, as plain text; the options are the possible answers (A, B, ...). |

### `jeff.knowledge.knowledge-qa.blank` (classify, choice)

Instructions: Which option correctly fills the blank (_) in the sentence?

This function takes its own fields:

| Field | Required | What it holds |
|---|---|---|
| `sentence` (input, text) | yes | A sentence with one blank written as _, as plain text; the options are the two candidate fillers. |

Labels:

  - The caller writes the text of each option; the keys are `A`, `B`.

### `jeff.knowledge.knowledge-qa.question` (classify, choice)

Instructions: Choose the option that correctly answers the question or completes the statement.

Labels:

  - The caller gives the labels (at most 8), each a key and its text.

### `jeff.knowledge.knowledge-qa.continuation` (classify, choice)

Instructions: Choose the ending that most plausibly continues the description of the activity.

This function takes its own fields:

| Field | Required | What it holds |
|---|---|---|
| `activity` (context) | yes | The activity being described, in a few words (for example "Breakdancing"). |
| `text` (input, fields) | yes | The start of a description of the activity, to be continued by one of the options. |

Labels:

  - The caller writes the text of each option; the keys are `A`, `B`, `C`, `D`.

### `jeff.knowledge.knowledge-qa.passage` (classify, choice)

Instructions: Read the passage and choose the option that correctly answers the question or completes the statement about it.

This function takes its own fields:

| Field | Required | What it holds |
|---|---|---|
| `passage` (context) | yes | The passage to read. |
| `question` (input, fields) | yes | The question about the passage, or a statement to complete. |

Labels:

  - The caller writes the text of each option; the keys are `A`, `B`, `C`, `D`.

### `jeff.knowledge.knowledge-qa.passage_text` (classify, choice)

Instructions: Read the passage and choose the option that correctly answers the question or completes the statement about it.

This function takes its own fields:

| Field | Required | What it holds |
|---|---|---|
| `passage_and_question` (input, text) | yes | The passage followed by its question, as one plain text. |

Labels:

  - The caller writes the text of each option; the keys are `A`, `B`, `C`, `D`.

### `jeff.knowledge.knowledge-qa.social` (classify, choice)

Instructions: Read the situation and choose the option that best answers the question about the people in it.

This function takes its own fields:

| Field | Required | What it holds |
|---|---|---|
| `situation` (context) | yes | A short everyday situation involving one or more people. |
| `question` (input, fields) | yes | The question about the people in the situation. |

Labels:

  - The caller writes the text of each option; the keys are `A`, `B`, `C`.

### `jeff.knowledge.knowledge-qa.solution` (classify, choice)

Instructions: Choose the solution that best achieves the goal.

This function takes its own fields:

| Field | Required | What it holds |
|---|---|---|
| `goal` (input, text) | yes | A practical goal, as plain text; the options are two candidate solutions. |

Labels:

  - The caller writes the text of each option; the keys are `A`, `B`.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| held-out rows of public benchmarks: WinoGrande, ARC, OpenBookQA, QASC, CommonsenseQA, SciQ, HellaSwag, RACE, MMLU (auxiliary), Social IQa and PIQA (public) | 1,889 | 78.9% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `blank` | 0.515 | 95.8% | 100.0% | 165 |
| `question` | 0.498 | 90.0% | 87.9% | 876 |
| `continuation` | 0.731 | 90.0% | 27.8% | 180 |
| `passage` | 0.724 | 90.1% | 67.3% | 150 |
| `passage_text` | 0.508 | 90.0% | 66.7% | 195 |
| `social` | 0.819 | 90.0% | 51.9% | 135 |
| `solution` | 0.888 | 90.2% | 21.8% | 188 |

Caveats:

- Part of the held-out rows were already in the base model's own pretraining data, which may make the score higher than on questions it has never seen.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Warfarin and antibiotics". Some antibiotics strengthen warfarin's effect and raise the risk of bleeding, so the answer is option `B`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["knowledge-qa"])

decide = client.function("jeff.knowledge.knowledge-qa.question", labels={
    "A": "Antibiotics neutralise warfarin in the stomach, so the dose stops working.",
    "B": "Some antibiotics can strengthen warfarin's effect and raise the risk of bleeding.",
    "C": "Antibiotics thicken the blood, so the patient is more likely to form clots.",
    "D": "Warfarin stops antibiotics from being absorbed, so the infection may not clear.",
})

decision = decide("A patient taking warfarin is prescribed a course of antibiotics. Why should the patient's INR (a blood clotting test) be checked more often during the course?")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.knowledge.knowledge-qa.question", labels={
        "A": "Antibiotics neutralise warfarin in the stomach, so the dose stops working.",
        "B": "Some antibiotics can strengthen warfarin's effect and raise the risk of bleeding.",
        "C": "Antibiotics thicken the blood, so the patient is more likely to form clots.",
        "D": "Warfarin stops antibiotics from being absorbed, so the infection may not clear.",
    })

    decision = decide("A patient taking warfarin is prescribed a course of antibiotics. Why should the patient's INR (a blood clotting test) be checked more often during the course?")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.knowledge.knowledge-qa.question", "input": "A patient taking warfarin is prescribed a course of antibiotics. Why should the patient'\''s INR (a blood clotting test) be checked more often during the course?", "labels": {"A": "Antibiotics neutralise warfarin in the stomach, so the dose stops working.", "B": "Some antibiotics can strengthen warfarin'\''s effect and raise the risk of bleeding.", "C": "Antibiotics thicken the blood, so the patient is more likely to form clots.", "D": "Warfarin stops antibiotics from being absorbed, so the infection may not clear."}}]}'
```

Recorded answer (2026-11-03): `B` with confidence 0.832.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-knowledge-qa --revision v1.4 --local-dir adapters/knowledge-qa
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

Adapter licence: **CC BY-NC 4.0 (Creative Commons Attribution-NonCommercial 4.0 International: free to use and share with credit, but not for commercial purposes; https://creativecommons.org/licenses/by-nc/4.0/)**

It is non-commercial because its training data includes sources licensed for non-commercial use only. The test and calibration sets in this repository are shared on the same terms.

**Qwen3.5-0.8B notice:** these weights were modified from [Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B) by the Jeff project: jeff-base is a fine-tune of Qwen3.5-0.8B, and this adapter was trained on top of it. Qwen3.5-0.8B is Copyright 2026 Alibaba Cloud and licensed under the Apache License, Version 2.0; a copy of that licence is in [`LICENSE`](LICENSE).

Jeff is an independent project, not affiliated with or endorsed by the makers of Qwen.
