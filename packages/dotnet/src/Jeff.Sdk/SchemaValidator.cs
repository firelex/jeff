using System.Collections.Concurrent;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Jeff.Sdk;

/// <summary>One validation error as Python's jsonschema.ValidationError holds it: its message, the keyword that
/// failed (null for a false schema), the absolute path of the instance (str property names and int indexes), the
/// instance, the keyword's value, the errors of every failing alternative of a oneOf (its context), and, for an error
/// in such a context, the index of the alternative it came from.</summary>
internal sealed record SchemaError(string Message, string? Keyword, IReadOnlyList<object> Path, JsonNode? Instance, JsonNode? KeywordValue,
    IReadOnlyList<SchemaError> Context, int? Alternative);

/// <summary>A port of jsonschema 4.26's Draft 2020-12 validator (jsonschema/_keywords.py and validators.py) for the
/// keywords the contract's schemas use. Keywords run in the schema's document order, as Python's applicable_validators
/// does; $ref resolves within a schema or across schemas by $id. Each level checks both the depth cap and the stack
/// the thread has left (StackGuard), so a deep document is a ContractError on any thread.</summary>
internal sealed class SchemaRegistry
{
    /// <summary>How a keyword holds subschemas: a map of name to subschema, a list of subschemas, or one.</summary>
    private enum Holds { Map, List, One }

    /// <summary>The keywords whose values hold subschemas, and how; CheckSchema walks into exactly these, so a new
    /// applicator cannot be validated without its subschemas also being checked at load.</summary>
    private static readonly IReadOnlyDictionary<string, Holds> Applicators = new Dictionary<string, Holds>(StringComparer.Ordinal)
    {
        ["properties"] = Holds.Map, ["$defs"] = Holds.Map, ["allOf"] = Holds.List, ["oneOf"] = Holds.List,
        ["additionalProperties"] = Holds.One, ["items"] = Holds.One, ["not"] = Holds.One, ["if"] = Holds.One, ["then"] = Holds.One,
        ["else"] = Holds.One, ["propertyNames"] = Holds.One,
    };

    /// <summary>The keywords this port validates, and the annotations it reads past.</summary>
    public static readonly IReadOnlySet<string> Supported = new HashSet<string>(
        new[]
        {
            "type", "required", "const", "enum", "minimum", "maximum", "exclusiveMinimum", "minLength", "maxLength", "pattern", "minItems",
            "maxItems", "minProperties", "maxProperties", "uniqueItems", "dependentRequired", "$ref",
            "$schema", "$id", "title", "description", "$comment", "examples", "default",
        }.Concat(Applicators.Keys),
        StringComparer.Ordinal);

    /// <summary>Checks a schema before use: every keyword in it (and in every subschema) is one this port validates or
    /// reads past (an unknown one would be ignored by mistake), and every pattern is one .NET reads as Python does.</summary>
    public static void CheckSchema(JsonNode? schema, string where)
    {
        if (StackGuard.Exhausted) throw new ContractError($"The schema at {where} {StackGuard.TooDeep}");
        if (schema is not JsonObject obj) return;
        foreach (var (keyword, value) in obj)
        {
            if (!Supported.Contains(keyword))
            {
                throw new ContractError($"{where} uses the schema keyword {PyFormat.Str(keyword)}, which this SDK's validator does not know");
            }
            if (keyword == "pattern") Pattern(value!.GetValue<string>());
            if (!Applicators.TryGetValue(keyword, out var holds)) continue;
            switch (holds)
            {
                case Holds.Map:
                    foreach (var (name, subschema) in value!.AsObject()) CheckSchema(subschema, $"{where}/{keyword}/{name}");
                    break;
                case Holds.List:
                    foreach (var subschema in value!.AsArray()) CheckSchema(subschema, $"{where}/{keyword}");
                    break;
                default:
                    CheckSchema(value, $"{where}/{keyword}");
                    break;
            }
        }
    }

    /// <summary>How many schemas deep one check may descend (a recursive schema such as graph's condition descends
    /// about three schemas deeper for each level of the document), as MsgPack caps nesting at 512 levels: past it the
    /// check is a ContractError rather than a stack overflow. Python's jsonschema stops with a RecursionError at
    /// about the same depth (a condition nested 165 levels deep; this port checks up to about 170).</summary>
    public const int MaxDepth = 512;

