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

# jeff-adapter-tool-route

**Agent tool selection from a menu.** Selects the next tool from a menu included in the request, using the conversation for context. Can also indicate that no tool is needed or that the user must supply missing information.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.agents.tool-route`, version 1.4.0; short names: `oss-tool-route`, `tool-route`
- Catalogue page: [jeffhub.ai/catalogue/jeff.agents.tool-route](https://jeffhub.ai/catalogue/jeff.agents.tool-route)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your tool menu is supplied as text, function signatures or schemas in the request.
- You need a tool-selection decision with a probability.

## When not to use it

- You need to generate tool arguments; this adapter selects a tool without constructing the call.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `app` (context) | yes | The application the agent works in: its name and a description of what it does and for whom. |
| `today` (context) | yes | Today's date, written out (for example 'Wednesday 23 June 2027'). |
| `tools` (context) | yes | The tool menu, in any common format (Python signatures, JSON schemas and so on). |
| `conversation` (context) | yes | The conversation so far, a list of messages (user, assistant, tool calls and tool results). |
| `request` (input, fields) | yes | The user's latest message. |

### `jeff.agents.tool-route.tool` (route, choice)

Instructions: What should the agent do next with the user's latest message? Choose the tool on the menu that should be called now when that tool can do what the user asks and the request and the conversation give every detail it requires (details the agent can take from earlier in the conversation count). Choose none when no tool is needed or none fits: the user is chatting or thanking, asks something the conversation already answers, or asks for something no tool on the menu can do. Choose ask_user when a tool fits but a required detail is missing and cannot be taken from the conversation, or when the request could mean two different records or actions and the agent must ask which. Read the tool names and descriptions carefully: similar tools do different things.

Labels:

  - The caller gives the labels (at most 24), each a key and its text.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| When2Call, a public function-calling benchmark (questions and tools from the Berkeley Function Calling Leaderboard) (public) | 3,646 | 71.6% |
| the in-house test (generator) | 2,000 | 98.2% |
| a second in-house test of unseen cases (generator) | 2,000 | 96.7% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `tool` | 0.413 | 98.5% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Send the invoice and copy me". The user has given the last detail the agent needed, so the next tool to call is `send_invoice`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["tool-route"])

decide = client.function("jeff.agents.tool-route.tool", context={
    "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
    "today": "Monday 2 November 2026",
    "tools": "def find_invoice(client: str, month: str) -> dict:\n    \"\"\"Finds the studio's invoice for a client and month.\"\"\"\ndef send_invoice(invoice_id: str, to: str, cc: list[str] = []) -> dict:\n    \"\"\"Emails an invoice to a recipient, with optional copies.\"\"\"\ndef create_invoice(client: str, items: list[dict]) -> dict:\n    \"\"\"Creates a new draft invoice.\"\"\"",
    "conversation": [
        {
            "role": "user",
            "content": "Morning! Can you send the October invoice for Orbit Print?",
        },
        {
            "role": "assistant",
            "content": "I found it, INV-2026-0412 for 2,350.00, still unpaid. Who at Orbit Print should receive it?",
        },
    ],
}, labels={
    "find_invoice": "Finds the studio's invoice for a client and month.",
    "send_invoice": "Emails an invoice to a recipient, with optional copies.",
    "create_invoice": "Creates a new draft invoice.",
    "none": "No tool is needed, or no tool on the menu can do what the user asks.",
    "ask_user": "A tool fits, but a required detail is missing and the agent must ask the user.",
})

decision = decide("Send it to their accounts address, accounts@orbitprint.example, and copy me.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.agents.tool-route.tool", context={
        "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
        "today": "Monday 2 November 2026",
        "tools": "def find_invoice(client: str, month: str) -> dict:\n    \"\"\"Finds the studio's invoice for a client and month.\"\"\"\ndef send_invoice(invoice_id: str, to: str, cc: list[str] = []) -> dict:\n    \"\"\"Emails an invoice to a recipient, with optional copies.\"\"\"\ndef create_invoice(client: str, items: list[dict]) -> dict:\n    \"\"\"Creates a new draft invoice.\"\"\"",
        "conversation": [
            {
                "role": "user",
                "content": "Morning! Can you send the October invoice for Orbit Print?",
            },
            {
                "role": "assistant",
                "content": "I found it, INV-2026-0412 for 2,350.00, still unpaid. Who at Orbit Print should receive it?",
            },
        ],
    }, labels={
        "find_invoice": "Finds the studio's invoice for a client and month.",
        "send_invoice": "Emails an invoice to a recipient, with optional copies.",
        "create_invoice": "Creates a new draft invoice.",
        "none": "No tool is needed, or no tool on the menu can do what the user asks.",
        "ask_user": "A tool fits, but a required detail is missing and the agent must ask the user.",
    })

    decision = decide("Send it to their accounts address, accounts@orbitprint.example, and copy me.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.agents.tool-route.tool", "input": "Send it to their accounts address, accounts@orbitprint.example, and copy me.", "context": {"app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio'\''s invoices and clients and emails invoices on request.", "today": "Monday 2 November 2026", "tools": "def find_invoice(client: str, month: str) -> dict:\n    \"\"\"Finds the studio'\''s invoice for a client and month.\"\"\"\ndef send_invoice(invoice_id: str, to: str, cc: list[str] = []) -> dict:\n    \"\"\"Emails an invoice to a recipient, with optional copies.\"\"\"\ndef create_invoice(client: str, items: list[dict]) -> dict:\n    \"\"\"Creates a new draft invoice.\"\"\"", "conversation": [{"role": "user", "content": "Morning! Can you send the October invoice for Orbit Print?"}, {"role": "assistant", "content": "I found it, INV-2026-0412 for 2,350.00, still unpaid. Who at Orbit Print should receive it?"}]}, "labels": {"find_invoice": "Finds the studio'\''s invoice for a client and month.", "send_invoice": "Emails an invoice to a recipient, with optional copies.", "create_invoice": "Creates a new draft invoice.", "none": "No tool is needed, or no tool on the menu can do what the user asks.", "ask_user": "A tool fits, but a required detail is missing and the agent must ask the user."}}]}'
```

Recorded answer (2026-11-03): `send_invoice` with confidence 0.998.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-tool-route --revision v1.4 --local-dir adapters/tool-route
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
