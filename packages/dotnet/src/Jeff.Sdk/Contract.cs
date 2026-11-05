using System.Collections.Concurrent;
using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>The kind of question a function answers.</summary>
public enum QuestionType
{
    /// <summary>Pick one of the options (classify, route, detect and other choice interfaces).</summary>
    Choice,

    /// <summary>A yes-or-no question, answered true or false.</summary>
    Noul,

    /// <summary>A level on a fixed scale, answered as a whole number.</summary>
    Score,
}

internal static class QuestionTypes
{
    public static string Wire(this QuestionType type) => type switch
    {
        QuestionType.Choice => "choice",
        QuestionType.Noul => "noul",
        QuestionType.Score => "score",
        _ => throw new ContractError($"{(int)type} is not a QuestionType"),
    };

    public static QuestionType Parse(string text, string where) => text switch
    {
        "choice" => QuestionType.Choice,
        "noul" => QuestionType.Noul,
        "score" => QuestionType.Score,
        _ => throw new ContractError($"{where} has an unknown question_type {PyFormat.Str(text)}"),
    };
}

internal sealed record InterfaceSpec(string Name, QuestionType QuestionType, IReadOnlyList<string>? Values, string Description);

internal sealed record ErrorSpec(string Name, long? Status, string Meaning);

/// <summary>The contract shipped with the package: the JSON Schemas, the interface catalogue and the error catalogue
/// (port of jeff/contract.py). Loaded once, on first use.</summary>
internal static class Contract
{
    public static readonly IReadOnlyList<string> Schemas =
    [
        "manifest", "decision", "graph", "graph_result", "socket", "decide_request", "decide_response", "functions_response",
        "graphs_response", "systemone_request", "systemone_response", "models_response", "served_request", "adapters_response",
        "health_response", "login_request", "login_response", "accounts_keys", "accounts_usage",
    ];

    private sealed record Loaded(IReadOnlyDictionary<string, JsonObject> ByName, SchemaRegistry Registry);

    private static readonly Lazy<Loaded> State = new(Load);
    private static readonly Lazy<OrderedMap<InterfaceSpec>> InterfaceSpecs = new(LoadInterfaces);
    private static readonly Lazy<OrderedMap<ErrorSpec>> ErrorSpecs = new(LoadErrors);
    private static readonly ConcurrentDictionary<string, JsonObject> DefinitionSchemas = new(StringComparer.Ordinal);

    private static Loaded Load()
    {
        var byName = new Dictionary<string, JsonObject>(StringComparer.Ordinal);
        foreach (var name in Schemas)
        {
            string path = $"contract/schemas/{name}.schema.json";
            if (Json.Parse(ShippedFiles.ReadText(path), path) is not JsonObject schema) throw new ContractError($"{path} is not a JSON object");
            SchemaRegistry.CheckSchema(schema, path);
            byName[name] = schema;
        }
        return new Loaded(byName, new SchemaRegistry(byName.Values));
    }

    private static JsonObject Schema(string name) => State.Value.ByName.TryGetValue(name, out var schema)
        ? schema
        : throw new ContractError($"There is no contract schema named {PyFormat.Str(name)} (the schemas: {string.Join(", ", Schemas)})");

    /// <summary>A copy of schema `name` (contract/schemas/&lt;name&gt;.schema.json).</summary>
    public static JsonObject LoadSchema(string name) => (JsonObject)Json.Canonical(Schema(name))!;

    /// <summary>The schema a document is checked against (schema `name`, or a $ref to one of its $defs) and the $id it
    /// resolves references against.</summary>
    private static (JsonNode Schema, string Id) Target(string name, string? definition)
    {
        var schema = Schema(name);
        string id = JsonRead.String(schema, "$id", $"the {name} schema");
        if (definition is null) return (schema, id);
        if (schema["$defs"] is not JsonObject definitions || !definitions.ContainsKey(definition))
        {
            throw new ContractError($"The {name} schema has no definition {PyFormat.Str(definition)}");
        }
        return (DefinitionSchemas.GetOrAdd($"{id}#/$defs/{definition}", reference => new JsonObject { ["$ref"] = reference }), id);
    }

