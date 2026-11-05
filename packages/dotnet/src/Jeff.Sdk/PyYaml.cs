using System.Globalization;
using System.Numerics;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using YamlDotNet.Core;
using YamlDotNet.Core.Events;

namespace Jeff.Sdk;

/// <summary>YAML as PyYAML's safe_load reads it (YAML 1.1): the same implicit resolvers in the same order (yes/no/on/off
/// are booleans, 017 is octal, 1e5 is a string because PyYAML's float needs a dot, 1:30 is sexagesimal), a repeated key
/// keeps its first place and takes the last value, an alias is a copy of its anchor. A bare YYYY-MM-DD date is kept as
/// its source text (what datetime.date.isoformat() gives back) once it is checked to be a real calendar day; a
/// timestamp with a time part, a non-string mapping key, a merge key (&lt;&lt;) and tags other than the core ones are
/// refused, naming the value: no manifest uses them, and refusing beats reading them differently from Python.
/// YamlDotNet supplies only the parser events; every value is resolved here.</summary>
internal static class PyYaml
{
    private const string CoreTag = "tag:yaml.org,2002:";
    private const int MaxDepth = 512;

    /// <summary>The most values aliases may add to one document. Each alias is expanded by copying its anchored value
    /// (PyYAML shares one object instead), so a few hundred bytes of nested aliases could otherwise build gigabytes;
    /// the Rust SDK has the same limit.</summary>
    internal const int AliasExpansionLimit = 1_000_000;

    /// <summary>One document's anchors (each with the number of values an alias to it adds) and how many values its
    /// aliases have added so far.</summary>
    private sealed class Anchors
    {
        public readonly Dictionary<string, (JsonNode? Value, long Size)> ByName = new(StringComparer.Ordinal);
        public long Expanded;
    }
    private const RegexOptions Options = RegexOptions.CultureInvariant;
    private static readonly Regex BoolPattern = new("^(?:yes|Yes|YES|no|No|NO|true|True|TRUE|false|False|FALSE|on|On|ON|off|Off|OFF)$", Options);
    private static readonly Regex FloatPattern = new(
        @"^(?:[-+]?(?:[0-9][0-9_]*)\.[0-9_]*(?:[eE][-+][0-9]+)?|\.[0-9][0-9_]*(?:[eE][-+][0-9]+)?|[-+]?[0-9][0-9_]*(?::[0-5]?[0-9])+\.[0-9_]*|[-+]?\.(?:inf|Inf|INF)|\.(?:nan|NaN|NAN))$",
        Options);
    private static readonly Regex IntPattern = new(
        @"^(?:[-+]?0b[0-1_]+|[-+]?0[0-7_]+|[-+]?(?:0|[1-9][0-9_]*)|[-+]?0x[0-9a-fA-F_]+|[-+]?[1-9][0-9_]*(?::[0-5]?[0-9])+)$", Options);
    private static readonly Regex NullPattern = new("^(?:~|null|Null|NULL|)$", Options);
    private static readonly Regex TimestampPattern = new(
        @"^(?:[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]|[0-9][0-9][0-9][0-9]-[0-9][0-9]?-[0-9][0-9]?(?:[Tt]|[ \t]+)[0-9][0-9]?:[0-9][0-9]:[0-9][0-9](?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9][0-9]?(?::[0-9][0-9])?))?)$",
        Options);
    private static readonly Regex DatePattern = new("^[0-9]{4}-[0-9]{2}-[0-9]{2}$", Options);

    private sealed class Problem(string message) : Exception(message);

    public static JsonNode? Load(string text, string path)
    {
        try
        {
            var parser = new Parser(new StringReader(text));
            parser.Consume<StreamStart>();
            if (parser.TryConsume<StreamEnd>(out _)) return null;
            parser.Consume<DocumentStart>();
            var anchors = new Anchors();
            var value = Node(parser, anchors, 0);
            parser.Consume<DocumentEnd>();
            if (!parser.TryConsume<StreamEnd>(out _)) throw new Problem("the file holds more than one YAML document; it must hold one YAML document");
            return value;
        }
        catch (Exception error) when (error is YamlException or Problem)
        {
            throw new ManifestError($"{path} is not valid YAML: {error.Message}");
        }
    }

