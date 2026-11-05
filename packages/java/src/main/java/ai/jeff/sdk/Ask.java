package ai.jeff.sdk;

/** Your own function behind an {@code llm} or {@code human} escalation step ({@link Step#llm}, {@link Step#human}):
 * shown a {@link Question}, it returns one of the question's option keys. */
@FunctionalInterface
public interface Ask {
    /** Answers the question.
     *
     * @param question what the model was asked, its state and its options, in the order the model reads them
     * @return one of {@code question.options()}' keys; anything else makes the escalation raise
     *     {@link JeffError.EscalationFailed} */
    String answer(Question question);
}
