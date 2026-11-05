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

# jeff-adapter-task-complete

**Agent task completion.** Checks the user's request against the agent's tool calls, results and final answer. Classifies the task as complete, incomplete or unclear.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.agents.task-complete`, version 1.4.0; short names: `oss-task-complete`, `task-complete`
- Catalogue page: [jeffhub.ai/catalogue/jeff.agents.task-complete](https://jeffhub.ai/catalogue/jeff.agents.task-complete)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to verify an agent's work before displaying its answer or closing the task.

## When not to use it

- You need to carry out the remaining work; this adapter only assesses completion.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `app` (context) | yes | The application the agent works in: its name and a description of what it does and for whom. |
| `today` (context) | yes | Today's date, written out (for example 'Wednesday 23 June 2027'). |
| `request` (context) | yes | The user's request, in the user's words. |
| `transcript` (context) | yes | The agent's tool calls, each with its call (tool and arguments) and result. |
| `final_answer` (input, fields) | yes | The agent's final answer to the user. |

### `jeff.agents.task-complete.complete` (classify, choice)

Instructions: Has the agent finished what the user asked? Check every part of the user's request against the tool calls and results in the transcript, and check the agent's final answer against the results. complete: every part was done by a call with the right details (records, dates, amounts, people) and its result shows it succeeded, and any question was answered with the value the results show. incomplete: at least one part has no call, its call failed (an error result), it was done with a different detail than the user asked, it was done for fewer items than asked, or the final answer gives a wrong value. unclear: no part is clearly missed or wrong, but for at least one part the transcript cannot show whether it succeeded (its result is missing or cut off, or only says the request was received or queued, or the status is unknown). The agent's final answer may claim more than was done; judge the calls and results themselves.

Labels:

  - `complete`: Complete: every part of the request was done with the right details, and any answer given is correct.
  - `incomplete`: Incomplete: at least one part was not done, failed, was done with the wrong details or for fewer items than asked, or the answer given to the user is wrong.
  - `unclear`: Unclear: nothing shows a part was missed or done wrongly, but the transcript cannot show whether some part actually succeeded.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,000 | 99.7% |
| a second in-house test of unseen cases (generator) | not recorded | 99.7% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `complete` | 0.850 | 100.0% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Invoice sent and copied". The agent says it is done, but the user was never copied in, so the task is `incomplete`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["task-complete"])

decide = client.function("jeff.agents.task-complete.complete", context={
    "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
    "today": "Monday 2 November 2026",
    "request": "Send the October invoice for Orbit Print to accounts@orbitprint.example and copy me.",
    "transcript": [
        {
            "call": {
                "tool": "find_invoice",
                "arguments": {
                    "client": "Orbit Print",
                    "month": "2026-10",
                },
            },
            "result": {
                "invoice_id": "INV-2026-0412",
                "total": 2350.0,
                "status": "unpaid",
            },
        },
        {
            "call": {
                "tool": "send_invoice",
                "arguments": {
                    "invoice_id": "INV-2026-0412",
                    "to": "accounts@orbitprint.example",
                    "cc": [],
                },
            },
            "result": {
                "status": "sent",
                "message_id": "m-77120",
            },
        },
    ],
})

decision = decide("Done! The October invoice (INV-2026-0412, 2,350.00) is on its way to Orbit Print, and you're copied in.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.agents.task-complete.complete", context={
        "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
        "today": "Monday 2 November 2026",
        "request": "Send the October invoice for Orbit Print to accounts@orbitprint.example and copy me.",
        "transcript": [
            {
                "call": {
                    "tool": "find_invoice",
                    "arguments": {
                        "client": "Orbit Print",
                        "month": "2026-10",
                    },
                },
                "result": {
                    "invoice_id": "INV-2026-0412",
                    "total": 2350.0,
                    "status": "unpaid",
                },
            },
            {
                "call": {
                    "tool": "send_invoice",
                    "arguments": {
                        "invoice_id": "INV-2026-0412",
                        "to": "accounts@orbitprint.example",
                        "cc": [],
                    },
                },
                "result": {
                    "status": "sent",
                    "message_id": "m-77120",
                },
            },
        ],
    })

    decision = decide("Done! The October invoice (INV-2026-0412, 2,350.00) is on its way to Orbit Print, and you're copied in.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.agents.task-complete.complete", "input": "Done! The October invoice (INV-2026-0412, 2,350.00) is on its way to Orbit Print, and you'\''re copied in.", "context": {"app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio'\''s invoices and clients and emails invoices on request.", "today": "Monday 2 November 2026", "request": "Send the October invoice for Orbit Print to accounts@orbitprint.example and copy me.", "transcript": [{"call": {"tool": "find_invoice", "arguments": {"client": "Orbit Print", "month": "2026-10"}}, "result": {"invoice_id": "INV-2026-0412", "total": 2350.0, "status": "unpaid"}}, {"call": {"tool": "send_invoice", "arguments": {"invoice_id": "INV-2026-0412", "to": "accounts@orbitprint.example", "cc": []}}, "result": {"status": "sent", "message_id": "m-77120"}}]}}]}'
```

Recorded answer (2026-11-05): `incomplete` with confidence 0.98.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-task-complete --revision v1.4 --local-dir adapters/task-complete
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
