---
title: Agents
---
# Agents

The agent adapters make decisions around an AI agent's tool calls quickly enough to run at every step: which
tool to call next, whether a proposed call is safe and does what the user asked, what to do after a result comes
back, whether that result answers the question, and whether the whole task is done. This page follows one turn of a
bookkeeping assistant through each of them. A studio owner asks the assistant to email a client's October invoice
and to copy them in. Three more adapters follow: a tool router for a fixed set of tools, a check of whether an
assistant's turn needs careful thinking, and a router that picks which specialist should answer a request.

Each example below uses an input written for this page and shows the answer returned by the live API. To run one yourself, set `JEFF_API_KEY`
as in the [Quick start](/docs/guides/quickstart).

## Choose the next tool

[Agent tool selection from a menu](/catalogue/jeff.agents.tool-route) reads a tool menu written into the request (in any common
format) and the conversation, then chooses the next tool, decides that no tool is needed, or asks the user for a missing detail.
Here the menu is written as Python function signatures, and the user has just given the address the assistant asked
for:

<!-- example: oss-tool-route -->

`send_invoice` means the agent has what it needs and should send the invoice. The agent supplies the call's arguments;
this function selects the tool.

## Check the call before it runs

[Agent action check](/catalogue/jeff.agents.action-check) checks an agent's proposed tool call against what the user
asked and the app's rules before it runs, and says whether to approve it, whether it is the wrong action or unsafe,
or whether to ask the user first. In this example, the proposed email omits the requested copy to the user:

<!-- example: oss-action-check -->

`wrong_action` means the call does not do what the user asked: they asked to be copied in, and `cc` is empty. Send
the call back to the agent to fix rather than running it.

## Decide what to do after a result

[Agent next-step selection](/catalogue/jeff.agents.after-result) decides, after each tool result, what an
AI agent should do next: continue, retry, change approach, stop, or hand over to a person. Here the corrected call
reached the mail service while it was briefly down:

<!-- example: oss-after-result -->

`retry` means the failure is temporary and this was the first attempt of the three the app allows, so the agent
should make the same call again.

## Check that a result answers the question

[Tool result relevance](/catalogue/jeff.agents.result-relevant) checks whether a tool result answers the agent's
current sub-question: yes, partly or no. Here the invoice search returned the wrong month:

<!-- example: oss-result-relevant -->

`no` means the result does not answer the sub-question (it is September's invoice, not October's), so the agent
should search again rather than build on it.

## Check that the task is done

[Agent task completion](/catalogue/jeff.agents.task-complete) reads the user's request, the agent's tool calls and
results and its final answer, and decides whether the task is complete, incomplete or unclear. Here a different run
of the agent sent the invoice without the copy, but its final answer says the user is copied in:

<!-- example: oss-task-complete -->

`incomplete` means part of the request was not done: the email went out with no copy to the user, whatever the
final answer claims. Do not show the answer as it stands.

## Route to one of your tools

[Agent tool selection from labelled options](/catalogue/jeff.routing.tools) picks the next tool for an AI agent to call, or says to answer
directly or to ask the user for missing information. You pass your tools as the labels to choose between, each with a
description. Here a travel desk assistant is asked to book a hotel:

<!-- example: oss-tools -->

`book_hotel` is the key of the tool to call; the agent then fills in the city and the dates of the stay.

## Decide whether a turn needs thinking

[Assistant reasoning check](/catalogue/jeff.agents.assistant-routing) predicts, for a turn an AI assistant must answer,
whether it needs careful thinking or can be answered straight away, so easy turns can take a cheaper, faster path.
Here the user only says thank you after the assistant moved a meeting:

<!-- example: api-assistant-routing -->

`false` means the turn needs no careful thinking: the assistant can reply straight away, without its slower thinking
step.

## Send a request to the right specialist

[Open-adapter router](/catalogue/jeff.routing.router-open-v14) picks which of the open specialists on
your menu should answer a request, or `base` when none of them fits and the general model should answer it. You
send it the request as the specialist would get it, with your menu of specialists as the labels. Here an email
assistant wants a message checked before it summarises it:

<!-- example: more-open-route-guard -->

`guard` is the specialist to call next: the request asks what kind of text the email is, from the point of view of
the AI model that will read it. [The swarm](/docs/guides/the-swarm) shows the router and its menu in full.
