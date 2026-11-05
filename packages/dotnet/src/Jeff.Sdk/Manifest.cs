using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Jeff.Sdk;

/// <summary>A context field a function reads: its key, description, whether every call must give it, and its code name.</summary>
/// <param name="Key">The field's key as the model reads it.</param>
/// <param name="Description">What the field holds.</param>
/// <param name="Required">True when every call must give it (MissingContext otherwise).</param>
/// <param name="CodeName">The name a call gives it by in its context (lower case, other characters as '_').</param>
public sealed record ContextField(string Key, string Description, bool Required, string CodeName);

/// <summary>The input a function reads: Form "fields" (the state is the context fields, then the input under Key) or
/// "text" (the state is the plain input string).</summary>
public sealed record InputField(string Key, string Description, string CodeName, string Form);

/// <summary>A function's labels as the manifest writes them; Fixed and FixedFirst keep the document's order and map each
/// key to its description (a string or null).</summary>
public sealed record LabelSpec(string Source, OrderedMap<JsonNode?> Fixed, IReadOnlyList<string> Levels, OrderedMap<JsonNode?> FixedFirst,
    string? CallerKeys, long? Max, IReadOnlyList<string> Required, IReadOnlyList<string> Keys, bool? Subset)
{
    // A shipped manifest is shared by every reader in the process, and a JSON node can be changed in place: the
    // descriptions are kept as a private copy and every read gives a fresh copy, so one caller's change is never seen by
    // another. The lists are kept read-only (they cannot be cast back to a list and changed).
    private readonly OrderedMap<JsonNode?> fixedLabels = ReadOnly.Copy(Fixed);
    private readonly OrderedMap<JsonNode?> fixedFirst = ReadOnly.Copy(FixedFirst);
    private readonly IReadOnlyList<string> levels = ReadOnly.List(Levels);
    private readonly IReadOnlyList<string> required = ReadOnly.List(Required);
    private readonly IReadOnlyList<string> keys = ReadOnly.List(Keys);

    /// <summary>The fixed labels, each key mapped to its description (a JSON string, or null when it has none), in the
    /// manifest's order; a fresh copy on every read.</summary>
    public OrderedMap<JsonNode?> Fixed { get => ReadOnly.Copy(fixedLabels); init => fixedLabels = ReadOnly.Copy(value); }

    /// <summary>The labels always offered first, before the caller's own, each mapped to its description; a fresh copy on
    /// every read.</summary>
    public OrderedMap<JsonNode?> FixedFirst { get => ReadOnly.Copy(fixedFirst); init => fixedFirst = ReadOnly.Copy(value); }

    /// <summary>The levels of a score function, lowest first.</summary>
    public IReadOnlyList<string> Levels { get => levels; init => levels = ReadOnly.List(value); }

    /// <summary>The label keys every call must offer.</summary>
    public IReadOnlyList<string> Required { get => required; init => required = ReadOnly.List(value); }

    /// <summary>The keys a describe function's caller must give a description for.</summary>
    public IReadOnlyList<string> Keys { get => keys; init => keys = ReadOnly.List(value); }
}

/// <summary>Where a calibration's data is mirrored, and its checksum.</summary>
/// <param name="Path">The mirrored copy's path.</param>
/// <param name="Sha256">The SHA-256 of the data.</param>
public sealed record DataMirror(string Path, string Sha256);

/// <summary>A calibration block (manifests and GET /v1/functions write it in snake_case). Excluded is only on an INT4
/// base's entry (calibration_by_format): which calibration rows were left out, and why.</summary>
public sealed record Calibration(string Data, long Rows, double TargetPrecision, double Threshold, double PrecisionAtThreshold,
    double CoverageAtThreshold, DataMirror? DataMirror, string? Excluded);

