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

# jeff-adapter-after-result

**Agent next-step selection.** Determines what an agent should do after a tool returns a result: continue, retry, change approach, stop or hand over to a person.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.agents.after-result`, version 1.4.0; short names: `oss-after-result`, `after-result`
- Catalogue page: [jeffhub.ai/catalogue/jeff.agents.after-result](https://jeffhub.ai/catalogue/jeff.agents.after-result)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your agent needs to choose its next step after each tool result.
- Failed or stalled runs should be referred to a person.

## When not to use it

- You need to select a specific tool; use Agent tool selection from a menu.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `app` (context) | yes | The application the agent works in: its name and a description of what it does and for whom. |
| `today` (context) | yes | Today's date, written out (for example 'Wednesday 23 June 2027'). |
| `tools` (context) | yes | The tools the agent can call, a list of 'name: what it does'. |
| `retry_limit` (context) | yes | The app's rule for retrying a failed call, as a sentence (for example 'Retry a failed call at most 3 times'). |
| `goal` (context) | yes | What the user asked the agent to achieve, in the user's words. |
| `plan` (context) | yes | The agent's plan, a list of numbered steps each marked done, current step or still to do. |
| `steps_so_far` (context) | yes | The calls the agent has made so far, each with its call (tool and arguments) and its result. |
| `latest_call` (context) | yes | The call just made: tool, arguments and the attempt number. |
| `latest_result` (input, fields) | yes | The result of the latest call, as the tool returned it (a success with data, or an error with a status and message). |

### `jeff.agents.after-result.next` (classify, choice)

Instructions: Given the agent's goal, its plan and the latest tool result, what should the agent do next? continue: the latest call worked and steps of the plan remain, or it returned only part of a list and the goal needs the rest (fetch the next part). retry: the call failed for a temporary reason (a timeout, the service unavailable, a rate limit with a short wait) and the attempts so far are fewer than the retry limit; make the same call again. change_approach: the call cannot succeed as it was made because of the agent's own mistake (a value in the wrong format or not allowed, an id or name that differs from the one in the goal, a search spelled differently from the goal); correct the call or use another tool. stop: report back to the user: the goal is now done (the last step of the plan worked, or the result itself answers the goal), or it cannot be done (the record in the goal does not exist, or the request was refused for a reason no retry or other tool can change). escalate: a person must act: permission denied, login expired, approval needed, or a temporary failure has already used every allowed retry.

Labels:

  - `change_approach`: Change approach: the call cannot work as it was made (a wrong value, a typo, a bad format); fix the call or use another tool.
  - `continue`: Continue: the step worked (or returned part of a list with more to fetch); go on to the next step.
  - `escalate`: Escalate: a person has to act: permission denied, login expired, approval needed, or a temporary failure has used up every allowed retry.
  - `retry`: Retry: a temporary failure, and attempts so far are below the retry limit; make the same call again.
  - `stop`: Stop: report back to the user: the goal is done, or it cannot be done and nothing the agent can do will change that.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,000 | 100.0% |
| a second in-house test of unseen cases (generator) | not recorded | 100.0% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `next` | 0.702 | 100.0% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Mail service briefly down". The mail service failed with a temporary error on the first attempt, so the agent should `retry`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["after-result"])

decide = client.function("jeff.agents.after-result.next", context={
    "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
    "today": "Monday 2 November 2026",
    "tools": [
        "find_invoice: finds the studio's invoice for a client and month",
        "send_invoice: emails an invoice to a recipient, with optional copies",
    ],
    "retry_limit": "Retry a failed call at most 3 times.",
    "goal": "Send the October invoice for Orbit Print to accounts@orbitprint.example and copy me.",
    "plan": [
        "1. Find the October invoice for Orbit Print (done)",
        "2. Email it to accounts@orbitprint.example with a copy to sam@pinecrest.example (current step)",
        "3. Tell the user it was sent (still to do)",
    ],
    "steps_so_far": [
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
    ],
    "latest_call": {
        "tool": "send_invoice",
        "arguments": {
            "invoice_id": "INV-2026-0412",
            "to": "accounts@orbitprint.example",
            "cc": [
                "sam@pinecrest.example",
            ],
        },
        "attempt": 1,
    },
})

decision = decide({
    "status": "error",
    "code": 503,
    "message": "Mail service temporarily unavailable, please try again in a few seconds.",
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.agents.after-result.next", context={
        "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
        "today": "Monday 2 November 2026",
        "tools": [
            "find_invoice: finds the studio's invoice for a client and month",
            "send_invoice: emails an invoice to a recipient, with optional copies",
        ],
        "retry_limit": "Retry a failed call at most 3 times.",
        "goal": "Send the October invoice for Orbit Print to accounts@orbitprint.example and copy me.",
        "plan": [
            "1. Find the October invoice for Orbit Print (done)",
            "2. Email it to accounts@orbitprint.example with a copy to sam@pinecrest.example (current step)",
            "3. Tell the user it was sent (still to do)",
        ],
        "steps_so_far": [
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
        ],
        "latest_call": {
            "tool": "send_invoice",
            "arguments": {
                "invoice_id": "INV-2026-0412",
                "to": "accounts@orbitprint.example",
                "cc": [
                    "sam@pinecrest.example",
                ],
            },
            "attempt": 1,
        },
    })

    decision = decide({
        "status": "error",
        "code": 503,
        "message": "Mail service temporarily unavailable, please try again in a few seconds.",
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.agents.after-result.next", "input": {"status": "error", "code": 503, "message": "Mail service temporarily unavailable, please try again in a few seconds."}, "context": {"app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio'\''s invoices and clients and emails invoices on request.", "today": "Monday 2 November 2026", "tools": ["find_invoice: finds the studio'\''s invoice for a client and month", "send_invoice: emails an invoice to a recipient, with optional copies"], "retry_limit": "Retry a failed call at most 3 times.", "goal": "Send the October invoice for Orbit Print to accounts@orbitprint.example and copy me.", "plan": ["1. Find the October invoice for Orbit Print (done)", "2. Email it to accounts@orbitprint.example with a copy to sam@pinecrest.example (current step)", "3. Tell the user it was sent (still to do)"], "steps_so_far": [{"call": {"tool": "find_invoice", "arguments": {"client": "Orbit Print", "month": "2026-10"}}, "result": {"invoice_id": "INV-2026-0412", "total": 2350.0, "status": "unpaid"}}], "latest_call": {"tool": "send_invoice", "arguments": {"invoice_id": "INV-2026-0412", "to": "accounts@orbitprint.example", "cc": ["sam@pinecrest.example"]}, "attempt": 1}}}]}'
```

Recorded answer (2026-11-05): `retry` with confidence 1.0.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-after-result --revision v1.4 --local-dir adapters/after-result
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
