package ai.jeff.sdk;

import java.lang.ref.Cleaner;
import java.util.ArrayList;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicBoolean;

/** A bound function that is a handle on the channel: bound here (context, labels, policy checked) and then on the
 * server with one {@code bind}; each {@link #call} is one {@code call} frame, {@link #many} one {@code call_many}. A policy the server can run is bound
 * with the handle; one with an llm, human or remote step is bound as "return" and run here on each decision. close()
 * releases the handle; a bound function the garbage collector takes has its release queued by a {@code Cleaner}
 * action. */
public final class ChannelBoundFunction extends RemoteBoundFunction {
    private static final Cleaner CLEANER = Cleaner.create();

    private final ChannelTransport transport;
    private final Object wire;
    private final long handle;
    private final AtomicBoolean released;

    private ChannelBoundFunction(ChannelJeff jeff, Call template, Policy policy, Binding binding, ChannelTransport transport,
                                 Object wire, long handle) {
        super(jeff, template, policy, binding);
        this.transport = transport;
        this.wire = wire;
        this.handle = handle;
        AtomicBoolean flag = new AtomicBoolean();
        this.released = flag;
        // the action holds the transport, the handle and the flag, never this object
        CLEANER.register(this, () -> {
            if (flag.compareAndSet(false, true)) transport.releaseLater(handle);
        });
    }

    static ChannelBoundFunction create(ChannelJeff jeff, Call template, Policy policy) {
        Binding binding = jeff.bindCall(template);
        Escalation.checkPolicy(jeff, binding.function().interfaceName(), policy);
        Object wire = jeff.wirePolicy(policy);
        ChannelTransport transport = jeff.channel();
        Map<String, Object> context = template.context() == null ? null : new LinkedHashMap<>(template.context());
        Object labels = template.labels() == null ? null : RemoteJeff.labelsToWire(template.labels());
        ChannelTransport.Handle handle = transport.bind(binding.function().id(), context, labels, wire == null ? "return" : wire);
        if (!handle.version().equals(binding.manifest().version())) {
            // The adapter changed on the server since connecting: the handle is released before the next request.
            transport.releaseLater(handle.handle());
            throw RemoteJeff.versionMismatch(binding.manifest().id(), handle.version(), binding.manifest().version());
        }
        return new ChannelBoundFunction(jeff, template, policy, binding, transport, wire, handle.handle());
    }

    private long live() {
        if (released.get()) {
            throw new JeffError.InvalidRequest("This bound function of " + binding.function().id() + " is closed; bind it again "
                + "with jeff.function");
        }
        return handle;
    }

    /** Decides one input with one {@code call} frame.
     *
     * @param input the input, any JSON value
     * @return its decision, after the bound policy
     * @throws JeffError.InvalidRequest when the handle was released (closed)
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError the server's named errors as they happen ({@link JeffError.EngineError},
     *     {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded}, {@link JeffError.BackendUnavailable}, ...) */
    @Override
    public Decision call(Object input) {
        Decision decision = Decision.fromJson(transport.call(live(), input));
        if (wire != null) return decision;
        return jeff.applyPolicy(callOf(input), decision, policy);
    }

    /** Decides many inputs with one {@code call_many} frame.
     *
     * @param inputs the inputs, at most 256
     * @return their decisions, in order, after the bound policy
     * @throws JeffError.InvalidRequest when the handle was released (closed) or there are more than 256 inputs
     * @throws JeffError.LowConfidence when it abstains under {@link Policy#ERROR}
     * @throws JeffError.EscalationFailed when an escalation step fails or answers with something that is not an
     *     option
     * @throws JeffError.EscalationBudgetExceeded when an escalation runs past its budget
     * @throws JeffError the server's named errors as they happen ({@link JeffError.EngineError},
     *     {@link JeffError.Busy}, {@link JeffError.DeadlineExceeded}, {@link JeffError.BackendUnavailable}, ...) */
    @Override
    public List<Decision> many(List<?> inputs) {
        long live = live();
        if (inputs.isEmpty()) return List.of();
        RemoteJeff.checkBatch(inputs.size(), "inputs");
        List<Decision> decisions = new ArrayList<>();
        for (Map<String, Object> item : transport.callMany(live, inputs)) decisions.add(Decision.fromJson(item));
        if (wire != null) return List.copyOf(decisions);
        List<Decision> out = new ArrayList<>();
        for (int index = 0; index < inputs.size(); index++) out.add(jeff.applyPolicy(callOf(inputs.get(index)), decisions.get(index), policy));
        return List.copyOf(out);
    }

    /** Releases the handle. Nothing to release once the channel is closed: its handles ended with it. */
    @Override
    public void close() {
        if (!released.compareAndSet(false, true)) return;
        if (!transport.isClosed()) transport.release(handle);
    }
}