/// <summary>One function of an adapter manifest: its IDs, interface, question type, instructions, labels, calibrations,
/// context and input.</summary>
/// <param name="AdapterId">The adapter's ID.</param>
/// <param name="Key">The function's key within the adapter.</param>
/// <param name="Id">The function's full ID: the adapter ID, a dot, the key.</param>
/// <param name="Interface">The interface it implements (classify, route, rate, ...).</param>
/// <param name="Type">The kind of question it answers.</param>
/// <param name="Instructions">The instructions the model reads.</param>
/// <param name="Labels">Its labels, as the manifest writes them.</param>
/// <param name="AbstainLabels">The labels that mean it declines to decide.</param>
/// <param name="Calibration">Its calibration on the bfloat16 base; null when it is uncalibrated.</param>
/// <param name="Context">The context fields it reads.</param>
/// <param name="Input">The input it reads.</param>
/// <param name="CalibrationByFormat">Its calibration on each INT4 base, by format_id.</param>
public sealed record FunctionSpec(string AdapterId, string Key, string Id, string Interface, QuestionType Type, string Instructions,
    LabelSpec Labels, IReadOnlyList<string> AbstainLabels, Calibration? Calibration, IReadOnlyList<ContextField> Context, InputField Input,
    OrderedMap<Calibration> CalibrationByFormat)
{
    private readonly IReadOnlyList<string> abstainLabels = ReadOnly.List(AbstainLabels);
    private readonly IReadOnlyList<ContextField> context = ReadOnly.List(Context);

    /// <summary>The labels that mean the function declines to decide (the decision then abstains).</summary>
    public IReadOnlyList<string> AbstainLabels { get => abstainLabels; init => abstainLabels = ReadOnly.List(value); }

    /// <summary>The context fields the function reads, in the order the model reads them.</summary>
    public IReadOnlyList<ContextField> Context { get => context; init => context = ReadOnly.List(value); }
}

/// <summary>An adapter manifest. Raw is the document as read (canonical, in its own order). Raw, Tests and Weights
/// are JSON documents, which can be changed in place, and a manifest is shared (Catalogue.Shipped() is read once): each
/// is kept as a private copy and every read gives a fresh copy, so a change one caller makes is never seen by
/// another.</summary>
public sealed partial record Manifest(string Id, string Version, IReadOnlyList<string> Aliases, string LicenceKind, JsonObject Weights, string BaseId,
    string BaseVersion, long MaxOptions, IReadOnlyList<ContextField> Context, InputField Input, OrderedMap<FunctionSpec> Functions,
    IReadOnlyList<string> RoutesTo, JsonObject Tests, JsonObject Raw, string Path)
{
    private readonly IReadOnlyList<string> aliases = ReadOnly.List(Aliases);
    private readonly IReadOnlyList<ContextField> context = ReadOnly.List(Context);
    private readonly IReadOnlyList<string> routesTo = ReadOnly.List(RoutesTo);
    private readonly JsonObject weights = CopyOf(Weights);
    private readonly JsonObject tests = CopyOf(Tests);
    private readonly JsonObject raw = CopyOf(Raw);

    /// <summary>Other names the adapter can be called by.</summary>
    public IReadOnlyList<string> Aliases { get => aliases; init => aliases = ReadOnly.List(value); }

    /// <summary>The context fields shared by the adapter's functions.</summary>
    public IReadOnlyList<ContextField> Context { get => context; init => context = ReadOnly.List(value); }

    /// <summary>The functions or interfaces a router adapter routes to; empty for any other adapter.</summary>
    public IReadOnlyList<string> RoutesTo { get => routesTo; init => routesTo = ReadOnly.List(value); }

    /// <summary>Where the adapter's weights come from, as the manifest writes it; a fresh copy on every read.</summary>
    public JsonObject Weights { get => CopyOf(weights); init => weights = CopyOf(value); }

    /// <summary>The manifest's tests block; a fresh copy on every read.</summary>
    public JsonObject Tests { get => CopyOf(tests); init => tests = CopyOf(value); }

    /// <summary>The manifest as read (canonical, in its own order); a fresh copy on every read.</summary>
    public JsonObject Raw { get => CopyOf(raw); init => raw = CopyOf(value); }

    /// <summary>True for a router adapter (one that routes to other functions or interfaces).</summary>
    public bool IsRouter => RoutesTo.Count > 0;

    private static JsonObject CopyOf(JsonObject document) => (JsonObject)Json.Canonical(document)!;
}

/// <summary>Reading and checking manifests (port of jeff/manifest.py; every check and message is the Python SDK's).</summary>
public sealed partial record Manifest
{
    /// <summary>Python's CODE_NAME, ^[a-z][a-z0-9_]*$, as Python's fullmatch reads it: the whole key (written with \z,
    /// since .NET's $ also matches before a final newline, which fullmatch never lets through).</summary>
    public static readonly Regex CodeName = new(@"^[a-z][a-z0-9_]*\z", RegexOptions.CultureInvariant);
    private const string CodeNamePattern = "^[a-z][a-z0-9_]*$"; // as Python's messages print CODE_NAME.pattern
    private static readonly Regex NotCode = new("[^a-z0-9]+", RegexOptions.CultureInvariant);

