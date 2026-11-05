package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import com.sun.net.httpserver.HttpExchange;
import com.sun.net.httpserver.HttpServer;
import java.io.IOException;
import java.io.OutputStream;
import java.net.ConnectException;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.Executors;
import java.util.concurrent.atomic.AtomicInteger;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

class RemoteTest {
    private static final String LISTING = (String) Values.map(Values.map(Values.list(Values.map(
        Fixtures.cases("remote.json").get(0)).get("exchanges")).get(0)).get("response")).get("body");
    private static final Map<String, Object> DECISION = Values.map(Values.list(Values.map(Json.parse((String) Values.map(
        Values.map(Values.list(Fixtures.cases("remote.json").get(1).get("exchanges")).get(1)).get("response")).get("body"))).get("decisions")).get(0));
    private static final Map<String, Object> CTX = Fixtures.ordered("company", "A furniture shop.", "channel", "email");

    /** A request handler; `hold` blocks until the server is closed (a server that never answers, or stops). */
    interface Handler {
        void handle(HttpExchange exchange, String body, Runnable hold) throws IOException;
    }

    /** A test server; close() releases every handler held by `hold` first. */
    private record Serve(HttpServer server, CountDownLatch gate) implements AutoCloseable {
        static Serve start(Handler handler) throws IOException {
            HttpServer server = HttpServer.create(new InetSocketAddress(InetAddress.getLoopbackAddress(), 0), 0);
            server.setExecutor(Executors.newVirtualThreadPerTaskExecutor());
            CountDownLatch gate = new CountDownLatch(1);
            Runnable hold = () -> {
                try {
                    gate.await();
                } catch (InterruptedException error) {
                    Thread.currentThread().interrupt(); // the server is stopping; the handler ends
                }
            };
            server.createContext("/", exchange -> handler.handle(exchange,
                new String(exchange.getRequestBody().readAllBytes(), StandardCharsets.UTF_8), hold));
            server.start();
            return new Serve(server, gate);
        }

        String url() {
            return "http://127.0.0.1:" + server.getAddress().getPort();
        }

        @Override
        public void close() {
            gate.countDown();
            server.stop(0);
        }
    }

    private static void json(HttpExchange exchange, int status, String body, String requestId) throws IOException {
        exchange.getResponseHeaders().add("content-type", "application/json");
        exchange.getResponseHeaders().add("x-request-id", requestId);
        byte[] bytes = body.getBytes(StandardCharsets.UTF_8);
        exchange.sendResponseHeaders(status, bytes.length);
        try (OutputStream out = exchange.getResponseBody()) {
            out.write(bytes);
        }
    }

    private static RemoteOptions options(long timeoutMs) {
        return RemoteOptions.timeoutMs(timeoutMs);
    }

    /** The first request of a fresh JVM loads the HTTP client's classes (about 100 ms); the timing tests measure
     * after one has been made. */
    @BeforeAll
    static void warmUp() throws IOException {
        try (Serve server = Serve.start((exchange, body, hold) -> json(exchange, 200, LISTING, "warm"));
             RemoteJeff jeff = Jeff.remote(server.url(), TokenSource.none(), options(5000))) {
            jeff.engine().installed(true);
        }
    }

    private static long millisSince(long started) {
        return (System.nanoTime() - started) / 1_000_000;
    }

    @Test
    void aServerThatCannotBeReachedIsBackendUnavailable() {
        assertThrows(JeffError.BackendUnavailable.class, () -> Jeff.remote("http://127.0.0.1:9", TokenSource.none(), options(2000)));
    }

    @Test
    void aServerThatDoesNotSendHeadersInTimeIsDeadlineExceededPromptly() throws IOException {
        try (Serve server = Serve.start((exchange, body, hold) -> hold.run())) {
            long started = System.nanoTime();
            JeffError.DeadlineExceeded error = assertThrows(JeffError.DeadlineExceeded.class,
                () -> Jeff.remote(server.url(), TokenSource.none(), options(200)));
            long elapsed = millisSince(started);
            assertEquals("The Jeff server at " + server.url() + " did not answer GET /v1/functions within 200 ms; the server may "
                + "still have run the request", error.getMessage());
            assertTrue(elapsed >= 190 && elapsed < 400, elapsed + " ms");
        }
    }