    private readonly Dictionary<string, JsonObject> byId = new(StringComparer.Ordinal);

    public SchemaRegistry(IEnumerable<JsonObject> schemas)
    {
        foreach (var schema in schemas) byId[JsonRead.String(schema, "$id", "a contract schema")] = schema;
    }

    private sealed record PathNode(PathNode? Parent, object Segment);

    private static IReadOnlyList<object> PathOf(PathNode? node)
    {
        var segments = new List<object>();
        for (var current = node; current is not null; current = current.Parent) segments.Add(current.Segment);
        segments.Reverse();
        return segments;
    }

    /// <summary>Every error of `instance` against `schema` (a schema of the resource `resourceId`, or one wrapping a
    /// $ref into it), in the order Python's iter_errors yields them.</summary>
    public IReadOnlyList<SchemaError> Errors(JsonNode? instance, JsonNode schema, string resourceId)
    {
        var sink = new Sink(firstOnly: false);
        Descend(instance, schema, resourceId, null, null, 0, sink);
        return sink.Errors;
    }

    /// <summary>Python's is_valid: stops at the first error.</summary>
    public bool IsValid(JsonNode? instance, JsonNode schema, string resourceId) => IsValid(instance, schema, resourceId, null, 0);

    private bool IsValid(JsonNode? instance, JsonNode schema, string resource, PathNode? path, int depth)
    {
        var sink = new Sink(firstOnly: true);
        Descend(instance, schema, resource, path, null, depth, sink);
        return sink.Errors.Count == 0;
    }

    /// <summary>Where errors are collected. The check is eager rather than lazy (Python's generators) so that each
    /// level of a deep document costs a few small stack frames; `firstOnly` stops it at the first error, as Python's
    /// is_valid stops pulling from the generator.</summary>
    private sealed class Sink(bool firstOnly)
    {
        public readonly List<SchemaError> Errors = [];

        public bool Done => firstOnly && Errors.Count > 0;

        public void Add(SchemaError error) => Errors.Add(error);
    }

    private static SchemaError Error(string message, string? keyword, PathNode? path, JsonNode? instance, JsonNode? value) =>
        new(message, keyword, PathOf(path), instance, value, [], null);

    /// <summary>Python's Validator.descend: the errors of `instance` (found at `parent`, then `segment` when the
    /// caller steps into a property or item) against `schema`. As in Python, a false schema's error carries the path
    /// without the segment: descend yields it before it would add its own path element.</summary>
    private void Descend(JsonNode? instance, JsonNode schema, string resource, PathNode? parent, object? segment, int depth, Sink sink)
    {
        if (depth > MaxDepth)
        {
            throw new ContractError($"The document is nested too deeply to check against the schema (more than {MaxDepth} levels of schema)");
        }
        if (StackGuard.Exhausted) throw new ContractError($"The document to check against the schema {StackGuard.TooDeep}");
        if (Json.KindOf(schema) == JsonKind.Bool)
        {
            if (!schema.GetValue<bool>())
            {
                sink.Add(Error($"False schema does not allow {PyFormat.Repr(instance)}", null, parent, instance, null));
            }
            return;
        }
        var path = segment is null ? parent : new PathNode(parent, segment);
        var obj = schema.AsObject();
        foreach (var (keyword, value) in obj)
        {
            if (sink.Done) return;
            Keyword(new Check(keyword, value!, instance, obj, resource, path, depth + 1, sink));
        }
    }

    /// <summary>One keyword of one schema applied to one instance.</summary>
    private sealed record Check(string Keyword, JsonNode Value, JsonNode? Instance, JsonObject Schema, string Resource, PathNode? Path,
        int Depth, Sink Sink)
    {
        public string Repr => PyFormat.Repr(Instance);

        public System.Numerics.BigInteger Count => Json.WholeOf(Value);

        public void Fail(string message) => Sink.Add(Error(message, Keyword, Path, Instance, Value));
    }