    /// <summary>A key's code name: the key itself when it is one, else lower-cased (as Python's str.lower does), every
    /// run of characters other than a-z and 0-9 turned into one '_', and '_' stripped from both ends.</summary>
    public static string CodeNameOf(string key) => CodeName.IsMatch(key) ? key : NotCode.Replace(PythonLowerForCodeName(key), "_").Trim('_');

    /// <summary>key.lower() as far as the code-name rule can see it: which characters become a-z or 0-9. Python's lower
    /// turns A-Z into a-z, U+0130 (capital I with dot) into "i" and a combining dot, and U+212A (the Kelvin sign) into
    /// "k"; every other character lower-cases to characters outside a-z and 0-9 (checked over every code point with
    /// Python 3.13), which the rule replaces anyway, so each becomes '\u0001' here.</summary>
    private static string PythonLowerForCodeName(string key)
    {
        var lowered = new System.Text.StringBuilder(key.Length);
        for (int index = 0; index < key.Length; index++)
        {
            char current = key[index];
            if (char.IsHighSurrogate(current) && index + 1 < key.Length && char.IsLowSurrogate(key[index + 1])) index++;
            if (current is >= 'A' and <= 'Z') lowered.Append((char)(current + 32));
            else if (current is >= 'a' and <= 'z' or >= '0' and <= '9') lowered.Append(current);
            else if (current == '\u0130') lowered.Append("i\u0001");
            else if (current == '\u212A') lowered.Append('k');
            else lowered.Append('\u0001');
        }
        return lowered.ToString();
    }

    internal static Calibration CalibrationFromWire(JsonObject block, string where)
    {
        DataMirror? mirror = null;
        if (block["data_mirror"] is JsonObject data)
        {
            mirror = new DataMirror(JsonRead.String(data, "path", where + " data_mirror"), JsonRead.String(data, "sha256", where + " data_mirror"));
        }
        return new Calibration(JsonRead.String(block, "data", where), JsonRead.Long(block, "rows", where), JsonRead.Number(block, "target_precision", where),
            JsonRead.Number(block, "threshold", where), JsonRead.Number(block, "precision_at_threshold", where),
            JsonRead.Number(block, "coverage_at_threshold", where), mirror, JsonRead.OptionalString(block, "excluded", where));
    }

    private static OrderedMap<JsonNode?> MapOf(JsonObject parent, string key)
    {
        var map = new OrderedMap<JsonNode?>();
        if (parent[key] is JsonObject entries) foreach (var (name, value) in entries) map.Set(name, Json.Canonical(value));
        return map;
    }

    private static List<string> ListOf(JsonObject parent, string key, string where) =>
        parent[key] is null ? [] : JsonRead.Strings(parent, key, where);

    private static LabelSpec LabelsOf(JsonObject raw, string where) => new(
        JsonRead.String(raw, "source", where), MapOf(raw, "fixed"), ListOf(raw, "levels", where), MapOf(raw, "fixed_first"),
        JsonRead.OptionalString(raw, "caller_keys", where), raw["max"] is null ? null : JsonRead.Long(raw, "max", where),
        ListOf(raw, "required", where), ListOf(raw, "keys", where), raw["subset"] is null ? null : JsonRead.Bool(raw, "subset", where));

    private static string CodeNameFor(string where, JsonObject field)
    {
        if (field.ContainsKey("code_name")) return JsonRead.String(field, "code_name", where);
        string key = JsonRead.String(field, "key", where);
        string code = CodeNameOf(key);
        if (!CodeName.IsMatch(code))
        {
            throw new ManifestError($"{where}: field {PyFormat.Str(key)} gives no code name by the rule (lower-case, other characters to "
                + $"'_': {PyFormat.Str(code)}, which is not of the form {CodeNamePattern}); declare its code_name");
        }
        return code;
    }

