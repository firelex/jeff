# Adapter kit: check your training data before you train

An adapter is a small add-on (LoRA) that teaches Jeff one job, such as routing support messages. Its quality depends
almost entirely on its training data. When we built nine adapters ourselves, every generated data set failed our own
review at least once. The reason was nearly always a **shortcut**: a surface feature that gives the answer away
without understanding the input. A model learns the shortcut, scores well on a test set built the same way, and fails
on real inputs.

`jeff-kit` is a set of command-line checks for that. It does not generate data, call any language model or download
anything, and it needs nothing beyond the `jeff` package itself. Run it from the repository root after `uv sync`.

## The steps

Every command below runs on the invented sample in [`sample/`](sample/) (60 rows, 8 companies) and writes to
`runs/kit/`. Use your own files in place of the sample's.

### 1. `check-rows`: is every row valid?

```bash
uv run jeff-kit check-rows examples/adapter-kit/sample/rows.jsonl
```

Each line of a rows file is one JSON object:

```json
{"id": "sample-01-1", "suite": "support-triage", "family": "company-01",
 "state": {"company": "Bramble Teas", "channel": "email", "message": "I returned the order two weeks ago ..."},
 "question": {"type": "choice", "instructions": "Which team should handle this message?",
              "criteria": {"refund": "Refunds, double charges and money back", "delivery": "Shipping, tracking and delivery problems",
                           "account": "Logging in, passwords and account settings", "other": "Not for any of these teams"}},
 "label": "refund", "target": "refund", "source": {"dataset": "adapter-kit-sample", "round": "round-1"}}
```

- **Good:** `60 rows, 8 families: all rows valid`.
- **Fails when:** a field is missing (`id`, `suite`, `family`, `state`, `question`, `label`, `target`, `source`); the
  label is not one of the option keys; an option key is a bare number (`"1"`, which JavaScript silently reorders);
  two rows share an id; a family is empty; or a text still holds a template slot such as `{{Order Number}}`. The
  error names each row (its line and id) and the problem. Fix the rows; nothing is fixed for you.
- **Family** groups rows that belong together: the same company, app or document. The test set holds out whole
  families, so pick it to match what "new" means for your job (a new company, a new app).

### 2. `split`: hold out whole families

```bash
uv run jeff-kit split examples/adapter-kit/sample/rows.jsonl --out runs/kit/split \
  --test 0.25 --development 0.125 --calibration 0.125 --seed 1
```

Writes `train.jsonl`, `development.jsonl` (for choosing a checkpoint), `calibration.jsonl` (for fitting the
temperature that makes the probabilities honest) and `test.jsonl`, and prints the rows, families and label shares of
each. No family is in two files. Shares are of rows and are met as closely as whole families allow; with real data
use about 0.1 each and many more families.

- **Good:** each split's label shares are close to the others'.
- **Fails when:** the shares add up to 1 or more, or there are too few families for one per split. If label shares
  differ a lot, you have too few families: add families, or try another seed and say so.

### 3. `leak-check`: no test row in training

```bash
uv run jeff-kit leak-check --train runs/kit/split/train.jsonl \
  --against runs/kit/split/test.jsonl runs/kit/split/development.jsonl runs/kit/split/calibration.jsonl
```

Fails (exit code 1, listing each training id and the evaluation id it copies) when a training row has the same state
as an evaluation row (at any length, so short voice commands count), the same candidate answer in its options, a
near copy of a long text (20+ words, 85% of 5-word sequences shared), or the same family. Text that 20 or more
evaluation rows share, such as a fixed prompt, is ignored, and so is option text found in two or more evaluation
rows (a label such as a team name, not row content). `--report leaks.json` saves the full list.

- **Good:** `leak check passed`.
- **When it fails:** delete the listed training rows (or move whole families), then run it again. Never fix a leak by
  editing the test.

### 4. `shortcut-report`: can a model cheat?

```bash
uv run jeff-kit shortcut-report --train runs/kit/split/train.jsonl --test runs/kit/split/test.jsonl \
  --out runs/kit/report.md --class-field source.round
```

Writes a Markdown report and prints its flags. The checks:

1. **Label balance** in train and test, and the score of always answering the most common label.
2. **The correct option's position and length**: how often it is the first, last, longest or shortest option,
   against chance.
3. **Models that see only surface features** (length, word count, punctuation, case, digits, state size, option
   count), trained on train and scored on test, for all labels together and for each label against the rest. Plus
   an **option picker** that sees only each option's position, length and shape.
4. **Words and punctuation by label**: filler words (um, uh), question words, question marks, articles, end
   punctuation and case; and phrases mostly found in one label.
5. **Near duplicates and split separation**: shared families, identical prompts with different answers, and test
   rows with a near duplicate in train.
6. **Kinds of row** (with `--class-field`, for example `source.round` or `source.generator`): each kind's label
   mix, and whether surface features tell the kinds apart. Both together are the "one generation round had its own
   style and its own label mix" shortcut.
7. **Leftovers and personal data**: template slots, refusals, chat preambles, control tokens, email addresses and
   phone numbers.

`--text-field NAME` picks the state field holding the main text; the default is the state's last field (or the state
itself when it is a string).

