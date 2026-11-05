package ai.jeff.sdk;

import java.util.List;
import java.util.Map;

/** How RemoteJeff reaches a server: one round trip per method, bodies and answers in the JSON shapes of
 * contract/schemas/decide_request and decide_response. */
interface Transport extends AutoCloseable {
    /** The decisions for `calls`, in order, with `policy` (wire form) applied by the server. */
    List<Map<String, Object>> decideCalls(List<Map<String, Object>> calls, Object policy);

    /** The routed decision for a {dispatch, on_low_confidence} body. */
    Map<String, Object> dispatch(Map<String, Object> body);

    /** The graph result for a {graph, input, params, ...} body. */
    Map<String, Object> graph(Map<String, Object> body);

    /** The server's GET /v1/functions answer (contract/schemas/functions_response). */
    Map<String, Object> functions();

    @Override
    void close();
}
