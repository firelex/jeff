# Jeff

**Fine-tunes of Qwen3.5 and Gemma 4 for zero-shot classification: small, fast decision models you slot into your
code, with the same request format as Jev.** You describe a situation and list the options in plain words; Jeff returns a
calibrated probability for each option from a single forward pass. No generated text, no parsing: about **22 ms** per
decision on an RTX PRO 6000 and **28 ms** on an Apple M4 Max (MLX).

Zero-shot means the options can be anything: support queues, user intents, moderation labels, voice commands, game
moves. Your categories don't need to appear in the training data; you describe them, and Jeff picks.

**What it is, and what it isn't.** These are very small models. They make extremely fast, well-calibrated judgement
calls between options, and they slot easily into your local code. On benchmarks they approach, and sometimes beat, Jev;
but at this size their reasoning won't match Jev's, which runs on a much larger model. If zero-shot accuracy isn't
good enough for your purposes, a short fine-tune on your own examples takes you much further: our
voice-navigation fine-tune moved held-out accuracy from 31.7% to 95.8% in under half an hour on one GPU.

**Built entirely on local hardware.** Training on one RTX PRO 6000 workstation GPU (the 0.8B trains in about 2 hours,
the 2B in about 3.5), all synthetic training data written by an open model (Qwen3.8-Flash-Next) on two DGX Sparks,
testing on a MacBook. No cloud GPUs, and no closed-model output in the training data; a closed model was used only to
spot-check the quality of a sample of the synthetic data.