    /// <summary>Applies one keyword. Only the keywords that descend into subschemas are dispatched here, each to a
    /// small method, so that the frames on the stack for each level of a deep document stay small; the others are in
    /// Leaf, which never recurses.</summary>
    private void Keyword(Check check)
    {
        switch (check.Keyword)
        {
            case "properties": Properties(check); break;
            case "additionalProperties": AdditionalProperties(check); break;
            case "items": Items(check); break;
            case "propertyNames": PropertyNames(check); break;
            case "allOf": AllOf(check); break;
            case "oneOf": OneOf(check); break;
            case "not": Not(check); break;
            case "if": If(check); break;
            case "$ref": Ref(check); break;
            default: Leaf(check); break;
        }
    }

    private void PropertyNames(Check check)
    {
        if (check.Instance is not JsonObject named) return;
        foreach (var key in named.Select(pair => pair.Key).ToList())
        {
            if (check.Sink.Done) return;
            Descend(JsonValue.Create(key), check.Value, check.Resource, check.Path, null, check.Depth, check.Sink);
        }
    }

    private void AllOf(Check check)
    {
        foreach (var subschema in check.Value.AsArray())
        {
            if (check.Sink.Done) return;
            Descend(check.Instance, subschema!, check.Resource, check.Path, null, check.Depth, check.Sink);
        }
    }

    private void Not(Check check)
    {
        if (IsValid(check.Instance, check.Value, check.Resource, check.Path, check.Depth))
        {
            check.Fail($"{check.Repr} should not be valid under {PyFormat.Repr(check.Value)}");
        }
    }

    /// <summary>Python's if_: then when the instance matches the if schema, else otherwise (each only when present).</summary>
    private void If(Check check)
    {
        string branch = IsValid(check.Instance, check.Value, check.Resource, check.Path, check.Depth) ? "then" : "else";
        if (check.Schema.TryGetPropertyValue(branch, out var subschema)) Descend(check.Instance, subschema!, check.Resource, check.Path, null, check.Depth, check.Sink);
    }

    private void Ref(Check check)
    {
        var (target, node) = Resolve(check.Value.GetValue<string>(), check.Resource);
        Descend(check.Instance, node, target, check.Path, null, check.Depth, check.Sink);
    }

    private static void Leaf(Check check)
    {
        switch (check.Keyword)
        {
            case "type": TypeKeyword(check); break;
            case "required": Required(check); break;
            case "const":
                if (!SchemaEqual(check.Instance, check.Value)) check.Fail($"{PyFormat.Repr(check.Value)} was expected");
                break;
            case "enum":
                if (check.Value.AsArray().All(each => !SchemaEqual(each, check.Instance))) check.Fail($"{check.Repr} is not one of {PyFormat.Repr(check.Value)}");
                break;
            case "minimum":
                if (IsNumber(check.Instance) && PyFormat.CompareNumbers(check.Instance, check.Value) < 0)
                {
                    check.Fail($"{check.Repr} is less than the minimum of {PyFormat.Repr(check.Value)}");
                }
                break;
            case "maximum":
                if (IsNumber(check.Instance) && PyFormat.CompareNumbers(check.Instance, check.Value) > 0)
                {
                    check.Fail($"{check.Repr} is greater than the maximum of {PyFormat.Repr(check.Value)}");
                }
                break;
            case "exclusiveMinimum":
                if (IsNumber(check.Instance) && PyFormat.CompareNumbers(check.Instance, check.Value) <= 0)
                {
                    check.Fail($"{check.Repr} is less than or equal to the minimum of {PyFormat.Repr(check.Value)}");
                }
                break;
            case "minLength":
                if (Json.KindOf(check.Instance) == JsonKind.String && PyFormat.CodePointLength(check.Instance!.GetValue<string>()) < check.Count)
                {
                    check.Fail($"{check.Repr} {(check.Count == 1 ? "should be non-empty" : "is too short")}");
                }
                break;
            case "maxLength":
                if (Json.KindOf(check.Instance) == JsonKind.String && PyFormat.CodePointLength(check.Instance!.GetValue<string>()) > check.Count)
                {
                    check.Fail($"{check.Repr} {(check.Count == 0 ? "is expected to be empty" : "is too long")}");
                }
                break;
            case "pattern":
                if (Json.KindOf(check.Instance) == JsonKind.String && !Pattern(check.Value.GetValue<string>()).IsMatch(check.Instance!.GetValue<string>()))
                {
                    check.Fail($"{check.Repr} does not match {PyFormat.Repr(check.Value)}");
                }
                break;
            case "minItems":
                if (check.Instance is JsonArray few && few.Count < check.Count)
                {
                    check.Fail($"{check.Repr} {(check.Count == 1 ? "should be non-empty" : "is too short")}");
                }
                break;
            case "maxItems":
                if (check.Instance is JsonArray many && many.Count > check.Count)
                {
                    check.Fail($"{check.Repr} {(check.Count == 0 ? "is expected to be empty" : "is too long")}");
                }
                break;
            case "minProperties":
                if (check.Instance is JsonObject small && small.Count < check.Count)
                {
                    check.Fail($"{check.Repr} {(check.Count == 1 ? "should be non-empty" : "does not have enough properties")}");
                }
                break;
            case "maxProperties":
                if (check.Instance is JsonObject large && large.Count > check.Count)
                {
                    check.Fail($"{check.Repr} {(check.Count == 0 ? "is expected to be empty" : "has too many properties")}");
                }
                break;
            case "uniqueItems":
                if (Json.IsTrue(check.Value) && check.Instance is JsonArray items && !Unique(items)) check.Fail($"{check.Repr} has non-unique elements");
                break;
            case "dependentRequired": DependentRequired(check); break;
            default:
                if (!Supported.Contains(check.Keyword))
                {
                    throw new ContractError($"The schema keyword {PyFormat.Str(check.Keyword)} is not one this SDK's validator knows");
                }
                break;
        }
    }

