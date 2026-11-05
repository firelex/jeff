using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace Jeff.Sdk;

/// <summary>One call of a function: its reference, input, context (by code name) and labels. The constructor copies
/// all three JSON values (Json.Canonical), so the caller's nodes stay the caller's.</summary>
public sealed class Call
{
    /// <summary>A call of `function` (an ID, alias or short reference, optionally @version) with this input, context and labels.</summary>
    /// <param name="function">The function to call, as the catalogue resolves it.</param>
    /// <param name="input">The function's input: a string or a JSON object, as its manifest's input field says.</param>
    /// <param name="context">The context fields, by code name; none when the function needs none.</param>
    /// <param name="labels">A JSON array of keys (subset labels), or an object of key to description (caller and describe labels).</param>
    public Call(string function, JsonNode? input, JsonObject? context = null, JsonNode? labels = null)
    {
        Function = function;
        Input = Json.Canonical(input);
        Context = context is null ? null : (JsonObject)Json.Canonical(context)!;
        Labels = Json.Canonical(labels);
    }

    /// <summary>The function reference, as given.</summary>
    public string Function { get; }

    /// <summary>A copy of the input.</summary>
    public JsonNode? Input { get; }

    /// <summary>A copy of the context fields, by code name; null when none were given.</summary>
    public JsonObject? Context { get; }

    /// <summary>A JSON array of keys (subset labels), or an object of key to description (caller and describe labels).</summary>
    public JsonNode? Labels { get; }
}

/// <summary>The question part of an engine row: its type, instructions and criteria (an object of option to
/// description for a choice, a list of levels for a score, the described answers of a noul, or null).</summary>
public sealed record RowQuestion(QuestionType Type, string Instructions, JsonNode? Criteria);

/// <summary>The row the model reads: the state (the context fields then the input, or the plain input string) and the
/// question.</summary>
public sealed record Row(JsonNode State, RowQuestion Question)
{
    /// <summary>The row as the engine socket carries it: {"state": ..., "question": {"type", "instructions", "criteria"?}}.</summary>
    public JsonObject ToJson()
    {
        var question = new JsonObject { ["type"] = Question.Type.Wire(), ["instructions"] = Question.Instructions };
        if (Question.Criteria is not null) question["criteria"] = Json.Canonical(Question.Criteria);
        return new JsonObject { ["state"] = Json.Canonical(State), ["question"] = question };
    }
}

/// <summary>A function bound to its context and labels: the bound context (by each field's exact key, in field order),
/// the criteria (wire key to description, choice functions only), each wire key's caller key, and the calibration the
/// function decides with on the engine's base (null when uncalibrated there).</summary>
internal sealed record Binding(FunctionSpec Function, Manifest Manifest, OrderedMap<JsonNode?> Context, OrderedMap<JsonNode?>? Criteria,
    OrderedMap<string> WireToCaller, Calibration? Calibration);

internal sealed record BoundCall(Call Call, Binding Binding);

/// <summary>Binding a function and building the row the model reads (port of jeff/binding.py). Context and labels are
/// bound before the input, so one binding gives one fixed prompt prefix the engine can reuse.</summary>
internal static class Bindings
{
    private const string PySpace = "\\t\\n\\v\\f\\r\\x1c-\\x20\\x85\\xa0\\u1680\\u2000-\\u200a\\u2028\\u2029\\u202f\\u205f\\u3000";

    // Python's NUMBER_LIKE (^\s*[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?\s*$), with Python's \s spelled out; \d is any
    // Unicode decimal digit in both languages.
    private static readonly Regex NumberLike = new($"^[{PySpace}]*[+-]?(\\d+\\.?\\d*|\\.\\d+)([eE][+-]?\\d+)?[{PySpace}]*$", RegexOptions.CultureInvariant);

