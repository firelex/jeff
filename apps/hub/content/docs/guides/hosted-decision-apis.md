---
title: Jeff vs. hosted decision APIs
---
# Jeff vs. hosted decision APIs

> **Dated results.** Everything on this page was measured on 17 October 2026, with the Jeff adapters published at
> that time and the versions of the other services named below. These are not results of the current v1.4 adapters.

This page reports one comparison of Jeff with two hosted decision APIs: TypeSafe's Jev and OpenAI's Decisions API.
Both are hosted services that answer typed questions with probabilities, as Jeff does.
Jev and OpenAI's Decisions API were accessed through their public APIs for evaluation only; their outputs were not
used for training.

## What was measured

Accuracy, calibration, latency and cost, on the same 4,500 rows: nine decision tasks, 500 rows each.

| System | What it is | How it was asked | Version |
| - | - | - | - |
| Jeff | A 0.8-billion-parameter open-weights model with a small adapter trained on each task's training split | Its own prompt | The adapters published at the time, full precision |
| Jev | TypeSafe's hosted decision model, zero-shot | Its native question API (Noul, Choice and Score), with each row's own instructions and criteria | `jev-1.14.0` (released 14 October 2026; TypeSafe's current stable release, `jev-latest`) on all 4,500 rows |
| OpenAI Decisions | OpenAI's hosted decision API (public beta), zero-shot | Its native `POST /v1/decisions` (predicate, choice and score) | `gpt-6-luna` on all 4,500 rows (no dated snapshot is returned) |

## Method

- Run on 17 October 2026 from a computer in the London area, on a home connection.
- Each hosted service was asked zero-shot through its own API: no examples and no training, only each row's own
  instructions and answer options. Jeff used its own prompt and a small adapter trained on each task's training
  split.
- The answer is the option with the highest returned probability. Invalid answers count as wrong: neither Jev
  version returned any; OpenAI refused 11 questions (3 guard, 1 triage and 7 tools).
- Differences between systems include paired bootstrap 95% intervals calculated on the same rows.
- Calibration error is the expected calibration error (ECE): the average gap between the probability a system gave
  its chosen answer and how often that answer was right, over 10 equal-width bins; 0 is perfect. It is computed on
  valid answers only.

## Results

| | Jeff | Jev 1.14 | OpenAI Decisions |
| - | - | - | - |
| Mean accuracy, all 9 tasks (4,500 rows) | 91.1% | 81.8% | 72.6% |
| Mean accuracy, the 5 mostly public tasks | 87.2% | 77.0% | 74.5% |
| Mean accuracy, the 4 mostly in-house tasks | 96.0% | 87.7% | 70.3% |
| Mean calibration error (ECE) | 0.016 | 0.085 | 0.115 |
| Median latency, network from London included | Not measured in this run | 209 ms | 317 ms |
| Cost per million decisions, at list price | Not measured in this run | $63 | $103 |

By task (500 rows each):

| Task | Test data | Jeff | Jev 1.14 | OpenAI Decisions |
| - | - | - | - | - |
| guard | Mixed: 115 public, 385 in-house | 98.0% | 84.4% | 81.4% |
| triage | In-house | 91.0% | 85.2% | 81.8% |
| support-intents | Public | 96.0% | 90.4% | 88.0% |
| tools | In-house | 97.0% | 90.4% | 54.0% |
| ground | Mostly public (376 public, 124 in-house) | 96.4% | 95.0% | 90.0% |
| nav | In-house | 98.2% | 90.8% | 64.0% |
| emotion | Public | 61.6% | 34.2% | 34.4% |
| spam | Mostly public (363 public, 137 in-house) | 97.0% | 91.4% | 86.4% |
| legal-clauses | Public | 84.8% | 74.0% | 73.6% |

The public test data comes from LEDGAR (legal-clauses), GoEmotions (emotion), HWU64, Bitext and SNIPS
(support-intents), SQuAD 2.0, HotpotQA and FEVER (ground), and public SMS and email spam and phishing collections
(spam). The in-house rows were made by Jeff's own data generators.

Calibration error by task:

| Task | Jeff | Jev 1.14 | OpenAI Decisions |
| - | - | - | - |
| guard | 0.011 | 0.072 | 0.112 |
| triage | 0.015 | 0.045 | 0.057 |
| support-intents | 0.008 | 0.047 | 0.034 |
| tools | 0.013 | 0.022 | 0.147 |
| ground | 0.019 | 0.030 | 0.032 |
| nav | 0.008 | 0.073 | 0.080 |
| emotion | 0.030 | 0.342 | 0.357 |
| spam | 0.017 | 0.015 | 0.051 |
| legal-clauses | 0.026 | 0.120 | 0.164 |

Jev is well calibrated on 7 of the 9 tasks. Both hosted services are substantially overconfident on emotion
and legal-clauses, the two tasks where they are least accurate. Jev 1.14 is better calibrated than Jeff on spam
(0.015 against 0.017).

## Reading the results

- **A trained specialist against general zero-shot services.** Jeff has an adapter trained on each task; Jev and
  OpenAI's Decisions API were asked with instructions only. That is how each is offered, but it is not a comparison
  of the same kind of system.
- **The in-house tasks favour Jeff.** On triage, tools, nav and most of guard, Jeff's training and test rows come
  from the same generator, which helps Jeff against any zero-shot system. The mostly public tasks are the fairer
  comparison; Jeff leads there by about 10 points over Jev.
- **One task where Jev is level.** On ground, Jev scores 95.0% against Jeff's 96.4% (−1.4 points, 95% interval of
  the gap −3.6 to +0.6), level within noise. OpenAI's
  Decisions API matches or beats Jeff on no task.
