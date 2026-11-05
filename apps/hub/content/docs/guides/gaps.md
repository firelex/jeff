---
title: Roadmap
---
# Roadmap

What Jeff has today, and what comes next. This is a working list, not a promise, and it has no dates.

## At launch

| Area | Today | At launch |
| - | - | - |
| Accounts and API keys | There is no sign-up yet | Jeff accounts with their own API keys: $1 a month for the API; running the open adapters on your own machine stays free |
| Python packages | `jeff-sdk` and `jeff-runtime` are not on PyPI yet | Both published on PyPI |
| Node package | `jeff-sdk` is not on npm yet | Published on npm |
| Source code | Not public yet | Public in [github.com/firelex/jeff](https://github.com/firelex/jeff) |

## Requests

| Area | Today | Next step |
| - | - | - |
| Calls per request | Up to 256 calls, one `dispatch`, or one graph of up to 256 nodes ([Functions and /v1/decide](/docs/guides/decide)) | Publish how request time grows with the number of calls |
| Input length | 8,192 tokens per call unless the engine is started with another limit; a longer input is refused with `InputTooLong`, never cut short | Train and measure longer inputs, and publish accuracy by length |
| Request size | Bodies up to 50 MiB; JSON nested beyond the server's supported depth is refused with `InvalidRequest` | Accept compressed (gzip and zstd) request bodies |
| Images | Not part of `POST /v1/decide`; only the older question API takes images | Images as part of a function's input |
| Errors | Every error has a stable name, an HTTP status and details ([Errors](/docs/guides/errors)) | Nothing planned |

## Running at scale

| Area | Today | Next step |
| - | - | - |
| Batching | The engine processes rows from many requests together, in batches up to the size it was started with, and answers `Busy` (HTTP 529, with `Retry-After`) when its queue is full | Several GPUs behind one server |
| Repeated context | On an NVIDIA GPU, the engine keeps the work done for a function's instructions and a bound context, and reuses it on the next call | The same on Apple silicon and on the CPU |
| Rate limits | Per caller, set by the server (`JEFF_RATE_LIMIT`), answered with HTTP 429 and `Retry-After` | Limits per account and per plan |
| Monitoring | `x-request-id` and `Server-Timing` on every response; Prometheus metrics at `GET /metrics` | Usage totals per account and per adapter |
| Low latency | One authenticated WebSocket connection per client ([The low-latency channel](/docs/guides/channel)) | Nothing planned |

## Adapters

| Area | Today | Next step |
| - | - | - |
| Describing adapters | Every adapter's functions, context, input and answers, in the [catalogue](/catalogue) and from `GET /v1/functions` | Each adapter's measured accuracy on its catalogue page |
| Routing | A router picks the right open specialist for a request ([The swarm](/docs/guides/the-swarm)) | More specialists behind the routers |
| Languages | Only English is trained and measured | Measure other languages |

## Developer experience

| Area | Today | Next step |
| - | - | - |
| SDKs | Python and Node, with the same behaviour, checked against one shared set of recorded cases | C#, Java and Rust SDKs (planned; see their pages) |
| Coding agents | `jeff mcp` serves functions as MCP tools ([Jeff with coding agents](/docs/guides/coding-agents)) | An `llms.txt` and an installable skill, so agents write correct Jeff code |
| Recipes | A worked example on most adapters' catalogue pages, and on the open-source adapter pages | A worked recipe for each adapter's main use |
| Writing guidance | [How to build](/docs/guides/how-to-build) | Measure how option wording and level descriptions affect accuracy, and publish the results |