    private static IReadOnlyList<SchemaError> ErrorsOf(string name, JsonNode? document, string? definition)
    {
        var (schema, id) = Target(name, definition);
        return State.Value.Registry.Errors(document, schema, id);
    }

    /// <summary>ContractError listing every problem when `document` does not match schema `name` (or, with
    /// `definition`, that one of its $defs), each with its path and the problems of a failed oneOf's alternatives.</summary>
    public static void Validate(string name, JsonNode? document, string where, string? definition = null)
    {
        var problems = ErrorsOf(name, document, definition).OrderBy(problem => problem.Path, PathOrder.Python).ToList();
        if (problems.Count == 0) return;
        var lines = new List<string>();
        foreach (var problem in problems)
        {
            lines.Add($"  at /{string.Join("/", problem.Path.Select(Segment))}: {problem.Message}");
            lines.AddRange(problem.Context.Select(sub => $"    - {sub.Message}"));
        }
        string shape = definition is null ? $"the {name} schema" : $"{definition} in the {name} schema";
        throw new ContractError($"{where} does not match {shape}:\n{string.Join("\n", lines)}");
    }

    /// <summary>Whether `document` matches schema `name` (or, with `definition`, that one of its $defs); stops at the
    /// first problem, as Python's is_valid does.</summary>
    public static bool IsValid(string name, JsonNode? document, string? definition = null)
    {
        var (schema, id) = Target(name, definition);
        return State.Value.Registry.IsValid(document, schema, id);
    }

    /// <summary>One short line naming the first thing wrong with a document that does not match schema `name` (port of
    /// jeff.contract.first_problem): wherever the first problem is a oneOf, the document is judged against the
    /// alternative it comes closest to matching (the one whose problems lie deepest, then the one with the fewest),
    /// repeatedly; a value with too many items or properties is reported by its count; a long message is cut at 200
    /// characters.</summary>
    public static string FirstProblem(string name, JsonNode? document, string? definition = null)
    {
        var problems = ErrorsOf(name, document, definition);
        if (problems.Count == 0) throw new ContractError($"first_problem was asked about a document that matches the {name} schema");
        var problem = First(problems, StringPath);
        while (problem.Keyword == "oneOf" && problem.Context.Count > 0)
        {
            var byAlternative = new OrderedMap<List<SchemaError>>();
            foreach (var sub in problem.Context)
            {
                string key = sub.Alternative!.Value.ToString(System.Globalization.CultureInfo.InvariantCulture);
                if (byAlternative.TryGetValue(key, out var group)) group.Add(sub);
                else byAlternative.Set(key, [sub]);
            }
            var closest = byAlternative.Values[0];
            foreach (var errors in byAlternative.Values.Skip(1))
            {
                int depth = errors.Min(error => error.Path.Count), best = closest.Min(error => error.Path.Count);
                if (depth > best || (depth == best && errors.Count < closest.Count)) closest = errors;
            }
            problem = First(closest, StringPath);
        }
        string message;
        if (problem.Keyword is "maxItems" or "maxProperties" && problem.Instance is JsonArray or JsonObject)
        {
            string kind = problem.Keyword == "maxItems" ? "items" : "properties";
            int count = problem.Instance is JsonArray array ? array.Count : problem.Instance!.AsObject().Count;
            message = $"{count} {kind}, at most {PyFormat.Repr(problem.KeywordValue)} are allowed";
        }
        else
        {
            message = PyFormat.CodePointLength(problem.Message) <= 200 ? problem.Message : PyFormat.CodePointPrefix(problem.Message, 200) + "...";
        }
        string where = string.Join("/", problem.Path.Select(Segment));
        return where.Length > 0 ? $"{message} (at /{where})" : message;
    }

    private static string Segment(object segment) => segment is int index ? index.ToString(System.Globalization.CultureInfo.InvariantCulture) : (string)segment;

    private static List<string> StringPath(SchemaError error) => error.Path.Select(Segment).ToList();

