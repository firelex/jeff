package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.io.UncheckedIOException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.stream.Stream;
import org.junit.jupiter.params.ParameterizedTest;
import org.junit.jupiter.params.provider.Arguments;
import org.junit.jupiter.params.provider.MethodSource;

/** remote.json: the exact requests the Python SDK sends a Jeff server, and the results it reads from the recorded
 * answers, replayed against a server that serves each recorded answer in order once the request is the recorded one. */
class RemoteConformanceTest {
    private static final class Recorded implements AutoCloseable {
        final HttpServer server;
        final List<Object> sent = Collections.synchronizedList(new ArrayList<>());
        private final AtomicInteger next = new AtomicInteger();

        Recorded(List<Object> exchanges) {
            try {
                server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
            } catch (IOException error) {
                throw new UncheckedIOException(error);
            }
            server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
            server.createContext("/", exchange -> {
                String text = new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8);
                Map<String, Object> got = new LinkedHashMap<>();
                got.put("method", exchange.getRequestMethod());
                got.put("path", exchange.getRequestURI().getPath());
                got.put("authorization", exchange.getRequestHeaders().getFirst("Authorization"));
                got.put("body", text.isEmpty() ? null : Json.parse(text));
                sent.add(got);
                int index = next.getAndIncrement();
                if (index >= exchanges.size()) {
                    respond(exchange, 599, Map.of(), "no recorded answer left");
                    return;
                }
                Map<String, Object> answer = Values.map(exchanges.get(index));
                if (!got.equals(answer.get("request"))) {
                    respond(exchange, 599, Map.of(), "this request is not the recorded one");
                    return;
                }
                Map<String, Object> response = Values.map(answer.get("response"));
                respond(exchange, ((Number) response.get("status")).intValue(), Values.map(response.get("headers")),
                    (String) response.get("body"));
            });
            server.start();
        }

        private static void respond(HttpExchange exchange, int status, Map<String, Object> headers, String body) throws IOException {
            for (Map.Entry<String, Object> header : headers.entrySet()) exchange.getResponseHeaders().add(header.getKey(), (String) header.getValue());
            byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
            exchange.sendResponseHeaders(status, bytes.length == 0 ? -1 : bytes.length);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write(bytes);
            }
        }

        String url() {
            return "http://127.0.0.1:" + server.getAddress().getPort();
        }

        @Override
        public void close() {
            server.stop(0);
        }
    }

    private static Map<String, Object> withId(JeffError error) {
        Map<String, Object> wire = new LinkedHashMap<>(Fixtures.wireOf(error));
        wire.put("request_id", error.requestId());
        return Values.map(Fixtures.normalised(wire));
    }

    static Stream<Arguments> cases() {
        return Fixtures.named(Fixtures.cases("remote.json"));
    }

    @ParameterizedTest(name = "{0}")
    @MethodSource("cases")
    void theRemoteClientExchangesWhatThePythonSdkExchanged(String name, Map<String, Object> item) {
        List<Object> exchanges = Values.list(item.get("exchanges"));
        try (Recorded server = new Recorded(exchanges)) {
            List<Object> questions = new ArrayList<>();
            List<Question> asked = new ArrayList<>();
            List<Object> results = new ArrayList<>();
            Map<String, Object> connect = new LinkedHashMap<>(Map.of("ok", true));
            TokenSource token = item.get("token") == null ? TokenSource.none() : TokenSource.of((String) item.get("token"));
            RestTransport transport = new RestTransport(server.url(), token, 10000, RemoteHttpTest.direct());
            RemoteJeff client = null;
            try {
                client = RemoteJeff.connect(transport.asTransport(), Fixtures.catalogueFor((String) item.get("catalogue")), transport.url());
            } catch (JeffError error) {
                connect = new LinkedHashMap<>(Map.of("error", withId(error)));
                transport.close();
            }
            if (client != null) {
                for (Object action : Values.list(item.get("actions"))) {
                    Map<String, Object> op = Values.map(action);
                    try {
                        results.add(Map.of("result", Fixtures.normalised(DecisionReplay.runOp(client, (String) op.get("op"),
                            Values.map(op.get("args")), questions, asked))));
                    } catch (JeffError error) {
                        results.add(Map.of("error", withId(error)));
                    }
                }
                client.close();
            }
            List<Object> requests = new ArrayList<>();
            for (Object exchange : exchanges) requests.add(Values.map(exchange).get("request"));
            assertEquals(requests, new ArrayList<>(server.sent));
            assertEquals(item.get("connect"), connect);
            assertEquals(item.get("results"), results);
            assertEquals(item.get("questions"), questions);
        }
    }
}