    private static void TypeKeyword(Check check)
    {
        var instance = check.Instance;
        List<string> types = Json.KindOf(check.Value) == JsonKind.String
            ? [check.Value.GetValue<string>()] : check.Value.AsArray().Select(item => item!.GetValue<string>()).ToList();
        if (!types.Any(type => IsType(instance, type))) check.Fail($"{check.Repr} is not of type {string.Join(", ", types.Select(PyFormat.Str))}");
    }

    private void Properties(Check check)
    {
        if (check.Instance is not JsonObject properties) return;
        foreach (var (name, subschema) in check.Value.AsObject())
        {
            if (check.Sink.Done) return;
            if (properties.TryGetPropertyValue(name, out var item)) Descend(item, subschema!, check.Resource, check.Path, name, check.Depth, check.Sink);
        }
    }

    private static void Required(Check check)
    {
        if (check.Instance is not JsonObject present) return;
        foreach (var name in check.Value.AsArray().Select(item => item!.GetValue<string>()))
        {
            if (!present.ContainsKey(name)) check.Fail($"{PyFormat.Str(name)} is a required property");
        }
    }

    /// <summary>Python checks the extra properties of an object schema in the order of a set of str, which changes
    /// from one process to the next (str hashes are randomised); this port uses the document's order. The extras of a
    /// false schema are named sorted, as Python does.</summary>
    private void AdditionalProperties(Check check)
    {
        if (check.Instance is not JsonObject checkedObject) return;
        var known = check.Schema["properties"] as JsonObject;
        var extras = checkedObject.Select(pair => pair.Key).Where(key => known is null || !known.ContainsKey(key)).ToList();
        if (Json.KindOf(check.Value) == JsonKind.Object)
        {
            foreach (var extra in extras)
            {
                if (check.Sink.Done) return;
                Descend(checkedObject[extra], check.Value, check.Resource, check.Path, extra, check.Depth, check.Sink);
            }
        }
        else if (!check.Value.GetValue<bool>() && extras.Count > 0)
        {
            var sorted = PyFormat.Sorted(extras);
            check.Fail($"Additional properties are not allowed ({string.Join(", ", sorted.Select(PyFormat.Str))} {(sorted.Count == 1 ? "was" : "were")} unexpected)");
        }
    }