    /// <summary>Python's min(): the first item whose key is smallest (a later equal one never replaces it).</summary>
    private static SchemaError First(IReadOnlyList<SchemaError> errors, Func<SchemaError, List<string>> key)
    {
        var best = errors[0];
        var bestKey = key(best);
        foreach (var error in errors.Skip(1))
        {
            var candidate = key(error);
            if (CompareStringLists(candidate, bestKey) < 0)
            {
                best = error;
                bestKey = candidate;
            }
        }
        return best;
    }

    /// <summary>Python's comparison of two lists of str: element by element in code-point order, a strict prefix first.</summary>
    private static int CompareStringLists(IReadOnlyList<string> a, IReadOnlyList<string> b)
    {
        for (int index = 0; index < Math.Min(a.Count, b.Count); index++)
        {
            int order = PyFormat.CompareCodePoints(a[index], b[index]);
            if (order != 0) return order;
        }
        return a.Count.CompareTo(b.Count);
    }

    /// <summary>Python's sorted(key=list(error.absolute_path)) ordering: element by element, ints numerically and str in
    /// code-point order (two paths differ first at a position under the same parent, so both elements there are of
    /// the same kind); a strict prefix first.</summary>
    private sealed class PathOrder : IComparer<IReadOnlyList<object>>
    {
        public static readonly PathOrder Python = new();

        public int Compare(IReadOnlyList<object>? a, IReadOnlyList<object>? b)
        {
            for (int index = 0; index < Math.Min(a!.Count, b!.Count); index++)
            {
                int order = (a[index], b[index]) switch
                {
                    (int x, int y) => x.CompareTo(y),
                    (string x, string y) => PyFormat.CompareCodePoints(x, y),
                    _ => throw new InvalidOperationException("Two schema error paths differ at a position holding an index in one and a key in the other"),
                };
                if (order != 0) return order;
            }
            return a.Count.CompareTo(b.Count);
        }
    }

    public static OrderedMap<InterfaceSpec> Interfaces() => InterfaceSpecs.Value;

    public static OrderedMap<ErrorSpec> ErrorCatalogue() => ErrorSpecs.Value;

    private static OrderedMap<InterfaceSpec> LoadInterfaces()
    {
        const string path = "contract/interfaces.yaml";
        var found = new OrderedMap<InterfaceSpec>();
        if (PyYaml.Load(ShippedFiles.ReadText(path), path) is not JsonObject document) throw new ContractError($"{path} is not a mapping");
        foreach (var (name, body) in document)
        {
            if (body is not JsonObject spec) throw new ContractError($"{path}: interface {name} is not a mapping");
            string where = $"{path}: interface {name}";
            var type = QuestionTypes.Parse(JsonRead.String(spec, "question_type", where), where);
            var values = spec.ContainsKey("values") ? JsonRead.Strings(spec, "values", where) : null;
            found.Set(name, new InterfaceSpec(name, type, values, JsonRead.String(spec, "description", where)));
        }
        return found;
    }

    private static OrderedMap<ErrorSpec> LoadErrors()
    {
        const string path = "contract/errors.yaml";
        var found = new OrderedMap<ErrorSpec>();
        if (PyYaml.Load(ShippedFiles.ReadText(path), path) is not JsonObject document) throw new ContractError($"{path} is not a mapping");
        foreach (var (name, body) in document)
        {
            if (body is not JsonObject spec) throw new ContractError($"{path}: {name} is not a mapping");
            if (!spec.TryGetPropertyValue("status", out var status)) throw new ContractError($"{path}: {name} has no status");
            long? code = null;
            if (status is not null)
            {
                if (!Json.TryLong(status, out long number) || number is < 400 or > 599)
                {
                    throw new ContractError($"{path}: {name} has status {PyFormat.Repr(status)}; use an HTTP error status or null");
                }
                code = number;
            }
            found.Set(name, new ErrorSpec(name, code, JsonRead.String(spec, "meaning", $"{path}: {name}")));
        }
        return found;
    }
}