    private static void CheckCodeNames(string where, IEnumerable<(string Key, string Code)> fields)
    {
        var byCode = new OrderedMap<List<string>>();
        foreach (var (key, code) in fields)
        {
            if (byCode.TryGetValue(code, out var keys)) keys.Add(key);
            else byCode.Set(code, [key]);
        }
        var shared = byCode.Where(pair => pair.Value.Count > 1).ToList();
        if (shared.Count > 0)
        {
            throw new ManifestError($"{where}: fields share a code name, which callers could not tell apart: "
                + string.Join("; ", shared.Select(pair => $"{string.Join(", ", pair.Value.Select(PyFormat.Str))} -> {pair.Key}"))
                + "; declare a distinct code_name for each");
        }
    }

    private static InputField InputFieldOf(string where, JsonObject raw, IReadOnlyList<ContextField> context)
    {
        string key = JsonRead.String(raw, "key", where);
        string form = JsonRead.String(raw, "form", where);
        if (form == "text" && context.Count > 0)
        {
            throw new ManifestError($"{where}: input {PyFormat.Str(key)} has form text (the state is the plain input string), so there can be no "
                + $"context fields, but it has {PyFormat.Repr(context.Select(field => field.Key))}");
        }
        return new InputField(key, JsonRead.String(raw, "description", where), CodeNameFor(where, raw), form);
    }

    private static (IReadOnlyList<ContextField> Context, InputField Input) RequestFields(string where, JsonArray rawContext, JsonObject rawInput)
    {
        var context = rawContext.Select(item => item!.AsObject())
            .Select(item => new ContextField(JsonRead.String(item, "key", where), JsonRead.String(item, "description", where),
                JsonRead.Bool(item, "required", where), CodeNameFor(where, item)))
            .ToList();
        var keys = context.Select(field => field.Key).ToList();
        string inputKey = JsonRead.String(rawInput, "key", where);
        if (keys.Distinct(StringComparer.Ordinal).Count() != keys.Count || keys.Contains(inputKey))
        {
            throw new ManifestError($"{where}: context keys must be unique and differ from the input key ({PyFormat.Repr(keys)}, input {inputKey})");
        }
        var input = InputFieldOf(where, rawInput, context);
        CheckCodeNames(where, context.Select(field => (field.Key, field.CodeName)).Append((input.Key, input.CodeName)));
        return (context, input);
    }

