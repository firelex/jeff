using System.Text.Json.Nodes;

namespace Jeff.Sdk;

/// <summary>Every error Jeff raises (contract/errors.yaml): one class per entry, all JeffError subclasses, each named
/// as its entry. Details holds the error's wire details exactly as the Python SDK sends them (snake_case keys), so an
/// error crosses the engine socket, HTTP and the channel unchanged; the typed properties read them and throw a
/// ContractError naming the field when it is missing or of another kind.</summary>
public class JeffError : Exception
{
    /// <summary>An error with this message, details (copied) and cause.</summary>
    /// <param name="message">What went wrong, naming what is missing or wrong.</param>
    /// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
    /// <param name="innerException">The failure that caused this one, if any.</param>
    public JeffError(string message, JsonObject? details = null, Exception? innerException = null) : base(message, innerException)
    {
        Details = details is null ? new JsonObject() : (JsonObject)Json.Canonical(details)!;
    }

    /// <summary>The contract's name of this error (its class name): its wire form's "type".</summary>
    public string Type => GetType().Name;

    /// <summary>The error's wire details (snake_case keys, as the Python SDK sends them); empty when it has none.</summary>
    public JsonObject Details { get; }

    /// <summary>The x-request-id of the Jeff server answer that carried this error (set by the remote client), else null.</summary>
    public string? RequestId { get; set; }

    /// <summary>Further failures noticed while handling this one (Python's add_note); not part of the message.</summary>
    public List<string> Notes { get; } = new();

    private protected string DetailString(string key) => JsonRead.String(Details, key, $"{Type}.details");

    private protected long DetailLong(string key) => JsonRead.Long(Details, key, $"{Type}.details");

    private protected double DetailNumber(string key) => JsonRead.Number(Details, key, $"{Type}.details");

    private protected List<string> DetailStrings(string key) => JsonRead.Strings(Details, key, $"{Type}.details");
}

/// <summary>A document does not match its contract schema (a server or engine answer included).</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class ContractError(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>An adapter manifest is invalid or inconsistent with the base or the interface catalogue.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class ManifestError(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>A reference is not of the form jeff.&lt;domain&gt;.&lt;name&gt;[.&lt;function&gt;][@&lt;version&gt;] or &lt;alias&gt;[.&lt;function&gt;].</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class InvalidReference(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>No function or adapter in the catalogue has this ID, alias or version.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class UnknownFunction(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The function's adapter is not installed on the engine or not served by the server; the message names the command that installs it.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class NotInstalled(string message, JsonObject? details = null) : JeffError(message, details)
{
    /// <summary>The ID of the function the error is about (details function_id); ContractError when the details lack it.</summary>
    public string FunctionId => DetailString("function_id");
}
/// <summary>The call lacks context fields the function needs; the message names them.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class MissingContext(string message, JsonObject? details = null) : JeffError(message, details)
{
    /// <summary>The ID of the function the error is about (details function_id); ContractError when the details lack it.</summary>
    public string FunctionId => DetailString("function_id");
    /// <summary>The missing context fields (details fields); ContractError when the details lack them.</summary>
    public List<string> Fields => DetailStrings("fields");
}
/// <summary>The call passes context fields the function does not have.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class UnknownContext(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The function takes its labels from the caller, and the call passes none.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class MissingLabels(string message, JsonObject? details = null) : JeffError(message, details)
{
    /// <summary>The ID of the function the error is about (details function_id); ContractError when the details lack it.</summary>
    public string FunctionId => DetailString("function_id");
}
/// <summary>Labels given where the function's labels are fixed, or keys that collide or are malformed.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class InvalidLabels(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>More labels than the function was trained on.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class TooManyLabels(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>A channel connection already holds the most bound-function handles it may (1,024); release one first.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class TooManyHandles(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>A function was called through an interface it does not implement.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class InterfaceMismatch(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The decision abstained and the policy on low confidence is "error"; Decision is the decision that abstained.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed partial class LowConfidence(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>An escalation step failed; Step names it.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
/// <param name="innerException">The failure that caused this one, if any.</param>
public sealed class EscalationFailed(string message, JsonObject? details = null, Exception? innerException = null)
    : JeffError(message, details, innerException)
{
    /// <summary>The escalation step that failed (details step); ContractError when the details lack it.</summary>
    public string Step => DetailString("step");
}
/// <summary>The escalation ran past its budget.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class EscalationBudgetExceeded(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>A policy on low confidence is not "return", "error" or Policy.Escalate(...), or has a step the place it runs cannot run.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class InvalidPolicy(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>A decision graph is invalid or cannot run.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
/// <param name="innerException">The failure that caused this one, if any.</param>
public sealed class GraphError(string message, JsonObject? details = null, Exception? innerException = null)
    : JeffError(message, details, innerException);
