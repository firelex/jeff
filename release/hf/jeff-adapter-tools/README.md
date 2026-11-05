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
- routing
---

# jeff-adapter-tools

**Agent tool selection from labelled options.** Selects the next tool from tools supplied as labelled options with descriptions. Can also indicate that the agent should answer directly or ask the user for missing information.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.routing.tools`, version 1.4.0; short names: `tools`
- Catalogue page: [jeffhub.ai/catalogue/jeff.routing.tools](https://jeffhub.ai/catalogue/jeff.routing.tools)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your agent supplies its tools as labelled options and needs to choose its next action.
- Low-confidence tool selections should be referred to a larger model.

## When not to use it

- You need to generate tool arguments; this adapter selects a tool without constructing the call.
- You need a complete sequence of tool calls; this adapter selects one step at a time.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `agent` (context) | yes | What the agent is for, in one or two sentences (for example a triage assistant that checks entitlements before routing tickets). |
| `conversation` (context) | yes | The conversation so far, a list of turns, each {role (user or assistant), text}. |
| `user_message` (input, fields) | yes | The user's latest message, as plain text. |

### `jeff.routing.tools.tool` (route, choice)

Instructions: Which tool should the agent call next to handle the user's latest message? Use the conversation for context. If no tool is needed, choose answer directly. If a tool is needed but information it requires is missing, choose ask the user.

Labels:

  - The caller gives the labels (at most 134), each a key and its text.
  - Always offered first: `answer_directly`: No tool is needed: answer the user directly from the conversation
  - Always offered first: `ask_user`: A tool is needed but required information is missing: ask the user first

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 5,157 | 97.1% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `tool` | 0.275 | 97.6% | 100.0% | 2,195 |

Caveats:

- This figure does not include public function-calling benchmarks.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Book a hotel in Leeds". The user asks for a hotel to be booked, so the tool to call next is `book_hotel`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["tools"])

decide = client.function("jeff.routing.tools.tool", context={
    "agent": "A travel desk assistant for a consulting firm that books trains and hotels for staff and checks the travel policy.",
    "conversation": [
        {
            "role": "user",
            "text": "I'm going to the Leeds workshop next week.",
        },
        {
            "role": "assistant",
            "text": "Great. Do you need travel or a hotel booked?",
        },
    ],
}, labels={
    "book_train": "Books a train ticket for a traveller, given the route, date and time.",
    "book_hotel": "Books a hotel room for a traveller, given the city or address and the nights.",
    "check_policy": "Looks up what the firm's travel policy allows (classes of travel, nightly hotel limits).",
    "cancel_booking": "Cancels an existing booking by its reference.",
})

decision = decide("Yes, please book me a hotel in Leeds for Tuesday and Wednesday night.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.routing.tools.tool", context={
        "agent": "A travel desk assistant for a consulting firm that books trains and hotels for staff and checks the travel policy.",
        "conversation": [
            {
                "role": "user",
                "text": "I'm going to the Leeds workshop next week.",
            },
            {
                "role": "assistant",
                "text": "Great. Do you need travel or a hotel booked?",
            },
        ],
    }, labels={
        "book_train": "Books a train ticket for a traveller, given the route, date and time.",
        "book_hotel": "Books a hotel room for a traveller, given the city or address and the nights.",
        "check_policy": "Looks up what the firm's travel policy allows (classes of travel, nightly hotel limits).",
        "cancel_booking": "Cancels an existing booking by its reference.",
    })

    decision = decide("Yes, please book me a hotel in Leeds for Tuesday and Wednesday night.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.routing.tools.tool", "input": "Yes, please book me a hotel in Leeds for Tuesday and Wednesday night.", "context": {"agent": "A travel desk assistant for a consulting firm that books trains and hotels for staff and checks the travel policy.", "conversation": [{"role": "user", "text": "I'\''m going to the Leeds workshop next week."}, {"role": "assistant", "text": "Great. Do you need travel or a hotel booked?"}]}, "labels": {"book_train": "Books a train ticket for a traveller, given the route, date and time.", "book_hotel": "Books a hotel room for a traveller, given the city or address and the nights.", "check_policy": "Looks up what the firm'\''s travel policy allows (classes of travel, nightly hotel limits).", "cancel_booking": "Cancels an existing booking by its reference."}}]}'
```

Recorded answer (2026-11-03): `book_hotel` with confidence 0.889.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-tools --revision v1.4 --local-dir adapters/tools
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
