package ai.jeff.sdk;

import java.util.LinkedHashMap;
import java.util.Map;
import java.util.SequencedMap;

/** A graph's result: every node's result (in the order the nodes got them), the outputs, and how many stages ran.
 *
 * @param nodes every node's result, by node name, in the order the nodes got them
 * @param outputs the results of the graph's outputs, by node name
 * @param stages how many stages ran (each stage runs every node whose dependencies have results) */
public record GraphResult(SequencedMap<String, NodeResult> nodes, SequencedMap<String, NodeResult> outputs, long stages) {
    /** Freezes the maps.
     *
     * @param nodes every node's result
     * @param outputs the outputs' results
     * @param stages how many stages ran */
    public GraphResult {
        nodes = Values.frozenMap(nodes);
        outputs = Values.frozenMap(outputs);
    }

    private static Map<String, Object> json(Map<String, NodeResult> results) {
        Map<String, Object> out = new LinkedHashMap<>();
        for (Map.Entry<String, NodeResult> entry : results.entrySet()) out.put(entry.getKey(), entry.getValue().toJson());
        return out;
    }

    /** The result as contract/schemas/graph_result.schema.json writes it.
     *
     * @return {@code {"nodes", "outputs", "stages"}} in that order */
    public Map<String, Object> toJson() {
        Map<String, Object> out = new LinkedHashMap<>();
        out.put("nodes", json(nodes));
        out.put("outputs", json(outputs));
        out.put("stages", stages);
        return out;
    }

    private static SequencedMap<String, NodeResult> read(Map<String, Object> data, String key) {
        if (!(data.get(key) instanceof Map<?, ?> results)) {
            throw new JeffError.GraphError("A graph result needs its '" + key + "' object (got " + PyFormat.repr(data.get(key)) + ")");
        }
        SequencedMap<String, NodeResult> out = new LinkedHashMap<>();
        for (Map.Entry<String, Object> entry : Values.map(results).entrySet()) {
            if (!(entry.getValue() instanceof Map<?, ?> node)) {
                throw new JeffError.GraphError("A graph result's " + key + " entry " + PyFormat.str(entry.getKey())
                    + " must be an object (got " + PyFormat.repr(entry.getValue()) + ")");
            }
            out.put(entry.getKey(), NodeResult.fromJson(Values.map(node)));
        }
        return out;
    }

    /** Reads a graph result in contract/schemas/graph_result.schema.json's form (a server's answer).
     *
     * @param data the result's JSON
     * @return the result
     * @throws JeffError.GraphError naming a part it lacks or holds in the wrong form */
    public static GraphResult fromJson(Map<String, Object> data) {
        Object stages = data.get("stages");
        if (!(stages instanceof Long || stages instanceof Integer)) {
            throw new JeffError.GraphError("A graph result needs its 'stages' count (got " + PyFormat.repr(stages) + ")");
        }
        return new GraphResult(read(data, "nodes"), read(data, "outputs"), ((Number) stages).longValue());
    }
}
