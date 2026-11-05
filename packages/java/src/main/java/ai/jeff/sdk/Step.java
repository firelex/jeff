package ai.jeff.sdk;

/** One escalation step: another function (run by the engine or server), another client, or your own function asking
 * an LLM or a person. */
public sealed interface Step permits Step.FunctionStep, Step.RemoteStep, Step.AskStep {
    /** The step's kind, as the Python SDK names it.
     *
     * @return {@code "function"}, {@code "remote"}, {@code "llm"} or {@code "human"} */
    String name();

    /** Ask another function the same input (it must implement the same interface).
     *
     * @param ref the function's reference */
    record FunctionStep(String ref) implements Step {
        /** The step's kind.
         *
         * @return {@code "function"} */
        public String name() {
            return "function";
        }
    }

    /** Ask another client (a remote Jeff, say) the same call.
     *
     * @param client the client that decides it */
    record RemoteStep(Decider client) implements Step {
        /** The step's kind.
         *
         * @return {@code "remote"} */
        public String name() {
            return "remote";
        }
    }

    /** Ask your own function, which asks an LLM or a person.
     *
     * @param ask your function
     * @param name {@code "llm"} or {@code "human"} */
    record AskStep(Ask ask, String name) implements Step {
        /** Checks the name.
         *
         * @param ask your function
         * @param name {@code "llm"} or {@code "human"}
         * @throws IllegalArgumentException for any other name */
        public AskStep {
            if (!"llm".equals(name) && !"human".equals(name)) {
                throw new IllegalArgumentException("An ask step is named llm or human, not " + name);
            }
        }
    }

    /** A step that asks another function (Python's {@code function_step}).
     *
     * @param ref the function's reference; when the policy is checked, an unknown function raises
     *     {@link JeffError.InvalidPolicy}, one the engine does not serve {@link JeffError.NotInstalled}, and one of
     *     another interface {@link JeffError.InterfaceMismatch}
     * @return the step */
    static FunctionStep function(String ref) {
        return new FunctionStep(ref);
    }

    /** A step that asks another client (Python's {@code remote}).
     *
     * @param client the client
     * @return the step */
    static RemoteStep remote(Decider client) {
        return new RemoteStep(client);
    }

    /** A step that asks an LLM through your function (Python's {@code llm}).
     *
     * @param ask your function; an answer that is not an option key raises {@link JeffError.EscalationFailed}
     * @return the step */
    static AskStep llm(Ask ask) {
        return new AskStep(ask, "llm");
    }

    /** A step that asks a person through your function (Python's {@code human}).
     *
     * @param ask your function; an answer that is not an option key raises {@link JeffError.EscalationFailed}
     * @return the step */
    static AskStep human(Ask ask) {
        return new AskStep(ask, "human");
    }
}