    @Test
    void anAnswerWhoseBodyStopsArrivingIsDeadlineExceededPromptly() throws IOException {
        try (Serve server = Serve.start((exchange, body, hold) -> {
            exchange.sendResponseHeaders(200, 0);
            OutputStream out = exchange.getResponseBody();
            out.write("{\"base_format\":".getBytes(StandardCharsets.UTF_8));
            out.flush();
            hold.run();
        })) {
            long started = System.nanoTime();
            JeffError.DeadlineExceeded error = assertThrows(JeffError.DeadlineExceeded.class,
                () -> Jeff.remote(server.url(), TokenSource.none(), options(300)));
            assertTrue(millisSince(started) < 500, millisSince(started) + " ms");
            assertTrue(error.getMessage().endsWith("did not answer GET /v1/functions within 300 ms; the server may still have run the request"));
        }
    }

    @Test
    void anAnswerThatIsNotAJeffErrorIsAnEngineErrorQuotingIt() throws IOException {
        try (Serve server = Serve.start((exchange, body, hold) -> {
            exchange.sendResponseHeaders(502, 11);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write("Bad gateway".getBytes(StandardCharsets.UTF_8));
            }
        })) {
            assertEquals("The Jeff server at " + server.url() + " answered GET /v1/functions with status 502 and a body that is "
                + "not a Jeff error: Bad gateway", assertThrows(JeffError.EngineError.class,
                    () -> Jeff.remote(server.url(), TokenSource.none(), options(2000))).getMessage());
        }
    }

    @Test
    void theUrlTheTokenAndTheTimeoutAreChecked() {
        assertEquals("The server URL 'ftp://x' must start with http:// or https://",
            assertThrows(JeffError.InvalidRequest.class, () -> new RestTransport("ftp://x", TokenSource.none(), 1000)).getMessage());
        assertThrows(JeffError.InvalidRequest.class, () -> Jeff.remote("http://x", TokenSource.none(), options(0)));
        RestTransport transport = new RestTransport("http://127.0.0.1:9", TokenSource.from(() -> ""), 1000);
        assertEquals("The token for http://127.0.0.1:9 must be a non-empty string; the token function returned an empty string",
            assertThrows(JeffError.Unauthorised.class, transport::functions).getMessage());
        assertThrows(NullPointerException.class, () -> TokenSource.of(null));
        transport.close();
    }

    @Test
    void aTokenIsNeverPutInAnErrorMessageOrAToString() {
        JeffError.Unauthorised error = assertThrows(JeffError.Unauthorised.class,
            () -> RestTransport.readToken(TokenSource.from(() -> "s3cret\nvalue"), "http://h"));
        assertFalse(error.getMessage().contains("s3cret"), error.getMessage());
        assertFalse(TokenSource.of("s3cret").toString().contains("s3cret"));
    }

    @Test
    void theTokenFunctionIsCalledBeforeEveryRequestAndSentAsABearerToken() throws IOException {
        List<String> seen = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger count = new AtomicInteger();
        try (Serve server = Serve.start((exchange, body, hold) -> {
            seen.add(exchange.getRequestHeaders().getFirst("Authorization"));
            json(exchange, 200, LISTING, "rid");
        }); RemoteJeff jeff = Jeff.remote(server.url(), () -> "token-" + count.incrementAndGet(), options(2000))) {
            jeff.engine().installed(true);
            assertEquals(List.of("Bearer token-1", "Bearer token-2"), List.copyOf(seen));
        }
    }

    @Test
    void aWireErrorBecomesItsJeffErrorWithTheAnswersRequestId() throws IOException {
        try (Serve server = Serve.start((exchange, body, hold) -> {
            if (exchange.getRequestMethod().equals("GET")) json(exchange, 200, LISTING, "rid-1");
            else json(exchange, 401, "{\"error\": {\"type\": \"Unauthorised\", \"message\": \"The token has expired\", \"details\": {}}}", "rid-42");
        }); RemoteJeff jeff = Jeff.remote(server.url(), "t", options(2000))) {
            JeffError.Unauthorised error = assertThrows(JeffError.Unauthorised.class,
                () -> jeff.decide(List.of(new Call("spam.is_spam", "Hi", Map.of("channel", "sms"), null))));
            assertEquals("The token has expired", error.getMessage());
            assertEquals("rid-42", error.requestId());
        }
    }

    @Test
    void a402UsageLimitReachedAnswerIsRaisedAsUsageLimitReachedWithItsDetails() throws IOException {
        try (Serve server = Serve.start((exchange, body, hold) -> {
            if (exchange.getRequestMethod().equals("GET")) json(exchange, 200, LISTING, "rid-1");
            else json(exchange, 402, "{\"error\": {\"type\": \"UsageLimitReached\", \"message\": \"This account has used its "
                + "50,000,000 input tokens for the period ending 2026-12-03 10:00 UTC; calls resume then\", \"details\": "
                + "{\"account\": \"u_1\", \"used\": 50000123, \"allowance\": 50000000, \"period_end\": \"2026-12-03T10:00:00Z\"}}}",
                "rid-402");
        }); RemoteJeff jeff = Jeff.remote(server.url(), "t", options(2000))) {
            JeffError.UsageLimitReached error = assertThrows(JeffError.UsageLimitReached.class,
                () -> jeff.decide(List.of(new Call("spam.is_spam", "Hi", Map.of("channel", "sms"), null))));
            assertEquals(List.of("u_1", 50_000_123L, 50_000_000L, "2026-12-03T10:00:00Z", "rid-402"),
                List.of(error.account(), error.used(), error.allowance(), error.periodEnd(), error.requestId()));
        }
    }

    @Test
    void aShorthandNamingAFunctionOfAnotherInterfaceIsRefusedBeforeAnyRequest() throws IOException {
        AtomicInteger posts = new AtomicInteger();
        try (Serve server = Serve.start((exchange, body, hold) -> {
            if (exchange.getRequestMethod().equals("GET")) json(exchange, 200, LISTING, "rid");
            else {
                posts.incrementAndGet();
                json(exchange, 500, "\"unexpected\"", "rid");
            }
        }); RemoteJeff jeff = Jeff.remote(server.url(), TokenSource.none(), options(2000))) {
            String message = assertThrows(JeffError.InterfaceMismatch.class,
                () -> jeff.classify("x", "jeff.support.triage.urgency", CallOptions.none().context(CTX))).getMessage();
            assertTrue(message.contains("implements rate, not classify"), message);
            assertEquals(0, posts.get());
        }
    }

    @Test
    void aServerDecisionThatDoesNotMatchTheDecisionSchemaIsAContractError() throws IOException {
        Map<String, Object> bad = new java.util.LinkedHashMap<>(DECISION);
        bad.put("abstained", "no");
        try (Serve server = Serve.start((exchange, body, hold) -> {
            if (exchange.getRequestMethod().equals("GET")) json(exchange, 200, LISTING, "rid");
            else json(exchange, 200, Json.write(Map.of("decisions", List.of(bad))), "rid");
        }); RemoteJeff jeff = Jeff.remote(server.url(), TokenSource.none(), options(2000))) {
            assertThrows(JeffError.ContractError.class,
                () -> jeff.rate("x", "jeff.support.triage.urgency", CallOptions.none().context(CTX)));
        }
    }

    @Test
    void aLowConfidenceWhoseDecisionDoesNotMatchTheSchemaIsAContractErrorWithTheRequestId() throws IOException {
        try (Serve server = Serve.start((exchange, body, hold) -> {
            if (exchange.getRequestMethod().equals("GET")) json(exchange, 200, LISTING, "rid-1");
            else json(exchange, 409, "{\"error\": {\"type\": \"LowConfidence\", \"message\": \"abstained\", \"details\": "
                + "{\"decision\": {\"value\": 1}}}}", "rid-1");
        }); RemoteJeff jeff = Jeff.remote(server.url(), TokenSource.none(), options(2000))) {
            JeffError error = assertThrows(JeffError.class, () -> jeff.rate("x", "jeff.support.triage.urgency",
                CallOptions.none().context(CTX).onLowConfidence(Policy.ERROR)));
            assertInstanceOf(JeffError.ContractError.class, error);
            assertEquals("rid-1", error.requestId());
        }
    }

    @Test
    void failuresMapToPythonsErrorsConnectingIsBackendUnavailableWaitingIsDeadlineExceeded() {
        String url = "http://h";
        java.util.function.BiFunction<Http1Client.NoAnswer.Kind, Throwable, JeffError> map = (kind, cause) ->
            RestTransport.noAnswerError(new Http1Client.NoAnswer(kind, cause), url, "GET", "/v1/functions", 500);
        assertEquals("Cannot reach the Jeff server at http://h: no connection within 500 ms",
            map.apply(Http1Client.NoAnswer.Kind.CONNECT_TIMEOUT, null).getMessage());
        assertInstanceOf(JeffError.BackendUnavailable.class, map.apply(Http1Client.NoAnswer.Kind.CONNECT_TIMEOUT, null));
        String deadline = "The Jeff server at http://h did not answer GET /v1/functions within 500 ms; the server may still have run "
            + "the request";
        for (Http1Client.NoAnswer.Kind kind : List.of(Http1Client.NoAnswer.Kind.READ_TIMEOUT, Http1Client.NoAnswer.Kind.POOL_TIMEOUT)) {
            JeffError error = map.apply(kind, null);
            assertInstanceOf(JeffError.DeadlineExceeded.class, error);
            assertEquals(deadline, error.getMessage());
        }
        ConnectException refused = new ConnectException();
        refused.initCause(new ConnectException("Connection refused"));
        assertEquals("Cannot reach the Jeff server at http://h: ConnectException: Connection refused",
            map.apply(Http1Client.NoAnswer.Kind.CONNECT_FAILED, refused).getMessage());
        assertEquals("The connection to the Jeff server at http://h failed during GET /v1/functions: IOException: closed",
            map.apply(Http1Client.NoAnswer.Kind.BROKEN, new IOException("closed")).getMessage());
        assertEquals("This remote client of http://h is closed; connect again with Jeff.remote",
            map.apply(Http1Client.NoAnswer.Kind.CLOSED, null).getMessage());
    }

    @Test
    void aRedirectIsNotFollowedA302IsAnAnswerThatIsNotAJeffErrorWithinTheTimeout() throws IOException {
        List<String> paths = Collections.synchronizedList(new ArrayList<>());
        try (Serve server = Serve.start((exchange, body, hold) -> {
            paths.add(exchange.getRequestURI().getPath());
            exchange.getResponseHeaders().add("location", "/never");
            exchange.sendResponseHeaders(302, 5);
            try (OutputStream out = exchange.getResponseBody()) {
                out.write("moved".getBytes(StandardCharsets.UTF_8));
            }
        })) {
            long started = System.nanoTime();
            assertEquals("The Jeff server at " + server.url() + " answered GET /v1/functions with status 302 and a body that is "
                + "not a Jeff error: moved", assertThrows(JeffError.EngineError.class,
                    () -> Jeff.remote(server.url(), TokenSource.none(), options(200))).getMessage());
            assertTrue(millisSince(started) < 200, millisSince(started) + " ms");
            assertEquals(List.of("/v1/functions"), List.copyOf(paths));
        }
    }

    @Test
    void anAnswerThatIsNotHttpIsABrokenConnectionNotAnUnreachableServer() throws Exception {
        try (ServerSocket tcp = new ServerSocket(0, 50, InetAddress.getLoopbackAddress())) {
            Thread.ofVirtual().start(() -> {
                try (Socket socket = tcp.accept()) {
                    socket.getOutputStream().write("this is not HTTP\r\n\r\n".getBytes(StandardCharsets.UTF_8));
                } catch (IOException closed) {
                    // the test ended
                }
            });
            String url = "http://127.0.0.1:" + tcp.getLocalPort();
            JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class,
                () -> Jeff.remote(url, TokenSource.none(), options(2000)));
            assertTrue(error.getMessage().startsWith("The connection to the Jeff server at " + url + " failed during GET /v1/functions: "),
                error.getMessage());
        }
    }

    @Test
    void closingAbortsARequestInFlightWithTheClosedError() throws Exception {
        try (Serve server = Serve.start((exchange, body, hold) -> hold.run())) {
            RestTransport transport = new RestTransport(server.url(), TokenSource.none(), 5000);
            CompletableFuture<Map<String, Object>> pending = Async.run(transport::functions);
            Thread.sleep(100);
            transport.close();
            assertEquals("This remote client of " + server.url() + " is closed; connect again with Jeff.remote",
                assertThrows(JeffError.InvalidRequest.class, () -> Async.join(pending)).getMessage());
            assertEquals(0, transport.inFlightCount());
        }
    }

    /** A listed id with no dot: Python's id.rsplit(".", 1)[0] is the whole id, an adapter this catalogue does not
     * have, so connecting succeeds (no StringIndexOutOfBoundsException). */
    @Test
    void aListedFunctionIdWithoutADotIsItsOwnAdapterIdAsInPython() throws IOException {
        Map<String, Object> listing = new LinkedHashMap<>(Values.map(Json.parse(LISTING)));
        List<Object> functions = new ArrayList<>(Values.list(listing.get("functions")));
        Map<String, Object> dotless = new LinkedHashMap<>(Values.map(functions.get(0)));
        dotless.put("id", "spam");
        functions.add(dotless);
        listing.put("functions", functions);
        String body = Json.write(listing);
        try (Serve server = Serve.start((exchange, request, hold) -> json(exchange, 200, body, "rid"));
             RemoteJeff jeff = Jeff.remote(server.url(), TokenSource.none(), options(2000))) {
            assertEquals(server.url(), jeff.url());
        }
    }

    @Test
    void aClosedClientRefusesRequests() throws IOException {
        try (Serve server = Serve.start((exchange, body, hold) -> json(exchange, 200, LISTING, "rid"))) {
            RemoteJeff jeff = Jeff.remote(server.url(), TokenSource.none(), options(2000));
            jeff.close();
            assertEquals("This remote client of " + server.url() + " is closed; connect again with Jeff.remote",
                assertThrows(JeffError.InvalidRequest.class, () -> jeff.transport().functions()).getMessage());
        }
    }

    @Test
    void policiesTheServerRunsReturnErrorAndFunctionStepsLlmStepsRunHere() {
        assertEquals("return", RemoteJeff.serverPolicy(Policy.RETURN));
        assertEquals(Map.of("escalate", List.of("a.b.c"), "budget_ms", 50L),
            RemoteJeff.serverPolicy(Policy.escalate(List.of(Step.function("a.b.c")), 50L)));
        assertNull(RemoteJeff.serverPolicy(Policy.escalate(List.of(Step.function("a.b.c"), Step.llm(question -> "x")))));
    }

    @Test
    void moreCallsThanOneRequestCarriesAreRefusedNotSplit() {
        RemoteJeff.checkBatch(RemoteJeff.MAX_CALLS, "calls");
        assertEquals("257 calls in one request: a Jeff server answers at most 256 per request; send them in batches of at most 256",
            assertThrows(JeffError.InvalidRequest.class, () -> RemoteJeff.checkBatch(RemoteJeff.MAX_CALLS + 1, "calls")).getMessage());
    }

    @Test
    void aRegisteredGraphWithAnLlmNodePolicyIsRefusedBeforeAnyRequest() throws IOException {
        AtomicInteger requests = new AtomicInteger();
        try (Serve server = Serve.start((exchange, body, hold) -> {
                requests.incrementAndGet();
                json(exchange, 200, LISTING, "rid");
            });
             RemoteJeff jeff = Jeff.remote(server.url(), TokenSource.none(), options(2000))) {
            String message = assertThrows(JeffError.InvalidPolicy.class, () -> jeff.runServerGraph("g", "x", Map.of(),
                Map.of("n", Policy.escalate(List.of(Step.llm(question -> "1")))))).getMessage();
            assertTrue(message.startsWith("Node n: a graph registered on the server runs there"), message);
            assertEquals(1, requests.get(), "only the GET /v1/functions of connecting");
        }
    }
}
