using System.Globalization;
using System.Numerics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using System.Text.Json.Serialization.Metadata;

namespace Jeff.Sdk;

/// <summary>The kinds of JSON value Python tells apart: an int (1) and a float (1.0) are different kinds, as Python's
/// json, repr and msgpack keep them.</summary>
internal enum JsonKind { Null, Bool, Int, Float, String, Array, Object }

/// <summary>JSON as the Python SDK reads and writes it. Every node the SDK holds is canonical: JsonObject (key order
/// kept) and JsonArray, and JsonValues backed by exactly bool, long, double or string, or by BigInteger for a whole
/// number outside the range of a long (Python's int has no size limit; a whole number that fits a long is always backed
/// by a long). Canonical() copies anything a caller passes into that form, so the caller's own nodes are never attached
/// to another tree.</summary>
internal static class Json
{
    private static readonly UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

    public static JsonKind KindOf(JsonNode? node) => node switch
    {
        null => JsonKind.Null,
        JsonObject => JsonKind.Object,
        JsonArray => JsonKind.Array,
        JsonValue value => value.GetValue<object>() switch
        {
            bool => JsonKind.Bool,
            long => JsonKind.Int,
            BigInteger => JsonKind.Int,
            double => JsonKind.Float,
            string => JsonKind.String,
            var other => throw new InvalidOperationException(
                $"A JSON value backed by {other.GetType().Name} is not canonical; pass it through Json.Canonical first"),
        },
        _ => throw new InvalidOperationException($"Unknown kind of JsonNode: {node.GetType().Name}"),
    };

    /// <summary>A whole number as a canonical JSON value: backed by a long when it fits one, else by the BigInteger.</summary>
    public static JsonValue Whole(BigInteger value) =>
        value >= long.MinValue && value <= long.MaxValue ? JsonValue.Create((long)value) : JsonValue.Create(value, BigIntegerInfo)!;

    /// <summary>The value of a canonical whole number (JsonKind.Int), of any size.</summary>
    public static BigInteger WholeOf(JsonNode? node) => node is JsonValue value ? value.GetValue<object>() switch
    {
        long whole => whole,
        BigInteger whole => whole,
        _ => throw new InvalidOperationException($"WholeOf was given a {KindOf(node)}"),
    } : throw new InvalidOperationException($"WholeOf was given a {KindOf(node)}");

    /// <summary>A canonical whole number that fits a long, as that long; false for anything else (a whole number
    /// outside that range included).</summary>
    public static bool TryLong(JsonNode? node, out long value)
    {
        value = 0;
        if (KindOf(node) != JsonKind.Int || !node!.AsValue().TryGetValue(out long whole)) return false;
        value = whole;
        return true;
    }

    /// <summary>Writes a BigInteger-backed node's digits if a caller serialises it with System.Text.Json itself (the SDK
    /// writes JSON with Json.Write).</summary>
    private sealed class BigIntegerConverter : JsonConverter<BigInteger>
    {
        public override BigInteger Read(ref Utf8JsonReader reader, Type typeToConvert, JsonSerializerOptions options) =>
            BigInteger.Parse(Encoding.ASCII.GetString(reader.ValueSpan), NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture);

        public override void Write(Utf8JsonWriter writer, BigInteger value, JsonSerializerOptions options) =>
            writer.WriteRawValue(value.ToString(CultureInfo.InvariantCulture));
    }

    private static readonly JsonTypeInfo<BigInteger> BigIntegerInfo =
        JsonMetadataServices.CreateValueInfo<BigInteger>(new JsonSerializerOptions { TypeInfoResolver = JsonTypeInfoResolver.Combine() }, new BigIntegerConverter());

    /// <summary>Python's limit on the digits of a decimal whole number it reads (sys.get_int_max_str_digits()).</summary>
    public const int MaxWholeDigits = 4300;

    /// <summary>ValueError's message when a decimal whole number has more digits than Python reads.</summary>
    public static string TooManyDigits(int digits) =>
        $"Exceeds the limit ({MaxWholeDigits} digits) for integer string conversion: value has {digits} digits; use "
        + "sys.set_int_max_str_digits() to increase the limit";

