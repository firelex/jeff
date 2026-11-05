package ai.jeff.sdk;

import java.util.AbstractMap;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.SequencedMap;

/** What an llm or human step is shown: the function, its instructions, the state the model read (named fields in the
 * order the model reads them, or the plain input string for a text input) and the option keys it may answer with,
 * each with its description, in option order. Build a prompt from it in these orders.
 *
 * @param function the function's id
 * @param instructions the function's instructions
 * @param state the state the model read: its fields in order (a SequencedMap), or the plain input string
 * @param options each option key the step may answer with, and its description, in option order */
public record Question(String function, String instructions, Object state, SequencedMap<String, Object> options) {
    /** The state's fields in the order the model reads them.
     *
     * @return the fields, or null when the state is the plain input string */
    public List<Map.Entry<String, Object>> stateEntries() {
        if (!(state instanceof Map<?, ?> fields)) return null;
        return entries(fields);
    }

    /** The options in option order.
     *
     * @return each key the step may answer with, and its description */
    public List<Map.Entry<String, Object>> optionEntries() {
        return entries(options);
    }

    private static List<Map.Entry<String, Object>> entries(Map<?, ?> map) {
        List<Map.Entry<String, Object>> out = new ArrayList<>();
        for (Map.Entry<?, ?> entry : map.entrySet()) {
            out.add(new AbstractMap.SimpleImmutableEntry<>((String) entry.getKey(), entry.getValue()));
        }
        return Collections.unmodifiableList(out);
    }

    /** The question as JSON, as the Python SDK's {@code Question} writes it.
     *
     * @return {@code {"function", "instructions", "state", "options"}} in that order */
    public Map<String, Object> toJson() {
        Map<String, Object> json = new LinkedHashMap<>();
        json.put("function", function);
        json.put("instructions", instructions);
        json.put("state", state instanceof Map<?, ?> fields ? new LinkedHashMap<>(fields) : state);
        json.put("options", new LinkedHashMap<>(options));
        return json;
    }
}
