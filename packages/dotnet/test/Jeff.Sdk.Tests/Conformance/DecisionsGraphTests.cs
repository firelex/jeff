namespace Jeff.Sdk.Tests.Conformance;

public class DecisionsGraphTests
{
    public static TheoryData<string> Cases => Fixtures.CaseNames("decisions.json", item => item["group"]!.GetValue<string>() == "graph");

    [Theory]
    [MemberData(nameof(Cases))]
    public async Task TheGraphRunsAsInThePythonSdk(string name)
    {
        var item = Fixtures.Case("decisions.json", name);
        var (got, engine, questions) = await RunDecisions.ReplayAsync(item);
        JsonAssert.Equal(item["expect"], got);
        Assert.Empty(engine.Mismatches);
        Assert.Equal(0, engine.Remaining);
        JsonAssert.Equal(item["questions"], questions);
    }
}
