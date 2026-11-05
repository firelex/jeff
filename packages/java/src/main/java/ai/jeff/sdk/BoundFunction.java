package ai.jeff.sdk;

import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.concurrent.CompletableFuture;

/** A function bound once, now: its context, labels and policy are checked when it is made
 * ({@link Jeff#function(String, CallOptions)}), and the binding is reused by every call. Inputs may be any JSON value,
 * null included. */
public class BoundFunction implements AutoCloseable {
    final Jeff jeff;
    final Call template;
    final Policy policy;
    final Binding binding;

    BoundFunction(Jeff jeff, Call template, Policy policy, Binding binding) {
        this.jeff = jeff;
        this.template = template;
        this.policy = policy;
        this.binding = binding;
    }

    static BoundFunction create(Jeff jeff, Call template, Policy policy) {
        Binding binding = jeff.bindCall(template);
        Escalation.checkPolicy(jeff, binding.function().interfaceName(), policy);
        return new BoundFunction(jeff, template, policy, binding);
    }

    Binding binding() {
        return binding;
    }

    Policy policy() {
        return policy;
    }

    Call callOf(Object input) {
        return new Call(template.function(), input, template.context(), template.labels());
    }

    /** Decides one input (Python's {@code bound(input)}).
     *
     * @param input the input, any JSON value
     * @return its decision, after the bound policy
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError the engine's or server's named errors as they happen ({@link JeffError.EngineError},
     *     {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded}, {@link JeffError.BackendUnavailable}, ...) */
    public Decision call(Object input) {
        return many(Collections.singletonList(input)).get(0);
    }

    /** Decides many inputs at once, in one engine request (or one server request).
     *
     * @param inputs the inputs, each any JSON value
     * @return their decisions, in order, after the bound policy
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError the engine's or server's named errors as they happen ({@link JeffError.EngineError},
     *     {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded}, {@link JeffError.BackendUnavailable}, ...) */
    public List<Decision> many(List<?> inputs) {
        List<Call> calls = new ArrayList<>();
        for (Object input : inputs) calls.add(callOf(input));
        List<Jeff.Bound> bound = new ArrayList<>();
        for (Call call : calls) bound.add(new Jeff.Bound(call, binding));
        List<Decision> decisions = jeff.decideBound(bound);
        List<Decision> out = new ArrayList<>();
        for (int index = 0; index < calls.size(); index++) out.add(jeff.applyPolicy(calls.get(index), decisions.get(index), policy));
        return Collections.unmodifiableList(out);
    }

    /** {@link #call} on a virtual thread.
     *
     * @param input the input
     * @return a future of its decision, failed with the error {@link #call} would raise */
    public CompletableFuture<Decision> callAsync(Object input) {
        return Async.run(() -> call(input));
    }

    /** {@link #many} on a virtual thread.
     *
     * @param inputs the inputs
     * @return a future of their decisions, failed with the error {@link #many} would raise */
    public CompletableFuture<List<Decision>> manyAsync(List<?> inputs) {
        // a null list fails in the future, as every other failure of an ...Async method does
        List<Object> copy = inputs == null ? null : new ArrayList<>(inputs);
        return Async.run(() -> many(copy));
    }

    /** Releases what this bound function holds on a server: nothing in local mode or over HTTP; over the channel
     * ({@link ChannelJeff}) its handle. */
    @Override
    public void close() {}
}
