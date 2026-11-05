package ai.jeff.sdk;

import java.util.LinkedHashMap;
import java.util.Map;

/** The question of a row: its type, instructions and criteria (a SequencedMap of option -> description, a List of
 * levels, or null).
 *
 * @param type the question type, as contract/interfaces.yaml names it
 * @param instructions what the model is asked
 * @param criteria a SequencedMap of option to description, a List of levels, or null */
public record RowQuestion(String type, String instructions, Object criteria) {
    /** The question as the engine reads it.
     *
     * @return {@code {"type", "instructions", "criteria"}} in that order, without {@code criteria} when it is null */
    public Map<String, Object> toWire() {
        Map<String, Object> wire = new LinkedHashMap<>();
        wire.put("type", type);
        wire.put("instructions", instructions);
        if (criteria != null) wire.put("criteria", criteria);
        return wire;
    }
}
