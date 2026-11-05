package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertFalse;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Base64;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicReference;
import java.util.function.BooleanSupplier;
import org.junit.jupiter.api.Test;

/** The channel's wire: its timing phases (the turn to write, the upload's stall watch, the reply), what a stuck send,
 * an interrupt or a cancelled caller leave behind, the server's frame limits, malformed or hostile frames, keepalive,
 * proxies and wss://. Against the fake ChannelServer. */
class ChannelWireTest {
    private static final Map<String, Object> CTX = Fixtures.ordered("company", "A shop.", "channel", "email");

    private static ChannelJeff open(String url, long timeoutMs) {
        return (ChannelJeff) Jeff.remote(url, TokenSource.of("secret-token"), RemoteOptions.timeoutMs(timeoutMs).withChannel(true));
    }

    private static ChannelJeff open(String url, long timeoutMs, HttpOptions options, long keepaliveMs) {
        return ChannelJeff.connect(url, TokenSource.of("secret-token"), timeoutMs, Catalogue.shipped(), options, keepaliveMs);
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
        long deadline = System.nanoTime() + 10_000_000_000L;
        while (!condition.getAsBoolean()) {
            if (System.nanoTime() > deadline) throw new AssertionError("condition not met in time");
            pause(10);
        }
    }

    private static long millisSince(long started) {
        return (System.nanoTime() - started) / 1_000_000;
    }

    private static BoundFunction urgency(ChannelJeff jeff) {
        return jeff.function("triage.urgency", CallOptions.none().context(CTX));
    }

    private static List<Object> inputs(ChannelServer server) {
        List<Object> inputs = new ArrayList<>();
        for (Map<String, Object> message : server.received()) if (message.containsKey("input")) inputs.add(message.get("input"));
        return inputs;
    }