    private void Items(Check check)
    {
        if (check.Instance is not JsonArray array) return;
        if (Json.KindOf(check.Value) == JsonKind.Bool)
        {
            if (!check.Value.GetValue<bool>() && array.Count > 0)
            {
                string rest = array.Count != 1 ? PyFormat.Repr(array) : PyFormat.Repr(array[0]);
                check.Fail($"Expected at most 0 items but found {array.Count} extra: {rest}");
            }
            return;
        }
        for (int index = 0; index < array.Count; index++)
        {
            if (check.Sink.Done) return;
            Descend(array[index], check.Value, check.Resource, check.Path, index, check.Depth, check.Sink);
        }
    }

    private static void DependentRequired(Check check)
    {
        if (check.Instance is not JsonObject dependent) return;
        foreach (var (property, dependencies) in check.Value.AsObject())
        {
            if (!dependent.ContainsKey(property)) continue;
            foreach (var each in dependencies!.AsArray().Select(item => item!.GetValue<string>()))
            {
                if (!dependent.ContainsKey(each)) check.Fail($"{PyFormat.Str(each)} is a dependency of {PyFormat.Str(property)}");
            }
        }
    }

    /// <summary>Python's oneOf: every failing alternative's errors (each tagged with its alternative's index) become
    /// the context of a "not valid under any" error; when one matches, the later ones are tried and every other match
    /// is named, then the first one.</summary>
    private void OneOf(Check check)
    {
        var alternatives = check.Value.AsArray();
        var all = new List<SchemaError>();
        int? firstValid = null;
        for (int index = 0; index < alternatives.Count; index++)
        {
            var own = new Sink(firstOnly: false);
            Descend(check.Instance, alternatives[index]!, check.Resource, check.Path, null, check.Depth, own);
            if (own.Errors.Count == 0)
            {
                firstValid = index;
                break;
            }
            int alternative = index;
            all.AddRange(own.Errors.Select(error => error with { Alternative = alternative }));
        }
        if (firstValid is null)
        {
            check.Sink.Add(Error($"{check.Repr} is not valid under any of the given schemas", check.Keyword, check.Path, check.Instance, check.Value)
                with { Context = all });
            return;
        }
        var moreValid = new List<JsonNode?>();
        for (int index = firstValid.Value + 1; index < alternatives.Count; index++)
        {
            if (IsValid(check.Instance, alternatives[index]!, check.Resource, check.Path, check.Depth)) moreValid.Add(alternatives[index]);
        }
        if (moreValid.Count > 0)
        {
            moreValid.Add(alternatives[firstValid.Value]);
            check.Fail($"{check.Repr} is valid under each of {string.Join(", ", moreValid.Select(PyFormat.Repr))}");
        }
    }

    private static bool IsNumber(JsonNode? instance) => Json.KindOf(instance) is JsonKind.Int or JsonKind.Float;

    /// <summary>jsonschema's Draft 2020-12 type checker: an integer is an int or a float with no fractional part; a
    /// bool is neither a number nor an integer.</summary>
    private static bool IsType(JsonNode? instance, string type)
    {
        var kind = Json.KindOf(instance);
        return type switch
        {
            "object" => kind == JsonKind.Object,
            "array" => kind == JsonKind.Array,
            "string" => kind == JsonKind.String,
            "boolean" => kind == JsonKind.Bool,
            "null" => kind == JsonKind.Null,
            "number" => kind is JsonKind.Int or JsonKind.Float,
            "integer" => kind == JsonKind.Int
                || (kind == JsonKind.Float && double.IsFinite(instance!.GetValue<double>()) && instance.GetValue<double>() == Math.Floor(instance.GetValue<double>())),
            _ => throw new ContractError($"The schema type {PyFormat.Str(type)} is not a JSON Schema type"),
        };
    }

