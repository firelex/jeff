package ai.jeff.sdk;

import java.util.List;
import java.util.Map;
import java.util.Objects;
import java.util.SequencedMap;

/** One node of a graph: a model call ({@code call}) or a code function ({@code code}); {@code context} maps a context code name to the
 * param it takes its value from; {@code labels} names the param holding its labels; {@code when} is its condition
 * (graph.schema.json); null for what the node does not have. A {@code when} that is not a JSON value (or nests deeper than
 * Python reads one) is a GraphError naming the node.
 *
 * @param name the node's name
 * @param call the function reference a model node calls, or null
 * @param code the name of the code function a code node runs, or null
 * @param after the nodes this node runs after (empty for none)
 * @param context each context code name, and the param it takes its value from
 * @param labels the param holding the node's labels, or null
 * @param when the node's condition, or null when it always runs
 * @param onLowConfidence the node's policy name ({@code "return"} or {@code "error"}), or null */
public record GraphNode(String name, String call, String code, List<String> after, SequencedMap<String, String> context,
                        String labels, Map<String, Object> when, String onLowConfidence) {
    /** Copies the lists and maps.
     *
     * @param name the node's name
     * @param call the function a model node calls, or null
     * @param code the code function a code node runs, or null
     * @param after the nodes it runs after
     * @param context its context, by code name
     * @param labels the param holding its labels, or null
     * @param when its condition, or null
     * @param onLowConfidence its policy name, or null
     * @throws NullPointerException when {@code after} or {@code context} is null
     * @throws JeffError.GraphError when {@code when} is not a JSON value graph.schema.json allows */
    public GraphNode {
        after = List.copyOf(Objects.requireNonNull(after, "after (an empty list when the node waits on no node)"));
        context = Values.frozenMap(Objects.requireNonNull(context, "context (an empty map when the node takes none)"));
        if (when != null) {
            try {
                when = Values.map(Values.frozen(Values.plain(when)));
            } catch (IllegalArgumentException error) {
                JeffError.GraphError refused = new JeffError.GraphError("The condition of node " + name
                    + " is not a condition graph.schema.json allows: " + error.getMessage());
                refused.initCause(error);
                throw refused;
            }
        }
    }
}