    private static OrderedMap<JsonNode?> ContextOf(FunctionSpec fn, JsonObject? given)
    {
        // no context given is an empty one, as Python's `context or {}`
        var values = given ?? new JsonObject();
        var names = values.Select(pair => pair.Key).ToList();
        if (fn.Input.Form == "text" && names.Count > 0)
        {
            throw new UnknownContext($"{fn.Id} takes plain text only (its input {fn.Input.CodeName} is the whole state the adapter sees), so it "
                + $"has no context fields; do not pass context (got {string.Join(", ", PyFormat.Sorted(names))})");
        }
        var known = fn.Context.Select(field => field.CodeName).ToHashSet(StringComparer.Ordinal);
        var unknown = PyFormat.Sorted(names.Where(name => !known.Contains(name)));
        if (unknown.Count > 0)
        {
            var codeOf = fn.Context.ToDictionary(field => field.Key, field => field.CodeName, StringComparer.Ordinal);
            var exact = unknown.Where(codeOf.ContainsKey).Select(key => $"{PyFormat.Str(key)} as {codeOf[key]}").ToList();
            string fields = fn.Context.Count > 0 ? string.Join(", ", fn.Context.Select(field => field.CodeName)) : "none";
            throw new UnknownContext($"{fn.Id} has no context field(s) {string.Join(", ", unknown)}; its fields are {fields}"
                + (exact.Count > 0 ? $"; context is given by code name, so pass {string.Join(", ", exact)}" : ""));
        }
        var missing = fn.Context.Where(field => field.Required && !values.ContainsKey(field.CodeName)).Select(field => field.CodeName).ToList();
        if (missing.Count > 0) throw Errors.MissingContextError(fn.Id, missing);
        var bound = new OrderedMap<JsonNode?>();
        foreach (var field in fn.Context)
        {
            if (values.TryGetPropertyValue(field.CodeName, out var value)) bound.Set(field.Key, Json.Canonical(value));
        }
        return bound;
    }

    private static List<string> LabelKeys(JsonNode labels, string refusal) =>
        labels is JsonArray array && array.All(key => Json.KindOf(key) == JsonKind.String)
            ? array.Select(key => key!.GetValue<string>()).ToList()
            : throw new InvalidLabels(refusal);

    private static OrderedMap<string> Identity(IEnumerable<string> keys) => new(keys.Select(key => new KeyValuePair<string, string>(key, key)));

    private static List<string> Repeated(List<string> keys) =>
        PyFormat.Sorted(keys.Where((key, index) => keys.IndexOf(key) != index).Distinct(StringComparer.Ordinal));

    private static (OrderedMap<JsonNode?> Criteria, OrderedMap<string> WireToCaller) SubsetCriteria(FunctionSpec fn, JsonNode labels)
    {
        var spec = fn.Labels;
        string fixedKeys = string.Join(", ", spec.Fixed.Keys);
        var keys = LabelKeys(labels, $"{fn.Id} offers a choice of its own labels; pass labels= as a list of keys from {fixedKeys}");
        if (keys.Count == 0) throw Errors.MissingLabelsError(fn.Id);
        var twice = Repeated(keys);
        if (twice.Count > 0) throw new InvalidLabels($"{fn.Id}: label(s) {string.Join(", ", twice)} given more than once");
        var unknown = keys.Where(key => !spec.Fixed.ContainsKey(key)).ToList();
        if (unknown.Count > 0) throw new InvalidLabels($"{fn.Id}: label(s) {string.Join(", ", unknown)} are not among its labels {fixedKeys}");
        var missing = spec.Required.Where(key => !keys.Contains(key)).ToList();
        if (missing.Count > 0) throw new InvalidLabels($"{fn.Id}: label(s) {string.Join(", ", missing)} must always be offered");
        var criteria = new OrderedMap<JsonNode?>(keys.Select(key => new KeyValuePair<string, JsonNode?>(key, Json.Canonical(spec.Fixed[key]))));
        return (criteria, Identity(keys));
    }

