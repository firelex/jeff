using System.Text.Json;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class BindingTests
{
    [Fact]
    public void ARowFromSerializedOrIntBackedValuesPacksAsPythonDoes()
    {
        var catalogue = Catalogue.Shipped();
        var fn = catalogue.Function("spam.is_spam");
        var manifest = catalogue.ManifestOf(fn);
        var serialized = (JsonObject)JsonSerializer.SerializeToNode(new { channel = "sms" })!;
        var fromDotNet = Bindings.BuildRow(Bindings.Bind(manifest, fn, serialized, null, fn.Calibration), JsonValue.Create(3));
        var fromJson = Bindings.BuildRow(Bindings.Bind(manifest, fn, (JsonObject)JsonAssert.Parse("{\"channel\": \"sms\"}")!, null, fn.Calibration),
            JsonAssert.Parse("3"));
        Assert.Equal(MsgPack.Encode(fromJson.ToJson()), MsgPack.Encode(fromDotNet.ToJson()));
        var withFloat = Bindings.BuildRow(Bindings.Bind(manifest, fn, serialized, null, fn.Calibration), JsonValue.Create(3.0));
        Assert.Equal(JsonKind.Float, Json.KindOf(withFloat.State["message"]));
    }

    [Fact]
    public void ACallCopiesWhatItIsGivenSoTheCallersNodesStayTheirs()
    {
        var context = new JsonObject { ["channel"] = "sms" };
        var parent = new JsonObject { ["context"] = context };
        var call = new Call("spam.is_spam", "Hi", context);
        context["channel"] = "email";
        Assert.Equal("sms", call.Context!["channel"]!.GetValue<string>());
        Assert.Same(parent, context.Parent);
    }

    [Fact]
    public void TheFormatIdOfABaseFormatIsRefusedWhenMalformed()
    {
        Assert.Null(Engines.FormatIdOf(new BaseFormat("bf16")));
        Assert.Equal("x", Engines.FormatIdOf(new BaseFormat("int4-g64", "x")));
        Assert.Equal("The engine's base format {'format': 'bf16', 'format_id': 'x'} is bfloat16 but carries a format_id",
            Assert.Throws<EngineError>(() => Engines.FormatIdOf(new BaseFormat("bf16", "x"))).Message);
        Assert.StartsWith("The engine's base format {'format': 'int4-g64'} names no format_id",
            Assert.Throws<EngineError>(() => Engines.FormatIdOf(new BaseFormat("int4-g64"))).Message);
    }
}
