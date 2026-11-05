using System.Globalization;
using System.Text;
using System.Text.Json.Nodes;

namespace Jeff.Sdk.Testing;

/// <summary>An engine whose answers come from a function you supply (port of jeff.testing.FakeEngine): answers gives the
/// probabilities for each row, installed the adapter IDs it serves, baseFormat the base it reports (bfloat16 when not
/// given; pass new BaseFormat("int4-g64", "&lt;format id&gt;") to act as an INT4 base).</summary>
public sealed class FakeEngine : IEngine
{
    private readonly Func<string, Row, IReadOnlyList<double>> answers;
    private readonly HashSet<string> served;
    private readonly List<(string AdapterId, IReadOnlyList<Row> Rows)> calls = new();

    /// <summary>A fake engine answering with `answers` (given the adapter ID and each row) for the adapters in `installed`.
    /// EngineError for a malformed base format (a bfloat16 one with a format_id, or another one without).</summary>
    public FakeEngine(Func<string, Row, IReadOnlyList<double>> answers, IEnumerable<string> installed, BaseFormat? baseFormat = null)
    {
        this.answers = answers;
        served = new HashSet<string>(installed, StringComparer.Ordinal);
        BaseFormat = baseFormat ?? new BaseFormat(Engines.Bf16);
        Engines.FormatIdOf(BaseFormat); // refuses a malformed format here, as a real engine's base folder would be
    }

    /// <summary>The base format the engine reports.</summary>
    public BaseFormat BaseFormat { get; }

    /// <summary>Every predict the engine was asked, in order: the adapter and its rows (a copy of the list).</summary>
    public IReadOnlyList<(string AdapterId, IReadOnlyList<Row> Rows)> Calls
    {
        get
        {
            lock (calls) return calls.ToList();
        }
    }

    /// <summary>The adapters given as installed (a new set each call).</summary>
    public Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default) =>
        Task.FromResult<IReadOnlySet<string>>(new HashSet<string>(served, StringComparer.Ordinal));

    /// <summary>The probabilities `answers` gives for each row, with a fake timing and token counts; the task fails with
    /// NotInstalled for an adapter not given as installed.</summary>
    public async Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default)
    {
        await Task.CompletedTask.ConfigureAwait(false); // a failure below fails the returned task, never the call itself
        if (!served.Contains(adapterId)) throw Errors.NotInstalledError(adapterId, "FakeEngine has not loaded it");
        lock (calls) calls.Add((adapterId, rows));
        var timing = new JsonObject { ["path"] = "fake", ["model_ms"] = 0.0 };
        foreach (var (key, value) in BaseFormat.ToJson()) timing[key] = Json.Canonical(value);
        return new EngineResult(rows.Select(row => answers(adapterId, row)).ToList(), timing, rows.Select(Golden.FakeTokens).ToList());
    }

    /// <summary>Nothing to release.</summary>
    public ValueTask DisposeAsync() => ValueTask.CompletedTask;
}

/// <summary>A golden case (contract/golden/*.json).</summary>
public sealed record GoldenCase(string Name, Call Call, JsonObject Expected, JsonNode? Tolerance)
{
    /// <summary>Reads a case of contract/golden/*.json; ContractError naming the field when name, call or expected is missing.</summary>
    public static GoldenCase FromJson(JsonObject data)
    {
        var call = JsonRead.Object(data, "call", "a golden case");
        return new GoldenCase(JsonRead.String(data, "name", "a golden case"),
            new Call(JsonRead.String(call, "function", "a golden call"), call["input"], call["context"] as JsonObject, call["labels"]),
            (JsonObject)Json.Canonical(JsonRead.Object(data, "expected", "a golden case"))!, Json.Canonical(data["tolerance"]));
    }
}

/// <summary>The golden comparison (port of jeff.evaluate.same and golden_problems) and FakeEngine's token count.</summary>
public static class Golden
{
    /// <summary>FakeEngine's stand-in for a row's prompt length, as Python's fake_tokens: one token per whitespace-
    /// separated word of the row's JSON as json.dumps writes it.</summary>
    internal static long FakeTokens(Row row) => PythonDumps(row.ToJson()).Split(' ', StringSplitOptions.RemoveEmptyEntries).Length;

