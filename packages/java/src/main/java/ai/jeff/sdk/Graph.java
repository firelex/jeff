package ai.jeff.sdk;

import java.util.List;
import java.util.SequencedMap;

/** A decision graph (contract/schemas/graph.schema.json), checked: its nodes in document order and the order its
 * dependencies give. Read one with {@link Graphs#load} or {@link Graphs#fromMap}, and run it with
 * {@link Jeff#runGraph(Graph, Object, Map)}.
 *
 * @param id the graph's id
 * @param version the graph's version
 * @param input the name the graph's input is read by
 * @param params the names of the graph's params
 * @param nodes the nodes by name, in document order
 * @param outputs the names of the nodes whose results are the graph's outputs
 * @param order the nodes in the order their dependencies give */
public record Graph(String id, String version, String input, List<String> params, SequencedMap<String, GraphNode> nodes,
                    List<String> outputs, List<String> order) {
    /** Copies the lists and freezes the nodes.
     *
     * @param id the graph's id
     * @param version the graph's version
     * @param input the input's name
     * @param params the params' names
     * @param nodes the nodes by name
     * @param outputs the output nodes' names
     * @param order the nodes in dependency order */
    public Graph {
        params = List.copyOf(params);
        nodes = Values.frozenMap(nodes);
        outputs = List.copyOf(outputs);
        order = List.copyOf(order);
    }
}
