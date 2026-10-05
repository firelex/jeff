<p align="center">
  <a href="https://jeffhub.ai"><img src="assets/jeff-logo.png" width="200" alt="Jeff"></a>
</p>

<h1 align="center">Jeff</h1>

<p align="center"><b>Millisecond decisions. Any domain.</b></p>

<p align="center">
  <a href="https://jeffhub.ai"><b>jeffhub.ai</b></a> ·
  <a href="https://huggingface.co/mstrasser">Hugging Face</a> ·
  <a href="#nine-adapters">Adapters</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#changelog">Changelog</a>
</p>

<!-- Sources of the numbers: the comparison with Qwen3.8-27B and every adapter result from jeff-reference-app
     results/jeffhub.json (also on jeffhub.ai); base-model scores from ~/jev/runs/eval/**/{0.8b-20260929-2258,
     2b-20260930-2347}-final-calibrated.json (v1.2) and {0.8b-20260929-0834,2b-20260929-1118}-final-calibrated.json (v1.1)
     on the training machine; sizes: model.safetensors of the v1.2 base is 1,706,027,688 bytes and the legal-clauses
     adapter 41,459,776 bytes (adapter weights + readout). -->

> **Community preview: Jeff v1.2 and nine adapters (1 October 2026).** Try them and tell us what works
> ([issues](https://github.com/firelex/jeff/issues)). A stable long-term-support base, **v1.3**, is due in about 36
> hours; the official adapters will be retrained on it shortly after. Adapters don't carry over between base versions,
> but data sets do: build yours to the [data guidelines](https://jeffhub.ai/docs) and it carries over too.

## Put Jeff in front of your 27B

Jeff is a 0.8B open "System 1" model. Let a strong local model such as Qwen3.8-27B do the writing and planning, and let
Jeff make the quick decisions in front of it: Jeff answers first, and only when it is unsure does the query go on to
the 27B. Across 8 adapters\*, with the same test rows both ways:

| | Qwen3.8-27B makes every decision | Jeff + adapters decide, the 27B only when Jeff is unsure |
|---|---:|---:|
| **Accuracy** (mean of 8 adapters\*) | 86.6% | **95.3%** |
| **Time per decision** (mean) | 8.1 s | **0.25 s: 38× faster** |
| **Wrong answers** | 13.4% | **4.7%: 2.8× fewer** |
| **Memory** | 28.6 GB | **+1.96 GB** for Jeff with all nine adapters loaded (+6.9%) |

On the five decisions an inbox agent makes for every message (guard, triage, support intent, tool choice, grounding)
alone: **87.7% → 95.7%, 39× faster.**

| Task | 27B alone | Jeff + adapter | Passed on to the 27B | Faster |
|---|---:|---:|---:|---:|
| **guard:** prompt injection and jailbreaks | 84.0%<br>3.9&nbsp;s | **98.0%**<br>0.10&nbsp;s | 0.0% | 38× |
| **triage:** urgency and sentiment | 81.3%<br>3.6&nbsp;s | **91.0%**<br>0.06&nbsp;s | 0.0% | 59× |
| **support-intents:** what the customer wants | 86.0%<br>6.4&nbsp;s | **95.3%**<br>0.12&nbsp;s | 0.0% | 55× |
| **tools:** which tool an agent should call | 90.3%<br>11.3&nbsp;s | **98.0%**<br>0.31&nbsp;s | 0.0% | 36× |
| **ground:** is the answer supported by the sources? | 96.7%<br>13.2&nbsp;s | 96.3%<br>0.66&nbsp;s | 1.7% | 20× |
| **nav:** voice commands to on-screen items | 91.3%<br>7.2&nbsp;s | **97.0%**<br>0.21&nbsp;s | 0.0% | 35× |
| **spam:** spam and phishing | 88.0%<br>2.7&nbsp;s | **98.7%**<br>0.07&nbsp;s | 0.0% | 37× |
| **legal-clauses:** contract clause types | 75.0%<br>16.5&nbsp;s | **87.8%**<br>0.47&nbsp;s | 0.0% | 36× |
| emotion\*: the strongest of 27 emotions, or neutral | 35.6%<br>4.8&nbsp;s | **60.6%**<br>0.11&nbsp;s | 0.0% | 42× |

\*Emotion is left out of the averages: picking the single strongest of 27 emotions (or neutral) in short Reddit
comments is hard even for people, and the human labels often disagree. Jeff + adapter scores 60.6% there against the
27B's 35.6%, at 42× the speed. Including it, the average across all nine adapters is 91.4% for Jeff + adapters
against 80.9% for the 27B, so leaving it out makes the gain shown above smaller, not larger.

**How this was measured.**
- **Setup:** an Apple M4 Max with 128 GB, both models on MLX, one at a time. Qwen3.8-27B in 8-bit, prompted to answer
  directly (step-by-step reasoning off).
- **Sample:** each task uses a fixed random sample of its held-out test set (300 rows; 500 for emotion and
  legal-clauses).
- **Times:** the mean per query from prompt to answer. When a query is passed on, Jeff's time and the 27B's both count.
- **Threshold:** each adapter has its own confidence threshold; below it, the 27B answers too and its answer is used.
  The threshold is the fastest that still beats the 27B by at least one point on the task's separate calibration
  rows, fixed before the test rows were scored.
- **Ground** is the one task where the 27B is strong. Jeff passes on its least sure 1.7% and ends up level with it
  (96.3% against 96.7%, one question in 300) at 20× the speed.
- **Jeff's memory** was measured on an RTX PRO 6000 with all nine adapters loaded, switching adapter on every request
  (30 ms per decision, median).

Every number, with its source, is on [jeffhub.ai](https://jeffhub.ai/results), and the comparison can be rebuilt with
the [reference app](https://github.com/firelex/jeff-reference-app).

## Nine adapters

Each adapter is a LoRA add-on of about 41 MB for Jeff-Qwen3.5-0.8B v1.2, trained in one epoch on one GPU in half an
hour to four hours. Results on each adapter's full held-out test set (accuracy, calibration error in brackets; the
adapters never saw these rows):

| Adapter | What it decides | Test rows | Qwen3.5-0.8B untrained | Jeff v1.2 alone | **Jeff v1.2 + adapter** |
|---|---|---:|---:|---:|---:|
| [guard](https://jeffhub.ai/adapters/guard) | Prompt-injection guard | 6,552 | 43.7% | 46.9% | **98.4%** (0.004) |
| [triage](https://jeffhub.ai/adapters/triage) | Support ticket triage | 7,256 | 44.4% | 67.1% | **91.8%** (0.009) |
| [support-intents](https://jeffhub.ai/adapters/support-intents) | Customer request intents | 5,577 | 33.9% | 85.1% | **96.8%** (0.006) |
| [tools](https://jeffhub.ai/adapters/tools) | Agent tool choice | 5,157 | 18.0% | 57.8% | **97.9%** (0.004) |
| [ground](https://jeffhub.ai/adapters/ground) | Passage re-ranking and answer grounding | 4,160 | 28.7% | 49.0% | **97.0%** (0.012) |
| [nav](https://jeffhub.ai/adapters/nav) | Voice navigation | 3,300 | 12.6% | 23.8% | **97.0%** (0.005) |
| [emotion](https://jeffhub.ai/adapters/emotion) | Emotion in short comments | 5,408 | 12.5% | 32.2% | **60.6%** (0.020) |
| [spam](https://jeffhub.ai/adapters/spam) | Spam and phishing in SMS and email | 3,603 | 59.4% | 72.3% | **98.4%** (0.008) |
| [legal-clauses](https://jeffhub.ai/adapters/legal-clauses) | Contract clause types | 9,895 | 12.5% | 66.0% | **85.7%** (0.011) |

Weights and test sets: `mstrasser/Jeff-Qwen3.5-0.8B-<adapter>` on Hugging Face. Each adapter's page on
[jeffhub.ai](https://jeffhub.ai) shows where it goes wrong, how sure it is when it is right, its data and its QA
report. **Build your own** with the [adapter kit](examples/adapter-kit): the checks we ran on all nine (format,
splits by group, leaks, and the shortcuts that sank our own first drafts).

## What Jeff is

You describe a situation and list the options in plain words; Jeff returns a calibrated probability for each option
from a single forward pass. No generated text, no parsing. The options can be anything (support queues, intents,
moderation labels, voice commands, tools), and they don't need to appear in the training data: that is the zero-shot
base model. **Adapters** add near-perfect accuracy on one job each; **you pick the ones you need**, and any request that
names no adapter goes to the untouched base. It's a small model: fast, well-calibrated choices between options, not
multi-step reasoning.

## Quick start

```bash
git clone https://github.com/firelex/jeff && cd jeff
uv sync --no-default-groups --extra lora          # add --extra cuda on NVIDIA GPUs, --extra mac on Apple silicon
uv run --no-default-groups hf download mstrasser/Jeff-Qwen3.5-0.8B --revision v1.2 --local-dir Jeff-Qwen3.5-0.8B-v1.2
for name in guard tools ground; do               # the adapters you want, one folder each
  uv run --no-default-groups hf download mstrasser/Jeff-Qwen3.5-0.8B-$name --local-dir adapters/$name
done
JEFF_CHECKPOINT=Jeff-Qwen3.5-0.8B-v1.2 JEFF_ADAPTERS=adapters PORT=8765 \
  uv run --no-default-groups --extra lora jeff-serve       # add JEFF_BACKEND=mlx and --extra mac on Apple silicon
```

Overlapping `/v1/systemone` requests get `529` by default. Set `JEFF_QUEUE_MS` (milliseconds) to wait for the decision lock instead of failing immediately.

```python
from jeff import Client
from jeff.client import choice_question, yes_no_question

jeff = Client("http://localhost:8765", model="jeff-latest")   # the plain base; jeff.with_model("guard") for an adapter
answers = jeff.ask("The parcel arrived crushed and I want my money back.", {
    "team": choice_question({"refunds": "Refunds and payments", "parcels": "Damaged or lost parcels",
                             "login": "Account and login problems"}, "Which team should handle this ticket?"),
    "angry": yes_no_question("Is the customer angry?"),
})
answers.choice("team").key    # "parcels", with its probability
```

Each answer has a probability per option, the chosen option and a confidence. Question types: `choice` (up to 254
options), `noul` (yes/no) and `score` (a point on a scale). The HTTP API, the TypeScript client
([clients/typescript](clients/typescript)), adding adapters without a restart and every option are in the
[docs on jeffhub.ai](https://jeffhub.ai/docs). Two rules matter: never use bare numbers as option keys, and put the
unchanging parts of a request first and the changing field last.

## Data

The base models' synthetic training data was written by an open model, Qwen3.8-Flash-Next, on two DGX Sparks; some
public data sets in the mix contain text their authors generated with closed models (for example RAGTruth's model
responses). Most of the five generated adapters' data (`ground`, `guard`, `tools`, `nav`, `triage`) was written by
Qwen3.8-Max through Alibaba Cloud's hosted API; every row records which model wrote and checked it, and each adapter's
card gives the counts. Every data set went through a shortcut check and an independent review before training. We
publish weights, code and each adapter's test and calibration sets; not the training data. Sources and licences:
[docs/data-sources.md](docs/data-sources.md).

**Independent project.** Jeff uses the same request format as Jev, but is not affiliated with or endorsed by TypeSafe,
the makers of Jev. Our training code starts from the open-source [AutoJev](https://github.com/denis-pplx/autojev)
recipe.

## Benchmarks

4,599 questions from five public benchmarks, plus JevBench's public hard tier (105 items, scored separately). The Qwen
models are v1.2, Jeff-Gemma4-E2B v1.0:

| Benchmark | Qwen3.5-0.8B untrained | Jeff-Qwen3.5-0.8B | Qwen3.5-2B untrained | Jeff-Qwen3.5-2B | Gemma 4 E2B untrained | Jeff-Gemma4-E2B | Jev (published) | AutoJev-27B (published) |
|---|---|---|---|---|---|---|---|---|
| **Overall (5 benchmarks)** | 45.3 | 78.7 | 46.5 | 81.7 | 62.5 | 81.6 | **83.0** | ***84.9*** |
| BBH | 39.5 | 63.2 | 46.0 | 66.4 | 51.3 | 66.4 | **94.3** | 82.8 |
| Financial PhraseBank | 36.0 | **96.3** | 53.4 | **95.6** | 86.0 | **96.1** | 77.0 | 84.2 |
| JudgeBench | 56.6 | 60.9 | 57.4 | 62.0 | 46.9 | 60.6 | **78.6** | ***78.9*** |
| RAGTruth | 49.1 | **85.5** | 35.9 | **85.5** | 63.8 | **87.4** | 77.3 | ***88.9*** |
| WinoGrande | 49.2 | 68.7 | 52.2 | 80.7 | 51.0 | 77.4 | **90.7** | 83.3 |
| JevBench hard (separate) | 36.2 | 44.8 | 45.7 | 57.1 | 41.0 | 48.6 | **73.3** | 70.3 |

**Bold:** the winner of Jeff against Jev in each row. ***Bold italic:*** AutoJev-27B where it is the best of all models
in the row; it is shown for reference, since the head-to-head comparison is
with Jev. The Qwen models are v1.2 and Jeff-Gemma4-E2B v1.0. The published Jev and AutoJev figures were measured on a different sample of the same benchmarks. Jeff's
overall score comes from classification and grounding, where it matches or beats the large models; on the
reasoning-heavy benchmarks (BBH, JudgeBench, JevBench) it stays well below them, as you would expect at this size.

## Speed and size

Median time per decision over the same 200 benchmark questions (about 200 input tokens each), one question at a time,
from raw text to probabilities:

| Model | Parameters | Weights (16-bit) | NVIDIA RTX PRO 6000 | Apple M4 Max (MLX) | CPU (32 threads) |
|---|---|---|---|---|---|
| **Jeff-Qwen3.5-0.8B** | 0.8B | 1.7 GB | **22 ms** | **28 ms** | 463 ms |
| Jeff-Qwen3.5-2B | 2B | 4.2 GB | 24 ms | 60 ms | 708 ms |
| Jeff-Gemma4-E2B | 2B effective (4.6B stored) | 9.3 GB | 29 ms | — (MLX runs Qwen only) | 1.0 s |
| AutoJev-27B | 27B | ~54 GB | not published | — | — |
| Jev | not disclosed | API only | 114–212 ms per call in published Doom runs, including the network | | |

**With adapters** (RTX PRO 6000, through jeff-serve's request path, 675 requests mixing all nine adapters' test
prompts):

| Setting | Median per decision | GPU memory |
|---|---:|---:|
| Base alone | 25.9 ms | 1.74 GB |
| Base + one adapter | 31.2 ms | 1.79 GB |
| Base + all nine adapters, switching adapter on every request | 30.0 ms | 1.96 GB |
| One adapter merged into the weights | 25.7 ms | 1.77 GB |

## Using it well

- **Reason in code, decide with Jeff.** It's a classifier, not a planner: state what each option leads to, don't ask it
  to forecast.
- **Wording matters.** Describe options consistently and in words; short descriptive keys (`"refunds"`), never bare
  numbers.
- **Ask independent questions together** in one request.
- **Use an adapter when zero-shot isn't enough,** or build your own with the [adapter kit](examples/adapter-kit).

## Train your own

Adapters: start with the [adapter kit](examples/adapter-kit) (format, splits by group, leak and shortcut checks,
replay, three-way evaluation) and `jeff-train --lora-rank 16 ...`. Base models: the full pipeline is in
[scripts/train_all.sh](scripts/train_all.sh): full-weight fine-tuning, one epoch, the final checkpoint, one fitted
temperature; the benchmark panel is never used for selection or tuning. Every source and its licence:
[docs/data-sources.md](docs/data-sources.md).

## Games and chess

**The base model decides well on things it has never seen.** As a test, it plays games zero-shot (v1.0): each turn the
code describes the situation and the legal moves in words, and Jeff picks one. Jeff-Qwen3.5-0.8B matches a hand-coded rule bot at Doom (6.55 kills) and Frogger (10.3 crossings) without ever
seeing the games, and collects 57 of 98 Pac-Man pellets (the bot: 94). Click a clip for the full video; every model's
results are on the [model card](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B).

<table><tr><td align="center" valign="top" width="33%"><a href="https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-jeff-0.8b.mp4"><img src="assets/previews/doom-jeff-0.8b.gif" width="260" alt="Jeff-Qwen3.5-0.8B playing Doom"></a><br>Doom</td><td align="center" valign="top" width="33%"><a href="https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-jeff-0.8b.mp4"><img src="assets/previews/frogger-jeff-0.8b.gif" width="260" alt="Jeff-Qwen3.5-0.8B playing Frogger"></a><br>Frogger</td><td align="center" valign="top" width="33%"><a href="https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-jeff-0.8b.mp4"><img src="assets/previews/pacman-jeff-0.8b.gif" width="260" alt="Jeff-Qwen3.5-0.8B playing Pac-Man"></a><br>Pac-Man</td></tr></table>

**Chess, as a fine-tuning example:** trained on 600,000 Lichess positions in about 3½ hours on one GPU,
[Jeff-Qwen3.5-0.8B-Chess](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B-Chess) solves 55.8% of 1,000 held-out
puzzles (zero-shot Jeff: 15.5%). It's about 1,000 Elo with no search, but each move is one forward pass, so one GPU
keeps up with about 600 blitz games at once. The scripts are in [examples/chess](examples/chess).

<a href="https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B-Chess"><img src="assets/previews/chess-game63.gif" width="720"
alt="Jeff-Qwen3.5-0.8B-Chess playing 100 blitz games at once; the featured game ends in checkmate"></a>

## Caveats

- **Adapters belong to one base.** The v1.2 adapters work only on Jeff-Qwen3.5-0.8B v1.2; the server refuses them on
  any other base. v1.3 will need retrained adapters; data sets in the documented format carry over.
- **The 27B comparison is one setup:** a fixed sample of held-out rows per task on one Mac, the 27B prompted with its
  step-by-step reasoning off.
- **Small models don't reason.** Expect fast, calibrated choices between the options you describe, not multi-step
  reasoning.
- **Option limits:** up to 254 options for the Qwen models; Jeff-Gemma4-E2B (still v1.0) only up to 26.
- **English and text only.**

## Changelog

**v1.2 (1 October 2026)**: [full release notes](https://github.com/firelex/jeff/releases/tag/v1.2)
- **Nine LoRA adapters**, served side by side on one base (`JEFF_ADAPTERS`, PyTorch and MLX), chosen per request,
  reloaded without a restart. With the 27B behind them: 86.6% → 95.3% across 8 adapters, 38× faster.
- **Cleaned training data** for Jeff-Qwen3.5-0.8B and -2B (284,747 questions): every overlap with our test sets
  removed (MAUD splits by question, so all of it went), answer-length, answer-letter and option-count shortcuts
  removed, voice navigation moved into the `nav` adapter. More honest, not smarter:

  | Test | 0.8B v1.1 | 0.8B v1.2 | 2B v1.1 | 2B v1.2 |
  |---|---:|---:|---:|---:|
  | Benchmark panel (4,599) | 79.1% | 78.7% | 82.0% | 81.7% |
  | Calibration error (ECE) | 0.021 | 0.028 | 0.026 | 0.021 |
  | Long lists v2, 20–254 options (1,886, new) | – | 93.1% | – | 93.6% |
  | Long documents (2,009) | 82.8% | 66.1% | 85.8% | 65.6% (v1.1 inflated by the leak) |
  | Voice navigation (3,324) | 95.0% | 90.2% | 95.8% | 91.4% (now zero-shot) |
  | JevBench hard (105) | 46.7% | 44.8% | 57.1% | 57.1% |

- **Python and TypeScript clients**, **answer twice** (`"orders": 2`), `score` questions in training, and the
  **adapter kit** (`jeff-kit`).
- Tried and dropped: a 0.8B distilled from the 2B scored the same as v1.2 on every test.
- **Coming: v1.3**, a long-term-support base with a fixed request format and faster serving; all adapters retrained.

**v1.1 (29 September 2026):** choices among up to 254 options (v1.0: 26; thanks @puhuk,
[#1](https://github.com/firelex/jeff/issues/1)), better calibration, the final checkpoint published, a serving-only
install (thanks @WavesMan, [#2](https://github.com/firelex/jeff/issues/2)).

**v1.0 (28 September 2026):** first release: Jeff-Qwen3.5-0.8B, Jeff-Qwen3.5-2B and Jeff-Gemma4-E2B.

## History

Jeff began as a fork of [AutoJev](https://github.com/denis-pplx/autojev) by Denis Yarats (MIT licence), an open recipe
that fine-tunes Qwen3.8-27B to return Jev-style decisions. We kept its core design (one forward pass per decision, a
trained answer readout, a fitted temperature for calibration) and built on it: small students (0.8B and 2B Qwen, Gemma
4 E2B), a local synthetic-data pipeline with a leak filter, prompt layouts for domain fine-tunes, MLX serving on Apple
silicon, game tests and a training dashboard. The original copyright notice is kept in [LICENSE](LICENSE).

## Licence

Code: MIT (including AutoJev's). Model weights: Apache 2.0. Doom harness adapted from
[jev-plays-doom](https://github.com/tirukovelamanoj/jev-plays-doom) (MIT). Training data: see the dataset card; each
source keeps its licence and is listed in [docs/data-sources.md](docs/data-sources.md). We release the weights and code, not the training data; some sources are share-alike (CC BY-SA).
