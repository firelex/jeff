package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.List;

/** A bound function of a {@link RemoteJeff}: bound here once (context, labels and policy checked), each
 * {@link #call} or {@link #many} one request to the server. */
public class RemoteBoundFunction extends BoundFunction {
    RemoteBoundFunction(Jeff jeff, Call template, Policy policy, Binding binding) {
        super(jeff, template, policy, binding);
    }

    static RemoteBoundFunction create(RemoteJeff jeff, Call template, Policy policy) {
        Binding binding = jeff.bindCall(template);
        Escalation.checkPolicy(jeff, binding.function().interfaceName(), policy);
        return new RemoteBoundFunction(jeff, template, policy, binding);
    }

    /** Decides many inputs in one request to the server, which runs the policy when it can (an llm, human or
     * remote step runs here).
     *
     * @param inputs the inputs, at most 256
     * @return their decisions, in order, after the bound policy
     * @throws JeffError.InvalidRequest when there are more than 256 inputs
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError the server's named errors as they happen ({@link JeffError.EngineError},
     *     {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded}, {@link JeffError.BackendUnavailable}, ...) */
    @Override
    public List<Decision> many(List<?> inputs) {
        List<Jeff.Bound> bound = new ArrayList<>();
        for (Object input : inputs) bound.add(new Jeff.Bound(callOf(input), binding));
        return ((RemoteJeff) jeff).decidePolicy(bound, policy);
    }
}