    @Test
    void aMessageOnASlowButSteadyLinkTakesAsLongAsItNeedsAndItsReplyIsTimedFromWhenItWasSent() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withBytesPerSecond(4L << 20));
             ChannelJeff jeff = open(server.url(), 400)) {
            BoundFunction bound = urgency(jeff);
            long started = System.nanoTime();
            assertEquals(2L, bound.call("x".repeat(3 << 20)).value());
            assertTrue(millisSince(started) > 400, millisSince(started) + " ms");
        }
    }

    @Test
    void aRequestBehindASlowUploadWaitsAtMostTimeoutForItsTurnAndIsThenNeverSent() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withBytesPerSecond(2L << 20));
             ChannelJeff jeff = open(server.url(), 1000)) {
            BoundFunction bound = urgency(jeff);
            String big = "y".repeat(5 << 20);
            CompletableFuture<Decision> slow = bound.callAsync(big);
            pause(50);
            long started = System.nanoTime();
            JeffError.DeadlineExceeded error = assertThrows(JeffError.DeadlineExceeded.class, () -> bound.call("small"));
            assertEquals("The channel " + ChannelTransport.channelUrl(server.url()) + " could not start sending 'call' within 1000 ms, "
                + "as earlier messages were still being written; it was not sent", error.getMessage());
            assertTrue(millisSince(started) >= 990 && millisSince(started) < 1500, millisSince(started) + " ms");
            assertEquals(2L, Async.join(slow).value());
            assertEquals(2L, bound.call("after").value());
            assertEquals(List.of(big, "after"), inputs(server));
            assertFalse(jeff.channel().isClosed());
        }
    }

    @Test
    void aSendTheServerStopsTakingEndsTheChannelWithBackendUnavailableNamingTheStall() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withStopReadingAfter("bind"));
             ChannelJeff jeff = open(server.url(), 300)) {
            BoundFunction bound = urgency(jeff);
            long started = System.nanoTime();
            JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class, () -> bound.call("z".repeat(20 << 20)));
            String expected = ChannelTransport.channelUrl(server.url()) + ": the socket accepted no bytes of a frame for 300 ms (the "
                + "server or the network stopped taking data); the channel closed";
            assertEquals(expected, error.getMessage());
            assertTrue(millisSince(started) < 3000, millisSince(started) + " ms");
            assertEquals(expected, assertThrows(JeffError.BackendUnavailable.class, () -> bound.call("later")).getMessage());
        }
    }

    @Test
    void closeEndsASendStuckOnAServerThatStoppedReadingWithinTheTimeout() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withStopReadingAfter("bind"))) {
            ChannelJeff jeff = open(server.url(), 1000);
            BoundFunction bound = urgency(jeff);
            CompletableFuture<Decision> stuck = bound.callAsync("z".repeat(20 << 20));
            CompletableFuture<Decision> queued = bound.callAsync("behind");
            pause(200);
            long started = System.nanoTime();
            jeff.close();
            assertTrue(millisSince(started) < 1600, millisSince(started) + " ms");
            String closed = "This remote client of " + server.url() + " is closed; connect again with Jeff.remote";
            assertEquals(closed, assertThrows(JeffError.InvalidRequest.class, () -> Async.join(stuck)).getMessage());
            assertEquals(closed, assertThrows(JeffError.InvalidRequest.class, () -> Async.join(queued)).getMessage());
        }
    }

    @Test
    void anInterruptedCallerNeverLeavesHalfAMessageAndTheChannelGoesOn() throws InterruptedException {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withBytesPerSecond(2L << 20));
             ChannelJeff jeff = open(server.url(), 3000)) {
            BoundFunction bound = urgency(jeff);
            String big = "w".repeat(3 << 19);
            AtomicReference<Throwable> failure = new AtomicReference<>();
            Thread caller = Thread.ofPlatform().start(() -> {
                try {
                    bound.call(big);
                } catch (Throwable error) {
                    failure.set(error);
                }
            });
            pause(100);
            caller.interrupt();
            caller.join();
            assertInstanceOf(JeffError.BackendUnavailable.class, failure.get());
            assertEquals("Interrupted while waiting for " + ChannelTransport.channelUrl(server.url()) + " to answer 'call'",
                failure.get().getMessage());
            assertEquals(2L, bound.call("after").value()); // the big message was written whole before it
            assertEquals(List.of(big, "after"), inputs(server));
            assertFalse(jeff.channel().isClosed());
        }
    }

    @Test
    void aCancelledCallersLateReplyIsDroppedAndTheChannelStaysOpen() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withGated("call"));
             ChannelJeff jeff = open(server.url(), 5000)) {
            BoundFunction bound = urgency(jeff);
            List<CompletableFuture<Decision>> cancelled = new ArrayList<>();
            for (int index = 0; index < 20; index++) cancelled.add(bound.callAsync("cancelled " + index));
            until(() -> server.waiting() == 20);
            for (CompletableFuture<Decision> call : cancelled) call.cancel(true);
            server.openGate();
            assertEquals(2L, bound.call("next").value());
            pause(100);
            assertFalse(jeff.channel().isClosed());
            assertEquals(2L, bound.call("again").value());
        }
    }

    @Test
    void framesOverTheServersLimitsAreRefusedBeforeAnythingIsSent() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults())) {
            String large = "t".repeat(70_000);
            JeffError.InvalidRequest auth = assertThrows(JeffError.InvalidRequest.class,
                () -> Jeff.remote(server.url(), TokenSource.of(large), RemoteOptions.timeoutMs(2000).withChannel(true)));
            assertTrue(auth.getMessage().matches("A 'auth' frame of \\d+ bytes is over the channel's limit of 65536 bytes before auth"),
                auth.getMessage());
            assertFalse(auth.getMessage().contains(large));
            assertEquals(List.of("hello"), server.ops());
            try (ChannelJeff jeff = open(server.url(), 5000)) {
                BoundFunction bound = urgency(jeff);
                JeffError.InvalidRequest call = assertThrows(JeffError.InvalidRequest.class, () -> bound.call("x".repeat(50 << 20)));
                assertTrue(call.getMessage().matches("A 'call' frame of \\d+ bytes is over the channel's limit of 52428800 bytes \\(50 MiB\\)"),
                    call.getMessage());
                assertEquals(2L, bound.call("small").value());
                assertEquals(List.of("small"), inputs(server));
            }
        }
    }

    /** A server-to-client WebSocket frame: `first` (FIN, opcode) and an unmasked payload. */
    private static byte[] wsFrame(int first, byte[] payload) {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        out.write(first);
        if (payload.length < 126) {
            out.write(payload.length);
        } else if (payload.length < 65536) {
            out.write(126);
            out.write(payload.length >>> 8);
            out.write(payload.length);
        } else {
            out.write(127);
            for (int shift = 56; shift >= 0; shift -= 8) out.write((int) ((long) payload.length >>> shift));
        }
        out.writeBytes(payload);
        return out.toByteArray();
    }

    private static byte[] framed(byte[] body) {
        byte[] frame = new byte[4 + body.length];
        frame[0] = (byte) (body.length >>> 24);
        frame[1] = (byte) (body.length >>> 16);
        frame[2] = (byte) (body.length >>> 8);
        frame[3] = (byte) body.length;
        System.arraycopy(body, 0, frame, 4, body.length);
        return frame;
    }

    private record Hostile(String what, byte[] bytes, Class<? extends JeffError> type, String message) {}

    @Test
    void malformedOrHostileFramesEndTheChannelWithANamedErrorAndNeverCrashIt() {
        String ws = "<ws>";
        byte[] deep = new byte[200_001];
        java.util.Arrays.fill(deep, (byte) 0x91);
        deep[200_000] = (byte) 0xc0;
        byte[] oversize = {(byte) 0x82, 127, 0, 0, 0, 0, 4, 0, 0, 5};
        byte[] masked = {(byte) 0x82, (byte) 0x81, 1, 2, 3, 4, 5};
        List<Hostile> cases = List.of(
            new Hostile("text", wsFrame(0x81, "hi".getBytes(StandardCharsets.UTF_8)), JeffError.EngineError.class,
                "The channel <ws> sent a text message; it carries binary frames only"),
            new Hostile("masked", masked, JeffError.EngineError.class, "The channel <ws> sent a masked frame (a server's frames are "
                + "never masked), which breaks the WebSocket protocol (RFC 6455); the channel is closed"),
            new Hostile("reserved bits", wsFrame(0xc2, new byte[0]), JeffError.EngineError.class, "The channel <ws> sent a frame with "
                + "reserved bits set (no extension was agreed), which breaks the WebSocket protocol (RFC 6455); the channel is closed"),
            new Hostile("unknown opcode", wsFrame(0x83, new byte[0]), JeffError.EngineError.class, "The channel <ws> sent a frame with the "
                + "unknown opcode 3, which breaks the WebSocket protocol (RFC 6455); the channel is closed"),
            new Hostile("stray continuation", wsFrame(0x80, new byte[0]), JeffError.EngineError.class, "The channel <ws> sent a "
                + "continuation frame with no message to continue, which breaks the WebSocket protocol (RFC 6455); the channel is closed"),
            new Hostile("over 64 MiB", oversize, JeffError.EngineError.class,
                "The channel <ws> sent a message of more than 67108868 bytes, over the 64 MiB frame limit"),
            new Hostile("short", wsFrame(0x82, new byte[] {1, 2}), JeffError.EngineError.class,
                "The channel <ws> sent a message of 2 bytes, shorter than a frame header"),
            new Hostile("not one frame", wsFrame(0x82, new byte[] {0, 0, 0, 10, 1, 2, 3}), JeffError.EngineError.class,
                "The channel <ws> sent a message that is not exactly one frame: its header announces 10 bytes and the message carries 3"),
            new Hostile("not msgpack", wsFrame(0x82, framed(new byte[] {(byte) 0xc1})), JeffError.EngineError.class,
                "The channel <ws> sent a frame that does not match the socket schema: Socket frame is not valid msgpack: "),
            new Hostile("200000 lists deep", wsFrame(0x82, framed(deep)), JeffError.EngineError.class,
                "The channel <ws> sent a frame that does not match the socket schema: Socket frame is not valid msgpack: "),
            new Hostile("not a reply", wsFrame(0x82, EngineServer.frame(Fixtures.ordered("op", "hello", "version", 2L))),
                JeffError.EngineError.class, "The channel <ws> sent a message that is not a reply (op 'hello'); the channel is closed"),
            new Hostile("close 4000", wsFrame(0x88, new byte[] {0x0f, (byte) 0xa0, 'b', 'y', 'e'}), JeffError.BackendUnavailable.class,
                "<ws>: the channel closed (WebSocket close code 4000: bye)"));
        for (Hostile hostile : cases) {
            try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url(), 2000)) {
                String message = hostile.message().replace(ws, ChannelTransport.channelUrl(server.url()));
                server.sendRaw(hostile.bytes());
                until(() -> jeff.channel().isClosed());
                JeffError error = assertThrows(JeffError.class, () -> jeff.decide(List.of(new Call("triage.urgency", "x", CTX, null))),
                    hostile.what());
                assertInstanceOf(hostile.type(), error, hostile.what());
                assertTrue(error.getMessage().startsWith(message), hostile.what() + ": " + error.getMessage());
                assertThrows(hostile.type(), () -> urgency(jeff), hostile.what());
            }
        }
    }

    @Test
    void aServerThatStopsAnsweringKeepalivePingsEndsTheChannelAndOneThatAnswersKeepsIt() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withIgnoredPings());
             ChannelJeff jeff = open(server.url(), 2000, HttpOptions.standard(), 150)) {
            until(() -> jeff.channel().isClosed());
            assertEquals(ChannelTransport.channelUrl(server.url()) + ": the server did not answer a keepalive ping within 150 ms; the "
                + "channel closed", assertThrows(JeffError.BackendUnavailable.class, () -> urgency(jeff)).getMessage());
        }
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults());
             ChannelJeff jeff = open(server.url(), 2000, HttpOptions.standard(), 100)) {
            until(() -> server.pings() >= 4);
            assertFalse(jeff.channel().isClosed());
            assertEquals(2L, urgency(jeff).call("x").value());
        }
    }

    @Test
    void theChannelsProxyIsChosenAsWebsocketsChoosesIt() {
        Map<String, String> env = new LinkedHashMap<>();
        // ALL_PROXY is not read; for ws:// WS_PROXY wins over HTTPS_PROXY and HTTP_PROXY
        env.put("ALL_PROXY", "http://all:1");
        assertNull(Proxies.forChannel("h", 80, false, env));
        env.put("HTTP_PROXY", "http://plain:2");
        assertEquals("http://plain:2|", Proxies.forChannel("h", 80, false, env).described());
        assertNull(Proxies.forChannel("h", 443, true, env), "HTTP_PROXY is not read for wss://");
        env.put("HTTPS_PROXY", "http://secure:3");
        assertEquals("http://secure:3|", Proxies.forChannel("h", 80, false, env).described());
        assertEquals("http://secure:3|", Proxies.forChannel("h", 443, true, env).described());
        env.put("ws_proxy", "https://U%20s:p%3Aw@WS:4/");
        assertEquals("https://ws:4|Proxy-Authorization=Basic " + Base64.getEncoder().encodeToString("U s:p:w".getBytes(StandardCharsets.UTF_8)),
            Proxies.forChannel("h", 80, false, env).described());
        assertEquals("http://secure:3|", Proxies.forChannel("h", 443, true, env).described());
        env.put("WSS_PROXY", "http://wss:5");
        assertEquals("http://wss:5|", Proxies.forChannel("h", 443, true, env).described());
        // NO_PROXY as urllib's proxy_bypass_environment: suffixes, leading dots, host:port, "*"
        for (String bypass : List.of("h", ".h", "x,  h ", "h:443", "*")) {
            env.put("NO_PROXY", bypass);
            assertNull(Proxies.forChannel("h", 443, true, env), bypass);
        }
        env.put("NO_PROXY", "example.com");
        assertNull(Proxies.forChannel("api.example.com", 443, true, env));
        assertEquals("http://wss:5|", Proxies.forChannel("myexample.com", 443, true, env).described());
        env.put("NO_PROXY", "h:80");
        assertEquals("http://wss:5|", Proxies.forChannel("h", 443, true, env).described());
        // SOCKS_PROXY comes before HTTPS_PROXY and is refused by name
        Map<String, String> socks = Map.of("SOCKS_PROXY", "http://user:secret@s:1080", "HTTPS_PROXY", "http://secure:3");
        JeffError.InvalidRequest refused = assertThrows(JeffError.InvalidRequest.class, () -> Proxies.forChannel("h", 443, true, socks));
        assertEquals("The proxy URL 'socks5h://user:[secure]@s:1080' from the environment cannot be used for the channel: it is a "
            + "SOCKS proxy, which this SDK does not support; use an HTTP proxy, or add the server's host to NO_PROXY", refused.getMessage());
        for (String[] bad : new String[][] {
            {"http://p:1/path", "path is meaningless"}, {"http://p:1?q=1", "query is meaningless"}, {"http://p:1#f", "fragment is meaningless"},
            {"http://user@p:1", "username provided without password"}, {"p:3128", "scheme p isn't supported"},
            {"ftp://p:1", "scheme ftp isn't supported"}, {"http://", "hostname isn't provided"}, {"http://p:99999", "has an invalid port '99999'"}}) {
            assertEquals("The proxy URL " + PyFormat.str(bad[0]) + " from the environment cannot be used for the channel: " + bad[1],
                assertThrows(JeffError.InvalidRequest.class, () -> Proxies.forChannel("h", 80, false, Map.of("ws_proxy", bad[0]))).getMessage());
        }
    }

    @Test
    void aWsChannelGoesThroughTheProxysConnectTunnelWithItsCredentialsAndNoProxyBypassesIt() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); TestProxy proxy = new TestProxy(null)) {
            int port = Integer.parseInt(server.url().substring(server.url().lastIndexOf(':') + 1));
            HttpOptions through = HttpOptions.standard().withEnvironment(Map.of("http_proxy", "http://al%20ice:pw@127.0.0.1:" + proxy.port()));
            try (ChannelJeff jeff = open(server.url(), 2000, through, ChannelTransport.KEEPALIVE_MS)) {
                assertEquals(2L, urgency(jeff).call("x").value());
            }
            RawHttpServer.Request connect = proxy.seen.stream().filter(request -> request.line().startsWith("CONNECT")).findFirst().orElseThrow();
            assertEquals("CONNECT 127.0.0.1:" + port + " HTTP/1.1", connect.line());
            assertEquals(List.of("Host: 127.0.0.1:" + port, "User-Agent: jeff-sdk-java", "Proxy-Authorization: Basic "
                + Base64.getEncoder().encodeToString("al ice:pw".getBytes(StandardCharsets.UTF_8))), connect.headers());
            assertTrue(proxy.seen.stream().anyMatch(request -> request.line().startsWith("GET http://127.0.0.1:" + port + "/v1/functions")),
                "GET /v1/functions goes through the proxy httpx chooses");
            int seen = proxy.seen.size();
            HttpOptions bypass = HttpOptions.standard().withEnvironment(Map.of("http_proxy", "http://127.0.0.1:" + proxy.port(), "no_proxy", "127.0.0.1"));
            try (ChannelJeff jeff = open(server.url(), 2000, bypass, ChannelTransport.KEEPALIVE_MS)) {
                assertEquals(2L, urgency(jeff).call("x").value());
            }
            assertEquals(seen, proxy.seen.size());
        }
    }

    @Test
    void aProxyThatRefusesTheTunnelOrCannotBeReachedIsBackendUnavailableAsPythonReportsIt() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults());
             TestProxy proxy = new TestProxy("407 Proxy Authentication Required")) {
            String ws = ChannelTransport.channelUrl(server.url());
            HttpOptions refused = HttpOptions.standard().withEnvironment(Map.of("ws_proxy", "http://127.0.0.1:" + proxy.port()));
            assertEquals("Cannot reach the Jeff server at " + server.url() + " (" + ws + "): InvalidProxyStatus: proxy rejected connection: "
                + "HTTP 407", assertThrows(JeffError.BackendUnavailable.class,
                    () -> open(server.url(), 2000, refused, ChannelTransport.KEEPALIVE_MS)).getMessage());
            HttpOptions nowhere = HttpOptions.standard().withEnvironment(Map.of("ws_proxy", "http://127.0.0.1:9"));
            assertTrue(assertThrows(JeffError.BackendUnavailable.class, () -> open(server.url(), 2000, nowhere, ChannelTransport.KEEPALIVE_MS))
                .getMessage().startsWith("Cannot reach the Jeff server at " + server.url() + " (" + ws + "): ConnectException: "));
        }
    }

    @Test
    void aWssChannelChecksTheServersCertificateDirectlyAndThroughATunnel() {
        TestTls tls = TestTls.create("dns:localhost");
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults(), tls.server()); TestProxy proxy = new TestProxy(null)) {
            // each transport gets HttpOptions of its own: closing a transport closes its connection pool
            HttpOptions trusting = HttpOptions.standard().withTls(() -> tls.client().getSocketFactory());
            try (ChannelJeff jeff = open(server.url(), 3000, trusting, ChannelTransport.KEEPALIVE_MS)) {
                assertTrue(jeff.channel().wsUrl().startsWith("wss://localhost:"));
                assertEquals(2L, urgency(jeff).call("x").value());
            }
            JeffError.BackendUnavailable untrusted = assertThrows(JeffError.BackendUnavailable.class,
                () -> open(server.url(), 3000, HttpOptions.standard(), ChannelTransport.KEEPALIVE_MS));
            assertTrue(untrusted.getMessage().startsWith("Cannot reach the Jeff server at " + server.url() + " ("
                + ChannelTransport.channelUrl(server.url()) + "): "), untrusted.getMessage());
            HttpOptions tunnelled = HttpOptions.standard().withTls(() -> tls.client().getSocketFactory()).withEnvironment(Map.of("HTTPS_PROXY", "http://127.0.0.1:" + proxy.port()));
            try (ChannelJeff jeff = open(server.url(), 3000, tunnelled, ChannelTransport.KEEPALIVE_MS)) {
                assertEquals(2L, urgency(jeff).call("x").value());
            }
            String target = server.url().substring("https://".length());
            assertTrue(proxy.seen.stream().anyMatch(request -> request.line().equals("CONNECT " + target + " HTTP/1.1")), proxy.seen.toString());
        }
    }

    @Test
    void aPingOrPongBetweenTheFragmentsOfAReplyIsHandledAndTheReplyKept() {
        for (String control : List.of("ping", "pong")) {
            try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withInterleaved(control));
                 ChannelJeff jeff = open(server.url(), 2000)) {
                BoundFunction bound = urgency(jeff);
                assertEquals(2L, bound.call("x").value(), control);
                assertEquals(List.of(2L, 2L), bound.many(List.of("a", "b")).stream().map(Decision::value).toList(), control);
                assertFalse(jeff.channel().isClosed(), control);
                if (control.equals("ping")) until(() -> server.pongs() >= 3); // auth, bind, call, call_many: each ping answered
            }
        }
    }

    @Test
    void aCloseBetweenTheFragmentsOfAReplyIsAProtocolErrorAnsweredWith1002AsWebsocketsAnswersIt() {
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults().withInterleaved("close"))) {
            JeffError.EngineError error = assertThrows(JeffError.EngineError.class, () -> open(server.url(), 2000));
            assertEquals("The channel " + ChannelTransport.channelUrl(server.url()) + " sent a close frame in the middle of a fragmented "
                + "message, which breaks the WebSocket protocol (RFC 6455); the channel is closed", error.getMessage());
            until(() -> !server.closes().isEmpty());
            assertEquals("03ea", java.util.HexFormat.of().formatHex(server.closes().get(0)));
        }
    }

    @Test
    void aServersCloseFrameIsCheckedAsWebsocketsChecksItAndOnlyAValidOneIsEchoed() {
        byte[] badUtf8 = {0x0f, (byte) 0xa0, (byte) 0xc3, 0x28};
        // payload, expected error message after "<ws> ", expected payload this side answers with
        List<Object[]> cases = List.of(
            new Object[] {new byte[] {0x03}, "sent a close frame of 1 byte (too short for a status code)", "03ea"},
            new Object[] {new byte[] {0x03, (byte) 0xed}, "sent a close frame with the invalid status code 1005", "03ea"},
            new Object[] {new byte[] {0x03, (byte) 0xee}, "sent a close frame with the invalid status code 1006", "03ea"},
            new Object[] {new byte[] {0x03, (byte) 0xe7}, "sent a close frame with the invalid status code 999", "03ea"},
            new Object[] {new byte[] {0x13, (byte) 0x88}, "sent a close frame with the invalid status code 5000", "03ea"},
            new Object[] {new byte[] {0x03, (byte) 0xec}, "sent a close frame with the invalid status code 1004", "03ea"},
            new Object[] {badUtf8, "sent a close frame whose reason is not valid UTF-8", "03ef"});
        for (Object[] each : cases) {
            byte[] payload = (byte[]) each[0];
            try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url(), 2000)) {
                String ws = ChannelTransport.channelUrl(server.url());
                server.sendRaw(wsFrame(0x88, payload));
                until(() -> jeff.channel().isClosed());
                JeffError.EngineError error = assertThrows(JeffError.EngineError.class, () -> urgency(jeff));
                assertEquals("The channel " + ws + " " + each[1] + ", which breaks the WebSocket protocol (RFC 6455); the channel is "
                    + "closed", error.getMessage());
                until(() -> !server.closes().isEmpty());
                assertEquals(each[2], java.util.HexFormat.of().formatHex(server.closes().get(0)), (String) each[1]);
            }
        }
        for (int code : List.of(1000, 1001, 1003, 1007, 1011, 1012, 1013, 1014, 3000, 4999)) {
            try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url(), 2000)) {
                byte[] payload = {(byte) (code >>> 8), (byte) code, 'o', 'k'};
                server.sendRaw(wsFrame(0x88, payload));
                until(() -> jeff.channel().isClosed());
                assertEquals(ChannelTransport.channelUrl(server.url()) + ": the channel closed (WebSocket close code " + code + ": ok)",
                    assertThrows(JeffError.BackendUnavailable.class, () -> urgency(jeff)).getMessage());
                until(() -> !server.closes().isEmpty());
                assertEquals(java.util.HexFormat.of().formatHex(payload), java.util.HexFormat.of().formatHex(server.closes().get(0)));
            }
        }
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults()); ChannelJeff jeff = open(server.url(), 2000)) {
            server.sendRaw(wsFrame(0x88, new byte[0]));
            until(() -> jeff.channel().isClosed());
            assertEquals(ChannelTransport.channelUrl(server.url()) + ": the channel closed (WebSocket close code 1005)",
                assertThrows(JeffError.BackendUnavailable.class, () -> urgency(jeff)).getMessage());
            until(() -> !server.closes().isEmpty());
            assertEquals(0, server.closes().get(0).length, "an empty close is echoed empty, never as code 1005");
        }
        assertFalse(WebSocketConnection.validCloseCode(1004));
        assertFalse(WebSocketConnection.validCloseCode(1015));
        assertFalse(WebSocketConnection.validCloseCode(2999));
    }

    @Test
    void aCertificateForAnotherHostOrOneNobodyTrustsIsRefusedByNameDirectlyAndThroughATunnel() {
        TestTls elsewhere = TestTls.create("dns:other.example");
        TestTls trusted = TestTls.create("dns:localhost");
        try (ChannelServer wrongHost = ChannelServer.start(ChannelServer.Options.defaults(), elsewhere.server());
             ChannelServer untrusted = ChannelServer.start(ChannelServer.Options.defaults(), trusted.server());
             TestProxy proxy = new TestProxy(null)) {
            for (boolean tunnel : List.of(false, true)) {
                Map<String, String> env = tunnel ? Map.of("WSS_PROXY", "http://127.0.0.1:" + proxy.port()) : Map.of();
                HttpOptions trustingOther = HttpOptions.standard().withTls(() -> elsewhere.client().getSocketFactory()).withEnvironment(env);
                String wrong = assertThrows(JeffError.BackendUnavailable.class,
                    () -> open(wrongHost.url(), 3000, trustingOther, ChannelTransport.KEEPALIVE_MS)).getMessage();
                assertTrue(wrong.contains("CertificateException: No subject alternative DNS name matching localhost found"), wrong);
                HttpOptions defaults = HttpOptions.standard().withEnvironment(env);
                String unknown = assertThrows(JeffError.BackendUnavailable.class,
                    () -> open(untrusted.url(), 3000, defaults, ChannelTransport.KEEPALIVE_MS)).getMessage();
                assertTrue(unknown.contains("SunCertPathBuilderException: unable to find valid certification path"), unknown);
            }
            assertTrue(proxy.seen.stream().anyMatch(request -> request.line().startsWith("CONNECT localhost:")), "the tunnel was used");
        }
    }

    private static String jwt(String payload) {
        Base64.Encoder encoder = Base64.getUrlEncoder().withoutPadding();
        return encoder.encodeToString("{\"alg\":\"EdDSA\"}".getBytes(StandardCharsets.UTF_8)) + "."
            + encoder.encodeToString(payload.getBytes(StandardCharsets.UTF_8)) + ".c2ln";
    }

    @Test
    void aJwtsExpIsReadAsPythonReadsItAndAnExpTooFarAheadNeverOverflowsTheRefresher() {
        assertEquals(12.5, ChannelTransport.tokenExpiry(jwt("{\"exp\": 12.5}")));
        // Python's urlsafe_b64decode is not strict: padding may be written, other characters are skipped
        String padded = "a." + Base64.getUrlEncoder().encodeToString("{\"exp\": 7}".getBytes(StandardCharsets.UTF_8)) + ".c";
        assertEquals(7L, ChannelTransport.tokenExpiry(padded));
        for (String payload : List.of("{\"exp\": \"soon\"}", "{\"exp\": true}", "[1]", "{\"exp\": 1e400}")) {
            assertThrows(JeffError.Unauthorised.class, () -> ChannelTransport.tokenExpiry(jwt(payload)), payload);
        }
        assertEquals("The token is a JWT without a numeric exp (got inf); the server needs one",
            assertThrows(JeffError.Unauthorised.class, () -> ChannelTransport.tokenExpiry(jwt("{\"exp\": 1e400}"))).getMessage());
        String farAhead = jwt("{\"exp\": 1000000000000000000000000000000}");
        assertEquals(new java.math.BigInteger("1000000000000000000000000000000"), ChannelTransport.tokenExpiry(farAhead));
        try (ChannelServer server = ChannelServer.start(ChannelServer.Options.defaults());
             ChannelJeff jeff = (ChannelJeff) Jeff.remote(server.url(), TokenSource.from(() -> farAhead),
                 RemoteOptions.timeoutMs(2000).withChannel(true))) {
            pause(200);
            assertFalse(jeff.channel().isClosed());
            assertEquals(List.of("hello", "auth"), server.ops());
        }
    }
}
