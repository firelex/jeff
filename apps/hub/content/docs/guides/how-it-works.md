---
title: How Jeff works
---
# How Jeff works

Jeff returns typed values that code can use directly. Each answer comes from
a **function**, one trained decision with a fixed set of possible answers, and Jeff returns a probability for each of
those answers rather than generated text.

Jeff processes natural language without generating text. It uses a small language model whose output probabilities are
read in a single step: the function's answers are labelled A, B, C and so on, and Jeff's probability for each label
becomes that answer's probability. Nothing is sampled and nothing is parsed.

## Functions and interfaces

A function has an ID such as `jeff.safety.guard.attempt`: the adapter it belongs to (`jeff.safety.guard`) and the
function's own key (`attempt`). It takes:

- an **input**: the one thing that changes from call to call, such as a message, an alert or a clause;
- a **context**: a few named fields that stay the same across many calls, such as the company the message was sent
  to or where the text came from (a few functions take none);
- for some functions, **labels**: the answers to choose between, when they are yours (your teams, your tools)
  rather than the function's own.

Every function implements one **interface**, which fixes the shape of its answer:

| Interface | What the function does | `value` is |
| - | - | - |
| `classify` | Chooses one label from a defined set | A label key |
| `route` | Chooses which candidate (a team, a tool, another function) should handle the input | A label key |
| `rate` | Places the input on an ordered scale of levels | The position of the most likely level, from 0 (and `score` holds the expected level) |
| `detect` | Says whether the input is an instance of the thing it detects | `true` or `false` |
| `is_supported` | Judges whether an answer is supported by its sources | `supported`, `partly_supported`, `contradicted` or `unsupported` |
| `equivalent` | Judges whether two records describe the same entity | `match`, `possible_match` or `no_match` |
| `requires_escalation` | Says whether a person must act on the input now | `true` or `false` |

The prompt injection guard, for example, has two functions over the same input: `attempt` (`detect`: is this an attempt to
take control of the model?) and `kind` (`classify`: which kind of text is it?).

## Calibrated confidence

Every decision carries a `confidence` value: the probability assigned to its chosen answer. Jeff is trained for calibrated
decisions: across many answers, those assigned 0.8 should be correct about 80% of the time. That is a statement
about a large number of answers taken together, not a guarantee about the next one.

Most functions are also calibrated against held-out data: their **threshold** is the lowest confidence at which
their measured precision reaches a set target. A decision below the threshold comes back with `abstained: true`
(its `value` still holds the most likely answer), and the request's `on_low_confidence` policy says what happens
then. A function with no threshold says `calibrated: false` and never abstains on confidence. See
[Escalation](/docs/guides/escalation).

## A base and its adapters

Jeff is one small **base** model plus **adapters**: small sets of extra weights, each trained for one area, that sit
on top of the base. The base loads once; each function names the adapter that answers it, so one server answers
functions from many adapters side by side. See [Adapters](/docs/guides/adapters).

## One step in your program

Jeff provides the prediction; the application determines the action. To screen a message before an AI assistant reads it,
for example:

1. Call `jeff.safety.guard.attempt` on the message, with your application and the message's source as context.
2. If `value` is `false` and the decision did not abstain, pass the message on.
3. If `value` is `true`, refuse it; if the decision abstained, hold it for a person or a larger model.

Several independent questions about the same input (how urgent is it, which team should handle it, does it need a
person) can run in one request, as separate calls or as a [graph](/docs/guides/graphs), with the results returned together.

## Call it

Send `POST {{origin}}/v1/decide` (see [Functions and /v1/decide](/docs/guides/decide)), or use the Python package's
`Jeff.remote` (see [Through the API](/docs/guides/remote)). The [Quick start](/docs/guides/quickstart) does both.