    private static void CheckFunction(FunctionSpec spec, long maxOptions, string where)
    {
        var known = Contract.Interfaces();
        string name = $"{where}: function {spec.Key}";
        if (!known.TryGetValue(spec.Interface, out var iface))
        {
            throw new ManifestError($"{name} names interface {PyFormat.Str(spec.Interface)}, which is not in the interface catalogue "
                + $"({string.Join(", ", known.Keys)})");
        }
        if (spec.Type != iface.QuestionType)
        {
            throw new ManifestError($"{name} has type {spec.Type.Wire()} but interface {spec.Interface} compiles to {iface.QuestionType.Wire()}");
        }
        var labels = spec.Labels;
        if (spec.Type != QuestionType.Choice && (labels.Required.Count > 0 || labels.Keys.Count > 0))
        {
            throw new ManifestError($"{name}: only choice functions have 'required' or 'keys'");
        }
        if (spec.Type == QuestionType.Choice)
        {
            if (labels.Levels.Count > 0) throw new ManifestError($"{name}: choice functions have no levels");
            if (labels.Source == "fixed" && labels.Fixed.Count == 0) throw new ManifestError($"{name}: fixed labels need a non-empty 'fixed'");
            if (labels.Source == "caller" && labels.Max is null)
            {
                throw new ManifestError($"{name}: caller labels need 'max', the most labels the adapter was trained on");
            }
            if (labels.Required.Count > 0 && labels.Source != "subset") throw new ManifestError($"{name}: only subset labels have 'required'");
            if (labels.Keys.Count > 0 && labels.Source != "describe") throw new ManifestError($"{name}: only describe labels have 'keys'");
            if (labels.Source == "subset")
            {
                if (labels.Fixed.Count == 0 || labels.FixedFirst.Count > 0 || !string.IsNullOrEmpty(labels.CallerKeys) || labels.Max is not null)
                {
                    throw new ManifestError($"{name}: subset labels need a non-empty 'fixed' (every allowed label) and no fixed_first, caller_keys or max");
                }
                var stray = labels.Required.Where(key => !labels.Fixed.ContainsKey(key)).ToList();
                if (stray.Count > 0)
                {
                    throw new ManifestError($"{name}: required labels {PyFormat.Repr(stray)} are not in 'fixed' ({PyFormat.Repr(labels.Fixed.Keys)})");
                }
            }
            if (labels.Source == "describe")
            {
                if (labels.Keys.Count == 0 || labels.Fixed.Count > 0 || labels.FixedFirst.Count > 0 || !string.IsNullOrEmpty(labels.CallerKeys)
                    || labels.Max is not null)
                {
                    throw new ManifestError($"{name}: describe labels need non-empty 'keys' and no fixed, fixed_first, caller_keys or max");
                }
                if (labels.Keys.Distinct(StringComparer.Ordinal).Count() != labels.Keys.Count)
                {
                    throw new ManifestError($"{name}: describe keys {PyFormat.Repr(labels.Keys)} repeat a key");
                }
            }
            long count = labels.Fixed.Count + labels.FixedFirst.Count + (labels.Max ?? 0) + labels.Keys.Count;
            if (count > maxOptions) throw new ManifestError($"{name} allows {count} options, more than the base's max_options {maxOptions}");
            var fixedKeys = labels.Fixed.Keys;
            if (iface.Values is not null && !fixedKeys.SequenceEqual(iface.Values, StringComparer.Ordinal))
            {
                throw new ManifestError($"{name}: interface {spec.Interface} requires labels {PyFormat.Repr(iface.Values)}, got {PyFormat.Repr(fixedKeys)}");
            }
        }
        else if (spec.Type == QuestionType.Score)
        {
            if (labels.Levels.Count < 2 || labels.Fixed.Count > 0 || labels.Source != "fixed")
            {
                throw new ManifestError($"{name}: score functions need fixed 'levels' (2 to 10) and nothing else");
            }
        }
        else if (labels.Levels.Count > 0 || labels.Fixed.Keys.Any(key => key is not ("true" or "false")) || labels.Source != "fixed")
        {
            throw new ManifestError($"{name}: noul functions may only describe 'true' and 'false' in 'fixed'");
        }
        var knownLabels = labels.Fixed.Keys.Concat(labels.FixedFirst.Keys).Concat(labels.Keys).ToHashSet(StringComparer.Ordinal);
        var unknown = spec.AbstainLabels.Where(label => !knownLabels.Contains(label)).ToList();
        if (unknown.Count > 0)
        {
            throw new ManifestError($"{name}: abstain_labels {PyFormat.Repr(unknown)} are not among its labels {PyFormat.Repr(PyFormat.Sorted(knownLabels))}");
        }
    }

    /// <summary>tests.function_by as its list of criteria (a single value is the list holding only it).</summary>
    private static List<string> Criteria(JsonNode? functionBy, string where)
    {
        if (Json.KindOf(functionBy) == JsonKind.String) return [functionBy!.GetValue<string>()];
        if (functionBy is JsonArray items && items.All(item => Json.KindOf(item) == JsonKind.String))
        {
            return items.Select(item => item!.GetValue<string>()).ToList();
        }
        throw new ManifestError($"{where}: tests.function_by is {PyFormat.Repr(functionBy)}, which is neither a criterion nor a list of criteria");
    }

