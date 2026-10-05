<p align="center">
  <a href="https://jeffhub.ai"><img src="assets/jeff-logo.png" width="200" alt="Jeff"></a>
</p>

<h1 align="center">Jeff</h1>

<p align="center"><b>Millisecond decisions. Any domain.</b></p>

<p align="center">
  <a href="https://jeffhub.ai"><b>jeffhub.ai</b></a> ·
  <a href="https://huggingface.co/mstrasser">Hugging Face</a> ·
  <a href="#jeff-code">Jeff-Code</a> ·
  <a href="#adapters">Adapters</a> ·
  <a href="#gguf-for-llamacpp">GGUF</a> ·
  <a href="#quick-start">Quick start</a> ·
  <a href="#changelog">Changelog</a>
</p>

<!-- Sources of the numbers: the comparison with Qwen3.8-27B and every adapter result from jeff-reference-app
     results/jeffhub.json (also on jeffhub.ai); base-model scores from ~/jev/runs/eval/**/{0.8b-20260929-2258,
     2b-20260930-2347}-final-calibrated.json (v1.2) and {0.8b-20260929-0834,2b-20260929-1118}-final-calibrated.json (v1.1)
     on the training machine; sizes: model.safetensors of the v1.2 base is 1,706,027,688 bytes and the legal-clauses
     adapter 41,459,776 bytes (adapter weights + readout). -->

