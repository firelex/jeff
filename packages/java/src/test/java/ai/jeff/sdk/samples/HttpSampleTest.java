package ai.jeff.sdk.samples;

import static org.junit.jupiter.api.Assertions.assertEquals;

import ai.jeff.sdk.Decision;
import ai.jeff.sdk.RestTransport;
import ai.jeff.sdk.TokenSource;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.atomic.AtomicReference;
import org.junit.jupiter.api.Test;

/** The shape of Java the website generates for a worked example sent over HTTP (apps/hub/app/lib/sdk-code.server.ts,
 * javaHttp), written in another package so only the public surface is reachable: it must compile and run. */
class HttpSampleTest {
    private static final String DECISION = "{\"decisions\": [{\"value\": \"yes\", \"confidence\": 0.9, \"abstained\": false,"
        + " \"calibrated\": true, \"interface\": \"jeff.binary\", \"function\": \"jeff.test.answer\", \"version\": \"1.0.0\","
        + " \"probabilities\": {\"yes\": 0.9, \"no\": 0.1}, \"score\": null, \"route\": [], \"decided_by\": \"jeff.test.answer\","
        + " \"escalations\": [], \"timing\": {}, \"evidence_refs\": []}]}";

    @Test
    void theWebsitesHttpSampleCompilesAgainstThePublicSurfaceAndRuns() throws IOException {
        AtomicReference<String> sent = new AtomicReference<>();
        HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
        server.createContext("/v1/decide", exchange -> {
            sent.set(new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8));
            byte[] body = DECISION.getBytes(StandardCharsets.UTF_8);
            exchange.getResponseHeaders().add("Content-Type", "application/json");
            exchange.sendResponseHeaders(200, body.length);
            exchange.getResponseBody().write(body);
            exchange.close();
        });
        server.start();
        String origin = "http://127.0.0.1:" + server.getAddress().getPort();
        String token = "key";
        try {
            // from here, the generated sample's statements
            var call = new LinkedHashMap<String, Object>();
            call.put("function", "jeff.test.answer");
            call.put("input", "Is the sky blue?");
            try (RestTransport transport = new RestTransport(origin, TokenSource.of(token), 30000)) {
                List<Map<String, Object>> answers = transport.decideCalls(List.of(call), "return");
                Decision decision = Decision.fromJson(answers.get(0));
                assertEquals("yes 0.9", decision.value() + " " + decision.confidence());
                assertEquals(origin, transport.url());
            }
        } finally {
            server.stop(0);
        }
        assertEquals("{\"calls\":[{\"function\":\"jeff.test.answer\",\"input\":\"Is the sky blue?\"}],\"on_low_confidence\":\"return\"}",
            sent.get());
    }
}
