using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class JsonTests
{
    [Fact]
    public void ParsingKeepsPythonsIntAndFloatApart()
    {
        Assert.Equal(JsonKind.Int, Json.KindOf(JsonAssert.Parse("1")));
        Assert.Equal(JsonKind.Float, Json.KindOf(JsonAssert.Parse("1.0")));
        Assert.Equal(JsonKind.Float, Json.KindOf(JsonAssert.Parse("1e5")));
        Assert.Equal(JsonKind.Int, Json.KindOf(JsonAssert.Parse("-9223372036854775808")));
        Assert.Equal(JsonKind.Int, Json.KindOf(JsonAssert.Parse("9223372036854775808")));
        Assert.False(Json.TryParse("{\"a\": 1} x", out _, out _));
    }

    [Fact]
    public void ARepeatedKeyKeepsItsFirstPlaceAndTakesTheLastValueAsPythonsJsonDoes()
    {
        var parsed = (JsonObject)JsonAssert.Parse("{\"a\": 1, \"b\": 2, \"a\": 3}")!;
        Assert.Equal(new[] { "a", "b" }, parsed.Select(pair => pair.Key));
        Assert.Equal(3L, parsed["a"]!.GetValue<long>());
    }

    [Fact]
    public void NumberLikeKeysKeepTheirOrder()
    {
        var parsed = JsonAssert.Parse("{\"b\": 1, \"10\": 2, \"2\": 3}")!;
        Assert.Equal("{\"b\":1,\"10\":2,\"2\":3}", Json.Write(parsed));
    }

    [Fact]
    public void WritingUsesPythonsFloatFormsAndRefusesWhatJsonCannotCarry()
    {
        var value = new JsonObject { ["a"] = 1.0, ["b"] = 1e-5, ["c"] = 3L, ["d"] = "café", ["e"] = null, ["f"] = true };
        Assert.Equal("{\"a\":1.0,\"b\":1e-05,\"c\":3,\"d\":\"café\",\"e\":null,\"f\":true}", Json.Write(Json.Canonical(value)));
        var error = Assert.Throws<InvalidRequest>(() => Json.Write(JsonValue.Create(double.NaN)));
        Assert.Equal("JSON cannot carry the number nan", error.Message);
    }

    [Fact]
    public void WritingPutsTextAboveTheBasicMultilingualPlaneAsRawUtf8NotASurrogatePairEscape()
    {
        // Expected values are CPython 3.13's json.dumps(..., ensure_ascii=False)
        // (`uv run python3 -c "import json; print(json.dumps('Gewinnen Sie jetzt 💶, café', ensure_ascii=False))"`),
        // matching contract/conformance/rows.json's "input: text outside ASCII" fixture text.
        Assert.Equal("\"Gewinnen Sie jetzt 💶, café\"", Json.Write(JsonAssert.Parse("\"Gewinnen Sie jetzt 💶, café\"")));
        // A supplementary-plane character inside a run of plain ASCII, and one standing alone.
        Assert.Equal("\"a😀b\"", Json.Write(JsonAssert.Parse("\"a😀b\"")));
        Assert.Equal("\"😀\"", Json.Write(JsonAssert.Parse("\"😀\"")));
        // The same holds for an object key, not only a string value (Utf8JsonWriter.WritePropertyName has the same
        // escaping bug as WriteStringValue; `uv run python3 -c "import json; print(json.dumps({'😀key': ['💶', 1]},
        // ensure_ascii=False, separators=(',', ':')))"` gives {"😀key":["💶",1]}).
        var withEmojiKey = Json.Canonical(new JsonObject { ["😀key"] = new JsonArray("💶", 1L) });
        Assert.Equal("{\"😀key\":[\"💶\",1]}", Json.Write(withEmojiKey));
    }

    [Fact]
    public void WritingIgnoresTheCurrentCulture()
    {
        var before = CultureInfo.CurrentCulture;
        CultureInfo.CurrentCulture = new CultureInfo("de-DE");
        try
        {
            Assert.Equal("[0.5,1.25]", Json.Write(Json.Canonical(new JsonArray(0.5, 1.25))));
            Assert.Equal(JsonKind.Float, Json.KindOf(JsonAssert.Parse("0.5")));
        }
        finally
        {
            CultureInfo.CurrentCulture = before;
        }
    }

    [Fact]
    public void CanonicalCopiesTurnDotNetNumbersIntoPythonKinds()
    {
        Assert.Equal(JsonKind.Int, Json.KindOf(Json.Canonical(JsonValue.Create(3))));
        Assert.Equal(JsonKind.Int, Json.KindOf(Json.Canonical(JsonValue.Create((byte)3))));
        Assert.Equal(JsonKind.Float, Json.KindOf(Json.Canonical(JsonValue.Create(3.0f))));
        var serialized = JsonSerializer.SerializeToNode(new { count = 3, share = 0.5 })!;
        var copy = (JsonObject)Json.Canonical(serialized)!;
        Assert.Equal(JsonKind.Int, Json.KindOf(copy["count"]));
        Assert.Equal(JsonKind.Float, Json.KindOf(copy["share"]));
        // JsonValue.Create(JsonElement) only wraps a scalar element (.NET refuses one of kind object or array), so
        // the JsonElement-reparse branch of Canonical is exercised through a scalar property, not a whole object.
        var element = JsonDocument.Parse("{\"x\": 1.0}").RootElement.GetProperty("x");
        Assert.Equal(JsonKind.Float, Json.KindOf(Json.Canonical(JsonValue.Create(element))));
        var refused = Assert.Throws<InvalidRequest>(() => Json.Canonical(JsonValue.Create(1.5m)));
        Assert.Contains("Decimal", refused.Message);
    }

    [Fact]
    public void ACanonicalCopyLeavesTheOriginalWithItsOwnParent()
    {
        var context = new JsonObject { ["channel"] = "sms" };
        var holder = new JsonObject { ["context"] = context };
        var copy = Json.Canonical(context);
        var other = new JsonObject { ["context"] = copy };
        Assert.Same(holder, context.Parent);
        Assert.Same(other, copy!.Parent);
    }

    [Fact]
    public void StrictEqualityCountsOrderAndNumberKindContentEqualityOnlyKind()
    {
        Assert.False(Json.StrictEquals(JsonAssert.Parse("1"), JsonAssert.Parse("1.0")));
        Assert.False(Json.StrictEquals(JsonAssert.Parse("{\"a\":1,\"b\":2}"), JsonAssert.Parse("{\"b\":2,\"a\":1}")));
        Assert.True(Json.ContentEquals(JsonAssert.Parse("{\"a\":1,\"b\":[2]}"), JsonAssert.Parse("{\"b\":[2],\"a\":1}")));
        Assert.False(Json.ContentEquals(JsonAssert.Parse("{\"a\":1}"), JsonAssert.Parse("{\"a\":1.0}")));
    }

    [Fact]
    public void TypedReadsNameWhatIsMissing()
    {
        var data = (JsonObject)JsonAssert.Parse("{\"n\": 2, \"s\": 3}")!;
        Assert.Equal(2.0, JsonRead.Number(data, "n", "a thing"));
        var error = Assert.Throws<ContractError>(() => JsonRead.String(data, "s", "a thing"));
        Assert.Equal("a thing is missing its 's' string (got 3)", error.Message);
        Assert.Equal("a thing is missing its 'x' string (got None)",
            Assert.Throws<ContractError>(() => JsonRead.String(data, "x", "a thing")).Message);
    }
}
