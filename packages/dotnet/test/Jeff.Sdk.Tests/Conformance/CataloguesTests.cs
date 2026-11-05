using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

public class CataloguesTests
{
    private static JsonObject File => Fixtures.File("catalogues.json");

    [Fact]
    public void TheShippedManifestsReadAsThePythonSdkReadsThem()
    {
        var recorded = File["shipped"]!["manifests"]!.AsObject();
        var shipped = Fixtures.CatalogueFor("shipped");
        Assert.Equal(recorded.Select(pair => pair.Key).OrderBy(id => id, StringComparer.Ordinal),
            shipped.Manifests().Select(manifest => manifest.Id).OrderBy(id => id, StringComparer.Ordinal));
        foreach (var manifest in shipped.Manifests()) JsonAssert.Equal(recorded[manifest.Id], manifest.Raw);
        JsonAssert.Equal(File["shipped"]!["bases"], shipped.Bases);
    }

    public static TheoryData<string> Variants()
    {
        var names = new TheoryData<string>();
        foreach (var (name, _) in Fixtures.File("catalogues.json")["variants"]!.AsObject()) names.Add(name);
        return names;
    }

    [Theory]
    [MemberData(nameof(Variants))]
    public void EveryVariantLoadsAndKeepsItsDocument(string name)
    {
        var catalogue = Fixtures.CatalogueFor(name);
        foreach (var raw in File["variants"]![name]!.AsArray())
        {
            JsonAssert.Equal(raw, catalogue.Adapter(raw!["id"]!.GetValue<string>()).Raw);
        }
    }
}
