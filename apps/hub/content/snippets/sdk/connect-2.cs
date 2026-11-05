using System.Text.Json.Nodes;
using Jeff.Sdk;

// Called before every request, so a token refreshed here (read again from the environment, or fetched from your
// secret store) is used from the next request on.
static ValueTask<string?> CurrentToken(CancellationToken cancellationToken) =>
    ValueTask.FromResult<string?>(Environment.GetEnvironmentVariable("JEFF_API_KEY")
        ?? throw new InvalidOperationException("Set JEFF_API_KEY"));

await using var jeff = await JeffClient.RemoteAsync(
    "{{origin}}", TokenSource.From(CurrentToken), new RemoteOptions { Timeout = TimeSpan.FromSeconds(30) });

await using var guard = await jeff.FunctionAsync("guard.attempt", new CallOptions
{
    Context = new JsonObject
    {
        ["application"] = "A support chat assistant for an online shop.",
        ["source"] = "user message",
    },
});

Console.WriteLine((await guard.CallAsync("What are your opening hours?")).Value);