    private static JsonNode? Node(IParser parser, Anchors anchors, int depth)
    {
        if (depth > MaxDepth) throw new Problem($"the value is nested more than {MaxDepth} levels deep");
        if (StackGuard.Exhausted) throw new Problem($"the value {StackGuard.TooDeep}");
        if (parser.TryConsume<AnchorAlias>(out var alias))
        {
            if (!anchors.ByName.TryGetValue(alias.Value.Value, out var anchored)) throw new Problem($"the alias *{alias.Value.Value} names no anchor before it");
            anchors.Expanded += anchored.Size;
            if (anchors.Expanded > AliasExpansionLimit)
            {
                throw new Problem($"line {alias.Start.Line}: the aliases expand to more than {AliasExpansionLimit} values; write the repeated parts out "
                    + "or use fewer aliases");
            }
            return Json.Canonical(anchored.Value);
        }
        if (parser.TryConsume<Scalar>(out var scalar)) return Remember(anchors, scalar.Anchor, ScalarValue(scalar));
        if (parser.TryConsume<SequenceStart>(out var sequence))
        {
            CheckTag(sequence.Tag, "seq");
            var array = new JsonArray();
            while (!parser.TryConsume<SequenceEnd>(out _)) array.Add(Node(parser, anchors, depth + 1));
            return Remember(anchors, sequence.Anchor, array);
        }
        if (parser.TryConsume<MappingStart>(out var mapping))
        {
            CheckTag(mapping.Tag, "map");
            var obj = new JsonObject();
            while (!parser.TryConsume<MappingEnd>(out _))
            {
                if (parser.Current is Scalar { Value: "<<", Style: ScalarStyle.Plain })
                {
                    throw new Problem("a merge key (<<) is not read by this SDK; write the merged keys out");
                }
                var key = Node(parser, anchors, depth + 1);
                if (Json.KindOf(key) != JsonKind.String)
                {
                    throw new Problem($"the mapping key {PyFormat.Repr(key)} is not a string; quote it");
                }
                obj[key!.GetValue<string>()] = Node(parser, anchors, depth + 1);
            }
            return Remember(anchors, mapping.Anchor, obj);
        }
        throw new Problem($"unexpected {parser.Current?.GetType().Name ?? "end of file"}");
    }

    /// <summary>The number of values in `value`, itself included, counted without recursion.</summary>
    private static long CountValues(JsonNode? value)
    {
        long count = 0;
        var pending = new Stack<JsonNode?>();
        pending.Push(value);
        while (pending.Count > 0)
        {
            var next = pending.Pop();
            count++;
            if (next is JsonArray items) foreach (var item in items) pending.Push(item);
            else if (next is JsonObject fields) foreach (var (_, item) in fields) pending.Push(item);
        }
        return count;
    }

    private static JsonNode? Remember(Anchors anchors, AnchorName anchor, JsonNode? value)
    {
        if (!anchor.IsEmpty) anchors.ByName[anchor.Value] = (Json.Canonical(value), CountValues(value));
        return value;
    }

    private static void CheckTag(TagName tag, string kind)
    {
        if (!tag.IsEmpty && !tag.IsNonSpecific && tag.Value != CoreTag + kind)
        {
            throw new Problem($"the tag {ShortTag(tag.Value)} is not read by this SDK");
        }
    }

    private static string ShortTag(string tag) => tag.StartsWith(CoreTag, StringComparison.Ordinal) ? "!!" + tag[CoreTag.Length..] : tag;

    private static JsonNode? ScalarValue(Scalar scalar)
    {
        string text = scalar.Value;
        if (!scalar.Tag.IsEmpty && !scalar.Tag.IsNonSpecific) return Tagged(scalar.Tag.Value, text);
        if (scalar.Style != ScalarStyle.Plain) return JsonValue.Create(text);
        if (BoolPattern.IsMatch(text)) return JsonValue.Create(text.ToLowerInvariant() is "yes" or "true" or "on");
        if (FloatPattern.IsMatch(text)) return JsonValue.Create(ConstructFloat(text));
        if (IntPattern.IsMatch(text)) return Json.Whole(ConstructInt(text));
        if (text == "=") throw new Problem("the value '=' has YAML's value tag, which PyYAML's safe_load cannot construct either");
        if (NullPattern.IsMatch(text)) return null;
        if (TimestampPattern.IsMatch(text)) return JsonValue.Create(ConstructDate(text));
        return JsonValue.Create(text);
    }

