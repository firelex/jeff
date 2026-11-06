---
title: What are System 1 models?
---
# What are System 1 models?

Psychologists who study how people think often describe two modes of thought, an idea called dual-process theory.
**System 1** is fast and intuitive: you recognise a face, read a road sign or hear that a voice is angry without
working anything out. **System 2** is slow and deliberate: you plan a route, check a sum or weigh a contract clause
step by step.

Jeff borrows the two terms. A large language model that writes out its reasoning before it answers works like
System 2: it can take on almost any question, but each answer takes seconds, incurs a cost per token and comes back
as text. A Jeff function works like System 1: a small model trained for one narrow decision, which reads its input
once and answers straight away.

## What a System 1 model is

- **Narrow.** Each function makes one decision, such as "is this message spam?", "which team should handle it?" or
  "does this answer follow from its sources?". Its possible answers are a fixed set, known before you call it.
- **Fast.** The model reads the input, and the answer is read off in a single step: nothing is generated, sampled or
  parsed (see [How Jeff works](/docs/guides/how-it-works)). On an Apple M4 Max, the model took about 28.6 ms per
  decision ([measured with the Node SDK](/docs/sdk/connect#section-choosing-the-batch-window)).
- **Small.** All functions share one small base model; each area adds an adapter, a small set of extra weights, so
  one machine serves many adapters side by side (see [Adapters](/docs/guides/adapters)).
- **Calibrated.** Every answer comes with a confidence value trained to reflect how often answers like it are correct, and
  most functions are also checked against held-out data.

## Why it matters

Much of the text that software handles (messages, tickets, documents, logs, requests to an AI assistant) needs a
decision before code can act on it. That decision has usually meant hand-written rules, which break on wording
nobody foresaw, or a call to a large language model, which is slow, costs money on every call and answers in free
text that your code has to parse and check.

A System 1 model makes that decision the way software makes any other:

- **A typed answer.** `value` is always one of the function's own answers (a label, `true` or `false`, a level), so
  there is nothing to parse.
- **A confidence that tells you when to escalate.** Most functions are calibrated with a threshold; a decision
  below it comes back marked `abstained`. Your code acts on the confident decisions and hands the rest to System 2: a
  larger model or a person (see [Escalation](/docs/guides/escalation)). The slower, more expensive step then runs only where
  it is needed.
- **Software cost.** Open adapters run on your own machine for free. Through the API, {{price}} a month includes
  {{allowance}} input tokens and every adapter in the catalogue (see [Pricing](/pricing)).
- **The same answer each time.** The same call on the same adapter version gives the same probabilities, apart
  from tiny rounding differences on GPUs.
- **Local or API, the same code.** Bind a function, call it, read `value` and `confidence`, on your own machine with
  `Jeff.local` or against the API with `Jeff.remote`.

For a measured comparison of accuracy, calibration, latency and cost against hosted decision APIs, see
[Jeff vs. hosted decision APIs](/docs/guides/hosted-decision-apis).

## A decision becomes a function call

With a System 1 model, a decision about unstructured text is a function in your program, like any other:

- **Test it.** Keep a set of real inputs with the answers you expect and run them in CI, as you would run unit
  tests (see [Test it like code](/docs/guides/how-to-build#section-test-it-like-code)).
- **Compose it.** Wire several functions into a [graph](/docs/guides/graphs), with conditions on their answers, and
  run the graph locally or on the server.
- **Ship it.** Call a function by its versioned ID, so the function you tested is the one that runs (see
  [Pinning a version](/docs/guides/decide#section-pinning-a-version)).

## Why the site looks like a Commodore 64

Home computers such as the Commodore 64 brought programming to millions of people. Programming then meant the
automated processing of structured data: numbers, records and fields that a program could compare and count.
Unstructured data, such as an email, a contract clause or a support chat, stayed with people, because no program could
read it.

System 1 models can do for unstructured data what those computers did for structured data. A decision about text
becomes a function you call, as cheap and fast as any other function in your program, on your own machine. That
is the new programming vocabulary Jeff is built around, and the reason for the retro design.

## An example

[Spam and phishing detection](/catalogue/jeff.safety.spam) reads a message and says whether it is
legitimate, spam or a phishing scam. Here is a text message about a parcel, with the answer returned by the live API:

<!-- example: oss-spam -->

The answer is `phishing`, with a confidence of 0.911; the next most likely answer, spam, has a probability of 0.088. Your code can
block the link immediately, or, if the confidence had been below the function's threshold, send the message to a person.
Switch the code between "On your machine (free)" and "Through the API" to see that the call is the same either way.

## Next steps

- [Quick start](/docs/guides/quickstart): make your first call, on your machine or through the API.
- [How Jeff works](/docs/guides/how-it-works): functions, interfaces, calibration and adapters.
- [The catalogue](/catalogue): every function you can call.
