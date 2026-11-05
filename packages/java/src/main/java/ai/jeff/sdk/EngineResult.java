package ai.jeff.sdk;

import java.util.List;
import java.util.SequencedMap;

/** What an engine answers for one predict: one probability array per row (over the row's options, in order), its
 * timing, and each row's prompt length in tokens (null when the engine does not count them). The arrays are copied
 * in and out, so no holder can change what another sees.
 *
 * @param probabilities one probability array per row, over the row's options in order
 * @param timing the engine's timing fields (such as {@code model_ms}), in its order
 * @param inputTokens each row's prompt length in tokens, or null when the engine does not count them */
public record EngineResult(List<double[]> probabilities, SequencedMap<String, Object> timing, List<Long> inputTokens) {
    /** Copies the arrays in and freezes the timing.
     *
     * @param probabilities one probability array per row
     * @param timing the engine's timing fields
     * @param inputTokens each row's prompt length in tokens, or null */
    public EngineResult {
        probabilities = probabilities.stream().map(double[]::clone).toList();
        timing = frozenTiming(timing);
        inputTokens = inputTokens == null ? null : List.copyOf(inputTokens);
    }

    @SuppressWarnings("unchecked")
    private static SequencedMap<String, Object> frozenTiming(SequencedMap<String, Object> timing) {
        return (SequencedMap<String, Object>) Values.frozen(Values.plain(timing));
    }

    /** Each row's probabilities, copied, so changing them changes nothing here.
     *
     * @return one array per row, over the row's options in order */
    @Override
    public List<double[]> probabilities() {
        return probabilities.stream().map(double[]::clone).toList();
    }
}
