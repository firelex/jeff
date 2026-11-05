package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;

/** Escalate through these steps, in order, until one answers without abstaining. Made by
 * {@link Policy#escalate(List)}. */
public final class Escalate implements Policy {
    private final List<Step> steps;
    private final Number budgetMs;

    Escalate(List<Step> steps, Number budgetMs) {
        if (steps.isEmpty()) throw new JeffError.InvalidPolicy("escalate() needs at least one step");
        if (budgetMs != null && !(budgetMs instanceof Long || budgetMs instanceof Integer || budgetMs instanceof Double)) {
            throw new JeffError.InvalidPolicy("escalate() budget_ms must be a Long, an Integer or a Double of milliseconds; got a "
                + budgetMs.getClass().getName());
        }
        this.steps = Collections.unmodifiableList(new ArrayList<>(steps));
        this.budgetMs = budgetMs instanceof Integer whole ? Long.valueOf(whole) : budgetMs;
    }

    /** The steps, in the order they are tried.
     *
     * @return the steps */
    public List<Step> steps() {
        return steps;
    }

    /** The longest the whole escalation may take.
     *
     * @return milliseconds (a Long or a Double), or null for no limit */
    public Number budgetMs() {
        return budgetMs;
    }
}
