---
title: A support agent, end to end
---
# A support agent, end to end

## The problem

An online shop's support assistant receives this message:

> Cancel my order #4471 and refund it to my card.

The assistant is an agent: a language model that calls the shop's tools to look the order up, cancel it and refund
it. Each of those calls changes something real. Before each one, the agent must make a judgement: is the message an
attack, is the call safe and what the customer asked for, did the call work, and is the job finished? Asking a large
model each time is slow and costly, and its answer is prose that has to be parsed.

## How Jeff helps

Jeff answers each of those judgements with one small, fast call that returns one of a fixed set of answers and a
calibrated confidence. Your program keeps the loop and your language model still writes the tool calls; Jeff checks
around them. When a Jeff answer falls below its function's threshold, the program hands that one step to your
language model.

The steps, all with open adapters, so you can run them on your machine:

1. [Prompt-injection guard](/catalogue/jeff.safety.guard) checks the message before the agent reads it.
2. [Assistant routing](/catalogue/jeff.agents.assistant-routing) says whether the agent's model should think before
   it writes the calls.
3. [Tool result relevance](/catalogue/jeff.agents.result-relevant) checks that the order lookup answers what the
   agent needs to know.
4. [Agent action check](/catalogue/jeff.agents.action-check) checks each call that changes something, before it
   runs.
5. [Agent next-step selection](/catalogue/jeff.agents.after-result) decides what to do after each result.
6. [Agent task completion](/catalogue/jeff.agents.task-complete) checks the final answer against what was done.

## The solution

Each step below is a live call; the answer under it is the one the API returned. Choose your language and whether to
run it on your machine (free) or through the API.

### Check the message

The guard reads the customer's message before the agent does:

<!-- example: worked-support-guard -->

`false` means the message is an ordinary request, not an attempt to take over the assistant, so the agent goes on.

### Decide whether the turn needs thinking

Cancelling and refunding takes several calls with money involved, so the agent's model may need to think first:

<!-- example: worked-support-routing -->

`true` means this turn goes to the model with thinking on.

### Look the order up

The agent's model chooses the first call. Here it should look the order up before changing it. We asked
[Agent tool selection from a menu](/catalogue/jeff.agents.tool-route) to make this choice and it answered `cancel_order` (0.65,
recorded on 4 November 2026), skipping the lookup, even after the tool menu said to look an order up first. So
this page leaves the choice to the agent's model. The program below passes it a fixed list of calls.

Once the lookup returns, the agent checks that the result answers what it needs to know:

<!-- example: worked-support-result-relevant -->

`yes`: the result says whether the order has shipped, what it cost and which card paid, so the agent can go on.

### Check the cancellation before it runs

The order has not shipped, so the agent proposes to cancel it:

<!-- example: worked-support-check-cancel -->

`approve` means the call follows the shop's rules and does what the customer asked, so the agent runs it.

### Decide what to do after the result

The shop's system answers that the order is cancelled:

<!-- example: worked-support-after-cancel -->

`continue` means the step worked and the agent goes on to the refund.

### Check the refund before it runs

The agent proposes to refund the 189.00 to the card that paid:

<!-- example: worked-support-check-refund -->

`approve`, with confidence 0.82, above the function's threshold of 0.51.

### Check that the task is done

The agent has cancelled the order and made the refund, and writes its answer:

<!-- example: worked-support-task-complete -->

`complete`: every part of the request was done, so the answer can go to the customer.

### The other branch: a refund to a new card

Now the customer writes "Cancel my order #4471 and refund it to a new card ending 9921." The shop's rule is that a
refund goes to the card that paid, unless the customer confirms a new card and the amount. The agent should stop and
ask (`ask_user`). Jeff's action check got this one wrong in our test: it answered `approve` (0.61, above its
threshold), even after the rule was made clearer. So the program does not leave this step to Jeff. A refund to any
card other than the paying one goes back to the customer to confirm, in your own code.

### The whole loop

One program wires the steps together. Every Jeff call is bound with an escalation policy: when its confidence is
below the function's threshold, the question goes to your language model (`ask_an_llm`, a stand-in here) instead.
Your tools and your model's calls are stand-ins too. The program runs both branches.

<!-- snippet: sdk/support-agent -->

## What you get

- Each check is one call to a small model, with one of a fixed set of answers, so the agent's code can branch on it
  directly.
- The seven Jeff answers shown were all right and all above their functions' thresholds. The two steps left out
  were confident and wrong, which is why the program handles them without Jeff.
- How the functions measure, from each adapter's page (in-house tests come from the same generator as the training
  data):

| Step | Test accuracy | When confident |
| - | - | - |
| Guard | 98.4% (6,552 rows, mixed sources) | right 99.1% of the time, confident on 100% |
| Assistant routing | 86.6% (771 rows, in-house) | right 95.2% of the time, confident on 54.8% |
| Tool result relevance | 98.6% (2,000 rows, in-house) | right 99.2% of the time, confident on 100% |
| Action check | 99.4% (1,934 rows, in-house) | right 99.6% of the time, confident on 100% |
| Next step after a result | 100.0% (2,000 rows, in-house) | right 99.95% of the time, confident on 100% |
| Task completion | 99.7% (2,000 rows, in-house) | right 99.95% of the time, confident on 100% |

The two steps this page leaves out show where in-house figures stop: tool routing scores 71.6% on a public
function-calling benchmark (When2Call, 3,646 rows), against 98.2% on its in-house test. Check your own cases before
relying on a step, and keep the rules your code can check exactly in code.
