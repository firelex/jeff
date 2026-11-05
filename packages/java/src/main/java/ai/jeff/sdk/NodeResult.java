package ai.jeff.sdk;

import java.util.LinkedHashMap;
import java.util.Map;

/** One node's result: a model node's decision, a skipped node's reason, or what a code node returned. toJson() is
 * contract/schemas/graph_result.schema.json's form. */
public sealed interface NodeResult permits NodeResult.Decided, NodeResult.Skipped, NodeResult.Code {
    /** The result as contract/schemas/graph_result.schema.json writes a node.
     *
     * @return {@code {"decision": ...}}, {@code {"skipped": true, "reason": ...}} or {@code {"code": ...}} */
    Map<String, Object> toJson();

    /** A model node's decision.
     *
     * @param decision the decision */
    record Decided(Decision decision) implements NodeResult {
        /** The result as JSON.
         *
         * @return {@code {"decision": ...}} */
        public Map<String, Object> toJson() {
            Map<String, Object> json = new LinkedHashMap<>();
            json.put("decision", decision.toJson());
            return json;
        }
    }

    /** A node that did not run, and why (its {@code when} condition was false, or a node it needs was skipped).
     *
     * @param reason why it was skipped */
    record Skipped(String reason) implements NodeResult {
        /** The result as JSON.
         *
         * @return {@code {"skipped": true, "reason": ...}} */
        public Map<String, Object> toJson() {
            Map<String, Object> json = new LinkedHashMap<>();
            json.put("skipped", true);
            json.put("reason", reason);
            return json;
        }
    }

    /** What a code node returned.
     *
     * @param value the value, any JSON value ({@code null} included) */
    record Code(Object value) implements NodeResult {
        /** The result as JSON.
         *
         * @return {@code {"code": ...}} */
        public Map<String, Object> toJson() {
            Map<String, Object> json = new LinkedHashMap<>();
            json.put("code", value);
            return json;
        }
    }

    /** Reads a node result from its JSON form.
     *
     * @param data {@code {"decision": ...}}, {@code {"skipped": true, "reason": ...}} or {@code {"code": ...}}, already
     *     checked against contract/schemas/graph_result.schema.json
     * @return the result
     * @throws JeffError.GraphError when it holds none of these, or a skipped node has no reason string */
    static NodeResult fromJson(Map<String, Object> data) {
        if (data.containsKey("decision")) return new Decided(Decision.fromJson(Values.map(data.get("decision"))));
        if (data.containsKey("skipped")) {
            if (!(data.get("reason") instanceof String reason)) {
                throw new JeffError.GraphError("A skipped graph result node needs its 'reason' string (got "
                    + PyFormat.repr(data.get("reason")) + ")");
            }
            return new Skipped(reason);
        }
        if (data.containsKey("code")) return new Code(Values.plain(data.get("code")));
        throw new JeffError.GraphError("A graph result node must hold 'decision', 'skipped' or 'code'; got keys "
            + PyFormat.repr(PyFormat.sorted(data.keySet())));
    }
}
