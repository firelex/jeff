using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class PyYamlTests
{
    private static JsonNode? Value(string text) => ((JsonObject)PyYaml.Load($"d: {text}\n", "doc.yaml")!)["d"];

    // Every expected value below is what yaml.safe_load("d: <text>") gives in Python (PyYAML 6.0.3, YAML 1.1).
    [Theory]
    [InlineData("yes", "true")]
    [InlineData("No", "false")]
    [InlineData("on", "true")]
    [InlineData("OFF", "false")]
    [InlineData("017", "15")]
    [InlineData("0x1F", "31")]
    [InlineData("0b101", "5")]
    [InlineData("1_000", "1000")]
    [InlineData("1:30", "90")]
    [InlineData("+1", "1")]
    [InlineData("-0", "0")]
    [InlineData("0o17", "\"0o17\"")]
    [InlineData("1.5", "1.5")]
    [InlineData("1e5", "\"1e5\"")]
    [InlineData("1.0e+5", "100000.0")]
    [InlineData("1.", "1.0")]
    [InlineData("190:20:30.15", "685230.15")]
    [InlineData("~", "null")]
    [InlineData("null", "null")]
    [InlineData("", "null")]
    [InlineData("'10'", "\"10\"")]
    [InlineData("\"yes\"", "\"yes\"")]
    [InlineData("!!str 12", "\"12\"")]
    public void PlainScalarsResolveAsPyYamlResolvesThem(string text, string json) => JsonAssert.Equal(JsonAssert.Parse(json), Value(text));

    [Fact]
    public void InfinityAndNanAreFloats()
    {
        Assert.Equal(double.PositiveInfinity, Value(".inf")!.GetValue<double>());
        Assert.Equal(double.NegativeInfinity, Value("-.inf")!.GetValue<double>());
        Assert.True(double.IsNaN(Value(".nan")!.GetValue<double>()));
    }

    [Fact]
    public void ABareDateIsKeptAsItsSourceTextAndAQuotedOneIsAPlainString()
    {
        Assert.Equal("2026-10-03", Value("2026-10-03")!.GetValue<string>());
        Assert.Equal("2026-10-03", Value("'2026-10-03'")!.GetValue<string>());
        Assert.Equal("0099-01-01", Value("0099-01-01")!.GetValue<string>());
    }

    [Fact]
    public void ADatetimeOrADayNotOnTheCalendarIsRefused()
    {
        var timed = Assert.Throws<ManifestError>(() => Value("2026-10-03T10:11:12Z"));
        Assert.Contains("time part", timed.Message);
        Assert.StartsWith("doc.yaml is not valid YAML: ", timed.Message);
        Assert.Contains("real calendar date", Assert.Throws<ManifestError>(() => Value("2026-02-30")).Message);
    }

    [Fact]
    public void ARepeatedKeyKeepsItsFirstPlaceAndTakesTheLastValue()
    {
        JsonAssert.Equal(JsonAssert.Parse("{\"a\": 3, \"b\": 2}"), PyYaml.Load("a: 1\nb: 2\na: 3\n", "doc.yaml"));
    }

    [Fact]
    public void MappingsKeepTheirOrderAndAliasesAreCopies()
    {
        var loaded = (JsonObject)PyYaml.Load("first: &x {b: 1, '10': 2}\nsecond: *x\n", "doc.yaml")!;
        Assert.Equal("{\"first\":{\"b\":1,\"10\":2},\"second\":{\"b\":1,\"10\":2}}", Json.Write(loaded));
        Assert.NotSame(loaded["first"], loaded["second"]);
    }

    [Fact]
    public void WhatThisSdkDoesNotReadIsRefusedNamingIt()
    {
        Assert.Contains("not a string", Assert.Throws<ManifestError>(() => PyYaml.Load("1: one\n", "doc.yaml")).Message);
        Assert.Contains("merge", Assert.Throws<ManifestError>(() => PyYaml.Load("a: &x {b: 1}\nc:\n  <<: *x\n", "doc.yaml")).Message);
        Assert.Contains("!!binary", Assert.Throws<ManifestError>(() => PyYaml.Load("a: !!binary aGk=\n", "doc.yaml")).Message);
        Assert.Contains("one YAML document", Assert.Throws<ManifestError>(() => PyYaml.Load("a: 1\n---\nb: 2\n", "doc.yaml")).Message);
        Assert.StartsWith("doc.yaml is not valid YAML: ", Assert.Throws<ManifestError>(() => PyYaml.Load("a: [1\n", "doc.yaml")).Message);
    }

    [Fact]
    public void AnEmptyFileIsNone() => Assert.Null(PyYaml.Load("", "doc.yaml"));

    private static string Nested(int levels) => new string('[', levels) + "0" + new string(']', levels);

    [Fact]
    public void NestingOfExactly512LevelsIsRead()
    {
        var loaded = PyYaml.Load(Nested(512) + "\n", "doc.yaml");
        var level = (JsonNode?)loaded;
        for (int depth = 0; depth < 512; depth++) level = Assert.IsType<JsonArray>(level)[0];
        Assert.Equal(0, level!.GetValue<long>());
    }

    [Fact]
    public void NestingOfMoreThan512LevelsIsRefusedByNameInsteadOfOverflowingTheStack()
    {
        var error = Assert.Throws<ManifestError>(() => PyYaml.Load(Nested(513) + "\n", "doc.yaml"));
        Assert.Contains("512 levels", error.Message);
        Assert.StartsWith("doc.yaml is not valid YAML: ", error.Message);
    }

    [Fact]
    public void AliasesThatExpandPastTheLimitAreRefusedByNameBeforeMemoryRunsOut()
    {
        // 7 levels of 10 aliases: about 450 bytes that would expand to 11 million values. PyYAML's safe_load shares the
        // aliased objects; copying them, this reader must stop at the limit instead.
        var text = new System.Text.StringBuilder("a0: &a0 [x, x, x, x, x, x, x, x, x, x]\n");
        for (int level = 1; level < 8; level++) text.Append($"a{level}: &a{level} [{string.Join(", ", Enumerable.Repeat($"*a{level - 1}", 10))}]\n");
        Assert.True(text.Length < 500, $"the bomb is {text.Length} bytes");
        var error = Assert.Throws<ManifestError>(() => PyYaml.Load(text.ToString(), "laughs.yaml"));
        Assert.StartsWith("laughs.yaml is not valid YAML: line ", error.Message);
        Assert.EndsWith("the aliases expand to more than 1000000 values; write the repeated parts out or use fewer aliases", error.Message);
        // below the limit (5 levels, 123,440 values from aliases) the same shape is read in full
        string small = string.Join("\n", text.ToString().Split('\n').Take(5)) + "\n";
        Assert.Equal("x", PyYaml.Load(small, "small.yaml")!["a4"]![9]![9]![9]![9]![9]!.GetValue<string>());
    }
}
