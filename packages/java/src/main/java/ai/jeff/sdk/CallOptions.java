package ai.jeff.sdk;

import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.Map;

/** How a function is called: its context (by code name), its labels, and what happens when the decision abstains.
 * Immutable: each setter returns a new CallOptions. Labels are a SequencedMap (LinkedHashMap) of key -> description,
 * or a List of keys; a map that keeps no order is refused when the call is bound. */
public final class CallOptions {
    private static final CallOptions NONE = new CallOptions(null, null, Policy.RETURN);

    private final Map<String, Object> context;
    private final Object labels;
    private final Policy onLowConfidence;

    private CallOptions(Map<String, ?> context, Object labels, Policy onLowConfidence) {
        this.context = context == null ? null : Collections.unmodifiableMap(new LinkedHashMap<String, Object>(context));
        this.labels = labels;
        this.onLowConfidence = onLowConfidence;
    }

    /** No context, no labels, and {@code on_low_confidence} "return" (the Python SDK's default).
     *
     * @return the options with nothing set */
    public static CallOptions none() {
        return NONE;
    }

    /** These options with another context.
     *
     * @param newContext the context fields by code name (any map: they are bound in the manifest's field order), or
     *     {@code null} for none; missing required fields raise {@link JeffError.MissingContext} and unknown ones
     *     {@link JeffError.UnknownContext} when the call is bound
     * @return new options */
    public CallOptions context(Map<String, ?> newContext) {
        return new CallOptions(newContext, labels, onLowConfidence);
    }

    /** These options with other labels.
     *
     * @param newLabels a {@code SequencedMap} of key to description, a {@code List} of keys, or {@code null} for none;
     *     labels the function does not accept raise {@link JeffError.InvalidLabels} when the call is bound
     * @return new options */
    public CallOptions labels(Object newLabels) {
        return new CallOptions(context, newLabels, onLowConfidence);
    }

    /** These options with another policy for a decision that abstains.
     *
     * @param policy {@link Policy#RETURN}, {@link Policy#ERROR} or {@link Policy#escalate(java.util.List)}; anything
     *     else ({@code null} included) raises {@link JeffError.InvalidPolicy} when the call is checked, as the Python
     *     SDK refuses it
     * @return new options */
    public CallOptions onLowConfidence(Policy policy) {
        return new CallOptions(context, labels, policy);
    }

    /** The context fields by code name.
     *
     * @return an unmodifiable copy of the context given, or {@code null} for none */
    public Map<String, Object> context() {
        return context;
    }

    /** The caller's labels.
     *
     * @return the labels given, or {@code null} for none */
    public Object labels() {
        return labels;
    }

    /** What happens when the decision abstains.
     *
     * @return the policy given ({@link Policy#RETURN} unless set) */
    public Policy onLowConfidence() {
        return onLowConfidence;
    }
}
