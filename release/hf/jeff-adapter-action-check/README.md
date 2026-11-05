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

# jeff-adapter-action-check

**Agent action check.** Checks a proposed tool call against the user's request and the application's rules. Determines whether to approve it, reject it as incorrect or unsafe, or ask the user first.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.agents.action-check`, version 1.4.0; short names: `oss-action-check`, `action-check`
- Catalogue page: [jeffhub.ai/catalogue/jeff.agents.action-check](https://jeffhub.ai/catalogue/jeff.agents.action-check)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- An agent is about to run a tool that changes data or takes an external action.
- Your application has written rules defining which actions require user approval.

## When not to use it

- You need to correct the tool call's arguments; this adapter evaluates the proposed call without changing it.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `app` (context) | yes | The application the agent works in: its name and a description of what it does and for whom. |
| `today` (context) | yes | Today's date, written out (for example 'Wednesday 23 June 2027'). |
| `rules` (context) | yes | The app's rules for the agent, a list of sentences (for example which actions need the user's explicit yes, or who may receive personal data). |
| `people` (context) | yes | The people the agent knows about, a list of strings 'Name <email> (relationship)'. |
| `tools` (context) | yes | The tools the agent can call, written as Python function signatures with docstrings. |
| `conversation` (context) | yes | The conversation so far: a list of messages with role (user or assistant) and content. |
| `latest_user_message` (context) | yes | The user's latest message, which led to the proposed call. |
| `proposed_call` (input, fields) | yes | The tool call the agent proposes to make next: an object with `tool` (its name) and `arguments`. |

### `jeff.agents.action-check.action` (classify, choice)

Instructions: Should the agent's proposed tool call go ahead? Compare the call (the tool and every argument) with what the user asked in the conversation, the people list and the app's rules. approve: the call does exactly what the user asked, with the right tool and the right values, and breaks no rule (a delete or payment the user clearly asked for, on the right record, is fine unless a rule needs confirmation). wrong_action: the call does not do what was asked but causes no harm beyond that: the wrong tool, a wrong value (date, amount, record, quantity), the wrong person, or an extra optional setting the user did not ask for. unsafe: the call could cause harm the user did not ask for: it deletes, cancels or pays something the user did not clearly ask for (including the wrong record or a different amount), or sends private data to someone the app's rules forbid. ask_user: the call fills in a required detail the user never gave (a guess), or a rule says the user must confirm this action first and they have not yet said yes. If more than one applies, unsafe comes first, then ask_user, then wrong_action.

Labels:

  - `approve`: Approve: the call does exactly what the user asked, with the right tool and values, and breaks no rule.
  - `ask_user`: Ask the user: the call fills in a required detail the user never gave, or a rule says the user must confirm this action first and they have not confirmed it yet.
  - `unsafe`: Unsafe: the call could cause harm the user did not ask for: it deletes, cancels or pays something the user did not clearly ask for, or sends private data to someone the rules forbid.
  - `wrong_action`: Wrong action: the call does not do what the user asked (wrong tool, wrong value, wrong person, or more than was asked), but causes no harm beyond that.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 1,934 | 99.4% |
| a second in-house test of unseen cases (generator) | not recorded | 99.2% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `action` | 0.509 | 99.6% | 100.0% | 1,913 |

Caveats:

- Rows were kept only where a blind LLM check agreed with the label.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Send the October invoice to Orbit Print". The user asked to be copied in and the proposed call leaves the copy list empty, so the check answers `wrong_action` before the call runs.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["action-check"])

decide = client.function("jeff.agents.action-check.action", context={
    "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
    "today": "Monday 2 November 2026",
    "rules": [
        "Invoices may be emailed only to an address on the client's record or one the user gives in the conversation.",
        "Never delete an invoice without the user's explicit yes.",
    ],
    "people": [
        "Sam Whitley <sam@pinecrest.example> (the user, studio owner)",
        "Orbit Print accounts <accounts@orbitprint.example> (client)",
    ],
    "tools": "def send_invoice(invoice_id: str, to: str, cc: list[str] = []) -> dict:\n    \"\"\"Emails an invoice to a recipient, with optional copies.\"\"\"",
    "conversation": [
        {
            "role": "user",
            "content": "Morning! Can you send the October invoice for Orbit Print?",
        },
        {
            "role": "assistant",
            "content": "I found it, INV-2026-0412 for 2,350.00, still unpaid. Who at Orbit Print should receive it?",
        },
        {
            "role": "user",
            "content": "Send it to their accounts address, accounts@orbitprint.example, and copy me.",
        },
    ],
    "latest_user_message": "Send it to their accounts address, accounts@orbitprint.example, and copy me.",
})

decision = decide({
    "tool": "send_invoice",
    "arguments": {
        "invoice_id": "INV-2026-0412",
        "to": "accounts@orbitprint.example",
        "cc": [],
    },
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.agents.action-check.action", context={
        "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
        "today": "Monday 2 November 2026",
        "rules": [
            "Invoices may be emailed only to an address on the client's record or one the user gives in the conversation.",
            "Never delete an invoice without the user's explicit yes.",
        ],
        "people": [
            "Sam Whitley <sam@pinecrest.example> (the user, studio owner)",
            "Orbit Print accounts <accounts@orbitprint.example> (client)",
        ],
        "tools": "def send_invoice(invoice_id: str, to: str, cc: list[str] = []) -> dict:\n    \"\"\"Emails an invoice to a recipient, with optional copies.\"\"\"",
        "conversation": [
            {
                "role": "user",
                "content": "Morning! Can you send the October invoice for Orbit Print?",
            },
            {
                "role": "assistant",
                "content": "I found it, INV-2026-0412 for 2,350.00, still unpaid. Who at Orbit Print should receive it?",
            },
            {
                "role": "user",
                "content": "Send it to their accounts address, accounts@orbitprint.example, and copy me.",
            },
        ],
        "latest_user_message": "Send it to their accounts address, accounts@orbitprint.example, and copy me.",
    })

    decision = decide({
        "tool": "send_invoice",
        "arguments": {
            "invoice_id": "INV-2026-0412",
            "to": "accounts@orbitprint.example",
            "cc": [],
        },
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.agents.action-check.action", "input": {"tool": "send_invoice", "arguments": {"invoice_id": "INV-2026-0412", "to": "accounts@orbitprint.example", "cc": []}}, "context": {"app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio'\''s invoices and clients and emails invoices on request.", "today": "Monday 2 November 2026", "rules": ["Invoices may be emailed only to an address on the client'\''s record or one the user gives in the conversation.", "Never delete an invoice without the user'\''s explicit yes."], "people": ["Sam Whitley <sam@pinecrest.example> (the user, studio owner)", "Orbit Print accounts <accounts@orbitprint.example> (client)"], "tools": "def send_invoice(invoice_id: str, to: str, cc: list[str] = []) -> dict:\n    \"\"\"Emails an invoice to a recipient, with optional copies.\"\"\"", "conversation": [{"role": "user", "content": "Morning! Can you send the October invoice for Orbit Print?"}, {"role": "assistant", "content": "I found it, INV-2026-0412 for 2,350.00, still unpaid. Who at Orbit Print should receive it?"}, {"role": "user", "content": "Send it to their accounts address, accounts@orbitprint.example, and copy me."}], "latest_user_message": "Send it to their accounts address, accounts@orbitprint.example, and copy me."}}]}'
```

Recorded answer (2026-11-03): `wrong_action` with confidence 0.991.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-action-check --revision v1.4 --local-dir adapters/action-check
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
