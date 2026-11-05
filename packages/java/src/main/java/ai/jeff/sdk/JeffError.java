package ai.jeff.sdk;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CopyOnWriteArrayList;
import java.util.function.BiFunction;

/** Every error Jeff raises (contract/errors.yaml): one nested class per entry, all subclasses of this one, and all
 * unchecked. The message is the one the Python SDK gives for the same failure. {@link #details()} holds the error's
 * wire details exactly as the Python SDK sends them (snake_case keys), so an error crosses the engine socket, HTTP and
 * the channel unchanged; the typed getters read them and throw {@link ContractError} naming a field that is missing.
 * No message, note or {@code toString()} ever shows a token. */
public class JeffError extends RuntimeException {
    /** The error's wire details, read-only. */
    private final Map<String, Object> details;
    /** The request id of the server answer that carried the error, or null. */
    private volatile String requestId;
    /** Further failures noticed while handling this one. */
    private final List<String> notes = new CopyOnWriteArrayList<>();

    /** An error with no details.
     *
     * @param message what went wrong */
    public JeffError(String message) {
        this(message, Map.of());
    }

    /** An error with its wire details.
     *
     * @param message what went wrong
     * @param details the details, with snake_case keys as the Python SDK sends them (copied, read-only) */
    public JeffError(String message, Map<String, ?> details) {
        super(message);
        this.details = Collections.unmodifiableSequencedMap(new LinkedHashMap<String, Object>(details));
    }

    /** The error's name in contract/errors.yaml.
     *
     * @return the class's simple name, such as {@code NotInstalled} */
    public final String type() {
        return getClass().getSimpleName();
    }

    /** The error's wire details.
     *
     * @return a read-only map, empty when there are none */
    public final Map<String, Object> details() {
        return details;
    }

    /** The request id of the Jeff server answer that carried this error.
     *
     * @return its {@code x-request-id}, set by the remote client, or null */
    public final String requestId() {
        return requestId;
    }

    /** Records the request id of the server answer that carried this error.
     *
     * @param requestId the {@code x-request-id}, or null */
    public final void setRequestId(String requestId) {
        this.requestId = requestId;
    }

    /** Further failures noticed while handling this one (Python's {@code __notes__}); not part of the message.
     *
     * @return the notes, read-only, in the order added */
    public final List<String> notes() {
        return Collections.unmodifiableList(notes);
    }

    /** Adds a note (Python's {@code add_note}).
     *
     * @param note the note */
    public final void addNote(String note) {
        notes.add(note);
    }

    /** "Type: message", as Python writes an exception (type(error).__name__ and str(error)). */
    final String described() {
        return type() + ": " + getMessage();
    }

    /** "Type: message" of any exception, as Python writes one (an exception without a message gives "Type: "). */
    static String describe(Throwable error) {
        if (error instanceof JeffError jeff) return jeff.described();
        return error.getClass().getSimpleName() + ": " + (error.getMessage() == null ? "" : error.getMessage());
    }

    private static Object detail(JeffError error, String key) {
        return error.details().get(key);
    }

    static String requiredString(JeffError error, String key) {
        if (detail(error, key) instanceof String text) return text;
        throw new ContractError(error.type() + ".details is missing its '" + key + "' string (got "
            + PyFormat.repr(detail(error, key)) + ")");
    }

    static double requiredNumber(JeffError error, String key) {
        Object value = detail(error, key);
        // A Float is a Number, but PyFormat.repr has no case for one; coerce it to Double before the check so a
        // Float detail is accepted like any other number instead of crashing the rejection path below.
        if (value instanceof Float number) value = number.doubleValue();
        if (value instanceof Number number) return number.doubleValue();
        throw new ContractError(error.type() + ".details is missing its '" + key + "' number (got " + PyFormat.repr(value) + ")");
    }

