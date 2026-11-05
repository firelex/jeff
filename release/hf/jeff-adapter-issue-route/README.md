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
- engineering
---

# jeff-adapter-issue-route

**Issue and pull request classification.** Assigns an issue or pull request to a project component and classifies it as a bug, feature, question, documentation change or duplicate.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.engineering.issue-route`, version 1.4.0; short names: `oss-issue-route`, `issue-route`
- Catalogue page: [jeffhub.ai/catalogue/jeff.engineering.issue-route](https://jeffhub.ai/catalogue/jeff.engineering.issue-route)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need to sort incoming issues or pull requests using a list of project components and their descriptions.

## When not to use it

- You need to write a response or resolve the issue; this adapter only classifies and routes it.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `project` (context) | yes | The project in a few sentences, and its components, each with its owner team, what it owns and its code paths. |
| `open_issues` (context) | yes | The project's open issues, one string each ("#number [Component] title: summary"). |
| `new_item` (input, fields) | yes | The new issue or pull request, an object with its type, number, title, body and any labels or changed files. |

### `jeff.engineering.issue-route.kind` (classify, choice)

Instructions: What kind of issue or pull request is this? Apply the first rule that fits. duplicate_candidate: it reports the same problem or asks for the same thing as one of the listed open issues, even in different words (a similar topic with a different problem is not a duplicate). docs: only documentation needs to change (wrong, missing or unclear docs, README, examples or code comments) and the software itself works as intended. bug: the software does not work as intended or as documented (errors, crashes, wrong results); a pull request that fixes such a defect is a bug. feature: it asks for new behaviour or an improvement the software does not claim to have, even if the writer calls it a bug; a pull request that adds behaviour is a feature. question: it asks how to do something or what the expected behaviour is, and does not report a defect or ask for a change.

Labels:

  - `bug`: Bug: the software does not work as intended or as documented (errors, crashes, wrong results, broken behaviour).
  - `docs`: Docs: only documentation needs to change (wrong, missing or unclear docs, README, examples or comments); the software works as intended.
  - `duplicate_candidate`: Duplicate candidate: reports the same problem or asks for the same thing as one of the listed open issues.
  - `feature`: Feature: asks for new behaviour or an improvement the software does not claim to have, even if called a bug.
  - `question`: Question: asks how to do something or what the expected behaviour is, without reporting a defect or asking for a change.

### `jeff.engineering.issue-route.owner` (route, choice)

Instructions: Which component owns this issue or pull request? Choose the component whose code, configuration or documentation must change to resolve it, using the component descriptions and paths. Where the problem was noticed does not decide it: follow the error message, stack trace, file paths or the cause the report describes. For a pull request, the component whose files it changes. For a question, the component whose behaviour is asked about. For a report that repeats one of the listed open issues, the component that owns the issue it repeats.

Labels:

  - The caller gives the labels (at most 12), each a key and its text.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,000 | 99.0% |
| a second in-house test of unseen cases (generator) | not recorded | 98.9% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `kind` | 0.564 | 99.1% | 100.0% | 1,020 |
| `owner` | 0.435 | 97.9% | 100.0% | 980 |

Caveats:

- Training and test rows were kept only where a blind LLM check agreed with the label, so these scores are probably inflated.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Crash on missing timestamps: who owns it". The crash shows in a chart command but comes from reading the file, so the owning part of the project is `readers`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["issue-route"])

decide = client.function("jeff.engineering.issue-route.owner", context={
    "project": "tidepool is an open-source Python library for reading and writing tide-gauge data files. Components:\n- readers (team io): file readers for CSV, NetCDF and the station formats; paths tidepool/readers/\n- plotting (team viz): charts and maps; paths tidepool/plot/\n- cli (team tools): the tidepool command; paths tidepool/cli/\n- docs (team docs): the documentation site; paths docs/",
    "open_issues": [
        "#398 [plotting] Map legend overlaps the colour bar",
        "#405 [cli] Add a --quiet flag",
    ],
}, labels={
    "readers": "File readers for CSV, NetCDF and the station formats (tidepool/readers/)",
    "plotting": "Charts and maps (tidepool/plot/)",
    "cli": "The tidepool command (tidepool/cli/)",
    "docs": "The documentation site (docs/)",
})

decision = decide({
    "type": "issue",
    "number": 417,
    "title": "tidepool plot crashes on a NetCDF file with missing timestamps",
    "body": "Running `tidepool plot station.nc` stops with \"KeyError: 'time'\" in tidepool/readers/netcdf.py line 88, before any chart appears. The same file opens fine in other tools.",
    "labels": [],
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.engineering.issue-route.owner", context={
        "project": "tidepool is an open-source Python library for reading and writing tide-gauge data files. Components:\n- readers (team io): file readers for CSV, NetCDF and the station formats; paths tidepool/readers/\n- plotting (team viz): charts and maps; paths tidepool/plot/\n- cli (team tools): the tidepool command; paths tidepool/cli/\n- docs (team docs): the documentation site; paths docs/",
        "open_issues": [
            "#398 [plotting] Map legend overlaps the colour bar",
            "#405 [cli] Add a --quiet flag",
        ],
    }, labels={
        "readers": "File readers for CSV, NetCDF and the station formats (tidepool/readers/)",
        "plotting": "Charts and maps (tidepool/plot/)",
        "cli": "The tidepool command (tidepool/cli/)",
        "docs": "The documentation site (docs/)",
    })

    decision = decide({
        "type": "issue",
        "number": 417,
        "title": "tidepool plot crashes on a NetCDF file with missing timestamps",
        "body": "Running `tidepool plot station.nc` stops with \"KeyError: 'time'\" in tidepool/readers/netcdf.py line 88, before any chart appears. The same file opens fine in other tools.",
        "labels": [],
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.engineering.issue-route.owner", "input": {"type": "issue", "number": 417, "title": "tidepool plot crashes on a NetCDF file with missing timestamps", "body": "Running `tidepool plot station.nc` stops with \"KeyError: '\''time'\''\" in tidepool/readers/netcdf.py line 88, before any chart appears. The same file opens fine in other tools.", "labels": []}, "context": {"project": "tidepool is an open-source Python library for reading and writing tide-gauge data files. Components:\n- readers (team io): file readers for CSV, NetCDF and the station formats; paths tidepool/readers/\n- plotting (team viz): charts and maps; paths tidepool/plot/\n- cli (team tools): the tidepool command; paths tidepool/cli/\n- docs (team docs): the documentation site; paths docs/", "open_issues": ["#398 [plotting] Map legend overlaps the colour bar", "#405 [cli] Add a --quiet flag"]}, "labels": {"readers": "File readers for CSV, NetCDF and the station formats (tidepool/readers/)", "plotting": "Charts and maps (tidepool/plot/)", "cli": "The tidepool command (tidepool/cli/)", "docs": "The documentation site (docs/)"}}]}'
```

Recorded answer (2026-11-03): `readers` with confidence 1.0.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-issue-route --revision v1.4 --local-dir adapters/issue-route
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
