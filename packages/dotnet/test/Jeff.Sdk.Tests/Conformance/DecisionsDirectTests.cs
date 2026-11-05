using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

/// <summary>The decide cases of one call and one predict, replayed without the client: bind, build the row, make the
/// decision from the probabilities the engine answered. DecisionsClientTests (Task 8) replays every case through the
/// client.</summary>
public class DecisionsDirectTests
{
    private static bool Direct(JsonObject item) =>
        item["op"]!.GetValue<string>() == "decide" && item["args"]!["calls"]!.AsArray().Count == 1 && item["predicts"]!.AsArray().Count == 1;

    public static TheoryData<string> Cases => Fixtures.CaseNames("decisions.json", Direct);

    [Fact]
    public void ThereAreDirectCases() => Assert.True(Fixtures.Cases("decisions.json").Count(Direct) > 150);

    [Theory]
    [MemberData(nameof(Cases))]
    public void TheDecisionIsThePythonSdksDecision(string name)
    {
        var item = Fixtures.Case("decisions.json", name);
        var catalogue = Fixtures.CatalogueFor(item["catalogue"]!.GetValue<string>());
        var call = item["args"]!["calls"]![0]!.AsObject();
        var predict = item["predicts"]![0]!.AsObject();
        JsonObject got;
        try
        {
            var fn = catalogue.Function(call["function"]!.GetValue<string>());
            var calibration = Manifest.CalibrationFor(fn, Engines.FormatIdOf(BaseFormat.FromJson(item["base_format"]!.AsObject(), "base_format")));
            var binding = Bindings.Bind(catalogue.ManifestOf(fn), fn, call["context"] as JsonObject, call["labels"], calibration);
            var row = Bindings.BuildRow(binding, call["input"]);
            Assert.Equal(predict["rows_msgpack"]!.GetValue<string>(), Fixtures.ToBase64(MsgPack.Encode(new JsonArray(row.ToJson()))));
            var probabilities = Fixtures.Probabilities(predict["probabilities"]![0]);
            got = new JsonObject { ["result"] = new JsonArray(Decisions.Make(binding, probabilities, predict["timing"]!.AsObject()).ToJson()) };
        }
        catch (JeffError error)
        {
            got = new JsonObject { ["error"] = Fixtures.WireOf(error) };
        }
        JsonAssert.Equal(item["expect"], got);
    }
}
