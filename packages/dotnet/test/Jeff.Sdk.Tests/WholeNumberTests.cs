using System.Numerics;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

/// <summary>Whole numbers of any size, as Python's int: every expected value here is what CPython 3.13 with
/// msgpack-python 1.2.3 gives (json.loads, json.dumps(separators=(",", ":")), repr, msgpack.packb, float()).</summary>
public class WholeNumberTests
{
    private const string Overflow = "OverflowError: Integer value out of range";

    // literal, msgpack.packb(json.loads(literal)).hex() (or msgpack-python's OverflowError), repr(float(value))
    public static TheoryData<string, string, string> Cases => new()
    {
        { "9223372036854775807", "cf7fffffffffffffff", "9.223372036854776e+18" },
        { "9223372036854775808", "cf8000000000000000", "9.223372036854776e+18" },
        { "18446744073709551615", "cfffffffffffffffff", "1.8446744073709552e+19" },
        { "18446744073709551616", Overflow, "1.8446744073709552e+19" },
        { "-9223372036854775808", "d38000000000000000", "-9.223372036854776e+18" },
        { "-9223372036854775809", Overflow, "-9.223372036854776e+18" },
        { "123456789012345678901234567890", Overflow, "1.2345678901234568e+29" },
        { "-123456789012345678901234567890", Overflow, "-1.2345678901234568e+29" },
    };

    [Theory]
    [MemberData(nameof(Cases))]
    public void AWholeNumberOfAnySizeReadsWritesPacksAndPrintsAsPythonDoes(string literal, string packed, string asFloat)
    {
        var value = JsonAssert.Parse(literal);
        Assert.Equal(JsonKind.Int, Json.KindOf(value));
        Assert.Equal(BigInteger.Parse(literal), Json.WholeOf(value));
        Assert.Equal(literal, Json.Write(value));
        Assert.Equal($"[{literal},{{\"k\":{literal}}}]", Json.Write(JsonAssert.Parse($"[{literal}, {{\"k\": {literal}}}]")));
        Assert.Equal(literal, PyFormat.Repr(value));
        Assert.Equal("int", PyFormat.TypeName(value));
        Assert.Equal(asFloat, PyFormat.Float(JsonRead.NumberOf(value)!.Value));
        Assert.True(Json.StrictEquals(value, Json.Canonical(value)));
        Assert.True(Json.StrictEquals(value, PyYaml.Load($"a: {literal}", "a test document")!["a"]));
        if (packed == Overflow)
        {
            Assert.Equal(Overflow, Assert.Throws<MsgPackError>(() => MsgPack.Encode(value)).Message);
        }
        else
        {
            Assert.Equal(packed, Convert.ToHexString(MsgPack.Encode(value)).ToLowerInvariant());
            Assert.True(Json.StrictEquals(value, MsgPack.Decode(Convert.FromHexString(packed))));
        }
    }

    [Fact]
    public void AWholeNumberThatFitsALongIsAlwaysBackedByOne()
    {
        Assert.True(JsonAssert.Parse("-9223372036854775808")!.AsValue().TryGetValue(out long _));
        Assert.True(Json.Whole(new BigInteger(5)).AsValue().TryGetValue(out long five) && five == 5);
        Assert.True(Json.StrictEquals(JsonAssert.Parse("7"), Json.Canonical(JsonValue.Create(new BigInteger(7)))));
        Assert.True(Json.StrictEquals(JsonAssert.Parse("18446744073709551615"), Json.Canonical(JsonValue.Create(ulong.MaxValue))));
        Assert.False(Json.TryLong(JsonAssert.Parse("9223372036854775808"), out _));
        Assert.False(Json.StrictEquals(JsonAssert.Parse("18446744073709551616"), JsonAssert.Parse("18446744073709551616.0")));
    }