    /// <summary>A canonical deep copy: an int, short, byte or uint becomes a long, a float a double, a JsonElement is
    /// read again with Python's rules (1 an int, 1.0 a float); any other .NET value is refused, naming its type.</summary>
    public static JsonNode? Canonical(JsonNode? node)
    {
        if (StackGuard.Exhausted) throw new InvalidRequest($"The JSON value {StackGuard.TooDeep}");
        switch (node)
        {
            case null:
                return null;
            case JsonObject obj:
                var copy = new JsonObject();
                foreach (var (key, item) in obj) copy[key] = Canonical(item);
                return copy;
            case JsonArray array:
                var items = new JsonArray();
                foreach (var item in array) items.Add(Canonical(item));
                return items;
            case JsonValue value:
                if (value.TryGetValue(out JsonElement element)) return Parse(element.GetRawText(), "a JsonElement");
                return value.GetValue<object>() switch
                {
                    bool flag => JsonValue.Create(flag),
                    string text => JsonValue.Create(text),
                    long whole => JsonValue.Create(whole),
                    int whole => JsonValue.Create((long)whole),
                    short whole => JsonValue.Create((long)whole),
                    sbyte whole => JsonValue.Create((long)whole),
                    byte whole => JsonValue.Create((long)whole),
                    ushort whole => JsonValue.Create((long)whole),
                    uint whole => JsonValue.Create((long)whole),
                    ulong whole => Whole(whole),
                    Int128 whole => Whole((BigInteger)whole),
                    UInt128 whole => Whole((BigInteger)whole),
                    BigInteger whole => Whole(whole),
                    double number => JsonValue.Create(number),
                    float number => JsonValue.Create((double)number),
                    var other => throw new InvalidRequest($"A JSON value of .NET type {other.GetType().Name} cannot be used: give a "
                        + "string, a bool, a whole number (int, long, ulong, Int128, UInt128 or BigInteger), a double, a JsonArray or a JsonObject"),
                };
            default:
                throw new InvalidOperationException($"Unknown kind of JsonNode: {node.GetType().Name}");
        }
    }

    /// <summary>Reads JSON as Python's json.loads does: a number with '.', 'e' or 'E' is a float, any other an int
    /// of any size (refused, as Python refuses it, when it has more than 4300 digits); a repeated key keeps its first
    /// place and takes the last value.</summary>
    public static bool TryParse(string text, out JsonNode? value, out string problem)
    {
        value = null;
        byte[] bytes;
        try
        {
            bytes = StrictUtf8.GetBytes(text);
        }
        catch (EncoderFallbackException error)
        {
            problem = $"the text is not valid Unicode ({error.Message})";
            return false;
        }
        var reader = new Utf8JsonReader(bytes, new JsonReaderOptions { MaxDepth = 1000 });
        try
        {
            if (!reader.Read())
            {
                problem = "the text is empty";
                return false;
            }
            value = ReadValue(ref reader);
            if (reader.Read())
            {
                value = null;
                problem = $"there is more after the JSON value (at byte {reader.TokenStartIndex})";
                return false;
            }
        }
        catch (Exception error) when (error is JsonException or InvalidOperationException or NumberTooLarge or TooDeep)
        {
            // InvalidOperationException: Utf8JsonReader.GetString on an escape that is not valid UTF-16
            value = null;
            problem = error.Message;
            return false;
        }
        problem = "";
        return true;
    }

    public static JsonNode? Parse(string text, string where) =>
        TryParse(text, out var value, out var problem) ? value : throw new ContractError($"{where} is not valid JSON: {problem}");

    private sealed class NumberTooLarge(string message) : Exception(message);

    private sealed class TooDeep(string message) : Exception(message);

    private static JsonNode? ReadValue(ref Utf8JsonReader reader)
    {
        if (StackGuard.Exhausted) throw new TooDeep($"the value {StackGuard.TooDeep}");
        switch (reader.TokenType)
        {
            case JsonTokenType.StartObject:
                var obj = new JsonObject();
                while (reader.Read() && reader.TokenType != JsonTokenType.EndObject)
                {
                    string key = reader.GetString()!;
                    reader.Read();
                    obj[key] = ReadValue(ref reader);
                }
                return obj;
            case JsonTokenType.StartArray:
                var array = new JsonArray();
                while (reader.Read() && reader.TokenType != JsonTokenType.EndArray) array.Add(ReadValue(ref reader));
                return array;
            case JsonTokenType.String:
                return JsonValue.Create(reader.GetString()!);
            case JsonTokenType.Number:
                string literal = Encoding.ASCII.GetString(reader.ValueSpan);
                if (literal.AsSpan().IndexOfAny(".eE") >= 0)
                {
                    return JsonValue.Create(double.Parse(literal, NumberStyles.Float, CultureInfo.InvariantCulture));
                }
                if (long.TryParse(literal, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out long whole)) return JsonValue.Create(whole);
                int digits = literal.StartsWith('-') ? literal.Length - 1 : literal.Length;
                if (digits > MaxWholeDigits) throw new NumberTooLarge(TooManyDigits(digits));
                return Whole(BigInteger.Parse(literal, NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture));
            case JsonTokenType.True:
                return JsonValue.Create(true);
            case JsonTokenType.False:
                return JsonValue.Create(false);
            case JsonTokenType.Null:
                return null;
            default:
                throw new JsonException($"unexpected {reader.TokenType}");
        }
    }

