package ai.jeff.sdk;

import java.util.List;

/** What happens when a decision abstains: "return" it, raise LowConfidence ("error"), or escalate through explicit
 * steps. A Named policy with any other name is refused when it is checked, as the Python SDK refuses it. */
public sealed interface Policy permits Policy.Named, Escalate {
    /** A policy by name: {@code "return"} or {@code "error"}; any other name raises {@link JeffError.InvalidPolicy}
     * when it is checked.
     *
     * @param name the policy's name */
    record Named(String name) implements Policy {}

    /** Return the decision that abstained (the default). */
    Policy RETURN = new Named("return");
    /** Raise {@link JeffError.LowConfidence}, which carries the decision. */
    Policy ERROR = new Named("error");

    /** A policy by name, as Python's {@code on_low_confidence="..."}.
     *
     * @param name {@code "return"} or {@code "error"}; any other name raises {@link JeffError.InvalidPolicy} when it
     *     is checked
     * @return the policy */
    static Policy named(String name) {
        return new Named(name);
    }

    /** Escalate through these steps, in order, until one answers without abstaining, with no time limit.
     *
     * @param steps the steps, at least one
     * @return the policy
     * @throws JeffError.InvalidPolicy when there is no step */
    static Escalate escalate(List<Step> steps) {
        return new Escalate(steps, null);
    }

    /** Escalate through these steps, in order, until one answers without abstaining, within a time limit: a step
     * that would start after it raises {@link JeffError.EscalationBudgetExceeded}.
     *
     * @param steps the steps, at least one
     * @param budgetMs the longest the whole escalation may take: a Long, an Integer or a Double of milliseconds, or
     *     null for no limit
     * @return the policy
     * @throws JeffError.InvalidPolicy when there is no step or the budget is another kind of number */
    static Escalate escalate(List<Step> steps, Number budgetMs) {
        return new Escalate(steps, budgetMs);
    }
}
