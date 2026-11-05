import { Jeff, escalate, llm } from "jeff-sdk";

const SHOP = "Brightfield Outfitters, an online shop for outdoor gear. Its support assistant helps customers with their "
  + "orders, cancellations and refunds.";
const TODAY = "Wednesday 4 November 2026";
const TOOLS = `def lookup_order(order_id: str) -> dict:
    """Looks up an order: its items, total, payment card and whether it has shipped. Call it before any change to an order."""
def cancel_order(order_id: str) -> dict:
    """Cancels an order that has not shipped."""
def refund(order_id: str, amount: float, card_last4: str) -> dict:
    """Refunds an amount for an order to a card."""`;
const RULES = [
  "Cancel an order only if it has not shipped.",
  "Refund only the amount the customer paid for the order.",
  "A refund goes to the card that paid for the order. If the customer asks for a refund to any other card, "
    + "ask them to confirm the new card and the amount before refunding.",
];

const LOOKED_UP = "Order 4471 (Ridgeline 2 tent, 189.00, paid with Visa ending 3054) has not shipped yet.";

async function askAnLlm(question) {
  // Your own language model goes here: give it question.instructions, question.stateEntries and
  // question.optionEntries, and return the option key it picks. This stand-in picks the first option.
  return question.optionEntries[0][0];
}

function runTool(call) {
  // Your shop's tools. These stand-ins return fixed results.
  if (call.tool === "lookup_order") return { order_id: "4471", total: 189.0, paid_with: "Visa ending 3054", shipped: false };
  return { status: "ok", order_id: "4471" };
}

const jeff = await Jeff.remote("{{origin}}", process.env.JEFF_API_KEY, { timeoutMs: 30000 });

// Every step: Jeff's answer when it is confident, your LLM's below the function's threshold.
async function decide(name, context, input) {
  const bound = await jeff.function(name, { context, onLowConfidence: escalate([llm(askAnLlm)]) });
  const decision = await bound.call(input);
  console.log(`${name}: ${decision.value} (decided by ${decision.decidedBy})`);
  return decision.value;
}

// Runs the agent on one customer message and returns what it tells the customer.
async function handle(message, refundCard) {
  const guard = { application: "A support chat assistant for an online shop of outdoor gear.", source: "user message" };
  if (await decide("guard.attempt", guard, message)) return "Sorry, I can't help with that.";

  // Whether the LLM that writes the calls should think first.
  await decide("assistant-routing.needs_thinking", {
    accounts: "Brightfield Outfitters order system [shop]",
    date_and_time_zone: "It is Wednesday 4 November 2026, 10:15, and the customer is in Manchester (UK time).",
    conversation: `User: ${message}`,
  }, "Choose the next tool call to cancel order 4471 and refund it.");

  // The calls your LLM proposes, one at a time; here, the three it needs.
  const calls = [
    { tool: "lookup_order", arguments: { order_id: "4471" } },
    { tool: "cancel_order", arguments: { order_id: "4471" } },
    { tool: "refund", arguments: { order_id: "4471", amount: 189.0, card_last4: refundCard } },
  ];
  const conversation = [{ role: "user", content: message }];
  const steps = [];
  for (const call of calls) {
    if (call.tool === "refund" && call.arguments.card_last4 !== "3054") {
      // Not left to Jeff's action check: a refund to another card goes back to the customer to confirm.
      return `Before I refund 189.00 to the card ending ${refundCard}, can you confirm that card and amount?`;
    }
    if (call.tool !== "lookup_order") { // check every call that changes something
      const verdict = await decide("action-check.action", {
        app: SHOP, today: TODAY, rules: RULES, tools: TOOLS,
        people: ["Maya Okafor <maya.okafor@example.com> (the customer, order 4471)"],
        conversation, latest_user_message: message,
      }, call);
      if (verdict !== "approve") return `I have passed your request to a colleague (${call.tool}: ${verdict}).`;
    }

    const result = runTool(call);
    const plan = calls.map((c, i) => `${i + 1}. ${c.tool} `
      + (i < steps.length ? "(done)" : i === steps.length ? "(current step)" : "(still to do)"));
    const next = await decide("after-result.next", {
      app: SHOP, today: TODAY, goal: message, plan, steps_so_far: steps,
      tools: ["lookup_order: looks up an order", "cancel_order: cancels an order that has not shipped",
        "refund: refunds an amount for an order to a card"],
      retry_limit: "Retry a failed call at most 2 times.",
      latest_call: { ...call, attempt: 1 },
    }, result);
    steps.push({ call, result });
    if (call.tool === "lookup_order") conversation.push({ role: "assistant", content: LOOKED_UP }); // what your LLM tells the customer
    if (next !== "continue" && next !== "stop") return `I have passed your request to a colleague (${call.tool}: ${next}).`;
  }

  const answer = "Done. Order #4471 is cancelled and 189.00 is refunded to your Visa ending 3054.";
  const complete = await decide("task-complete.complete", { app: SHOP, today: TODAY, request: message, transcript: steps }, answer);
  return complete === "complete" ? answer : "I have passed your request to a colleague.";
}

console.log(await handle("Cancel my order #4471 and refund it to my card.", "3054"));
console.log(await handle("Cancel my order #4471 and refund it to a new card ending 9921.", "9921"));

await jeff.close();
