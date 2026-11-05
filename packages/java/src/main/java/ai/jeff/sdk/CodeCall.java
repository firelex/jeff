package ai.jeff.sdk;

import java.util.Map;
import java.util.SequencedMap;

/** What a code function is given: the graph's input, its params, and every node result so far, in the order the
 * nodes got them.
 *
 * @param input the graph's input
 * @param params the graph's params
 * @param nodes every node result so far, by node name */
public record CodeCall(Object input, Map<String, Object> params, SequencedMap<String, NodeResult> nodes) {}
