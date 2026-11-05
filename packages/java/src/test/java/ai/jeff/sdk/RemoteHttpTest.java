package ai.jeff.sdk;

import static org.junit.jupiter.api.Assertions.assertEquals;
import static org.junit.jupiter.api.Assertions.assertInstanceOf;
import static org.junit.jupiter.api.Assertions.assertNull;
import static org.junit.jupiter.api.Assertions.assertThrows;
import static org.junit.jupiter.api.Assertions.assertTrue;

import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.io.InputStream;
import java.io.OutputStream;
import java.net.InetAddress;
import java.net.ServerSocket;
import java.net.Socket;
import java.nio.charset.StandardCharsets;
import java.util.ArrayList;
import java.util.Collections;
import java.util.List;
import java.util.Map;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Function;
import java.util.zip.Deflater;
import java.util.zip.GZIPOutputStream;
import org.junit.jupiter.api.BeforeAll;
import org.junit.jupiter.api.Test;

/** The HTTP behaviour of RestTransport, against raw test servers: the phases timed on their own, the pool's limits
 * and order, nothing sent twice, answers read from their bytes as httpx and json.loads read them. */
class RemoteHttpTest {
    private static final String LISTING = (String) Values.map(Values.map(Values.list(Values.map(
        Fixtures.cases("remote.json").get(0)).get("exchanges")).get(0)).get("response")).get("body");
    private static final byte[] LISTING_BYTES = LISTING.getBytes(StandardCharsets.UTF_8);
    private static final List<String> JSON_TYPE = List.of("Content-Type: application/json");

    /** A server that answers every request on every connection (kept alive) with what `answer` gives. */
    private static RawHttpServer serving(Function<RawHttpServer.Request, byte[]> answer) {
        RawHttpServer[] self = new RawHttpServer[1];
        self[0] = new RawHttpServer((socket, index) -> {
            InputStream in = socket.getInputStream();
            OutputStream out = socket.getOutputStream();
            while (true) {
                RawHttpServer.Request request = self[0].readRequest(in);
                if (request == null) return;
                out.write(answer.apply(request));
                out.flush();
            }
        });
        return self[0];
    }

    private static byte[] listing() {
        return RawHttpServer.answer(200, JSON_TYPE, LISTING_BYTES);
    }

    /** The standard options, with no proxy whatever this machine's environment says. */
    static HttpOptions direct() {
        return HttpOptions.standard().withEnvironment(Map.of());
    }

    private static RestTransport transport(String url, long timeoutMs) {
        return new RestTransport(url, TokenSource.none(), timeoutMs, direct());
    }

    private static RestTransport transport(String url, long timeoutMs, ConnectionPool pool, HttpConnection.Opener opener) {
        return new RestTransport(url, TokenSource.none(), timeoutMs, direct().withPool(pool).withOpener(opener));
    }

    private static long millisSince(long started) {
        return (System.nanoTime() - started) / 1_000_000;
    }

    private static void sleep(long ms) {
        try {
            Thread.sleep(ms);
        } catch (InterruptedException error) {
            throw new AssertionError("interrupted", error);
        }
    }

    @BeforeAll
    static void warmUp() {
        try (RawHttpServer server = serving(request -> listing()); RestTransport transport = transport(server.url(), 5000)) {
            transport.functions();
        }
    }

    @Test
    void aServerUrlWithABadPortNoHostUserInfoAQueryOrAFragmentIsRefusedByName() {
        Function<String, String> refusal = url -> assertThrows(JeffError.InvalidRequest.class, () -> transport(url, 1000)).getMessage();
        assertEquals("The server URL 'http://h:99999' has an invalid port '99999'", refusal.apply("http://h:99999"));
        assertEquals("The server URL 'http://h:65536/jeff' has an invalid port '65536'", refusal.apply("http://h:65536/jeff"));
        assertEquals("The server URL 'http://h:abc' has an invalid port 'abc'", refusal.apply("http://h:abc"));
        assertEquals("The server URL 'http://h:-1' has an invalid port '-1'", refusal.apply("http://h:-1"));
        assertEquals("The server URL 'http://' has no host", refusal.apply("http://"));
        assertEquals("The server URL 'https://:8443/x' has no host", refusal.apply("https://:8443/x"));
        assertEquals("The server URL 'http://user:pw@h:1' must not carry a user name or password; pass the token instead",
            refusal.apply("http://user:pw@h:1"));
        assertEquals("The server URL 'http://h/x?q=1' must not have a query (the part after '?')", refusal.apply("http://h/x?q=1"));
        assertEquals("The server URL 'http://h/x#top' must not have a fragment (the part after '#')", refusal.apply("http://h/x#top"));
        assertTrue(refusal.apply("http://h/a b").startsWith("The server URL 'http://h/a b' is not a URL: "));
        ServerUrl plain = ServerUrl.parse("http://h:/");
        assertEquals(List.of("h", 80, "h", ""), List.of(plain.host(), plain.port(), plain.hostHeader(), plain.basePath()));
        ServerUrl secure = ServerUrl.parse("https://[::1]:8443/jeff/");
        assertEquals(List.of("::1", 8443, "[::1]:8443", "/jeff", "https://[::1]:8443/jeff"),
            List.of(secure.host(), secure.port(), secure.hostHeader(), secure.basePath(), secure.url()));
        assertEquals("api.example.com", ServerUrl.parse("https://api.example.com:443").hostHeader());
    }

    @Test
    void everyRequestPathFollowsTheServerUrlsOwnPath() {
        List<String> lines = Collections.synchronizedList(new ArrayList<>());
        try (RawHttpServer server = serving(request -> {
            lines.add(request.line() + " " + request.header("host"));
            return listing();
        }); RestTransport transport = transport(server.url() + "/jeff/", 2000)) {
            transport.functions();
            assertEquals(List.of("GET /jeff/v1/functions HTTP/1.1 127.0.0.1:" + server.port()), lines);
        }
    }

    @Test
    void aSlowConnectFollowedByAFastAnswerNeverAddsUpToATimeout() {
        HttpConnection.Opener slow = new HttpConnection.Opener() {
            @Override
            public void connect(java.nio.channels.SocketChannel channel, java.net.InetSocketAddress address, int timeoutMs)
                throws IOException {
                sleep(150);
                channel.socket().connect(address, timeoutMs);
            }
        };
        try (RawHttpServer server = serving(request -> {
            sleep(150);
            return listing();
        }); RestTransport transport = transport(server.url(), 250, new ConnectionPool(100, 20, 5000), slow)) {
            long started = System.nanoTime();
            transport.functions();
            assertTrue(millisSince(started) >= 300, millisSince(started) + " ms");
        }
    }

    /** An Error inside a request (here thrown while connecting; out of memory or a stack overflow in real life) goes
     * to the caller unchanged, and the pool's one place is given back, so the next request is served. */
    @Test
    void anErrorInARequestGivesItsPoolPlaceBack() {
        AtomicInteger connects = new AtomicInteger();
        HttpConnection.Opener failingOnce = new HttpConnection.Opener() {
            @Override
            public void connect(java.nio.channels.SocketChannel channel, java.net.InetSocketAddress address, int timeoutMs)
                throws IOException {
                if (connects.getAndIncrement() == 0) throw new StackOverflowError("thrown by the test");
                channel.socket().connect(address, timeoutMs);
            }
        };
        try (RawHttpServer server = serving(request -> listing());
             RestTransport transport = transport(server.url(), 500, new ConnectionPool(1, 1, 5000), failingOnce)) {
            assertEquals("thrown by the test", assertThrows(StackOverflowError.class, transport::functions).getMessage());
            assertEquals(0, transport.inFlightCount());
            assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
        }
    }

