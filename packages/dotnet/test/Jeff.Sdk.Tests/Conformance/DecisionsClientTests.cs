namespace Jeff.Sdk.Tests.Conformance;

public class DecisionsClientTests
{
    private static readonly string[] Groups = ["decide", "escalation", "dispatch"];

    public static TheoryData<string> Cases => Fixtures.CaseNames("decisions.json", item => Groups.Contains(item["group"]!.GetValue<string>()));

    [Theory]
    [MemberData(nameof(Cases))]
    public async Task TheClientGivesThePythonSdksAnswer(string name)
    {
        var item = Fixtures.Case("decisions.json", name);
        var (got, engine, questions) = await RunDecisions.ReplayAsync(item);
        JsonAssert.Equal(item["expect"], got);
        Assert.Empty(engine.Mismatches);
        Assert.Equal(0, engine.Remaining);
        JsonAssert.Equal(item["questions"], questions);
    }

    [Fact]
    public void EveryGroupOfTheFileIsReplayedAndEveryCaseNameIsUnique()
    {
        var cases = Fixtures.Cases("decisions.json").ToList();
        Assert.Equal(new[] { "decide", "dispatch", "escalation", "graph" },
            cases.Select(item => item["group"]!.GetValue<string>()).Distinct().OrderBy(group => group, StringComparer.Ordinal));
        Assert.Equal(cases.Count, cases.Select(item => item["name"]!.GetValue<string>()).Distinct().Count());
    }
}