    /// <summary>Compact JSON (no spaces), floats in Python's repr form ("1.0", "1e-05"), text outside ASCII as is
    /// (including a supplementary-plane character such as an emoji: its raw UTF-8 bytes, not a surrogate-pair
    /// escape), exactly as Python's json.dumps(..., ensure_ascii=False) writes it. Built by hand rather than through
    /// System.Text.Json's Utf8JsonWriter/JavaScriptEncoder: that writer always escapes a code point above U+FFFF as
    /// \uXXXX\uXXXX (in both a string value and a property name), with no encoder configuration able to turn that
    /// off.</summary>
    public static string Write(JsonNode? node)
    {
        var builder = new StringBuilder();
        WriteValue(builder, node);
        return builder.ToString();
    }

    private static void WriteValue(StringBuilder builder, JsonNode? node)
    {
        if (StackGuard.Exhausted) throw new ContractError($"The JSON value to write {StackGuard.TooDeep}");
        switch (KindOf(node))
        {
            case JsonKind.Null: builder.Append("null"); break;
            case JsonKind.Bool: builder.Append(node!.GetValue<bool>() ? "true" : "false"); break;
            case JsonKind.Int: builder.Append(WholeOf(node).ToString(CultureInfo.InvariantCulture)); break;
            case JsonKind.Float:
                double number = node!.GetValue<double>();
                if (!double.IsFinite(number)) throw new InvalidRequest($"JSON cannot carry the number {PyFormat.Float(number)}");
                builder.Append(PyFormat.Float(number));
                break;
            case JsonKind.String: WriteString(builder, node!.GetValue<string>()); break;
            case JsonKind.Array:
                builder.Append('[');
                bool firstItem = true;
                foreach (var item in node!.AsArray())
                {
                    if (!firstItem) builder.Append(',');
                    firstItem = false;
                    WriteValue(builder, item);
                }
                builder.Append(']');
                break;
            case JsonKind.Object:
                builder.Append('{');
                bool firstPair = true;
                foreach (var (key, item) in node!.AsObject())
                {
                    if (!firstPair) builder.Append(',');
                    firstPair = false;
                    WriteString(builder, key);
                    builder.Append(':');
                    WriteValue(builder, item);
                }
                builder.Append('}');
                break;
        }
    }

    /// <summary>A JSON string literal (quotes included), escaping only the backslash, the quote, and a control
    /// character below U+0020 (\b \f \n \r \t by name, any other as \u00XX) — Python's own escape set. Every other
    /// character, however high its code point, is appended as is; a surrogate pair stays a pair, so it UTF-8-encodes
    /// to the same bytes Python writes.</summary>
    private static void WriteString(StringBuilder builder, string text)
    {
        builder.Append('"');
        foreach (char character in text)
        {
            switch (character)
            {
                case '"': builder.Append("\\\""); break;
                case '\\': builder.Append("\\\\"); break;
                case '\b': builder.Append("\\b"); break;
                case '\f': builder.Append("\\f"); break;
                case '\n': builder.Append("\\n"); break;
                case '\r': builder.Append("\\r"); break;
                case '\t': builder.Append("\\t"); break;
                default:
                    if (character < 0x20) builder.Append("\\u").Append(((int)character).ToString("x4", CultureInfo.InvariantCulture));
                    else builder.Append(character);
                    break;
            }
        }
        builder.Append('"');
    }

    /// <summary>The same JSON: same kinds (1 is not 1.0), same values, same key order.</summary>
    public static bool StrictEquals(JsonNode? a, JsonNode? b) => Equal(a, b, ordered: true);

    /// <summary>The same JSON content whatever the key order; number kinds still count.</summary>
    public static bool ContentEquals(JsonNode? a, JsonNode? b) => Equal(a, b, ordered: false);

    private static bool Equal(JsonNode? a, JsonNode? b, bool ordered)
    {
        if (StackGuard.Exhausted) throw new ContractError($"The JSON values to compare are nested too deeply: each {StackGuard.TooDeep}");
        var kind = KindOf(a);
        if (kind != KindOf(b)) return false;
        switch (kind)
        {
            case JsonKind.Null: return true;
            case JsonKind.Bool: return a!.GetValue<bool>() == b!.GetValue<bool>();
            case JsonKind.Int: return WholeOf(a) == WholeOf(b);
            case JsonKind.Float: return a!.GetValue<double>().Equals(b!.GetValue<double>());
            case JsonKind.String: return string.Equals(a!.GetValue<string>(), b!.GetValue<string>(), StringComparison.Ordinal);
            case JsonKind.Array:
                var left = a!.AsArray();
                var right = b!.AsArray();
                if (left.Count != right.Count) return false;
                for (int index = 0; index < left.Count; index++) if (!Equal(left[index], right[index], ordered)) return false;
                return true;
            default:
                var x = a!.AsObject();
                var y = b!.AsObject();
                if (x.Count != y.Count) return false;
                if (ordered)
                {
                    using var first = x.GetEnumerator();
                    using var second = y.GetEnumerator();
                    while (first.MoveNext() && second.MoveNext())
                    {
                        if (first.Current.Key != second.Current.Key || !Equal(first.Current.Value, second.Current.Value, ordered)) return false;
                    }
                    return true;
                }
                return x.All(pair => y.TryGetPropertyValue(pair.Key, out var other) && Equal(pair.Value, other, ordered));
        }
    }

