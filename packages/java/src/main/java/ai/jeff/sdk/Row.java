package ai.jeff.sdk;

import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;

/** The row the model reads: the state (named fields in their trained order, or the plain input string) and the
 * question. {@link #toWire()} is both its JSON and what is packed for the engine.
 *
 * @param state the named fields in their trained order (a SequencedMap), or the plain input string
 * @param question the question the model answers */
public record Row(Object state, RowQuestion question) {
    /** The row as the engine reads it.
     *
     * @return {@code {"state": ..., "question": ...}}, in that order */
    public Map<String, Object> toWire() {
        Map<String, Object> wire = new LinkedHashMap<>();
        wire.put("state", state);
        wire.put("question", question.toWire());
        return wire;
    }

    /** A row given in its wire form (a socket fixture, a test), its maps kept in their order. */
    static Row fromWire(Map<String, Object> wire) {
        Object state = wire.get("state") instanceof Map<?, ?> fields ? Values.frozenMap(Values.map(fields)) : wire.get("state");
        Map<String, Object> question = Values.map(wire.get("question"));
        Object criteria = question.get("criteria");
        if (criteria instanceof Map<?, ?> options) criteria = Values.frozenMap(Values.map(options));
        else if (criteria instanceof List<?> levels) criteria = Values.frozenList(Values.list(levels));
        return new Row(state, new RowQuestion((String) question.get("type"), (String) question.get("instructions"), criteria));
    }
}