/// <summary>Local mode needs the jeff-runtime package (raised by the Python SDK; it can reach this SDK over the wire).</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class RuntimeMissing(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The adapter is proprietary: its weights download only through JeffHub with a licence.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class ProprietaryWeights(string message, JsonObject? details = null) : JeffError(message, details)
{
    /// <summary>The proprietary adapter (details adapter_id); ContractError when the details lack it.</summary>
    public string AdapterId => DetailString("adapter_id");
}
/// <summary>Calibration cannot reach the target or has no data, or a function has no calibration for the base the engine serves.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class CalibrationError(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The engine returned output that breaks its contract (wrong number of probabilities, invalid values), or the engine socket failed.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class EngineError(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The request is longer than the engine's token limit; it was refused, never truncated.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class InputTooLong(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The engine backend or the Jeff server cannot be reached or used: no server at the URL, a dropped or closed channel, or an engine backend that cannot run on its machine.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class BackendUnavailable(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The engine queue is full; retry after RetryAfterS seconds (the engine's estimate of when there will be room).</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class Busy(string message, JsonObject? details = null) : JeffError(message, details)
{
    /// <summary>Seconds to wait before retrying (details retry_after_s); ContractError when the details lack it.</summary>
    public double RetryAfterS => DetailNumber("retry_after_s");
}
/// <summary>The request's deadline or timeout passed before it was answered.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class DeadlineExceeded(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The two ends of the engine socket or the channel speak different protocol versions.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class ProtocolMismatch(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The engine is still starting.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class NotReady(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>Missing or invalid credentials.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class Unauthorised(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The caller's token does not include this proprietary function's licence.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class NotLicensed(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The caller exceeded its rate limit; retry after RetryAfterS seconds.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class RateLimited(string message, JsonObject? details = null) : JeffError(message, details)
{
    /// <summary>Seconds to wait before retrying (details retry_after_s); ContractError when the details lack it.</summary>
    public double RetryAfterS => DetailNumber("retry_after_s");
}
/// <summary>The account has used its input-token allowance for the current billing period (HTTP 402); calls resume
/// when the next period starts, at PeriodEnd.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class UsageLimitReached(string message, JsonObject? details = null) : JeffError(message, details)
{
    /// <summary>The account's id on the hub (details account); ContractError when the details lack it.</summary>
    public string Account => DetailString("account");
    /// <summary>The input tokens the account has used this billing period (details used); ContractError when the details lack it.</summary>
    public long Used => DetailLong("used");
    /// <summary>The input tokens the account may use in a billing period (details allowance); ContractError when the details lack it.</summary>
    public long Allowance => DetailLong("allowance");
    /// <summary>When the current billing period ends and calls resume, ISO 8601 in UTC (details period_end); ContractError when the details lack it.</summary>
    public string PeriodEnd => DetailString("period_end");
}
/// <summary>The request does not match the API schema, or this SDK refuses it before sending (a closed client, a timeout out of range, too many calls in one request).</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class InvalidRequest(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The request does not fit the current state of the factory run (for example, approving a spec twice).</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class Conflict(string message, JsonObject? details = null) : JeffError(message, details);
/// <summary>The factory is off on this server, or the request needs a part of it this server does not run.</summary>
/// <param name="message">What went wrong, naming what is missing or wrong.</param>
/// <param name="details">The error's wire details (snake_case keys, as the Python SDK sends them); empty when not given.</param>
public sealed class FactoryOff(string message, JsonObject? details = null) : JeffError(message, details);

