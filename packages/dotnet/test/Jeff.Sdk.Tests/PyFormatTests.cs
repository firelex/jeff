using System.Globalization;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Tests;

public class PyFormatTests
{
    [Fact]
    public void ReprOfAStrPicksPythonsQuotesAndEscapes()
    {
        Assert.Equal("'plain'", PyFormat.Str("plain"));
        Assert.Equal("\"it's\"", PyFormat.Str("it's"));
        Assert.Equal("'it\\'s \"x\"'", PyFormat.Str("it's \"x\""));
        Assert.Equal("'a\\nb\\tc\\\\'", PyFormat.Str("a\nb\tc\\"));
        Assert.Equal("'\\x07\\xa0\\u2028'", PyFormat.Str("\u0007\u00a0\u2028"));
        Assert.Equal("'café 💶'", PyFormat.Str("café 💶"));
        Assert.Equal("'\\ud800'", PyFormat.Str("\ud800"));
    }

    // Every expected value is repr() of the same float in CPython 3.13 (`uv run python -c "print(repr(...))"`).
    [Theory]
    [InlineData(1.0, "1.0")]
    [InlineData(0.1, "0.1")]
    [InlineData(0.771902, "0.771902")]
    [InlineData(1e-5, "1e-05")]
    [InlineData(0.0001, "0.0001")]
    [InlineData(1.23e-18, "1.23e-18")]
    [InlineData(1e15, "1000000000000000.0")]
    [InlineData(9007199254740992.0, "9007199254740992.0")]
    [InlineData(1e16, "1e+16")]
    [InlineData(1e21, "1e+21")]
    [InlineData(1e22, "1e+22")]
    [InlineData(1.7976931348623157e308, "1.7976931348623157e+308")]
    [InlineData(2.2250738585072014e-308, "2.2250738585072014e-308")]
    [InlineData(0.1 + 0.2, "0.30000000000000004")]
    [InlineData(123456789.5, "123456789.5")]
    [InlineData(-2.5e-7, "-2.5e-07")]
    [InlineData(5e-324, "5e-324")]
    [InlineData(0.0, "0.0")]
    [InlineData(-0.0, "-0.0")]
    [InlineData(double.PositiveInfinity, "inf")]
    [InlineData(double.NegativeInfinity, "-inf")]
    [InlineData(double.NaN, "nan")]
    public void ReprOfAFloatIsPythonsShortestForm(double value, string expected) => Assert.Equal(expected, PyFormat.Float(value));

    [Theory]
    [InlineData(0.3125, 3, "0.312")]
    [InlineData(0.4375, 3, "0.438")]
    [InlineData(0.0625, 3, "0.062")]
    [InlineData(0.689444, 3, "0.689")]
    [InlineData(0.1, 3, "0.100")]
    [InlineData(2.5, 0, "2")]
    [InlineData(-0.0001, 3, "-0.000")]
    [InlineData(-0.0, 3, "-0.000")]
    [InlineData(5e-324, 3, "0.000")]
    [InlineData(1e22, 2, "10000000000000000000000.00")]
    [InlineData(1234.5678, 2, "1234.57")]
    public void FixedDigitsRoundExactHalvesToEvenAsPythonsFormatDoes(double value, int digits, string expected)
        => Assert.Equal(expected, PyFormat.Fixed(value, digits));

    [Theory]
    [InlineData(double.NaN, "nan")]
    [InlineData(double.PositiveInfinity, "inf")]
    [InlineData(double.NegativeInfinity, "-inf")]
    public void FixedDigitsPrintsNonFiniteValuesAsPythonDoes(double value, string expected)
        => Assert.Equal(expected, PyFormat.Fixed(value, 3));

    [Fact]
    public void FixedDigitsRefuseANegativeCount()
        => Assert.Throws<ArgumentOutOfRangeException>(() => PyFormat.Fixed(0.5, -1));