    /// <summary>json.dumps(value) with its defaults: ", " and ": " separators; every character outside space to "~" (control
    /// characters, DEL and everything beyond ASCII) escaped, a character beyond U+FFFF as its surrogate pair.</summary>
    internal static string PythonDumps(JsonNode? value)
    {
        var builder = new StringBuilder();
        Dump(builder, value);
        return builder.ToString();
    }

    private static void Dump(StringBuilder builder, JsonNode? value)
    {
        switch (Json.KindOf(value))
        {
            case JsonKind.Null: builder.Append("null"); break;
            case JsonKind.Bool: builder.Append(value!.GetValue<bool>() ? "true" : "false"); break;
            case JsonKind.Int: builder.Append(Json.WholeOf(value).ToString(CultureInfo.InvariantCulture)); break;
            case JsonKind.Float:
                double number = value!.GetValue<double>();
                builder.Append(double.IsNaN(number) ? "NaN" : double.IsPositiveInfinity(number) ? "Infinity"
                    : double.IsNegativeInfinity(number) ? "-Infinity" : PyFormat.Float(number));
                break;
            case JsonKind.String: DumpString(builder, value!.GetValue<string>()); break;
            case JsonKind.Array:
                builder.Append('[');
                bool firstItem = true;
                foreach (var item in value!.AsArray())
                {
                    if (!firstItem) builder.Append(", ");
                    firstItem = false;
                    Dump(builder, item);
                }
                builder.Append(']');
                break;
            default:
                builder.Append('{');
                bool firstKey = true;
                foreach (var (key, item) in value!.AsObject())
                {
                    if (!firstKey) builder.Append(", ");
                    firstKey = false;
                    DumpString(builder, key);
                    builder.Append(": ");
                    Dump(builder, item);
                }
                builder.Append('}');
                break;
        }
    }

    private static void DumpString(StringBuilder builder, string text)
    {
        builder.Append('"');
        foreach (char character in text)
        {
            builder.Append(character switch
            {
                '"' => "\\\"",
                '\\' => "\\\\",
                '\n' => "\\n",
                '\r' => "\\r",
                '\t' => "\\t",
                '\b' => "\\b",
                '\f' => "\\f",
                _ when character < 0x20 || character > 0x7e => "\\u" + ((int)character).ToString("x4", CultureInfo.InvariantCulture),
                _ => character.ToString(),
            });
        }
        builder.Append('"');
    }

    /// <summary>Equal and of the same JSON kind, so that true does not match 1 and 1 does not match 1.0.</summary>
    internal static bool Same(JsonNode? value, JsonNode? expected) => Json.StrictEquals(value, expected);

    /// <summary>How a decision differs from a golden case; empty when it reproduces it. The value must be the same, the
    /// confidence within the case's tolerance, and abstained must match except when the recorded confidence is within
    /// the tolerance of the threshold.</summary>
    public static IReadOnlyList<string> Problems(GoldenCase item, Decision decision, double? threshold)
    {
        var expected = item.Expected;
        double tolerance = JsonRead.NumberOf(item.Tolerance) ?? throw new ContractError($"golden case {item.Name} has no numeric tolerance");
        double expectedConfidence = JsonRead.Number(expected, "confidence", $"golden case {item.Name}'s expected");
        bool expectedAbstained = JsonRead.Bool(expected, "abstained", $"golden case {item.Name}'s expected");
        var problems = new List<string>();
        if (!Same(decision.Value, expected["value"])) problems.Add($"value {PyFormat.Repr(decision.Value)}, expected {PyFormat.Repr(expected["value"])}");
        if (decision.Confidence is null) problems.Add("no confidence");
        else if (Math.Abs(decision.Confidence.Value - expectedConfidence) > tolerance)
        {
            problems.Add($"confidence {PyFormat.Float(decision.Confidence.Value)}, expected {PyFormat.Repr(expected["confidence"])} +/- {PyFormat.Repr(item.Tolerance)}");
        }
        bool nearThreshold = threshold is not null && Math.Abs(expectedConfidence - threshold.Value) <= tolerance;
        if (decision.Abstained != expectedAbstained && !nearThreshold)
        {
            problems.Add($"abstained {(decision.Abstained ? "True" : "False")}, expected {(expectedAbstained ? "True" : "False")} (the recorded confidence "
                + $"is more than {PyFormat.Repr(item.Tolerance)} from the threshold {PyFormat.ReprOrNone(threshold)})");
        }
        return problems;
    }
}
