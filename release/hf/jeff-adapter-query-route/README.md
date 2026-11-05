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
- retrieval
---

# jeff-adapter-query-route

**Search source selection.** Selects which document collection, database or tool should handle a query from a menu of sources. Can also indicate that several sources or no source are needed.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.retrieval.query-route`, version 1.4.0; short names: `oss-query-route`, `query-route`
- Catalogue page: [jeffhub.ai/catalogue/jeff.retrieval.query-route](https://jeffhub.ai/catalogue/jeff.retrieval.query-route)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your assistant can search several sources and needs to choose where to look.

## When not to use it

- You need to rewrite the query for a source; this adapter only selects the source.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `menu` (context) | yes | The sources a query can be sent to (document collections, indexes, databases, tools), written as tool definitions with names and descriptions. |
| `query` (input, fields) | yes | The user's query. |

### `jeff.retrieval.query-route.source` (route, choice)

Instructions: Which of the sources on the menu should this query be sent to? Each source (a document collection, index, database or tool) is described on the menu. Choose the one source whose contents answer the query; judge by what each source holds, not by words the query shares with a source's name. none: no source on the menu holds what the query needs (it belongs to a source that is not listed), or the message needs no lookup at all (a greeting, thanks, or a request to reword text). several: answering needs facts from two or more different sources on the menu, because no single source holds all of them.

Labels:

  - The caller gives the labels (at most 13), each a key and its text.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test (generator) | 2,000 | 99.3% |
| a second in-house test of unseen cases (generator) | 2,000 | 98.9% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `source` | 0.343 | 98.7% | 100.0% | 2,000 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Heat pump warranty question". A question about a product's warranty goes to the `warranty_terms` source.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["query-route"])

decide = client.function("jeff.retrieval.query-route.source", context={
    "menu": "- product_manuals: installation and user manuals for every heat pump and boiler the company sells\n- warranty_terms: the warranty terms for each product, by model and installation date\n- customer_orders: each customer's orders, installations and visits (a database)\n- pricing: current prices and offers",
}, labels={
    "product_manuals": "Installation and user manuals for every heat pump and boiler the company sells",
    "warranty_terms": "The warranty terms for each product, by model and installation date",
    "customer_orders": "Each customer's orders, installations and visits (a database)",
    "pricing": "Current prices and offers",
    "none": "No source on the menu holds what the query needs, or no lookup is needed",
    "several": "The answer needs facts from two or more of these sources",
})

decision = decide("How long is the warranty on an Aster 2 heat pump installed after March 2026?")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.retrieval.query-route.source", context={
        "menu": "- product_manuals: installation and user manuals for every heat pump and boiler the company sells\n- warranty_terms: the warranty terms for each product, by model and installation date\n- customer_orders: each customer's orders, installations and visits (a database)\n- pricing: current prices and offers",
    }, labels={
        "product_manuals": "Installation and user manuals for every heat pump and boiler the company sells",
        "warranty_terms": "The warranty terms for each product, by model and installation date",
        "customer_orders": "Each customer's orders, installations and visits (a database)",
        "pricing": "Current prices and offers",
        "none": "No source on the menu holds what the query needs, or no lookup is needed",
        "several": "The answer needs facts from two or more of these sources",
    })

    decision = decide("How long is the warranty on an Aster 2 heat pump installed after March 2026?")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.retrieval.query-route.source", "input": "How long is the warranty on an Aster 2 heat pump installed after March 2026?", "context": {"menu": "- product_manuals: installation and user manuals for every heat pump and boiler the company sells\n- warranty_terms: the warranty terms for each product, by model and installation date\n- customer_orders: each customer'\''s orders, installations and visits (a database)\n- pricing: current prices and offers"}, "labels": {"product_manuals": "Installation and user manuals for every heat pump and boiler the company sells", "warranty_terms": "The warranty terms for each product, by model and installation date", "customer_orders": "Each customer'\''s orders, installations and visits (a database)", "pricing": "Current prices and offers", "none": "No source on the menu holds what the query needs, or no lookup is needed", "several": "The answer needs facts from two or more of these sources"}}]}'
```

Recorded answer (2026-11-03): `warranty_terms` with confidence 0.981.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-query-route --revision v1.4 --local-dir adapters/query-route
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
