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
- support
---

# jeff-adapter-requires-action

**Team action and escalation check.** Checks a message against a team's policy to determine whether action or escalation is required. Each decision is yes, no or unclear.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.support.requires-action`, version 1.4.0; short names: `oss-requires-action`, `requires-action`
- Catalogue page: [jeffhub.ai/catalogue/jeff.support.requires-action](https://jeffhub.ai/catalogue/jeff.support.requires-action)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your team receives many informational messages and has a policy defining which ones require action.

## When not to use it

- You need to choose between several teams; use Shared inbox triage.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `team` (context) | yes | The team: an object with the organisation and the team's name. |
| `policy` (context) | yes | The team's policy: a list of sentences saying what the team handles and when a message must be escalated. |
| `message` (input, fields) | yes | The message: an object with from, to, date, subject and body. |

### `jeff.support.requires-action.requires_action` (classify, choice)

Instructions: Given the team's policy, does this message require action from the team? yes: the message asks the team, openly or by clear implication, to do something about a matter the policy gives to this team, or one of the policy's escalation rules applies to it (escalating is an action). no: the message only shares information and no escalation rule applies, or it concerns a matter the policy gives to another team, or the policy says the team does not act on it (for example below a stated amount). unclear: the answer depends on something the policy and the message do not settle, for example a matter the policy gives to no team, or an amount that a rule depends on but the message does not state. Judge what the message actually asks and the facts it gives, not its tone, its claims of urgency or other misleading cues.

Labels:

  - `no`: The team does not need to act on this message.
  - `unclear`: The policy and the message do not settle whether the team must act.
  - `yes`: The team must act on this message.

### `jeff.support.requires-action.requires_escalation` (classify, choice)

Instructions: Given the team's policy, must this message be escalated? yes: one of the policy's escalation rules applies, for example the message raises a kind of matter that the policy says must always be escalated, or a request's amount is above the limit the policy sets. no: no escalation rule applies, for example an amount at or below the limit when the rule says 'above', or nothing of the kinds the rules list. unclear: whether a rule applies depends on something the message does not settle, for example the amount a rule depends on is not stated, or the message only hints vaguely at a matter that must be escalated. Judge the facts the message gives, not its tone, its claims of urgency or other misleading cues.

Labels:

  - `no`: No escalation rule of the policy applies to this message.
  - `unclear`: The policy and the message do not settle whether an escalation rule applies.
  - `yes`: The policy requires this message to be escalated.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,136 | 98.2% |
| a second in-house test of unseen cases (generator) | 2,224 | 98.1% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `requires_action` | 0.502 | 98.3% | 100.0% | 1,087 |
| `requires_escalation` | 0.489 | 99.1% | 100.0% | 1,087 |

Caveats:

- Training and test rows were kept only where a blind LLM check agreed with the label, so these scores are probably inflated.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Pothole reported to the council". Potholes are on the highways team's list, so the answer is `yes`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["requires-action"])

decide = client.function("jeff.support.requires-action.requires_action", context={
    "team": {
        "organisation": "Westmoor Borough Council",
        "name": "Highways team",
    },
    "policy": [
        "The team handles potholes, damaged road signs, blocked drains on roads and broken street lights.",
        "Any report of an immediate danger to people (for example an exposed cable or a collapsed road) must be escalated to the duty officer.",
        "Bin collections and parking fines belong to other teams.",
    ],
})

decision = decide({
    "from": "r.osei@mailbox.example",
    "to": "highways@westmoor.example",
    "date": "2 November 2026",
    "subject": "Pothole on Mill Lane",
    "body": "There's a deep pothole outside number 14 Mill Lane, right where cyclists come down the hill. Could someone take a look?",
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.support.requires-action.requires_action", context={
        "team": {
            "organisation": "Westmoor Borough Council",
            "name": "Highways team",
        },
        "policy": [
            "The team handles potholes, damaged road signs, blocked drains on roads and broken street lights.",
            "Any report of an immediate danger to people (for example an exposed cable or a collapsed road) must be escalated to the duty officer.",
            "Bin collections and parking fines belong to other teams.",
        ],
    })

    decision = decide({
        "from": "r.osei@mailbox.example",
        "to": "highways@westmoor.example",
        "date": "2 November 2026",
        "subject": "Pothole on Mill Lane",
        "body": "There's a deep pothole outside number 14 Mill Lane, right where cyclists come down the hill. Could someone take a look?",
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.support.requires-action.requires_action", "input": {"from": "r.osei@mailbox.example", "to": "highways@westmoor.example", "date": "2 November 2026", "subject": "Pothole on Mill Lane", "body": "There'\''s a deep pothole outside number 14 Mill Lane, right where cyclists come down the hill. Could someone take a look?"}, "context": {"team": {"organisation": "Westmoor Borough Council", "name": "Highways team"}, "policy": ["The team handles potholes, damaged road signs, blocked drains on roads and broken street lights.", "Any report of an immediate danger to people (for example an exposed cable or a collapsed road) must be escalated to the duty officer.", "Bin collections and parking fines belong to other teams."]}}]}'
```

Recorded answer (2026-11-03): `yes` with confidence 0.991.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-requires-action --revision v1.4 --local-dir adapters/requires-action
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
