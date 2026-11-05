using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

public class RowsTests
{
    public static TheoryData<string> Cases => Fixtures.CaseNames("rows.json");

    [Theory]
    [MemberData(nameof(Cases))]
    public void TheRowIsByteForByteThePythonSdksRow(string name)
    {
        var item = Fixtures.Case("rows.json", name);
        var catalogue = Fixtures.CatalogueFor(item["catalogue"]!.GetValue<string>());
        var call = item["call"]!.AsObject();
        JsonObject got;
        try
        {
            var fn = catalogue.Function(call["function"]!.GetValue<string>());
            var manifest = catalogue.ManifestOf(fn);
            var calibration = Manifest.CalibrationFor(fn, Engines.FormatIdOf(BaseFormat.FromJson(item["base_format"]!.AsObject(), "base_format")));
            var row = Bindings.BuildRow(Bindings.Bind(manifest, fn, call["context"] as JsonObject, call["labels"], calibration), call["input"]);
            got = new JsonObject
            {
                ["adapter"] = manifest.Id,
                ["row"] = row.ToJson(),
                ["row_msgpack"] = Fixtures.ToBase64(MsgPack.Encode(row.ToJson())),
            };
        }
        catch (JeffError error)
        {
            got = new JsonObject { ["error"] = Fixtures.WireOf(error) };
        }
        JsonAssert.Equal(item["expect"], got);
    }
}
