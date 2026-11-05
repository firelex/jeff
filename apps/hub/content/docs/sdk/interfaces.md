---
title: Interfaces
---
# Interfaces

An **interface** defines the shape of a function's answer, so functions answering the same kind of question can
be used the same way. Every function implements exactly one. These are the interfaces in this version:

| Interface | What it asks | `value` is |
| - | - | - |
| `classify` | Choose one label from a defined set of labels. | a label key |
| `route` | Choose which candidate (a team, a tool, a screen item, or another function) should handle the input. | a label key |
| `rate` | Place the input on an ordered scale of levels. | the level's index, from 0; `score` holds the expected level |
| `detect` | Say whether the input is an instance of the thing the function detects. | true or false |
| `is_supported` | Judge whether an answer is supported by its sources, using only the sources. | `supported`, `partly_supported`, `contradicted` or `unsupported` |
| `equivalent` | Judge whether two records describe the same entity. | `match`, `possible_match` or `no_match` |
| `requires_escalation` | Say whether a person must act on the input now. | true or false |

The catalogue lists which functions implement each one:

<!-- snippet: sdk/interfaces-1 -->

## The shorthand methods

Each interface has a method of the same name on the client:

```text
client.classify(input, *, function=None, context=None, labels=None, on_low_confidence="return")
```

The same applies to `route`, `rate`, `detect`, `is_supported`, `equivalent` and `requires_escalation`, named according to each
language's conventions (`isSupported` in Node and Java, `IsSupportedAsync` in C#). Each makes one call and returns one
`Decision`. The function to call is named by any reference [Functions](/docs/sdk/functions#section-naming-a-function)
accepts: as `function` among the options in Python, Node and C# (`ShorthandOptions` in Rust and C#), and as the
second argument in Java (`jeff.rate(input, "triage.urgency", options)`). A function that implements another
interface raises `InterfaceMismatch`, naming the method to use instead. The context, labels and policy work as they do when
[binding a function](/docs/sdk/functions#section-binding-a-function).

<!-- snippet: sdk/interfaces-2 -->

A shorthand is the quickest way to make a single call. To call the same function many times with the same context,
[bind it](/docs/sdk/functions#section-binding-a-function) once instead: the context is then checked once, not on
every call.

## Without a function: dispatch

Leaving the function out hands the input to `client.dispatch(input, *, interface, context=None, labels=None,
router="jeff.core.router", on_low_confidence="return")`, which asks a tree of trained router functions which function
should answer, then calls it. The decision's `route` lists each router's choice. Dispatch needs the router's
manifest in the client's catalogue. The catalogue included with each package contains no such router (its one
router, `jeff.routing.router-open`, picks from a menu you send it and is called like any other function: see
[The swarm](/docs/guides/the-swarm)). With this catalogue, a
shorthand without a function (or a call to `dispatch`) raises `UnknownFunction`, with a message explaining that no
router is available and that a function must be specified. Over HTTP, the server can route a request itself: see
[Functions and /v1/decide](/docs/guides/decide#section-letting-jeff-choose-dispatch).

## Next steps

- [Graphs](/docs/sdk/graphs): several functions wired together, with conditions.
- [Escalation](/docs/sdk/escalation): what `on_low_confidence` can do.
