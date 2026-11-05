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
- security
---

# jeff-adapter-soc

**Security alert triage.** Applies a security team's playbook to an alert and recommends ignoring, investigating, isolating, blocking or escalating it.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.security.soc`, version 1.4.0; short names: `soc`
- Catalogue page: [jeffhub.ai/catalogue/jeff.security.soc](https://jeffhub.ai/catalogue/jeff.security.soc)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your security operations team has a written playbook defining rule order, thresholds, assets and allowed actions.
- Analysts should review uncertain alerts based on their probabilities.

## When not to use it

- You need to detect threats outside the supplied playbook; this adapter applies the rules provided.
- The assessment requires several events to be considered together.
- You plan commercial use; the adapter is licensed CC BY-NC 4.0 (non-commercial use only), because its training data includes non-commercial and all-rights-reserved sources.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `organisation` (context) | yes | One line on the organisation, its sector and which alert source this queue handles. |
| `playbook` (context) | yes | The playbook: numbered rules (the first that matches decides), thresholds, severity table, asset inventory or office policy, allowed actions, and what to do when a rule calls for an action the team may not take. |
| `alert` (input, fields) | yes | The new alert as the security tools report it - what fired, on which host or user, the evidence and any lookups. |

### `jeff.security.soc.action` (classify, choice)

Instructions: You are the first-line analyst in a security operations centre. Apply the organisation's playbook to the new alert and choose the action the playbook requires. The playbook's rules are checked in order and the first rule that matches decides.

Labels:

  - `block`: Block: block the outside address, file or account involved.
  - `isolate`: Isolate: cut the affected internal computer off the network.
  - `escalate`: Escalate: hand the alert at once to the senior responder.
  - `investigate`: Investigate: open a case for an analyst; no containment now.
  - `ignore`: Ignore: close the alert because no action at all is needed.
  - A call may offer a subset of these labels.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the held-out test (unknown) | 4,929 | 95.6% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `action` | 0.731 | 98.0% | 95.9% | 991 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Mass file renaming on a laptop". Thousands of files renamed within seconds on a laptop look like ransomware, so the playbook says to `isolate` the machine.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["soc"])

decide = client.function("jeff.security.soc.action", context={
    "organisation": "A regional logistics company; this queue handles alerts from the laptops' endpoint protection.",
    "playbook": "1. Ransomware behaviour (many files renamed or encrypted quickly) on any laptop: isolate the laptop.\n2. A known malicious outside address contacted: block the address.\n3. A login from a new country for an administrator: escalate.\n4. Anything else that fired: investigate.\nAllowed actions: block, isolate, escalate, investigate, ignore.",
})

decision = decide("Alert: suspicious file activity on laptop LT-0441 (user: warehouse planner).\nIn 90 seconds, 2,314 files in the user's documents folder were renamed with the extension .locked.\nProcess: an unsigned program started from the downloads folder.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.security.soc.action", context={
        "organisation": "A regional logistics company; this queue handles alerts from the laptops' endpoint protection.",
        "playbook": "1. Ransomware behaviour (many files renamed or encrypted quickly) on any laptop: isolate the laptop.\n2. A known malicious outside address contacted: block the address.\n3. A login from a new country for an administrator: escalate.\n4. Anything else that fired: investigate.\nAllowed actions: block, isolate, escalate, investigate, ignore.",
    })

    decision = decide("Alert: suspicious file activity on laptop LT-0441 (user: warehouse planner).\nIn 90 seconds, 2,314 files in the user's documents folder were renamed with the extension .locked.\nProcess: an unsigned program started from the downloads folder.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.security.soc.action", "input": "Alert: suspicious file activity on laptop LT-0441 (user: warehouse planner).\nIn 90 seconds, 2,314 files in the user'\''s documents folder were renamed with the extension .locked.\nProcess: an unsigned program started from the downloads folder.", "context": {"organisation": "A regional logistics company; this queue handles alerts from the laptops'\'' endpoint protection.", "playbook": "1. Ransomware behaviour (many files renamed or encrypted quickly) on any laptop: isolate the laptop.\n2. A known malicious outside address contacted: block the address.\n3. A login from a new country for an administrator: escalate.\n4. Anything else that fired: investigate.\nAllowed actions: block, isolate, escalate, investigate, ignore."}}]}'
```

Recorded answer (2026-11-03): `isolate` with confidence 0.795.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-soc --revision v1.4 --local-dir adapters/soc
```

## Files

| File | What it is |
|---|---|
| `adapter_model.safetensors`, `adapter_config.json` | The LoRA weights and their configuration |
| `readout.safetensors` | The adapter's readout over the answer codes |
| `decision_config.json` | Answer codes, temperature, prompt layout and the base it was trained on (the SHA-256 of the base's weights, which the engine checks) |
| `manifest.yaml` | The adapter's manifest: its functions, labels, fields and calibration, as the SDKs read it |
| `LICENSE` | The Apache License 2.0, for the Qwen3.5-0.8B notice below |

The test and calibration sets are not published yet. The manifest's `tests` entries already name them, so until they are, `jeff test` and `jeff calibrate` cannot download them for this adapter.

## Licence

Adapter licence: **CC BY-NC 4.0 (Creative Commons Attribution-NonCommercial 4.0 International: free to use and share with credit, but not for commercial purposes; https://creativecommons.org/licenses/by-nc/4.0/)**

It is non-commercial because its training data includes sources licensed for non-commercial use only.

**Qwen3.5-0.8B notice:** these weights were modified from [Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B) by the Jeff project: jeff-base is a fine-tune of Qwen3.5-0.8B, and this adapter was trained on top of it. Qwen3.5-0.8B is Copyright 2026 Alibaba Cloud and licensed under the Apache License, Version 2.0; a copy of that licence is in [`LICENSE`](LICENSE).

Jeff is an independent project, not affiliated with or endorsed by the makers of Qwen.
