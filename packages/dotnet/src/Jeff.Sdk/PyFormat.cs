using System.Globalization;
using System.Numerics;
using System.Text;

namespace Jeff.Sdk;

/// <summary>Python's own text forms and arithmetic, so that every message, reason and number this SDK writes is the
/// one the Python SDK (the reference) writes; contract/conformance pins them. Everything here is culture-invariant.
/// The parts that read JSON values (Repr, TypeName, PyEquals) are in PyFormat.Json.cs.</summary>
internal static partial class PyFormat
{
    private static readonly CultureInfo Invariant = CultureInfo.InvariantCulture;

    /// <summary>repr() of a str: single quotes unless the text holds a single quote and no double quote; \t \n \r,
    /// the backslash and the quote escaped; every character str.isprintable() refuses as \xNN, \uNNNN or \UNNNNNNNN
    /// (a lone surrogate included).</summary>
    public static string Str(string text)
    {
        char quote = text.Contains('\'') && !text.Contains('"') ? '"' : '\'';
        var builder = new StringBuilder(text.Length + 2).Append(quote);
        for (int index = 0; index < text.Length; index++)
        {
            int code = text[index];
            if (char.IsHighSurrogate(text[index]) && index + 1 < text.Length && char.IsLowSurrogate(text[index + 1]))
            {
                code = char.ConvertToUtf32(text[index], text[index + 1]);
                index++;
            }
            if (code == quote || code == '\\') builder.Append('\\').Append((char)code);
            else if (code == '\t') builder.Append("\\t");
            else if (code == '\n') builder.Append("\\n");
            else if (code == '\r') builder.Append("\\r");
            else if (code != ' ' && !IsPrintable(code))
            {
                builder.Append(code <= 0xff ? "\\x" + code.ToString("x2", Invariant)
                    : code <= 0xffff ? "\\u" + code.ToString("x4", Invariant)
                    : "\\U" + code.ToString("x8", Invariant));
            }
            else builder.Append(char.ConvertFromUtf32(code));
        }
        return builder.Append(quote).ToString();
    }

    private static bool IsPrintable(int code) => CharUnicodeInfo.GetUnicodeCategory(code) switch
    {
        UnicodeCategory.Control or UnicodeCategory.Format or UnicodeCategory.Surrogate or UnicodeCategory.PrivateUse
            or UnicodeCategory.OtherNotAssigned or UnicodeCategory.LineSeparator or UnicodeCategory.ParagraphSeparator
            or UnicodeCategory.SpaceSeparator => false,
        _ => true,
    };

    /// <summary>repr() of a float: the shortest digits that read back the same value, in scientific form below 1e-4
    /// and from 1e16 (with at least two exponent digits), otherwise positional with at least one decimal.</summary>
    public static string Float(double value)
    {
        if (double.IsNaN(value)) return "nan";
        if (double.IsPositiveInfinity(value)) return "inf";
        if (double.IsNegativeInfinity(value)) return "-inf";
        if (value == 0) return double.IsNegative(value) ? "-0.0" : "0.0";
        // .NET's "R" gives the shortest round-trip digits, but laid out its own way ("1E-05", "1E+15", "123456789.5",
        // switching to the exponent form at other thresholds than Python's); only the digits and the exponent are
        // taken from it, and they are laid out again as Python lays them out.
        string shortest = value.ToString("R", Invariant);
        bool negative = shortest.StartsWith('-');
        if (negative) shortest = shortest[1..];
        int e = shortest.IndexOfAny(['E', 'e']);
        string mantissa = e < 0 ? shortest : shortest[..e];
        int shift = e < 0 ? 0 : int.Parse(shortest[(e + 1)..], NumberStyles.AllowLeadingSign, Invariant);
        int dot = mantissa.IndexOf('.');
        string whole = dot < 0 ? mantissa : mantissa[..dot];
        string all = whole + (dot < 0 ? "" : mantissa[(dot + 1)..]);
        int first = 0;
        while (all[first] == '0') first++;
        string digits = all[first..].TrimEnd('0');
        int exponent = whole.Length - first - 1 + shift;
        string body;
        if (exponent < -4 || exponent >= 16)
        {
            string shown = digits.Length > 1 ? $"{digits[0]}.{digits[1..]}" : digits;
            body = $"{shown}e{(exponent < 0 ? '-' : '+')}{Math.Abs(exponent).ToString("00", Invariant)}";
        }
        else if (exponent < 0) body = "0." + new string('0', -exponent - 1) + digits;
        else if (digits.Length <= exponent + 1) body = digits + new string('0', exponent + 1 - digits.Length) + ".0";
        else body = $"{digits[..(exponent + 1)]}.{digits[(exponent + 1)..]}";
        return negative ? "-" + body : body;
    }

