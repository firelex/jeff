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
- agents
---

# jeff-adapter-assistant-routing

**Assistant reasoning check.** Predicts whether an assistant's next response requires careful reasoning or can be answered directly. Use it to route simpler turns to a faster, cheaper response path.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.agents.assistant-routing`, version 1.4.0; short names: `assistant-routing`
- Catalogue page: [jeffhub.ai/catalogue/jeff.agents.assistant-routing](https://jeffhub.ai/catalogue/jeff.agents.assistant-routing)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your assistant has a separate reasoning step and needs to decide when to use it.

## When not to use it

- You need to choose a tool or specialist; this adapter only checks whether reasoning is needed.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `accounts` (context) | yes | The user's accounts the assistant can act on, as one text (for example "Work account [work], Personal account [personal]"). |
| `date_and_time_zone` (context) | yes | Today's date and time and the user's time zone, as one sentence. |
| `conversation` (context) | yes | The conversation so far as one text, including the user's messages and the tool calls and results. |
| `turn` (input, fields) | yes | What the main assistant must do next in this turn (for example "Reply to the user in words."). |

### `jeff.agents.assistant-routing.needs_thinking` (detect, noul)

Instructions: Does the main assistant need to think carefully before writing this turn, rather than answering straight away?

Labels:

  - A yes/no question: the answer is `true` or `false`.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| held-out simulated conversations (generator) | 771 | 86.6% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `needs_thinking` | 0.882 | 95.2% | 54.8% | 341 |

Caveats:

- The label is a stand-in: whether a large LLM chose to think before answering, not a human judgement.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Thanks after a moved meeting". The user only says thank you after a finished task, so the reply needs no careful thinking and the answer is `false`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["assistant-routing"])

decide = client.function("jeff.agents.assistant-routing.needs_thinking", context={
    "accounts": "Work account [work], Personal account [personal]",
    "date_and_time_zone": "It is Tuesday 3 November 2026, 16:20, and the user is in London (UK time).",
    "conversation": "User: Can you move my 4 pm call with Priya to Thursday at the same time? Assistant: (calls calendar.move_event) Result: the call is now on Thursday 5 November at 16:00. Assistant: Done, your call with Priya is now on Thursday at 4 pm. User: Great, thanks!",
})

decision = decide("Reply to the user in words.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.agents.assistant-routing.needs_thinking", context={
        "accounts": "Work account [work], Personal account [personal]",
        "date_and_time_zone": "It is Tuesday 3 November 2026, 16:20, and the user is in London (UK time).",
        "conversation": "User: Can you move my 4 pm call with Priya to Thursday at the same time? Assistant: (calls calendar.move_event) Result: the call is now on Thursday 5 November at 16:00. Assistant: Done, your call with Priya is now on Thursday at 4 pm. User: Great, thanks!",
    })

    decision = decide("Reply to the user in words.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.agents.assistant-routing.needs_thinking", "input": "Reply to the user in words.", "context": {"accounts": "Work account [work], Personal account [personal]", "date_and_time_zone": "It is Tuesday 3 November 2026, 16:20, and the user is in London (UK time).", "conversation": "User: Can you move my 4 pm call with Priya to Thursday at the same time? Assistant: (calls calendar.move_event) Result: the call is now on Thursday 5 November at 16:00. Assistant: Done, your call with Priya is now on Thursday at 4 pm. User: Great, thanks!"}}]}'
```

Recorded answer (2026-11-03): `false` with confidence 0.951.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-assistant-routing --revision v1.4 --local-dir adapters/assistant-routing
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
