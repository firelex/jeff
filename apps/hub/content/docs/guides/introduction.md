---
title: Introduction
---
# Introduction

Programs are built from a few primitives: values, conditions, loops and function calls. None of them can read an
email, a contract clause or a support chat and decide something about it. Until now that took a person, or a
language model asked for a structured response that then had to be parsed and checked.

**Jeff adds a new software primitive: the decision.**

- A **variable** stores a value.
- A **function** performs an operation.
- An **object** encapsulates state and behaviour.
- A **condition** determines which instructions execute.
- A **loop** repeats instructions.
- An **expression** evaluates inputs and produces a value: `total > 100 and country == "DE"`.
- A **decision** interprets unstructured inputs and produces a typed result with calibrated confidence: is this
  email a refund request, and how sure is that?

The decision is closest to the expression. Both take inputs and give back a value your code can branch on. An
expression works on numbers, fields and records, and its answer is exact. A decision works on text, such as an
email, a clause or a chat, so its answer comes with a confidence, and your code decides what to do when that
confidence is too low. A decision about unstructured data becomes an ordinary function call. You import an adapter, pass in the text, and get back a typed result with a calibrated
confidence, in milliseconds, on your own machine or through the API. Like any function, it can be tested, versioned
and composed with others. These docs are organised around that primitive: how to call one, what one is made of,
and how to build programs from them.

Each decision is made by a **decision function**, a small model trained for one specific task, such as
detecting prompt injection or routing a message to a team. It takes an input and, for most functions, a
**context**: fixed facts about the application. It returns a typed **decision** with a selected answer, a
probability for each possible answer and calibrated **confidence**. Jeff does not generate text.

```mermaid
flowchart LR
    caller["your code<br/>input + context"] --> function["a function<br/>for example jeff.safety.guard.attempt"]
    function --> decision["decision<br/>value + confidence"]
    decision -->|confident| act["your code acts on value"]
    decision -->|"confidence below the<br/>function's threshold"| escalate["escalation<br/>another function, an LLM or a person"]
```

Decisions below the calibrated confidence threshold are marked `abstained`. The caller can use the decision,
raise an error or escalate to another function, a language model or a person. Escalation requires an explicit policy. Over HTTP, the policy is
set once for the whole request, and the server escalates only to other functions; escalation to a language model or
a person runs in the Python SDK, on the decision the server returned. See [Escalation](/docs/guides/escalation).

## Three ways to run a function

- **The HTTP API.** One endpoint, [`POST /v1/decide`](/docs/guides/decide), from any language. It calls named
  functions, lets Jeff pick the function for you (`dispatch`), or runs a [graph](/docs/guides/graphs) of functions
  in one request.
- **`Jeff.remote`**, in the Python package `jeff-sdk` (`pip install jeff-sdk`, imported as
  `jeff`): the same calls from Python, each one made against a Jeff server. See
  [Through the API](/docs/guides/remote).
- **`Jeff.local`**, in the same package: the model runs in your own process, with no server and no network, for
  the lowest latency. It needs the engine package as well (`pip install jeff-sdk jeff-runtime`), and the weights of
  the base model and of each adapter it runs, which are public for every adapter in the catalogue. In the Node,
  Rust, C# and Java SDKs, local mode runs the model in an engine process on your machine, which it starts itself
  and your programs share; it needs only uv, installed once (see
  [Connect](/docs/sdk/connect#section-on-your-machine-free)).

The Python code is the same in both modes: bind a function, call it, read `value` and `confidence`.

## The catalogue

Functions are grouped into **adapters**: small sets of extra weights on one shared base model, each trained for one area
(prompt-injection checks, support routing, contract clauses and so on), each with one or more functions. The
[catalogue](/catalogue) lists the adapters, all of them open: their weights are public, so you can call them
through the API or run them yourself with `Jeff.local`.

See [Adapters](/docs/guides/adapters).

## Next steps

- [What are System 1 models?](/docs/guides/system-1): the models behind the primitive, and why a decision
  that is a function call matters.
- [Quick start](/docs/guides/quickstart): make your first call, on your machine or through the API.
- [Worked examples](/docs/guides/worked-support-agent): four small systems built from Jeff functions, from a
  support agent to CI failure routing, each step a live call.
- [How Jeff works](/docs/guides/how-it-works): functions, interfaces, calibration and adapters.
- [Functions and /v1/decide](/docs/guides/decide): every field in a request and a decision.
- [The catalogue](/catalogue): every function you can call.
- [Roadmap](/docs/guides/gaps): what is not built yet.