- **Emotion is hard for every zero-shot system.** It uses GoEmotions: choose the strongest of 28 emotions. The
  zero-shot services score about 34%; Jeff learned the data set's labelling from its training split and reaches 61.6%.
- **OpenAI's low scores on tools (54%) and nav (64%).** These are the tasks with the most options (up to 136 and
  200), whose values are short IDs with the text in each option's description. Re-sending the first 50 rows of each
  with the full option text as the value gave OpenAI identical scores (tools 30/50, nav 31/50 both ways), so the
  representation does not explain them.
- **Noise.** 500 rows per task, from one fixed sample: a difference under about 3 points on one task is within
  noise.
- **Jeff's calibration** comes from a rerun on exactly the same 4,500 rows on the same day, each adapter at the
  temperature it ships with; the rerun chose the same answer as the original run on every row.
- **Latency was not compared like-for-like.** The services' times are wall-clock from London over a home connection,
  network included (100 calls each, one at a time), and cannot be split into model time and network time. Jeff was
  measured separately, on a local GPU with no network: about 30 ms per decision on its serving path of that time.
  No speed ratio is calculated.
- **Cost** comes from the input-token counts the services returned, at list prices (Jev $0.042, OpenAI $0.10 per
  million input tokens; output is free on both), not from bills.
- **One date and one place.** Both services change: OpenAI's API is in beta, and Jev is updated from time to time.
  These are the versions of 17 October 2026.

## Where Jev is ahead

The same day's tests on contract clauses show where a zero-shot service does better:

- **Labels Jeff was not trained on.** On 615 paragraphs from CUAD contracts, with 34 CUAD clause types the
  legal-clauses adapter was not trained on as the options, Jev scores 85.0% and OpenAI 82.0%,
  against Jeff's 68.3% (Jev ahead by 16.7 points, interval 13.5 to 20.0). Jev is also better calibrated there (ECE
  0.036 against 0.059).
- **How many examples close that gap.** Jeff adapters trained on CUAD examples from other contracts match Jev at
  about 590 examples (95% interval 320 to 1,260); they are level within noise from 500 examples and ahead beyond
  noise at 2,000 (88.9%, +3.9 points; one training run at that size). Jev needs no examples at all.
- **Clause checks on new non-disclosure agreements.** On 1,321 clause and test pairs from 105 agreements, Jev scores
  92.0% against Jeff's 88.9%, with better recall and calibration. On the 66 agreements with
  the least near-copied text, the two are level within noise.
- **New documents with labels Jeff was trained on.** On 628 new LEDGAR provisions with the clause types the adapter
  knows, Jeff scores 78.3% against Jev's 69.9%, with a calibration error of 0.025 against
  0.135.

In these tests, Jeff was more accurate on the decisions its adapters were trained for, and Jev on labels Jeff had
not been trained on.

## Next steps

- [What are System 1 models?](/docs/guides/system-1): what Jeff's functions are and why they are fast and
  calibrated.
- [How to build](/docs/guides/how-to-build): train an adapter on your own labelled examples.
- [The catalogue](/catalogue): the adapters you can call today.
