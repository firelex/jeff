package ai.jeff.sdk;

import java.util.List;

/** Anything that decides calls: a Jeff, local or remote (a remote escalation step asks one). */
public interface Decider {
    /** Decides calls, one decision per call in their order.
     *
     * @param calls the calls
     * @return their decisions */
    List<Decision> decide(List<Call> calls);
}
