package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNotSame;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import ai.jeff.sdk.testing.FakeEngine;
import java.io.IOException;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.BooleanSupplier;
import org.junit.jupiter.api.Test;

/** Jeff.remote with channel: bound functions are handles on the server's /v1/channel (port of the Node SDK's
 * channel.test.ts and the Python SDK's test_channel_client.py, against a fake channel server). */
class ChannelTest {
    private static final Map<String, Object> CTX = Fixtures.ordered("company", "A shop.", "channel", "email");

    private static String jwt(long exp) {
        Base64.Encoder encoder = Base64.getUrlEncoder().withoutPadding();
        return encoder.encodeToString("{\"alg\":\"EdDSA\"}".getBytes(StandardCharsets.UTF_8)) + "."
            + encoder.encodeToString(("{\"sub\":\"alice\",\"exp\":" + exp + "}").getBytes(StandardCharsets.UTF_8)) + ".c2ln";
    }

    private static ChannelJeff open(String url, TokenSource token, long timeoutMs) {
        return (ChannelJeff) Jeff.remote(url, token, RemoteOptions.timeoutMs(timeoutMs).withChannel(true));
    }

    private static ChannelJeff open(String url) {
        return open(url, TokenSource.of("secret-token"), 2000);
    }

    private static void pause(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException error) {
            Thread.currentThread().interrupt();
            throw new IllegalStateException(error);
        }
    }

    private static void until(BooleanSupplier condition) {
        long deadline = System.nanoTime() + 5_000_000_000L;
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) throw new AssertionError("condition not met in time");
            pause(10);
        }
    }

    private static long millisSince(long started) {
        return (System.nanoTime() - started) / 1_000_000;
    }

    private static void assertSameError(JeffError expected, Throwable actual) {
        assertInstanceOf(expected.getClass(), actual);
        assertEquals(expected.getMessage(), actual.getMessage());
        assertEquals(expected.details(), ((JeffError) actual).details());
    }

    private static Call urgency(String input) {
        return new Call("triage.urgency", input, CTX, null);
    }

    @Test
    void theChannelUrlFollowsTheServerUrlAndAJwtsExpIsReadWithoutVerifyingIt() {
        assertEquals("ws://h:1/jeff/v1/channel", ChannelTransport.channelUrl("http://h:1/jeff/"));
        assertEquals("wss://h/v1/channel", ChannelTransport.channelUrl("https://h"));
        assertEquals("The server URL 'ftp://h' must start with http:// or https://",
            assertThrows(JeffError.InvalidRequest.class, () -> ChannelTransport.channelUrl("ftp://h")).getMessage());
        assertNull(ChannelTransport.tokenExpiry("an-api-key"));
        assertEquals(1234L, ChannelTransport.tokenExpiry(jwt(1234)));
        assertEquals("The token is shaped like a JWT, but its payload is not base64-encoded JSON",
            assertThrows(JeffError.Unauthorised.class, () -> ChannelTransport.tokenExpiry("a.!!!.c")).getMessage());
        String noExp = "a." + Base64.getUrlEncoder().withoutPadding().encodeToString("{\"sub\":\"alice\"}".getBytes(StandardCharsets.UTF_8)) + ".c";
        assertEquals("The token is a JWT without a numeric exp (got None); the server needs one",
            assertThrows(JeffError.Unauthorised.class, () -> ChannelTransport.tokenExpiry(noExp)).getMessage());
    }

    @Test
    void aBoundFunctionSendsOneBindThenOnlyCallFramesAndCloseReleasesItsHandle() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url())) {
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            assertEquals(2L, bound.call("Fire").value());
            assertEquals(List.of(2L, 2L), bound.many(List.of("a", "b")).stream().map(Decision::value).toList());
            bound.close();
            assertThrows(JeffError.InvalidRequest.class, () -> bound.call("again"));
            assertEquals(List.of("hello", "auth", "bind", "call", "call_many", "release"), server.ops());
            assertEquals(Fixtures.ordered("op", "bind", "function", "jeff.support.triage.urgency", "on_low_confidence", "return",
                "context", CTX, "id", 2L), server.received().get(2));
            assertEquals(List.of("Bearer secret-token"), server.headers());
        }
    }

    @Test
    void withoutATokenThereIsNoAuthAndNoAuthorizationHeader() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults());
             ChannelJeff jeff = open(server.url(), TokenSource.none(), 2000)) {
            jeff.decide(List.of(urgency("x")));
            assertEquals(List.of("hello", "decide"), server.ops());
            assertEquals(List.of(""), server.headers());
        }
    }

    @Test
    void aTokenTheServerRefusesIsUnauthorisedAtConnectAndTheMessageNeverShowsAToken() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withToken("the-key"))) {
            JeffError.Unauthorised error = assertThrows(JeffError.Unauthorised.class,
                () -> open(server.url(), TokenSource.of("the-kez"), 2000));
            assertFalse(error.getMessage().contains("the-kez"));
            try (ChannelJeff jeff = open(server.url(), TokenSource.of("the-key"), 2000)) {
                assertEquals(2L, jeff.decide(List.of(urgency("x"))).get(0).value());
            }
        }
    }

    @Test
    void theBindCarriesThePolicyTheServerRunsOneWithAnLlmStepIsBoundAsReturnAndRunHere() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url())) {
            jeff.function("triage.urgency", CallOptions.none().context(CTX)
                .onLowConfidence(Policy.escalate(List.of(Step.function("triage.urgency")))));
            jeff.function("triage.urgency", CallOptions.none().context(CTX).onLowConfidence(Policy.escalate(List.of(Step.llm(question -> "3")))));
            List<Object> policies = server.received().stream().filter(message -> message.get("op").equals("bind"))
                .map(message -> message.get("on_low_confidence")).toList();
            assertEquals(List.of(Map.of("escalate", List.of("jeff.support.triage.urgency")), "return"), policies);
        }
    }

    @Test
    void aBindAnsweredWithAnotherAdapterVersionIsRefusedAndItsHandleReleasedBeforeTheNextRequest() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withVersion("9.9.9")); ChannelJeff jeff = open(server.url())) {
            String local = jeff.catalogue().adapter("jeff.support.triage").version();
            assertEquals("jeff.support.triage: the server serves version 9.9.9, this client's catalogue has " + local + "; pass "
                + "catalogue= with the server's manifests", assertThrows(JeffError.ContractError.class,
                    () -> jeff.function("triage.urgency", CallOptions.none().context(CTX))).getMessage());
            jeff.decide(List.of(urgency("x")));
            assertEquals(List.of("hello", "auth", "bind", "release", "decide"), server.ops());
        }
    }

    @Test
    void aQueuedReleaseIsSentBeforeTheNextRequest() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url())) {
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            jeff.channel().releaseLater(1);
            jeff.decide(List.of(urgency("x")));
            List<Object> ops = server.ops();
            assertEquals(List.of("release", "decide"), ops.subList(ops.size() - 2, ops.size()));
            assertEquals("jeff.support.triage.urgency", bound.binding().function().id());
        }
    }

    @Test
    void aFailureTheServerSendsCarriesTheConnectionsIdAsChannelId() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url())) {
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            jeff.channel().release(1);
            JeffError.InvalidRequest error = assertThrows(JeffError.InvalidRequest.class, () -> bound.call("x"));
            assertSameError(new JeffError.InvalidRequest("handle 1 is not bound", Map.of("channel_id", "0badc0de")), error);
            assertNull(error.requestId());
        }
    }

    @Test
    void aLowConfidenceWhoseDecisionDoesNotMatchTheSchemaIsAContractError() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withBadLowConfidenceDecision());
             ChannelJeff jeff = open(server.url())) {
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            assertInstanceOf(JeffError.ContractError.class, assertThrows(JeffError.class, () -> bound.call("x")));
        }
    }

    @Test
    void aFunctionTokenThatIsAJwtIsRefreshedBeforeItsExp() {
        long now = System.currentTimeMillis() / 1000;
        List<String> tokens = List.of(jwt(now + 61), jwt(now + 3600));
        AtomicInteger calls = new AtomicInteger();
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults());
             ChannelJeff jeff = open(server.url(), TokenSource.from(() -> calls.getAndIncrement() < 2 ? tokens.get(0) : tokens.get(1)), 2000)) {
            pause(1600);
            assertFalse(jeff.channel().isClosed());
            assertTrue(server.ops().contains("refresh"), server.ops().toString());
            assertEquals(tokens.get(1), server.received().stream().filter(message -> message.get("op").equals("refresh"))
                .findFirst().orElseThrow().get("token"));
        }
    }

    @Test
    void aStringTokenIsNeverRefreshedEvenAJwtInsideTheRefreshWindow() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults());
             ChannelJeff jeff = open(server.url(), TokenSource.of(jwt(System.currentTimeMillis() / 1000 + 30)), 2000)) {
            pause(300);
            assertFalse(jeff.channel().isClosed());
            assertEquals(List.of("hello", "auth"), server.ops());
        }
    }

    @Test
    void aTokenFunctionThatNeverRenewsClosesTheChannelAtExpWithUnauthorisedCarryingEveryFailure() {
        String fixed = jwt(System.currentTimeMillis() / 1000 + 2);
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults());
             ChannelJeff jeff = open(server.url(), TokenSource.from(() -> fixed), 2000)) {
            pause(3200);
            JeffError.Unauthorised error = assertThrows(JeffError.Unauthorised.class, () -> jeff.decide(List.of(urgency("x"))));
            assertTrue(error.getMessage().matches("The channel's token expired at \\d+ before the token function returned one.*"), error.getMessage());
            assertTrue(error.notes().size() >= 2, error.notes().toString());
            assertTrue(error.notes().stream().allMatch(note -> note.contains("not after the channel's current token")), error.notes().toString());
            assertFalse(server.ops().contains("refresh"));
        }
    }

    @Test
    void anUnansweredRequestIsDeadlineExceededPromptlyItsLateReplyIsDroppedAndTheChannelStaysOpen() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withSilent("decide"));
             ChannelJeff jeff = open(server.url(), TokenSource.of("secret-token"), 300)) {
            long started = System.nanoTime();
            JeffError.DeadlineExceeded error = assertThrows(JeffError.DeadlineExceeded.class, () -> jeff.decide(List.of(urgency("x"))));
            long elapsed = millisSince(started);
            assertEquals("The channel " + ChannelTransport.channelUrl(server.url()) + " did not answer 'decide' within 300 ms; the "
                + "server may still have run the request", error.getMessage());
            assertTrue(elapsed >= 290 && elapsed < 500, elapsed + " ms");
            Map<String, Object> late = server.received().stream().filter(message -> message.get("op").equals("decide")).findFirst().orElseThrow();
            server.send(Fixtures.ordered("id", late.get("id"), "ok", true, "result", Fixtures.ordered("decisions", List.of())));
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            assertEquals(2L, bound.call("Fire").value());
        }
    }

    @Test
    void aServerThatNeverAnswersTheOpeningHandshakeIsBackendUnavailablePromptly() throws IOException {
        try (ServerSocket silent = new ServerSocket(0, 50, InetAddress.getLoopbackAddress())) {
            List<Socket> accepted = new ArrayList<>();
            Thread.ofVirtual().start(() -> {
                try {
                    accepted.add(silent.accept()); // accepted and read from by nobody: the upgrade is never answered
                } catch (IOException closed) {
                    // the test ended
                }
            });
            String url = "http://127.0.0.1:" + silent.getLocalPort();
            long started = System.nanoTime();
            JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class,
                () -> open(url, TokenSource.of("secret-token"), 200));
            long elapsed = millisSince(started);
            assertEquals("Cannot reach the Jeff server at " + url + ": opening " + ChannelTransport.channelUrl(url) + " took more "
                + "than 200 ms", error.getMessage());
            assertTrue(elapsed >= 190 && elapsed < 400, elapsed + " ms");
        }
    }

    @Test
    void aServerThatSendsNoHelloIsBackendUnavailablePromptly() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withSilent("hello"))) {
            long started = System.nanoTime();
            assertEquals("No hello from the channel " + ChannelTransport.channelUrl(server.url()) + " within 200 ms",
                assertThrows(JeffError.BackendUnavailable.class, () -> open(server.url(), TokenSource.of("secret-token"), 200)).getMessage());
            assertTrue(millisSince(started) < 400, millisSince(started) + " ms");
        }
    }

    @Test
    void aServerRefusingTheWebSocketIsAnEngineErrorNamingTheHttpStatus() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults())) {
            String elsewhere = server.url() + "/elsewhere";
            assertEquals("The Jeff server at " + elsewhere + " refused to open the channel " + ChannelTransport.channelUrl(elsewhere)
                + ": HTTP status 400", assertThrows(JeffError.EngineError.class, () -> open(elsewhere)).getMessage());
        }
    }

    @Test
    void aReplyForAnIdNobodySentEndsTheChannelWithEngineError() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url())) {
            server.send(Fixtures.ordered("id", 999L, "ok", true, "result", new LinkedHashMap<>()));
            pause(50);
            assertEquals("The channel " + ChannelTransport.channelUrl(server.url()) + " sent a reply for id 999, which no request is "
                + "waiting for; the channel is closed", assertThrows(JeffError.EngineError.class,
                    () -> jeff.function("triage.urgency", CallOptions.none().context(CTX))).getMessage());
        }
    }

    @Test
    void aFailureWithIdZeroEndsTheChannelWithTheErrorItNames() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url())) {
            server.send(Fixtures.ordered("id", 0L, "ok", false, "error", Fixtures.ordered("type", "Unauthorised", "message",
                "the channel's token expired", "details", Fixtures.ordered("channel_id", "0badc0de"))));
            pause(50);
            assertSameError(new JeffError.Unauthorised("the channel's token expired", Map.of("channel_id", "0badc0de")),
                assertThrows(JeffError.Unauthorised.class, () -> jeff.decide(List.of(urgency("x")))));
        }
    }

    @Test
    void theServerGoingAwayFailsWaitingAndLaterRequestsWithBackendUnavailable() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withSilent("decide"));
             ChannelJeff jeff = open(server.url(), TokenSource.of("secret-token"), 5000)) {
            CompletableFuture<List<Decision>> waiting = jeff.decideAsync(List.of(urgency("x")));
            pause(50);
            server.dropConnections();
            JeffError.BackendUnavailable closed = new JeffError.BackendUnavailable(ChannelTransport.channelUrl(server.url())
                + ": the channel closed (no close frame received)");
            assertSameError(closed, assertThrows(JeffError.BackendUnavailable.class, () -> Async.join(waiting)));
            assertThrows(JeffError.BackendUnavailable.class, () -> jeff.function("triage.urgency", CallOptions.none().context(CTX)));
        }
    }

    @Test
    void closeEndsTheChannelWithACloseFrameAndLaterRequestsAreInvalidRequest() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults())) {
            ChannelJeff jeff = open(server.url());
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            jeff.close();
            assertEquals("This remote client of " + server.url() + " is closed; connect again with Jeff.remote",
                assertThrows(JeffError.InvalidRequest.class, () -> jeff.decide(List.of(urgency("x")))).getMessage());
            assertThrows(JeffError.InvalidRequest.class, () -> bound.call("x"));
            bound.close(); // nothing to release: the handles ended with the connection
            assertEquals(List.of("hello", "auth", "bind"), server.ops());
        }
    }

    private static Map<String, Object> withoutTiming(Decision decision) {
        Map<String, Object> json = new LinkedHashMap<>(decision.toJson());
        json.remove("timing");
        return json;
    }

    @Test
    void concurrentRequestsShareOneChannelAndEachGetsItsOwnAnswerWhateverOrderTheRepliesComeIn() {
        List<String> inputs = new ArrayList<>();
        for (int index = 0; index < 160; index++) inputs.add("message " + index + (index % 3 == 0 ? " unsure" : ""));
        Jeff local = Jeff.create(new FakeEngine(ChannelServer::answers, ChannelServer.INSTALLED), Catalogue.shipped());
        BoundFunction expectedBound = local.function("triage.urgency", CallOptions.none().context(CTX));
        Map<String, Map<String, Object>> expected = new LinkedHashMap<>();
        for (String input : inputs) expected.put(input, withoutTiming(expectedBound.call(input)));
        assertEquals(Set.of(true, false), new HashSet<>(expected.values().stream().map(decision -> decision.get("abstained")).toList()));
        // 160 calls, 2 call_many and 2 decide: every reply is held until all 164 are ready, then sent in reverse order
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withReorder(164));
             ChannelJeff jeff = open(server.url(), TokenSource.of("secret-token"), 10000)) {
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            List<CompletableFuture<Decision>> calls = new ArrayList<>();
            for (String input : inputs) calls.add(bound.callAsync(input));
            CompletableFuture<List<Decision>> manyA = bound.manyAsync(inputs.subList(0, 5));
            CompletableFuture<List<Decision>> manyB = bound.manyAsync(inputs.subList(5, 12));
            CompletableFuture<List<Decision>> decideA = jeff.decideAsync(List.of(urgency(inputs.get(12))));
            CompletableFuture<List<Decision>> decideB = jeff.decideAsync(List.of(urgency(inputs.get(13))));
            for (int index = 0; index < inputs.size(); index++) {
                assertEquals(expected.get(inputs.get(index)), withoutTiming(Async.join(calls.get(index))));
            }
            List<Decision> many = new ArrayList<>(Async.join(manyA));
            many.addAll(Async.join(manyB));
            for (int index = 0; index < 12; index++) assertEquals(expected.get(inputs.get(index)), withoutTiming(many.get(index)));
            assertEquals(expected.get(inputs.get(12)), withoutTiming(Async.join(decideA).get(0)));
            assertEquals(expected.get(inputs.get(13)), withoutTiming(Async.join(decideB).get(0)));
            assertEquals(1, server.headers().size());
            List<Object> ids = server.received().stream().filter(message -> !message.get("op").equals("hello"))
                .map(message -> message.get("id")).toList();
            assertEquals(ids.size(), new HashSet<>(ids).size());
            assertEquals(2 + 164, ids.size());
        }
    }

    @Test
    void requestsWaitingWhenTheChannelDropsEachFailOnceWithBackendUnavailableAndSoDoesEveryLaterOne() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withGated("call"));
             ChannelJeff jeff = open(server.url(), TokenSource.of("secret-token"), 5000)) {
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            List<CompletableFuture<Decision>> waiting = new ArrayList<>();
            for (int index = 0; index < 4; index++) waiting.add(bound.callAsync("pending " + index));
            until(() -> server.waiting() == 4);
            server.dropConnections();
            JeffError.BackendUnavailable closed = new JeffError.BackendUnavailable(ChannelTransport.channelUrl(server.url())
                + ": the channel closed (no close frame received)");
            List<Throwable> errors = new ArrayList<>();
            for (CompletableFuture<Decision> call : waiting) errors.add(assertThrows(JeffError.BackendUnavailable.class, () -> Async.join(call)));
            for (Throwable error : errors) assertSameError(closed, error);
            for (int index = 1; index < errors.size(); index++) assertNotSame(errors.get(0), errors.get(index));
            assertSameError(closed, assertThrows(JeffError.BackendUnavailable.class, () -> bound.call("later")));
            server.openGate();
        }
    }

    @Test
    void the257thOutstandingRequestIsRateLimitedAndTheChannelStaysOpen() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withGated("call"));
             ChannelJeff jeff = open(server.url(), TokenSource.of("secret-token"), 10000)) {
            BoundFunction bound = jeff.function("triage.urgency", CallOptions.none().context(CTX));
            List<CompletableFuture<Decision>> results = new ArrayList<>();
            for (int index = 0; index < 257; index++) results.add(bound.callAsync("call " + index));
            until(() -> server.waiting() == 256);
            until(() -> results.stream().anyMatch(CompletableFuture::isCompletedExceptionally));
            server.openGate();
            List<JeffError.RateLimited> refused = new ArrayList<>();
            List<Object> values = new ArrayList<>();
            for (CompletableFuture<Decision> result : results) {
                try {
                    values.add(Async.join(result).value());
                } catch (JeffError.RateLimited limited) {
                    refused.add(limited);
                }
            }
            assertEquals(1, refused.size());
            assertEquals(1.0, refused.get(0).retryAfterS());
            assertEquals("0badc0de", refused.get(0).details().get("channel_id"));
            assertEquals(256, values.size());
            assertTrue(values.stream().allMatch(value -> value.equals(2L)));
            assertFalse(jeff.channel().isClosed());
            assertEquals(2L, bound.call("after").value());
        }
    }
}
