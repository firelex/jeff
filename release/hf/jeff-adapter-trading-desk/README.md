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
- finance
---

# jeff-adapter-trading-desk

**Trading desk decisions.** Applies a trading desk's written rules to the current order book. Selects buy, sell or hold, trade size, venue, actions for open orders and whether to reduce risk.

A LoRA adapter for [jeff-base](https://huggingface.co/mstrasser/jeff-base) **v1.4**, Jeff's small open decision model (a fine-tune of Qwen3.5-0.8B). You give it a situation and a question with named options; it returns a calibrated probability for every option from one forward pass, with no generated text to parse.

- Adapter ID: `jeff.finance.trading-desk`, version 1.4.0; short names: `trading-desk`
- Catalogue page: [jeffhub.ai/catalogue/jeff.finance.trading-desk](https://jeffhub.ai/catalogue/jeff.finance.trading-desk)
- Code, server and SDKs: [github.com/firelex/jeff](https://github.com/firelex/jeff)
- Base it needs: [`mstrasser/jeff-base`](https://huggingface.co/mstrasser/jeff-base) at revision `v1.4`. The engine refuses an adapter trained on another base, so this adapter loads only on that revision.

## When to use it

- Your desk has written rules for entry levels, sizing, position limits, venues, order handling and risk-reduction triggers.
- Uncertain decisions should be referred to a person or a further check based on their probabilities.

## When not to use it

- You need price forecasts or a trading strategy; this adapter applies the rules provided.
- The rules are simple enough to implement directly in code.

## What it decides

What a call sends:

| Field | Required | What it holds |
|---|---|---|
| `desk` (context) | yes | The desk definition: strategy and entry rule, clip size, position limit, spread and cut-off filters, order handling, risk-off triggers, the instrument, the venues with fees and latencies, and the routing rule. |
| `market_context` (context) | yes | The stock and the day: date, opening mid price, the morning's range and spread, and whether trading is calm, normal or active. |
| `market_state` (input, fields) | yes | The live state: time, order book, spread, mid and its average, order-flow imbalance, realised volatility, recent trades, displayed size per venue, position and P&L, open orders, parent order, news, and the request. |

### `jeff.finance.trading-desk.route` (classify, choice)

Instructions: You are the order router of an automated trading desk. Send the new order described in the request line of the live market state to one of the desk's three venues, using the routing rule in the desk definition. A buy order takes the ask side and a sell order takes the bid side, so compare the order size with each venue's displayed size on that side.

Labels:

  - The caller writes the text of each option; the keys are `venue_a`, `venue_b`, `venue_c`.

### `jeff.finance.trading-desk.risk_off` (classify, choice)

Instructions: You are the risk check of an automated trading desk. Decide whether the desk must go risk-off now under the risk-off triggers in its desk definition. Going risk-off means the desk stops opening trades in this stock, cancels its orders and reduces its position. Answer yes if any one trigger is met by the live market state, otherwise no.

Labels:

  - `yes`: Yes: at least one risk-off trigger is met; go risk-off now.
  - `no`: No: no risk-off trigger is met; keep trading.

### `jeff.finance.trading-desk.direction` (classify, choice)

Instructions: You are the decision step of an automated trading desk. The desk definition states the strategy's entry rule, the clip size (the standard order size), the position limit, a maximum spread and a cut-off time for new trades. Apply them to the live market state and choose the next action. Use only these entry rules: risk-off triggers, open orders and order routing are decided separately.

Labels:

  - `buy`: Buy one clip now: the buy rule is met and no filter (spread, position limit, cut-off time, signal filter if the desk uses one) blocks it.
  - `sell`: Sell one clip now: the sell rule is met and no filter (spread, position limit, cut-off time, signal filter if the desk uses one) blocks it.
  - `hold`: Hold: no entry rule is met, or a filter blocks the trade.

### `jeff.finance.trading-desk.size` (classify, choice)

Instructions: You are the decision step of an automated trading desk. A volatility-target desk sizes its position from realised volatility and the unrealised loss on its position; an execution desk paces a client parent order against its schedule and its limit price. Apply the sizing or pacing rule in the desk definition to the live market state.

Labels:

  - `increase`: Increase: add one clip to the position, or speed up the parent order.
  - `decrease`: Decrease: cut the position by one clip, or slow down the parent order.
  - `keep`: Keep the current size or pace.

### `jeff.finance.trading-desk.order` (classify, choice)

Instructions: You manage the open orders of an automated trading desk. Decide what to do with the open order named in the request line of the live market state, using the order rules in the desk definition. 'Ticks behind' is the distance from the best price on the order's own side (the best bid for a buy order, the best ask for a sell order).

Labels:

  - `cancel`: Cancel the order.
  - `amend`: Amend the order: re-price it to the best price on its side.
  - `leave`: Leave the order as it is.

## How it was measured

Base: Jeff v1.4; adapter trained 2026-10-25.

| Test | Rows | Accuracy |
|---|--:|--:|
| the in-house test of simulated order books (generator) | 5,000 | 97.7% |

Accuracy is the share of test rows whose top answer was right. Kinds: public, a public benchmark; real, held-out real documents; generator, an in-house test from the same generator as the training data (it measures less than a public or real test does); mixed, several kinds; unknown, not recorded.

When it is confident (from the calibration set): the threshold is the confidence from which an answer counts as confident, precision the share of confident answers that were right, coverage the share of answers that were confident.

| Function | Threshold | Precision | Coverage | Calibration rows |
|---|--:|--:|--:|--:|
| `route` | 0.809 | 99.4% | 90.5% | 200 |
| `risk_off` | 1.000 | 100.0% | 100.0% | 200 |
| `direction` | 0.981 | 100.0% | 100.0% | 200 |
| `size` | 0.969 | 99.2% | 63.0% | 200 |
| `order` | 0.999 | 100.0% | 100.0% | 200 |

## How to call it

Install the Python SDK and the engine (see the [install guide](https://jeffhub.ai/docs/sdk/install); until the packages are on PyPI, install them from [github.com/firelex/jeff](https://github.com/firelex/jeff)).

The example below is "Losing long position below its average". The day's loss on the stock is beyond the limit the desk's rules set, so the answer to going risk-off is `yes`.

On your machine (`Jeff.local` downloads this adapter and the base from Hugging Face the first time):

```python
import jeff

client = jeff.Jeff.local(["trading-desk"])

decide = client.function("jeff.finance.trading-desk.risk_off", context={
    "desk": "Strategy: mean reversion on one stock. Buy when the mid is more than 0.6% below its 20-minute average, sell when more than 0.6% above.\nClip size: 500 shares. Position limit: 3,000 shares either way.\nFilters: no new trades when the spread is wider than 8 basis points, or after 15:45.\nRisk-off triggers: (1) the day's loss on this stock exceeds 25,000; (2) realised volatility over 5 minutes is above 60% annualised; (3) a trading halt or a news alert flagged \"material\" on this stock.\nVenues: venue_a (lit exchange, fee 0.3 bp, 0.4 ms), venue_b (lit exchange, rebate 0.1 bp, 1.1 ms), venue_c (dark pool, fee 0.1 bp, 2.0 ms).",
    "market_context": "Stock: a mid-cap European industrial. Date: 2 November 2026. Opening mid 41.20; morning range 40.95 to 41.60; typical spread 4 bp; trading normal.",
})

decision = decide("Time 13:12:05. Mid 40.31, 20-minute average 40.88. Spread 5 bp.\nOrder-flow imbalance -0.42. Realised volatility (5 min) 38% annualised.\nPosition: long 2,500 shares, average price 41.35. Day P&L on this stock: -26,400.\nOpen orders: none. News: none flagged.")

print(decision.value, decision.confidence)
```

Through the Jeff API (an API key from [jeffhub.ai](https://jeffhub.ai)):

```python
import os

import jeff

with jeff.Jeff.remote("https://api.jeffhub.ai", token=os.environ["JEFF_API_KEY"], timeout_s=30) as client:
    decide = client.function("jeff.finance.trading-desk.risk_off", context={
        "desk": "Strategy: mean reversion on one stock. Buy when the mid is more than 0.6% below its 20-minute average, sell when more than 0.6% above.\nClip size: 500 shares. Position limit: 3,000 shares either way.\nFilters: no new trades when the spread is wider than 8 basis points, or after 15:45.\nRisk-off triggers: (1) the day's loss on this stock exceeds 25,000; (2) realised volatility over 5 minutes is above 60% annualised; (3) a trading halt or a news alert flagged \"material\" on this stock.\nVenues: venue_a (lit exchange, fee 0.3 bp, 0.4 ms), venue_b (lit exchange, rebate 0.1 bp, 1.1 ms), venue_c (dark pool, fee 0.1 bp, 2.0 ms).",
        "market_context": "Stock: a mid-cap European industrial. Date: 2 November 2026. Opening mid 41.20; morning range 40.95 to 41.60; typical spread 4 bp; trading normal.",
    })

    decision = decide("Time 13:12:05. Mid 40.31, 20-minute average 40.88. Spread 5 bp.\nOrder-flow imbalance -0.42. Realised volatility (5 min) 38% annualised.\nPosition: long 2,500 shares, average price 41.35. Day P&L on this stock: -26,400.\nOpen orders: none. News: none flagged.")

    print(decision.value, decision.confidence)
```

```bash
curl -s https://api.jeffhub.ai/v1/decide \
  -H "Authorization: Bearer $JEFF_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"calls": [{"function": "jeff.finance.trading-desk.risk_off", "input": "Time 13:12:05. Mid 40.31, 20-minute average 40.88. Spread 5 bp.\nOrder-flow imbalance -0.42. Realised volatility (5 min) 38% annualised.\nPosition: long 2,500 shares, average price 41.35. Day P&L on this stock: -26,400.\nOpen orders: none. News: none flagged.", "context": {"desk": "Strategy: mean reversion on one stock. Buy when the mid is more than 0.6% below its 20-minute average, sell when more than 0.6% above.\nClip size: 500 shares. Position limit: 3,000 shares either way.\nFilters: no new trades when the spread is wider than 8 basis points, or after 15:45.\nRisk-off triggers: (1) the day'\''s loss on this stock exceeds 25,000; (2) realised volatility over 5 minutes is above 60% annualised; (3) a trading halt or a news alert flagged \"material\" on this stock.\nVenues: venue_a (lit exchange, fee 0.3 bp, 0.4 ms), venue_b (lit exchange, rebate 0.1 bp, 1.1 ms), venue_c (dark pool, fee 0.1 bp, 2.0 ms).", "market_context": "Stock: a mid-cap European industrial. Date: 2 November 2026. Opening mid 41.20; morning range 40.95 to 41.60; typical spread 4 bp; trading normal."}}]}'
```

Recorded answer (2026-11-03): `yes` with confidence 1.0.

To download the weights yourself:

```bash
hf download mstrasser/jeff-base --revision v1.4 --local-dir jeff-base
hf download mstrasser/jeff-adapter-trading-desk --revision v1.4 --local-dir adapters/trading-desk
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
