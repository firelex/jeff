using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests.Conformance;

/// <summary>contract/conformance/manifests.json: manifests every SDK refuses, read from their YAML text as a manifest
/// file is read (Manifest.Load: PyYaml, then FromDict), each refused with the Python SDK's error.</summary>
public class ManifestsTests
{
    public static TheoryData<string> Names() => Fixtures.CaseNames("manifests.json");

    [Theory]
    [MemberData(nameof(Names))]
    public void EachManifestIsRefusedWithThePythonSdksError(string name)
    {
        var item = Fixtures.Case("manifests.json", name);
        string path = item["path"]!.GetValue<string>();
        var expected = item["expect"]!["error"]!;
        var error = Assert.ThrowsAny<JeffError>(() => Manifest.Load(item["yaml"]!.GetValue<string>(), path));
        Assert.Equal((expected["type"]!.GetValue<string>(), expected["message"]!.GetValue<string>()), (error.Type, error.Message));
    }

    [Fact]
    public void TheFixtureHasTheNonFiniteCasesTheRefusedAliasesAndTheRefusedAccesses() => Assert.Equal(16, Fixtures.Cases("manifests.json").Count());

    /// <summary>access is for a server only: the SDK accepts it and does nothing with it.</summary>
    [Theory]
    [InlineData("access: owner\n")]
    [InlineData("access: {accounts: [acct1, B-2_x]}\n")]
    public void AManifestWithAValidAccessIsRead(string access)
    {
        var refused = Fixtures.Case("manifests.json", "an access that is a string other than owner");
        string yaml = refused["yaml"]!.GetValue<string>().Replace("access: everyone\n", access);
        Assert.Equal("jeff.safety.spam", Manifest.Load(yaml, "m.yaml").Id);
    }

    [Fact]
    public void ANumberThatIsNotFiniteIsRefusedWhereverItIs()
    {
        var document = new JsonObject { ["changelog"] = new JsonArray(new JsonObject { ["note"] = double.NegativeInfinity }) };
        Assert.Equal("x: /changelog/0/note is -inf, not a finite number, which JSON cannot hold; write a finite number",
            Assert.Throws<ManifestError>(() => Manifest.FromDict(document, "x")).Message);
    }
}