    public static JsonArray Strings(IEnumerable<string> items) => new(items.Select(item => (JsonNode?)JsonValue.Create(item)).ToArray());

    /// <summary>Exactly JSON true (Python's `is True`), never 1.</summary>
    public static bool IsTrue(JsonNode? node) => KindOf(node) == JsonKind.Bool && node!.GetValue<bool>();

    public static bool IsFalse(JsonNode? node) => KindOf(node) == JsonKind.Bool && !node!.GetValue<bool>();
}

/// <summary>Typed reads of a canonical JSON object's fields. A missing field, or one of another kind, is a
/// ContractError naming where it was read, the field and what it holds instead: no value is ever coerced or made up.</summary>
internal static class JsonRead
{
    private static ContractError Missing(JsonObject data, string key, string what, string where) =>
        new($"{where} is missing its '{key}' {what} (got {(data.TryGetPropertyValue(key, out var value) ? PyFormat.Repr(value) : "None")})");

    public static string String(JsonObject data, string key, string where) =>
        data.TryGetPropertyValue(key, out var value) && Json.KindOf(value) == JsonKind.String
            ? value!.GetValue<string>() : throw Missing(data, key, "string", where);

    public static string? OptionalString(JsonObject data, string key, string where)
    {
        if (!data.TryGetPropertyValue(key, out var value) || value is null) return null;
        return Json.KindOf(value) == JsonKind.String ? value.GetValue<string>() : throw Missing(data, key, "string", where);
    }

    /// <summary>A whole number that fits a long; one outside that range is refused, naming it.</summary>
    public static long Long(JsonObject data, string key, string where)
    {
        if (!data.TryGetPropertyValue(key, out var value) || Json.KindOf(value) != JsonKind.Int) throw Missing(data, key, "whole number", where);
        return Json.TryLong(value, out long whole) ? whole
            : throw new ContractError($"{where}: its '{key}' whole number {PyFormat.Repr(value)} is outside the range this SDK reads "
                + "(-2**63 to 2**63-1)");
    }

    public static double Number(JsonObject data, string key, string where) =>
        data.TryGetPropertyValue(key, out var value) ? NumberOf(value) ?? throw Missing(data, key, "number", where) : throw Missing(data, key, "number", where);

    public static bool Bool(JsonObject data, string key, string where) =>
        data.TryGetPropertyValue(key, out var value) && Json.KindOf(value) == JsonKind.Bool
            ? value!.GetValue<bool>() : throw Missing(data, key, "boolean", where);

    public static JsonObject Object(JsonObject data, string key, string where) =>
        data.TryGetPropertyValue(key, out var value) && value is JsonObject obj ? obj : throw Missing(data, key, "object", where);

    public static JsonArray Array(JsonObject data, string key, string where) =>
        data.TryGetPropertyValue(key, out var value) && value is JsonArray array ? array : throw Missing(data, key, "array", where);

    public static List<string> Strings(JsonObject data, string key, string where)
    {
        if (data.TryGetPropertyValue(key, out var value) && value is JsonArray array && array.All(item => Json.KindOf(item) == JsonKind.String))
        {
            return array.Select(item => item!.GetValue<string>()).ToList();
        }
        throw Missing(data, key, "string array", where);
    }

    /// <summary>A JSON array of numbers (ints or floats) as doubles.</summary>
    public static List<double> Numbers(JsonNode? array, string where)
    {
        if (array is JsonArray items && items.All(item => NumberOf(item) is not null)) return items.Select(item => NumberOf(item)!.Value).ToList();
        throw new ContractError($"{where} is not a list of numbers (got {PyFormat.Repr(array)})");
    }

    /// <summary>An int or float as a double (an int as Python's float() gives it: rounded to the nearest double, an
    /// int beyond the doubles refused); null for anything else (a bool is not a number here).</summary>
    public static double? NumberOf(JsonNode? value) => Json.KindOf(value) switch
    {
        JsonKind.Int => PyFormat.IntToFloat(Json.WholeOf(value)),
        JsonKind.Float => value!.GetValue<double>(),
        _ => null,
    };
}