    [Theory]
    // a, b, a == b, a < b in Python
    [InlineData("18446744073709551616", "18446744073709551616.0", true, false)]
    [InlineData("18446744073709551617", "18446744073709551617.0", false, false)]
    [InlineData("9223372036854775808", "9223372036854775808.0", true, false)]
    [InlineData("9223372036854775807", "9223372036854775808.0", false, true)]
    [InlineData("1000000000000000000000000000000", "1e30", false, true)]
    [InlineData("-9223372036854775809", "-9223372036854775808.0", false, true)]
    [InlineData("18446744073709551616", "18446744073709551616", true, false)]
    [InlineData("123456789012345678901234567890", "18446744073709551616", false, false)]
    public void ComparingABigWholeNumberIsExactAsInPython(string a, string b, bool equal, bool less)
    {
        var x = JsonAssert.Parse(a);
        var y = JsonAssert.Parse(b);
        Assert.Equal(equal, PyFormat.PyEquals(x, y));
        Assert.Equal(equal, PyFormat.PyEquals(y, x));
        Assert.Equal(less, PyFormat.CompareNumbers(x, y) < 0);
        Assert.Equal(-PyFormat.CompareNumbers(x, y), PyFormat.CompareNumbers(y, x));
    }

    [Theory]
    // float(value) in Python: the nearest double, ties to even
    [InlineData("1208925819614629308923904", "1.2089258196146292e+24")] // 2**80 + 2**27: a tie, kept even
    [InlineData("1208925819614629308923905", "1.2089258196146294e+24")] // 2**80 + 2**27 + 1
    [InlineData("1208925819614629577359360", "1.2089258196146297e+24")] // 2**80 + 3 * 2**27: a tie, rounded up to even
    [InlineData("-1208925819614629308923905", "-1.2089258196146294e+24")]
    public void ABigWholeNumberBecomesTheDoublePythonsFloatGives(string literal, string expected) =>
        Assert.Equal(expected, PyFormat.Float(PyFormat.IntToFloat(BigInteger.Parse(literal))));

    [Fact]
    public void AWholeNumberBeyondTheDoublesIsRefusedAsPythonsFloatRefusesIt()
    {
        var max = BigInteger.Pow(2, 1024) - BigInteger.Pow(2, 971);
        Assert.Equal("1.7976931348623157e+308", PyFormat.Float(PyFormat.IntToFloat(max)));
        Assert.Equal("1.7976931348623157e+308", PyFormat.Float(PyFormat.IntToFloat(BigInteger.Pow(2, 1024) - BigInteger.Pow(2, 970) - 1)));
        Assert.Equal("int too large to convert to float",
            Assert.Throws<ContractError>(() => PyFormat.IntToFloat(BigInteger.Pow(2, 1024) - BigInteger.Pow(2, 970))).Message);
    }

    [Fact]
    public void ADecimalOfMoreThan4300DigitsIsRefusedAsPythonRefusesIt()
    {
        Assert.Equal(JsonKind.Int, Json.KindOf(JsonAssert.Parse(new string('9', 4300))));
        Assert.Equal(JsonKind.Int, Json.KindOf(JsonAssert.Parse("-" + new string('9', 4300))));
        Assert.False(Json.TryParse("[" + new string('9', 4301) + "]", out _, out var problem));
        Assert.Equal("Exceeds the limit (4300 digits) for integer string conversion: value has 4301 digits; use "
            + "sys.set_int_max_str_digits() to increase the limit", problem);
    }

    [Fact]
    public void AManifestCountOutsideTheRangeOfALongIsRefusedNamingIt()
    {
        var data = (JsonObject)JsonAssert.Parse("{\"rows\": 9223372036854775808}")!;
        Assert.Equal("calibration: its 'rows' whole number 9223372036854775808 is outside the range this SDK reads (-2**63 to 2**63-1)",
            Assert.Throws<ContractError>(() => JsonRead.Long(data, "rows", "calibration")).Message);
    }
}