    /** A whole-number detail that fits a long (a Long, Integer, Short or Byte); anything else, a whole number past 64
     * bits included, is a ContractError naming it. */
    static long requiredLong(JeffError error, String key) {
        Object value = detail(error, key);
        if (value instanceof Long || value instanceof Integer || value instanceof Short || value instanceof Byte) {
            return ((Number) value).longValue();
        }
        throw new ContractError(error.type() + ".details is missing its '" + key + "' whole number (got "
            + PyFormat.repr(value) + ")");
    }

    static List<String> requiredStrings(JeffError error, String key) {
        if (detail(error, key) instanceof List<?> list && list.stream().allMatch(item -> item instanceof String)) {
            return list.stream().map(item -> (String) item).toList();
        }
        throw new ContractError(error.type() + ".details is missing its '" + key + "' string array (got "
            + PyFormat.repr(detail(error, key)) + ")");
    }

    /** A document does not match its contract schema. A Jeff server answers it with HTTP status 500. */
    public static class ContractError extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public ContractError(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public ContractError(String message, Map<String, ?> details) { super(message, details); }
    }

    /** An adapter manifest or the routing tree is invalid. A Jeff server answers it with HTTP status 500. */
    public static class ManifestError extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public ManifestError(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public ManifestError(String message, Map<String, ?> details) { super(message, details); }
    }

    /** A function reference is malformed. A Jeff server answers it with HTTP status 422. */
    public static class InvalidReference extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public InvalidReference(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public InvalidReference(String message, Map<String, ?> details) { super(message, details); }
    }

