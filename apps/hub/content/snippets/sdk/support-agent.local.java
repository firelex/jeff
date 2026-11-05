import ai.jeff.sdk.*;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

public class SupportAgent {
    static final String SHOP = "Brightfield Outfitters, an online shop for outdoor gear. Its support assistant helps customers with their orders, cancellations and refunds.";
    static final String TODAY = "Wednesday 4 November 2026";
    static final String TOOLS = """
        def lookup_order(order_id: str) -> dict:
            \"""Looks up an order: its items, total, payment card and whether it has shipped. Call it before any change to an order.\"""
        def cancel_order(order_id: str) -> dict:
            \"""Cancels an order that has not shipped.\"""
        def refund(order_id: str, amount: float, card_last4: str) -> dict:
            \"""Refunds an amount for an order to a card.\"\"\"""";
    static final List<String> RULES = List.of(
        "Cancel an order only if it has not shipped.",
        "Refund only the amount the customer paid for the order.",
        "A refund goes to the card that paid for the order. If the customer asks for a refund to any other card, ask them to confirm the new card and the amount before refunding.");
    static final String LOOKED_UP = "Order 4471 (Ridgeline 2 tent, 189.00, paid with Visa ending 3054) has not shipped yet.";

    // A JSON object with its keys in the order given.
    static Map<String, Object> obj(Object... pairs) {
        var map = new LinkedHashMap<String, Object>();
        for (int i = 0; i < pairs.length; i += 2) map.put((String) pairs[i], pairs[i + 1]);
        return map;
    }

    // Your own language model goes here: give it question.instructions(), question.stateEntries() and
    // question.optionEntries(), and return the option key it picks. This stand-in picks the first option.
    static String askAnLlm(Question question) {
        return question.optionEntries().get(0).getKey();
    }

    // Your shop's tools. These stand-ins return fixed results.
    static Map<String, Object> runTool(Map<String, Object> call) {
        if (call.get("tool").equals("lookup_order")) {
            return obj("order_id", "4471", "total", 189.00, "paid_with", "Visa ending 3054", "shipped", false);
        }
        return obj("status", "ok", "order_id", "4471");
    }

    // Every step: Jeff's answer when it is confident, your LLM's below the function's threshold.
    static Object decide(Jeff jeff, String name, Map<String, Object> context, Object input) {
        BoundFunction bound = jeff.function(name, CallOptions.none().context(context)
            .onLowConfidence(Policy.escalate(List.of(Step.llm(SupportAgent::askAnLlm)))));
        Decision decision = bound.call(input);
        System.out.println(name + ": " + decision.value() + " (decided by " + decision.decidedBy() + ")");
        return decision.value();
    }

    // Runs the agent on one customer message and returns what it tells the customer.
    static String handle(Jeff jeff, String message, String refundCard) {
        var guard = obj("application", "A support chat assistant for an online shop of outdoor gear.", "source", "user message");
        if (Boolean.TRUE.equals(decide(jeff, "guard.attempt", guard, message))) return "Sorry, I can't help with that.";

        // Whether the LLM that writes the calls should think first.
        decide(jeff, "assistant-routing.needs_thinking", obj(
            "accounts", "Brightfield Outfitters order system [shop]",
            "date_and_time_zone", "It is Wednesday 4 November 2026, 10:15, and the customer is in Manchester (UK time).",
            "conversation", "User: " + message), "Choose the next tool call to cancel order 4471 and refund it.");

        // The calls your LLM proposes, one at a time; here, the three it needs.
        List<Map<String, Object>> calls = List.of(
            obj("tool", "lookup_order", "arguments", obj("order_id", "4471")),
            obj("tool", "cancel_order", "arguments", obj("order_id", "4471")),
            obj("tool", "refund", "arguments", obj("order_id", "4471", "amount", 189.00, "card_last4", refundCard)));
        List<Object> conversation = new ArrayList<>(List.of(obj("role", "user", "content", message)));
        List<Object> steps = new ArrayList<>();
        for (var call : calls) {
            String tool = (String) call.get("tool");
            if (tool.equals("refund") && !((Map<?, ?>) call.get("arguments")).get("card_last4").equals("3054")) {
                // Not left to Jeff's action check: a refund to another card goes back to the customer to confirm.
                return "Before I refund 189.00 to the card ending " + refundCard + ", can you confirm that card and amount?";
            }
            if (!tool.equals("lookup_order")) { // check every call that changes something
                Object verdict = decide(jeff, "action-check.action", obj(
                    "app", SHOP, "today", TODAY, "rules", RULES, "tools", TOOLS,
                    "people", List.of("Maya Okafor <maya.okafor@example.com> (the customer, order 4471)"),
                    "conversation", conversation, "latest_user_message", message), call);
                if (!"approve".equals(verdict)) return "I have passed your request to a colleague (" + tool + ": " + verdict + ").";
            }

            var result = runTool(call);
            List<String> plan = new ArrayList<>();
            for (int i = 0; i < calls.size(); i++) {
                String status = i < steps.size() ? "(done)" : i == steps.size() ? "(current step)" : "(still to do)";
                plan.add((i + 1) + ". " + calls.get(i).get("tool") + " " + status);
            }
            var latestCall = new LinkedHashMap<>(call);
            latestCall.put("attempt", 1L);
            Object next = decide(jeff, "after-result.next", obj(
                "app", SHOP, "today", TODAY, "goal", message, "plan", plan, "steps_so_far", steps,
                "tools", List.of("lookup_order: looks up an order", "cancel_order: cancels an order that has not shipped",
                    "refund: refunds an amount for an order to a card"),
                "retry_limit", "Retry a failed call at most 2 times.",
                "latest_call", latestCall), result);
            steps.add(obj("call", call, "result", result));
            if (tool.equals("lookup_order")) conversation.add(obj("role", "assistant", "content", LOOKED_UP)); // what your LLM tells the customer
            if (!"continue".equals(next) && !"stop".equals(next)) return "I have passed your request to a colleague (" + tool + ": " + next + ").";
        }

        String answer = "Done. Order #4471 is cancelled and 189.00 is refunded to your Visa ending 3054.";
        Object complete = decide(jeff, "task-complete.complete", obj(
            "app", SHOP, "today", TODAY, "request", message, "transcript", steps), answer);
        return "complete".equals(complete) ? answer : "I have passed your request to a colleague.";
    }

    public static void main(String[] args) {
        try (Jeff jeff = Jeff.local("/tmp/jeff.sock", LocalOptions.timeoutMs(30000))) {
            System.out.println(handle(jeff, "Cancel my order #4471 and refund it to my card.", "3054"));
            System.out.println(handle(jeff, "Cancel my order #4471 and refund it to a new card ending 9921.", "9921"));
        }
    }
}