> **Jeff v1.3 ([changelog](#changelog)).** A new adapter-first base, 15 adapters, GGUF files for llama.cpp, and
> **Jeff-Code**: Qwen 3.8-27B coding tasks take 32% less time on average at the same pass rate. v1.3 adapters work only on the v1.3
> base; the v1.2 models stay available under their old names.

## Put Jeff in front of your 27B

Jeff is a 0.8B open "System 1" model. Let a strong local model such as Qwen3.8-27B do the writing and planning, and let
Jeff make the quick decisions in front of it. Across 8 adapters\*, with the same test rows each way:

| | Qwen3.8-27B makes every decision | **Jeff + adapter alone** | Jeff first, the 27B only when Jeff is unsure |
|---|---:|---:|---:|
| **Accuracy** (mean of 8 adapters\*) | 86.6% | **94.6%** | 95.0% |
| **Time per decision** (mean, Apple M4 Max) | 8.1 s | **0.25 s: 35× faster** | 0.46 s: 28× faster |
| **Memory** | 28.6 GB | **+1.96 GB** for Jeff with all nine adapters loaded | |

Jeff alone already beats the 27B on every task but one; with the 27B as a fallback, Jeff passes on only the questions
it is unsure about.

| Task | 27B alone | Jeff + adapter | Jeff, 27B as fallback | Passed on |
|---|---:|---:|---:|---:|
| **guard:** prompt injection and jailbreaks | 84.0%<br>3.9&nbsp;s | **98.7%**<br>0.07&nbsp;s | 98.7%<br>0.07&nbsp;s | 0% |
| **triage:** urgency and sentiment | 81.3%<br>3.6&nbsp;s | **88.7%**<br>0.11&nbsp;s | 88.7%<br>0.11&nbsp;s | 0% |
| **support-intents:** what the customer wants | 86.0%<br>6.4&nbsp;s | **94.3%**<br>0.20&nbsp;s | 94.3%<br>0.20&nbsp;s | 0% |
| **tools:** which tool an agent should call | 90.3%<br>11.3&nbsp;s | **98.3%**<br>0.32&nbsp;s | 98.3%<br>0.32&nbsp;s | 0% |
| **ground:** is the answer supported by the sources? | 96.7%<br>13.2&nbsp;s | 95.0%<br>0.43&nbsp;s | **98.0%**<br>2.09&nbsp;s | 11% |
| **nav:** voice commands to on-screen items | 91.3%<br>7.2&nbsp;s | **98.7%**<br>0.23&nbsp;s | 98.7%<br>0.23&nbsp;s | 0% |
| **spam:** spam and phishing | 88.0%<br>2.7&nbsp;s | **98.7%**<br>0.08&nbsp;s | 98.7%<br>0.08&nbsp;s | 0% |
| **legal-clauses:** contract clause types | 75.0%<br>16.5&nbsp;s | **84.8%**<br>0.58&nbsp;s | 84.8%<br>0.58&nbsp;s | 0% |
| emotion\*: the strongest of 27 emotions, or neutral | 35.6%<br>4.8&nbsp;s | **59.6%**<br>0.14&nbsp;s | 59.6%<br>0.14&nbsp;s | 0% |

\*Emotion is left out of the averages: picking the single strongest of 27 emotions (or neutral) in short Reddit
comments is hard even for people, and the human labels often disagree. Including it makes the gain larger, not smaller.

**How this was measured.**
- **Setup:** an Apple M4 Max with 128 GB, both models on MLX, one at a time. Qwen3.8-27B in 8-bit, prompted to answer
  directly (step-by-step reasoning off). These are Mac times; on an NVIDIA GPU, Jeff takes tens of milliseconds per
  decision (see [Speed and size](#speed-and-size)).
- **Sample:** each task uses a fixed random sample of its held-out test set (300 rows; 500 for emotion and
  legal-clauses), the same rows as for v1.2.
- **Times:** the mean per query from prompt to answer. When a query is passed on, Jeff's time and the 27B's both count.
- **Fallback threshold:** each adapter's threshold is the fastest that beats the 27B by at least one point on the
  task's separate calibration rows, fixed before the test rows were scored; where no threshold does (ground), the one
  with the best calibration accuracy.

Every number, with its source, is on [jeffhub.ai](https://jeffhub.ai/results), and the comparison can be rebuilt with
the [reference app](https://github.com/firelex/jeff-reference-app).

## Jeff-Code

[Jeff-Code](https://github.com/firelex/jeff-code) is a coding agent built on [Pi](https://pi.dev), with two small Jeff
adapters trained for Qwen 3.8-27B. Around every Qwen turn, Jeff takes the routine information-gathering steps itself
(read a file, list a folder, search, check the toolchain) when it is confident, and decides whether Qwen needs to think
hard on that turn. Run side by side in paired blocks against Qwen 3.8-27B alone (the same build with every Jeff
feature off, thinking at full on every turn, as plain Pi runs it), on tasks Jeff never saw in training:

- **Same quality:** 62.4% against 62.8% pass rate; paired difference −0.2 points (95% interval −2.6 to +2.1) over
  1,242 paired tasks from six benchmarks.
- **32% less time per task on average:** a task takes 0.68× the baseline's time on average (geometric mean of the
  per-task ratios, 0.64-0.72; median 0.70×). SWE-bench Verified 0.63×, SWE-rebench 0.63-0.70×, Terminal-Bench Pro
  0.63-0.66×, Harbor Index 0.71×; no clear speed-up on Terminal-Bench 2.0 (0.96×) or SkillsBench (0.91×).
- **Total time over all tasks drops less, by 14% (0.86×, 0.80-0.93):** in about 5% of tasks Jeff-Code runs more than
  30 minutes longer, because it keeps going where Qwen alone gives up (there it solved 26 to Qwen's 24).
- **Why not just turn thinking off?** Faster still, but 7.6 points worse (−10.6 to −4.5; −13.5 on Terminal-Bench
  2.0). Jeff deciding when Qwen should think is what keeps the quality.

Adapters: [jeff-adapter-code](https://huggingface.co/mstrasser/jeff-adapter-code) and
[jeff-adapter-code-router](https://huggingface.co/mstrasser/jeff-adapter-code-router). Details on
[jeffhub.ai](https://jeffhub.ai).

## Adapters

Each adapter is a LoRA add-on for the Jeff v1.3 base, trained in one epoch. Results on each adapter's
held-out test set (accuracy, calibration error in brackets; the adapters never saw these rows):

| Adapter | What it decides | Qwen3.5-0.8B untrained | Jeff v1.3 alone | **Jeff v1.3 + adapter** |
|---|---|---:|---:|---:|
| [guard](https://jeffhub.ai/adapters/guard) | Prompt-injection guard | 43.8% | 49.4% | **98.2%** (0.004) |
| [triage](https://jeffhub.ai/adapters/triage) | Support ticket triage | 44.1% | 47.6% | **91.5%** (0.015) |
| [support-intents](https://jeffhub.ai/adapters/support-intents) | Customer request intents | 33.9% | 24.2% | **96.3%** (0.003) |
| [tools](https://jeffhub.ai/adapters/tools) | Agent tool choice | 17.9% | 30.2% | **97.2%** (0.007) |
| [ground](https://jeffhub.ai/adapters/ground) | Passage re-ranking and answer grounding | 28.9% | 50.9% | **96.6%** (0.007) |
| [nav](https://jeffhub.ai/adapters/nav) | Voice navigation | 12.3% | 13.9% | **97.3%** (0.006) |
| [emotion](https://jeffhub.ai/adapters/emotion) | Emotion in short comments | 12.6% | 25.3% | **60.5%** (0.018) |
| [spam](https://jeffhub.ai/adapters/spam) | Spam and phishing in SMS and email | 59.3% | 72.2% | **98.1%** (0.007) |
| [legal-clauses](https://jeffhub.ai/adapters/legal-clauses) | Contract clause types (100 options) | 12.5% | 7.4% | **83.6%** (0.011) |
| [trading-desk](https://jeffhub.ai/adapters/trading-desk) | Trading-desk decisions under written rules | 38.6% | 41.1% | **98.1%** (0.010) |
| [aml](https://jeffhub.ai/adapters/aml) | Anti-money-laundering review under a written policy | 36.0% | 40.5% | **95.0%** (0.012) |
| [sanctions](https://jeffhub.ai/adapters/sanctions) | Sanctions name screening | 34.4% | 68.3% | **99.96%** (0.001) |
| [soc](https://jeffhub.ai/adapters/soc) | Security-alert triage against a playbook | 22.1% | 33.8% | **94.1%** (0.014) |
| [code](https://jeffhub.ai/adapters/code) | Jeff-Code: the next information-gathering step | | | see [Jeff-Code](#jeff-code) |
| [code-router](https://jeffhub.ai/adapters/code-router) | Jeff-Code: whether Qwen should think hard | | | see [Jeff-Code](#jeff-code) |

Weights: `mstrasser/jeff-adapter-<name>` on Hugging Face (revision `v1.3`), with the GGUF versions in
`mstrasser/jeff-adapter-<name>-gguf`. **Licences differ:** sanctions and soc are CC BY-NC 4.0 because of their source
data; aml and trading-desk carry data-source terms of their own. Each adapter's page on [jeffhub.ai](https://jeffhub.ai)
gives its licence, its data, where it goes wrong and how sure it is when it is right. **Build your own** with the
[adapter kit](examples/adapter-kit).

## Why a Jeff base?

v1.3 is **adapter-first**: the base is no longer tuned to compete on zero-shot benchmarks. Its prompt layout puts the
fixed part of a request (instructions and options) first and the changing input last, so the fixed part can be cached;
the price is weaker zero-shot accuracy on long, unfamiliar option lists (legal-clauses without an adapter: 66.0% on
v1.2, 7.4% on v1.3). With an adapter, v1.3 is within −0.7 to +0.3 points of v1.2 on every task except legal-clauses
(83.6% vs 85.7%). For zero-shot use without an adapter, use v1.2.

Adapters work on plain Qwen3.5-0.8B too, so why a Jeff base? We trained the same triage adapter on both:

| training examples | adapter on Jeff v1.3 | adapter on plain Qwen3.5-0.8B |
|---:|---:|---:|
| 0 (no adapter) | 47.2% | 33.8% |
| 250 | 76.4% | 76.0% |
| 500 | 79.1% | 76.5% |
| 1,000 | 80.9% | 79.6% |
| 72,000 (all) | 91.5% | 91.2% |

With plenty of data the base hardly matters; with little data, the Jeff base gives the adapter a head start, because
it already knows how to decide between listed options and give a calibrated answer.

## What Jeff is

You describe a situation and list the options in plain words; Jeff returns a calibrated probability for each option
from a single forward pass. No generated text, no parsing. The options can be anything (support queues, intents,
moderation labels, voice commands, tools), and they don't need to appear in the training data: and the base model
handles them zero-shot, though v1.3 is built to be used with adapters. **Adapters** add near-perfect accuracy on one job each; **you pick the ones you need**, and any request that
names no adapter goes to the untouched base. It's a small model: fast, well-calibrated choices between options, not
multi-step reasoning.

## Quick start

```bash
git clone https://github.com/firelex/jeff && cd jeff
uv sync --no-default-groups --extra lora          # add --extra cuda on NVIDIA GPUs, --extra mac on Apple silicon
uv run --no-default-groups hf download mstrasser/jeff-base --revision v1.3 --local-dir jeff-base-v1.3
for name in guard tools ground; do               # the adapters you want, one folder each
  uv run --no-default-groups hf download mstrasser/jeff-adapter-$name --revision v1.3 --local-dir adapters/$name
done
JEFF_CHECKPOINT=jeff-base-v1.3 JEFF_ADAPTERS=adapters PORT=8765 \
  uv run --no-default-groups --extra lora jeff-serve       # add JEFF_BACKEND=mlx and --extra mac on Apple silicon
```

```python
from jeff import Client
from jeff.client import choice_question, yes_no_question

jeff = Client("http://localhost:8765", model="triage")   # name the adapter for each request
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

## GGUF for llama.cpp

`mstrasser/jeff-base-gguf` has the base in Q8_0 and Q4_K_M; each adapter has a small LoRA GGUF (about 169 MB) in
`mstrasser/jeff-adapter-<name>-gguf`, loaded with `--lora`, so one base serves every adapter. Q8_0 is effectively
lossless (within 0.4 points of full precision); Q4_K_M stays within about half a point, with its own temperature per
format so the probabilities stay calibrated. Run Jeff-Code's router on the Q8_0 base: its decisions are close calls,
and Q4_K_M changes about 6% of them. With llama-server, list every adapter in each request with scale 1 or 0. Details:
[jeffhub.ai/docs/llama-cpp](https://jeffhub.ai/docs/llama-cpp).

## Data

The base models' synthetic training data was written by an open model, Qwen3.8-Flash-Next, on two DGX Sparks; some
public data sets in the mix contain text their authors generated with closed models (for example RAGTruth's model
responses). Most of the five generated adapters' data (`ground`, `guard`, `tools`, `nav`, `triage`) was written by
Qwen3.8-Max through Alibaba Cloud's hosted API; every row records which model wrote and checked it, and each adapter's
card gives the counts. v1.3's new adapters use public data sets converted by scripts, and synthetic data where code simulates the scenario and fixes every label, with GLM 5.3 rewording or writing the text (it never decides a label). Every data set went through a shortcut check and an independent review before training. We
publish weights, code and each adapter's test and calibration sets; not the training data. Sources and licences:
[docs/data-sources.md](docs/data-sources.md).

**Independent project.** Jeff uses the same request format as Jev, but is not affiliated with or endorsed by TypeSafe,
the makers of Jev. Our training code starts from the open-source [AutoJev](https://github.com/denis-pplx/autojev)
recipe.

## Benchmarks

Zero-shot benchmark results of the **v1.2** base models (v1.3 is adapter-first: on the same panel it scores 78.6%
against v1.2's 78.8%, but much lower on long unfamiliar option lists). 4,599 questions from five public benchmarks, plus
JevBench's public hard tier (105 items, scored separately). The Qwen models are v1.2, Jeff-Gemma4-E2B v1.0:

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

**With adapters** (RTX PRO 6000, through jeff-serve's request path, 675 requests mixing all nine original adapters'
test prompts; v1.2 and v1.3 measured back to back on the same idle GPU, 5 October 2026):

| Setting | v1.3 median per decision | v1.2 median | GPU memory |
|---|---:|---:|---:|
| Base alone | 26.6 ms | 26.2 ms | 1.74 GB |
| Base + one adapter | 31.4 ms | 30.9 ms | 1.79 GB |
| Base + all nine adapters, switching adapter on every request | 31.8 ms | 31.5 ms | 1.96 GB |
| One adapter merged into the weights | 26.7 ms | 26.3 ms | 1.77 GB |

GPU memory was measured with v1.2; v1.3 has the same size. Most of each decision is fixed overhead: a 251-token
prompt takes about 25 ms and a 2,569-token prompt about 33 ms.

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

## Games and chess (earlier releases)

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

- **Adapters belong to one base.** v1.3 adapters work only on the v1.3 base, and v1.2 adapters only on v1.2; the
  server refuses them on any other base. Data sets in the documented format carry over.
- **v1.3 is adapter-first.** Without an adapter it is much weaker than v1.2 on long, unfamiliar option lists.
- **Licences differ by adapter**; check each adapter's card (some are non-commercial).
- **The 27B comparison is one setup:** a fixed sample of held-out rows per task on one Mac, the 27B prompted with its
  step-by-step reasoning off.
- **Small models don't reason.** Expect fast, calibrated choices between the options you describe, not multi-step
  reasoning.
- **Option limits:** up to 254 options for the Qwen models; Jeff-Gemma4-E2B (still v1.0) only up to 26.
- **English and text only.**

## Changelog

**v1.3 (October 2026)**
- **Adapter-first base** (`mstrasser/jeff-base`, revision v1.3): the fixed part of a request first and the changing
  input last, so prompts can be cached; trained on the same data as v1.2.
- **15 adapters:** the nine retrained, plus trading-desk, aml, sanctions and soc, and the two Jeff-Code adapters.
- **Jeff-Code:** Qwen 3.8-27B coding tasks take 32% less time on average at the same pass rate.
- **GGUF for llama.cpp:** one base per format plus one LoRA file per adapter.
- **New names on Hugging Face:** `jeff-base`, `jeff-adapter-<name>`, and `-gguf` versions.

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