**How flags are decided.** A rate or model is flagged when it **beats chance clearly**: at least 10 percentage points
above chance *and* at least 3 standard errors above it, so a small test set needs a bigger lead before it flags.
Word and punctuation gaps are flagged at 15 points or more, a ratio of at least 1.8 and 3 standard errors; label
balance at more than 5 points between train and test; near duplicates when more than 2% of test rows have one in
train. The report ends with the full list, and the numbers are constants at the top of
[`src/jeff/kit/shortcuts.py`](../../src/jeff/kit/shortcuts.py).

- **Good:** few or no flags, and each remaining one is the meaning of a label (a "question" label should have more
  question marks).
- **When a flag is a shortcut:** change the data, not the check. Make the feature equal across labels: write long and
  short examples for every label, drop filler words evenly, shuffle option order, give every round the same label mix.
  Then split, leak-check and report again. Read 20 to 50 rows by hand as well; the report finds patterns, not wrong
  labels.

**The sample's planted shortcut.** In `sample/rows.jsonl` every refund message is two or three sentences long and
every other message is one short line. The report flags it:

```
- A model that sees only the length of the main text (characters and words) tells 'refund' from the other labels
  with 100.0% balanced accuracy on test, against 50.0% by chance (z = 3.4).
- A model that sees only the form of the main text (length, punctuation, case, digits) tells 'refund' from the
  other labels with 100.0% balanced accuracy on test, against 50.0% by chance (z = 3.4).
- 'has an article (a, an, the)' differs by label: refund 100.0% against account 12.5%. ...
```

The third flag is the same shortcut seen another way: long messages almost always contain "the" or "a". The fix
would be refund requests that are short ("refund please, it arrived broken") and other requests that are long.

### 5. `replay-mix`: keep general skills (optional)

```bash
uv run jeff-kit replay-mix --train runs/kit/split/train.jsonl --replay examples/adapter-kit/sample/replay.jsonl \
  --protect runs/kit/split/test.jsonl runs/kit/split/development.jsonl runs/kit/split/calibration.jsonl \
  --share 0.10 --out runs/kit/train-with-replay.jsonl --seed 1
```

Training on one job can wear away what the model knew. Mixing in about 10% of rows like the base model's own
training data ("replay") limits that. The command draws `--share` x (training rows) rows from your replay file, in
proportion to its suites and source data sets, skips any that overlap the protected files, and writes training and
replay rows together in a seeded random order.

**Jeff ships no replay data yet.** An official replay set, a sample of the base model's own training data limited to
sources whose licences allow redistribution, is planned to ship with the v1.3 base. Until then, `sample/replay.jsonl`
is 20 invented rows that only show the format. Supply your own file (for example rows from public data sets of the
kinds Jeff was trained on; see [docs/data-sources.md](../../docs/data-sources.md)), or skip this step and train on your
rows alone. Run
`leak-check` again on the mixed file.

### 6. `evaluate`: three measurements on the same test set

```bash
uv run jeff-kit evaluate --test runs/kit/split/test.jsonl --base checkpoints/jeff-0.8b \
  --adapter checkpoints/jeff-0.8b-myadapter-merged --out runs/kit/eval \
  --untrained Qwen/Qwen3.5-0.8B --untrained-revision <40-character commit> --calibration runs/kit/split/calibration.jsonl
```

Runs `jeff-evaluate` three times, one model at a time, and prints accuracy and calibration error for each:

```
model                      rows  accuracy  calibration error (ECE)
untrained base model        ...
Jeff alone                  ...
Jeff with the adapter       ...
```

- **Untrained base model:** the base model before any Jeff training (`--untrained` and its exact
  `--untrained-revision`). `--calibration` fits its temperature first; without it the raw probabilities are scored.
  Leave both out to skip this measurement.
- **Jeff alone:** `--base`, a Jeff checkpoint folder (with `decision_config.json`).
- **Jeff with the adapter:** `--adapter` must also be a full decision checkpoint. `jeff-evaluate` cannot load a LoRA
  folder (`adapter_config.json` and adapter weights) by itself yet, and the kit refuses one with that message. Loading
  adapter folders directly comes with Jeff's LoRA support (the `lora` extra), which is on its way to the main branch;
  this command will then accept an adapter folder as it is. Until then, measure with a full checkpoint that already
  includes the adapter's weights and its answer readout.
- **Good:** the adapter beats Jeff alone clearly on accuracy, with calibration error no worse. If the untrained base
  already does as well, the job may not need an adapter. If the adapter scores far above what your shortcut report
  allows you to believe, look for a shortcut or leak the report missed.

## The lessons behind the checks

| Lesson | One-line example | Caught by |
|---|---|---|
| The correct option was more often the longest one | the right team's description was the one with extra detail | report, section 2 |
| Length alone predicted the answer | questions were much longer than commands | report, section 3 |
| One generation round had its own style and label mix | round 4's requests ended in "now" and were mostly "none of these" | report, section 6 (`--class-field`) |
| Filler words were missing from one kind of request | "um" and "uh" in navigation requests, never in questions | report, section 4 |
| Template slots were left unfilled | "Your order {{Order Number}} has shipped" | `check-rows`, report section 7 |
| A corpus leaked personal data | a real recipient's email address in a message | report, section 7 |
| Test families must be unseen | the same company in train and test | `split`, `leak-check` |
| No test row, or near copy, in training | "open settings" in both train and test | `leak-check` |
| Keep general skills | about 10% replay rows | `replay-mix` |
| Measure three ways on one test set | untrained base, Jeff alone, Jeff with the adapter | `evaluate` |