/// <summary>The error catalogue in code: the class for each contract name, the constructors that write the Python
/// SDK's messages, and the wire form.</summary>
internal static partial class Errors
{
    public static readonly IReadOnlyDictionary<string, Func<string, JsonObject?, JeffError>> Classes =
        new Dictionary<string, Func<string, JsonObject?, JeffError>>(StringComparer.Ordinal)
        {
            ["JeffError"] = (m, d) => new JeffError(m, d),
            ["ContractError"] = (m, d) => new ContractError(m, d),
            ["ManifestError"] = (m, d) => new ManifestError(m, d),
            ["InvalidReference"] = (m, d) => new InvalidReference(m, d),
            ["UnknownFunction"] = (m, d) => new UnknownFunction(m, d),
            ["NotInstalled"] = (m, d) => new NotInstalled(m, d),
            ["MissingContext"] = (m, d) => new MissingContext(m, d),
            ["UnknownContext"] = (m, d) => new UnknownContext(m, d),
            ["MissingLabels"] = (m, d) => new MissingLabels(m, d),
            ["InvalidLabels"] = (m, d) => new InvalidLabels(m, d),
            ["TooManyLabels"] = (m, d) => new TooManyLabels(m, d),
            ["TooManyHandles"] = (m, d) => new TooManyHandles(m, d),
            ["InterfaceMismatch"] = (m, d) => new InterfaceMismatch(m, d),
            ["LowConfidence"] = (m, d) => new LowConfidence(m, d),
            ["EscalationFailed"] = (m, d) => new EscalationFailed(m, d),
            ["EscalationBudgetExceeded"] = (m, d) => new EscalationBudgetExceeded(m, d),
            ["InvalidPolicy"] = (m, d) => new InvalidPolicy(m, d),
            ["GraphError"] = (m, d) => new GraphError(m, d),
            ["RuntimeMissing"] = (m, d) => new RuntimeMissing(m, d),
            ["ProprietaryWeights"] = (m, d) => new ProprietaryWeights(m, d),
            ["CalibrationError"] = (m, d) => new CalibrationError(m, d),
            ["EngineError"] = (m, d) => new EngineError(m, d),
            ["InputTooLong"] = (m, d) => new InputTooLong(m, d),
            ["BackendUnavailable"] = (m, d) => new BackendUnavailable(m, d),
            ["Busy"] = (m, d) => new Busy(m, d),
            ["DeadlineExceeded"] = (m, d) => new DeadlineExceeded(m, d),
            ["ProtocolMismatch"] = (m, d) => new ProtocolMismatch(m, d),
            ["NotReady"] = (m, d) => new NotReady(m, d),
            ["Unauthorised"] = (m, d) => new Unauthorised(m, d),
            ["NotLicensed"] = (m, d) => new NotLicensed(m, d),
            ["RateLimited"] = (m, d) => new RateLimited(m, d),
            ["UsageLimitReached"] = (m, d) => new UsageLimitReached(m, d),
            ["InvalidRequest"] = (m, d) => new InvalidRequest(m, d),
            ["Conflict"] = (m, d) => new Conflict(m, d),
            ["FactoryOff"] = (m, d) => new FactoryOff(m, d),
        };

    public static NotInstalled NotInstalledError(string functionId, string why = "")
    {
        int dots = functionId.Count(character => character == '.');
        string adapter = dots >= 3 ? functionId[..functionId.LastIndexOf('.')] : functionId;
        return new NotInstalled($"{functionId} is not installed{(why.Length > 0 ? ": " + why : "")}. Install it with: jeff pull {adapter}",
            new JsonObject { ["function_id"] = functionId });
    }

    public static MissingContext MissingContextError(string functionId, IReadOnlyList<string> fields) =>
        new($"{functionId} needs context field(s) {string.Join(", ", fields)}; pass them in context=",
            new JsonObject { ["function_id"] = functionId, ["fields"] = Json.Strings(fields) });

    public static MissingLabels MissingLabelsError(string functionId) =>
        new($"{functionId} takes its labels from the caller; pass labels=", new JsonObject { ["function_id"] = functionId });

    public static EscalationFailed EscalationFailedError(string step, string reason, Exception? cause = null) =>
        new($"Escalation step {step} failed: {reason}", new JsonObject { ["step"] = step }, cause);

    /// <summary>The error a wire body names, of its own class with its message and details; an unknown type is an
    /// EngineError naming it (jeff.wire.error_from_wire).</summary>
    public static JeffError FromWire(string type, string message, JsonNode? details)
    {
        if (!Classes.TryGetValue(type, out var make)) return new EngineError($"unknown error type {PyFormat.Str(type)} from the engine: {message}");
        if (details is not null and not JsonObject)
        {
            throw new ContractError($"The details of a {type} error must be an object, got {PyFormat.TypeName(details)}");
        }
        return make(message, (JsonObject?)details);
    }

    public static JsonObject ToWire(JeffError error) => new()
    {
        ["type"] = error.Type,
        ["message"] = error.Message,
        ["details"] = Json.Canonical(error.Details),
    };

    /// <summary>The same error (class, message, details, notes, request id) as a new instance, so each waiting caller
    /// gets its own.</summary>
    public static JeffError Copy(JeffError error)
    {
        var copy = FromWire(error.Type, error.Message, error.Details);
        copy.Notes.AddRange(error.Notes);
        copy.RequestId = error.RequestId;
        return copy;
    }

    /// <summary>"Type: message" of anything thrown: a JeffError's contract name, else the .NET exception's class name.</summary>
    public static string Described(Exception error) => $"{(error is JeffError jeff ? jeff.Type : error.GetType().Name)}: {error.Message}";
}
