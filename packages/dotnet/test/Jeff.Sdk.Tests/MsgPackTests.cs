using System.Text.Json.Nodes;
using Jeff.Sdk.Tests.Conformance;

namespace Jeff.Sdk.Tests;

public class MsgPackTests
{
    private static string Hex(JsonNode? value) => Convert.ToHexString(MsgPack.Encode(value)).ToLowerInvariant();

    [Fact]
    public void AnObjectKeepsItsInsertionOrderNumberLikeKeysIncluded()
    {
        // fixmap of 2: "b" -> 1, "10" -> 2, as msgpack.packb({"b": 1, "10": 2}) writes it
        Assert.Equal("82a16201a2313002", Hex(JsonAssert.Parse("{\"b\": 1, \"10\": 2}")));
    }

    [Theory]
    [InlineData("127", "7f")]
    [InlineData("128", "cc80")]
    [InlineData("255", "ccff")]
    [InlineData("256", "cd0100")]
    [InlineData("65535", "cdffff")]
    [InlineData("65536", "ce00010000")]
    [InlineData("4294967295", "ceffffffff")]
    [InlineData("4294967296", "cf0000000100000000")]
    [InlineData("-1", "ff")]
    [InlineData("-32", "e0")]
    [InlineData("-33", "d0df")]
    [InlineData("-128", "d080")]
    [InlineData("-129", "d1ff7f")]
    [InlineData("-32768", "d18000")]
    [InlineData("-32769", "d2ffff7fff")]
    [InlineData("-2147483648", "d280000000")]
    [InlineData("-2147483649", "d3ffffffff7fffffff")]
    [InlineData("1.0", "cb3ff0000000000000")]
    [InlineData("0.5", "cb3fe0000000000000")]
    [InlineData("true", "c3")]
    [InlineData("false", "c2")]
    [InlineData("null", "c0")]
    public void ScalarsPackInTheSmallestFormatPythonPicks(string json, string hex) => Assert.Equal(hex, Hex(JsonAssert.Parse(json)));

    [Fact]
    public void StringArrayAndMapHeadersSwitchFormatWherePythonsDo()
    {
        Assert.StartsWith("bf78", Hex(JsonValue.Create(new string('x', 31))));
        Assert.StartsWith("d920", Hex(JsonValue.Create(new string('x', 32))));
        Assert.StartsWith("da0100", Hex(JsonValue.Create(new string('x', 256))));
        Assert.StartsWith("9f", Hex(new JsonArray(Enumerable.Range(0, 15).Select(_ => (JsonNode?)JsonValue.Create(0L)).ToArray())));
        Assert.StartsWith("dc0010", Hex(new JsonArray(Enumerable.Range(0, 16).Select(_ => (JsonNode?)JsonValue.Create(0L)).ToArray())));
        var sixteen = new JsonObject();
        for (int index = 0; index < 16; index++) sixteen[index.ToString(System.Globalization.CultureInfo.InvariantCulture)] = 0L;
        Assert.StartsWith("de0010", Hex(sixteen));
    }

    public static TheoryData<string> RowCases => Fixtures.CaseNames("rows.json", item => item["expect"]!.AsObject().ContainsKey("row_msgpack"));

    [Theory]
    [MemberData(nameof(RowCases))]
    public void EveryRecordedRowReadsAndPacksBackToThePythonBytes(string name)
    {
        var bytes = Fixtures.FromBase64(Fixtures.Case("rows.json", name)["expect"]!["row_msgpack"]!.GetValue<string>());
        var decoded = MsgPack.Decode(bytes);
        JsonAssert.Equal(Fixtures.Case("rows.json", name)["expect"]!["row"], decoded);
        Assert.Equal(Fixtures.ToBase64(bytes), Fixtures.ToBase64(MsgPack.Encode(decoded)));
    }

    [Fact]
    public void WhatTheProtocolDoesNotCarryIsRefusedNamingIt()
    {
        Assert.Contains("extra bytes", Assert.Throws<MsgPackError>(() => MsgPack.Decode(new byte[] { 0xc0, 0xc0 })).Message);
        Assert.Contains("binary", Assert.Throws<MsgPackError>(() => MsgPack.Decode(new byte[] { 0xc4, 0x01, 0x00 })).Message);
        Assert.Contains("not a string", Assert.Throws<MsgPackError>(() => MsgPack.Decode(new byte[] { 0x81, 0x01, 0x02 })).Message);
        Assert.Contains("ends", Assert.Throws<MsgPackError>(() => MsgPack.Decode(new byte[] { 0x92, 0x01 })).Message);
        Assert.Contains("surrogate", Assert.Throws<MsgPackError>(() => MsgPack.Encode(JsonValue.Create("\ud800"))).Message);
    }
}
