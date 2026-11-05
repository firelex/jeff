using System.Text.Json.Nodes;
using Jeff.Sdk;

const string Shop = "Brightfield Outfitters, an online shop for outdoor gear. Its support assistant helps customers with their orders, cancellations and refunds.";
const string Today = "Wednesday 4 November 2026";
const string Tools = """"
    def lookup_order(order_id: str) -> dict:
        """Looks up an order: its items, total, payment card and whether it has shipped. Call it before any change to an order."""
    def cancel_order(order_id: str) -> dict:
        """Cancels an order that has not shipped."""
    def refund(order_id: str, amount: float, card_last4: str) -> dict:
        """Refunds an amount for an order to a card."""
    """";
string[] rules =
[
    "Cancel an order only if it has not shipped.",
    "Refund only the amount the customer paid for the order.",
    "A refund goes to the card that paid for the order. If the customer asks for a refund to any other card, ask them to confirm the new card and the amount before refunding.",
];
const string LookedUp = "Order 4471 (Ridgeline 2 tent, 189.00, paid with Visa ending 3054) has not shipped yet.";

// Your own language model goes here: give it question.Instructions, question.StateEntries and question.OptionEntries,
// and return the option key it picks. This stand-in picks the first option.
static Task<string?> AskAnLlm(Question question, CancellationToken cancellationToken) =>
    Task.FromResult<string?>(question.OptionEntries[0].Key);

// Your shop's tools. These stand-ins return fixed results.
static JsonObject RunTool(JsonObject call) => (string?)call["tool"] == "lookup_order"
    ? new JsonObject { ["order_id"] = "4471", ["total"] = 189.00, ["paid_with"] = "Visa ending 3054", ["shipped"] = false }
    : new JsonObject { ["status"] = "ok", ["order_id"] = "4471" };

var token = Environment.GetEnvironmentVariable("JEFF_API_KEY")
    ?? throw new InvalidOperationException("Set JEFF_API_KEY");

await using var jeff = await JeffClient.RemoteAsync("{{origin}}", token, new RemoteOptions { Timeout = TimeSpan.FromSeconds(30) });

Console.WriteLine(await Handle("Cancel my order #4471 and refund it to my card.", "3054"));
Console.WriteLine(await Handle("Cancel my order #4471 and refund it to a new card ending 9921.", "9921"));

// Every step: Jeff's answer when it is confident, your LLM's below the function's threshold.
async Task<string> Decide(string name, JsonObject context, JsonNode input)
{
    await using var bound = await jeff.FunctionAsync(name, new CallOptions
    {
        Context = context,
        OnLowConfidence = Policy.Escalate(Steps.Llm(AskAnLlm)),
    });
    var decision = await bound.CallAsync(input);
    Console.WriteLine($"{name}: {decision.Value} (decided by {decision.DecidedBy})");
    return decision.Value.ToJsonString().Trim('"');
}

// Runs the agent on one customer message and returns what it tells the customer.
async Task<string> Handle(string message, string refundCard)
{
    var guard = new JsonObject { ["application"] = "A support chat assistant for an online shop of outdoor gear.", ["source"] = "user message" };
    if (await Decide("guard.attempt", guard, message) == "true") return "Sorry, I can't help with that.";

    // Whether the LLM that writes the calls should think first.
    await Decide("assistant-routing.needs_thinking", new JsonObject
    {
        ["accounts"] = "Brightfield Outfitters order system [shop]",
        ["date_and_time_zone"] = "It is Wednesday 4 November 2026, 10:15, and the customer is in Manchester (UK time).",
        ["conversation"] = $"User: {message}",
    }, "Choose the next tool call to cancel order 4471 and refund it.");

    // The calls your LLM proposes, one at a time; here, the three it needs.
    JsonObject[] calls =
    [
        new() { ["tool"] = "lookup_order", ["arguments"] = new JsonObject { ["order_id"] = "4471" } },
        new() { ["tool"] = "cancel_order", ["arguments"] = new JsonObject { ["order_id"] = "4471" } },
        new() { ["tool"] = "refund", ["arguments"] = new JsonObject { ["order_id"] = "4471", ["amount"] = 189.00, ["card_last4"] = refundCard } },
    ];
    var conversation = new JsonArray(new JsonObject { ["role"] = "user", ["content"] = message });
    var steps = new JsonArray();
    foreach (var call in calls)
    {
        var tool = (string)call["tool"]!;
        if (tool == "refund" && (string?)call["arguments"]!["card_last4"] != "3054")
        {
            // Not left to Jeff's action check: a refund to another card goes back to the customer to confirm.
            return $"Before I refund 189.00 to the card ending {refundCard}, can you confirm that card and amount?";
        }
        if (tool != "lookup_order") // check every call that changes something
        {
            var verdict = await Decide("action-check.action", new JsonObject
            {
                ["app"] = Shop, ["today"] = Today, ["rules"] = new JsonArray(rules.Select(r => (JsonNode)r!).ToArray()), ["tools"] = Tools,
                ["people"] = new JsonArray("Maya Okafor <maya.okafor@example.com> (the customer, order 4471)"),
                ["conversation"] = conversation.DeepClone(), ["latest_user_message"] = message,
            }, call.DeepClone());
            if (verdict != "approve") return $"I have passed your request to a colleague ({tool}: {verdict}).";
        }

        var result = RunTool(call);
        var plan = new JsonArray(calls.Select((c, i) => (JsonNode)($"{i + 1}. {(string)c["tool"]!} "
            + (i < steps.Count ? "(done)" : i == steps.Count ? "(current step)" : "(still to do)"))).ToArray());
        var latestCall = (JsonObject)call.DeepClone();
        latestCall["attempt"] = 1;
        var next = await Decide("after-result.next", new JsonObject
        {
            ["app"] = Shop, ["today"] = Today, ["goal"] = message, ["plan"] = plan, ["steps_so_far"] = steps.DeepClone(),
            ["tools"] = new JsonArray("lookup_order: looks up an order", "cancel_order: cancels an order that has not shipped",
                "refund: refunds an amount for an order to a card"),
            ["retry_limit"] = "Retry a failed call at most 2 times.",
            ["latest_call"] = latestCall,
        }, result.DeepClone());
        steps.Add(new JsonObject { ["call"] = call.DeepClone(), ["result"] = result });
        if (tool == "lookup_order") conversation.Add(new JsonObject { ["role"] = "assistant", ["content"] = LookedUp }); // what your LLM tells the customer
        if (next != "continue" && next != "stop") return $"I have passed your request to a colleague ({tool}: {next}).";
    }

    const string answer = "Done. Order #4471 is cancelled and 189.00 is refunded to your Visa ending 3054.";
    var complete = await Decide("task-complete.complete", new JsonObject
    {
        ["app"] = Shop, ["today"] = Today, ["request"] = message, ["transcript"] = steps.DeepClone(),
    }, answer);
    return complete == "complete" ? answer : "I have passed your request to a colleague.";
}