**Independent project.** Jeff uses the same request format as Jev, but it is not affiliated with or endorsed by TypeSafe, the
makers of Jev. Our training code starts from the open-source [AutoJev](https://github.com/denis-pplx/autojev) recipe.

**Models on Hugging Face:** [Jeff-Qwen3.5-0.8B](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B) · [Jeff-Qwen3.5-2B](https://huggingface.co/mstrasser/Jeff-Qwen3.5-2B) · [Jeff-Gemma4-E2B](https://huggingface.co/mstrasser/Jeff-Gemma4-E2B)

## Quick start

```bash
uv sync --extra server --no-dev
uv run hf download mstrasser/Jeff-Qwen3.5-0.8B --local-dir checkpoints/jeff-0.8b

# NVIDIA GPU or CPU (PyTorch)
JEFF_CHECKPOINT=checkpoints/jeff-0.8b PORT=8765 uv run jeff-serve
# Apple silicon (MLX, much faster on a Mac; Qwen models only)
uv sync --extra server --extra mac --no-dev
JEFF_BACKEND=mlx JEFF_CHECKPOINT=checkpoints/jeff-0.8b PORT=8765 uv run jeff-serve
```

`server` is the small inference-only dependency profile. For training, evaluation, and tests, install the full
development environment instead:

```bash
uv sync
```

```bash
curl -s localhost:8765/v1/systemone -H 'content-type: application/json' -d '{
  "model": "jeff-latest",
  "state": "Refund request: the customer says the parcel arrived crushed and wants their money back.",
  "questions": {
    "route": {"type": "choice", "instructions": "Which team should handle this?",
              "criteria": {"1": "Refunds and payments", "2": "Damaged or lost parcels", "3": "Account and login problems"}},
    "angry": {"type": "noul", "instructions": "Is the customer angry?"}
  }
}'
```

Each answer has a probability per option, the chosen option and a confidence. Three question types: `choice` (pick one
of up to 26 options with the released models; see Caveats), `noul` (yes/no, returned as a probability) and `score` (a point on a scale you describe).
Several independent questions in one request are answered together.

## Benchmarks

4,599 questions from five public benchmarks, plus JevBench's public hard tier (105 items, scored separately):

![Accuracy of Jeff-Qwen3.5-0.8B, Jeff-Qwen3.5-2B and Jeff-Gemma4-E2B against Jev's published figures, per benchmark](assets/benchmarks.png)

| Benchmark | Qwen3.5-0.8B untrained | Jeff-Qwen3.5-0.8B | Qwen3.5-2B untrained | Jeff-Qwen3.5-2B | Gemma 4 E2B untrained | Jeff-Gemma4-E2B | Jev (published) | AutoJev-27B (published) |
|---|---|---|---|---|---|---|---|---|
| **Overall (5 benchmarks)** | 45.3 | 79.1 | 46.5 | **83.1** | 62.5 | 81.6 | 83.0 | ***84.9*** |
| BBH | 39.5 | 64.0 | 46.0 | 68.0 | 51.3 | 66.4 | **94.3** | 82.8 |
| Financial PhraseBank | 36.0 | **96.4** | 53.4 | **96.3** | 86.0 | **96.1** | 77.0 | 84.2 |
| JudgeBench | 56.6 | 62.6 | 57.4 | 64.6 | 46.9 | 60.6 | **78.6** | ***78.9*** |
| RAGTruth | 49.1 | **86.1** | 35.9 | **88.9** | 63.8 | **87.4** | 77.3 | ***88.9*** |
| WinoGrande | 49.2 | 68.6 | 52.2 | 79.0 | 51.0 | 77.4 | **90.7** | 83.3 |
| JevBench hard (separate) | 36.2 | 47.6 | 45.7 | 53.3 | 41.0 | 48.6 | **73.3** | 70.3 |

**Bold:** the winner of Jeff against Jev in each row. ***Bold italic:*** AutoJev-27B where it is the best of all models
in the row (on RAGTruth, tied with Jeff-Qwen3.5-2B); it is shown for reference, since the head-to-head comparison is
with Jev. The published Jev and AutoJev figures were measured on a different sample of the same benchmarks. Jeff's
overall score comes from classification and grounding, where it matches or beats the large models; on the
reasoning-heavy benchmarks (BBH, JudgeBench, JevBench) it stays well below them, as you would expect at this size.

## Games: a zero-shot test

To test zero-shot performance on tasks unlike anything in the benchmarks, we had Jeff play three games. Games aren't
the ideal zero-shot test, since a game's state isn't typical unstructured data; but they are a common, and fun, way to
test a System 1 model. Each turn, the code describes the situation and the legal moves in words, and the model picks
one. The options state what each move leads to (Frogger: "you would be hit by a car and lose a life"; Doom: "the
nearest monster is a little to your left"), but never which move is right. Each result is 20 episodes, seed 1234; ▶
opens a video of the run's first episode.

Jeff-Qwen3.5-0.8B playing, zero-shot (the bold row in the table below; click a clip for the full video):

<table><tr><td align="center" valign="top" width="33%"><a href="https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-jeff-0.8b.mp4"><img src="assets/previews/doom-jeff-0.8b.gif" width="260" alt="Jeff-Qwen3.5-0.8B playing Doom"></a><br>Doom</td><td align="center" valign="top" width="33%"><a href="https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-jeff-0.8b.mp4"><img src="assets/previews/frogger-jeff-0.8b.gif" width="260" alt="Jeff-Qwen3.5-0.8B playing Frogger"></a><br>Frogger</td><td align="center" valign="top" width="33%"><a href="https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-jeff-0.8b.mp4"><img src="assets/previews/pacman-jeff-0.8b.gif" width="260" alt="Jeff-Qwen3.5-0.8B playing Pac-Man"></a><br>Pac-Man</td></tr></table>

| Model | Doom, kills (monster's direction in words) | | Frogger, crossings (consequences) | | Pac-Man, pellets of 98 (consequences) | |
|---|---|---|---|---|---|---|
| Random moves | −0.05 | | 0 | | 11.2 | |
| Hand-coded rule bot | 6.55 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-rule-bot.mp4) | 10.25 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-rule-bot.mp4) | 94.1 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-rule-bot.mp4) |
| Qwen3.5-0.8B, untrained | 5.0 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-untrained-0.8b.mp4) | 1.0 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-untrained-0.8b.mp4) | 25.8 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-untrained-0.8b.mp4) |
| **Jeff-Qwen3.5-0.8B** | **6.55** | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-jeff-0.8b.mp4) | **10.3** | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-jeff-0.8b.mp4) | **57.0** | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-jeff-0.8b.mp4) |
| Qwen3.5-2B, untrained | 0.55 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-untrained-2b.mp4) | 0.05 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-untrained-2b.mp4) | 72.1 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-untrained-2b.mp4) |
| Jeff-Qwen3.5-2B | −0.9 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-jeff-2b.mp4) | 6.0 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-jeff-2b.mp4) | 41.2 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-jeff-2b.mp4) |
| Gemma 4 E2B, untrained | −0.55 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-untrained-g4.mp4) | 0 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-untrained-g4.mp4) | 3.2 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-untrained-g4.mp4) |
| Jeff-Gemma4-E2B | 0.55 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/doom-jeff-g4.mp4) | 0.15 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/frogger-jeff-g4.mp4) | 53.2 | [▶](https://huggingface.co/mstrasser/Jeff-Qwen3.5-0.8B/blob/main/videos/pacman-jeff-g4.mp4) |
| Jev (published, Doom) | 6.55, told the aiming rule; −0.60 without it | | — | | — | |

Jeff-0.8B decides in 29–49 ms per move on an M4 Max; Jev's published Doom run took 212 ms per call over its API. The
two times were not measured on the same hardware. To play them yourself:

```bash
uv sync --extra games
uv run python -m jeff.games --game doom --player jeff --criteria situation --url http://127.0.0.1:8765 --video --out runs/games/doom.json
uv run python -m jeff.games --game frogger --player jeff --criteria outcomes --url http://127.0.0.1:8765 --out runs/games/frogger.json
uv run python -m jeff.games --game pacman --player rule --out runs/games/pacman-rule.json
```

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

## Using it well

- **Reason in code, decide with Jeff.** It's a classifier, not a planner. State what each option leads to ("this move
  gets you hit by a car"); asked to forecast ("a car arrives in 2 turns"), it does no better than random.
- **Wording matters enormously.** Describe options consistently: giving Frogger's goal option the same words as every
  other forward option took one episode from 15 crossings to 23.
- **Use short option keys and descriptive text:** `{"1": "Engagement letter"}`, not long IDs, which cost time and add
  nothing.
- **Ask independent questions together** in one request.
- **Fine-tune it if zero-shot isn't enough.** A voice-navigation fine-tune on ~11k app-specific examples took about half
  an hour on one GPU and moved held-out accuracy from 31.7% to 95.8%, at about 40 ms per decision on an M4 Max:
  `autojev-train --initial-checkpoint <jeff> --epochs 1 ...`.
- **Pick the size for the job.** For fast option picking the 0.8B is the sweet spot: the 2B is more cautious and plays
  the games worse, despite scoring higher on the benchmarks.

## Train your own

```bash
uv run autojev-mix ...          # build the training set (public data, synthetic data, leak filter)
scripts/train.sh RUN data/mix/public.jsonl data/mix 5e-6 40 Qwen/Qwen3.5-0.8B <revision> --epochs 1
uv run autojev-evaluate --data data/panel.jsonl --local --checkpoint checkpoints/RUN/selected --output runs/eval/RUN.json
```

The full pipeline (synthetic data from a local teacher, leak filter, learning-rate sweeps, dashboard) is described in
[scripts/train_all.sh](https://github.com/firelex/jeff/blob/main/scripts/train_all.sh), and every training source with its licence in [docs/data-sources.md](https://github.com/firelex/jeff/blob/main/docs/data-sources.md). Training recipe: full-weight fine-tuning, one epoch, batches of 256, cross-entropy over the
option letters, then one fitted temperature for calibration; checkpoints are chosen on a development set, never on the
benchmark panel. At least half of each training family follows the panel's layout conventions (formats only; no panel
item is ever trained on).

## Caveats

- **At most 26 options per question, for now.** Options are coded A–Z, then AA, AB, and so on. The largest
  training question had 19 options, so the released models never learned to pick a two-letter code: an option
  in position 27 or later is effectively never chosen, whatever it says. The server therefore refuses questions
  with more than 26 options; shortlist longer lists first. Within 26, Jeff-Qwen3.5-0.8B picked the right city
  every time in our list-lookup check (10, 19 and 26 options), while Jeff-Gemma4-E2B managed 70%, 62% and 55%.
  Retrained models that handle up to 255 options are in progress. Thanks to @puhuk for the report ([#1](https://github.com/firelex/jeff/issues/1)).
- **Small models don't reason.** Expect fast, calibrated choices between the options you describe, not multi-step
  reasoning. At 0.8B–2B parameters this holds for every model, not just Jeff.
- **Jeff-2B is a weaker game player than Jeff-0.8B.** The untrained 2B already appears more risk-averse than the
  untrained 0.8B, and our training seems to have made that worse. This needs more investigation.
- **Benchmark scores don't predict game play.** The untrained Gemma 4 E2B beats the untrained Qwen models on the
  benchmarks yet plays the games worst: right most of the time, but not reliably. Training fixed its Pac-Man (3.2 → 53.2 pellets)
  but not its Doom or Frogger.
- **Prompts matter.** Jev's own Doom prompt (a raw bearing number plus an aiming rule) does not work for any of our
  models; options that state consequences in words do.
- **English and text only.**

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