    /// <summary>Fixed labels with subset true: the caller's keys in the manifest's order, each with its fixed description.</summary>
    private static (OrderedMap<JsonNode?> Criteria, OrderedMap<string> WireToCaller) FixedSubsetCriteria(FunctionSpec fn, JsonNode labels)
    {
        var spec = fn.Labels;
        string fixedKeys = string.Join(", ", spec.Fixed.Keys);
        var keys = LabelKeys(labels, $"{fn.Id} has fixed labels and may offer some of them; pass labels= as a list of keys from {fixedKeys}, or no "
            + "labels= to offer them all");
        if (keys.Count == 0)
        {
            throw new InvalidLabels($"{fn.Id}: labels= is an empty list; pass at least one key from {fixedKeys}, or no labels= to offer them all");
        }
        var twice = Repeated(keys);
        if (twice.Count > 0) throw new InvalidLabels($"{fn.Id}: label(s) {string.Join(", ", twice)} given more than once");
        var unknown = keys.Where(key => !spec.Fixed.ContainsKey(key)).ToList();
        if (unknown.Count > 0) throw new InvalidLabels($"{fn.Id}: label(s) {string.Join(", ", unknown)} are not among its labels {fixedKeys}");
        var offered = keys.ToHashSet(StringComparer.Ordinal);
        var criteria = new OrderedMap<JsonNode?>(spec.Fixed.Where(pair => offered.Contains(pair.Key))
            .Select(pair => new KeyValuePair<string, JsonNode?>(pair.Key, Json.Canonical(pair.Value))));
        return (criteria, Identity(criteria.Keys));
    }

    private static (OrderedMap<JsonNode?> Criteria, OrderedMap<string> WireToCaller) DescribeCriteria(FunctionSpec fn, JsonNode labels)
    {
        var spec = fn.Labels;
        if (labels is not JsonObject given)
        {
            throw new InvalidLabels($"{fn.Id}: pass labels= as an object with exactly the keys {string.Join(", ", spec.Keys)}, each with its description");
        }
        var extra = given.Select(pair => pair.Key).Where(key => !spec.Keys.Contains(key)).ToList();
        var missing = spec.Keys.Where(key => !given.ContainsKey(key)).ToList();
        if (extra.Count > 0 || missing.Count > 0)
        {
            throw new InvalidLabels($"{fn.Id} takes exactly the labels {string.Join(", ", spec.Keys)}"
                + (extra.Count > 0 ? $"; unknown: {string.Join(", ", extra)}" : "")
                + (missing.Count > 0 ? $"; missing: {string.Join(", ", missing)}" : ""));
        }
        var criteria = new OrderedMap<JsonNode?>(spec.Keys.Select(key => new KeyValuePair<string, JsonNode?>(key, Json.Canonical(given[key]))));
        return (criteria, Identity(spec.Keys));
    }

    private static (OrderedMap<JsonNode?> Criteria, OrderedMap<string> WireToCaller) ChoiceCriteria(FunctionSpec fn, JsonNode? labels)
    {
        var spec = fn.Labels;
        if (spec.Source == "fixed")
        {
            if (labels is null)
            {
                var all = new OrderedMap<JsonNode?>(spec.Fixed.Select(pair => new KeyValuePair<string, JsonNode?>(pair.Key, Json.Canonical(pair.Value))));
                return (all, Identity(spec.Fixed.Keys));
            }
            if (spec.Subset != true) throw new InvalidLabels($"{fn.Id} has fixed labels ({string.Join(", ", spec.Fixed.Keys)}); do not pass labels=");
            return FixedSubsetCriteria(fn, labels);
        }
        if (labels is null) throw Errors.MissingLabelsError(fn.Id);
        if (spec.Source == "subset") return SubsetCriteria(fn, labels);
        if (spec.Source == "describe") return DescribeCriteria(fn, labels);
        if (labels is not JsonObject caller) throw new InvalidLabels($"{fn.Id}: pass labels= as an object of key -> description");
        if (caller.Count == 0) throw Errors.MissingLabelsError(fn.Id);
        if (spec.Max is { } max && caller.Count > max) throw new TooManyLabels($"{fn.Id} got {caller.Count} labels; it was trained on at most {max}");
        foreach (var (key, _) in caller)
        {
            if (spec.FixedFirst.ContainsKey(key))
            {
                throw new InvalidLabels($"{fn.Id}: label {PyFormat.Str(key)} is reserved (always listed first: {string.Join(", ", spec.FixedFirst.Keys)}); "
                    + "rename yours");
            }
            if (NumberLike.IsMatch(key)) throw new InvalidLabels($"{fn.Id}: label key {PyFormat.Str(key)} looks like a number; use a word");
        }
        var criteria = new OrderedMap<JsonNode?>(spec.FixedFirst.Select(pair => new KeyValuePair<string, JsonNode?>(pair.Key, Json.Canonical(pair.Value))));
        var wireToCaller = Identity(spec.FixedFirst.Keys);
        int index = 0;
        foreach (var (key, description) in caller)
        {
            index++;
            string wire = spec.CallerKeys is not null
                ? spec.CallerKeys.Replace("{n}", index.ToString(System.Globalization.CultureInfo.InvariantCulture), StringComparison.Ordinal)
                : key;
            if (criteria.ContainsKey(wire)) throw new InvalidLabels($"{fn.Id}: label {PyFormat.Str(key)} collides with option {PyFormat.Str(wire)}");
            criteria.Set(wire, Json.Canonical(description));
            wireToCaller.Set(wire, key);
        }
        return (criteria, wireToCaller);
    }