    /// <summary>format(value, f".{digits}f"): the exact binary value rounded to `digits` decimals, an exact half to
    /// even (Python's rule; .NET's "F" format rounds a half away from zero). The sign is kept for a negative value
    /// that rounds to zero, and for -0.0 ("-0.000"), as in Python. A value that is not finite is printed as Python
    /// does: "nan", "inf", or "-inf".</summary>
    public static string Fixed(double value, int digits)
    {
        if (!double.IsFinite(value))
        {
            return Float(value);
        }
        if (digits < 0)
        {
            throw new ArgumentOutOfRangeException(nameof(digits), $"The number of decimals must be 0 or more, not {digits}");
        }
        long bits = BitConverter.DoubleToInt64Bits(value);
        bool negative = bits < 0;
        int exponentBits = (int)((bits >> 52) & 0x7FF);
        long fraction = bits & 0xFFFFFFFFFFFFFL;
        BigInteger mantissa = exponentBits == 0 ? fraction : fraction | (1L << 52);
        int exponent = exponentBits == 0 ? -1074 : exponentBits - 1075;
        BigInteger numerator = mantissa * BigInteger.Pow(10, digits);
        BigInteger denominator = BigInteger.One;
        if (exponent >= 0) numerator <<= exponent;
        else denominator <<= -exponent;
        BigInteger quotient = BigInteger.DivRem(numerator, denominator, out BigInteger remainder);
        BigInteger twice = remainder * 2;
        if (twice > denominator || (twice == denominator && !quotient.IsEven)) quotient += 1;
        string text = quotient.ToString(Invariant).PadLeft(digits + 1, '0');
        string body = digits == 0 ? text : text[..^digits] + "." + text[^digits..];
        return negative ? "-" + body : body;
    }

    /// <summary>sum() of a list of Python floats, as CPython 3.12 and later computes it (Neumaier's compensated
    /// summation). CPython runs this compensated loop only while every item is a float; an int among the floats (as in
    /// [1e100, 1, -1e100]) breaks it, and the rest is added without compensation, which can give a different answer.
    /// .NET passes doubles only, so this always compensates: it is exact for a list of Python floats, which is what
    /// every caller passes (probabilities, a score's weighted terms).</summary>
    public static double Sum(IEnumerable<double> values)
    {
        double total = 0, compensation = 0;
        foreach (double value in values)
        {
            double next = total + value;
            if (Math.Abs(total) >= Math.Abs(value)) compensation += (total - next) + value;
            else compensation += (value - next) + total;
            total = next;
        }
        if (compensation != 0 && double.IsFinite(compensation)) total += compensation;
        return total;
    }

    /// <summary>Python's ordering of str: by code point (.NET's ordinal order compares UTF-16 units, which puts
    /// U+FFFF after characters outside the Basic Multilingual Plane).</summary>
    public static int CompareCodePoints(string a, string b)
    {
        int i = 0, j = 0;
        while (i < a.Length && j < b.Length)
        {
            int x = CodePointAt(a, ref i), y = CodePointAt(b, ref j);
            if (x != y) return x < y ? -1 : 1;
        }
        return (i < a.Length ? 1 : 0) - (j < b.Length ? 1 : 0);
    }

    public static readonly IComparer<string> CodePointOrder = Comparer<string>.Create(CompareCodePoints);

    /// <summary>sorted() of strings.</summary>
    public static List<string> Sorted(IEnumerable<string> values)
    {
        var list = values.ToList();
        list.Sort(CodePointOrder);
        return list;
    }

    /// <summary>len() of a str: its code points (a surrogate pair counts once).</summary>
    public static int CodePointLength(string text)
    {
        int count = 0;
        for (int index = 0; index < text.Length; count++) CodePointAt(text, ref index);
        return count;
    }

    /// <summary>text[:count] in Python: the first `count` code points.</summary>
    public static string CodePointPrefix(string text, int count)
    {
        int index = 0;
        for (int taken = 0; taken < count && index < text.Length; taken++) CodePointAt(text, ref index);
        return text[..index];
    }

    /// <summary>A number as Python prints one a caller gave as a whole number of milliseconds or seconds: an int when
    /// it is whole, else the float's repr.</summary>
    public static string Number(double value) =>
        double.IsFinite(value) && value == Math.Floor(value) && Math.Abs(value) < 1e15
            ? ((long)value).ToString(Invariant)
            : Float(value);

    /// <summary>A timeout or budget in milliseconds, as messages print it.</summary>
    public static string Millis(TimeSpan span) => Number(span.TotalMilliseconds);

    private static int CodePointAt(string text, ref int index)
    {
        char unit = text[index];
        if (char.IsHighSurrogate(unit) && index + 1 < text.Length && char.IsLowSurrogate(text[index + 1]))
        {
            int code = char.ConvertToUtf32(unit, text[index + 1]);
            index += 2;
            return code;
        }
        index += 1;
        return unit;
    }
}