    private static void CheckTests(string where, JsonObject tests, OrderedMap<FunctionSpec> functions)
    {
        if (!tests.TryGetPropertyValue("function_by", out var functionBy)) throw new ManifestError($"{where}: tests has no function_by");
        var criteria = Criteria(functionBy, where);
        if (Json.KindOf(functionBy) == JsonKind.String && functionBy!.GetValue<string>() == "question_type")
        {
            var types = functions.Values.Select(spec => spec.Type.Wire()).ToList();
            if (types.Distinct(StringComparer.Ordinal).Count() != types.Count)
            {
                throw new ManifestError($"{where}: tests.function_by question_type needs one function per question type; the functions have types "
                    + PyFormat.Repr(types));
            }
        }
        if (tests["kinds"] is JsonObject kinds)
        {
            if (!criteria.Contains("source_kind"))
            {
                throw new ManifestError($"{where}: tests.kinds maps source.kind values to functions, so tests.function_by must use source_kind "
                    + $"(it is {PyFormat.Repr(functionBy)})");
            }
            var named = kinds.SelectMany(pair => Json.KindOf(pair.Value) == JsonKind.String
                ? (IEnumerable<string>)new[] { pair.Value!.GetValue<string>() }
                : pair.Value!.AsArray().Select(item => item!.GetValue<string>())).ToList();
            var stray = PyFormat.Sorted(named.Where(key => !functions.ContainsKey(key)).Distinct(StringComparer.Ordinal));
            if (stray.Count > 0)
            {
                throw new ManifestError($"{where}: tests.kinds names {PyFormat.Repr(stray)}, which are not its functions ({PyFormat.Repr(functions.Keys)})");
            }
            var several = kinds.Where(pair => Json.KindOf(pair.Value) != JsonKind.String).Select(pair => pair.Key).ToList();
            if (several.Count > 0 && criteria.Count == 1 && criteria[0] == "source_kind")
            {
                throw new ManifestError($"{where}: tests.kinds maps {PyFormat.Repr(several)} to several functions each, but tests.function_by is "
                    + "source_kind alone, so no other criterion can leave one of them: every row of those kinds would match several functions");
            }
        }
        bool uses = criteria.Contains("id_pattern");
        if (!tests.ContainsKey("id_patterns"))
        {
            if (uses)
            {
                throw new ManifestError($"{where}: tests.function_by uses id_pattern, so tests.id_patterns must give every function's row id "
                    + $"pattern ({string.Join(", ", functions.Keys)})");
            }
            return;
        }
        if (!uses)
        {
            throw new ManifestError($"{where}: tests.id_patterns is given, but tests.function_by does not use id_pattern (it is {PyFormat.Repr(functionBy)})");
        }
        var idPatterns = JsonRead.Object(tests, "id_patterns", where);
        var patternNames = idPatterns.Select(pair => pair.Key).ToList();
        if (!patternNames.SequenceEqual(functions.Keys, StringComparer.Ordinal))
        {
            throw new ManifestError($"{where}: tests.id_patterns must name every function, in the functions' order ({PyFormat.Repr(functions.Keys)}); "
                + $"it names {PyFormat.Repr(patternNames)}");
        }
        // The patterns only map test rows, which this SDK never does, so a pattern is never run here: each is checked
        // for being one Python's re.compile accepts, by a port of Python's own syntax checks (PythonReSyntax), with
        // Python's message. A construct that port does not follow is refused, never guessed at.
        foreach (var (key, _) in idPatterns)
        {
            string text = JsonRead.String(idPatterns, key, where);
            try
            {
                PythonReSyntax.Check(text);
            }
            catch (PythonReError error)
            {
                throw new ManifestError($"{where}: tests.id_patterns {key}: {PyFormat.Str(text)} is not a regular expression ({error.Message})");
            }
            catch (PythonReUnchecked refused)
            {
                throw new ManifestError($"{where}: tests.id_patterns {key}: {PyFormat.Str(text)} uses {refused.Message}, which the .NET SDK "
                    + "does not check as Python's re module would; write the pattern without it");
            }
        }
    }

    /// <summary>Refuses the first number in the manifest, in document order, that is not finite (NaN or an infinity,
    /// which YAML's .nan and .inf give), by its place (a JSON pointer) and value, as the Python SDK does
    /// (contract/conformance/manifests.json). JSON has no such number, and a calibration threshold of NaN would turn
    /// calibration off without a word: `confidence &lt; NaN` is always false. The walk keeps its own stack.</summary>
    internal static void CheckFinite(JsonNode? document, string where)
    {
        var pending = new Stack<(JsonNode? Node, string At)>();
        pending.Push((document, ""));
        while (pending.Count > 0)
        {
            var (node, at) = pending.Pop();
            switch (node)
            {
                case JsonObject fields:
                    foreach (var (key, child) in fields.Reverse()) pending.Push((child, $"{at}/{key}"));
                    break;
                case JsonArray items:
                    for (int index = items.Count - 1; index >= 0; index--) pending.Push((items[index], $"{at}/{index}"));
                    break;
                default:
                    if (Json.KindOf(node) == JsonKind.Float && !double.IsFinite(node!.GetValue<double>()))
                    {
                        throw new ManifestError($"{where}: {(at.Length == 0 ? "/" : at)} is {PyFormat.Float(node.GetValue<double>())}, not a "
                            + "finite number, which JSON cannot hold; write a finite number");
                    }
                    break;
            }
        }
    }

    private static readonly Regex VersionPart = new(@"^v[0-9]+\z", RegexOptions.CultureInvariant);

