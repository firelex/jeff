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

# jeff-adapter-result-relevant

**Tool result relevance.** Checks whether a tool result answers the agent's current question: yes, partly or no.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.agents.result-relevant`, version 1.4.0; short names: `oss-result-relevant`, `result-relevant`
- Catalogue page: [jeffhub.ai/catalogue/jeff.agents.result-relevant](https://jeffhub.ai/catalogue/jeff.agents.result-relevant)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your agent needs to check a search or tool result before proceeding.

## When not to use it

- You need to assess completion of the entire task; use Agent task completion.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `app` (context) | yes | The application the agent works in: its name and a description of what it does and for whom. |
| `today` (context) | yes | Today's date, written out (for example 'Wednesday 23 June 2027'). |
| `sub_question` (context) | yes | The agent's current sub-question. |
| `call` (context) | yes | The tool call made: an object with tool, its description and arguments. |
| `result` (input, fields) | yes | The tool's result, as returned. |

### `jeff.agents.result-relevant.relevant` (classify, choice)

Instructions: Does the tool result answer the agent's current sub-question? Check the record (id, name), the period and every detail the sub-question asks for. yes: the result gives every asked detail for the right record or period (other records or extra details next to it do not matter). partly: the result is about the right record or set and gives at least one of the asked details but not all of them, or only part of a list the sub-question needs in full was returned (more remain). no: the result is about a different record or a different period, gives none of the asked details (only other details, even for the right record), is empty, or is an error.

Labels:

  - `no`: No: the result does not answer it: it is about a different record or period, gives none of the asked details (only other ones), is empty, or is an error.
  - `partly`: Partly: the result is about the right record or set and gives at least one asked detail, but not all (a requested detail is missing), or only part of the list was returned.
  - `yes`: Yes: the result gives everything the sub-question asks, for the right record or period.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,000 | 98.6% |
| a second in-house test of unseen cases (generator) | not recorded | 98.5% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `relevant` | 0.369 | 99.2% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Orbit Print's September invoice". The agent needed October's invoice and the tool returned September's, so the answer is `no`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["result-relevant"])

decide = client.function("jeff.agents.result-relevant.relevant", context={
    "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
    "today": "Monday 2 November 2026",
    "sub_question": "What is the invoice number and total of Orbit Print's October 2026 invoice?",
    "call": {
        "tool": "find_invoice",
        "description": "Finds the studio's invoice for a client and month.",
        "arguments": {
            "client": "Orbit Print",
            "month": "2026-10",
        },
    },
})

decision = decide({
    "invoice_id": "INV-2026-0388",
    "client": "Orbit Print",
    "month": "2026-09",
    "total": 1980.0,
    "status": "paid",
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.agents.result-relevant.relevant", context={
        "app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio's invoices and clients and emails invoices on request.",
        "today": "Monday 2 November 2026",
        "sub_question": "What is the invoice number and total of Orbit Print's October 2026 invoice?",
        "call": {
            "tool": "find_invoice",
            "description": "Finds the studio's invoice for a client and month.",
            "arguments": {
                "client": "Orbit Print",
                "month": "2026-10",
            },
        },
    })

    decision = decide({
        "invoice_id": "INV-2026-0388",
        "client": "Orbit Print",
        "month": "2026-09",
        "total": 1980.0,
        "status": "paid",
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.agents.result-relevant.relevant", "input": {"invoice_id": "INV-2026-0388", "client": "Orbit Print", "month": "2026-09", "total": 1980.0, "status": "paid"}, "context": {"app": "Pinecrest Books, a bookkeeping assistant for a small design studio. It keeps the studio'\''s invoices and clients and emails invoices on request.", "today": "Monday 2 November 2026", "sub_question": "What is the invoice number and total of Orbit Print'\''s October 2026 invoice?", "call": {"tool": "find_invoice", "description": "Finds the studio'\''s invoice for a client and month.", "arguments": {"client": "Orbit Print", "month": "2026-10"}}}}]}'
```

Recorded answer (2026-11-05): `no` with confidence 0.954.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-result-relevant --revision v1.4 --local-dir adapters/result-relevant
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