    private static JsonNode? Tagged(string tag, string text) => ShortTag(tag) switch
    {
        "!!str" => JsonValue.Create(text),
        "!!int" => Json.Whole(ConstructInt(text)),
        "!!float" => JsonValue.Create(ConstructFloat(text)),
        "!!bool" => text.ToLowerInvariant() switch
        {
            "yes" or "true" or "on" => JsonValue.Create(true),
            "no" or "false" or "off" => JsonValue.Create(false),
            _ => throw new Problem($"{PyFormat.Str(text)} is not a YAML boolean"),
        },
        "!!null" => null,
        "!!timestamp" => JsonValue.Create(ConstructDate(text)),
        var other => throw new Problem($"the tag {other} is not read by this SDK"),
    };

    /// <summary>PyYAML's construct_yaml_int: underscores dropped; 0b binary, 0x hex, a leading 0 octal, a:b:c base 60.</summary>
    private static BigInteger ConstructInt(string text)
    {
        string value = text.Replace("_", "", StringComparison.Ordinal);
        int sign = 1;
        if (value.StartsWith('-')) sign = -1;
        if (value.StartsWith('-') || value.StartsWith('+')) value = value[1..];
        BigInteger result;
        if (value == "0") result = 0;
        else if (value.StartsWith("0b", StringComparison.Ordinal)) result = Digits(value[2..], 2, text);
        else if (value.StartsWith("0x", StringComparison.Ordinal)) result = Digits(value[2..], 16, text);
        else if (value.StartsWith('0')) result = Digits(value, 8, text);
        else if (value.Contains(':'))
        {
            result = 0;
            foreach (var part in value.Split(':')) result = result * 60 + Digits(part, 10, text);
        }
        else
        {
            // Python's int() refuses a decimal of more than 4300 digits (power-of-two bases have no such limit)
            if (value.Length > Json.MaxWholeDigits) throw new Problem(Json.TooManyDigits(value.Length));
            result = Digits(value, 10, text);
        }
        result *= sign;
        return result;
    }

    private static BigInteger Digits(string digits, int radix, string text)
    {
        if (digits.Length == 0) throw new Problem($"{PyFormat.Str(text)} is not an integer");
        BigInteger value = 0;
        foreach (char digit in digits)
        {
            int number = digit switch
            {
                >= '0' and <= '9' => digit - '0',
                >= 'a' and <= 'f' => digit - 'a' + 10,
                >= 'A' and <= 'F' => digit - 'A' + 10,
                _ => radix,
            };
            if (number >= radix) throw new Problem($"{PyFormat.Str(text)} is not an integer");
            value = value * radix + number;
        }
        return value;
    }

    /// <summary>PyYAML's construct_yaml_float: underscores dropped, case ignored; .inf, .nan, a:b:c.d base 60.</summary>
    private static double ConstructFloat(string text)
    {
        string value = text.Replace("_", "", StringComparison.Ordinal).ToLowerInvariant();
        double sign = 1;
        if (value.StartsWith('-')) sign = -1;
        if (value.StartsWith('-') || value.StartsWith('+')) value = value[1..];
        if (value == ".inf") return sign * double.PositiveInfinity;
        if (value == ".nan") return double.NaN;
        if (value.Contains(':'))
        {
            // in PyYAML's order: the last part first, each later one times the next power of 60
            double total = 0, power = 1;
            foreach (var part in value.Split(':').Reverse())
            {
                total += ParseFloat(part, text) * power;
                power *= 60;
            }
            return sign * total;
        }
        return sign * ParseFloat(value, text);
    }

    private static double ParseFloat(string value, string text) =>
        double.TryParse(value, NumberStyles.Float, CultureInfo.InvariantCulture, out double number)
            ? number : throw new Problem($"{PyFormat.Str(text)} is not a float");

    private static string ConstructDate(string text)
    {
        if (!DatePattern.IsMatch(text))
        {
            throw new Problem($"{PyFormat.Str(text)} is a YAML timestamp with a time part; this SDK only reads a manifest date as a "
                + "bare YYYY-MM-DD, so quote it to keep it a plain string");
        }
        if (!DateOnly.TryParseExact(text, "yyyy-MM-dd", CultureInfo.InvariantCulture, DateTimeStyles.None, out _))
        {
            throw new Problem($"{PyFormat.Str(text)} is not a real calendar date; quote it to keep it a plain string");
        }
        return text;
    }
}