    [Fact]
    public void SumUsesPythonsCompensatedSummation()
    {
        var tenths = Enumerable.Repeat(0.1, 10).ToList();
        Assert.Equal(0.9999999999999999, tenths.Aggregate(0.0, (total, value) => total + value));
        Assert.Equal(1.0, PyFormat.Sum(tenths));
        Assert.Equal(1.0, PyFormat.Sum([1e100, 1.0, -1e100]));
        Assert.Equal(0.0, PyFormat.Sum([]));
    }

    [Fact]
    public void SortedComparesCodePointsNotUtf16Units()
    {
        Assert.Equal(new[] { "a", "b", "\uffff", "\U0001F4B6" }, PyFormat.Sorted(["b", "a", "\U0001F4B6", "\uffff"]));
        Assert.Equal(3, PyFormat.CodePointLength("a💶b"));
        Assert.Equal("a💶", PyFormat.CodePointPrefix("a💶b", 2));
    }

    [Fact]
    public void MillisecondsReadAsPythonPrintsANumber()
    {
        Assert.Equal("200", PyFormat.Millis(TimeSpan.FromMilliseconds(200)));
        Assert.Equal("0.5", PyFormat.Millis(TimeSpan.FromMilliseconds(0.5)));
        Assert.Equal("1700000000", PyFormat.Number(1700000000));
    }

    [Fact]
    public void TextIsTheSameUnderAGermanCulture()
    {
        var before = CultureInfo.CurrentCulture;
        CultureInfo.CurrentCulture = new CultureInfo("de-DE");
        try
        {
            Assert.Equal("0.5", PyFormat.Float(0.5));
            Assert.Equal("0.312", PyFormat.Fixed(0.3125, 3));
            Assert.Equal("1e-05", PyFormat.Float(1e-5));
            Assert.Equal("1.5", PyFormat.Millis(TimeSpan.FromMilliseconds(1.5)));
        }
        finally
        {
            CultureInfo.CurrentCulture = before;
        }
    }

    [Fact]
    public void ReprOfJsonValuesIsPythons()
    {
        Assert.Equal("['a', 1, 2.5, True, None, 1.0]", PyFormat.Repr(JsonAssert.Parse("[\"a\", 1, 2.5, true, null, 1.0]")));
        Assert.Equal("{'format': 'bf16', 'format_id': 'x'}", PyFormat.Repr(JsonAssert.Parse("{\"format\": \"bf16\", \"format_id\": \"x\"}")));
        Assert.Equal("['b', 'a']", PyFormat.Repr(new[] { "b", "a" }));
        Assert.Equal("None", PyFormat.ReprOrNone((string?)null));
    }

    [Fact]
    public void TypeNamesAndEqualityFollowPython()
    {
        Assert.Equal(new[] { "NoneType", "bool", "int", "float", "str", "list", "dict" },
            new[] { "null", "true", "1", "1.5", "\"s\"", "[]", "{}" }.Select(text => PyFormat.TypeName(JsonAssert.Parse(text))));
        Assert.True(PyFormat.PyEquals(JsonAssert.Parse("true"), JsonAssert.Parse("1")));
        Assert.True(PyFormat.PyEquals(JsonAssert.Parse("false"), JsonAssert.Parse("0")));
        Assert.True(PyFormat.PyEquals(JsonAssert.Parse("1"), JsonAssert.Parse("1.0")));
        Assert.False(PyFormat.PyEquals(JsonAssert.Parse("1"), JsonAssert.Parse("\"1\"")));
        Assert.True(PyFormat.PyEquals(JsonAssert.Parse("{\"a\": [1]}"), JsonAssert.Parse("{\"a\": [1.0]}")));
        Assert.Equal(-1, PyFormat.CompareNumbers(JsonAssert.Parse("9007199254740993"), JsonAssert.Parse("9007199254740994.0")));
        Assert.Null(PyFormat.CompareNumbers(JsonAssert.Parse("1"), Json.Canonical(JsonValue.Create(double.NaN))));
        Assert.Equal("True", PyFormat.StrOf(JsonAssert.Parse("true")));
        Assert.Equal("2", PyFormat.StrOf(JsonAssert.Parse("\"2\"")));
    }
}
