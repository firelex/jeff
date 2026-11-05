---
title: A retrieval pipeline that knows when not to answer
---
# A retrieval pipeline that knows when not to answer

## The problem

An e-bike maker's help assistant answers customers from its own documents: manuals, warranty terms, orders and
prices. A retrieval pipeline finds passages and a language model writes the answer. When the passages do not hold
the answer, the model writes one anyway, and the customer is told something the company never promised.

## How Jeff helps

Four retrieval functions check the pipeline at each step, each in one fast call:

1. [Query routing](/catalogue/jeff.retrieval.query-route) picks the source to search.
2. [Passage relevance](/catalogue/jeff.retrieval.passage-relevant) says whether a passage bears on the question.
3. [Evidence sufficiency](/catalogue/jeff.retrieval.evidence-sufficient) says whether the passages kept are enough
   to answer. When they are not, the assistant says "I don't know" instead of guessing.
4. [Grounding check](/catalogue/jeff.retrieval.ground) checks the written answer against the passages before it is
   sent.

## The solution

Each step below is a live call; the answer under it is the one the API returned. Choose your language and whether to
run it on your machine (free) or through the API.

### Pick the source

A customer asks whether the warranty on their bike still covers the battery after two years:

<!-- example: worked-retrieval-route -->

`warranty_terms`: the question is about the warranty, so the pipeline searches the warranty terms.

### Keep the passages that bear on the question

The search returns two passages: the Volt 2's warranty terms, and a passage on charging. The charging passage does
not answer the question:

<!-- example: worked-retrieval-relevant-no -->

`no`: the pipeline drops it. For the warranty passage, the one that does answer, passage relevance also answered
`no` (0.62, recorded on 4 November 2026), even after the passage was written more plainly. So the pipeline does not
drop a passage on this function's answer alone: a passage it rejects goes to your language model to confirm before
it is dropped.

### Answer, or say "I don't know"

With the warranty passage kept, the evidence is enough:

<!-- example: worked-retrieval-sufficient -->

`sufficient`: the assistant writes an answer from it.

A second customer asks whether the warranty covers a battery damaged when the bike was left out in the rain. The
passages say nothing about water damage:

<!-- example: worked-retrieval-insufficient -->

`insufficient`: the assistant says it does not know and offers to pass the question to the service team, instead of
guessing.

### Check the answer before it is sent

The assistant's answer to the first customer stays within the warranty passage:

<!-- example: worked-retrieval-ground-supported -->

`supported`, but with confidence 0.55, below the function's threshold of 0.63. Here the decision abstains, so with an
escalation policy the question goes to your language model or a person before the answer is sent.

A different draft adds a promise the warranty terms do not make:

<!-- example: worked-retrieval-ground-unsupported -->

`partly_supported`: the three-year cover is in the passage, the free next-day replacement is not. Send the draft back
to be rewritten without it.

## What you get

- An assistant that says "I don't know" when its documents do not hold the answer, and that checks each answer
  before it goes out.
- Confidence on every step, so an unsure answer (like the grounding check at 0.55 above) is escalated rather than
  trusted.
- How the functions measure, from each adapter's page. Each has a public-benchmark figure below its in-house one:
  passage relevance 83.9% on SQuAD 2.0 (2,000 rows) against 99.5% in-house, and evidence sufficiency 93.0% on
  HotpotQA (2,000 rows) against 98.4% in-house. Query routing scores 99.3% on its in-house test (2,000 rows, from the
  same generator as the training data), and the grounding check 96.6% (4,160 rows, mixed sources). Expect the public
  figures, not the in-house ones, on documents unlike the training data.
