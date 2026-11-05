using System.Globalization;
using System.Numerics;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

internal static partial class PyFormat
{
    /// <summary>repr() of the Python value JSON would give: None, True/False, an int, a float's repr, a str's repr, a
    /// list, a dict in key order.</summary>
    public static string Repr(JsonNode? node)
    {
        if (StackGuard.Exhausted) throw new ContractError($"The value to quote {StackGuard.TooDeep}");
        return ReprOf(node);
    }

    private static string ReprOf(JsonNode? node) => Json.KindOf(node) switch
    {
        JsonKind.Null => "None",
        JsonKind.Bool => node!.GetValue<bool>() ? "True" : "False",
        JsonKind.Int => Json.WholeOf(node).ToString(CultureInfo.InvariantCulture),
        JsonKind.Float => Float(node!.GetValue<double>()),
        JsonKind.String => Str(node!.GetValue<string>()),
        JsonKind.Array => "[" + string.Join(", ", node!.AsArray().Select(Repr)) + "]",
        _ => "{" + string.Join(", ", node!.AsObject().Select(pair => $"{Str(pair.Key)}: {Repr(pair.Value)}")) + "}",
    };

    /// <summary>repr() of a list of str.</summary>
    public static string Repr(IEnumerable<string> items) => "[" + string.Join(", ", items.Select(Str)) + "]";

    public static string ReprOrNone(string? text) => text is null ? "None" : Str(text);

    public static string ReprOrNone(double? number) => number is null ? "None" : Float(number.Value);

    /// <summary>type(value).__name__ of the Python value JSON would give.</summary>
    public static string TypeName(JsonNode? node) => Json.KindOf(node) switch
    {
        JsonKind.Null => "NoneType",
        JsonKind.Bool => "bool",
        JsonKind.Int => "int",
        JsonKind.Float => "float",
        JsonKind.String => "str",
        JsonKind.Array => "list",
        _ => "dict",
    };

    /// <summary>str() of a scalar: a str itself, True/False, an int's digits, a float's repr.</summary>
    public static string StrOf(JsonNode? node) => Json.KindOf(node) == JsonKind.String ? node!.GetValue<string>() : Repr(node);

    /// <summary>Python's == between two JSON values: True == 1, False == 0 and 1 == 1.0; lists in order; dicts whatever
    /// their key order.</summary>
    public static bool PyEquals(JsonNode? a, JsonNode? b)
    {
        if (StackGuard.Exhausted) throw new ContractError($"The values to compare are nested too deeply: each {StackGuard.TooDeep}");
        var x = Json.KindOf(a);
        var y = Json.KindOf(b);
        static bool Numeric(JsonKind kind) => kind is JsonKind.Bool or JsonKind.Int or JsonKind.Float;
        if (Numeric(x) && Numeric(y)) return CompareNumbers(a, b) == 0;
        if (x != y) return false;
        return x switch
        {
            JsonKind.Null => true,
            JsonKind.String => a!.GetValue<string>() == b!.GetValue<string>(),
            JsonKind.Array => a!.AsArray().Count == b!.AsArray().Count && a.AsArray().Zip(b.AsArray()).All(pair => PyEquals(pair.First, pair.Second)),
            JsonKind.Object => a!.AsObject().Count == b!.AsObject().Count
                && a.AsObject().All(pair => b.AsObject().TryGetPropertyValue(pair.Key, out var other) && PyEquals(pair.Value, other)),
            _ => throw new InvalidOperationException($"PyEquals cannot compare {x}"),
        };
    }

    /// <summary>Python's exact comparison of two numbers (a bool counts as 0 or 1; an int and a float compare exactly,
    /// with no rounding of large ints, whatever their size): -1, 0 or 1, or null when they are unordered (a NaN).</summary>
    public static int? CompareNumbers(JsonNode? a, JsonNode? b)
    {
        static (BigInteger? Whole, double Number) Of(JsonNode? node) => Json.KindOf(node) switch
        {
            JsonKind.Bool => (node!.GetValue<bool>() ? 1 : 0, 0),
            JsonKind.Int => (Json.WholeOf(node), 0),
            JsonKind.Float => (null, node!.GetValue<double>()),
            var kind => throw new InvalidOperationException($"CompareNumbers was given a {kind}"),
        };
        var (wholeA, numberA) = Of(a);
        var (wholeB, numberB) = Of(b);
        if (wholeA is { } i && wholeB is { } j) return i.CompareTo(j);
        if (wholeA is null && wholeB is null) return double.IsNaN(numberA) || double.IsNaN(numberB) ? null : numberA.CompareTo(numberB);
        int? sign = wholeA is { } left ? CompareWholeToFloat(left, numberB) : -CompareWholeToFloat(wholeB!.Value, numberA);
        return sign;
    }

    private static int? CompareWholeToFloat(BigInteger whole, double number)
    {
        if (double.IsNaN(number)) return null;
        if (double.IsPositiveInfinity(number)) return -1;
        if (double.IsNegativeInfinity(number)) return 1;
        double truncated = Math.Truncate(number);
        // exact: a finite double with no fraction is a whole number the BigInteger holds without rounding
        var integral = new BigInteger(truncated);
        if (whole != integral) return whole.CompareTo(integral);
        double fraction = number - truncated;
        return fraction > 0 ? -1 : fraction < 0 ? 1 : 0;
    }

    /// <summary>float() of a Python int: the nearest double (ties to even); an int beyond the largest double is
    /// refused with Python's OverflowError message.</summary>
    public static double IntToFloat(BigInteger whole)
    {
        if (whole >= long.MinValue && whole <= long.MaxValue) return (long)whole;
        var magnitude = BigInteger.Abs(whole);
        int shift = (int)magnitude.GetBitLength() - 53;
        var top = magnitude >> shift;
        var rest = magnitude - (top << shift);
        var half = BigInteger.One << (shift - 1);
        if (rest > half || (rest == half && !top.IsEven)) top += 1;
        double value = Math.ScaleB((double)(long)top, shift);
        if (double.IsInfinity(value)) throw new ContractError("int too large to convert to float");
        return whole.Sign < 0 ? -value : value;
    }
}