    /// <summary>calibration: the block the function decides with on the engine's base format (Manifest.CalibrationFor),
    /// resolved before binding, or null when it is uncalibrated there.</summary>
    public static Binding Bind(Manifest manifest, FunctionSpec fn, JsonObject? context, JsonNode? labels, Calibration? calibration)
    {
        var boundContext = ContextOf(fn, context);
        if (fn.Type == QuestionType.Choice)
        {
            var (criteria, wireToCaller) = ChoiceCriteria(fn, labels);
            return new Binding(fn, manifest, boundContext, criteria, wireToCaller, calibration);
        }
        if (labels is not null) throw new InvalidLabels($"{fn.Id} is a {fn.Type.Wire()} function; it takes no labels=");
        return new Binding(fn, manifest, boundContext, null, Identity(OptionKeysFor(fn)), calibration);
    }

    public static IReadOnlyList<string> OptionKeysFor(FunctionSpec fn) => fn.Type switch
    {
        QuestionType.Noul => ["false", "true"],
        QuestionType.Score => fn.Labels.Levels.Select((_, index) => index.ToString(System.Globalization.CultureInfo.InvariantCulture)).ToList(),
        _ => throw new InvalidOperationException("choice functions get their option keys from a binding"),
    };

    public static IReadOnlyList<string> OptionKeys(Binding binding) => binding.Criteria is not null ? binding.Criteria.Keys : OptionKeysFor(binding.Function);

    /// <summary>The state the model reads: form fields, the bound context then the input under its key; form text, the
    /// input itself, which must be a string.</summary>
    public static JsonNode RequestState(string functionId, InputField spec, OrderedMap<JsonNode?> context, JsonNode? input)
    {
        if (spec.Form == "fields")
        {
            var state = new JsonObject();
            foreach (var (key, value) in context) state[key] = Json.Canonical(value);
            state[spec.Key] = Json.Canonical(input);
            return state;
        }
        if (Json.KindOf(input) != JsonKind.String)
        {
            throw new InvalidRequest($"{functionId} takes plain text: its input {spec.CodeName} must be a string, got {PyFormat.TypeName(input)}");
        }
        return Json.Canonical(input)!;
    }

    private static JsonObject ObjectOf(OrderedMap<JsonNode?> map)
    {
        var obj = new JsonObject();
        foreach (var (key, value) in map) obj[key] = Json.Canonical(value);
        return obj;
    }

    public static Row BuildRow(Binding binding, JsonNode? input)
    {
        var fn = binding.Function;
        var state = RequestState(fn.Id, fn.Input, binding.Context, input);
        JsonNode? criteria = fn.Type switch
        {
            QuestionType.Choice => ObjectOf(binding.Criteria ?? throw new InvalidOperationException($"the binding of choice function {fn.Id} has no criteria")),
            QuestionType.Score => Json.Strings(fn.Labels.Levels),
            _ => fn.Labels.Fixed.Count > 0 ? ObjectOf(fn.Labels.Fixed) : null,
        };
        return new Row(state, new RowQuestion(fn.Type, fn.Instructions, criteria));
    }
}
