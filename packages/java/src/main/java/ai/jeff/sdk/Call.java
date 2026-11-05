package ai.jeff.sdk;

import java.util.Map;

/** One call of a function: its reference, input, context by code name, and labels (null when not given). Labels are
 * a SequencedMap of key -> description (caller and describe labels; a LinkedHashMap keeps the order you give) or a
 * List of keys (subset labels).
 *
 * @param function the function's reference: its id ({@code jeff.safety.spam.is_spam}), short name
 *     ({@code spam.is_spam}), or either with {@code @version}
 * @param input the call's input, any JSON value ({@code null} included)
 * @param context the context fields by code name, or {@code null} for none
 * @param labels the caller's labels, or {@code null} when not given */
public record Call(String function, Object input, Map<String, ?> context, Object labels) {
    /** A call with no context and no labels.
     *
     * @param function the function's reference
     * @param input the call's input
     * @return the call */
    public static Call of(String function, Object input) {
        return new Call(function, input, null, null);
    }
}
