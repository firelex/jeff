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
- compliance
---

# jeff-adapter-sanctions

**Sanctions and watchlist screening.** Compares a customer or payment party with a single sanctions or watchlist entry. Returns match, possible match or no match.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.compliance.sanctions`, version 1.4.0; short names: `sanctions`
- Catalogue page: [jeffhub.ai/catalogue/jeff.compliance.sanctions](https://jeffhub.ai/catalogue/jeff.compliance.sanctions)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You have candidate watchlist entries and need to assess each customer-entry pair.
- You need to distinguish definite matches from cases that require further information and human review.

## When not to use it

- You need to search an entire watchlist; retrieve candidate entries first.
- You need a final decision without human review; screening decisions have legal consequences.
- You plan commercial use; the adapter is licensed CC BY-NC 4.0 (non-commercial use only), because its training data includes data licensed for non-commercial use only.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `watch_list_entry` (context) | yes | One sanctions or watch-list entry, as text: the listed name and aliases, type (person or entity), date of birth or registration, countries and the lists it is on. |
| `customer_record` (input, fields) | yes | The customer, or a party named in a payment, as text: name, party type, date of birth, nationality and other details the bank holds (some may be "not recorded"). |

### `jeff.compliance.sanctions.screening` (equivalent, choice)

Instructions: A bank checks each customer, or each party named in a payment, against one entry from a sanctions list or another watch list. Is the customer the party on the list? Names agree when the customer's name is the listed name or one of its aliases, allowing for other spellings or transliterations, a different order of the names, initials, a missing middle name and small typing mistakes. The deciding detail is the date of birth for a person, the country of registration for a company or organisation, and the IMO number for a ship. A year of birth alone cannot confirm a person, but a different year rules them out. A different kind of party, such as a ship instead of a person, is never the listed party. Nationality, occupation and the other details do not decide.

Labels:

  - `match`: Match: the customer is the listed party. The names agree and the deciding detail agrees with the entry.
  - `possible_match`: Possible match: the names agree and nothing contradicts the entry, but the deciding detail is missing from the customer record or from the entry, so a person must check it.
  - `no_match`: No match: the customer is a different party. The names clearly differ, the customer is a different kind of party, or the deciding detail contradicts the entry.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the held-out test (unknown) | 4,909 | 100.0% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `screening` | 0.951 | 99.8% | 100.0% | 1,012 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Dental hygienist in Toronto". The name is close to the listed person's, but the date of birth and nationality differ, so the answer is `no_match`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["sanctions"])

decide = client.function("jeff.compliance.sanctions.screening", context={
    "watch_list_entry": "Name: Viktor Aleksandrovich Morozan. Aliases: Victor Morozan; V. A. Morozan. Type: person.\nDate of birth: 14 June 1968. Nationality: Moldova. Lists: an asset-freeze list.",
})

decision = decide("Customer: Victor Morozan. Party type: person. Date of birth: 2 September 1991.\nNationality: Canada. Occupation: dental hygienist, Toronto. Customer since 2019.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.compliance.sanctions.screening", context={
        "watch_list_entry": "Name: Viktor Aleksandrovich Morozan. Aliases: Victor Morozan; V. A. Morozan. Type: person.\nDate of birth: 14 June 1968. Nationality: Moldova. Lists: an asset-freeze list.",
    })

    decision = decide("Customer: Victor Morozan. Party type: person. Date of birth: 2 September 1991.\nNationality: Canada. Occupation: dental hygienist, Toronto. Customer since 2019.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.compliance.sanctions.screening", "input": "Customer: Victor Morozan. Party type: person. Date of birth: 2 September 1991.\nNationality: Canada. Occupation: dental hygienist, Toronto. Customer since 2019.", "context": {"watch_list_entry": "Name: Viktor Aleksandrovich Morozan. Aliases: Victor Morozan; V. A. Morozan. Type: person.\nDate of birth: 14 June 1968. Nationality: Moldova. Lists: an asset-freeze list."}}]}'
```

Recorded answer (2026-11-03): `no_match` with confidence 1.0.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-sanctions --revision v1.4 --local-dir adapters/sanctions
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