    @Test
    void aTlsHandshakeThatNeverEndsIsAConnectTimeoutPromptly() throws IOException {
        try (ServerSocket silent = new ServerSocket(0, 50, InetAddress.getLoopbackAddress())) {
            List<Socket> held = Collections.synchronizedList(new ArrayList<>());
            Thread.ofVirtual().start(() -> {
                try {
                    while (true) held.add(silent.accept());
                } catch (IOException closed) {
                    // the test ended
                }
            });
            String url = "https://127.0.0.1:" + silent.getLocalPort();
            try (RestTransport transport = transport(url, 200)) {
                long started = System.nanoTime();
                JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class, transport::functions);
                long elapsed = millisSince(started);
                assertEquals("Cannot reach the Jeff server at " + url + ": no connection within 200 ms", error.getMessage());
                assertTrue(elapsed >= 190 && elapsed < 400, elapsed + " ms");
            }
            for (Socket socket : held) socket.close();
        }
    }

    @Test
    void aChunkedBodyWhoseChunksEachComeInTimeIsReadWholeHoweverLongItTakes() {
        try (RawHttpServer server = new RawHttpServer((socket, index) -> {
            RawHttpServer.read(socket.getInputStream());
            OutputStream out = socket.getOutputStream();
            out.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nTransfer-Encoding: chunked\r\n\r\n"
                .getBytes(StandardCharsets.ISO_8859_1));
            int step = LISTING_BYTES.length / 5 + 1;
            for (int from = 0; from < LISTING_BYTES.length; from += step) {
                int count = Math.min(step, LISTING_BYTES.length - from);
                out.write((Integer.toHexString(count) + ";ext=1\r\n").getBytes(StandardCharsets.ISO_8859_1));
                out.write(LISTING_BYTES, from, count);
                out.write("\r\n".getBytes(StandardCharsets.ISO_8859_1));
                out.flush();
                sleep(80);
            }
            out.write("0\r\nX-Trailer: 1\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
            out.flush();
        }); RestTransport transport = transport(server.url(), 200)) {
            long started = System.nanoTime();
            assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
            assertTrue(millisSince(started) >= 400, millisSince(started) + " ms");
        }
    }

    @Test
    void waitingForAConnectionIsItsOwnPhaseNotCountedAgainstTheHeaders() {
        try (RawHttpServer server = serving(request -> {
            sleep(150);
            return listing();
        }); RestTransport transport = transport(server.url(), 250, new ConnectionPool(1, 1, 5000), HttpConnection.DIRECT)) {
            CompletableFuture<Map<String, Object>> first = Async.run(transport::functions);
            sleep(20);
            long started = System.nanoTime();
            transport.functions(); // about 130 ms waiting for the one connection, then 150 ms for its answer
            assertTrue(millisSince(started) >= 250, millisSince(started) + " ms");
            Async.join(first);
            assertEquals(1, server.connections.get());
        }
    }

    @Test
    void aRequestThatWaitsLongerThanTheTimeoutForAConnectionMissesTheDeadlinePromptly() {
        CountDownLatch release = new CountDownLatch(1);
        try (RawHttpServer server = serving(request -> {
            try {
                release.await();
            } catch (InterruptedException error) {
                throw new AssertionError(error);
            }
            return listing();
        }); RestTransport transport = transport(server.url(), 200, new ConnectionPool(1, 1, 5000), HttpConnection.DIRECT)) {
            // the first request holds the one connection until its own headers phase runs out, at 200 ms
            CompletableFuture<Map<String, Object>> first = Async.run(transport::functions);
            sleep(20);
            try (RestTransport full = transport(server.url(), 200, new ConnectionPool(0, 0, 5000), HttpConnection.DIRECT)) {
                long started = System.nanoTime();
                JeffError.DeadlineExceeded error = assertThrows(JeffError.DeadlineExceeded.class, full::functions);
                assertTrue(millisSince(started) >= 190 && millisSince(started) < 400, millisSince(started) + " ms");
                assertEquals("The Jeff server at " + server.url() + " did not answer GET /v1/functions within 200 ms; the server "
                    + "may still have run the request", error.getMessage());
            }
            assertInstanceOf(JeffError.DeadlineExceeded.class, assertThrows(JeffError.class, () -> Async.join(first)));
            release.countDown();
            assertEquals(1, server.connections.get());
        }
    }

    @Test
    void requestsWaitingForAConnectionAreServedInArrivalOrder() {
        CountDownLatch release = new CountDownLatch(1);
        List<String> order = Collections.synchronizedList(new ArrayList<>());
        AtomicInteger tokens = new AtomicInteger();
        try (RawHttpServer server = serving(request -> {
            String token = request.header("authorization");
            order.add(token);
            if (token.equals("Bearer t0")) {
                try {
                    release.await();
                } catch (InterruptedException error) {
                    throw new AssertionError(error);
                }
            }
            return listing();
        }); RestTransport transport = new RestTransport(server.url(), TokenSource.from(() -> "t" + tokens.getAndIncrement()), 5000,
            direct().withPool(new ConnectionPool(1, 1, 5000)))) {
            List<CompletableFuture<Map<String, Object>>> pending = new ArrayList<>();
            for (int index = 0; index < 6; index++) {
                pending.add(Async.run(transport::functions));
                sleep(30); // each request reads its token, and joins the queue, before the next starts
            }
            release.countDown();
            for (CompletableFuture<Map<String, Object>> each : pending) Async.join(each);
            assertEquals(List.of("Bearer t0", "Bearer t1", "Bearer t2", "Bearer t3", "Bearer t4", "Bearer t5"), order);
        }
    }

    @Test
    void threeHundredConcurrentRequestsShareAtMostOneHundredConnectionsAndAllAreAnswered() {
        AtomicInteger active = new AtomicInteger();
        AtomicInteger peak = new AtomicInteger();
        try (RawHttpServer server = serving(request -> {
            peak.accumulateAndGet(active.incrementAndGet(), Math::max);
            sleep(60);
            active.decrementAndGet();
            return listing();
        }); RestTransport transport = transport(server.url(), 10000)) {
            List<CompletableFuture<Map<String, Object>>> pending = new ArrayList<>();
            for (int index = 0; index < 300; index++) pending.add(Async.run(transport::functions));
            for (CompletableFuture<Map<String, Object>> each : pending) {
                assertEquals(Map.of("format", "bf16"), Async.join(each).get("base_format"));
            }
            assertEquals(300, server.requests.get());
            assertTrue(server.connections.get() <= ConnectionPool.MAX_CONNECTIONS, server.connections.get() + " connections");
            assertTrue(peak.get() <= ConnectionPool.MAX_CONNECTIONS, peak.get() + " at once");
            assertTrue(transport.pool().idleCount() <= ConnectionPool.MAX_IDLE, transport.pool().idleCount() + " idle");
            assertEquals(transport.pool().idleCount(), transport.pool().openCount());
            assertEquals(0, transport.inFlightCount());
        }
    }

    @Test
    void aConnectionIdleLongerThanTheKeepaliveExpiryIsNotReusedAndOneWithinItIs() {
        try (RawHttpServer server = serving(request -> listing());
             RestTransport transport = transport(server.url(), 2000, new ConnectionPool(100, 20, 150), HttpConnection.DIRECT)) {
            transport.functions();
            transport.functions();
            assertEquals(1, server.connections.get());
            sleep(250);
            transport.functions();
            assertEquals(2, server.connections.get());
            assertEquals(1, transport.pool().openCount());
        }
    }

    @Test
    void atMostMaxIdleConnectionsAreKept() {
        try (RawHttpServer server = serving(request -> {
            sleep(100);
            return listing();
        }); RestTransport transport = transport(server.url(), 2000, new ConnectionPool(10, 2, 5000), HttpConnection.DIRECT)) {
            List<CompletableFuture<Map<String, Object>>> pending = new ArrayList<>();
            for (int index = 0; index < 5; index++) pending.add(Async.run(transport::functions));
            for (CompletableFuture<Map<String, Object>> each : pending) Async.join(each);
            assertEquals(5, server.connections.get());
            assertEquals(2, transport.pool().idleCount());
            assertEquals(2, transport.pool().openCount());
        }
    }

    @Test
    void aConnectionThatEndsBeforeTheAnswerFailsTheRequestWhichIsNeverSentAgain() {
        RawHttpServer[] self = new RawHttpServer[1];
        self[0] = new RawHttpServer((socket, index) -> {
            InputStream in = socket.getInputStream();
            self[0].readRequest(in);
            socket.getOutputStream().write(listing());
            socket.getOutputStream().flush();
            self[0].readRequest(in); // the second request on this connection: closed without an answer
        });
        try (RawHttpServer server = self[0]; RestTransport transport = transport(server.url(), 2000)) {
            transport.functions();
            JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class, transport::functions);
            assertEquals("The connection to the Jeff server at " + server.url() + " failed during GET /v1/functions: "
                + "RemoteProtocolError: Server disconnected without sending a response.", error.getMessage());
            sleep(100);
            assertEquals(List.of(2, 1), List.of(server.requests.get(), server.connections.get()));
        }
        RawHttpServer[] posts = new RawHttpServer[1];
        posts[0] = new RawHttpServer((socket, index) -> posts[0].readRequest(socket.getInputStream()));
        try (RawHttpServer server = posts[0]; RestTransport transport = transport(server.url(), 2000)) {
            assertThrows(JeffError.BackendUnavailable.class, () -> transport.decideCalls(List.of(Map.of("function", "x")), "return"));
            sleep(100);
            assertEquals(List.of(1, 1), List.of(server.requests.get(), server.connections.get()));
        }
    }

    @Test
    void aPooledConnectionTheServerHasClosedIsDroppedAndANewOneOpened() {
        RawHttpServer[] self = new RawHttpServer[1];
        self[0] = new RawHttpServer((socket, index) -> {
            self[0].readRequest(socket.getInputStream());
            socket.getOutputStream().write(listing());
            socket.getOutputStream().flush();
            // then closed, while the client keeps the connection idle
        });
        try (RawHttpServer server = self[0]; RestTransport transport = transport(server.url(), 2000)) {
            transport.functions();
            sleep(100);
            transport.functions();
            assertEquals(2, server.connections.get());
            assertEquals(2, server.requests.get());
        }
    }

    @Test
    void anAnswerThatAsksToCloseTheConnectionOrIsHttp10OrFramedTwiceIsNotReused() {
        for (byte[] answer : List.of(
            RawHttpServer.answer(200, List.of("Connection: keep-alive, Close"), LISTING_BYTES),
            ("HTTP/1.0 200 OK\r\nContent-Length: " + LISTING_BYTES.length + "\r\n\r\n" + LISTING).getBytes(StandardCharsets.UTF_8),
            ("HTTP/1.1 200 OK\r\nContent-Length: 999\r\nTransfer-Encoding: chunked\r\n\r\n" + Integer.toHexString(LISTING_BYTES.length)
                + "\r\n" + LISTING + "\r\n0\r\n\r\n").getBytes(StandardCharsets.UTF_8))) {
            try (RawHttpServer server = serving(request -> answer); RestTransport transport = transport(server.url(), 2000)) {
                transport.functions();
                transport.functions();
                assertEquals(2, server.connections.get());
                assertEquals(0, transport.pool().openCount());
            }
        }
    }

    @Test
    void anAnswerWithInformationalAnswersFirstAndABodyThatRunsToTheEndOfTheConnectionIsRead() {
        try (RawHttpServer server = new RawHttpServer((socket, index) -> {
            RawHttpServer.read(socket.getInputStream());
            socket.getOutputStream().write(("HTTP/1.1 100 Continue\r\n\r\nHTTP/1.1 103 Early Hints\r\nLink: </x>\r\n\r\n"
                + "HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n" + LISTING).getBytes(StandardCharsets.UTF_8));
        }); RestTransport transport = transport(server.url(), 2000)) {
            assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
        }
    }

    @Test
    void anAnswerWhoseHeadIsTooLargeOrCutShortIsABrokenConnection() {
        String big = "HTTP/1.1 200 OK\r\nX-Big: " + "a".repeat(HttpConnection.MAX_HEAD) + "\r\n\r\n";
        try (RawHttpServer server = serving(request -> big.getBytes(StandardCharsets.ISO_8859_1));
             RestTransport transport = transport(server.url(), 2000)) {
            assertTrue(assertThrows(JeffError.BackendUnavailable.class, transport::functions).getMessage()
                .endsWith("failed during GET /v1/functions: RemoteProtocolError: Receive buffer too long"));
        }
        try (RawHttpServer server = new RawHttpServer((socket, index) -> {
            RawHttpServer.read(socket.getInputStream());
            socket.getOutputStream().write(("HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nabc").getBytes(StandardCharsets.ISO_8859_1));
        }); RestTransport transport = transport(server.url(), 2000)) {
            assertTrue(assertThrows(JeffError.BackendUnavailable.class, transport::functions).getMessage()
                .endsWith("RemoteProtocolError: peer closed connection without sending complete message body (received 3 bytes, "
                    + "expected 10)"));
        }
        try (RawHttpServer server = new RawHttpServer((socket, index) -> {
            RawHttpServer.read(socket.getInputStream());
            socket.getOutputStream().write("this is not HTTP\r\n\r\n".getBytes(StandardCharsets.ISO_8859_1));
        }); RestTransport transport = transport(server.url(), 2000)) {
            assertEquals("The connection to the Jeff server at " + server.url() + " failed during GET /v1/functions: RemoteProtocolError: "
                + "illegal status line: bytearray(b'this is not HTTP')", assertThrows(JeffError.BackendUnavailable.class,
                    transport::functions).getMessage());
        }
        try (RawHttpServer server = new RawHttpServer((socket, index) -> {
            RawHttpServer.read(socket.getInputStream());
            socket.getOutputStream().write("HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc".getBytes(StandardCharsets.ISO_8859_1));
        }); RestTransport transport = transport(server.url(), 2000)) {
            assertTrue(assertThrows(JeffError.BackendUnavailable.class, transport::functions).getMessage()
                .endsWith("RemoteProtocolError: peer closed connection without sending complete message body (incomplete chunked read)"));
        }
    }

    @Test
    void closingEndsARequestWaitingForAConnection() {
        CountDownLatch release = new CountDownLatch(1);
        try (RawHttpServer server = serving(request -> {
            try {
                release.await();
            } catch (InterruptedException error) {
                throw new AssertionError(error);
            }
            return listing();
        })) {
            RestTransport transport = transport(server.url(), 5000, new ConnectionPool(1, 1, 5000), HttpConnection.DIRECT);
            CompletableFuture<Map<String, Object>> first = Async.run(transport::functions);
            sleep(50);
            CompletableFuture<Map<String, Object>> waiting = Async.run(transport::functions);
            sleep(50);
            transport.close();
            String closed = "This remote client of " + server.url() + " is closed; connect again with Jeff.remote";
            assertEquals(closed, assertThrows(JeffError.InvalidRequest.class, () -> Async.join(waiting)).getMessage());
            assertEquals(closed, assertThrows(JeffError.InvalidRequest.class, () -> Async.join(first)).getMessage());
            assertEquals(0, transport.inFlightCount());
            release.countDown();
        }
    }

    private static byte[] gzip(byte[] data) throws IOException {
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        try (GZIPOutputStream zipped = new GZIPOutputStream(out)) {
            zipped.write(data);
        }
        return out.toByteArray();
    }

    private static byte[] deflate(byte[] data, boolean raw) {
        Deflater deflater = new Deflater(Deflater.DEFAULT_COMPRESSION, raw);
        deflater.setInput(data);
        deflater.finish();
        ByteArrayOutputStream out = new ByteArrayOutputStream();
        byte[] buffer = new byte[4096];
        while (!deflater.finished()) out.write(buffer, 0, deflater.deflate(buffer));
        deflater.end();
        return out.toByteArray();
    }

    @Test
    void aGzipOrDeflateBodyIsDecodedAsHttpxDecodesIt() throws IOException {
        byte[] zipped = gzip(LISTING_BYTES);
        byte[] twice = gzip(deflate(LISTING_BYTES, false));
        List<byte[]> answers = List.of(
            RawHttpServer.answer(200, List.of("Content-Encoding: gzip"), zipped),
            RawHttpServer.answer(200, List.of("Content-Encoding: deflate"), deflate(LISTING_BYTES, false)),
            RawHttpServer.answer(200, List.of("Content-Encoding: deflate"), deflate(LISTING_BYTES, true)),
            RawHttpServer.answer(200, List.of("Content-Encoding: deflate, gzip"), twice),
            RawHttpServer.answer(200, List.of("Content-Encoding: identity, br-unknown"), LISTING_BYTES));
        for (byte[] answer : answers) {
            List<String> seen = Collections.synchronizedList(new ArrayList<>());
            try (RawHttpServer server = serving(request -> {
                seen.add(request.header("accept-encoding"));
                return answer;
            }); RestTransport transport = transport(server.url(), 2000)) {
                assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
                assertEquals(List.of("gzip, deflate"), seen);
            }
        }
    }

    /** The final review's answer: 5,128 bytes, Content-Encoding "gzip, gzip", 3 GB once both layers are undone
     * (src/test/resources/bombs/gzip-gzip-3gb.bin, made with Python's zlib: 3 GiB of zero bytes gzipped, then gzipped
     * again). Jeff.remote refuses it by name, without filling the heap, and closes its connection. */
    @Test
    void aCompressedAnswerPastTheLimitIsANamedErrorAndJeffRemoteClosesTheTransport() throws Exception {
        byte[] bomb;
        try (InputStream in = RemoteHttpTest.class.getResourceAsStream("/bombs/gzip-gzip-3gb.bin")) {
            bomb = in.readAllBytes();
        }
        assertEquals(5128, bomb.length);
        CountDownLatch clientClosed = new CountDownLatch(1);
        RawHttpServer[] self = new RawHttpServer[1];
        self[0] = new RawHttpServer((socket, index) -> {
            InputStream in = socket.getInputStream();
            OutputStream out = socket.getOutputStream();
            while (true) {
                if (self[0].readRequest(in) == null) {
                    clientClosed.countDown();
                    return;
                }
                out.write(RawHttpServer.answer(200, List.of("Content-Type: application/json", "Content-Encoding: gzip, gzip"), bomb));
                out.flush();
            }
        });
        try (RawHttpServer server = self[0]) {
            JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class,
                () -> Jeff.remote(server.url(), TokenSource.none(), RemoteOptions.timeoutMs(20000)));
            assertEquals("The Jeff server at " + server.url() + " answered GET /v1/functions with a body that decodes "
                + "(Content-Encoding) to more than 67108864 bytes, the most this client reads; the client is closed, connect again "
                + "with Jeff.remote", error.getMessage());
            assertTrue(clientClosed.await(5, java.util.concurrent.TimeUnit.SECONDS), "the transport's connection was left open");
        }
    }

    /** One gzip layer of 64 MiB and one byte of zeros (about 65 KB on the wire): refused by name once its decoding
     * passes the limit, and the transport's connection is closed. */
    @Test
    void aSingleGzipLayerPastTheLimitIsANamedErrorAndJeffRemoteClosesTheTransport() throws Exception {
        byte[] bomb = gzip(new byte[(int) HttpConnection.MAX_BODY + 1]);
        assertTrue(bomb.length < 100_000, "the bomb is small on the wire");
        CountDownLatch clientClosed = new CountDownLatch(1);
        RawHttpServer[] self = new RawHttpServer[1];
        self[0] = new RawHttpServer((socket, index) -> {
            InputStream in = socket.getInputStream();
            OutputStream out = socket.getOutputStream();
            while (true) {
                if (self[0].readRequest(in) == null) {
                    clientClosed.countDown();
                    return;
                }
                out.write(RawHttpServer.answer(200, List.of("Content-Type: application/json", "Content-Encoding: gzip"), bomb));
                out.flush();
            }
        });
        try (RawHttpServer server = self[0]) {
            JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class,
                () -> Jeff.remote(server.url(), TokenSource.none(), RemoteOptions.timeoutMs(20000)));
            assertEquals("The Jeff server at " + server.url() + " answered GET /v1/functions with a body that decodes "
                + "(Content-Encoding) to more than 67108864 bytes, the most this client reads; the client is closed, connect again "
                + "with Jeff.remote", error.getMessage());
            assertTrue(clientClosed.await(5, java.util.concurrent.TimeUnit.SECONDS), "the transport's connection was left open");
        }
    }

    /** After a decoded body past the limit the transport is closed: a later request is refused without being sent. */
    @Test
    void aDecodedBodyPastTheLimitClosesTheTransport() throws Exception {
        byte[] bomb = gzip(new byte[(int) HttpConnection.MAX_BODY + 1]);
        java.util.concurrent.atomic.AtomicInteger requests = new java.util.concurrent.atomic.AtomicInteger();
        try (RawHttpServer server = serving(request -> {
            requests.incrementAndGet();
            return RawHttpServer.answer(200, List.of("Content-Type: application/json", "Content-Encoding: gzip"), bomb);
        }); RestTransport transport = transport(server.url(), 20000)) {
            assertThrows(JeffError.BackendUnavailable.class, transport::functions);
            JeffError.InvalidRequest closed = assertThrows(JeffError.InvalidRequest.class, transport::functions);
            assertEquals("This remote client of " + server.url() + " is closed; connect again with Jeff.remote", closed.getMessage());
            assertEquals(1, requests.get());
        }
    }

    @Test
    void eachDecodedLayerIsHeldToTheLimitAndAnOverlongDeflateIsNotRetriedAsRaw() throws Exception {
        byte[] data = new byte[1000];
        List<String[]> gzipTwice = Collections.singletonList(new String[] {"content-encoding", "gzip, gzip"});
        assertEquals(1000, HttpBody.decoded(gzip(gzip(data)), gzipTwice, 1000).length);
        assertEquals("the decoded answer is larger than 999 bytes, more than this SDK holds",
            assertThrows(HttpBody.TooLarge.class, () -> HttpBody.decoded(gzip(gzip(data)), gzipTwice, 999)).getMessage());
        for (boolean raw : List.of(false, true)) {
            assertThrows(HttpBody.TooLarge.class,
                () -> HttpBody.decoded(deflate(data, raw), Collections.singletonList(new String[] {"content-encoding", "deflate"}), 999));
        }
    }

    /** An answer with neither Content-Length nor chunking ends with the connection; past the limit it is refused. */
    @Test
    void aBodyEndedByTheConnectionIsHeldToTheLimit() {
        try (RawHttpServer server = new RawHttpServer((socket, index) -> {
            RawHttpServer.read(socket.getInputStream());
            OutputStream out = socket.getOutputStream();
            out.write("HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\n\r\n".getBytes(StandardCharsets.US_ASCII));
            byte[] block = new byte[1 << 20];
            for (int sent = 0; sent <= 64; sent++) out.write(block);
        }); RestTransport transport = transport(server.url(), 10000)) {
            JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class, transport::functions);
            assertEquals("The Jeff server at " + server.url() + " answered GET /v1/functions with a body of more than 67108864 bytes, "
                + "the most this client reads; the client is closed, connect again with Jeff.remote", error.getMessage());
            assertEquals("This remote client of " + server.url() + " is closed; connect again with Jeff.remote",
                assertThrows(JeffError.InvalidRequest.class, transport::functions).getMessage());
        }
    }

    @Test
    void aBodyThatCannotBeDecodedIsAnEngineErrorWithoutARequestId() {
        try (RawHttpServer server = serving(request -> RawHttpServer.answer(200, List.of("Content-Encoding: gzip", "x-request-id: r"),
            "not gzip".getBytes(StandardCharsets.UTF_8))); RestTransport transport = transport(server.url(), 2000)) {
            JeffError.EngineError error = assertThrows(JeffError.EngineError.class, transport::functions);
            assertEquals("The answer of the Jeff server at " + server.url() + " to GET /v1/functions could not be read: DecodingError: "
                + "Error -3 while decompressing data: incorrect header check", error.getMessage());
            assertNull(error.requestId());
        }
    }

    @Test
    void anAnswerIsParsedFromItsBytesAsJsonLoadsParsesThem() {
        byte[] bom = new byte[LISTING_BYTES.length + 3];
        bom[0] = (byte) 0xEF;
        bom[1] = (byte) 0xBB;
        bom[2] = (byte) 0xBF;
        System.arraycopy(LISTING_BYTES, 0, bom, 3, LISTING_BYTES.length);
        for (byte[] body : List.of(bom, LISTING.getBytes(StandardCharsets.UTF_16LE), LISTING.getBytes(StandardCharsets.UTF_16),
            LISTING.getBytes(java.nio.charset.Charset.forName("UTF-32BE")))) {
            try (RawHttpServer server = serving(request -> RawHttpServer.answer(200, JSON_TYPE, body));
                 RestTransport transport = transport(server.url(), 2000)) {
                assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
            }
        }
        byte[] broken = "{\"base_format\": \"\u00e9\u00ff".getBytes(StandardCharsets.ISO_8859_1);
        try (RawHttpServer server = serving(request -> RawHttpServer.answer(200, List.of("x-request-id: rid-9"), broken));
             RestTransport transport = transport(server.url(), 2000)) {
            JeffError.EngineError error = assertThrows(JeffError.EngineError.class, transport::functions);
            assertEquals("The Jeff server at " + server.url() + " answered GET /v1/functions with status 200 and a body that is not "
                + "JSON: {\"base_format\": \"\ufffd\ufffd", error.getMessage());
            assertEquals("rid-9", error.requestId());
        }
        try (RawHttpServer server = serving(request -> RawHttpServer.answer(500, List.of("Content-Type: text/plain; charset=\"Latin-1\""),
            "caf\u00e9".getBytes(StandardCharsets.ISO_8859_1))); RestTransport transport = transport(server.url(), 2000)) {
            assertTrue(assertThrows(JeffError.EngineError.class, transport::functions).getMessage().endsWith("not a Jeff error: caf\u00e9"));
        }
    }

    private static RawHttpServer errorServer(String details) {
        return serving(request -> RawHttpServer.answer(400, List.of("x-request-id: rid-7"),
            ("{\"error\": {\"type\": \"InvalidRequest\", \"message\": \"bad\"" + details + "}}").getBytes(StandardCharsets.UTF_8)));
    }

    @Test
    void errorDetailsPythonReadsAsFalseAreNoneAndOtherDetailsThatAreNotAnObjectAreRefusedByName() {
        for (String details : List.of("", ", \"details\": null", ", \"details\": false", ", \"details\": 0", ", \"details\": 0.0",
            ", \"details\": \"\"", ", \"details\": []", ", \"details\": {}")) {
            try (RawHttpServer server = errorServer(details); RestTransport transport = transport(server.url(), 2000)) {
                JeffError.InvalidRequest error = assertThrows(JeffError.InvalidRequest.class, transport::functions);
                assertEquals(List.of("bad", Map.of(), "rid-7"), List.of(error.getMessage(), error.details(), error.requestId()));
            }
        }
        for (String[] details : List.of(new String[] {"\"garbage\"", "'garbage'"}, new String[] {"5", "5"},
            new String[] {"true", "True"}, new String[] {"[[\"decision\", 1]]", "[['decision', 1]]"})) {
            try (RawHttpServer server = errorServer(", \"details\": " + details[0]); RestTransport transport = transport(server.url(), 2000)) {
                JeffError.ContractError error = assertThrows(JeffError.ContractError.class, transport::functions);
                assertEquals("The InvalidRequest error received has 'details' that are not an object: " + details[1] + "; an error's "
                    + "details must be an object (its message was: bad)", error.getMessage());
                assertEquals("rid-7", error.requestId());
            }
        }
    }

    @Test
    void aDecisionThatFailsTheSchemaInAnErrorOfAnyTypeIsAContractErrorWithTheRequestId() {
        try (RawHttpServer server = serving(request -> RawHttpServer.answer(409, List.of("x-request-id: rid-3"),
            "{\"error\": {\"type\": \"NoSuchType\", \"message\": \"m\", \"details\": {\"decision\": {\"value\": 1}}}}"
                .getBytes(StandardCharsets.UTF_8))); RestTransport transport = transport(server.url(), 2000)) {
            JeffError.ContractError error = assertThrows(JeffError.ContractError.class, transport::functions);
            assertTrue(error.getMessage().startsWith("The NoSuchType error the Jeff server at " + server.url() + " answered GET "
                + "/v1/functions with carries a decision that does not match contract/schemas/decision: "), error.getMessage());
            assertEquals("rid-3", error.requestId());
        }
    }

    @Test
    void aRequestIdIsReadAsHttpxReadsItsHeaders() {
        List<byte[]> answers = List.of(
            ("HTTP/1.1 401 X\r\nx-request-id: caf\u00e9\r\nContent-Length: 2\r\n\r\n{}").getBytes(StandardCharsets.UTF_8),
            ("HTTP/1.1 401 X\r\nx-request-id: caf\u00e9\r\nContent-Length: 2\r\n\r\n{}").getBytes(StandardCharsets.ISO_8859_1),
            ("HTTP/1.1 401 X\r\nX-Request-Id: a\r\nx-request-id:  b \r\nContent-Length: 2\r\n\r\n{}").getBytes(StandardCharsets.ISO_8859_1));
        List<String> ids = new ArrayList<>();
        for (byte[] answer : answers) {
            try (RawHttpServer server = serving(request -> answer); RestTransport transport = transport(server.url(), 2000)) {
                ids.add(assertThrows(JeffError.EngineError.class, transport::functions).requestId());
            }
        }
        assertEquals(List.of("caf\u00e9", "caf\u00e9", "a, b"), ids);
    }

    @Test
    void aRequestIsSentOnceWithTheHeadersHttpxSends() {
        List<RawHttpServer.Request> seen = Collections.synchronizedList(new ArrayList<>());
        try (RawHttpServer server = serving(request -> {
            seen.add(request);
            return RawHttpServer.answer(200, JSON_TYPE, "{\"decisions\": []}".getBytes(StandardCharsets.UTF_8));
        }); RestTransport transport = new RestTransport(server.url(), TokenSource.of("tok"), 2000)) {
            assertThrows(JeffError.ContractError.class, () -> transport.decideCalls(List.of(Map.of("function", "f", "input", "\u00e9")), "return"));
            RawHttpServer.Request request = seen.get(0);
            assertEquals("POST /v1/decide HTTP/1.1", request.line());
            assertEquals(List.of("127.0.0.1:" + server.port(), "*/*", "gzip, deflate", "keep-alive", "Bearer tok", "application/json"),
                List.of(request.header("host"), request.header("accept"), request.header("accept-encoding"),
                    request.header("connection"), request.header("authorization"), request.header("content-type")));
            assertEquals(Map.of("calls", List.of(Map.of("function", "f", "input", "\u00e9")), "on_low_confidence", "return"),
                Json.parse(new String(request.body(), StandardCharsets.UTF_8)));
            assertEquals(String.valueOf(request.body().length), request.header("content-length"));
            assertEquals(1, seen.size());
        }
    }

    @Test
    void manyClientsCanUseOneTransportFromManyThreadsWithoutLosingAConnectionPlace() {
        try (RawHttpServer server = serving(request -> listing());
             RestTransport transport = transport(server.url(), 5000, new ConnectionPool(4, 2, 5000), HttpConnection.DIRECT)) {
            List<CompletableFuture<Void>> loops = new ArrayList<>();
            for (int worker = 0; worker < 16; worker++) {
                loops.add(CompletableFuture.runAsync(() -> {
                    for (int round = 0; round < 25; round++) transport.functions();
                }, Async.EXECUTOR));
            }
            for (CompletableFuture<Void> loop : loops) Async.join(loop);
            assertEquals(400, server.requests.get());
            assertTrue(transport.pool().idleCount() <= 2, transport.pool().idleCount() + " idle");
            assertEquals(transport.pool().idleCount(), transport.pool().openCount());
            assertEquals(0, transport.inFlightCount());
        }
    }

    @Test
    void anExpiredRequestLeavesNoTimerAndNoPlaceBehind() {
        try (RawHttpServer server = serving(request -> {
            sleep(300);
            return listing();
        }); RestTransport transport = transport(server.url(), 100, new ConnectionPool(1, 1, 5000), HttpConnection.DIRECT)) {
            for (int round = 0; round < 3; round++) assertInstanceOf(JeffError.DeadlineExceeded.class,
                assertThrows(JeffError.class, transport::functions));
            assertEquals(0, transport.pool().openCount());
            assertEquals(0, transport.inFlightCount());
        }
    }

    @Test
    void anHttpsServerIsReachedWithItsCertificateCheckedAndOneThatIsNotTrustedOrForAnotherHostCannotBeReached() {
        TestTls tls = TestTls.create("ip:127.0.0.1,dns:localhost");
        com.sun.net.httpserver.HttpsServer server = tls.serve(LISTING_BYTES);
        try {
            int port = server.getAddress().getPort();
            for (String url : List.of("https://127.0.0.1:" + port, "https://localhost:" + port)) {
                try (RestTransport transport = new RestTransport(url, TokenSource.none(), 5000,
                    direct().withTls(tls.client()::getSocketFactory))) {
                    assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
                    assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
                    assertEquals(1, transport.pool().openCount());
                }
            }
            String url = "https://127.0.0.1:" + port;
            try (RestTransport untrusted = transport(url, 5000)) {
                String message = assertThrows(JeffError.BackendUnavailable.class, untrusted::functions).getMessage();
                assertTrue(message.startsWith("Cannot reach the Jeff server at " + url + ": "), message);
            }
        } finally {
            server.stop(0);
        }
        TestTls other = TestTls.create("dns:other.example");
        com.sun.net.httpserver.HttpsServer elsewhere = other.serve(LISTING_BYTES);
        try {
            int port = elsewhere.getAddress().getPort();
            for (String[] reached : List.of(new String[] {"https://127.0.0.1:" + port, "No subject alternative names matching IP address 127.0.0.1 found"},
                new String[] {"https://localhost:" + port, "No subject alternative DNS name matching localhost found"})) {
                try (RestTransport transport = new RestTransport(reached[0], TokenSource.none(), 5000,
                    direct().withTls(other.client()::getSocketFactory))) {
                    String message = assertThrows(JeffError.BackendUnavailable.class, transport::functions).getMessage();
                    assertTrue(message.startsWith("Cannot reach the Jeff server at " + reached[0] + ": CertificateException: ")
                        && message.contains(reached[1]), message);
                }
            }
        } finally {
            elsewhere.stop(0);
        }
    }

    private static String failure(byte[] answer) {
        try (RawHttpServer server = new RawHttpServer((socket, index) -> {
            RawHttpServer.read(socket.getInputStream());
            socket.getOutputStream().write(answer);
        }); RestTransport transport = transport(server.url(), 2000)) {
            String message = assertThrows(JeffError.BackendUnavailable.class, transport::functions).getMessage();
            String prefix = "The connection to the Jeff server at " + server.url() + " failed during GET /v1/functions: RemoteProtocolError: ";
            assertTrue(message.startsWith(prefix), message);
            return message.substring(prefix.length());
        }
    }

    private static byte[] latin1(String text) {
        return text.getBytes(StandardCharsets.ISO_8859_1);
    }

    @Test
    void malformedAnswersAreRefusedInH11sWords() {
        String chunked = "HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n";
        String incomplete = "peer closed connection without sending complete message body (incomplete chunked read)";
        assertEquals(incomplete, failure(latin1(chunked + "10000000000000000\r\nabc")));
        assertEquals(incomplete, failure(latin1(chunked + "7fffffff\r\nabc")));
        assertEquals(incomplete, failure(latin1(chunked + "ffffffffffffffffffff\r\nabc")));
        assertEquals("illegal chunk header: bytearray(b'zz\\r\\n')", failure(latin1(chunked + "zz\r\n")));
        assertEquals("illegal chunk header: bytearray(b'-1\\r\\n')", failure(latin1(chunked + "-1\r\n")));
        assertEquals("malformed chunk footer: bytearray(b'XY') (expected b'\\r\\n')", failure(latin1(chunked + "3\r\nabcXY")));
        assertEquals("conflicting Content-Length headers", failure(latin1("HTTP/1.1 200 OK\r\nContent-Length: 2, x\r\n\r\n{}")));
        assertEquals("bad Content-Length", failure(latin1("HTTP/1.1 200 OK\r\nContent-Length: " + "1".repeat(21) + "\r\n\r\n{}")));
        assertEquals("continuation line at start of headers", failure(latin1("HTTP/1.1 200 OK\r\n  folded: x\r\n\r\n")));
        assertEquals("illegal header line: bytearray(b'x-request-id: a\\rb')",
            failure(latin1("HTTP/1.1 401 X\r\nx-request-id: a\rb\r\nContent-Length: 2\r\n\r\n{}")));
        assertEquals("illegal header line: bytearray(b'X-Trailer: a\\x00b')",
            failure(latin1(chunked + "2\r\n{}\r\n0\r\nX-Trailer: a\u0000b\r\n\r\n")));
        String endless = chunked + "2\r\n{}\r\n0\r\n" + "X-Trailer: aaaaaaaaaa\r\n".repeat(HttpConnection.MAX_HEAD / 20);
        assertEquals("Receive buffer too long", failure(latin1(endless)));
        // a Content-Length of 19 digits with leading zeros is the length h11 reads
        try (RawHttpServer server = serving(request -> latin1("HTTP/1.1 200 OK\r\nContent-Length: 000000000000000"
            + String.format("%04d", LISTING_BYTES.length) + "\r\n\r\n" + new String(LISTING_BYTES, StandardCharsets.ISO_8859_1)));
             RestTransport transport = transport(server.url(), 2000)) {
            assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
        }
    }

    /** Looks every name up slowly: `delayMs` before the real lookup. */
    private static HttpConnection.Opener slowLookup(long delayMs) {
        return new HttpConnection.Opener() {
            @Override
            public InetAddress[] resolve(String host) throws java.net.UnknownHostException {
                sleep(delayMs);
                return InetAddress.getAllByName(host);
            }
        };
    }

    @Test
    void aSlowNameLookupIsBoundedByTheConnectTimeoutAndEndedByClose() {
        try (RawHttpServer server = serving(request -> listing())) {
            try (RestTransport transport = transport(server.url(), 200, new ConnectionPool(100, 20, 5000), slowLookup(3000))) {
                long started = System.nanoTime();
                JeffError.BackendUnavailable error = assertThrows(JeffError.BackendUnavailable.class, transport::functions);
                assertTrue(millisSince(started) >= 190 && millisSince(started) < 400, millisSince(started) + " ms");
                assertEquals("Cannot reach the Jeff server at " + server.url() + ": the address of 127.0.0.1 was not found within 200 ms",
                    error.getMessage());
                assertEquals(0, transport.pool().openCount());
            }
            RestTransport closing = transport(server.url(), 5000, new ConnectionPool(100, 20, 5000), slowLookup(3000));
            CompletableFuture<Map<String, Object>> pending = Async.run(closing::functions);
            sleep(100);
            long started = System.nanoTime();
            closing.close();
            assertEquals("This remote client of " + server.url() + " is closed; connect again with Jeff.remote",
                assertThrows(JeffError.InvalidRequest.class, () -> Async.join(pending)).getMessage());
            assertTrue(millisSince(started) < 200, millisSince(started) + " ms");
            assertEquals(0, closing.inFlightCount());
            // a lookup that ends in time is followed by connecting as usual
            try (RestTransport transport = transport(server.url(), 300, new ConnectionPool(100, 20, 5000), slowLookup(100))) {
                assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
            }
        }
    }

    private static final String PROXY_TABLE = """
        [{"env": {"HTTP_PROXY": "p1:3128"}, "urls": ["http://h/x", "https://h", "http://h:8080"], "proxies": ["http://p1:3128|", null, "http://p1:3128|"]}, {"env": {"HTTPS_PROXY": "http://p2:3128", "HTTP_PROXY": "http://p1:3128"}, "urls": ["http://h", "https://h"], "proxies": ["http://p1:3128|", "http://p2:3128|"]}, {"env": {"ALL_PROXY": "http://pa:1", "HTTPS_PROXY": "http://p2:2"}, "urls": ["http://h", "https://h"], "proxies": ["http://pa:1|", "http://p2:2|"]}, {"env": {"https_proxy": "http://lower:1", "HTTPS_PROXY": "http://upper:2"}, "urls": ["https://h"], "proxies": ["http://lower:1|"]}, {"env": {"HTTPS_PROXY": "http://upper:2", "https_proxy": ""}, "urls": ["https://h"], "proxies": [null]}, {"env": {"HTTP_PROXY": "http://p:1", "REQUEST_METHOD": "GET"}, "urls": ["http://h"], "proxies": [null]}, {"env": {"http_proxy": "http://p:1", "REQUEST_METHOD": "GET"}, "urls": ["http://h"], "proxies": ["http://p:1|"]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": "example.com"}, "urls": ["http://example.com", "http://www.example.com", "http://wwwexample.com", "http://example.com.evil", "http://EXAMPLE.com"], "proxies": [null, null, "http://p:1|", "http://p:1|", null]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": ".example.com"}, "urls": ["http://example.com", "http://www.example.com"], "proxies": ["http://p:1|", null]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": " localhost , 127.0.0.1,::1"}, "urls": ["http://localhost:8000", "http://127.0.0.1:1", "http://[::1]:2", "http://127.0.0.2"], "proxies": [null, null, null, "http://p:1|"]}, {"env": {"ALL_PROXY": "http://p:1", "no_proxy": "*"}, "urls": ["http://h"], "proxies": [null]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": "a.com,*"}, "urls": ["http://h"], "proxies": [null]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": "192.168.0.0/16"}, "urls": ["http://192.168.0.0", "http://192.168.1.1"], "proxies": [null, "http://p:1|"]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": "h:8080"}, "urls": ["http://h:8080", "http://h"], "proxies": [null, "http://p:1|"]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": "https://h"}, "urls": ["https://h", "http://h"], "proxies": [null, "http://p:1|"]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": "*.example.com"}, "urls": ["http://example.com", "http://a.example.com"], "proxies": ["http://p:1|", "http://p:1|"]}, {"env": {"HTTPS_PROXY": "https://user:p%40ss@sp:8443/path"}, "urls": ["https://h"], "proxies": ["https://sp:8443|Proxy-Authorization=Basic dXNlcjpwQHNz"]}, {"env": {"ALL_PROXY": "http://p:1", "NO_PROXY": "LocalHost"}, "urls": ["http://localhost", "http://LOCALHOST"], "proxies": [null, null]}]
        """;

    @Test
    void theProxyIsChosenFromTheEnvironmentAsHttpxChoosesIt() {
        // recorded from httpx 0.28.1 (trust_env=True) with each environment alone: Client()._transport_for_url(url)
        for (Object item : Values.list(Json.parse(PROXY_TABLE))) {
            Map<String, Object> each = Values.map(item);
            Map<String, String> environment = new java.util.LinkedHashMap<>();
            Values.map(each.get("env")).forEach((name, value) -> environment.put(name, (String) value));
            List<Object> urls = Values.list(each.get("urls"));
            List<Object> expected = Values.list(each.get("proxies"));
            for (int index = 0; index < urls.size(); index++) {
                Proxies.Proxy chosen = Proxies.forUrl(ServerUrl.parse((String) urls.get(index)), environment);
                assertEquals(expected.get(index), chosen == null ? null : chosen.described(), environment + " " + urls.get(index));
            }
        }
        Function<String, String> refusal = value -> assertThrows(JeffError.InvalidRequest.class,
            () -> Proxies.forUrl(ServerUrl.parse("http://h"), Map.of("HTTP_PROXY", value))).getMessage();
        assertEquals("The proxy URL 'ftp://p:1' from the environment has an unknown scheme; use http:// or https://", refusal.apply("ftp://p:1"));
        assertEquals("The proxy URL 'socks5://user:[secure]@p:1' from the environment is a SOCKS proxy, which this SDK does not support",
            refusal.apply("socks5://user:pw@p:1"));
        assertEquals("The proxy URL 'http://p:99999' from the environment has an invalid port '99999'", refusal.apply("http://p:99999"));
        assertTrue(!Proxies.proxyOf("http://user:pw@p:1").toString().contains("pw"));
    }

    /** A proxy's user name and password percent-decoded as httpx's Proxy decodes them (urllib.parse.unquote): only
     * ASCII hex digits make an escape, bad UTF-8 becomes U+FFFD, and a character outside the BMP is kept whole. The
     * expected headers are httpx 0.28's (base64 of raw_auth). */
    @Test
    void proxyCredentialsArePercentDecodedAsUnquoteDecodesThem() {
        assertEquals("Basic dXNlcjpwJe+8ke+8knNz", Proxies.proxyOf("http://user:p%\uff11\uff12ss@p:1").authorization());
        assertEquals("Basic dXPvv71yOnDwn5iA", Proxies.proxyOf("http://us%e9r:p%F0%9F%98%80@p:1").authorization());
        assertEquals("Basic 8J+YgDp4", Proxies.proxyOf("http://\ud83d\ude00:x@p:1").authorization());
    }

    private static Map<String, String> proxied(TestProxy proxy, String variable, String noProxy) {
        Map<String, String> environment = new java.util.LinkedHashMap<>();
        environment.put(variable, "http://us%40er:p%3Ass@127.0.0.1:" + proxy.port());
        if (noProxy != null) environment.put("NO_PROXY", noProxy);
        return environment;
    }

    @Test
    void anHttpRequestIsForwardedThroughHttpProxyWithItsCredentialsAndNoProxyBypassesIt() {
        String credentials = "Basic " + java.util.Base64.getEncoder().encodeToString("us@er:p:ss".getBytes(StandardCharsets.UTF_8));
        List<String> reached = Collections.synchronizedList(new ArrayList<>());
        try (RawHttpServer server = serving(request -> {
            reached.add(request.line() + " " + request.header("host"));
            return listing();
        }); TestProxy proxy = new TestProxy(null)) {
            try (RestTransport transport = new RestTransport(server.url() + "/jeff", TokenSource.of("tok"), 2000,
                direct().withEnvironment(proxied(proxy, "HTTP_PROXY", "example.com")))) {
                transport.functions();
                transport.functions();
            }
            assertEquals(1, proxy.seen.size());
            RawHttpServer.Request first = proxy.seen.get(0);
            assertEquals("GET http://127.0.0.1:" + server.port() + "/jeff/v1/functions HTTP/1.1", first.line());
            assertEquals(List.of(credentials, "Bearer tok", "127.0.0.1:" + server.port()),
                List.of(first.header("proxy-authorization"), first.header("authorization"), first.header("host")));
            assertEquals(2, reached.size());
            try (RestTransport bypassing = new RestTransport(server.url(), TokenSource.none(), 2000,
                direct().withEnvironment(proxied(proxy, "ALL_PROXY", "localhost, 127.0.0.1")))) {
                bypassing.functions();
            }
            assertEquals(1, proxy.seen.size());
            assertEquals(3, reached.size());
        }
    }

    @Test
    void anHttpsRequestGoesThroughAConnectTunnelWithTheServersCertificateChecked() {
        TestTls tls = TestTls.create("ip:127.0.0.1,dns:localhost");
        com.sun.net.httpserver.HttpsServer server = tls.serve(LISTING_BYTES);
        String credentials = "Basic " + java.util.Base64.getEncoder().encodeToString("us@er:p:ss".getBytes(StandardCharsets.UTF_8));
        try (TestProxy proxy = new TestProxy(null)) {
            String url = "https://127.0.0.1:" + server.getAddress().getPort();
            try (RestTransport transport = new RestTransport(url, TokenSource.none(), 5000,
                direct().withTls(tls.client()::getSocketFactory).withEnvironment(proxied(proxy, "https_proxy", null)))) {
                assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
                assertEquals(Map.of("format", "bf16"), transport.functions().get("base_format"));
            }
            assertEquals(1, proxy.seen.size());
            RawHttpServer.Request connect = proxy.seen.get(0);
            String target = "127.0.0.1:" + server.getAddress().getPort();
            assertEquals("CONNECT " + target + " HTTP/1.1", connect.line());
            assertEquals(List.of(target, "*/*", credentials),
                List.of(connect.header("host"), connect.header("accept"), connect.header("proxy-authorization")));
            try (RestTransport untrusted = new RestTransport(url, TokenSource.none(), 5000,
                direct().withEnvironment(proxied(proxy, "HTTPS_PROXY", null)))) {
                assertTrue(assertThrows(JeffError.BackendUnavailable.class, untrusted::functions).getMessage()
                    .startsWith("Cannot reach the Jeff server at " + url + ": "));
            }
        } finally {
            server.stop(0);
        }
    }

    @Test
    void aProxyThatRefusesTheTunnelOrCannotBeReachedIsReportedAsHttpxReportsIt() throws IOException {
        try (TestProxy proxy = new TestProxy("407 Proxy Authentication Required");
             RestTransport transport = new RestTransport("https://jeff.example", TokenSource.none(), 2000,
                 direct().withEnvironment(proxied(proxy, "HTTPS_PROXY", null)))) {
            assertEquals("The connection to the Jeff server at https://jeff.example failed during GET /v1/functions: ProxyError: 407 "
                + "Proxy Authentication Required", assertThrows(JeffError.BackendUnavailable.class, transport::functions).getMessage());
        }
        int closedPort;
        try (ServerSocket free = new ServerSocket(0, 1, InetAddress.getLoopbackAddress())) {
            closedPort = free.getLocalPort();
        }
        try (RestTransport transport = new RestTransport("http://jeff.example", TokenSource.none(), 2000,
            direct().withEnvironment(Map.of("http_proxy", "127.0.0.1:" + closedPort)))) {
            String message = assertThrows(JeffError.BackendUnavailable.class, transport::functions).getMessage();
            assertTrue(message.startsWith("Cannot reach the Jeff server at http://jeff.example: ConnectException: Connection refused"),
                message);
        }
    }

    @Test
    void theTestRunPinsAnEmptyProxyEnvironmentSoTheMachinesVariablesNeverReachATest() {
        assertEquals(Map.of(), HttpOptions.standard().environment());
        assertNull(Proxies.forUrl(ServerUrl.parse("http://127.0.0.1:1"), HttpOptions.standard().environment()));
        assertNull(Proxies.forUrl(ServerUrl.parse("https://127.0.0.1:1"), HttpOptions.standard().environment()));
    }
}
