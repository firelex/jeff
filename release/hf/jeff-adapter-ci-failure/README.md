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

# jeff-adapter-ci-failure

**Build failure triage.** Classifies a failed build or test run using its log, code changes and run history. Distinguishes regressions, flaky tests, infrastructure issues, dependency changes and tests that need updating.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.engineering.ci-failure`, version 1.4.0; short names: `oss-ci-failure`, `ci-failure`
- Catalogue page: [jeffhub.ai/catalogue/jeff.engineering.ci-failure](https://jeffhub.ai/catalogue/jeff.engineering.ci-failure)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You need an initial classification of continuous integration failures before a maintainer investigates.

## When not to use it

- You need to fix the failure; this adapter only classifies it.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `project` (context) | yes | The software project: its name and a description of what it is. |
| `ci_run` (context) | yes | The failed CI run: an object with job, trigger (for example a pull request), commit and date. |
| `change` (context) | yes | The code change being tested: an object with its title and description. |
| `ci_facts` (context) | yes | Facts the CI system records: attempts on this commit, the history of the same test or job on the main branch, other jobs in this run, dependency versions compared with the last passing run, and durations. |
| `log` (input, fields) | yes | The log of the failed run, as text. |

### `jeff.engineering.ci-failure.failure` (classify, choice)

Instructions: What kind of failure is this CI run? Use the log, the change (if there is one) and the CI facts (attempts on this commit, the history of the same test or job on the main branch, other jobs, dependency versions compared with the last passing run, durations). Apply the first rule that fits. build_or_config: the run failed before any test ran, in compiling, type checking, linting, packaging or the CI configuration itself (a bad pipeline file, a missing secret or variable, a wrong path), even if the change caused it. environment_or_infra: the error comes from the CI machine or an outside service (network or DNS errors, a package registry or mirror down, no disk space, a lost runner, a service container that did not start), not from the code; other jobs often fail the same way. flaky_test: the test fails only some of the time on the same code: a rerun of the same commit passed, or the test fails now and then on the main branch without code changes. timeout_or_resource: a test or job was stopped for exceeding a time limit or a memory limit (out of memory, killed, exit code 137) on every attempt. dependency_change: a third-party package resolves to a different version than in the last passing run (pulled in automatically, or upgraded by the change itself) and the error comes from that package. test_needs_update: the change says this behaviour was changed on purpose, and the failing test still expects the old behaviour. real_regression: the change broke behaviour it did not mean to change: the failure repeats on every attempt and the same test passes on the main branch. Warnings or retries in the log that did not stop the run do not decide the answer.

Labels:

  - `build_or_config`: Build or configuration: the run failed before any test ran: compiling, type checking, linting, packaging, or the CI configuration itself (bad pipeline file, missing secret or variable, wrong path).
  - `dependency_change`: Dependency change: a third-party package now resolves to a different version than in the last passing run (pulled in automatically or upgraded by the change), and the failure comes from that package.
  - `environment_or_infra`: Environment or infrastructure: the failure comes from the CI machine or an outside service (network, package registry, disk space, a lost runner, a service container that did not start), not from the project's code.
  - `flaky_test`: Flaky test: the test fails only some of the time on the same code (a rerun of the same commit passed, or it fails now and then on unchanged code).
  - `real_regression`: Real regression: the change broke behaviour it did not mean to change; the failure repeats on every attempt and the same test passes on the main branch.
  - `test_needs_update`: Test needs update: the change says the behaviour was changed on purpose, and the failing test still expects the old behaviour.
  - `timeout_or_resource`: Timeout or resource limit: a test or job was stopped for exceeding a time limit or a memory limit, and it happens on every attempt.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,000 | 99.7% |
| a second in-house test of unseen cases (generator) | not recorded | 99.9% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `failure` | 0.595 | 99.6% | 100.0% | 1,500 |

Caveats:

- Training and test rows were kept only where a blind LLM check agreed with the label, so these scores are probably inflated.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "CSV test missing rows". The test fails the same way twice on this commit and passes on the main branch, so the failure is a `real_regression`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["ci-failure"])

decide = client.function("jeff.engineering.ci-failure.failure", context={
    "project": "tidepool, an open-source Python library for reading and writing tide-gauge data files.",
    "ci_run": {
        "job": "tests (python 3.12)",
        "trigger": "pull request 412",
        "commit": "9f3c2e1",
        "date": "2 November 2026",
    },
    "change": {
        "title": "Speed up the CSV reader",
        "description": "Reads rows in blocks of 4,096 instead of one at a time. No behaviour change intended.",
    },
    "ci_facts": {
        "attempts_on_this_commit": "2, both failed the same way",
        "same_test_on_main": "passed in each of the last 30 runs",
        "other_jobs_in_this_run": "lint passed; docs passed",
        "dependency_versions": "unchanged since the last passing run",
        "duration": "3 min 40 s (usual 3 min 30 s)",
    },
})

decision = decide("tests/test_csv.py::test_reads_last_row FAILED\nE   AssertionError: expected 1,000 rows, got 904\nE   assert 904 == 1000\n========== 1 failed, 213 passed in 41.2s ==========")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.engineering.ci-failure.failure", context={
        "project": "tidepool, an open-source Python library for reading and writing tide-gauge data files.",
        "ci_run": {
            "job": "tests (python 3.12)",
            "trigger": "pull request 412",
            "commit": "9f3c2e1",
            "date": "2 November 2026",
        },
        "change": {
            "title": "Speed up the CSV reader",
            "description": "Reads rows in blocks of 4,096 instead of one at a time. No behaviour change intended.",
        },
        "ci_facts": {
            "attempts_on_this_commit": "2, both failed the same way",
            "same_test_on_main": "passed in each of the last 30 runs",
            "other_jobs_in_this_run": "lint passed; docs passed",
            "dependency_versions": "unchanged since the last passing run",
            "duration": "3 min 40 s (usual 3 min 30 s)",
        },
    })

    decision = decide("tests/test_csv.py::test_reads_last_row FAILED\nE   AssertionError: expected 1,000 rows, got 904\nE   assert 904 == 1000\n========== 1 failed, 213 passed in 41.2s ==========")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.engineering.ci-failure.failure", "input": "tests/test_csv.py::test_reads_last_row FAILED\nE   AssertionError: expected 1,000 rows, got 904\nE   assert 904 == 1000\n========== 1 failed, 213 passed in 41.2s ==========", "context": {"project": "tidepool, an open-source Python library for reading and writing tide-gauge data files.", "ci_run": {"job": "tests (python 3.12)", "trigger": "pull request 412", "commit": "9f3c2e1", "date": "2 November 2026"}, "change": {"title": "Speed up the CSV reader", "description": "Reads rows in blocks of 4,096 instead of one at a time. No behaviour change intended."}, "ci_facts": {"attempts_on_this_commit": "2, both failed the same way", "same_test_on_main": "passed in each of the last 30 runs", "other_jobs_in_this_run": "lint passed; docs passed", "dependency_versions": "unchanged since the last passing run", "duration": "3 min 40 s (usual 3 min 30 s)"}}}]}'
```

Recorded answer (2026-11-03): `real_regression` with confidence 0.996.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-ci-failure --revision v1.4 --local-dir adapters/ci-failure
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