    /** No function or adapter in the catalogue has this ID, alias or version. A Jeff server answers it with HTTP
     * status 404. */
    public static class UnknownFunction extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public UnknownFunction(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public UnknownFunction(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The function exists but its adapter is not loaded. A Jeff server answers it with HTTP status 424. */
    public static class NotInstalled extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public NotInstalled(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public NotInstalled(String message, Map<String, ?> details) { super(message, details); }
        /** The function that is not installed.
         *
         * @return its id, from the {@code function_id} detail
         * @throws ContractError when the detail is missing */
        public String functionId() { return requiredString(this, "function_id"); }
    }

    /** Required context fields are missing. A Jeff server answers it with HTTP status 422. */
    public static class MissingContext extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public MissingContext(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public MissingContext(String message, Map<String, ?> details) { super(message, details); }
        /** The function that needs the fields.
         *
         * @return its id, from the {@code function_id} detail
         * @throws ContractError when the detail is missing */
        public String functionId() { return requiredString(this, "function_id"); }
        /** The context fields that are missing.
         *
         * @return their keys, from the {@code fields} detail
         * @throws ContractError when the detail is missing */
        public List<String> fields() { return requiredStrings(this, "fields"); }
    }

    /** Context fields the function does not have. A Jeff server answers it with HTTP status 422. */
    public static class UnknownContext extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public UnknownContext(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public UnknownContext(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The function takes its labels from the caller and none were given. A Jeff server answers it with HTTP status
     * 422. */
    public static class MissingLabels extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public MissingLabels(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public MissingLabels(String message, Map<String, ?> details) { super(message, details); }
        /** The function that takes its labels from the caller.
         *
         * @return its id, from the {@code function_id} detail
         * @throws ContractError when the detail is missing */
        public String functionId() { return requiredString(this, "function_id"); }
    }

    /** Labels are given where they are fixed, or keys collide or are malformed. A Jeff server answers it with HTTP
     * status 422. */
    public static class InvalidLabels extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public InvalidLabels(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public InvalidLabels(String message, Map<String, ?> details) { super(message, details); }
    }

    /** More labels than the function was trained on. A Jeff server answers it with HTTP status 422. */
    public static class TooManyLabels extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public TooManyLabels(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public TooManyLabels(String message, Map<String, ?> details) { super(message, details); }
    }

    /** A channel connection already holds the most bound-function handles it may (1024); release one first. A Jeff
     * server answers it with HTTP status 422. */
    public static class TooManyHandles extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public TooManyHandles(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public TooManyHandles(String message, Map<String, ?> details) { super(message, details); }
    }

    /** A function was called through an interface it does not implement. A Jeff server answers it with HTTP status
     * 422. */
    public static class InterfaceMismatch extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public InterfaceMismatch(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public InterfaceMismatch(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The decision abstained and {@code on_low_confidence} was 'error'; details carry the decision. A Jeff server
     * answers it with HTTP status 409. */
    public static class LowConfidence extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public LowConfidence(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public LowConfidence(String message, Map<String, ?> details) { super(message, details); }

        /** The decision that abstained, as the error's wire details carry it.
         *
         * @return the decision
         * @throws ContractError when the details carry no decision */
        public Decision decision() {
            if (!(details().get("decision") instanceof Map<?, ?> data)) {
                throw new ContractError(type() + ".details is missing its 'decision' object (got "
                    + PyFormat.repr(details().get("decision")) + ")");
            }
            return Decision.fromJson(Values.map(data));
        }
    }

    /** An escalation step failed. A Jeff server answers it with HTTP status 502. */
    public static class EscalationFailed extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public EscalationFailed(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public EscalationFailed(String message, Map<String, ?> details) { super(message, details); }
        /** The step that failed.
         *
         * @return its name, from the {@code step} detail
         * @throws ContractError when the detail is missing */
        public String step() { return requiredString(this, "step"); }
    }

    /** An escalation ran past its {@code budget_ms}. A Jeff server answers it with HTTP status 504. */
    public static class EscalationBudgetExceeded extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public EscalationBudgetExceeded(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public EscalationBudgetExceeded(String message, Map<String, ?> details) { super(message, details); }
    }

    /** {@code on_low_confidence} is not valid for this call. A Jeff server answers it with HTTP status 422. */
    public static class InvalidPolicy extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public InvalidPolicy(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public InvalidPolicy(String message, Map<String, ?> details) { super(message, details); }
    }

    /** A decision graph is invalid or cannot run. A Jeff server answers it with HTTP status 422. */
    public static class GraphError extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public GraphError(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public GraphError(String message, Map<String, ?> details) { super(message, details); }
    }

    /** Local mode needs the {@code jeff-runtime} package. It is never sent over HTTP. */
    public static class RuntimeMissing extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public RuntimeMissing(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public RuntimeMissing(String message, Map<String, ?> details) { super(message, details); }
    }

    /** A proprietary adapter's weights need a licence or a local folder. It is never sent over HTTP. */
    public static class ProprietaryWeights extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public ProprietaryWeights(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public ProprietaryWeights(String message, Map<String, ?> details) { super(message, details); }
        /** The proprietary adapter.
         *
         * @return its id, from the {@code adapter_id} detail
         * @throws ContractError when the detail is missing */
        public String adapterId() { return requiredString(this, "adapter_id"); }
    }

    /** Calibration cannot reach the target or has no data. It is never sent over HTTP. */
    public static class CalibrationError extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public CalibrationError(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public CalibrationError(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The engine failed or broke its contract. A Jeff server answers it with HTTP status 500. */
    public static class EngineError extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public EngineError(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public EngineError(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The request is longer than the token limit; it was refused, never truncated. A Jeff server answers it with
     * HTTP status 422. */
    public static class InputTooLong extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public InputTooLong(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public InputTooLong(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The engine backend cannot run on this machine. A Jeff server answers it with HTTP status 503. */
    public static class BackendUnavailable extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public BackendUnavailable(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public BackendUnavailable(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The engine queue is full; retry after the {@code Retry-After} seconds. A Jeff server answers it with HTTP
     * status 529. */
    public static class Busy extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public Busy(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public Busy(String message, Map<String, ?> details) { super(message, details); }
        /** How long to wait before retrying.
         *
         * @return seconds, from the {@code retry_after_s} detail
         * @throws ContractError when the detail is missing */
        public double retryAfterS() { return requiredNumber(this, "retry_after_s"); }
    }

    /** The request's deadline passed before it ran. A Jeff server answers it with HTTP status 504. */
    public static class DeadlineExceeded extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public DeadlineExceeded(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public DeadlineExceeded(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The two ends of the engine socket speak different protocol versions. It is never sent over HTTP. */
    public static class ProtocolMismatch extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public ProtocolMismatch(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public ProtocolMismatch(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The engine is still starting. A Jeff server answers it with HTTP status 503. */
    public static class NotReady extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public NotReady(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public NotReady(String message, Map<String, ?> details) { super(message, details); }
    }

    /** Missing or invalid credentials. A Jeff server answers it with HTTP status 401. */
    public static class Unauthorised extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public Unauthorised(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public Unauthorised(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The caller's token does not include this proprietary function's licence. A Jeff server answers it with HTTP
     * status 403. */
    public static class NotLicensed extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public NotLicensed(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public NotLicensed(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The caller exceeded its rate limit; retry after the {@code Retry-After} seconds. A Jeff server answers it
     * with HTTP status 429. */
    public static class RateLimited extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public RateLimited(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public RateLimited(String message, Map<String, ?> details) { super(message, details); }
        /** How long to wait before retrying.
         *
         * @return seconds, from the {@code retry_after_s} detail
         * @throws ContractError when the detail is missing */
        public double retryAfterS() { return requiredNumber(this, "retry_after_s"); }
    }

    /** The account has used its input-token allowance for the billing period; calls resume when the next period
     * starts, at {@link #periodEnd()}. A Jeff server answers it with HTTP status 402. */
    public static class UsageLimitReached extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public UsageLimitReached(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public UsageLimitReached(String message, Map<String, ?> details) { super(message, details); }
        /** The account whose allowance ran out.
         *
         * @return the account's id on the hub, from the {@code account} detail
         * @throws ContractError when the detail is missing */
        public String account() { return requiredString(this, "account"); }
        /** The input tokens the account has used in the current billing period.
         *
         * @return input tokens, from the {@code used} detail
         * @throws ContractError when the detail is missing or not a whole number */
        public long used() { return requiredLong(this, "used"); }
        /** The input tokens the account may use in a billing period.
         *
         * @return input tokens, from the {@code allowance} detail
         * @throws ContractError when the detail is missing or not a whole number */
        public long allowance() { return requiredLong(this, "allowance"); }
        /** When the current billing period ends and calls resume.
         *
         * @return an ISO 8601 time in UTC, from the {@code period_end} detail
         * @throws ContractError when the detail is missing */
        public String periodEnd() { return requiredString(this, "period_end"); }
    }

    /** The request does not match the API schema. A Jeff server answers it with HTTP status 422. */
    public static class InvalidRequest extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public InvalidRequest(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public InvalidRequest(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The request does not fit the current state of the factory run. A Jeff server answers it with HTTP status 409. */
    public static class Conflict extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public Conflict(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public Conflict(String message, Map<String, ?> details) { super(message, details); }
    }

    /** The factory is off on this server. A Jeff server answers it with HTTP status 403. */
    public static class FactoryOff extends JeffError {
        /** An error with no details.
         *
         * @param message what went wrong */
        public FactoryOff(String message) { super(message); }
        /** An error with its wire details.
         *
         * @param message what went wrong
         * @param details the details, with snake_case keys as the Python SDK sends them */
        public FactoryOff(String message, Map<String, ?> details) { super(message, details); }
    }

    private static final Map<String, BiFunction<String, Map<String, ?>, JeffError>> TYPES = types();

    private static Map<String, BiFunction<String, Map<String, ?>, JeffError>> types() {
        Map<String, BiFunction<String, Map<String, ?>, JeffError>> found = new LinkedHashMap<>();
        found.put("JeffError", JeffError::new);
        found.put("ContractError", ContractError::new);
        found.put("ManifestError", ManifestError::new);
        found.put("InvalidReference", InvalidReference::new);
        found.put("UnknownFunction", UnknownFunction::new);
        found.put("NotInstalled", NotInstalled::new);
        found.put("MissingContext", MissingContext::new);
        found.put("UnknownContext", UnknownContext::new);
        found.put("MissingLabels", MissingLabels::new);
        found.put("InvalidLabels", InvalidLabels::new);
        found.put("TooManyLabels", TooManyLabels::new);
        found.put("TooManyHandles", TooManyHandles::new);
        found.put("InterfaceMismatch", InterfaceMismatch::new);
        found.put("LowConfidence", LowConfidence::new);
        found.put("EscalationFailed", EscalationFailed::new);
        found.put("EscalationBudgetExceeded", EscalationBudgetExceeded::new);
        found.put("InvalidPolicy", InvalidPolicy::new);
        found.put("GraphError", GraphError::new);
        found.put("RuntimeMissing", RuntimeMissing::new);
        found.put("ProprietaryWeights", ProprietaryWeights::new);
        found.put("CalibrationError", CalibrationError::new);
        found.put("EngineError", EngineError::new);
        found.put("InputTooLong", InputTooLong::new);
        found.put("BackendUnavailable", BackendUnavailable::new);
        found.put("Busy", Busy::new);
        found.put("DeadlineExceeded", DeadlineExceeded::new);
        found.put("ProtocolMismatch", ProtocolMismatch::new);
        found.put("NotReady", NotReady::new);
        found.put("Unauthorised", Unauthorised::new);
        found.put("NotLicensed", NotLicensed::new);
        found.put("RateLimited", RateLimited::new);
        found.put("UsageLimitReached", UsageLimitReached::new);
        found.put("InvalidRequest", InvalidRequest::new);
        found.put("Conflict", Conflict::new);
        found.put("FactoryOff", FactoryOff::new);
        return Collections.unmodifiableSequencedMap(new LinkedHashMap<>(found));
    }

    /** JeffError and every error type, in contract/errors.yaml's order. */
    static Set<String> typeNames() {
        return TYPES.keySet();
    }

    /** Python's {@code NotInstalled(function_id)}: the message names the {@code jeff pull} command that installs it.
     *
     * @param functionId the function (or adapter) that is not installed
     * @return the error, to throw */
    public static NotInstalled notInstalled(String functionId) {
        return notInstalled(functionId, "");
    }

    /** Python's {@code NotInstalled(function_id, why)}: the message gives the reason and names the {@code jeff pull}
     * command that installs it.
     *
     * @param functionId the function (or adapter) that is not installed
     * @param why the reason, or {@code ""} for none
     * @return the error, to throw */
    public static NotInstalled notInstalled(String functionId, String why) {
        long dots = functionId.chars().filter(character -> character == '.').count();
        String adapter = dots >= 3 ? functionId.substring(0, functionId.lastIndexOf('.')) : functionId;
        return new NotInstalled(functionId + " is not installed" + (why.isEmpty() ? "" : ": " + why)
            + ". Install it with: jeff pull " + adapter, Map.of("function_id", functionId));
    }

    /** Python's {@code MissingContext(function_id, fields)}.
     *
     * @param functionId the function
     * @param fields the context fields it needs and was not given
     * @return the error, to throw */
    public static MissingContext missingContext(String functionId, List<String> fields) {
        // Map.of with two or more entries does not keep insertion order (ImmutableCollections.MapN permutes it by a
        // per-JVM-run salt); build this one with a LinkedHashMap so it keeps the order Python's wire.py builds it in
        // (function_id, then fields), which contract/conformance fixtures compare against.
        Map<String, Object> details = new LinkedHashMap<>();
        details.put("function_id", functionId);
        details.put("fields", List.copyOf(fields));
        return new MissingContext(functionId + " needs context field(s) " + String.join(", ", fields)
            + "; pass them in context=", details);
    }

    /** Python's {@code MissingLabels(function_id)}.
     *
     * @param functionId the function, which takes its labels from the caller
     * @return the error, to throw */
    public static MissingLabels missingLabels(String functionId) {
        return new MissingLabels(functionId + " takes its labels from the caller; pass labels=",
            Map.of("function_id", functionId));
    }

    /** Python's {@code EscalationFailed(step, reason)}.
     *
     * @param step the step that failed: {@code llm}, {@code human}, {@code remote} or {@code function}
     * @param reason why
     * @return the error, to throw */
    public static EscalationFailed escalationFailed(String step, String reason) {
        return new EscalationFailed("Escalation step " + step + " failed: " + reason, Map.of("step", step));
    }

    /** Python's {@code LowConfidence(decision)}: the message shows the confidence as Python's {@code str()} does
     * ({@code None} when the decision has none), and the details carry the decision.
     *
     * @param decision the decision that abstained
     * @return the error, to throw */
    public static LowConfidence lowConfidence(Decision decision) {
        String confidence = decision.confidence() == null ? "None" : PyFormat.floatRepr(decision.confidence());
        return new LowConfidence(decision.function() + " abstained (confidence " + confidence + "); on_low_confidence='error'",
            Map.of("decision", decision.toJson()));
    }

    /** The error a wire body {type, message, details} names; an unknown type is an EngineError naming it. A body
     * missing 'type' or 'message' is malformed and fails loud (Python's error_from_wire indexes body["type"] and
     * body["message"], raising KeyError on either being absent, rather than silently treating a missing key as
     * None). Details that are absent or that Python reads as false (null, false, 0, "", []) are none, as the Python
     * SDK's {@code dict(body.get("details") or {})} reads them; any other details that are not an object break the contract
     * (an error's details are an object) and are a ContractError naming them, where Python crashes or misreads them. */
    static JeffError fromWire(Map<String, ?> body) {
        if (!body.containsKey("type")) throw new ContractError("error wire body is missing its 'type' key");
        if (!body.containsKey("message")) throw new ContractError("error wire body is missing its 'message' key");
        String name = PyFormat.toStr(body.get("type"));
        String message = PyFormat.toStr(body.get("message"));
        BiFunction<String, Map<String, ?>, JeffError> make = TYPES.get(name);
        if (make == null) return new EngineError("unknown error type " + PyFormat.str(name) + " from the engine: " + message);
        Map<String, Object> details = new LinkedHashMap<>();
        Object given = body.get("details");
        if (given instanceof Map<?, ?> fields) {
            for (Map.Entry<?, ?> entry : fields.entrySet()) details.put((String) entry.getKey(), entry.getValue());
        } else if (!pythonFalsy(given)) {
            throw new ContractError("The " + name + " error received has 'details' that are not an object: " + PyFormat.repr(given)
                + "; an error's details must be an object (its message was: " + message + ")");
        }
        return make.apply(message, details);
    }

    /** Whether Python reads the value as false (None, False, 0, 0.0, "", [] or {}): the Python SDK's
     * {@code dict(body.get("details") or {})} reads such details as none. */
    private static boolean pythonFalsy(Object value) {
        return value == null || Boolean.FALSE.equals(value) || value instanceof Number number && isZero(number)
            || value instanceof String text && text.isEmpty() || value instanceof List<?> items && items.isEmpty();
    }

    private static boolean isZero(Number number) {
        if (number instanceof java.math.BigInteger whole) return whole.signum() == 0;
        if (number instanceof java.math.BigDecimal decimal) return decimal.signum() == 0;
        return number.doubleValue() == 0;
    }

    static Map<String, Object> toWire(JeffError error) {
        Map<String, Object> wire = new LinkedHashMap<>();
        wire.put("type", error.type());
        wire.put("message", error.getMessage());
        wire.put("details", new LinkedHashMap<>(error.details()));
        return wire;
    }

    /** The same error (class, message, details, notes, request id) as a new instance, for each waiting caller to
     * get its own. */
    static JeffError copy(JeffError error) {
        JeffError copy = fromWire(toWire(error));
        error.notes().forEach(copy::addNote);
        copy.setRequestId(error.requestId());
        return copy;
    }
}
