using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

/// <summary>The files of contract/conformance/, each read once and canonical (key order and int versus float as the
/// Python recorder wrote them).</summary>
internal static partial class Fixtures
{
    private static readonly ConcurrentDictionary<string, JsonObject> Files = new();

    public static JsonObject File(string name) => Files.GetOrAdd(name, file =>
        (JsonObject)Json.Parse(System.IO.File.ReadAllText(Repo.PathOf("contract", "conformance", file)), $"contract/conformance/{file}")!);

    public static IEnumerable<JsonObject> Cases(string file) => File(file)["cases"]!.AsArray().Select(item => item!.AsObject());

    public static JsonObject Case(string file, string name) =>
        Cases(file).Single(item => item["name"]!.GetValue<string>() == name);

    public static TheoryData<string> CaseNames(string file, Func<JsonObject, bool>? where = null)
    {
        var names = new TheoryData<string>();
        foreach (var item in Cases(file).Where(item => where is null || where(item))) names.Add(item["name"]!.GetValue<string>());
        return names;
    }

    public static byte[] FromBase64(string text) => Convert.FromBase64String(text);

    public static string ToBase64(byte[] bytes) => Convert.ToBase64String(bytes);

    /// <summary>A recorded row of probabilities: numbers, and for one that is not finite (which JSON cannot write) the
    /// string "NaN", "Infinity" or "-Infinity" (contract/socket.md, "The probabilities of a predict").</summary>
    public static List<double> Probabilities(JsonNode? row) => row is JsonArray items
        ? items.Select(item => Json.KindOf(item) == JsonKind.String
            ? item!.GetValue<string>() switch
            {
                "NaN" => double.NaN,
                "Infinity" => double.PositiveInfinity,
                "-Infinity" => double.NegativeInfinity,
                var other => throw new InvalidOperationException($"A recorded probability {PyFormat.Str(other)} is not 'NaN', 'Infinity' or '-Infinity'"),
            }
            : JsonRead.NumberOf(item) ?? throw new InvalidOperationException($"A recorded probability {PyFormat.Repr(item)} is not a number")).ToList()
        : throw new InvalidOperationException($"A recorded row of probabilities {PyFormat.Repr(row)} is not a list");
}