    /// <summary>The ID's prefix (jeff.&lt;domain&gt;), its name's hyphen-separated segments, and the indexes of those
    /// that are a version part (v and digits, the whole segment).</summary>
    private static (string Prefix, List<string> Segments, List<int> Versions) VersionParts(string adapterId)
    {
        int dot = adapterId.LastIndexOf('.');
        var segments = adapterId[(dot + 1)..].Split('-').ToList();
        var versions = Enumerable.Range(0, segments.Count).Where(index => VersionPart.IsMatch(segments[index])).ToList();
        return (adapterId[..(dot + 1)], segments, versions);
    }

    /// <summary>The adapter ID without its version part (contract/schemas/manifest.schema.json, aliases): the one
    /// hyphen-separated segment of its name (the part after the second dot) that is v and digits, removed with one
    /// hyphen, as jeff.routing.router-open-v14 -> jeff.routing.router-open. Null when the name has no such segment,
    /// more than one, or nothing besides it.</summary>
    public static string? UnversionedId(string adapterId)
    {
        var (prefix, segments, versions) = VersionParts(adapterId);
        if (versions.Count != 1 || segments.Count == 1) return null;
        segments.RemoveAt(versions[0]);
        return $"{prefix}{string.Join('-', segments)}";
    }

    /// <summary>No alias is listed twice, and an alias of the ID form (the schema allows it as
    /// jeff.&lt;domain&gt;.&lt;name&gt;) must be the adapter's unversioned ID.</summary>
    private static void CheckAliases(string where, string adapterId, IReadOnlyList<string> aliases)
    {
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var alias in aliases)
        {
            if (!seen.Add(alias)) throw new ManifestError($"{where}: the alias {alias} is listed twice");
            if (!alias.StartsWith("jeff.", StringComparison.Ordinal)) continue;
            var (_, segments, versions) = VersionParts(adapterId);
            string form = $"{where}: the alias {alias} has the form of an adapter ID, which only the adapter's ID without its "
                + "version part may have, and ";
            if (versions.Count == 0)
                throw new ManifestError($"{form}{adapterId} has no version part (one segment v<digits> in its name besides "
                    + "others, as in jeff.routing.router-open-v14)");
            if (versions.Count > 1)
                throw new ManifestError($"{form}{adapterId} has more than one version part "
                    + $"({string.Join(", ", versions.Select(index => segments[index]))}), so it has no ID without one");
            if (segments.Count == 1)
                throw new ManifestError($"{form}the name of {adapterId} is only a version part, so it has no ID without one");
            string? expected = UnversionedId(adapterId);
            if (alias != expected)
                throw new ManifestError($"{where}: the alias {alias} has the form of an adapter ID, so it must be {expected}, "
                    + $"the ID {adapterId} without its version part");
        }
    }

    /// <summary>A manifest document (canonical or not) as a checked Manifest; `path` names it in every message.</summary>
    public static Manifest FromDict(JsonNode? raw, string path)
    {
        string where = path;
        var document = Json.Canonical(raw);
        CheckFinite(document, where);
        Contract.Validate("manifest", document, where);
        var plain = document!.AsObject();
        string adapterId = JsonRead.String(plain, "id", where);
        var aliases = JsonRead.Strings(plain, "aliases", where);
        CheckAliases(where, adapterId, aliases);
        var (context, input) = RequestFields(where, JsonRead.Array(plain, "context", where), JsonRead.Object(plain, "input", where));
        var baseBlock = JsonRead.Object(plain, "base", where);
        long maxOptions = JsonRead.Long(baseBlock, "max_options", where);
        var functions = new OrderedMap<FunctionSpec>();
        foreach (var (key, node) in JsonRead.Object(plain, "functions", where))
        {
            var body = node!.AsObject();
            string at = $"{where}: function {key}";
            Calibration? calibration = body["calibration"] is JsonObject block ? CalibrationFromWire(block, $"{at} calibration") : null;
            var byFormat = new OrderedMap<Calibration>();
            if (body["calibration_by_format"] is JsonObject formats)
            {
                foreach (var (formatId, entry) in formats) byFormat.Set(formatId, CalibrationFromWire(entry!.AsObject(), $"{at} calibration_by_format {formatId}"));
            }
            if (byFormat.Count > 0 && calibration is null)
            {
                throw new ManifestError($"{where}: function {key} has calibration_by_format entries ({string.Join(", ", byFormat.Keys)}) but calibration "
                    + "null; a function uncalibrated on the bfloat16 base stays uncalibrated in every format");
            }
            var own = body.ContainsKey("input")
                ? RequestFields(at, JsonRead.Array(body, "context", at), JsonRead.Object(body, "input", at))
                : (context, input);
            var spec = new FunctionSpec(adapterId, key, $"{adapterId}.{key}", JsonRead.String(body, "interface", at),
                QuestionTypes.Parse(JsonRead.String(body, "type", at), at), JsonRead.String(body, "instructions", at),
                LabelsOf(JsonRead.Object(body, "labels", at), at), JsonRead.Strings(body, "abstain_labels", at), calibration, own.Item1, own.Item2, byFormat);
            CheckFunction(spec, maxOptions, where);
            functions.Set(key, spec);
        }
        var tests = JsonRead.Object(plain, "tests", where);
        CheckTests(where, tests, functions);
        var routesTo = JsonRead.Strings(plain, "routes_to", where);
        if (routesTo.Count > 0)
        {
            var only = functions.Values;
            var first = only.Count > 0 ? only[0] : null;
            if (only.Count != 1 || first is null || first.Interface != "route" || first.Labels.Source != "subset"
                || !first.Labels.Fixed.Keys.SequenceEqual(routesTo, StringComparer.Ordinal))
            {
                throw new ManifestError($"{where}: a router has exactly one route function with subset labels whose keys equal routes_to "
                    + $"{PyFormat.Repr(routesTo)} (dispatch offers the targets that lead to the requested interface)");
            }
            if (first.Labels.Required.Count > 0)
            {
                throw new ManifestError($"{where}: a router's labels have no required keys; dispatch offers only the targets that lead to the "
                    + "requested interface");
            }
        }
        var licence = JsonRead.Object(plain, "licence", where);
        return new Manifest(adapterId, JsonRead.String(plain, "version", where), aliases,
            JsonRead.String(licence, "kind", where), JsonRead.Object(plain, "weights", where), JsonRead.String(baseBlock, "id", where),
            JsonRead.String(baseBlock, "version", where), maxOptions, context, input, functions, routesTo, tests, plain, path);
    }

    /// <summary>Reads a manifest from YAML text; `path` names it in messages. ManifestError or ContractError naming what is wrong.</summary>
    public static Manifest Load(string text, string path) => FromDict(PyYaml.Load(text, path), path);

    /// <summary>Reads the manifest file at `path` (UTF-8). ManifestError or ContractError naming what is wrong.</summary>
    public static Manifest LoadFile(string path) => Load(ReadUtf8(path), path);

    private static readonly System.Text.UTF8Encoding StrictUtf8 = new(encoderShouldEmitUTF8Identifier: false, throwOnInvalidBytes: true);

    /// <summary>A file's text as Python's read_text(encoding="utf-8") reads it: bytes that are not UTF-8 are an error,
    /// never replaced.</summary>
    internal static string ReadUtf8(string path) => File.ReadAllText(path, StrictUtf8);

    /// <summary>The calibration a function decides with on the engine's base: its bfloat16 block for a bfloat16 base
    /// (formatId null), else its entry for that format; a calibrated function without one is refused.</summary>
    internal static Calibration? ResolveCalibration(string functionId, Calibration? calibration, OrderedMap<Calibration> byFormat, string? formatId)
    {
        if (formatId is null || calibration is null) return calibration;
        if (byFormat.TryGetValue(formatId, out var entry)) return entry;
        string calibratedOn = byFormat.Count > 0 ? string.Join(", ", byFormat.Keys) : "no INT4 base";
        throw new ManifestError($"{functionId} has no calibration for the INT4 base this engine serves ({formatId}); it is calibrated on the "
            + $"bfloat16 base and on {calibratedOn}. Fit its threshold on this base with `jeff calibrate {functionId} --base <the INT4 base "
            + "folder>` (for an adapter served from an adapter folder, `jeff calibrate <that folder> --base <the INT4 base folder>`, which "
            + "writes into the folder's manifest.yaml)");
    }

    /// <summary>The calibration `fn` decides with on a base: its bfloat16 calibration when formatId is null, else its entry for that
    /// INT4 format_id; null when it is uncalibrated. ManifestError when a calibrated function has no entry for that format.</summary>
    public static Calibration? CalibrationFor(FunctionSpec fn, string? formatId) =>
        ResolveCalibration(fn.Id, fn.Calibration, fn.CalibrationByFormat, formatId);
}
