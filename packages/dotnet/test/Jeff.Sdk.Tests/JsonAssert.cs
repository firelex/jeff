using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

/// <summary>JSON comparison for the fixtures: StrictEquals (key order and int versus float count), showing both
/// documents when they differ.</summary>
internal static class JsonAssert
{
    public static void Equal(JsonNode? expected, JsonNode? actual)
    {
        if (!Json.StrictEquals(expected, actual))
        {
            Assert.Fail($"The JSON differs.\nexpected: {Json.Write(expected)}\nactual:   {Json.Write(actual)}");
        }
    }

    /// <summary>Equal content, key order ignored (for request bodies a server reads as JSON).</summary>
    public static void SameContent(JsonNode? expected, JsonNode? actual)
    {
        if (!Json.ContentEquals(expected, actual))
        {
            Assert.Fail($"The JSON content differs.\nexpected: {Json.Write(expected)}\nactual:   {Json.Write(actual)}");
        }
    }

    public static JsonNode? Parse(string text) => Json.Parse(text, "a test document");
}
