using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

internal static partial class Fixtures
{
    public const string Elapsed = "<elapsed>";

    /// <summary>A thrown error as the fixtures write it; anything but a JeffError fails the test with the error itself.</summary>
    public static JsonObject WireOf(Exception error)
    {
        if (error is not JeffError) ExceptionDispatchInfo.Capture(error).Throw();
        var wire = Errors.ToWire((JeffError)error);
        wire["details"]!.AsObject().Remove("request_id");
        return (JsonObject)Normalised(wire)!;
    }

    /// <summary>The value with timing.ms of every decision an llm or human step made written "&lt;elapsed&gt;", as the
    /// recorder writes it.</summary>
    public static JsonNode? Normalised(JsonNode? value)
    {
        var copy = Json.Canonical(value);
        Rewrite(copy);
        return copy;
    }

    private static void Rewrite(JsonNode? node)
    {
        if (node is JsonArray array)
        {
            foreach (var item in array) Rewrite(item);
        }
        else if (node is JsonObject obj)
        {
            foreach (var (_, item) in obj.ToList()) Rewrite(item);
            if (obj["decided_by"] is JsonValue by && Json.KindOf(by) == JsonKind.String && by.GetValue<string>() is "llm" or "human"
                && obj["timing"] is JsonObject timing && timing.ContainsKey("ms"))
            {
                timing["ms"] = Elapsed;
            }
        }
    }
}