    /// <summary>jsonschema's _utils.equal: strings and containers by content, numbers by value (1 equals 1.0), and a
    /// bool equal only to a bool (True does not equal 1).</summary>
    public static bool SchemaEqual(JsonNode? a, JsonNode? b)
    {
        if (StackGuard.Exhausted) throw new ContractError($"The values to compare are nested too deeply: each {StackGuard.TooDeep}");
        var x = Json.KindOf(a);
        var y = Json.KindOf(b);
        if (x == JsonKind.String || y == JsonKind.String) return x == y && a!.GetValue<string>() == b!.GetValue<string>();
        if (x == JsonKind.Array && y == JsonKind.Array)
        {
            var left = a!.AsArray();
            var right = b!.AsArray();
            return left.Count == right.Count && left.Zip(right).All(pair => SchemaEqual(pair.First, pair.Second));
        }
        if (x == JsonKind.Object && y == JsonKind.Object)
        {
            var left = a!.AsObject();
            var right = b!.AsObject();
            return left.Count == right.Count && left.All(pair => right.TryGetPropertyValue(pair.Key, out var other) && SchemaEqual(pair.Value, other));
        }
        if (x == JsonKind.Bool || y == JsonKind.Bool) return x == y && a!.GetValue<bool>() == b!.GetValue<bool>();
        if ((x is JsonKind.Int or JsonKind.Float) && (y is JsonKind.Int or JsonKind.Float)) return PyFormat.CompareNumbers(a, b) == 0;
        return x == y;
    }

    private static bool Unique(JsonArray items)
    {
        for (int i = 0; i < items.Count; i++)
        {
            for (int j = i + 1; j < items.Count; j++) if (SchemaEqual(items[i], items[j])) return false;
        }
        return true;
    }

    /// <summary>A pattern as Python's re.search uses it (a match anywhere), read by PythonPattern.</summary>
    private static readonly ConcurrentDictionary<string, Regex> Patterns = new(StringComparer.Ordinal);

    internal static Regex Pattern(string pattern) => Patterns.GetOrAdd(pattern, PythonPattern.Compile);

    private (string Resource, JsonNode Node) Resolve(string reference, string resource)
    {
        int hash = reference.IndexOf('#');
        string id = hash < 0 ? reference : reference[..hash];
        string pointer = hash < 0 ? "" : reference[(hash + 1)..];
        string target = id.Length == 0 ? resource : id;
        if (!byId.TryGetValue(target, out var root)) throw new ContractError($"The schema reference {PyFormat.Str(reference)} names no contract schema");
        JsonNode node = root;
        foreach (var raw in pointer.Split('/').Skip(1))
        {
            string segment = raw.Replace("~1", "/", StringComparison.Ordinal).Replace("~0", "~", StringComparison.Ordinal);
            JsonNode? next = node switch
            {
                JsonObject obj => obj.TryGetPropertyValue(segment, out var child) ? child : null,
                JsonArray array when int.TryParse(segment, System.Globalization.NumberStyles.None, System.Globalization.CultureInfo.InvariantCulture, out int index) && index < array.Count => array[index],
                _ => null,
            };
            node = next ?? throw new ContractError($"The schema reference {PyFormat.Str(reference)} points at nothing (at {PyFormat.Str(segment)})");
        }
        return (target, node);
    }
}

/// <summary>A regular expression written for Python's re module (as the contract's "pattern" keywords are), compiled
/// for .NET so that it matches the same strings. .NET reads most of Python's syntax the same way; the parts it reads
/// differently are rewritten or refused (a ContractError naming the construct), never left to mean something else:
/// <list type="bullet">
/// <item>\d outside a character class is any Unicode decimal digit in Python, a supplementary-plane one (such as
/// U+1D7CF) included, while .NET's \d only sees single UTF-16 units: it is rewritten to an explicit set of every
/// decimal digit, supplementary ones as surrogate pairs.</item>
/// <item>\d inside a character class, \D, \w, \W, \b, \B, \Z, \A, \z, {,n} and . (any character) are refused:
/// their meanings differ (word characters, a supplementary-plane character counted as two, \Z being .NET's \z, \z an
/// error in Python 3.13, {,n} being {0,n} in Python and a literal in .NET). A Python construct .NET cannot parse at
/// all, such as (?P&lt;name&gt;...), is a ContractError naming the pattern. \s and
/// \S are allowed only inside a character class, where the contract writes [\s\S] (every character in both).</item>
/// </list></summary>
internal static class PythonPattern
{
    private static readonly Lazy<string> AnyDigit = new(BuildAnyDigit);

