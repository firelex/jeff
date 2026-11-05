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
- language
---

# jeff-adapter-emotion

**Emotion detection in short text.** Identifies the strongest emotion in a short comment or message, choosing from 27 emotions and neutral.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.language.emotion`, version 1.4.0; short names: `emotion`
- Catalogue page: [jeffhub.ai/catalogue/jeff.language.emotion](https://jeffhub.ai/catalogue/jeff.language.emotion)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need more detail than positive or negative sentiment, such as gratitude, confusion or disappointment.
- Your input consists of short, informal English comments or messages.

## When not to use it

- Your texts are long, formal or in another language.
- You need a clinical or safety assessment, such as self-harm risk; this adapter only identifies emotions.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `source` (context) | yes | One short phrase on where the text comes from. In training this was always "Reddit comment ([NAME] and [RELIGION] replace removed names)". |
| `comment` (input, fields) | yes | The comment or message to read. |

### `jeff.language.emotion.emotion` (classify, choice)

Instructions: Which emotion does the comment express most? Choose the emotion that is strongest in the comment, or neutral if it expresses no particular emotion.

Labels:

  - `admiration`: Admiration: finding something impressive or worthy of respect.
  - `amusement`: Amusement: finding something funny or entertaining.
  - `anger`: Anger: a strong feeling of displeasure or hostility.
  - `annoyance`: Annoyance: mild anger or irritation.
  - `approval`: Approval: having or expressing a favourable opinion; agreeing.
  - `caring`: Caring: kindness and concern for others.
  - `confusion`: Confusion: lack of understanding or uncertainty.
  - `curiosity`: Curiosity: a strong wish to know or learn something.
  - `desire`: Desire: a strong wish for something or for something to happen.
  - `disappointment`: Disappointment: sadness or displeasure because hopes or expectations were not met.
  - `disapproval`: Disapproval: having or expressing an unfavourable opinion; disagreeing.
  - `disgust`: Disgust: revulsion or strong disapproval caused by something unpleasant or offensive.
  - `embarrassment`: Embarrassment: self-consciousness, shame or awkwardness.
  - `excitement`: Excitement: great enthusiasm and eagerness.
  - `fear`: Fear: being afraid or worried about danger or harm.
  - `gratitude`: Gratitude: feeling thankful or appreciative.
  - `grief`: Grief: intense sorrow, especially over someone's death.
  - `joy`: Joy: a feeling of pleasure and happiness.
  - `love`: Love: strong positive emotion of affection and regard.
  - `nervousness`: Nervousness: apprehension, worry or anxiety.
  - `neutral`: Neutral: no particular emotion is expressed.
  - `optimism`: Optimism: hopefulness and confidence about the future.
  - `pride`: Pride: pleasure or satisfaction from one's own or a close person's achievements.
  - `realization`: Realization: becoming aware of something.
  - `relief`: Relief: reassurance and relaxation after anxiety or distress ends.
  - `remorse`: Remorse: regret or guilt about something one did.
  - `sadness`: Sadness: feeling sorrow or unhappiness.
  - `surprise`: Surprise: being astonished or startled by something unexpected.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the test split of GoEmotions, a public benchmark of 27 emotions and neutral (public) | 5,408 | 59.8% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `emotion` | 0.846 | 90.1% | 15.1% | 1,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Thanks to the night nurses". A forum post thanking the people who recommended the nurses who looked after the writer's father reads as `gratitude`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["emotion"])

decide = client.function("jeff.language.emotion.emotion", context={
    "source": "A comment on an online forum",
})

decision = decide("Just wanted to say thank you to whoever recommended the night nurses at St Aldric's in my last thread. They looked after my dad all week and kept me updated every evening. I honestly couldn't have got through it without you all.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.language.emotion.emotion", context={
        "source": "A comment on an online forum",
    })

    decision = decide("Just wanted to say thank you to whoever recommended the night nurses at St Aldric's in my last thread. They looked after my dad all week and kept me updated every evening. I honestly couldn't have got through it without you all.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.language.emotion.emotion", "input": "Just wanted to say thank you to whoever recommended the night nurses at St Aldric'\''s in my last thread. They looked after my dad all week and kept me updated every evening. I honestly couldn'\''t have got through it without you all.", "context": {"source": "A comment on an online forum"}}]}'
```

Recorded answer (2026-11-03): `gratitude` with confidence 0.934.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-emotion --revision v1.4 --local-dir adapters/emotion
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
