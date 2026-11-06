using System.Text.Json.Nodes;
using Jeff.Sdk;

var shop = new JsonObject { ["company"] = "A furniture shop.", ["channel"] = "email" };

var message = "My wardrobe arrived with a broken door and I need it replaced before Friday.";

await using var jeff = await JeffClient.LocalAsync(new[] { "triage", "guard" });

var team = await jeff.RouteAsync(message, new ShorthandOptions
{
    Function = "triage.route",
    Context = shop,
    Labels = new JsonObject { ["deliveries"] = "Deliveries and damaged goods", ["refunds"] = "Refunds and returns" },
});

var urgency = await jeff.RateAsync(message, new ShorthandOptions { Function = "triage.urgency", Context = shop });

var needsHuman = await jeff.RequiresEscalationAsync(message, new ShorthandOptions { Function = "triage.needs_human", Context = shop });

var kind = await jeff.ClassifyAsync(message, new ShorthandOptions
{
    Function = "guard.kind",
    Context = new JsonObject { ["application"] = "A support assistant for a furniture shop.", ["source"] = "email body" },
});

Console.WriteLine($"team: {team.Value}");

Console.WriteLine($"urgency level: {urgency.Value} expected level: {urgency.Score:F2}");

Console.WriteLine($"a person must act now: {needsHuman.Value}");

Console.WriteLine($"kind of text: {kind.Value}");
