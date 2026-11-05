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
- routing
---

# jeff-adapter-router-open-v14

**Open-adapter router.** Selects an open specialist from your menu to handle a request. Returns base when no specialist fits and the general model should handle it.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.routing.router-open-v14`, version 1.4.0; short names: `jeff-router-open-v14`, `jeff.routing.router-open`, `router-open-v14`
- Catalogue page: [jeffhub.ai/catalogue/jeff.routing.router-open-v14](https://jeffhub.ai/catalogue/jeff.routing.router-open-v14)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- You route requests among the open specialists supported by this router.

## When not to use it

- Your menu includes specialists outside the set listed in the router's documentation.
- You already know which adapter to call.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `request` (input, fields) | yes | The request to route, as a JSON object: the `instructions` of the question, its `options` (the label keys) and its `state` (the input, as JSON text). |

### `jeff.routing.router-open-v14.specialist` (route, choice)

Instructions: Which specialist should answer this request? Pick the specialist whose task matches what the request asks, or base if no specialist fits and the general model should answer.

Labels:

  - The caller gives the labels (at most 32), each a key and its text.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-11-03.

| Test | Rows | Accuracy |
|---|--:|--:|
| requests from each open specialist's own held-out test data and from public benchmarks, some reworded by an LLM (mixed) | 6,930 | 99.8% |
| the requests as written (mixed) | 5,378 | 99.9% |
| the requests reworded by an LLM (mixed) | 1,552 | 99.5% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

No calibration figure: A router picks a specialist for every request rather than holding back, so it has no calibration and no threshold.

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "CI failure request for the open router". A failed test run whose test passes on the main branch goes to the `ci-failure` specialist.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["router-open-v14"])

decide = client.function("jeff.routing.router-open.specialist", labels={
    "message-triage": "Triages a message using the inbox's own message kinds, routing table and rules: what it is, what action it needs, how urgent it is and who should handle it.",
    "support-intents": "Names what a customer or assistant user is asking for, from a fixed list of requests such as cancel_order or track_refund.",
    "triage": "Routes a support ticket or email to a team and scores its urgency, sentiment and need for a person.",
    "passage-relevant": "For search and retrieval: checks whether a passage answers a question for the right subject and period: yes, partly or no.",
    "evidence-sufficient": "For search and retrieval: checks whether a set of passages is enough to answer a question: sufficient, insufficient or conflicting.",
    "entails": "Checks whether the evidence given (documents, messages or records) supports a claim: supported, unsupported or unclear.",
    "contradicts": "Checks whether two statements or records conflict: yes, no, or unclear (for example a date that can be read two ways).",
    "guard": "Checks a user message or outside content for prompt injection, jailbreak and data-leak attempts, and names the kind.",
    "spam": "Says whether a text message or email is spam or phishing, and whether it is legitimate, spam or a phishing scam.",
    "tool-route": "Given a tool menu written into the request (in any common format) and the conversation, picks the tool to call next, no tool, or asking the user for a missing detail.",
    "ci-failure": "Reads a failed automated build-and-test run (its log, the code change and the run history) and says what kind of failure it is: a real regression, a flaky test, infrastructure, a dependency change, a test that needs updating, and so on.",
    "base": "No listed specialist fits this request, so the general model with no specialist answers it: everyday reasoning, reading, knowledge and judging tasks, or any task that none of the listed specialists covers.",
})

decision = decide({
    "instructions": "What kind of failure is this CI run? Use the log, the change (if there is one) and the CI facts (attempts on this commit, the history of the same test or job on the main branch, other jobs, dependency versions compared with the last passing run, durations). Apply the first rule that fits. build_or_config: the run failed before any test ran, in compiling, type checking, linting, packaging or the CI configuration itself (a bad pipeline file, a missing secret or variable, a wrong path), even if the change caused it. environment_or_infra: the error comes from the CI machine or an outside service (network or DNS errors, a package registry or mirror down, no disk space, a lost runner, a service container that did not start), not from the code; other jobs often fail the same way. flaky_test: the test fails only some of the time on the same code: a rerun of the same commit passed, or the test fails now and then on the main branch without code changes. timeout_or_resource: a test or job was stopped for exceeding a time limit or a memory limit (out of memory, killed, exit code 137) on every attempt. dependency_change: a third-party package resolves to a different version than in the last passing run (pulled in automatically, or upgraded by the change itself) and the error comes from that package. test_needs_update: the change says this behaviour was changed on purpose, and the failing test still expects the old behaviour. real_regression: the change broke behaviour it did not mean to change: the failure repeats on every attempt and the same test passes on the main branch. Warnings or retries in the log that did not stop the run do not decide the answer.",
    "options": [
        "build_or_config",
        "dependency_change",
        "environment_or_infra",
        "flaky_test",
        "real_regression",
        "test_needs_update",
        "timeout_or_resource",
    ],
    "state": "{\"project\": \"tidepool, an open-source Python library for reading and writing tide-gauge data files.\", \"ci_run\": {\"job\": \"tests (python 3.12)\", \"trigger\": \"pull request 412\", \"commit\": \"9f3c2e1\", \"date\": \"2 November 2026\"}, \"change\": {\"title\": \"Speed up the CSV reader\", \"description\": \"Reads rows in blocks of 4,096 instead of one at a time. No behaviour change intended.\"}, \"ci_facts\": {\"attempts_on_this_commit\": \"2, both failed the same way\", \"same_test_on_main\": \"passed in each of the last 30 runs\", \"other_jobs_in_this_run\": \"lint passed; docs passed\", \"dependency_versions\": \"unchanged since the last passing run\", \"duration\": \"3 min 40 s (usual 3 min 30 s)\"}, \"log\": \"tests/test_csv.py::test_reads_last_row FAILED\\nE   AssertionError: expected 1,000 rows, got 904\\nE   assert 904 == 1000\\n========== 1 failed, 213 passed in 41.2s ==========\"}",
})

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.routing.router-open.specialist", labels={
        "message-triage": "Triages a message using the inbox's own message kinds, routing table and rules: what it is, what action it needs, how urgent it is and who should handle it.",
        "support-intents": "Names what a customer or assistant user is asking for, from a fixed list of requests such as cancel_order or track_refund.",
        "triage": "Routes a support ticket or email to a team and scores its urgency, sentiment and need for a person.",
        "passage-relevant": "For search and retrieval: checks whether a passage answers a question for the right subject and period: yes, partly or no.",
        "evidence-sufficient": "For search and retrieval: checks whether a set of passages is enough to answer a question: sufficient, insufficient or conflicting.",
        "entails": "Checks whether the evidence given (documents, messages or records) supports a claim: supported, unsupported or unclear.",
        "contradicts": "Checks whether two statements or records conflict: yes, no, or unclear (for example a date that can be read two ways).",
        "guard": "Checks a user message or outside content for prompt injection, jailbreak and data-leak attempts, and names the kind.",
        "spam": "Says whether a text message or email is spam or phishing, and whether it is legitimate, spam or a phishing scam.",
        "tool-route": "Given a tool menu written into the request (in any common format) and the conversation, picks the tool to call next, no tool, or asking the user for a missing detail.",
        "ci-failure": "Reads a failed automated build-and-test run (its log, the code change and the run history) and says what kind of failure it is: a real regression, a flaky test, infrastructure, a dependency change, a test that needs updating, and so on.",
        "base": "No listed specialist fits this request, so the general model with no specialist answers it: everyday reasoning, reading, knowledge and judging tasks, or any task that none of the listed specialists covers.",
    })

    decision = decide({
        "instructions": "What kind of failure is this CI run? Use the log, the change (if there is one) and the CI facts (attempts on this commit, the history of the same test or job on the main branch, other jobs, dependency versions compared with the last passing run, durations). Apply the first rule that fits. build_or_config: the run failed before any test ran, in compiling, type checking, linting, packaging or the CI configuration itself (a bad pipeline file, a missing secret or variable, a wrong path), even if the change caused it. environment_or_infra: the error comes from the CI machine or an outside service (network or DNS errors, a package registry or mirror down, no disk space, a lost runner, a service container that did not start), not from the code; other jobs often fail the same way. flaky_test: the test fails only some of the time on the same code: a rerun of the same commit passed, or the test fails now and then on the main branch without code changes. timeout_or_resource: a test or job was stopped for exceeding a time limit or a memory limit (out of memory, killed, exit code 137) on every attempt. dependency_change: a third-party package resolves to a different version than in the last passing run (pulled in automatically, or upgraded by the change itself) and the error comes from that package. test_needs_update: the change says this behaviour was changed on purpose, and the failing test still expects the old behaviour. real_regression: the change broke behaviour it did not mean to change: the failure repeats on every attempt and the same test passes on the main branch. Warnings or retries in the log that did not stop the run do not decide the answer.",
        "options": [
            "build_or_config",
            "dependency_change",
            "environment_or_infra",
            "flaky_test",
            "real_regression",
            "test_needs_update",
            "timeout_or_resource",
        ],
        "state": "{\"project\": \"tidepool, an open-source Python library for reading and writing tide-gauge data files.\", \"ci_run\": {\"job\": \"tests (python 3.12)\", \"trigger\": \"pull request 412\", \"commit\": \"9f3c2e1\", \"date\": \"2 November 2026\"}, \"change\": {\"title\": \"Speed up the CSV reader\", \"description\": \"Reads rows in blocks of 4,096 instead of one at a time. No behaviour change intended.\"}, \"ci_facts\": {\"attempts_on_this_commit\": \"2, both failed the same way\", \"same_test_on_main\": \"passed in each of the last 30 runs\", \"other_jobs_in_this_run\": \"lint passed; docs passed\", \"dependency_versions\": \"unchanged since the last passing run\", \"duration\": \"3 min 40 s (usual 3 min 30 s)\"}, \"log\": \"tests/test_csv.py::test_reads_last_row FAILED\\nE   AssertionError: expected 1,000 rows, got 904\\nE   assert 904 == 1000\\n========== 1 failed, 213 passed in 41.2s ==========\"}",
    })

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.routing.router-open.specialist", "input": {"instructions": "What kind of failure is this CI run? Use the log, the change (if there is one) and the CI facts (attempts on this commit, the history of the same test or job on the main branch, other jobs, dependency versions compared with the last passing run, durations). Apply the first rule that fits. build_or_config: the run failed before any test ran, in compiling, type checking, linting, packaging or the CI configuration itself (a bad pipeline file, a missing secret or variable, a wrong path), even if the change caused it. environment_or_infra: the error comes from the CI machine or an outside service (network or DNS errors, a package registry or mirror down, no disk space, a lost runner, a service container that did not start), not from the code; other jobs often fail the same way. flaky_test: the test fails only some of the time on the same code: a rerun of the same commit passed, or the test fails now and then on the main branch without code changes. timeout_or_resource: a test or job was stopped for exceeding a time limit or a memory limit (out of memory, killed, exit code 137) on every attempt. dependency_change: a third-party package resolves to a different version than in the last passing run (pulled in automatically, or upgraded by the change itself) and the error comes from that package. test_needs_update: the change says this behaviour was changed on purpose, and the failing test still expects the old behaviour. real_regression: the change broke behaviour it did not mean to change: the failure repeats on every attempt and the same test passes on the main branch. Warnings or retries in the log that did not stop the run do not decide the answer.", "options": ["build_or_config", "dependency_change", "environment_or_infra", "flaky_test", "real_regression", "test_needs_update", "timeout_or_resource"], "state": "{\"project\": \"tidepool, an open-source Python library for reading and writing tide-gauge data files.\", \"ci_run\": {\"job\": \"tests (python 3.12)\", \"trigger\": \"pull request 412\", \"commit\": \"9f3c2e1\", \"date\": \"2 November 2026\"}, \"change\": {\"title\": \"Speed up the CSV reader\", \"description\": \"Reads rows in blocks of 4,096 instead of one at a time. No behaviour change intended.\"}, \"ci_facts\": {\"attempts_on_this_commit\": \"2, both failed the same way\", \"same_test_on_main\": \"passed in each of the last 30 runs\", \"other_jobs_in_this_run\": \"lint passed; docs passed\", \"dependency_versions\": \"unchanged since the last passing run\", \"duration\": \"3 min 40 s (usual 3 min 30 s)\"}, \"log\": \"tests/test_csv.py::test_reads_last_row FAILED\\nE   AssertionError: expected 1,000 rows, got 904\\nE   assert 904 == 1000\\n========== 1 failed, 213 passed in 41.2s ==========\"}"}, "labels": {"message-triage": "Triages a message using the inbox'\''s own message kinds, routing table and rules: what it is, what action it needs, how urgent it is and who should handle it.", "support-intents": "Names what a customer or assistant user is asking for, from a fixed list of requests such as cancel_order or track_refund.", "triage": "Routes a support ticket or email to a team and scores its urgency, sentiment and need for a person.", "passage-relevant": "For search and retrieval: checks whether a passage answers a question for the right subject and period: yes, partly or no.", "evidence-sufficient": "For search and retrieval: checks whether a set of passages is enough to answer a question: sufficient, insufficient or conflicting.", "entails": "Checks whether the evidence given (documents, messages or records) supports a claim: supported, unsupported or unclear.", "contradicts": "Checks whether two statements or records conflict: yes, no, or unclear (for example a date that can be read two ways).", "guard": "Checks a user message or outside content for prompt injection, jailbreak and data-leak attempts, and names the kind.", "spam": "Says whether a text message or email is spam or phishing, and whether it is legitimate, spam or a phishing scam.", "tool-route": "Given a tool menu written into the request (in any common format) and the conversation, picks the tool to call next, no tool, or asking the user for a missing detail.", "ci-failure": "Reads a failed automated build-and-test run (its log, the code change and the run history) and says what kind of failure it is: a real regression, a flaky test, infrastructure, a dependency change, a test that needs updating, and so on.", "base": "No listed specialist fits this request, so the general model with no specialist answers it: everyday reasoning, reading, knowledge and judging tasks, or any task that none of the listed specialists covers."}}]}'
```

Recorded answer (2026-11-03): `ci-failure` with confidence 1.0.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-router-open-v14 --revision v1.4 --local-dir adapters/router-open-v14
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

Adapter licence: **Apache-2.0**

**Qwen3.5-0.8B notice:** these weights were modified from [Qwen3.5-0.8B](https://huggingface.co/Qwen/Qwen3.5-0.8B) by the Jeff project: jeff-base is a fine-tune of Qwen3.5-0.8B, and this adapter was trained on top of it. Qwen3.5-0.8B is Copyright 2026 Alibaba Cloud and licensed under the Apache License, Version 2.0; a copy of that licence is in [`LICENSE`](LICENSE).

Jeff is an independent project, not affiliated with or endorsed by the makers of Qwen.