    public static Regex Compile(string pattern)
    {
        string translated = Translate(pattern);
        try
        {
            return new Regex(translated, RegexOptions.CultureInvariant);
        }
        catch (RegexParseException error)
        {
            // a Python construct .NET does not have, such as (?P<name>...) or \N{...}: named, not left as .NET's own error
            throw new ContractError($"The schema pattern {PyFormat.Str(pattern)} is not one .NET's regular expressions can read: {error.Message}");
        }
    }

    public static string Translate(string pattern)
    {
        var output = new System.Text.StringBuilder(pattern.Length);
        bool inClass = false;
        for (int index = 0; index < pattern.Length; index++)
        {
            char current = pattern[index];
            if (current == '\\')
            {
                if (index + 1 >= pattern.Length) throw Refused(pattern, "a trailing backslash");
                char next = pattern[++index];
                switch (next)
                {
                    case 'd' when !inClass:
                        output.Append(AnyDigit.Value);
                        continue;
                    case 's' or 'S' when inClass:
                        break;
                    case 'd' or 'D' or 'w' or 'W' or 's' or 'S' or 'b' or 'B' or 'Z' or 'A' or 'z':
                        throw Refused(pattern, $"\\{next}{(inClass ? " inside a character class" : "")}");
                }
                output.Append(current).Append(next);
                continue;
            }
            if (inClass)
            {
                if (current == ']') inClass = false;
            }
            else if (current == '[')
            {
                inClass = true;
                output.Append(current);
                // a ] (or ^]) right after [ is a literal in both, not the end of the class
                if (index + 1 < pattern.Length && pattern[index + 1] == '^') output.Append(pattern[++index]);
                if (index + 1 < pattern.Length && pattern[index + 1] == ']') output.Append(pattern[++index]);
                continue;
            }
            else if (current == '.')
            {
                throw Refused(pattern, ". (any character)");
            }
            else if (current == '{' && index + 1 < pattern.Length && pattern[index + 1] == ',')
            {
                // {,n} is {0,n} in Python 3.13 and a literal in .NET
                throw Refused(pattern, "{,n}");
            }
            output.Append(current);
        }
        if (inClass) throw Refused(pattern, "an unclosed character class");
        return output.ToString();
    }

    private static ContractError Refused(string pattern, string construct) =>
        new($"The schema pattern {PyFormat.Str(pattern)} uses {construct}, which .NET's regular expressions do not read as Python's re does");

    /// <summary>(?:[every BMP decimal digit]|high-surrogate[low-surrogate range]|...) for every code point whose
    /// Unicode category is Nd (what Python's \d matches in a str pattern).</summary>
    private static string BuildAnyDigit()
    {
        var ranges = new List<(int First, int Last)>();
        for (int code = 0; code <= 0x10FFFF; code++)
        {
            if (code is >= 0xD800 and <= 0xDFFF) continue;
            if (System.Globalization.CharUnicodeInfo.GetUnicodeCategory(code) != System.Globalization.UnicodeCategory.DecimalDigitNumber) continue;
            if (ranges.Count > 0 && ranges[^1].Last == code - 1) ranges[^1] = (ranges[^1].First, code);
            else ranges.Add((code, code));
        }
        static string Unit(int unit) => "\\u" + unit.ToString("X4", System.Globalization.CultureInfo.InvariantCulture);
        var bmp = new System.Text.StringBuilder("[");
        var astral = new List<string>();
        foreach (var (first, last) in ranges)
        {
            if (last <= 0xFFFF)
            {
                bmp.Append(Unit(first)).Append('-').Append(Unit(last));
                continue;
            }
            // split at each change of high surrogate (every 1024 code points)
            for (int start = first; start <= last;)
            {
                int end = Math.Min(last, start | 0x3FF);
                string high = Unit(0xD800 + ((start - 0x10000) >> 10));
                astral.Add($"{high}[{Unit(0xDC00 + ((start - 0x10000) & 0x3FF))}-{Unit(0xDC00 + ((end - 0x10000) & 0x3FF))}]");
                start = end + 1;
            }
        }
        bmp.Append(']');
        return "(?:" + string.Join("|", new[] { bmp.ToString() }.Concat(astral)) + ")";
    }
}

