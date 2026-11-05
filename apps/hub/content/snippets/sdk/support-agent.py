import os

import jeff

SHOP = ("Brightfield Outfitters, an online shop for outdoor gear. Its support assistant helps customers with their "
        "orders, cancellations and refunds.")
TODAY = "Wednesday 4 November 2026"
TOOLS = '''def lookup_order(order_id: str) -> dict:
    """Looks up an order: its items, total, payment card and whether it has shipped. Call it before any change to an order."""
def cancel_order(order_id: str) -> dict:
    """Cancels an order that has not shipped."""
def refund(order_id: str, amount: float, card_last4: str) -> dict:
    """Refunds an amount for an order to a card."""'''
RULES = ["Cancel an order only if it has not shipped.",
         "Refund only the amount the customer paid for the order.",
         "A refund goes to the card that paid for the order. If the customer asks for a refund to any other card, "
         "ask them to confirm the new card and the amount before refunding."]


LOOKED_UP = "Order 4471 (Ridgeline 2 tent, 189.00, paid with Visa ending 3054) has not shipped yet."


def ask_an_llm(question: jeff.Question) -> str:
    # Your own language model goes here: give it question.instructions, question.state and question.options, and
    # return the option key it picks. This stand-in picks the first option.
    return next(iter(question.options))


def run_tool(call: dict) -> dict:
    # Your shop's tools. These stand-ins return fixed results.
    if call["tool"] == "lookup_order":
        return {"order_id": "4471", "total": 189.00, "paid_with": "Visa ending 3054", "shipped": False}
    return {"status": "ok", "order_id": "4471"}


client = jeff.Jeff.remote("{{origin}}", token=os.environ["JEFF_API_KEY"], timeout_s=30)


def decide(function: str, context: dict, input: object) -> object:
    # Every step: Jeff's answer when it is confident, your LLM's below the function's threshold.
    bound = client.function(function, context=context, on_low_confidence=jeff.escalate(jeff.llm(ask_an_llm)))
    decision = bound(input)
    print(f"{function}: {decision.value} (decided by {decision.decided_by})")
    return decision.value


def handle(message: str, refund_card: str) -> str:
    """Runs the agent on one customer message and returns what it tells the customer."""
    if decide("guard.attempt", {"application": "A support chat assistant for an online shop of outdoor gear.",
                                "source": "user message"}, message):
        return "Sorry, I can't help with that."

    # Whether the LLM that writes the calls should think first.
    decide("assistant-routing.needs_thinking", {
        "accounts": "Brightfield Outfitters order system [shop]",
        "date_and_time_zone": "It is Wednesday 4 November 2026, 10:15, and the customer is in Manchester (UK time).",
        "conversation": f"User: {message}",
    }, "Choose the next tool call to cancel order 4471 and refund it.")

    # The calls your LLM proposes, one at a time; here, the three it needs.
    calls = [{"tool": "lookup_order", "arguments": {"order_id": "4471"}},
             {"tool": "cancel_order", "arguments": {"order_id": "4471"}},
             {"tool": "refund", "arguments": {"order_id": "4471", "amount": 189.00, "card_last4": refund_card}}]
    conversation = [{"role": "user", "content": message}]
    steps: list[dict] = []
    for call in calls:
        if call["tool"] == "refund" and call["arguments"]["card_last4"] != "3054":
            # Not left to Jeff's action check: a refund to another card goes back to the customer to confirm.
            return f"Before I refund 189.00 to the card ending {refund_card}, can you confirm that card and amount?"
        if call["tool"] != "lookup_order":  # check every call that changes something
            verdict = decide("action-check.action", {
                "app": SHOP, "today": TODAY, "rules": RULES, "tools": TOOLS,
                "people": ["Maya Okafor <maya.okafor@example.com> (the customer, order 4471)"],
                "conversation": conversation, "latest_user_message": message,
            }, call)
            if verdict != "approve":
                return f"I have passed your request to a colleague ({call['tool']}: {verdict})."

        result = run_tool(call)
        plan = [f"{i + 1}. {c['tool']} "
                + ("(done)" if i < len(steps) else "(current step)" if i == len(steps) else "(still to do)")
                for i, c in enumerate(calls)]
        next_step = decide("after-result.next", {
            "app": SHOP, "today": TODAY, "goal": message, "plan": plan, "steps_so_far": steps,
            "tools": ["lookup_order: looks up an order", "cancel_order: cancels an order that has not shipped",
                      "refund: refunds an amount for an order to a card"],
            "retry_limit": "Retry a failed call at most 2 times.",
            "latest_call": {**call, "attempt": 1},
        }, result)
        steps.append({"call": call, "result": result})
        if call["tool"] == "lookup_order":  # what your LLM tells the customer after the lookup
            conversation.append({"role": "assistant", "content": LOOKED_UP})
        if next_step not in ("continue", "stop"):
            return f"I have passed your request to a colleague ({call['tool']}: {next_step})."

    answer = "Done. Order #4471 is cancelled and 189.00 is refunded to your Visa ending 3054."
    complete = decide("task-complete.complete", {"app": SHOP, "today": TODAY, "request": message,
                                                 "transcript": steps}, answer)
    return answer if complete == "complete" else "I have passed your request to a colleague."

print(handle("Cancel my order #4471 and refund it to my card.", "3054"))
print(handle("Cancel my order #4471 and refund it to a new card ending 9921.", "9921"))

client.close()
