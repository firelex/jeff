using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>The base's weight format, read once when an engine connects: {"format": "bf16"}, or an INT4 format with
/// the format_id naming its exact weights.</summary>
public sealed record BaseFormat(string Format, string? FormatId = null)
{
    /// <summary>The format as the engine and server write it: {"format": ..., "format_id"?: ...}.</summary>
    public JsonObject ToJson()
    {
        var json = new JsonObject { ["format"] = Format };
        if (FormatId is not null) json["format_id"] = FormatId;
        return json;
    }

    /// <summary>Reads a base format block; ContractError naming `where` when "format" is missing or a field is not a string.</summary>
    public static BaseFormat FromJson(JsonObject data, string where) =>
        new(JsonRead.String(data, "format", where), data.ContainsKey("format_id") ? JsonRead.String(data, "format_id", where) : null);
}

/// <summary>One predict's answer: one list of probabilities per row, over the row's options in order; the engine's
/// timing; each row's prompt length in tokens when the engine reports it.</summary>
public sealed record EngineResult(IReadOnlyList<IReadOnlyList<double>> Probabilities, JsonObject Timing, IReadOnlyList<long>? InputTokens);

/// <summary>What the decision logic needs from an engine: the engine socket (SocketEngine), a Jeff server's listing
/// (ServedFunctions) or a test engine (FakeEngine). Implementations report failures through the returned task.</summary>
public interface IEngine : IAsyncDisposable
{
    /// <summary>The base's weight format, which picks each function's calibration.</summary>
    BaseFormat BaseFormat { get; }

    /// <summary>The adapter IDs loaded; a cached copy, asked of the engine again with refresh.</summary>
    Task<IReadOnlySet<string>> InstalledAsync(bool refresh = false, CancellationToken cancellationToken = default);

    /// <summary>Runs the adapter on the rows: one list of probabilities per row, in row order. A failure is reported through
    /// the returned task (NotInstalled for an adapter the engine has not loaded, EngineError for a broken reply).</summary>
    Task<EngineResult> PredictAsync(string adapterId, IReadOnlyList<Row> rows, CancellationToken cancellationToken = default);
}

internal static class Engines
{
    public const string Bf16 = "bf16";

    /// <summary>Null for a bfloat16 base (functions decide with their bfloat16 calibration); for any other format its
    /// format_id (port of jeff.engine.format_id_of).</summary>
    public static string? FormatIdOf(BaseFormat baseFormat)
    {
        if (baseFormat.Format == Bf16)
        {
            if (baseFormat.FormatId is not null)
            {
                throw new EngineError($"The engine's base format {PyFormat.Repr(baseFormat.ToJson())} is bfloat16 but carries a format_id");
            }
            return null;
        }
        return baseFormat.FormatId ?? throw new EngineError($"The engine's base format {PyFormat.Repr(baseFormat.ToJson())} names no format_id; "
            + $"every base format other than {Bf16} is one exact set of weights, named by its format_id");
    }

    /// <summary>Whether the engine serves the adapter: a cached list missing it is asked once more, with refresh.</summary>
    public static async Task<bool> ServesAsync(IEngine engine, string adapterId, CancellationToken cancellationToken = default) =>
        (await engine.InstalledAsync(false, cancellationToken).ConfigureAwait(false)).Contains(adapterId)
        || (await engine.InstalledAsync(true, cancellationToken).ConfigureAwait(false)).Contains(adapterId);
}
